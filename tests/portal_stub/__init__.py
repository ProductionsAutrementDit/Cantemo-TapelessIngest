"""AD-11 — this is the ONLY sanctioned Portal/Cantemo-dep mocking.

Injects fake `portal.*` modules (plus the non-PyPI Cantemo distributions
`VidiRest` and `RestAPIBase`, and the dead `pyxb` dependency) into
`sys.modules` so the real plugin code under this repo imports off-server.
`portal.plugins.__path__` is pointed at this repo's parent directory so
`portal.plugins.TapelessIngest.*` absolute imports resolve to the real
plugin code through the normal import machinery.

No other Portal mocking is allowed anywhere in the test tree: no
`unittest.mock.patch("portal...")`, no per-test `sys.modules` hacks, no
`monkeypatch` of `portal.*`.
"""

import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Plugin:
    """Plain-object stand-in for portal.pluginbase.core.Plugin."""


def implements(interface):
    """No-op stand-in for portal.pluginbase.core.implements."""


class QueryElasticFake:
    """Stateful stand-in for portal.search.elastic.query_elastic.

    models/folder.py binds the name at import time (`from portal.search.elastic
    import query_elastic`), so this very instance — installed into the stub
    module before any plugin import — is the object `Folder.scan` calls.

    FIFO response queue + `(first, number)` call log + per-call
    `(search_doc, doc_type)` log (so tests can pin the exact query document
    scan sends); the conftest autouse reset fixture asserts the queue was
    fully consumed and clears everything after every test so unconsumed
    responses never leak into a later test.

    `route()` installs a RESPONDER instead of the FIFO script (story 2.6):
    the fake then answers each call from the search doc it was handed, the
    way the real index does, so a tree run's results depend on WHICH
    folders the recursion chose to query rather than on the order it
    queried them in. The queue stays empty, so the autouse unconsumed-page
    guard is satisfied by construction rather than by counting.

    `route_search_after()` is story 4.1's addition: the index path pages
    with `search_after` carried IN the search doc, so a responder that can
    only see `(first, number)` cannot model it. That responder reads the
    cursor out of the doc, exactly where the real index reads it.

    `first`/`number` are deliberately required keyword arguments: Folder.scan
    always passes both explicitly, so the fake must never paper over a caller
    relying on defaults of the real query_elastic.

    `call_kwargs` records EVERY argument of every call, the `**kwargs` the
    pre-4.1 fake swallowed unrecorded included: `.calls` keeps its
    `(first, number)` tuple shape (test_scan_pagination and
    test_scan_counters depend on it) and cannot grow a third element
    without breaking those pins, so the full record lives beside it.
    """

    def __init__(self):
        self.queue = []
        self.calls = []
        self.call_docs = []
        self.call_kwargs = []
        self.responder = None

    @property
    def last_search_doc(self):
        return self.call_docs[-1][0] if self.call_docs else None

    def push(self, response):
        """Queue one raw search result dict to be returned by the next call."""
        self.queue.append(response)

    def route(self, responder):
        """Answer from the search doc instead of from the FIFO queue.

        ``responder(search_doc, first=..., number=...)`` returns the raw
        result dict. Mutually exclusive with ``push`` in practice: the
        responder wins.
        """
        self.responder = responder

    def route_search_after(self, hits):
        """Serve one ordered hit stream the way `search_after` paging does.

        `hits` are raw hit dicts in the query's sort order, each carrying
        its own `sort` values (see the `es_page` fixture's `sort=True`).
        The cursor is read from `search_doc["search_after"]` — the index
        path never uses `from`/`size` beyond page one, so `first` says
        nothing about where a page starts and only `number` bounds it.

        An unknown cursor is an AssertionError rather than an empty page:
        a page requested from a position that does not exist in the stream
        means the code under test invented one.
        """
        ordered = list(hits)

        def respond(search_doc, first, number):
            cursor = search_doc.get("search_after")
            start = 0
            if cursor is not None:
                cursor = list(cursor)
                for index, hit in enumerate(ordered):
                    if hit.get("sort") == cursor:
                        start = index + 1
                        break
                else:
                    raise AssertionError(
                        f"query_elastic fake asked for search_after={cursor!r}, "
                        f"which is not a sort value in the routed stream"
                    )
            page = ordered[start : start + number]
            return {"hits": {"total": {"value": len(ordered)}, "hits": page}}

        self.route(respond)

    def reset(self):
        self.queue.clear()
        self.calls.clear()
        self.call_docs.clear()
        self.call_kwargs.clear()
        self.responder = None

    def __call__(self, search_doc, doc_type=None, *, first, number, **kwargs):
        self.calls.append((first, number))
        self.call_docs.append((search_doc, doc_type))
        self.call_kwargs.append(
            dict(
                kwargs,
                search_doc=search_doc,
                doc_type=doc_type,
                first=first,
                number=number,
            )
        )
        if self.responder is not None:
            return self.responder(search_doc, first=first, number=number)
        if not self.queue:
            raise AssertionError(
                "query_elastic fake called with an empty response queue "
                f"(first={first}, number={number}) — push() a page first"
            )
        return self.queue.pop(0)


query_elastic_fake = QueryElasticFake()


class VSFile:
    """Functional dict-backed stand-in for VidiRest.objects.storage.VSFile.

    Mapping VERIFIED on prod (2026-08-20, synthetic instantiation of vendor
    portal/externals/VidiRest/objects/storage.pyc): the getters read `_source`
    keys path/hash/storage/id/size. `getState` is deliberately omitted (prod
    derives NOT_IMPORTED from an empty `item` list rather than passing raw
    `state` through; the pinned code paths never call it, so reaching it here
    raises AttributeError rather than mismodelling it). `replace_urls`
    (settings.VIDISPINE_REPLACE_URLS, a dict of URL-prefix rewrites; `{}` in
    Tier 2 settings) is only passed through.
    """

    def __init__(self, source, replace_urls=None):
        self._source = source
        self._replace_urls = replace_urls

    def _get(self, key):
        try:
            return self._source[key]
        except KeyError:
            raise KeyError(
                f"VSFile stub _source is missing key {key!r} "
                f"(present: {sorted(self._source)})"
            ) from None

    def getPath(self):
        return self._get("path")

    def getHash(self):
        return self._get("hash")

    def getStorage(self):
        return self._get("storage")

    def getId(self):
        return self._get("id")

    def getSize(self):
        return self._get("size")

    def getFileName(self):
        return os.path.basename(self._get("path"))

    def __str__(self):
        # Deterministic rendering so error strings built from f"{file}" are
        # exactly assertable in tests (prod's default repr embeds id()) —
        # see the caveat in tests/pinned-bugs.md.
        return self._get("path")


class FakeStorageMethod:
    """One storage-access method on a FakeStorage (browse flag + URI)."""

    def __init__(self, browse, url=None):
        self._browse = browse
        self._url = url

    def getBrowse(self):
        return self._browse

    def getFirstURI(self):
        return {"url": self._url}


class FakeStorage:
    """Opaque VS storage stand-in returned by StorageHelperFake.getStorage."""

    def __init__(self, storage_id, methods):
        self._storage_id = storage_id
        self._methods = methods

    def getId(self):
        return self._storage_id

    def getMethods(self):
        return list(self._methods)


class StorageAPIFake:
    """The ``storageapi`` attribute of StorageHelperFake (story 2.5).

    Models the ONE call the plugin makes through it: the legacy-storage
    hash lookup of ``Clip.recover_item_id``. Configured and counted
    class-side on StorageHelperFake, because plugin code builds a fresh
    ``StorageHelper()`` per call site.

    Default answer for any hash is a clean miss (``hits: 0``), so a test
    that expects ZERO lookups fails on the call counter rather than on a
    mysterious KeyError.
    """

    def getFilesInStorage(self, storage_id, query=None):
        cls = StorageHelperFake
        query = query or {}
        hashes = query.get("hash") or []
        file_hash = hashes[0] if hashes else None
        cls.get_files_in_storage_calls.append((storage_id, file_hash))
        for key in ((storage_id, file_hash), (None, file_hash)):
            if key in cls.hash_errors:
                raise VSAPIError(
                    f"getFilesInStorage failed for hash {file_hash} in "
                    f"storage {storage_id} (configured)"
                )
            if key in cls.hash_items:
                item_id = cls.hash_items[key]
                return {
                    "hits": 1,
                    "file": [
                        {
                            "id": f"{storage_id}-FILE-{file_hash}",
                            "item": [{"id": item_id}] if item_id else [],
                        }
                    ],
                }
        return {"hits": 0, "file": []}


class StorageHelperFake:
    """Configurable counting stand-in for portal.vidispine.istorage.StorageHelper
    (QueryElasticFake tradition, story 2.1).

    Plugin code instantiates it freshly per call site (with the prod-verified
    ``slug``/``user``/``runas`` keywords), so all state is CLASS-level:

    - ``set_root(storage_id, root)`` configures a storage whose ``getMethods``
      yields exactly one browse-capable method with that root URL;
    - ``set_no_browse(storage_id)`` configures a storage with no
      browse-capable method (-> ``resolve_storages`` yields
      ``root_path=None``);
    - ``set_missing(storage_id)`` makes ``getStorage`` raise NotFoundError;
    - an UNCONFIGURED id raises AssertionError naming the id — silent
      success would hide unintended storage traffic;
    - ``get_storage_calls`` counts ``getStorage`` per id (all outcomes,
      the assertion included) for the once-per-run acceptance criterion;
    - ``set_hash_item(hash, item_id, storage_id=None)`` /
      ``set_hash_error(hash, storage_id=None)`` configure the
      ``storageapi.getFilesInStorage`` hash lookup, whose every call is
      recorded in ``get_files_in_storage_calls`` — the counter story
      2.5's "zero Vidispine calls for an already-ingested clip" rests on.

    Only ``getStorage`` and ``storageapi.getFilesInStorage`` are
    implemented — any other method access still raises AttributeError,
    keeping tests off-server honest. The conftest autouse fixture resets
    the class state after every test. The story-1.3 pins bypass this fake
    entirely (preset ``_root_path``, ``context=None`` -> property fallback
    never reaches a StorageHelper).
    """

    roots = {}
    no_browse = set()
    missing = set()
    get_storage_calls = {}
    # (storage_id or None, hash) -> item id returned by the hash lookup.
    hash_items = {}
    # (storage_id or None, hash) whose lookup raises, per the try/except
    # around getFilesInStorage.
    hash_errors = set()
    get_files_in_storage_calls = []

    def __init__(self, slug=None, user=None, runas=None):
        self.slug = slug
        self.user = user
        self.runas = runas
        self.storageapi = StorageAPIFake()

    @classmethod
    def set_root(cls, storage_id, root):
        cls.roots[storage_id] = root

    @classmethod
    def set_no_browse(cls, storage_id):
        cls.no_browse.add(storage_id)

    @classmethod
    def set_missing(cls, storage_id):
        cls.missing.add(storage_id)

    @classmethod
    def set_hash_item(cls, file_hash, item_id, storage_id=None):
        """Make the hash lookup find ``item_id`` (storage_id None = any)."""
        cls.hash_items[(storage_id, file_hash)] = item_id

    @classmethod
    def set_hash_error(cls, file_hash, storage_id=None):
        """Make the hash lookup raise for this hash (storage_id None = any)."""
        cls.hash_errors.add((storage_id, file_hash))

    @classmethod
    def reset(cls):
        cls.roots.clear()
        cls.no_browse.clear()
        cls.missing.clear()
        cls.get_storage_calls.clear()
        cls.hash_items.clear()
        cls.hash_errors.clear()
        cls.get_files_in_storage_calls.clear()

    def removeFileItemRelationship(self, storage_id, file_id):
        """Detach a file from its item (the replace path's first half)."""
        VidispineFake.record(
            "removeFileItemRelationship", storage_id=storage_id, file_id=file_id
        )

    def getStorage(self, storage_id):
        cls = type(self)
        cls.get_storage_calls[storage_id] = cls.get_storage_calls.get(storage_id, 0) + 1
        if storage_id in cls.missing:
            raise NotFoundError(f"storage {storage_id} not found (configured)")
        if storage_id in cls.roots:
            return FakeStorage(
                storage_id,
                [FakeStorageMethod(browse=True, url=cls.roots[storage_id])],
            )
        if storage_id in cls.no_browse:
            return FakeStorage(storage_id, [FakeStorageMethod(browse=False)])
        raise AssertionError(
            f"StorageHelperFake.getStorage called for unconfigured storage id "
            f"{storage_id!r} — configure it via set_root/set_no_browse/"
            f"set_missing (silent success would hide unintended storage "
            f"traffic)"
        )


class InjectedVidispineFault(Exception):
    """The default fault ``VidispineFake.fail_next`` raises.

    Its own type, so a test can tell "the fault I armed" from "the plugin
    raised something of its own" — a distinction a bare ``Exception``
    cannot make.
    """


class ComponentBudgetExceeded(Exception):
    """Vidispine's ``400`` when a component overflows the declared budget.

    ``{"invalidInput": {"explanation": "No more components of that type is
    accepted", "value": "VIDEO_COMPONENT"}}``, measured 2026-08-31 by
    declaring ``video=len(extras)`` for a DEDUCIBLE anchor. It is what
    makes "just subtract one, always" wrong, so the stub has to be able
    to answer it — otherwise the conditional count could be mutated into
    a constant with the suite still green.
    """


class DuplicateImportOfAnAttachedFile(ComponentBudgetExceeded):
    """A component imported again after its file has already ATTACHED.

    A TRIPWIRE, not a Vidispine answer: what Vidispine does with a
    re-import of a file that is already on the item's shape — a
    PROMOTED shape in particular — has NOT been measured. What has been
    measured (2026-09-01) is the budget refusal a second component of
    the same type meets once the first has landed, and every plugin
    guard against re-importing an attached file exists so that this
    request is never sent at all. So the fake refuses it loudly and says
    what it is, rather than dressing an unmeasured case up as the
    measured 400. A subclass of ``ComponentBudgetExceeded`` so a test
    that only wants "refused" can stay coarse.
    """


class ImportWithoutAFileId(AssertionError):
    """An import sent with ``fileId=None``.

    Loud on purpose. ``file.getClipMainMediaFile`` builds
    ``{"file_id": None}`` for a clip with no ``file``, and the fake used
    to model that request as a full SUCCESS — it landed ``{"id": None}``
    on the shape and promoted it — so the plugin's refusal of a
    file-less anchor could be deleted with the suite still green. What
    a real Vidispine does with ``{"fileId": None}`` is not measured; a
    fake that accepts it certifies nothing either way.
    """


class UnknownShape(AssertionError):
    """A budget declared against a shape this fake never created.

    Loud on purpose: silently ignoring the declaration is exactly how the
    pre-story stub let ``updatePlaceholderComponentCount`` be a no-op, and
    a no-op contract cannot fail for the right reason.
    """


class VidispineFake:
    """Shared configuration and call log for the ingest-side helper doubles.

    ``Clip.import_file`` is the plugin's riskiest function and was, until
    now, unreachable off-server: it constructs six Portal helpers with
    prod constructor keywords the placeholders refused. So a mutation
    reverting FR-36's verdict ladder left the whole suite green.

    AD-11 forbids Portal mocking OUTSIDE this package; INSIDE it, this is
    the sanctioned place. What is modelled here is deliberately the
    narrow surface ``import_file`` drives, with every ANSWER configured
    per test — the fakes decide nothing, they only carry what a test
    said Vidispine would reply and record what was asked.

    Configure:
    - ``set_import_response(response)`` — what the next
      ``importFileToPlaceholder`` / ``doImportToPlaceholder`` returns
      (queue; a minted ``{"jobId": "VX-JOB-DEFAULT-<n>"}`` once it is
      empty — see ``next_import_response``);
    - ``set_original_shape(item_id, shape_id, files, placeholder=False)``
      — the "original" shapes ``getItemShapesFromNames`` reports for an
      item, the files on each, and whether the shape is a PLACEHOLDER
      shape; an unconfigured item has none, i.e. a bare placeholder;
    - ``set_item(item_id, item)`` — an item ``getItem`` finds;
    - ``fail_next(call_name, error=None)`` — arm a fault: the named call
      records itself, applies its effect, and then RAISES. Raising after
      the effect is the honest half — it models a process that died with
      the Vidispine write already committed and nothing written back,
      which is the only half that can orphan anything.

    Read ``calls`` for what was asked, in order.

    Threading invariant (story 3.2): faults are ARMED and state is RESET
    from the test thread only, between runs — never from a worker; under
    ``workers > 1`` the ORDER of ``calls`` is the pool's choice, which is
    why tests compare SORTED call names.
    """

    import_responses = []
    # The default answer's job id is MINTED, one per response — see
    # `next_import_response`.
    DEFAULT_JOB_ID_PREFIX = "VX-JOB-DEFAULT"
    _default_job_counter = 0
    items = {}
    item_shapes = {}
    calls = []
    faults = []
    # The COMPONENT BUDGET (see "The component budget" below): what a
    # placeholder shape was told to expect, and what has actually LANDED
    # against it — nothing in between, because Vidispine keeps nothing in
    # between (measured 2026-09-02). Before this, `doImportToPlaceholder`
    # promoted the placeholder on any `jobId`, ignored
    # `updatePlaceholderComponentCount` entirely and never let a component
    # land in a binary slot — so both production defects this story fixes
    # were structurally unobservable in the suite.
    component_jobs = {}
    file_paths = {}
    job_statuses = {}
    non_deducible_files = set()
    audio_anchor_files = set()
    hold_component_jobs = False
    hold_container_jobs = False
    settle_after_polls = 1
    stalled_jobs = set()
    _placeholder_counter = 0
    # Story 3.2: pool workers reach the two compound class-state operations
    # below (`fault_point`'s scan-then-pop, `new_placeholder_id`'s
    # read-increment-format) from up to `workers` threads at once. The GIL
    # makes a single list.append atomic, not these.
    # An RLock, not a Lock: the budget helpers below both TAKE the lock
    # and call each other (`refuse_if_budget_full` -> `_budget`/`slots_for`,
    # `promote_if_complete` -> `_budget`/`slots_for`), and every one of
    # them must be guarded — story 3.2 runs them from pool workers, and
    # the FR-4 waiver claims all new state is under `_lock`. With a plain
    # Lock, guarding them all would deadlock on the nesting; leaving the
    # inner ones unguarded is what the waiver said was not the case.
    _lock = threading.RLock()

    @classmethod
    def reset(cls):
        with cls._lock:
            cls.import_responses.clear()
            cls.items.clear()
            cls.item_shapes.clear()
            cls.calls.clear()
            cls.faults.clear()
            cls.component_jobs.clear()
            cls.file_paths.clear()
            cls.job_statuses.clear()
            cls.non_deducible_files.clear()
            cls.audio_anchor_files.clear()
            cls.hold_component_jobs = False
            cls.hold_container_jobs = False
            cls.settle_after_polls = 1
            cls.stalled_jobs.clear()
            cls._placeholder_counter = 0
            cls._default_job_counter = 0

    @classmethod
    def fail_next(cls, call_name, error=None):
        """Queue a fault for the next call of ``call_name``.

        One entry, one call: a two-run crash test arms the fault for run
        one and leaves run two clean without having to disarm anything.
        """
        with cls._lock:
            cls.faults.append((call_name, error))

    @classmethod
    def record(cls, name, **details):
        cls.calls.append((name, details))

    @classmethod
    def fault_point(cls, name):
        """Raise here if a fault is armed for ``name``.

        Called at the END of the modelled call, once its effect is in
        ``VidispineFake``'s state: the interesting failure is the one
        where Vidispine DID the thing and the caller never learned it.

        The scan-then-pop is taken under the class lock (story 3.2): under
        a worker pool several threads reach the same fault point at once,
        and an unsynchronized pop could fire one armed fault twice — or
        never — turning a one-crash test nondeterministic. The raise
        happens OUTSIDE the lock; only the claim of the fault is guarded.
        """
        armed = None
        with cls._lock:
            for index, (call_name, error) in enumerate(cls.faults):
                if call_name == name:
                    armed = cls.faults.pop(index)
                    break
        if armed is not None:
            _call_name, error = armed
            raise error or InjectedVidispineFault(
                f"injected fault: the process died inside {name}"
            )

    @classmethod
    def call_names(cls):
        return [name for name, _ in cls.calls]

    @classmethod
    def set_import_response(cls, response):
        cls.import_responses.append(response)

    @classmethod
    def next_import_response(cls):
        # Check-then-pop, same compound-operation class as `fault_point`:
        # two pool workers importing at once must not both claim (or both
        # miss) the single queued response.
        with cls._lock:
            if cls.import_responses:
                return cls.import_responses.pop(0)
            # A UNIQUE id per default answer. It was the one constant
            # `VX-JOB-DEFAULT`, and every import now registers a job
            # under its id: two pool workers importing single-component
            # clips with no queued response both registered under the
            # SAME key, so worker B's `component_jobs[...]` overwrote
            # worker A's before A settled it — A's settle landed B's
            # file, B's settle found the job already settled, and A's
            # placeholder never promoted. Sequentially the overwrite is
            # harmless, which is why only a pooled run could see it.
            cls._default_job_counter += 1
            return {"jobId": f"{cls.DEFAULT_JOB_ID_PREFIX}-{cls._default_job_counter}"}

    @classmethod
    def set_item(cls, item_id, item=None):
        cls.items[item_id] = item if item is not None else FakeItem(item_id)
        return cls.items[item_id]

    @classmethod
    def set_original_shape(cls, item_id, shape_id, files=(), placeholder=False):
        cls.item_shapes.setdefault(item_id, []).append(
            {"id": shape_id, "files": list(files), "placeholder": bool(placeholder)}
        )

    @classmethod
    def fill_placeholder_shapes(cls, item_id, files):
        """What a SUCCESSFUL import does to an item's placeholder shape.

        Vidispine attaches the file and the shape stops being a
        placeholder. Without this, every "second run after a genuine
        success" scenario is exercised against a state production never
        reaches: an item holding a placeholder shape forever, which the
        un-parameterized shape query cannot see and the FR-35 rung
        therefore never gets to judge.
        """
        for shape in cls.item_shapes.get(item_id, []):
            if shape.get("placeholder"):
                shape["placeholder"] = False
                shape["files"] = list(files)

    @classmethod
    def shapes_for(cls, item_id, placeholder=None):
        """The shape-LIST endpoint's three-state ``placeholder`` FILTER.

        Vidispine 25.4.11 (``ref/item/shape.html``) on
        ``GET /item/(id)/shape``: "``true`` - Only return placeholder
        shapes. ``false`` (default) - Only return non-placeholder shapes.
        ``all`` - Return all shapes."

        A filter, NOT an include flag — the two are the opposite mental
        model for the ``false`` case, and the plugin's retry path depends
        on the real one: the un-parameterized query at
        ``Clip.import_file`` must NOT see the placeholder shape an
        interrupted submission left, or the retry diverts into
        ``_should_replace_original_files`` and strips the item.

        (``includePlaceholder`` is a different parameter on a different
        endpoint — the single-shape retrieve — and governs which
        COMPONENTS appear inside one shape. Not modelled: nothing calls
        it.)
        """
        assert placeholder in (None, "true", "false", "all"), (
            f"unmodelled placeholder filter value {placeholder!r} — the real "
            f"endpoint defines true/false/all; silently treating anything "
            f"else as 'false' would hide a caller-side encoding bug"
        )
        shapes = cls.item_shapes.get(item_id, [])
        if placeholder == "all":
            return list(shapes)
        wanted = placeholder == "true"
        return [shape for shape in shapes if bool(shape.get("placeholder")) is wanted]

    # ---------------------------------------------------------------
    # The component budget
    # ---------------------------------------------------------------
    #
    # Vidispine promotes a placeholder shape only when every component
    # slot the placeholder was told to expect has been FILLED. Three
    # things follow, and the plugin's two defects live in all three:
    #
    # 1. `updatePlaceholderComponentCount` is a CONTRACT, not a no-op.
    #    A slot that is declared and never filled leaves the shape a
    #    placeholder for ever — no `original` tag, no transcode.
    # 2. A component that would overflow the declared budget is REFUSED
    #    with a 400 (`ComponentBudgetExceeded`).
    # 3. The anchor's own container import fills the container slot AND
    #    — only when Vidispine can deduce its essence — a video one. A
    #    source it cannot decode lands in a BINARY slot instead, which
    #    satisfies the container and fills no video slot at all.
    #    `set_non_deducible(file_id)` is how a fixture declares one.
    #
    # Files are attached when the JOB LANDS, never when the request is
    # made: the whole of defect B is the gap between the two. Promotion
    # is evaluated ONLY when the anchor's container import lands, because
    # in production that is the only job that ever evaluates it — which is
    # what makes the anchor's wait for the extras load bearing here.

    @classmethod
    def set_non_deducible(cls, *file_ids):
        """Declare source files Vidispine's shape deduction cannot read."""
        with cls._lock:
            cls.non_deducible_files.update(file_ids)

    @classmethod
    def set_audio_anchor(cls, *file_ids):
        """Declare main files whose essence is AUDIO, not video.

        A P2 clip's anchor can be an audio track, and
        `Clip._count_media_components` counts it in the AUDIO budget — so
        the fake has to be able to fill an audio slot from a container
        import, or that branch of the count could be mutated with the
        suite still green.
        """
        with cls._lock:
            cls.audio_anchor_files.update(file_ids)

    # "Leave it as it is" for `hold_jobs(settle_after_polls=...)`: distinct
    # from `None`, which is a real value meaning "never settles".
    KEEP = object()

    @classmethod
    def hold_jobs(cls, settle_after_polls=KEEP, containers=False):
        """Make extra-component jobs settle only when the test says so.

        Component imports then answer with a job that reports
        ``inProgress()`` until it has been polled ``settle_after_polls``
        times; ``None`` means never. That is what makes defect B's race
        reproducible with no threads and no clock: the anchor either
        waits for those jobs or it closes an incomplete component set.

        Every argument is STICKY within a test: `containers=True` is
        never turned back off by a later call, and `settle_after_polls`
        is changed only when the call names it. Both used to be plain
        assignments, so `hold_jobs(settle_after_polls=None)` followed by
        `hold_jobs(containers=True)` silently turned "never settles"
        into "settles after one poll" — and the reverse order turned the
        container hold off. `reset()` is what clears them, between tests.
        """
        with cls._lock:
            cls.hold_component_jobs = True
            cls.hold_container_jobs = cls.hold_container_jobs or containers
            if settle_after_polls is not cls.KEEP:
                cls.settle_after_polls = settle_after_polls

    @classmethod
    def stall_jobs(cls, *job_ids):
        """Named jobs that never settle, whatever the poll count.

        Coarser than `hold_jobs` on purpose: it is how a test builds a
        PARTIAL component set — some extras landed, one did not — which
        is the state the resume path exists for.

        A named job is stalled WHATEVER its component: `register_component_job`
        checks `stalled_jobs` independently of the container hold, and so
        does `poll_component_job`. That is why this takes no `containers`
        flag — one was here, had no caller, and could not have changed
        the outcome for the job it named.
        """
        with cls._lock:
            cls.hold_component_jobs = True
            cls.stalled_jobs.update(job_ids)

    @classmethod
    def placeholder_shape(cls, item_id):
        with cls._lock:
            for shape in cls.item_shapes.get(item_id, []):
                if shape.get("placeholder"):
                    return shape
            return None

    @classmethod
    def _budget(cls, shape):
        # An UNDECLARED budget names the container slot and NOTHING else,
        # and that is deliberate: a slot with no entry here is a slot with
        # no limit, so a shape nobody declared a budget for refuses
        # nothing. The single-component path is exactly that shape — it
        # never calls `updatePlaceholderComponentCount` — and a default of
        # `video: 0` would have it refused by the fake for a rule
        # production has never applied to it. What the container entry
        # DOES buy is promotion: one container component and the
        # placeholder is complete, which is the pre-story behaviour for
        # every single-component import.
        with cls._lock:
            return shape.setdefault("budget", {"container": 1})

    @classmethod
    def declare_component_budget(cls, item_id, shape_id, container, video, audio):
        """``updatePlaceholderComponentCount``, with the MEASURED semantics.

        Re-declaring REPLACES the declaration; it does not accumulate,
        and it does not give a consumed slot back. Measured on prod
        2026-09-01 WITH A CONTROL ARM, on throwaway items and files since
        removed: control (no re-declaration) — import A lands, import B
        is refused with the exact ``400 … VIDEO_COMPONENT``, so the
        budget is enforced and consumed; measured (re-declaring the same
        budget between the two) — B is refused too, so the declaration
        was replaced rather than added to. Hence ``budget`` is
        overwritten here while ``landed`` is deliberately left alone: a
        resume may safely re-declare, and re-declaring buys it no extra
        room.

        (An earlier attempt concluded "accumulates" and was WRONG: its
        two imports were 117 ms apart with job A still ``READY``, so it
        measured latency, not the budget.)
        """
        with cls._lock:
            for shape in cls.item_shapes.get(item_id, []):
                if shape["id"] != shape_id:
                    continue
                shape["budget"] = {
                    "container": container or 0,
                    "video": video or 0,
                    "audio": audio or 0,
                }
                return
        raise UnknownShape(
            f"updatePlaceholderComponentCount declared a budget against "
            f"{shape_id!r} on {item_id!r}, which this fake never created — "
            f"a declaration that lands nowhere cannot hold anything to it"
        )

    @classmethod
    def slots_for(cls, component, file_id):
        """The slots one import claims: container imports DEDUCE.

        An extra component fills the slot it was imported as. The main
        file fills the container slot plus whatever Vidispine's shape
        deduction extracts from it: a video slot normally, an AUDIO one
        when the anchor is an audio track (`set_audio_anchor`), and a
        BINARY one — which fills no essence slot at all — when the source
        cannot be decoded (`set_non_deducible`).
        """
        if component != "container":
            return [component]
        with cls._lock:
            if file_id in cls.non_deducible_files:
                deduced = "binary"
            elif file_id in cls.audio_anchor_files:
                deduced = "audio"
            else:
                deduced = "video"
        return ["container", deduced]

    @classmethod
    def refuse_if_budget_full(cls, item_id, component, file_id):
        """Refuse this import the way Vidispine does — at REQUEST time,
        against what has LANDED — or let it through.

        Measured, both halves. 2026-09-01: a slot is consumed when the
        component LANDS, not when the import is requested — two imports
        fired 93–117 ms apart are both accepted, and the same import sent
        again AFTER the first landed is refused with the 400 below.
        2026-09-02: a job that lands into a budget already full ATTACHES
        ANYWAY — both jobs `FINISHED`, no error, no warning, two video
        components on a shape declared for one. So there is no
        reservation to keep: this reads `landed`, nothing is written,
        and `_land_component` checks nothing. (Until 2026-09-02 this fake
        CLAIMED a slot here, which made an in-flight duplicate look like
        a 400 and manufactured the proof of a wrong resume guard.)

        A file already attached to ANY shape of the item trips
        ``DuplicateImportOfAnAttachedFile`` first, placeholder or not: a
        landing that PROMOTED the shape used to make this fake blind to
        it, because the budget check only looked at a still-placeholder
        shape, and a duplicate into a promoted shape was then silently
        accepted — precisely the request the plugin's resume guards exist
        never to send. What Vidispine itself answers there is NOT
        measured, and the tripwire's text says so instead of quoting the
        400 (``removeItemShape`` is record-only in this fake, so a shape
        removed by a test still trips it — model a removal by editing
        ``item_shapes``).
        """
        with cls._lock:
            for shape in cls.item_shapes.get(item_id, []):
                if any(entry.get("id") == file_id for entry in shape.get("files", [])):
                    raise DuplicateImportOfAnAttachedFile(
                        f"{file_id} is already attached to shape {shape['id']} of "
                        f"{item_id} (a {component} component) and was sent for "
                        f"import AGAIN — what Vidispine does with this request "
                        f"is UNMEASURED; the plugin's resume guards exist so it "
                        f"is never sent"
                    )
        shape = cls.placeholder_shape(item_id)
        if shape is None:
            return None
        # Under the lock because `_budget` is a `setdefault` — a WRITE —
        # and because the read of `landed` must not interleave with a
        # landing on another pool worker. (An RLock, so the nested
        # acquires in `_budget` and `slots_for` are free.)
        with cls._lock:
            budget = cls._budget(shape)
            landed = shape.setdefault("landed", {})
            for slot in cls.slots_for(component, file_id):
                if slot not in budget:
                    continue
                if landed.get(slot, 0) >= budget[slot]:
                    raise ComponentBudgetExceeded(
                        f'400 {{"invalidInput": {{"explanation": "No more '
                        f'components of that type is accepted", "value": '
                        f'"{slot.upper()}_COMPONENT"}}}} — {item_id} declared '
                        f"{slot}={budget[slot]}, {landed.get(slot, 0)} landed"
                    )
        return shape

    @classmethod
    def register_component_job(cls, job_id, item_id, component, file_id):
        """Record the job an import started, settling it unless held."""
        with cls._lock:
            # `or component == "container"` USED to be here, which made a
            # container job impossible to hold — so the one import that
            # must never be duplicated, the anchor, could not be put in
            # flight and the guard against re-importing it was untestable.
            # `hold_container_jobs` keeps every existing fixture's
            # behaviour (the anchor settles at once) and lets a test that
            # needs the anchor pending ask for it.
            held = cls.hold_component_jobs and (
                component != "container" or cls.hold_container_jobs
            )
            settle_now = not held and job_id not in cls.stalled_jobs
            cls.component_jobs[job_id] = {
                "item_id": item_id,
                "component": component,
                "file_id": file_id,
                "settled": False,
                "polls": 0,
            }
            # INSIDE the lock (an RLock, so the nested acquires in
            # `settle_component_job` / `_land_component` are free): with
            # the settle outside it, another worker could re-register
            # the same id between this write and the settle, and the
            # settle landed the OTHER worker's file.
            if settle_now:
                cls.settle_component_job(job_id)

    @classmethod
    def component_jobs_for_item(cls, item_id):
        """The component import jobs this fake still has for an item.

        What ``JobHelper.getAllJobsForItem`` answers, and the reason the
        RESUME path is observable at all: a run that starts while the
        previous run's component jobs are still IN FLIGHT sees those
        files missing from the shape (the job has not attached them yet)
        and would import them a second time.
        """
        with cls._lock:
            return [
                (job_id, dict(job))
                for job_id, job in cls.component_jobs.items()
                if job["item_id"] == item_id
            ]

    # The storage root a component job's source URI is built under. It
    # carries SPACES on purpose: the production root is
    # `/mnt/PAD_Storage/AA - RUSHES TAPELESS`, so `getSourceFilePath()`
    # answers a PERCENT-ENCODED `file://` URI and a comparison against
    # the raw string matches nothing — silently. A fake with a
    # space-free root could not fail that.
    JOB_SOURCE_ROOT = "/mnt/PAD_Storage/AA - RUSHES TAPELESS"

    @classmethod
    def set_file_path(cls, file_id, path):
        """The storage-relative path Vidispine knows this file by.

        The plugin's import sends only a ``fileId``, so the fake has to
        be told the path separately — exactly as Vidispine knows it and
        the plugin does not. Without it a job cannot answer
        ``getSourceFilePath()``, which is how a resume identifies the
        component a running job is importing (job DATA does not carry it;
        measured 2026-09-01 on the 6.2.1 server).
        """
        with cls._lock:
            cls.file_paths[file_id] = path

    @classmethod
    def component_job_source_uri(cls, job_id):
        """``VSJob.getSourceFilePath()``: a percent-encoded ``file://`` URI.

        ``None`` when the test never registered a path for the file —
        which is the fake's model of the production job whose source
        cannot be read, and must leave the caller on its fail-safe.
        """
        with cls._lock:
            job = cls.component_jobs.get(job_id)
            path = cls.file_paths.get(job["file_id"]) if job else None
        if not path:
            return None
        absolute = f"{cls.JOB_SOURCE_ROOT}/{str(path).lstrip('/')}"
        return "file://" + quote(absolute)

    @classmethod
    def set_job_status(cls, job_id, status):
        """The string ``VSJob.getStatus()`` answers for this job.

        The plugin decides "is this job still coming" from the STATUS,
        not from ``inProgress()`` — which on the 6.2.1 server answers
        False for ``WAITING``, an ordinary status on a busy Vidispine.
        A fake that exposed only ``inProgress()`` could never execute
        that layer, so the whole status mapping could be reverted to the
        measured-wrong reading with the suite still green.

        Unset means the fake answers no status at all, which is the OTHER
        real case: a job object from a Vidispine that did not report one,
        where the plugin falls back to ``inProgress()``.
        """
        with cls._lock:
            cls.job_statuses[job_id] = status

    @classmethod
    def job_status(cls, job_id):
        with cls._lock:
            return cls.job_statuses.get(job_id)

    @classmethod
    def component_job_item(cls, job_id):
        """``VSJob.getTargetItem()``."""
        with cls._lock:
            job = cls.component_jobs.get(job_id)
        return job["item_id"] if job else None

    @classmethod
    def poll_component_job(cls, job_id):
        """``FakeJob.inProgress()``: True until the job has settled."""
        with cls._lock:
            job = cls.component_jobs.get(job_id)
            if job is None or job["settled"]:
                return False
            job["polls"] += 1
            if job_id in cls.stalled_jobs:
                return True
            due = cls.settle_after_polls is not None and (
                job["polls"] >= cls.settle_after_polls
            )
        if due:
            cls.settle_component_job(job_id)
            return False
        return True

    @classmethod
    def settle_component_job(cls, job_id=None):
        """Land a held job's file on its shape (all of them if unnamed)."""
        job_ids = [job_id] if job_id is not None else list(cls.component_jobs)
        for one in job_ids:
            with cls._lock:
                job = cls.component_jobs.get(one)
                if job is None or job["settled"]:
                    continue
                job["settled"] = True
            cls._land_component(job)

    @classmethod
    def fail_component_job(cls, job_id):
        """A job that STOPS without attaching — Vidispine's FAILED/ABORTED.

        ``inProgress()`` answers False from here on and the file never
        reaches the shape, which is the exact shape of the mistake the
        wait must not make: "it stopped" is not "it landed".
        """
        with cls._lock:
            job = cls.component_jobs.get(job_id)
            if job is not None:
                job["settled"] = True

    @classmethod
    def _land_component(cls, job):
        """Attach the job's file to the item's placeholder shape.

        With NO placeholder shape the landing is dropped silently — the
        job still settles, nothing is attached. That is the fake's
        answer for an item nothing declared a shape on, and for a job
        that lands after the shape promoted; neither is a measured
        Vidispine behaviour, so a test that reaches here on purpose
        must assert the shape state it expects, not rely on this.
        """
        shape = cls.placeholder_shape(job["item_id"])
        if shape is None:
            return
        with cls._lock:
            landed = shape.setdefault("landed", {})
            # UNCONDITIONAL, past any budget (measured 2026-09-02): a job
            # that lands into a full budget attaches its file all the
            # same, and the shape ends over-full. Nothing here refuses.
            for slot in cls.slots_for(job["component"], job["file_id"]):
                landed[slot] = landed.get(slot, 0) + 1
            # On the SHAPE's own file list, which is what
            # `getItemShapesFromNames(...).getAllFiles()` reads: while the
            # shape is still a placeholder that list IS the production
            # state this story is about — an item holding all its media on
            # a placeholder that was never promoted.
            shape.setdefault("files", []).append(
                {"id": job["file_id"], "storage": "VX-41"}
            )
        if job["component"] == "container":
            cls.promote_if_complete(job["item_id"])

    @classmethod
    def promote_if_complete(cls, item_id):
        """The anchor's job: promote only when every slot is filled.

        An unfilled slot leaves the placeholder exactly as production
        leaves it — holding all of the clip's media, tagged nothing,
        transcoded never, and with no error anywhere. An OVER-full slot
        (a duplicate that landed, 2026-09-02) counts as filled here —
        whether Vidispine promotes an over-full shape is NOT measured;
        `>=` is the reading consistent with what was seen.
        """
        with cls._lock:
            shape = cls.placeholder_shape(item_id)
            if shape is None:
                return False
            budget = cls._budget(shape)
            landed = shape.setdefault("landed", {})
            if any(landed.get(slot, 0) < needed for slot, needed in budget.items()):
                return False
            shape["placeholder"] = False
            return True

    @classmethod
    def new_placeholder_id(cls):
        # Story 3.2: minted from up to `workers` pool threads at once. The
        # bare `+= 1` is a read-increment-write; two racing workers could
        # mint the SAME id — manufacturing, in the test stub, exactly the
        # duplicate-placeholder state NFR-1 forbids production to create.
        with cls._lock:
            cls._placeholder_counter += 1
            return f"VX-PLACEHOLDER-{cls._placeholder_counter}"


class FakeItem:
    """VS item: only the two accessors the plugin calls."""

    def __init__(self, item_id, metadata=None):
        self._item_id = item_id
        self._metadata = metadata if metadata is not None else [{}]

    def getId(self):
        return self._item_id

    def getMetadata(self):
        return self._metadata


class FakeJob:
    def __init__(self, job_id):
        self._job_id = job_id

    def getId(self):
        return self._job_id

    def inProgress(self):
        """The terminal test the anchor's wait polls.

        A job this fake never registered (the single-component import, a
        bare `getJob`) is terminal: only a HELD component job answers
        True, and only until the test lets it settle.
        """
        return VidispineFake.poll_component_job(self._job_id)

    def getStatus(self):
        """``VSJob.getStatus()`` — only when a test set one.

        ``None`` otherwise, so the ``inProgress()`` fallback stays
        reachable and both halves of the rule are exercised.
        """
        return VidispineFake.job_status(self._job_id)

    def getSourceFilePath(self):
        """``VSJob.getSourceFilePath()`` — a percent-encoded ``file://`` URI.

        This is how the resume path learns WHICH file a still-running job
        is importing. It is deliberately NOT ``getDataByKey``: measured
        on the 6.2.1 server 2026-09-01, a real ``PLACEHOLDER_IMPORT``
        job's ``data`` carries no ``sourceFileId`` and no ``fileIds`` at
        all (VX-696013 carries only ``item``; VX-696024 only
        ``errorMessage``, ``item``, ``transcodeProgress``,
        ``transcodeWallTime``), so a fake answering those keys would
        certify a lookup that can never work in production.
        """
        return VidispineFake.component_job_source_uri(self._job_id)

    def getFilename(self):
        """``VSJob.getFilename()`` — the source's basename, unencoded."""
        uri = VidispineFake.component_job_source_uri(self._job_id)
        return os.path.basename(unquote(uri)) if uri else None

    def getTargetItem(self):
        """``VSJob.getTargetItem()`` — the item the job is importing into."""
        return VidispineFake.component_job_item(self._job_id)


class FakeIngestGroup:
    def __init__(self, name="Ingest"):
        self.name = name

    def __str__(self):
        return self.name


class FakeShapeFile:
    def __init__(self, file_id, storage):
        self._file_id = file_id
        self._storage = storage

    def getId(self):
        return self._file_id

    def getStorage(self):
        return self._storage


class VSShapeFake:
    """VidiRest.objects.shape.VSShape over a plain response dict."""

    def __init__(self, response, replace_urls=None):
        self._response = response or {}
        self._replace_urls = replace_urls

    def getId(self):
        return self._response.get("id")

    def getAllFiles(self):
        return [
            FakeShapeFile(entry.get("id"), entry.get("storage"))
            for entry in self._response.get("files", [])
        ]


class VSAPIFake:
    """What ItemAPI reads off the helper's ``_vsapi``."""

    super_url = "http://vidispine.test/"
    base64string = "dGVzdDp0ZXN0"


class RestURLFake:
    """RestAPIBase.resturl.RestURL — URL building, no I/O."""

    def __init__(self, url):
        self._url = url
        self._query = {}

    def addQuery(self, query):
        self._query.update(query)

    def geturl(self):
        if not self._query:
            return self._url
        return f"{self._url}?{urlencode(self._query, doseq=True)}"


class RestTransportFake:
    """The RestAPIBase request pair, answering from VidispineFake.

    ``ItemAPIEnhanced.getItemShapeIdsFromNames`` is real plugin code that
    goes through these two functions, so they are what makes it runnable
    off-server — and what pins the URL it builds.
    """

    calls = []

    @classmethod
    def reset(cls):
        cls.calls.clear()

    @classmethod
    def prepare(cls, base64string, url, runasuser=None, return_format="json"):
        return {"url": url, "runasuser": runasuser, "return_format": return_format}

    @classmethod
    def perform(cls, url=None, runasuser=None, return_format="json", **kwargs):
        cls.calls.append(url)
        match = re.search(r"/item/([^/?]+)/shape", url or "")
        if match:
            # The query string is READ, not ignored: `placeholder` is a
            # three-state filter on this endpoint and the plugin's retry
            # path turns on its exclusivity (see VidispineFake.shapes_for).
            query = parse_qs(urlsplit(url or "").query)
            placeholder = query.get("placeholder", [None])[0]
            shapes = VidispineFake.shapes_for(match.group(1), placeholder)
            return json.dumps({"uri": [shape["id"] for shape in shapes]})
        raise AssertionError(
            f"RestTransportFake.perform called for an unmodelled URL {url!r} — "
            f"silent success would hide unintended Vidispine traffic"
        )


class _HelperFake:
    """Common constructor surface of the Portal Vidispine helpers.

    Prod builds them per call site with ``runas=``/``user=``/``slug=``;
    accepting those keywords is the whole reason this class exists.
    """

    def __init__(self, runas=None, user=None, slug=None):
        self.runas = runas
        self.user = user
        self.slug = slug
        self._vsapi = VSAPIFake()
        provide = getattr(self, "provideItemAPI", None)
        if callable(provide):
            # Prod's helpers wire their API object at construction; the
            # plugin's ItemHelperExtended / TapelessIngestHelper rely on it.
            provide()


class ItemAPIFake:
    """VidiRest.itemapi.ItemAPI — the calls the plugin makes on it."""

    def __init__(self, vsapi=None):
        self.vsapi = vsapi or VSAPIFake()

    def getItemShape(self, item_id=None, shape_id=None, runasuser=None):
        for shape in VidispineFake.item_shapes.get(item_id, []):
            if shape["id"] == shape_id:
                return shape
        return {"id": shape_id, "files": []}

    def createPlaceholderShape(self, item_id, runasuser=None):
        shape_id = f"{item_id}-SHAPE"
        VidispineFake.record("createPlaceholderShape", item_id=item_id)
        VidispineFake.set_original_shape(item_id, shape_id, placeholder=True)
        VidispineFake.fault_point("createPlaceholderShape")
        return shape_id.encode("UTF-8")

    def removeItemShape(self, item_id, shape_id, runasuser=None):
        VidispineFake.record("removeItemShape", item_id=item_id, shape_id=shape_id)

    def updatePlaceholderComponentCount(
        self, item_id, shape_id, container=None, video=None, audio=None
    ):
        VidispineFake.record(
            "updatePlaceholderComponentCount",
            item_id=item_id,
            shape_id=shape_id,
            container=container,
            video=video,
            audio=audio,
        )
        # Not a no-op: this is the CONTRACT the shape is then held to.
        VidispineFake.declare_component_budget(
            item_id, shape_id, container, video, audio
        )

    def doImportToPlaceholder(
        self,
        item_id,
        query=None,
        matrix=None,
        component="container",
        ingestprofile_groups=None,
        ignore_sidecars=False,
        runasuser=None,
        return_format="json",
    ):
        VidispineFake.record(
            "doImportToPlaceholder",
            item_id=item_id,
            component=component,
            query=query,
        )
        if not (query or {}).get("fileId"):
            raise ImportWithoutAFileId(
                f"doImportToPlaceholder on {item_id!r} was sent with no "
                f"fileId ({query!r}) — the plugin must refuse a file-less "
                f"import before sending it"
            )
        response = VidispineFake.next_import_response()
        VidispineFake.fault_point("doImportToPlaceholder")
        job_id = response.get("jobId")
        if job_id:
            # The budget check writes nothing (measured: no reservation
            # exists), so its place in the sequence is only about which
            # request the 400 is attributed to. Kept after the queued
            # response and the fault point: an import that answered
            # without a job id started nothing, and a fault models a call
            # that died — neither reached the check.
            file_id = (query or {}).get("fileId")
            VidispineFake.refuse_if_budget_full(item_id, component, file_id)
            # The file is attached by the JOB, not by the request — the
            # whole of defect B is the gap between the two. A container
            # import additionally EVALUATES the placeholder when it lands,
            # and that is the only evaluation there ever is.
            VidispineFake.register_component_job(job_id, item_id, component, file_id)
        return response


class ItemHelperFake(_HelperFake):
    """portal.vidispine.iitem.ItemHelper."""

    def getItem(self, item_id):
        VidispineFake.record("getItem", item_id=item_id)
        item = VidispineFake.items.get(item_id)
        if item is None:
            raise NotFoundError(f"item {item_id} not found (unconfigured)")
        return item

    def createPlaceholder(self, metadata_document=None, settingsprofile_id=None):
        item_id = VidispineFake.new_placeholder_id()
        VidispineFake.record("createPlaceholder", item_id=item_id)
        item = VidispineFake.set_item(item_id)
        # The item EXISTS before the fault fires: that is what makes an
        # orphan possible at all. A fault raised first would model
        # "Vidispine created nothing", the harmless half.
        VidispineFake.fault_point("createPlaceholder")
        return item

    def setItemMetadata(self, item_id, metadata_document=None):
        VidispineFake.record("setItemMetadata", item_id=item_id)

    def setItemMetadataFieldGroup(self, item_id, group_name):
        VidispineFake.record(
            "setItemMetadataFieldGroup", item_id=item_id, group=group_name
        )
        VidispineFake.fault_point("setItemMetadataFieldGroup")


class IngestHelperFake(_HelperFake):
    """portal.vidispine.iitem.IngestHelper — the single-component import."""

    def importFileToPlaceholder(
        self,
        item_id,
        file_id=None,
        ingestprofile_groups=None,
        notification_id=None,
        noTranscode=None,
        ignore_sidecars=False,
    ):
        VidispineFake.record(
            "importFileToPlaceholder", item_id=item_id, file_id=file_id
        )
        if not file_id:
            raise ImportWithoutAFileId(
                f"importFileToPlaceholder on {item_id!r} was sent with "
                f"file_id=None — the plugin must refuse a file-less import "
                f"before sending it"
            )
        response = VidispineFake.next_import_response()
        job_id = response.get("jobId")
        if job_id:
            # A successful import attaches the file and the placeholder
            # shape becomes a real one. Modelling that is what lets a
            # SECOND run be exercised against the state production really
            # reaches.
            #
            # Through the SAME two phases as `doImportToPlaceholder`, and
            # not by attaching the file inline: the request starts a job,
            # the JOB attaches the file, and the gap between the two is
            # where the single-component resume lives. Attaching inline
            # made a container job from this path impossible to hold, so
            # `hold_jobs(containers=True)` could not put the ONE import
            # that must never be duplicated — the anchor of a one-file
            # clip — in flight, and the guard against re-importing it was
            # unobservable end to end. An unheld job settles inside
            # `register_component_job`, which lands the file and promotes
            # the shape exactly as before: every existing fixture sees
            # the same state it always did.
            #
            # BEFORE the fault point, deliberately, and unlike
            # `doImportToPlaceholder`: this call's fault models a process
            # that died with the Vidispine write already committed, which
            # is the only half that can orphan anything.
            VidispineFake.refuse_if_budget_full(item_id, "container", file_id)
            VidispineFake.register_component_job(job_id, item_id, "container", file_id)
        VidispineFake.fault_point("importFileToPlaceholder")
        return response


class JobHelperFake(_HelperFake):
    def getJob(self, job_id):
        VidispineFake.record("getJob", job_id=job_id)
        return FakeJob(job_id)

    def getAllJobsForItem(self, item_id, job_type=None, max_hits=0):
        """The item's import jobs — the RESUME path's other input.

        ``job_type``/``max_hits`` are the real signature
        (``portal.vidispine.ijob.JobHelper``); accepting them is what
        lets the plugin filter on ``PLACEHOLDER_IMPORT`` at all. A job
        that has SETTLED is gone from the answer only in the sense that
        it reports itself terminal — it is still listed, exactly as
        Vidispine lists finished jobs, and it is ``inProgress()`` that
        decides, never the listing.
        """
        VidispineFake.record("getAllJobsForItem", item_id=item_id, job_type=job_type)
        jobs = [
            FakeJob(job_id)
            for job_id, _details in VidispineFake.component_jobs_for_item(item_id)
        ]
        if max_hits > 0:
            jobs = jobs[:max_hits]
        return jobs


class GroupHelperFake(_HelperFake):
    def getUserIngestGroups(self):
        group = FakeIngestGroup()
        return [group], group


class UserHelperFake(_HelperFake):
    def getUserSettingsProfile(self, basegroup=None):
        # `runas` is recorded so tests can prove the profile was resolved
        # AS THE RUN'S USER — story 3.1's per-call UserHelper(runas=user)
        # fix in `import_file` is invisible without it (the shared
        # class-definition-time default carried runas=None).
        VidispineFake.record(
            "getUserSettingsProfile", runas=self.runas, basegroup=basegroup
        )
        return "VX-6"


class CollectionHelperFake(_HelperFake):
    def getCollection(self, collection_id):
        VidispineFake.record("getCollection", collection_id=collection_id)
        return {"id": collection_id}

    def createCollection(self, collection_name=None, settingsprofile_id=None):
        # `collection_name=`, not `name=`: record()'s first positional IS
        # `name` (the call name), and the old kwarg collided with it —
        # a TypeError no test ever hit because nothing executed this
        # method until story 3.1's collection-lock tests.
        VidispineFake.record("createCollection", collection_name=collection_name)
        return FakeItem(f"VX-COLLECTION-{collection_name}")

    def addCollectionToCollection(self, parent_id, collection_id):
        VidispineFake.record(
            "addCollectionToCollection", parent=parent_id, child=collection_id
        )

    def addItemToCollection(self, collection_id, item_id):
        VidispineFake.record(
            "addItemToCollection", collection_id=collection_id, item_id=item_id
        )
        VidispineFake.fault_point("addItemToCollection")


def create_metadata_document_fake(metadata, groups=None):
    """VidiRest.helpers.vidispine.createMetadataDocumentFromDict."""
    return {"metadata": metadata, "groups": groups}


def create_merged_metadata_document_fake(md, custom_metadata, mode):
    """VidiRest.helpers.vidispine.createMergedBatchItemMetadataDocument."""
    return {"merged": md, "onto": custom_metadata, "mode": mode}


class SignalFake:
    """Recording stand-in for a Django signal (portal.vidispine.signals).

    ``Clip._import_multi_component`` fires ``vidispine_pre_ingest`` /
    ``vidispine_post_ingest`` on its way to the job-id check story 2.5
    fixed (FR-36), so the FR-36 pin cannot be written without them.
    Recording rather than raising: prod's ``send`` returns the receivers'
    results and has no other effect on the caller, and a recorded call is
    still not a silent one.
    """

    def __init__(self, name):
        self.name = name
        self.calls = []
        self.receivers = []

    def send(self, sender=None, **kwargs):
        self.calls.append((sender, kwargs))
        return [(receiver, receiver(**kwargs)) for receiver in self.receivers]

    def connect(self, receiver, **kwargs):
        """Register a receiver, as ``django.dispatch.Signal.connect`` does.

        Recording it is the point: ``plistner`` connects at import time and
        the plugin went years with that module never imported, so "was a
        receiver actually registered" is the thing a test has to be able
        to ask.
        """
        self.receivers.append(receiver)


class InvalidateItemCacheFake:
    """Recording no-op for portal.items.cache.invalidate_item_cache.

    Same reason as SignalFake: it sits on the multi-component import path
    before the job-id check. Prod's version drops cache entries and
    returns nothing, so recording the item ids models it faithfully.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, item_id):
        self.calls.append(item_id)


vidispine_pre_ingest = SignalFake("vidispine_pre_ingest")
vidispine_post_ingest = SignalFake("vidispine_post_ingest")
vidispine_post_delete = SignalFake("vidispine_post_delete")
invalidate_item_cache_fake = InvalidateItemCacheFake()


def _stub_class(name):
    return type(name, (), {"__doc__": f"portal_stub placeholder for {name}"})


def _stub_exception(name):
    return type(name, (Exception,), {"__doc__": f"portal_stub placeholder for {name}"})


class ClientResponse:
    """The two attributes plugin code reads off a ``portal.api.client`` answer."""

    def __init__(self, status_code, data):
        self.status_code = status_code
        self.data = data


class ClientFake:
    """``portal.api.client`` double — AD-11's sanctioned home for it.

    Default behavior is unchanged from the refusing placeholder: calling
    ``client.put`` with no responder installed raises, exactly like every
    other unmodelled Portal surface. A test that needs the v2 search
    surface (story 3.1: ``get_collection_from_path``'s collection lookup)
    installs one with ``route_put`` and the conftest autouse reset clears
    it after every test.
    """

    put_responder = None

    @classmethod
    def route_put(cls, responder):
        cls.put_responder = responder

    @classmethod
    def reset(cls):
        cls.put_responder = None


def _client_put(*args, **kwargs):
    if ClientFake.put_responder is None:
        raise NotImplementedError(
            "portal.api.client.put is a portal_stub placeholder and must not "
            "be called in off-server tests (AD-11) — install a responder "
            "with ClientFake.route_put(...) first"
        )
    return ClientFake.put_responder(*args, **kwargs)


def _stub_callable(qualname):
    def _stub(*args, **kwargs):
        raise NotImplementedError(
            f"{qualname} is a portal_stub placeholder and must not be called "
            f"in off-server tests (AD-11)"
        )

    _stub.__name__ = qualname.rsplit(".", 1)[-1]
    _stub.__qualname__ = qualname
    return _stub


# Cantemo-only exception types (must be real Exception subclasses: plugin
# code has `except NotFoundError:` etc. at runtime).
NotFoundError = _stub_exception("NotFoundError")
VSAPIError = _stub_exception("VSAPIError")
RestAPIBaseComError = _stub_exception("RestAPIBaseComError")

# Dotted module name -> attributes it must export (the exact import surface
# of the plugin code, per the story's Code Map). None = plain module/package.
_MODULES = {
    "portal": {},
    "portal.pluginbase": {},
    "portal.pluginbase.core": {"Plugin": Plugin, "implements": implements},
    "portal.generic": {},
    "portal.generic.plugin_interfaces": {
        "IPluginURL": _stub_class("IPluginURL"),
        "IPluginBlock": _stub_class("IPluginBlock"),
        "IAppRegister": _stub_class("IAppRegister"),
    },
    # views.py's Portal base classes and permission decorator. Import-time
    # surface only: the sweep proves the module loads, it does not serve
    # requests.
    "portal.generic.baseviews": {
        "CView": _stub_class("CView"),
        "ClassView": _stub_class("ClassView"),
    },
    "portal.generic.decorators": {
        "isAdminPermission": _stub_class("isAdminPermission"),
    },
    "portal.search": {},
    "portal.search.elastic": {"query_elastic": query_elastic_fake},
    "portal.api": {},
    "portal.api.client": {
        "get": _stub_callable("portal.api.client.get"),
        "post": _stub_callable("portal.api.client.post"),
        "put": _client_put,
        "delete": _stub_callable("portal.api.client.delete"),
    },
    "portal.api.v2": {},
    "portal.api.v2.utils": {
        "format_datetime": _stub_callable("portal.api.v2.utils.format_datetime")
    },
    "portal.vidispine": {},
    "portal.vidispine.signals": {
        "vidispine_pre_ingest": vidispine_pre_ingest,
        "vidispine_post_ingest": vidispine_post_ingest,
        "vidispine_post_delete": vidispine_post_delete,
    },
    "portal.vidispine.ijob": {"JobHelper": JobHelperFake},
    "portal.vidispine.iitem": {
        "ItemHelper": ItemHelperFake,
        "IngestHelper": IngestHelperFake,
    },
    "portal.vidispine.icollection": {"CollectionHelper": CollectionHelperFake},
    "portal.vidispine.igroup": {"GroupHelper": GroupHelperFake},
    "portal.vidispine.istorage": {"StorageHelper": StorageHelperFake},
    "portal.vidispine.iuser": {"UserHelper": UserHelperFake},
    "portal.vidispine.iexception": {
        "handleRestAPIError": _stub_callable(
            "portal.vidispine.iexception.handleRestAPIError"
        ),
        "NotFoundError": NotFoundError,
        "VSAPIError": VSAPIError,
    },
    "portal.vidispine.igeneral": {
        "performVSAPICall": _stub_callable("portal.vidispine.igeneral.performVSAPICall")
    },
    "portal.items": {},
    "portal.items.cache": {"invalidate_item_cache": invalidate_item_cache_fake},
    "portal.utils": {},
    "portal.utils.templatetags": {},
    "portal.utils.templatetags.vidispinetags": {
        "getJobStatusLabel": _stub_callable(
            "portal.utils.templatetags.vidispinetags.getJobStatusLabel"
        ),
        "getJobTypeLabel": _stub_callable(
            "portal.utils.templatetags.vidispinetags.getJobTypeLabel"
        ),
    },
    "portal.utils.templatetags.datetimeformatting": {
        "datetimeobject": _stub_class("datetimeobject")
    },
    "portal.plugins": {},
    "VidiRest": {},
    "VidiRest.itemapi": {"ItemAPI": ItemAPIFake},
    "VidiRest.objects": {},
    "VidiRest.objects.storage": {"VSFile": VSFile},
    "VidiRest.objects.shape": {"VSShape": VSShapeFake},
    "VidiRest.helpers": {},
    "VidiRest.helpers.vidispine": {
        "createMetadataDocumentFromDict": create_metadata_document_fake,
        "createMergedBatchItemMetadataDocument": create_merged_metadata_document_fake,
    },
    "RestAPIBase": {},
    "RestAPIBase.resturl": {"RestURL": RestURLFake},
    "RestAPIBase.utility": {
        "perform_request": RestTransportFake.perform,
        "prepare_request": RestTransportFake.prepare,
        "RestAPIBaseComError": RestAPIBaseComError,
    },
    "pyxb": {},
    "pyxb.utils": {},
}


# Top-level distributions this package stubs; a real, already-imported one
# must never be silently overwritten.
_STUBBED_ROOTS = ("portal", "VidiRest", "RestAPIBase", "pyxb")


def install():
    """Idempotently seed sys.modules with the stub modules.

    Must run before any django/plugin import (the conftest owns the ordering).
    """
    assert REPO_ROOT.name == "TapelessIngest", (
        f"portal_stub expects the repo directory to be named 'TapelessIngest' "
        f"(portal.plugins.__path__ resolution depends on it), got {REPO_ROOT}"
    )

    # Clobber check FIRST — even on a repeat call, a real distribution that
    # appeared since the first install must be reported, never overwritten.
    for root in _STUBBED_ROOTS:
        existing = sys.modules.get(root)
        if existing is not None and not getattr(existing, "__portal_stub__", False):
            raise RuntimeError(
                f"real {root!r} already imported; refusing to stub (AD-11: "
                f"off-server tests must never mask a live Portal environment)"
            )

    already = sys.modules.get("portal")
    if already is not None and getattr(already, "__portal_stub__", False):
        log.debug("portal_stub already installed; skipping")
        return

    for dotted, attrs in _MODULES.items():
        module = ModuleType(dotted)
        module.__portal_stub__ = True
        # Every non-leaf stub is a package; empty __path__ means the import
        # machinery never searches the filesystem for stubbed submodules.
        module.__path__ = []
        for attr_name, value in attrs.items():
            setattr(module, attr_name, value)
        sys.modules[dotted] = module

    # Wire each stub module onto its parent package, so attribute access like
    # `pyxb.utils` after `import pyxb.utils` works exactly as for real packages.
    # Missing parents (a future _MODULES edit skipping one) are auto-created
    # along the FULL dotted chain rather than KeyError-ing at install.
    def _ensure_module(dotted):
        module = sys.modules.get(dotted)
        if module is None:
            module = ModuleType(dotted)
            module.__portal_stub__ = True
            module.__path__ = []
            sys.modules[dotted] = module
            if "." in dotted:
                parent_name, _, child_name = dotted.rpartition(".")
                setattr(_ensure_module(parent_name), child_name, module)
        return module

    for dotted in _MODULES:
        if "." in dotted:
            parent_name, _, child_name = dotted.rpartition(".")
            setattr(_ensure_module(parent_name), child_name, sys.modules[dotted])

    # The one real path: `portal.plugins.TapelessIngest` must resolve to this
    # repo (its directory is literally named TapelessIngest under the parent).
    sys.modules["portal.plugins"].__path__ = [str(REPO_ROOT.parent)]

    log.debug("portal_stub installed (%d stub modules)", len(_MODULES))

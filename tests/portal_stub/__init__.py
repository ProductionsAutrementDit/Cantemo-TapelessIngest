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
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlencode, urlsplit

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

    `first`/`number` are deliberately required keyword arguments: Folder.scan
    always passes both explicitly, so the fake must never paper over a caller
    relying on defaults of the real query_elastic.
    """

    def __init__(self):
        self.queue = []
        self.calls = []
        self.call_docs = []
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

    def reset(self):
        self.queue.clear()
        self.calls.clear()
        self.call_docs.clear()
        self.responder = None

    def __call__(self, search_doc, doc_type=None, *, first, number, **kwargs):
        self.calls.append((first, number))
        self.call_docs.append((search_doc, doc_type))
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
      (queue; ``default_import_response`` once it is empty);
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
    """

    import_responses = []
    default_import_response = {"jobId": "VX-JOB-DEFAULT"}
    items = {}
    item_shapes = {}
    calls = []
    faults = []
    _placeholder_counter = 0

    @classmethod
    def reset(cls):
        cls.import_responses.clear()
        cls.items.clear()
        cls.item_shapes.clear()
        cls.calls.clear()
        cls.faults.clear()
        cls._placeholder_counter = 0

    @classmethod
    def fail_next(cls, call_name, error=None):
        """Queue a fault for the next call of ``call_name``.

        One entry, one call: a two-run crash test arms the fault for run
        one and leaves run two clean without having to disarm anything.
        """
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
        """
        for index, (call_name, error) in enumerate(cls.faults):
            if call_name == name:
                cls.faults.pop(index)
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
        if cls.import_responses:
            return cls.import_responses.pop(0)
        return dict(cls.default_import_response)

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

    @classmethod
    def new_placeholder_id(cls):
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
            video=video,
            audio=audio,
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
        response = VidispineFake.next_import_response()
        if response.get("jobId") and component == "container":
            VidispineFake.fill_placeholder_shapes(
                item_id, [{"id": "VX-IMPORTED-FILE", "storage": "VX-41"}]
            )
        VidispineFake.fault_point("doImportToPlaceholder")
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
        response = VidispineFake.next_import_response()
        if response.get("jobId"):
            # A successful import attaches the file and the placeholder
            # shape becomes a real one. Modelling that is what lets a
            # SECOND run be exercised against the state production really
            # reaches (see VidispineFake.fill_placeholder_shapes).
            VidispineFake.fill_placeholder_shapes(
                item_id, [{"id": file_id, "storage": "VX-41"}]
            )
        VidispineFake.fault_point("importFileToPlaceholder")
        return response


class JobHelperFake(_HelperFake):
    def getJob(self, job_id):
        VidispineFake.record("getJob", job_id=job_id)
        return FakeJob(job_id)

    def getAllJobsForItem(self, item_id):
        VidispineFake.record("getAllJobsForItem", item_id=item_id)
        return []


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

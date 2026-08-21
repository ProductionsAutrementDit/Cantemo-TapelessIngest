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

import logging
import os
import sys
from pathlib import Path
from types import ModuleType

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

    `first`/`number` are deliberately required keyword arguments: Folder.scan
    always passes both explicitly, so the fake must never paper over a caller
    relying on defaults of the real query_elastic.
    """

    def __init__(self):
        self.queue = []
        self.calls = []
        self.call_docs = []

    @property
    def last_search_doc(self):
        return self.call_docs[-1][0] if self.call_docs else None

    def push(self, response):
        """Queue one raw search result dict to be returned by the next call."""
        self.queue.append(response)

    def reset(self):
        self.queue.clear()
        self.calls.clear()
        self.call_docs.clear()

    def __call__(self, search_doc, doc_type=None, *, first, number, **kwargs):
        self.calls.append((first, number))
        self.call_docs.append((search_doc, doc_type))
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

    def send(self, sender=None, **kwargs):
        self.calls.append((sender, kwargs))
        return []


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
invalidate_item_cache_fake = InvalidateItemCacheFake()


def _stub_class(name):
    return type(name, (), {"__doc__": f"portal_stub placeholder for {name}"})


def _stub_exception(name):
    return type(name, (Exception,), {"__doc__": f"portal_stub placeholder for {name}"})


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
    "portal.search": {},
    "portal.search.elastic": {"query_elastic": query_elastic_fake},
    "portal.api": {},
    "portal.api.client": {
        "get": _stub_callable("portal.api.client.get"),
        "post": _stub_callable("portal.api.client.post"),
        "put": _stub_callable("portal.api.client.put"),
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
    },
    "portal.vidispine.ijob": {"JobHelper": _stub_class("JobHelper")},
    "portal.vidispine.iitem": {
        "ItemHelper": _stub_class("ItemHelper"),
        "IngestHelper": _stub_class("IngestHelper"),
    },
    "portal.vidispine.icollection": {
        "CollectionHelper": _stub_class("CollectionHelper")
    },
    "portal.vidispine.igroup": {"GroupHelper": _stub_class("GroupHelper")},
    "portal.vidispine.istorage": {"StorageHelper": StorageHelperFake},
    "portal.vidispine.iuser": {"UserHelper": _stub_class("UserHelper")},
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
    "VidiRest.itemapi": {"ItemAPI": _stub_class("ItemAPI")},
    "VidiRest.objects": {},
    "VidiRest.objects.storage": {"VSFile": VSFile},
    "VidiRest.objects.shape": {"VSShape": _stub_class("VSShape")},
    "VidiRest.helpers": {},
    "VidiRest.helpers.vidispine": {
        "createMetadataDocumentFromDict": _stub_callable(
            "VidiRest.helpers.vidispine.createMetadataDocumentFromDict"
        ),
        "createMergedBatchItemMetadataDocument": _stub_callable(
            "VidiRest.helpers.vidispine.createMergedBatchItemMetadataDocument"
        ),
    },
    "RestAPIBase": {},
    "RestAPIBase.resturl": {"RestURL": _stub_class("RestURL")},
    "RestAPIBase.utility": {
        "perform_request": _stub_callable("RestAPIBase.utility.perform_request"),
        "prepare_request": _stub_callable("RestAPIBase.utility.prepare_request"),
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

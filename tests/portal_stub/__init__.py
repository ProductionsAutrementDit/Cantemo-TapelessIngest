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
import sys
from pathlib import Path
from types import ModuleType

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Plugin:
    """Plain-object stand-in for portal.pluginbase.core.Plugin."""


def implements(interface):
    """No-op stand-in for portal.pluginbase.core.implements."""


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
    "portal.search.elastic": {
        "query_elastic": _stub_callable("portal.search.elastic.query_elastic")
    },
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
    "portal.vidispine.signals": {},
    "portal.vidispine.ijob": {"JobHelper": _stub_class("JobHelper")},
    "portal.vidispine.iitem": {
        "ItemHelper": _stub_class("ItemHelper"),
        "IngestHelper": _stub_class("IngestHelper"),
    },
    "portal.vidispine.icollection": {
        "CollectionHelper": _stub_class("CollectionHelper")
    },
    "portal.vidispine.igroup": {"GroupHelper": _stub_class("GroupHelper")},
    "portal.vidispine.istorage": {"StorageHelper": _stub_class("StorageHelper")},
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
    "portal.items.cache": {
        "invalidate_item_cache": _stub_callable(
            "portal.items.cache.invalidate_item_cache"
        )
    },
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
    "VidiRest.objects.storage": {"VSFile": _stub_class("VSFile")},
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


def install():
    """Idempotently seed sys.modules with the stub modules.

    Must run before any django/plugin import (the conftest owns the ordering).
    """
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
    for dotted in _MODULES:
        if "." in dotted:
            parent_name, _, child_name = dotted.rpartition(".")
            setattr(sys.modules[parent_name], child_name, sys.modules[dotted])

    # The one real path: `portal.plugins.TapelessIngest` must resolve to this
    # repo (its directory is literally named TapelessIngest under the parent).
    sys.modules["portal.plugins"].__path__ = [str(REPO_ROOT.parent)]

    log.debug("portal_stub installed (%d stub modules)", len(_MODULES))

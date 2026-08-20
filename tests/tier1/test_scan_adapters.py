"""Tier 1 (story 2.1): scan/adapters.py through the counting StorageHelper fake.

resolve_storages/build_context/build_default_context and the provider's
context-first ``get_file_absolute_path``. Storage ids are unique per test so
the Django ``storage:{id}`` cache (populated by the property chain) can
never couple tests.
"""

import pytest

from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.providers.providers import Provider
from portal.plugins.TapelessIngest.scan.adapters import (
    build_context,
    build_default_context,
    resolve_storages,
)
from tests.portal_stub import VSFile

PATH = "2026/AH_20260101_adapters"


def _options():
    return dict(
        user=None,
        dry_run=True,
        providers=None,
        legacy_storages=[],
        replace=False,
    )


def _vsfile(storage_id, path="2026/AH_x/CLIP.fake"):
    return VSFile(
        {"path": path, "hash": "h", "storage": storage_id, "id": "F-1", "size": 1}
    )


def test_resolve_storages_maps_each_unique_id_once(storage_fake):
    storage_fake.set_root("VX-RES-A", "/mnt/a")
    storage_fake.set_no_browse("VX-RES-B")

    storages = resolve_storages(["VX-RES-A", "VX-RES-B", "VX-RES-A"])

    assert set(storages) == {"VX-RES-A", "VX-RES-B"}
    assert storages["VX-RES-A"].root_path == "/mnt/a"
    assert storages["VX-RES-A"].storage is not None
    # Storage exists but has no browse-capable method -> rootless info.
    assert storages["VX-RES-B"].root_path is None
    # One getStorage per UNIQUE id, duplicates included in the input.
    assert storage_fake.get_storage_calls == {"VX-RES-A": 1, "VX-RES-B": 1}


def test_resolve_storages_not_found_yields_rootless_info(storage_fake):
    storage_fake.set_missing("VX-RES-GONE")

    storages = resolve_storages(["VX-RES-GONE"])

    assert storages["VX-RES-GONE"].root_path is None
    assert storages["VX-RES-GONE"].storage is None
    assert storage_fake.get_storage_calls == {"VX-RES-GONE": 1}


def test_resolve_storages_survives_arbitrary_getstorage_failure(storage_fake):
    # An UNCONFIGURED id makes the fake raise AssertionError — a stand-in
    # for any non-NotFoundError resolution failure. resolve_storages must
    # log and degrade to a rootless info (per-folder "Cannot get full
    # path" errors downstream), never crash before the scan starts.
    storages = resolve_storages(["VX-RES-BOOM"])

    assert storages["VX-RES-BOOM"].root_path is None
    assert storages["VX-RES-BOOM"].storage is None
    assert storage_fake.get_storage_calls == {"VX-RES-BOOM": 1}


def test_build_context_wires_storages_and_options(storage_fake):
    storage_fake.set_root("VX-BC-1", "/mnt/bc")
    user = object()

    ctx = build_context(
        ["VX-BC-1"],
        user=user,
        dry_run=True,
        providers=["faketest"],
        legacy_storages=["VX-2"],
        replace=True,
    )

    assert ctx.root_path_for("VX-BC-1") == "/mnt/bc"
    assert ctx.options.user is user
    assert ctx.options.dry_run is True
    assert ctx.options.providers == ["faketest"]
    assert ctx.options.legacy_storages == ["VX-2"]
    assert ctx.options.replace is True
    with pytest.raises(TypeError):
        ctx.storages["VX-BC-2"] = None


def test_build_default_context_prefers_memo_without_storage_deref(storage_fake):
    folder = Folder(storage_id="VX-DC-MEMO", path=PATH)
    folder._root_path = "/memo-root"

    ctx = build_default_context(folder, **_options())

    assert ctx.root_path_for("VX-DC-MEMO") == "/memo-root"
    # Lazy contract: folder.storage was never dereferenced.
    assert storage_fake.get_storage_calls == {}


def test_build_default_context_falls_back_to_property_chain(storage_fake):
    storage_fake.set_root("VX-DC-PROP", "/prop-root")
    folder = Folder(storage_id="VX-DC-PROP", path=PATH)

    ctx = build_default_context(folder, **_options())

    assert ctx.root_path_for("VX-DC-PROP") == "/prop-root"
    assert storage_fake.get_storage_calls == {"VX-DC-PROP": 1}


def test_build_default_context_preserves_pin5_attributeerror(storage_fake):
    # storage=None, no memo: the property chain raises exactly as today
    # (pin #5, tests/pinned-bugs.md — fixed in 2.6, not here).
    folder = Folder(storage_id=None, path=PATH)

    with pytest.raises(AttributeError, match="_root_path"):
        build_default_context(folder, **_options())


def test_scan_with_ctx_miss_falls_back_to_property_chain(
    es_fake, storage_fake, fake_provider
):
    # Unresolvable storage in the ctx (root None) -> property-chain
    # fallback -> today's AttributeError; the index is never queried.
    storage_fake.set_no_browse("VX-CTX-UNRES")
    ctx = build_context(
        ["VX-CTX-UNRES"],
        user=None,
        dry_run=True,
        providers=[fake_provider.machine_name],
        legacy_storages=[],
        replace=False,
    )
    folder = Folder(storage_id="VX-CTX-UNRES", path=PATH)

    with pytest.raises(AttributeError, match="_root_path"):
        folder.scan(number=0, count_only=True, context=ctx)

    assert es_fake.calls == []


def test_provider_absolute_path_from_ctx_zero_storage_calls(storage_fake):
    storage_fake.set_root("VX-PROV-CTX", "/mnt/prov")
    ctx = build_context(["VX-PROV-CTX"], **_options())
    assert storage_fake.get_storage_calls == {"VX-PROV-CTX": 1}

    path = Provider().get_file_absolute_path(
        _vsfile("VX-PROV-CTX"), {"scan_context": ctx}
    )

    assert path == "/mnt/prov/2026/AH_x/CLIP.fake"
    # Zero ADDITIONAL storage HTTP calls beyond the one per-run resolution.
    assert storage_fake.get_storage_calls == {"VX-PROV-CTX": 1}


def test_provider_absolute_path_legacy_resolution_without_ctx(storage_fake):
    storage_fake.set_root("VX-PROV-LEG", "/mnt/leg")

    provider = Provider()
    assert (
        provider.get_file_absolute_path(_vsfile("VX-PROV-LEG"))
        == "/mnt/leg/2026/AH_x/CLIP.fake"
    )
    assert (
        provider.get_file_absolute_path(_vsfile("VX-PROV-LEG"), {"folder": None})
        == "/mnt/leg/2026/AH_x/CLIP.fake"
    )
    # Legacy per-call resolution: one getStorage per call, exactly as today.
    assert storage_fake.get_storage_calls == {"VX-PROV-LEG": 2}


def test_provider_absolute_path_ctx_miss_uses_legacy_fallback(storage_fake):
    storage_fake.set_root("VX-PROV-MISS", "/mnt/miss")
    storage_fake.set_no_browse("VX-PROV-OTHER")
    ctx = build_context(["VX-PROV-OTHER"], **_options())

    path = Provider().get_file_absolute_path(
        _vsfile("VX-PROV-MISS"), {"scan_context": ctx}
    )

    assert path == "/mnt/miss/2026/AH_x/CLIP.fake"
    assert storage_fake.get_storage_calls["VX-PROV-MISS"] == 1


def test_provider_legacy_fallback_raises_on_unresolvable_root(storage_fake):
    # Review-ruled: the legacy fallback must fail loudly (AttributeError,
    # the pre-2.1 failure point) rather than hand None to the callers'
    # os.path.dirname.
    storage_fake.set_no_browse("VX-PROV-NB")

    with pytest.raises(AttributeError, match="browse-capable"):
        Provider().get_file_absolute_path(_vsfile("VX-PROV-NB"))

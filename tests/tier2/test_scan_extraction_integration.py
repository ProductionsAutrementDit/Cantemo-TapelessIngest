"""Tier 2: the provider registry and extraction phase inside Folder.scan.

Sibling to test_scan_counters.py, which stays byte-unmodified. Covered
here, end to end through a real ``scan()``:

- every applicable provider runs and merges, including one returning a
  fresh dict rather than mutating in place;
- the pre-filter, spy-counted: a provider is never invoked for a file it
  cannot claim — and the card providers, whose guard is sidecar presence
  rather than the extension, keep every file they claim today;
- hits are processed in ``_source["path"]`` order whatever order the
  index returns them in;
- the degraded paths (no registry, empty extension map) behave like an
  unfiltered run rather than starving every file;
- the provider context dict spans the whole invocation, so a sidecar
  resolved on page 1 is not resolved again on page 2;
- sidecar probes go through the scan's directory listings: a listed
  directory costs no extra scandir, an unlisted parent costs exactly one,
  and a listings-less context behaves like ``os.path.isfile``.

ORM needed: Clip.get_or_new and folder.save hit the DB.
"""

import os

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider
from portal.plugins.TapelessIngest.scan.context import (
    RunOptions,
    ScanContext,
    StorageInfo,
)
from portal.plugins.TapelessIngest.scan.extraction import build_extension_map
from portal.plugins.TapelessIngest.scan.verification import FolderListings

STORAGE_ID = "VX-41"

MUTATING_NAME = "fakemutating"
FRESH_NAME = "fakefresh"
MOV_NAME = "fakemov"
SIDECAR_NAME = "fakesidecar"


def _source(path, file_id):
    return {
        "path": path,
        "hash": f"hash-{file_id}",
        "storage": STORAGE_ID,
        "id": file_id,
        "size": 1024,
    }


def _folder(tmp_path, rel_path):
    folder = Folder(storage_id=STORAGE_ID, path=rel_path)
    # Presetting _root_path bypasses StorageHelper/cache entirely.
    folder._root_path = str(tmp_path)
    return folder


class SpyProvider:
    """Records every file it is invoked for; mutates metadatas in place."""

    def __init__(self, machine_name, extensions):
        self.machine_name = machine_name
        self._extensions = extensions
        self.seen_paths = []

    def getExtensions(self):
        return self._extensions

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        self.seen_paths.append(media_file.getPath())
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas


class FreshDictProvider:
    """Returns a fresh dict instead of mutating metadatas in place."""

    def __init__(self, machine_name, extensions):
        self.machine_name = machine_name
        self._extensions = extensions
        self.seen_paths = []

    def getExtensions(self):
        return self._extensions

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        self.seen_paths.append(media_file.getPath())
        return {"contributed_by": self.machine_name, "clipname": "from-fresh-dict"}


class SidecarProvider(BaseProvider):
    """Probes a same-directory AND a parent-directory sidecar."""

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "Fake Sidecar Provider"
        self.machine_name = SIDECAR_NAME
        self.probes = []
        self.parsed = []

    def getExtensions(self):
        return [".fake"]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        filename, _ = os.path.splitext(media_file.getFileName())
        media_absolute_path = self.get_file_absolute_path(media_file, context)
        media_dirname = os.path.dirname(media_absolute_path)
        same_dir_sidecar = os.path.join(media_dirname, filename + "M01.XML")
        # Deliberately un-normalized, exactly like xdcam's MEDIAPRO probe.
        parent_sidecar = os.path.join(media_dirname, "../MEDIAPRO.XML")
        same_dir_hit = self.probe_is_file(same_dir_sidecar, context)
        # Parse-once-per-invocation cache in the provider context, the
        # shape xdcam uses for MEDIAPRO.XML.
        cache = context.setdefault("sidecar_cache", {})
        if parent_sidecar not in cache:
            cache[parent_sidecar] = self.probe_is_file(parent_sidecar, context)
            if cache[parent_sidecar]:
                self.parsed.append(parent_sidecar)
        self.probes.append((same_dir_hit, cache[parent_sidecar]))
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas


@pytest.fixture
def registered_providers():
    """Register test providers in the Clip._PROVIDER_CACHE seam."""
    providers = {
        MUTATING_NAME: SpyProvider(MUTATING_NAME, [".fake"]),
        FRESH_NAME: FreshDictProvider(FRESH_NAME, [".fake"]),
        MOV_NAME: SpyProvider(MOV_NAME, [".mov"]),
        SIDECAR_NAME: SidecarProvider(),
    }
    Clip._PROVIDER_CACHE.update(providers)
    yield providers
    for name in providers:
        Clip._PROVIDER_CACHE.pop(name, None)


def _count_scandirs(monkeypatch, root):
    """Forwarding os.scandir counter scoped to paths under `root`."""
    root = str(root)
    calls = []
    real_scandir = os.scandir

    def counting_scandir(path, *args, **kwargs):
        fs_path = os.fspath(path)
        if isinstance(fs_path, str) and fs_path.startswith(root):
            calls.append(fs_path)
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    return calls


# --------------------------------------------------------------------------
# Two applicable providers, both merged
# --------------------------------------------------------------------------


def test_two_applicable_providers_both_run_and_merge(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    rel = "2026/AH_20260101_merge"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPMERGE.fake").write_bytes(b"clip data")

    es_fake.push(es_page([_source(f"{rel}/CLIPMERGE.fake", "VX-41-MERGE")], total=1))

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=[MUTATING_NAME, FRESH_NAME])

    assert response["errors"] == []
    assert response["processed"] == 1
    [clip] = response["clips"]

    mutating = registered_providers[MUTATING_NAME]
    fresh = registered_providers[FRESH_NAME]
    # Both were invoked — the loop never breaks on first match.
    assert mutating.seen_paths == [f"{rel}/CLIPMERGE.fake"]
    assert fresh.seen_paths == [f"{rel}/CLIPMERGE.fake"]
    # Both contributions survive; the FRESH dict is merged, not dropped.
    assert clip.metadatas["provider"] == MUTATING_NAME
    assert clip.metadatas["umid"] == f"{rel}/CLIPMERGE"
    assert clip.metadatas["contributed_by"] == FRESH_NAME
    # Later keys win: the fresh dict's clipname is the surviving one.
    assert clip.metadatas["clipname"] == "from-fresh-dict"


def test_registry_order_decides_which_key_wins(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    # Same two providers, reversed: the mutating one now runs LAST, so its
    # clipname-free contribution no longer sits behind the fresh dict's.
    rel = "2026/AH_20260101_order"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPORDER.fake").write_bytes(b"clip data")

    es_fake.push(es_page([_source(f"{rel}/CLIPORDER.fake", "VX-41-ORDER")], total=1))

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=[FRESH_NAME, MUTATING_NAME])

    [clip] = response["clips"]
    assert clip.metadatas["provider"] == MUTATING_NAME
    assert clip.metadatas["contributed_by"] == FRESH_NAME
    assert clip.metadatas["clipname"] == "from-fresh-dict"


# --------------------------------------------------------------------------
# The pre-filter, spy-counted
# --------------------------------------------------------------------------


def test_only_suffix_applicable_providers_are_invoked(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    rel = "2026/AH_20260101_prefilter"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPA.fake").write_bytes(b"fake clip")
    (tmp_path / rel / "CLIPB.mov").write_bytes(b"mov clip")

    es_fake.push(
        es_page(
            [
                _source(f"{rel}/CLIPA.fake", "VX-41-A"),
                _source(f"{rel}/CLIPB.mov", "VX-41-B"),
            ],
            total=2,
        )
    )

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=[MUTATING_NAME, MOV_NAME])

    assert response["errors"] == []
    assert response["processed"] == 2
    # Each provider saw ONLY the files whose suffix it declares.
    assert registered_providers[MUTATING_NAME].seen_paths == [f"{rel}/CLIPA.fake"]
    assert registered_providers[MOV_NAME].seen_paths == [f"{rel}/CLIPB.mov"]


def test_zero_applicable_providers_errors_with_no_umid(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    rel = "2026/AH_20260101_noapplicable"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPX.zzz").write_bytes(b"unknown suffix")

    es_fake.push(es_page([_source(f"{rel}/CLIPX.zzz", "VX-41-X")], total=1))

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=[MUTATING_NAME])

    # No provider invoked at all...
    assert registered_providers[MUTATING_NAME].seen_paths == []
    # ...so the file falls out through the "No UMID" path and today's
    # per-file error wrapper, still counted as processed.
    assert response["clips"] == []
    assert response["processed"] == 1
    assert response["errors"] == [
        f"Error scanning file {rel}/CLIPX.zzz: "
        f"No UMID found in file {rel}/CLIPX.zzz"
    ]


# --------------------------------------------------------------------------
# Deterministic per-page iteration order
# --------------------------------------------------------------------------


def test_hits_are_processed_in_path_order(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    rel = "2026/AH_20260101_sorted"
    (tmp_path / rel / "SUB").mkdir(parents=True)
    for name in ("AAA.fake", "MMM.fake"):
        (tmp_path / rel / name).write_bytes(b"clip data")
    (tmp_path / rel / "SUB" / "ZZZ.fake").write_bytes(b"clip data")

    # Index order is deliberately NOT path order.
    es_fake.push(
        es_page(
            [
                _source(f"{rel}/SUB/ZZZ.fake", "VX-41-Z"),
                _source(f"{rel}/MMM.fake", "VX-41-M"),
                _source(f"{rel}/AAA.fake", "VX-41-A"),
            ],
            total=3,
        )
    )

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=[MUTATING_NAME])

    expected = [f"{rel}/AAA.fake", f"{rel}/MMM.fake", f"{rel}/SUB/ZZZ.fake"]
    assert registered_providers[MUTATING_NAME].seen_paths == expected
    assert [clip.umid for clip in response["clips"]] == [
        os.path.splitext(path)[0] for path in expected
    ]


# --------------------------------------------------------------------------
# Sidecar probes through context["listings"]
# --------------------------------------------------------------------------


def test_sidecar_probes_reuse_the_batched_listings(
    migrated_db, es_fake, es_page, registered_providers, tmp_path, monkeypatch
):
    rel = "2026/AH_20260101_sidecar"
    clip_dir = tmp_path / rel
    clip_dir.mkdir(parents=True)
    for name in ("CLIP1", "CLIP2"):
        (clip_dir / f"{name}.fake").write_bytes(b"clip data")
        # Same-directory sidecar: already covered by the verification listing.
        (clip_dir / f"{name}M01.XML").write_bytes(b"<xml/>")
    # Parent-directory sidecar: outside the listed directory.
    (clip_dir.parent / "MEDIAPRO.XML").write_bytes(b"<xml/>")

    es_fake.push(
        es_page(
            [
                _source(f"{rel}/CLIP1.fake", "VX-41-1"),
                _source(f"{rel}/CLIP2.fake", "VX-41-2"),
            ],
            total=2,
        )
    )

    scandir_calls = _count_scandirs(monkeypatch, tmp_path)

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=[SIDECAR_NAME])

    assert response["errors"] == []
    assert response["processed"] == 2
    # Both probes answered True for both files.
    assert registered_providers[SIDECAR_NAME].probes == [(True, True), (True, True)]
    # Exactly two scandirs for the whole page: the clip directory (once,
    # for verification + the same-dir sidecars) and the parent directory
    # (once, lazily, for the ../MEDIAPRO.XML probes of BOTH files).
    assert sorted(scandir_calls) == [str(clip_dir.parent), str(clip_dir)]


def test_absent_sidecars_contribute_nothing_and_raise_nothing(
    migrated_db, es_fake, es_page, registered_providers, tmp_path, monkeypatch
):
    rel = "2026/AH_20260101_nosidecar"
    clip_dir = tmp_path / rel
    clip_dir.mkdir(parents=True)
    (clip_dir / "CLIPBARE.fake").write_bytes(b"clip data")
    # No CLIPBAREM01.XML, no ../MEDIAPRO.XML.

    es_fake.push(es_page([_source(f"{rel}/CLIPBARE.fake", "VX-41-BARE")], total=1))

    scandir_calls = _count_scandirs(monkeypatch, tmp_path)

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=[SIDECAR_NAME])

    assert response["errors"] == []
    assert response["processed"] == 1
    assert registered_providers[SIDECAR_NAME].probes == [(False, False)]
    # The absent parent sidecar still costs exactly one lazy scandir of
    # the parent directory, cached for the rest of the invocation.
    assert sorted(scandir_calls) == [str(clip_dir.parent), str(clip_dir)]


def test_probe_falls_back_to_os_path_without_listings(tmp_path):
    """A legacy, listings-less context behaves exactly like today."""
    provider = SidecarProvider()
    present = tmp_path / "PRESENT.XML"
    present.write_bytes(b"<xml/>")
    absent = tmp_path / "ABSENT.XML"

    for context in (None, {}, {"folder": None}):
        assert provider.probe_is_file(str(present), context) is True
        assert provider.probe_is_file(str(absent), context) is False


# --------------------------------------------------------------------------
# The superset rule, end to end: a card provider must keep its clips
# --------------------------------------------------------------------------

XDCAM_SIDECAR = b"""<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta>
  <TargetMaterial umidRef="XDCAM-UMID-C0001"/>
  <Duration value="250"/>
  <CreationDate value="2026-01-01T10:00:00+01:00"/>
  <Device manufacturer="Sony" modelName="PXW-Z750" serialNo="12345"/>
</NonRealTimeMeta>
"""


@pytest.mark.parametrize("clip_name", ["C0001.wav", "C0001.mov"])
def test_card_provider_keeps_files_outside_its_declared_suffixes(
    migrated_db, es_fake, es_page, tmp_path, clip_name
):
    """A .wav/.mov inside an XDCAM structure is xdcam's, not `file`'s.

    xdcam declares only [".mxf", ".mp4"] but its runtime guard is sidecar
    presence, which is extension-agnostic. If the pre-filter bounded it
    by suffix, `file` would claim this clip and the umid would flip from
    the card's umidRef to an ffprobe/hash value — a new primary key and a
    duplicate ingest on the next run. The REAL xdcam provider runs here,
    against a real sidecar.
    """
    rel = "2026/AH_20260101_card/XDROOT/Clip"
    clip_dir = tmp_path / rel
    clip_dir.mkdir(parents=True)
    (clip_dir / clip_name).write_bytes(b"media payload")
    (clip_dir / "C0001M01.XML").write_bytes(XDCAM_SIDECAR)

    es_fake.push(es_page([_source(f"{rel}/{clip_name}", "VX-41-CARD")], total=1))

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=["xdcam", "file"])

    assert response["errors"] == []
    [clip] = response["clips"]
    assert clip.metadatas["provider"] == "xdcam"
    assert clip.umid == "XDCAM-UMID-C0001"
    # `file` never claimed it, so no ffprobe hash umid.
    assert clip.metadatas["umid"] != f"hash-VX-41-CARD"


def test_card_provider_declines_without_its_sidecar(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    # The mirror image: always-applicable does NOT mean always-claiming.
    # With no sidecar, xdcam contributes nothing and the next applicable
    # provider wins, exactly as before the pre-filter existed.
    rel = "2026/AH_20260101_nocard"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "LOOSE.fake").write_bytes(b"media payload")

    es_fake.push(es_page([_source(f"{rel}/LOOSE.fake", "VX-41-LOOSE")], total=1))

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=["xdcam", MUTATING_NAME])

    assert response["errors"] == []
    [clip] = response["clips"]
    assert clip.metadatas["provider"] == MUTATING_NAME


# --------------------------------------------------------------------------
# The degraded-registry fallbacks are reachable and equivalent
# --------------------------------------------------------------------------


def _page_sources(rel):
    return [
        _source(f"{rel}/CLIPA.fake", "VX-41-FA"),
        _source(f"{rel}/CLIPB.fake", "VX-41-FB"),
    ]


def _bare_context(root_path, providers):
    """A ScanContext with the registry seams left unfilled."""
    return ScanContext(
        storages={STORAGE_ID: StorageInfo(id=STORAGE_ID, root_path=str(root_path))},
        options=RunOptions(providers=providers, legacy_storages=[]),
        provider_registry=None,
        extension_map=None,
    )


def test_scan_without_a_registry_matches_the_registry_backed_run(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    rel = "2026/AH_20260101_noregistry"
    (tmp_path / rel).mkdir(parents=True)
    for name in ("CLIPA.fake", "CLIPB.fake"):
        (tmp_path / rel / name).write_bytes(b"clip data")

    es_fake.push(es_page(_page_sources(rel), total=2))
    registry_backed = _folder(tmp_path, rel).scan(providers=[MUTATING_NAME])

    # Same folder path replayed: reset the rows the first scan persisted.
    Clip.objects.all().delete()
    Folder.objects.all().delete()
    registered_providers[MUTATING_NAME].seen_paths.clear()

    es_fake.push(es_page(_page_sources(rel), total=2))
    ctx = _bare_context(tmp_path, [MUTATING_NAME])
    assert ctx.provider_registry is None and ctx.extension_map is None
    degraded = _folder(tmp_path, rel).scan(context=ctx)

    assert [c.umid for c in degraded["clips"]] == [
        c.umid for c in registry_backed["clips"]
    ]
    for key in ("hits", "processed", "created", "already_ingested", "errors"):
        assert degraded[key] == registry_backed[key], key


def test_scan_with_an_empty_extension_map_does_not_starve_files(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    """An empty map is falsy but NOT None — it must take the same fallback.

    Guarding on `is None` would hand every file zero applicable providers
    and fail the whole page with "No UMID found".
    """
    rel = "2026/AH_20260101_emptymap"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPE.fake").write_bytes(b"clip data")

    provider = registered_providers[MUTATING_NAME]
    empty_map = build_extension_map(())
    assert empty_map is not None and not empty_map

    ctx = ScanContext(
        storages={STORAGE_ID: StorageInfo(id=STORAGE_ID, root_path=str(tmp_path))},
        options=RunOptions(providers=[MUTATING_NAME], legacy_storages=[]),
        provider_registry=(provider,),
        extension_map=empty_map,
    )

    es_fake.push(es_page([_source(f"{rel}/CLIPE.fake", "VX-41-E")], total=1))
    response = _folder(tmp_path, rel).scan(context=ctx)

    assert response["errors"] == []
    assert [c.umid for c in response["clips"]] == [f"{rel}/CLIPE"]
    assert provider.seen_paths == [f"{rel}/CLIPE.fake"]


# --------------------------------------------------------------------------
# The provider context spans the whole invocation, not one page
# --------------------------------------------------------------------------


def test_provider_context_cache_survives_the_page_boundary(
    migrated_db, es_fake, es_page, registered_providers, tmp_path
):
    """Sidecar work done on page 1 is not repeated on page 2.

    The provider context dict is built once per scan() invocation; if it
    were rebuilt per page, every provider cache in it (xdcam's parsed
    MEDIAPRO.XML, for one) would be thrown away at each page boundary and
    re-done.
    """
    rel = "2026/AH_20260101_ctxpages"
    clip_dir = tmp_path / rel
    clip_dir.mkdir(parents=True)
    (clip_dir.parent / "MEDIAPRO.XML").write_bytes(b"<xml/>")

    page1 = []
    for i in range(100):
        name = f"P1_{i:03d}.fake"
        (clip_dir / name).write_bytes(b"clip data")
        page1.append(_source(f"{rel}/{name}", f"VX-41-P1-{i:03d}"))
    page2 = []
    for name in ("P2_A.fake", "P2_B.fake"):
        (clip_dir / name).write_bytes(b"clip data")
        page2.append(_source(f"{rel}/{name}", f"VX-41-{name}"))

    es_fake.push(es_page(page1, total=102))
    es_fake.push(es_page(page2, total=102))

    response = _folder(tmp_path, rel).scan(providers=[SIDECAR_NAME], number=0)

    assert es_fake.calls == [(0, 100), (100, 100)]
    assert response["processed"] == 102
    assert response["errors"] == []
    # The shared parent sidecar was resolved ONCE for all 102 files across
    # both pages — the cache lives in the invocation-wide context dict.
    assert registered_providers[SIDECAR_NAME].parsed == [
        os.path.join(str(clip_dir), "../MEDIAPRO.XML")
    ]
    assert all(
        probe == (False, True) for probe in registered_providers[SIDECAR_NAME].probes
    )


# --------------------------------------------------------------------------
# probe_is_file contract
# --------------------------------------------------------------------------


def test_relative_probe_with_listings_is_reported_not_silently_resolved(
    tmp_path, monkeypatch, caplog
):
    """A relative probe cannot use the listings (they refuse relative paths).

    It must be surfaced rather than silently answered against the process
    CWD, and it must not raise out of the provider either.
    """
    provider = SidecarProvider()
    (tmp_path / "SIDE.XML").write_bytes(b"<xml/>")
    monkeypatch.chdir(tmp_path)

    context = {"listings": FolderListings()}
    assert provider.probe_is_file("SIDE.XML", context) is True

    assert any(
        record.levelname == "ERROR" and "relative sidecar probe" in record.message
        for record in caplog.records
    ), caplog.text
    # And the listings cache was never asked to normalize it.
    assert context["listings"].errors() == {}

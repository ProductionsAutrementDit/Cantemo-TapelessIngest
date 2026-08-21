"""Tier 2 (story 2.3): registry v2 + the extraction phase inside Folder.scan.

Sibling to test_scan_counters.py (which stays byte-unmodified). Here:

- FR-32 end-to-end: two applicable providers both run, in registry order,
  and the second one's FRESH-dict return is MERGED rather than dropped —
  the pre-2.3 loop kept only in-place mutations;
- the FR-13 pre-filter, spy-counted: a provider whose declared suffixes
  do not match a file is never invoked for it;
- deterministic per-page iteration: hits are processed in
  ``_source["path"]`` order regardless of the index's order;
- FR-16 sidecar probes through ``context["listings"]``: a same-directory
  sidecar costs ZERO extra scandir (the directory was already listed for
  verification), a parent-directory sidecar lazily scandirs that
  directory ONCE and caches it across files and pages, an absent sidecar
  simply contributes nothing, and a context without listings falls back
  to ``os.path.isfile`` — today's behavior exactly.

ORM needed: Clip.get_or_new and folder.save hit the DB.
"""

import os

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider

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
    """Returns a FRESH dict — the AD-7 contribution pre-2.3 threw away."""

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
    """Probes a same-directory AND a parent-directory sidecar (FR-16)."""

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "Fake Sidecar Provider"
        self.machine_name = SIDECAR_NAME
        self.probes = []

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
        self.probes.append(
            (
                self.probe_is_file(same_dir_sidecar, context),
                self.probe_is_file(parent_sidecar, context),
            )
        )
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
# FR-32: two applicable providers, both merged
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
# FR-13 pre-filter, spy-counted
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
# Deterministic per-page iteration order (FR-4)
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
# FR-16 sidecar probes through context["listings"]
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

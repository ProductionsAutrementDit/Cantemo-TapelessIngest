"""Tier 2 (story 2.2): batched verification wired into Folder.scan.

Sibling to test_scan_counters.py (which stays byte-unmodified and proves
the CLIPGONE error strings end-to-end). Here:

- a single page spans the folder dir plus a subdir, and monkeypatch
  counters — scoped to paths under the tmp storage root so incidental
  library calls elsewhere never break them — prove exactly one
  os.scandir per unique directory and zero per-file os.path.exists
  (the batching AC), with today's response shape intact;
- the provider context dict's "listings" key is load-bearing: the
  FakeProvider records it, and it IS the single FolderListings instance
  the scan created;
- a two-page run (number=0 path) proves the cache lifetime spans all
  pages of one scan invocation: directories shared across pages are
  scandir'd once.

FakeProvider matches by extension only (.fake, no sub-paths), so subdir
files just need to be in the es_page hits and on the tmp-root disk.
"""

import os

from portal.plugins.TapelessIngest.models import folder as folder_module
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.scan.verification import FolderListings

STORAGE_ID = "VX-41"

SCAN_KEYS = {"clips", "hits", "errors", "created", "already_ingested", "processed"}


def _source(path, file_id):
    return {
        "path": path,
        "hash": f"hash-{file_id}",
        "storage": STORAGE_ID,
        "id": file_id,
        "size": 1024,
    }


def _install_counters(monkeypatch, root):
    """Forwarding os.scandir / os.path.exists counters scoped to `root`."""
    root = str(root)
    scandir_calls = []
    real_scandir = os.scandir

    def counting_scandir(path, *args, **kwargs):
        fs_path = os.fspath(path)
        if isinstance(fs_path, str) and fs_path.startswith(root):
            scandir_calls.append(fs_path)
        return real_scandir(path, *args, **kwargs)

    exists_calls = []
    real_exists = os.path.exists

    def counting_exists(path, *args, **kwargs):
        try:
            fs_path = os.fspath(path)
        except TypeError:
            fs_path = path
        if isinstance(fs_path, str) and fs_path.startswith(root):
            exists_calls.append(fs_path)
        return real_exists(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    monkeypatch.setattr(os.path, "exists", counting_exists)
    return scandir_calls, exists_calls


def _install_recording_listings(monkeypatch):
    """Record every FolderListings instance Folder.scan constructs."""
    created = []

    class RecordingListings(FolderListings):
        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(folder_module, "FolderListings", RecordingListings)
    return created


def test_multi_directory_page_scans_once_per_directory(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, monkeypatch
):
    rel = "2026/AH_20260101_batched"
    (tmp_path / rel / "SUBDIR").mkdir(parents=True)
    (tmp_path / rel / "CLIPTOP.fake").write_bytes(b"top clip data")
    (tmp_path / rel / "SUBDIR" / "CLIPSUB.fake").write_bytes(b"sub clip data")

    es_fake.push(
        es_page(
            [
                _source(f"{rel}/CLIPTOP.fake", "VX-41-TOP"),
                _source(f"{rel}/SUBDIR/CLIPSUB.fake", "VX-41-SUB"),
            ],
            total=2,
        )
    )

    scandir_calls, exists_calls = _install_counters(monkeypatch, tmp_path)
    created_listings = _install_recording_listings(monkeypatch)

    folder = Folder(storage_id=STORAGE_ID, path=rel)
    folder._root_path = str(tmp_path)
    response = folder.scan(providers=[fake_provider.machine_name])

    # Exactly one scandir per unique directory on the page: 2 dirs, 2 calls.
    assert sorted(scandir_calls) == [
        str(tmp_path / rel),
        str(tmp_path / rel / "SUBDIR"),
    ]
    # Zero per-file os.path.exists under the storage root (all files
    # present, no symlinks): every verification was a pure membership hit.
    assert exists_calls == []

    # The "listings" key is load-bearing: scan built exactly one
    # FolderListings and handed that same instance to the provider.
    assert len(created_listings) == 1
    assert fake_provider.seen_listings == [created_listings[0]] * 2
    assert fake_provider.seen_listings[0] is created_listings[0]

    # Response shape and counters identical to the unbatched behavior.
    assert set(response.keys()) == SCAN_KEYS
    assert response["hits"] == 2
    assert response["processed"] == 2
    assert response["created"] == 2
    assert response["errors"] == []
    assert [clip.umid for clip in response["clips"]] == [
        f"{rel}/CLIPTOP",
        f"{rel}/SUBDIR/CLIPSUB",
    ]


def test_cache_lifetime_spans_all_pages_of_one_scan(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, monkeypatch
):
    # number=0 drives scan's own pagination: a full 100-hit page forces a
    # second query. Files on page 2 live in the SAME directories as page
    # 1's — the listings cache must answer them without new scandirs.
    rel = "2026/AH_20260101_paged"
    (tmp_path / rel / "SUBDIR").mkdir(parents=True)

    page1_sources = []
    for i in range(100):
        name = f"CLIP{i:03d}.fake"
        (tmp_path / rel / name).write_bytes(b"clip data")
        page1_sources.append(_source(f"{rel}/{name}", f"VX-41-P1-{i:03d}"))

    page2_sources = []
    for name in ["LATE1.fake", "LATE2.fake"]:
        (tmp_path / rel / name).write_bytes(b"late clip data")
        page2_sources.append(_source(f"{rel}/{name}", f"VX-41-{name}"))
    (tmp_path / rel / "SUBDIR" / "LATE3.fake").write_bytes(b"late sub data")
    page2_sources.append(_source(f"{rel}/SUBDIR/LATE3.fake", "VX-41-LATE3"))

    es_fake.push(es_page(page1_sources, total=103))
    es_fake.push(es_page(page2_sources, total=103))

    scandir_calls, exists_calls = _install_counters(monkeypatch, tmp_path)

    folder = Folder(storage_id=STORAGE_ID, path=rel)
    folder._root_path = str(tmp_path)
    response = folder.scan(number=0, providers=[fake_provider.machine_name])

    # Both ES pages were consumed by ONE scan invocation...
    assert es_fake.calls == [(0, 100), (100, 100)]
    assert response["processed"] == 103
    assert response["errors"] == []
    # ...and the listings cache spanned them: one scandir per unique
    # directory across the whole run, zero per-file exists.
    assert sorted(scandir_calls) == [
        str(tmp_path / rel),
        str(tmp_path / rel / "SUBDIR"),
    ]
    assert exists_calls == []

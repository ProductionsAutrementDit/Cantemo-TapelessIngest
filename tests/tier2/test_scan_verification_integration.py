"""Tier 2 (story 2.2): batched verification wired into Folder.scan.

Sibling to test_scan_counters.py (which stays byte-unmodified and proves
the CLIPGONE error strings end-to-end): here a single page spans the
folder dir plus a subdir, and monkeypatch counters prove exactly one
os.scandir per unique directory and zero os.path.exists calls under the
storage root (the batching AC), with today's response shape intact.

FakeProvider matches by extension only (.fake, no sub-paths), so the
subdir file just needs to be in the es_page hits and on the tmp-root
disk.
"""

import os

from portal.plugins.TapelessIngest.models.folder import Folder

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

    scandir_calls = []
    real_scandir = os.scandir

    def counting_scandir(path, *args, **kwargs):
        scandir_calls.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    exists_calls = []
    real_exists = os.path.exists

    def counting_exists(path, *args, **kwargs):
        exists_calls.append(os.fspath(path))
        return real_exists(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    monkeypatch.setattr(os.path, "exists", counting_exists)

    folder = Folder(storage_id=STORAGE_ID, path=rel)
    folder._root_path = str(tmp_path)
    response = folder.scan(providers=[fake_provider.machine_name])

    # Exactly one scandir per unique directory on the page: 2 dirs, 2 calls.
    assert sorted(scandir_calls) == [
        str(tmp_path / rel),
        str(tmp_path / rel / "SUBDIR"),
    ]
    # Zero per-file os.path.exists under the storage root (no symlinks here):
    # the batched membership test replaced every per-file stat.
    assert [p for p in exists_calls if str(p).startswith(str(tmp_path))] == []

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

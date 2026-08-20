"""Tier 2 pins for Folder.scan: counters, persistence, frozen NFR-5 key sets.

ORM needed: Clip.get_or_new and folder.save hit the DB. Files live under a
real tmp root because scan's per-file os.path.exists check is real-FS and
deliberate. Every scan/ingest passes providers=[fake] (providers=None would
fall back to the real PROVIDERS_LIST → ffprobe).
"""

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder

STORAGE_ID = "VX-41"

# Frozen NFR-5 key sets — never change without human sign-off.
SCAN_KEYS = {"clips", "hits", "errors", "created", "already_ingested", "processed"}
INGEST_KEYS = SCAN_KEYS | {"ingested", "skipped", "failed", "replaced"}


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


def test_single_page_scan_counters(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    rel = "2026/AH_20260101_counters"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPNEW.fake").write_bytes(b"new clip data")
    (tmp_path / rel / "CLIPOLD.fake").write_bytes(b"old clip data")
    # CLIPGONE.fake is deliberately absent from disk (index/filesystem desync).

    # Pre-existing umid: the FakeProvider derives umid from the full storage
    # path with the extension stripped.
    Clip(umid=f"{rel}/CLIPOLD").save()

    es_fake.push(
        es_page(
            [
                _source(f"{rel}/CLIPNEW.fake", "VX-41-NEW"),
                _source(f"{rel}/CLIPOLD.fake", "VX-41-OLD"),
                _source(f"{rel}/CLIPGONE.fake", "VX-41-GONE"),
            ],
            total=3,
        )
    )

    folder = _folder(tmp_path, rel)
    response = folder.scan(providers=[fake_provider.machine_name])

    # (0, 25): scan forwards its OWN first/number defaults explicitly on
    # every query_elastic call — the fake accepts no defaults for them.
    assert es_fake.calls == [(0, 25)]
    assert set(response.keys()) == SCAN_KEYS
    assert response["hits"] == 3
    assert response["processed"] == 3
    assert response["created"] == 1
    # Ledger bug pinned as-is (do not fix, tests/pinned-bugs.md):
    # already_ingested counts every successfully processed clip, because
    # get_clip_from_file always sets clip.file (clip.py:332) — not only
    # clips already known to the index.
    assert response["already_ingested"] == 2
    gone_abs = tmp_path / rel / "CLIPGONE.fake"
    # Caveat: the exact wording after "Error scanning file {path}: " partly
    # reflects the stub VSFile.__str__ (the path); prod's default repr would
    # embed an object address there. The pinned production part is the
    # "Error scanning file {path}: ..." template and the swallow-and-continue
    # behavior.
    assert response["errors"] == [
        f"Error scanning file {rel}/CLIPGONE.fake: "
        f"File {rel}/CLIPGONE.fake does not exist ({gone_abs})"
    ]
    assert [clip.umid for clip in response["clips"]] == [
        f"{rel}/CLIPNEW",
        f"{rel}/CLIPOLD",
    ]

    # Ledger quirk pinned as-is (tests/pinned-bugs.md): a "scan" is not
    # read-only — it persists provider_names and scanned_on on the folder.
    saved = Folder.objects.get(storage_id=STORAGE_ID, path=rel)
    assert saved.provider_names == fake_provider.machine_name
    assert saved.scanned_on is not None


def test_ingest_dry_run_key_superset(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    rel = "2026/AH_20260101_ingestkeys"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPKEYS.fake").write_bytes(b"clip data")

    es_fake.push(es_page([_source(f"{rel}/CLIPKEYS.fake", "VX-41-KEYS")], total=1))

    folder = _folder(tmp_path, rel)
    response = folder.ingest(dry_run=True, providers=[fake_provider.machine_name])

    # ingest adds exactly ingested/skipped/failed/replaced to the scan keys.
    assert set(response.keys()) == INGEST_KEYS
    assert (
        response["ingested"],
        response["skipped"],
        response["failed"],
        response["replaced"],
    ) == (0, 0, 0, 0)
    assert response["processed"] == 1
    assert response["errors"] == []

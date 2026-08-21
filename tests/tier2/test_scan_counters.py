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
    # path with the extension stripped. It carries an item_id so the honest
    # already_ingested counter is actually exercised — without one the
    # expected value would be 0 and the assertion would pin nothing.
    Clip(umid=f"{rel}/CLIPOLD", item_id="VX-100").save()

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
    # Ledger row #1, FIXED by story 2.5 (deleted from tests/pinned-bugs.md;
    # declared in tests/fr4-waivers.md). already_ingested is now item_id
    # presence (FR-23): CLIPOLD counts, the brand-new CLIPNEW does not.
    # Pre-2.5 the counter reported 2 — every clip the page processed,
    # because attach_file_metadatas always sets clip.file.
    assert response["already_ingested"] == 1
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

    # Ledger row #4, FIXED by story 2.4 (deleted from tests/pinned-bugs.md;
    # declared in tests/fr4-waivers.md). A scan still persists
    # provider_names/scanned_on — that part is unchanged and stays pinned
    # here — but the row is now written by the single atomic write unit
    # AFTER the page loop: exactly once per scan invocation whatever the
    # page count, and never at all for a folder no provider claimed.
    saved = Folder.objects.get(storage_id=STORAGE_ID, path=rel)
    assert saved.provider_names == fake_provider.machine_name
    assert saved.scanned_on is not None
    assert Folder.objects.filter(storage_id=STORAGE_ID, path=rel).count() == 1
    # (the multi-page half of "exactly once" is
    # test_persistence_write_unit.py::test_multipage_folder_saved_once)

    # Zero-hit folder: its file errors out exactly like CLIPGONE above, so
    # no provider claimed anything — and nothing at all is written.
    zero_rel = "2026/AH_20260101_zerohit"
    (tmp_path / zero_rel).mkdir(parents=True)
    es_fake.push(es_page([_source(f"{zero_rel}/ABSENT.fake", "VX-41-ABSENT")], total=1))
    zero_response = _folder(tmp_path, zero_rel).scan(
        providers=[fake_provider.machine_name]
    )

    assert len(zero_response["errors"]) == 1
    assert not Folder.objects.filter(path=zero_rel).exists()
    assert Folder.objects.count() == 1


def test_ingest_dry_run_key_superset(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    rel = "2026/AH_20260101_ingestkeys"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPKEYS.fake").write_bytes(b"clip data")
    # An already-ingested clip in the fixture makes the zeros below
    # DISCRIMINATING: it is exactly the clip the 2.5 ladder buckets
    # `skipped` on a real run, so a dry run leaking ladder-skip counting
    # would report skipped=1 here. Dry-run counters stay at today's
    # baseline until story 2.7 redefines them as would-be counters.
    (tmp_path / rel / "CLIPSEEN.fake").write_bytes(b"clip data")
    Clip(umid=f"{rel}/CLIPSEEN", item_id="VX-200").save()

    es_fake.push(
        es_page(
            [
                _source(f"{rel}/CLIPKEYS.fake", "VX-41-KEYS"),
                _source(f"{rel}/CLIPSEEN.fake", "VX-41-SEEN"),
            ],
            total=2,
        )
    )

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
    assert response["processed"] == 2
    assert response["already_ingested"] == 1
    assert response["errors"] == []

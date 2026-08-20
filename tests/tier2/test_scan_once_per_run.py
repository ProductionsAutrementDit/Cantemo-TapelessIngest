"""Tier 2 (story 2.1): one ctx over many folders — storages resolved once.

Tree mode: handle() builds one ScanContext and threads it through the
recursion, so a run over N folders performs exactly one getStorage per
unique storage id (FR-7), with scan/ingest results identical to today's.
Folders here deliberately have NO preset ``_root_path`` — resolution goes
through the context, unlike the story-1.3 pins which bypass it.
"""

from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.scan.adapters import build_context

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


def test_tree_run_resolves_each_storage_once(
    migrated_db, es_fake, es_page, fake_provider, storage_fake, tmp_path
):
    rel_a = "2026/AH_20260101_ctxa"
    rel_b = "2026/AH_20260102_ctxb"
    for rel, name in ((rel_a, "CLIPA"), (rel_b, "CLIPB")):
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / f"{name}.fake").write_bytes(b"clip data")

    storage_fake.set_root(STORAGE_ID, str(tmp_path))

    # One context per run, exactly as the commands' handle() builds it.
    ctx = build_context(
        [STORAGE_ID],
        user=None,
        dry_run=True,
        providers=[fake_provider.machine_name],
        legacy_storages=[],
        replace=False,
    )
    assert storage_fake.get_storage_calls == {STORAGE_ID: 1}

    folder_a = Folder(storage_id=STORAGE_ID, path=rel_a)
    folder_b = Folder(storage_id=STORAGE_ID, path=rel_b)

    es_fake.push(es_page([_source(f"{rel_a}/CLIPA.fake", "VX-41-A")], total=1))
    response_a = folder_a.scan(context=ctx)
    es_fake.push(es_page([_source(f"{rel_b}/CLIPB.fake", "VX-41-B")], total=1))
    response_b = folder_b.ingest(context=ctx)

    # The whole ≥2-folder run cost exactly one getStorage per unique id.
    assert storage_fake.get_storage_calls == {STORAGE_ID: 1}

    # Results equal today's: same NFR-5 key sets, same counters.
    assert set(response_a.keys()) == SCAN_KEYS
    assert (
        response_a["hits"],
        response_a["processed"],
        response_a["created"],
        response_a["already_ingested"],
        response_a["errors"],
    ) == (1, 1, 1, 1, [])
    assert [clip.umid for clip in response_a["clips"]] == [f"{rel_a}/CLIPA"]

    assert set(response_b.keys()) == INGEST_KEYS
    assert (
        response_b["hits"],
        response_b["processed"],
        response_b["created"],
        response_b["errors"],
    ) == (1, 1, 1, [])
    # ctx dry_run=True is authoritative for ingest: nothing is ingested.
    assert (
        response_b["ingested"],
        response_b["skipped"],
        response_b["failed"],
        response_b["replaced"],
    ) == (0, 0, 0, 0)

    # Memo seeding: ingest-time clip.root_path reads the ctx-resolved root
    # with zero further storage calls (xdcam's clip.root_path consumer).
    clip = response_a["clips"][0]
    assert clip.root_path == str(tmp_path)
    assert storage_fake.get_storage_calls == {STORAGE_ID: 1}

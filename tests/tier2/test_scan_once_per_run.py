"""Tier 2 (story 2.1): one ctx over many folders — storages resolved once.

Tree mode: handle() builds one ScanContext and hands it to the walk, so a
run over N folders performs exactly one getStorage per unique storage id
(FR-7), with scan/ingest results identical to today's. Folders here
deliberately have NO preset ``_root_path`` — resolution goes through the
context, unlike the story-1.3 pins which bypass it.
"""

from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.scan.adapters import build_context

STORAGE_ID = "VX-41"

# Frozen NFR-5 key sets — never change without human sign-off.
# `consumed_subdirs` was ADDED by story 2.6 (FR-19): purely additive, no key
# lost or changed, sanctioned pin edit with its own tests/fr4-waivers.md row.
SCAN_KEYS = {
    "clips",
    "hits",
    "errors",
    "created",
    "already_ingested",
    "processed",
    "consumed_subdirs",
}
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
    # already_ingested counts item_id presence, so a brand-new clip is 0 (FR-23).
    assert (
        response_a["hits"],
        response_a["processed"],
        response_a["created"],
        response_a["already_ingested"],
        response_a["errors"],
    ) == (1, 1, 1, 0, [])
    assert [clip.umid for clip in response_a["clips"]] == [f"{rel_a}/CLIPA"]

    assert set(response_b.keys()) == INGEST_KEYS
    assert (
        response_b["hits"],
        response_b["processed"],
        response_b["created"],
        response_b["errors"],
    ) == (1, 1, 1, [])
    # ctx dry_run=True is authoritative for ingest: nothing is SUBMITTED.
    # Story 2.7 re-baselined what that looks like in the counters — the
    # same superseded 2.5 clause as `test_ingest_dry_run_key_superset`,
    # in a second place the 2.7 spec's Code Map did not enumerate. CLIPB
    # is brand-new and hashed, so the ladder selects it and the dry run
    # reports the WOULD-BE ingest; `failed`/`replaced` stay 0
    # structurally, because no submission occurred. That nothing was
    # really submitted is pinned in
    # tests/tier2/test_dry_run_purity.py::test_ctx_authoritative_dry_run_is_pure.
    assert (
        response_b["ingested"],
        response_b["skipped"],
        response_b["failed"],
        response_b["replaced"],
    ) == (1, 0, 0, 0)

    # Memo seeding: ingest-time clip.root_path reads the ctx-resolved root
    # with zero further storage calls (xdcam's clip.root_path consumer).
    clip = response_a["clips"][0]
    assert clip.root_path == str(tmp_path)
    assert storage_fake.get_storage_calls == {STORAGE_ID: 1}

    # The provider dict's "scan_context" key is load-bearing: every
    # provider call saw THE run's context object (identity, not equality).
    assert len(fake_provider.seen_scan_contexts) == 2
    assert all(seen is ctx for seen in fake_provider.seen_scan_contexts)


def test_real_tree_walk_resolves_storage_once(
    migrated_db, es_fake, es_page, fake_provider, storage_fake, tmp_path
):
    """The REAL tree walk over a tmp tree: one getStorage total.

    Demonstrated 2.1-review shortfall: the recursion's
    os.scandir(parent_folder.absolute_path) used to re-resolve the storage
    per folder through the property chain. With the ctx seeding in place,
    a whole tree run costs exactly one getStorage per unique storage id.

    Story 2.8 rebound this onto the seam that replaced the command-level
    recursion: `Folder.scan_tree(ctx, emit=…)` IS the walk now, so no
    module-global logger injection is needed — the sink is a parameter.
    Same assertions, same fixture, same claim.
    """
    root_rel = "2026"
    (tmp_path / root_rel / "AH_child_one").mkdir(parents=True)
    (tmp_path / root_rel / "AH_child_two").mkdir(parents=True)

    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    ctx = build_context(
        [STORAGE_ID],
        user=None,
        dry_run=True,
        providers=[fake_provider.machine_name],
        legacy_storages=[],
        replace=False,
        startwith=["AH_"],
    )

    messages = []

    parent = Folder(storage_id=STORAGE_ID, path=root_rel)
    # handle() seeds the top-level folder's memoized root from the ctx;
    # mirror that here — process_folder seeds every child itself.
    parent._root_path = ctx.root_path_for(STORAGE_ID)

    # One index query per child folder: empty page, hits=0 -> the walk
    # descends into the (empty) child directory via scandir.
    es_fake.push(es_page([], total=0))
    es_fake.push(es_page([], total=0))

    run_result = parent.scan_tree(ctx, emit=messages.append)

    assert (run_result.folders_scanned, run_result.folders_failed) == (2, 0)
    # Real once-per-run: the build_context resolution was the ONLY
    # getStorage for the entire tree walk (scandir + ingest + descent).
    assert storage_fake.get_storage_calls == {STORAGE_ID: 1}


def test_paged_ingest_default_ctx_carries_actual_options(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """Paged mode (no ctx passed): ingest builds the default ctx itself.

    The provider dict's scan_context must report ingest's ACTUAL options —
    scan's own default build could not know dry_run/replace (2.1 review
    item: misreported options).
    """
    rel = "2026/AH_20260101_pagedopts"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPOPT.fake").write_bytes(b"clip data")
    es_fake.push(es_page([_source(f"{rel}/CLIPOPT.fake", "VX-41-OPT")], total=1))

    folder = Folder(storage_id=STORAGE_ID, path=rel)
    # Paged seam: preset memo, StorageHelper never involved.
    folder._root_path = str(tmp_path)

    response = folder.ingest(
        dry_run=True, replace=True, providers=[fake_provider.machine_name]
    )

    assert response["processed"] == 1
    [seen] = fake_provider.seen_scan_contexts
    assert seen is not None
    assert seen.options.dry_run is True
    assert seen.options.replace is True
    assert seen.options.providers == [fake_provider.machine_name]
    assert seen.root_path_for(STORAGE_ID) == str(tmp_path)

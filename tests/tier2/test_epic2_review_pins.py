"""Tier 2 (Epic 2 final review): behaviours a mutation proved unpinned.

The verification-gap reviewer ran mutations against a scratch copy and
found six behaviours that survived deletion with the whole suite green.
The ones that need a database live here, together with the correctness
fixes from the same round that are only observable end to end.

Nothing in this file is a new feature test — every one of these pins an
existing promise that was, demonstrably, not being kept by any assertion.
"""

import importlib
import logging
import os
import re
import traceback
from contextlib import contextmanager

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.scan.adapters import build_context
from portal.plugins.TapelessIngest.scan.coordinator import (
    FolderOutcome,
    SequentialDispatcher,
    WorkerResult,
    walk_tree,
)

from tests.portal_stub import VidispineFake

STORAGE_ID = "VX-41"
ROOT = "2026"


def _source(path):
    return {
        "path": path,
        "hash": f"hash-{path}",
        "storage": STORAGE_ID,
        "id": f"VX-41-{path}",
        "size": 1024,
    }


def _queried_folder(search_doc):
    """The bare escaped folder path a scan's search doc asks about.

    `build_search_doc` always emits the folder's own escaped path as one
    `regexp` parent filter, plus one per provider sub-path — so the
    shortest is the folder itself.
    """
    found = []

    def walk(node):
        if isinstance(node, dict):
            regexp = node.get("regexp")
            if isinstance(regexp, dict) and "parent" in regexp:
                found.append(regexp["parent"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(search_doc)
    return min(found, key=len) if found else None


def _folder(tmp_path, rel_path):
    folder = Folder(storage_id=STORAGE_ID, path=rel_path)
    folder._root_path = str(tmp_path)
    return folder


def _write(tmp_path, *rel_paths):
    for rel in rel_paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")


def _ctx(tmp_path, storage_fake, fake_provider, **options):
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    defaults = dict(
        user=None,
        dry_run=True,
        providers=[fake_provider.machine_name],
        legacy_storages=[],
        replace=False,
    )
    defaults.update(options)
    return build_context([STORAGE_ID], **defaults)


def _run_tree(tmp_path, ctx, root=ROOT):
    emitted = []
    folder = Folder(storage_id=STORAGE_ID, path=root)
    folder._root_path = ctx.root_path_for(STORAGE_ID)
    return folder.scan_tree(ctx, emit=emitted.append), emitted


# --------------------------------------------------------------------------
# A1: a rolled-back write unit must stop the folder
# --------------------------------------------------------------------------


@pytest.fixture
def exploding_write(monkeypatch):
    def boom(writes, stale_deletes):
        raise DatabaseError("metadata upsert exploded")

    monkeypatch.setattr(Clip, "persist_metadatas_bulk", staticmethod(boom))


def test_a_rolled_back_write_unit_yields_no_clips_to_ingest(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, exploding_write
):
    """Nothing may be SUBMITTED for a folder whose rows were not written.

    ``Clip.ingest`` persists its state through a targeted UPDATE that
    matches no row, then falls back to a full ``save()`` — writing rows
    through the path AD-6 exists to avoid. Worse, ``import_file`` would
    already have created a Vidispine item, and no row would record its
    ``item_id``, so the next run would ingest the same clip again.
    """
    rel = f"{ROOT}/AH_20260101_rollback"
    _write(tmp_path, f"{rel}/CLIPA.fake", f"{rel}/CLIPB.fake")
    es_fake.push(
        es_page(
            [_source(f"{rel}/CLIPA.fake"), _source(f"{rel}/CLIPB.fake")],
            total=2,
        )
    )

    response = _folder(tmp_path, rel).ingest(
        providers=[fake_provider.machine_name], legacy_storages=[]
    )

    assert any("Error persisting scan results" in e for e in response["errors"])
    # The clips are gone from the response, so the ladder saw nothing...
    assert response["clips"] == []
    assert (response["ingested"], response["skipped"]) == (0, 0)
    # ...and nothing reached Vidispine.
    assert VidispineFake.calls == []
    assert Clip.objects.count() == 0
    # The counters still report what the scan FOUND — the folder is not
    # silently emptied, it is stopped.
    assert (response["hits"], response["processed"]) == (2, 2)


def test_a_rolled_back_write_unit_forbids_descent(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, exploding_write
):
    """A folder that did not record its clips cannot vouch for them."""
    rel = f"{ROOT}/AH_20260101_rollbackdescent"
    _write(tmp_path, f"{rel}/CARD/CLIPA.fake")
    es_fake.push(es_page([_source(f"{rel}/CARD/CLIPA.fake")], total=1))

    response = _folder(tmp_path, rel).scan(providers=[fake_provider.machine_name])

    assert response["consumed_subdirs"] is None
    assert any("Not descending into" in e for e in response["errors"])


# --------------------------------------------------------------------------
# A2: loop exit is not completeness
# --------------------------------------------------------------------------


def test_a_short_page_does_not_authorize_descent(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """The page loop also ends when a page comes back SHORT.

    A truncated or shifting index returns fewer rows than the total it
    reports. Before this round the loop simply exited and `complete_pass`
    was still true, so a consumed set built from a FRACTION of the folder
    authorized descent — under-consumption, the duplicate direction.
    """
    rel = f"{ROOT}/AH_20260101_short"
    _write(tmp_path, f"{rel}/CLIPA.fake")
    # total says 50; the page carries one row and is therefore not full,
    # so the loop stops after it.
    es_fake.push(es_page([_source(f"{rel}/CLIPA.fake")], total=50))

    response = _folder(tmp_path, rel).scan(
        number=0, providers=[fake_provider.machine_name]
    )

    assert response["hits"] == 50
    assert response["processed"] == 1
    assert response["consumed_subdirs"] is None
    assert any(
        "the index reported 50 files but only 1 were returned" in e
        for e in response["errors"]
    )


def test_a_complete_pass_still_authorizes_descent(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """The guard must not have closed the ordinary path."""
    rel = f"{ROOT}/AH_20260101_complete"
    _write(tmp_path, f"{rel}/CLIPA.fake")
    es_fake.push(es_page([_source(f"{rel}/CLIPA.fake")], total=1))

    response = _folder(tmp_path, rel).scan(
        number=0, providers=[fake_provider.machine_name]
    )

    assert response["consumed_subdirs"] == frozenset()
    assert response["errors"] == []


# --------------------------------------------------------------------------
# D19: coordinator ORM-freedom, guarded by the stack rather than a grep
# --------------------------------------------------------------------------


COORDINATOR_FILE = os.path.join("scan", "coordinator.py")


@contextmanager
def _orm_calls_from_the_coordinator():
    """Flag any statement issued from a coordinator frame.

    The four-token source grep this replaces passed happily while a
    ``_default_manager`` call added inside ``merge_results`` issued real
    SELECTs — a grep can only ever know the spellings someone thought of.
    This watches the actual stack: a statement is a violation when a
    ``scan/coordinator.py`` frame appears in it with no ``process_folder``
    frame BENEATH it, which is the boundary Epic 3's pool depends on (it
    wraps `process_folder`, and only `process_folder`, in per-worker
    connection hygiene).
    """
    violations = []

    def recorder(execute, sql, params, many, context):
        stack = traceback.extract_stack()
        coordinator_frames = [
            index
            for index, frame in enumerate(stack)
            if frame.filename.endswith(COORDINATOR_FILE)
        ]
        if coordinator_frames:
            deepest = coordinator_frames[-1]
            below = [frame.name for frame in stack[deepest + 1 :]]
            if "process_folder" not in below:
                violations.append((sql, [frame.name for frame in stack[deepest:]]))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(recorder):
        yield violations


def test_the_coordinator_issues_no_query_of_its_own(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, storage_fake
):
    rel = f"{ROOT}/AH_20260101_ormfree"
    _write(tmp_path, f"{rel}/CLIPA.fake")
    es_fake.push(es_page([_source(f"{rel}/CLIPA.fake")], total=1))
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])

    with _orm_calls_from_the_coordinator() as violations:
        run_result, _emitted = _run_tree(tmp_path, ctx)

    # The run really did query — otherwise the guard is watching nothing.
    assert run_result.counters.processed == 1
    assert not violations, violations


def test_the_guard_catches_an_orm_call_planted_in_the_coordinator(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, storage_fake, monkeypatch
):
    """The reviewer's exact mutation, run as a test.

    Without this, "no violations" above could mean the wrapper never fired.
    """
    from portal.plugins.TapelessIngest.scan import coordinator

    real_merge = coordinator.merge_results

    def leaky_merge(outcomes):
        Folder._default_manager.count()  # the planted defect
        return real_merge(outcomes)

    # Patch the name `Folder.scan_tree` actually calls.
    folder_module = importlib.import_module(
        "portal.plugins.TapelessIngest.models.folder"
    )
    leaky_merge.__code__ = leaky_merge.__code__.replace(
        co_filename=os.path.join(
            os.path.dirname(coordinator.__file__), "coordinator.py"
        )
    )
    monkeypatch.setattr(folder_module, "merge_results", leaky_merge)

    rel = f"{ROOT}/AH_20260101_leak"
    _write(tmp_path, f"{rel}/CLIPA.fake")
    es_fake.push(es_page([_source(f"{rel}/CLIPA.fake")], total=1))
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])

    with _orm_calls_from_the_coordinator() as violations:
        _run_tree(tmp_path, ctx)

    assert violations, "the stack guard did not see a planted coordinator query"


# --------------------------------------------------------------------------
# D20 / D21: the never-raising wrapper's two untested promises
# --------------------------------------------------------------------------


def test_keyboard_interrupt_propagates_out_of_the_run(
    migrated_db, es_fake, fake_provider, tmp_path, storage_fake, monkeypatch
):
    """`process_folder` catches `Exception`, deliberately not `BaseException`.

    Widening that handler passed the whole suite — so a Ctrl-C during a
    nightly run would have been swallowed per folder, and the operator's
    only way to stop 8,000 folders would have stopped working silently.
    """
    from portal.plugins.TapelessIngest.models import folder as folder_module

    (tmp_path / ROOT / "AH_interrupt").mkdir(parents=True)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(folder_module, "_folder_worker", interrupt)
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])

    with pytest.raises(KeyboardInterrupt):
        _run_tree(tmp_path, ctx)


def test_system_exit_propagates_out_of_the_run(
    migrated_db, es_fake, fake_provider, tmp_path, storage_fake, monkeypatch
):
    from portal.plugins.TapelessIngest.models import folder as folder_module

    (tmp_path / ROOT / "AH_exit").mkdir(parents=True)

    def bail(*args, **kwargs):
        raise SystemExit(2)

    monkeypatch.setattr(folder_module, "_folder_worker", bail)
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])

    with pytest.raises(SystemExit):
        _run_tree(tmp_path, ctx)


def test_a_vanished_folder_reports_the_file_not_found_template(
    migrated_db, es_fake, fake_provider, tmp_path, storage_fake, monkeypatch
):
    """The branch story 2.8 rewrote, and which no test touched.

    Altering its template passed. It is the handler that stopped reading a
    possibly-unbound `folder` — it formats from the `(storage_id, path)`
    the worker was HANDED, so a `get_or_new` that itself raises cannot
    turn one missing folder into a NameError that takes the run down.
    """
    from portal.plugins.TapelessIngest.models import folder as folder_module

    (tmp_path / ROOT / "AH_gone").mkdir(parents=True)

    def vanished(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(folder_module, "_folder_worker", vanished)
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])

    run_result, emitted = _run_tree(tmp_path, ctx)

    expected = f"Path doesn't exists anymore: {ROOT}/AH_gone"
    assert expected in run_result.errors
    assert expected in emitted
    assert (run_result.folders_scanned, run_result.folders_failed) == (0, 1)


def test_the_file_not_found_template_survives_an_unopenable_folder(
    migrated_db, fake_provider, tmp_path, storage_fake, monkeypatch
):
    """`get_or_new` itself raising is the case the rewrite was FOR."""
    from portal.plugins.TapelessIngest.models import folder as folder_module

    (tmp_path / ROOT / "AH_unopenable").mkdir(parents=True)
    real_get_or_new = Folder.get_or_new

    def raising_get_or_new(cls_defaults=None, **kwargs):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(
        folder_module.Folder, "get_or_new", staticmethod(raising_get_or_new)
    )
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])

    run_result, _emitted = _run_tree(tmp_path, ctx)

    assert run_result.errors == (f"Path doesn't exists anymore: {ROOT}/AH_unopenable",)
    assert real_get_or_new is not None  # (kept: the monkeypatch is undone)


# --------------------------------------------------------------------------
# D22: `expect_persisted=False` is the REST endpoint's contract
# --------------------------------------------------------------------------


def test_an_unsaved_clip_logs_the_broken_invariant_by_default(migrated_db, caplog):
    umid = "REST/CLIP-DEFAULT"
    clip = Clip(umid=umid, path="2026/A", storage_id=STORAGE_ID)

    with caplog.at_level(logging.ERROR):
        clip.persist_ingest_state()

    assert any("reached ingest unsaved" in record.message for record in caplog.records)


def test_expect_persisted_false_is_silent_for_a_clip_built_from_a_request(
    migrated_db, caplog
):
    """Deleting the kwarg in `views.py` passed the whole suite.

    The regression is not a crash: it is every REST ingest logging an
    ERROR whose text means "the scan's write unit failed", which trains
    operators to ignore the line that matters.
    """
    umid = "REST/CLIP-EXPECTED"
    clip = Clip(umid=umid, path="2026/A", storage_id=STORAGE_ID)

    with caplog.at_level(logging.ERROR):
        clip.persist_ingest_state(expect_persisted=False)

    assert not [r for r in caplog.records if "reached ingest unsaved" in r.message]
    assert Clip.objects.filter(pk=umid).exists()


def test_views_still_passes_expect_persisted_false():
    """The call site itself — a static guard, like the sweep's own.

    `views.py` has no request-level harness, so this is what stands
    between the semantic pin above and the endpoint that depends on it.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "views.py").read_text("utf-8")
    ingest_calls = re.findall(r"clip\.ingest\((.*?)\)", source, flags=re.S)
    assert ingest_calls, "views.py no longer calls clip.ingest()"
    assert all("expect_persisted=False" in call for call in ingest_calls), ingest_calls


# --------------------------------------------------------------------------
# D23: FR-24's waiver row, whose failure mode is an infinite walk
# --------------------------------------------------------------------------


def test_a_symlink_cycle_does_not_make_the_walk_run_forever(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, storage_fake
):
    """The FR-24 waiver had no test at all.

    `listing.dirs` is built with `follow_symlinks=False`, so a symlinked
    directory is never a walk target. Without that, `loop -> ..` recurses
    until the OS refuses — an 8,000-folder nightly that never returns.
    """
    shoot = f"{ROOT}/AH_20260101_cycle"
    (tmp_path / shoot / "real").mkdir(parents=True)
    (tmp_path / shoot / "loop").symlink_to(tmp_path / shoot)
    (tmp_path / shoot / "real" / "back").symlink_to(tmp_path / shoot)

    es_fake.route(lambda search_doc, first, number: es_page([], total=0))
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])

    run_result, _emitted = _run_tree(tmp_path, ctx)

    # The shoot folder and its one REAL child. The two symlinks are not in
    # `dirs` at all, so the walk terminates.
    assert (run_result.folders_scanned, run_result.folders_failed) == (2, 0)


# --------------------------------------------------------------------------
# A7 / B8: the cron can see failure
# --------------------------------------------------------------------------


def test_an_ingesting_run_without_a_user_fails_fast(
    migrated_db, fake_provider, tmp_path, storage_fake
):
    """The `user is None` guard went with the old walk.

    A real run then proceeded silently and failed per clip, deep inside
    `Clip.ingest`, with no explanation anywhere.
    """
    (tmp_path / ROOT / "AH_nouser").mkdir(parents=True)
    ctx = _ctx(tmp_path, storage_fake, fake_provider, dry_run=False, user=None)

    with pytest.raises(TapelessIngestException, match="User has to be provided"):
        _run_tree(tmp_path, ctx)


def test_a_dry_run_without_a_user_warns_and_rehearses(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, storage_fake
):
    """A rehearsal submits nothing, so it genuinely does not need one."""
    (tmp_path / ROOT / "AH_nouserdry").mkdir(parents=True)
    es_fake.route(lambda search_doc, first, number: es_page([], total=0))
    ctx = _ctx(tmp_path, storage_fake, fake_provider, dry_run=True, user=None)

    run_result, emitted = _run_tree(tmp_path, ctx)

    assert emitted[0].startswith("User has to be provided")
    assert run_result.folders_scanned == 1


@pytest.fixture
def command_user():
    user = User.objects.create(pk=4247, username="epic2-review-pins")
    yield user
    user.delete()  # keep the auth table empty for the unknown-user tests


def _command_args(*extra):
    return [
        "--storage",
        STORAGE_ID,
        "--path",
        ROOT,
        "--userId",
        "4247",
        "--providers",
        "faketest",
        *extra,
    ]


def test_a_failed_folder_gives_the_cron_a_non_zero_exit(
    migrated_db,
    es_fake,
    fake_provider,
    tmp_path,
    storage_fake,
    command_user,
    monkeypatch,
):
    """`handle()` discarded the RunResult, which knows `folders_failed`.

    A run in which every folder failed exited 0 — the only thing cron
    reads — so a silently broken nightly looked exactly like a clean one.
    """
    module = importlib.import_module(
        "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir"
    )
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    (tmp_path / ROOT / "AH_failing").mkdir(parents=True)

    def boom(search_doc, first, number):
        raise TapelessIngestException("index unavailable")

    es_fake.route(boom)

    with pytest.raises(CommandError, match="1 of 1 folder"):
        call_command("scan_tapeless_dir", *_command_args("--dryrun"))

    # Slack was flushed BEFORE the exit status was set: the report is the
    # only thing that says what actually happened.
    assert any("index unavailable" in m for m in module.logger.messages)


def test_a_clean_run_exits_zero(
    migrated_db,
    es_fake,
    es_page,
    fake_provider,
    tmp_path,
    storage_fake,
    command_user,
    monkeypatch,
):
    module = importlib.import_module(
        "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir"
    )
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    (tmp_path / ROOT / "AH_clean").mkdir(parents=True)
    es_fake.route(lambda search_doc, first, number: es_page([], total=0))

    call_command("scan_tapeless_dir", *_command_args("--dryrun"))

    assert any("0 failed in" in m for m in module.logger.messages)


def test_a_broken_root_gives_the_cron_a_non_zero_exit(
    migrated_db, fake_provider, tmp_path, storage_fake, command_user, monkeypatch
):
    """B11 end to end: a missing --path is not "0 folders scanned, 0 failed"."""
    module = importlib.import_module(
        "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir"
    )
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    # ROOT is never created on disk.

    with pytest.raises(CommandError, match="1 of 1 folder"):
        call_command("scan_tapeless_dir", *_command_args("--dryrun"))


def test_the_startup_banner_names_the_date_window(
    migrated_db,
    es_fake,
    es_page,
    fake_provider,
    tmp_path,
    storage_fake,
    command_user,
    monkeypatch,
):
    """Since 2.6 the window is its own filter, so `only` no longer shows it."""
    module = importlib.import_module(
        "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir"
    )
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    (tmp_path / ROOT).mkdir(parents=True)
    es_fake.route(lambda search_doc, first, number: es_page([], total=0))

    call_command("scan_tapeless_dir", *_command_args("--dryrun", "--since", "1d"))

    [banner] = [m for m in module.logger.messages if m.startswith("Scanning folder ")]
    assert re.search(r"date window \d{8}\.\.\d{8}$", banner), banner


# --------------------------------------------------------------------------
# C15: the folder's own directory is listed ONCE
# --------------------------------------------------------------------------


def test_the_folder_directory_is_scanned_once_per_folder(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, storage_fake, monkeypatch
):
    """The worker verified against one listing, the walk built another.

    Two `os.scandir` calls per folder on an 8,000-folder tree, and the
    same `Error listing directory` string appended twice whenever one
    failed.
    """
    shoot = f"{ROOT}/AH_20260101_onescan"
    _write(tmp_path, f"{shoot}/CLIPA.fake")
    (tmp_path / shoot / "SUB").mkdir(parents=True, exist_ok=True)

    scanned = []
    real_scandir = os.scandir

    def counting_scandir(path):
        scanned.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)

    def respond(search_doc, first, number):
        # Path-routed, like the real index: only the shoot folder's own
        # query returns its file. A responder keyed on a substring would
        # hand SUB the same hit, and SUB's worker would then list the
        # SHOOT directory — a second scandir the fixture invented.
        if _queried_folder(search_doc) == re.escape(shoot):
            return es_page([_source(f"{shoot}/CLIPA.fake")], total=1)
        return es_page([], total=0)

    es_fake.route(respond)
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])

    _run_tree(tmp_path, ctx)

    shoot_absolute = os.path.normpath(str(tmp_path / shoot))
    assert scanned.count(shoot_absolute) == 1, scanned


def test_a_listing_failure_is_reported_once_not_twice(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, storage_fake
):
    shoot = f"{ROOT}/AH_20260101_locked"
    (tmp_path / shoot).mkdir(parents=True)
    (tmp_path / shoot).chmod(0o000)
    es_fake.route(lambda search_doc, first, number: es_page([], total=0))
    ctx = _ctx(tmp_path, storage_fake, fake_provider, startwith=["AH_"])
    try:
        run_result, _emitted = _run_tree(tmp_path, ctx)
    finally:
        (tmp_path / shoot).chmod(0o700)

    listing_errors = [e for e in run_result.errors if e.startswith("Error listing ")]
    assert len(listing_errors) == 1, run_result.errors


# --------------------------------------------------------------------------
# C16: the ingest-state UPDATE buys no SELECT
# --------------------------------------------------------------------------


def test_persisting_ingest_state_issues_exactly_one_statement(
    migrated_db, command_user
):
    """`INGEST_STATE_FIELDS` read `user`, not `user_id`.

    Reading the FK descriptor on a row whose User is not already loaded
    fetches it — one SELECT per clip, inside the submission loop the whole
    AD-6 effort exists to keep query-free — and the UPDATE only ever
    needed the id.
    """
    umid = "2026/A/CLIPFK"
    Clip.objects.create(umid=umid, path="2026/A", storage_id=STORAGE_ID)
    # Reloaded, so the FK is deferred exactly as it is mid-ingest.
    clip = Clip.objects.get(pk=umid)
    clip.user_id = command_user.pk
    clip.item_id = "VX-ITEM-FK"

    statements = []

    def recorder(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(recorder):
        clip.persist_ingest_state()

    assert len(statements) == 1, statements
    assert statements[0].lstrip().upper().startswith("UPDATE")
    assert Clip.objects.get(pk=umid).user_id == command_user.pk


# --------------------------------------------------------------------------
# A3, wired: the co-matching provider's layout is consumed by the WALK
# --------------------------------------------------------------------------

CARD_NAME = "reviewcard"
SIDE_NAME = "reviewside"


class _CardProvider:
    """Claims the file and names itself — the layout provider."""

    name = "Review Card Provider"
    machine_name = CARD_NAME

    def getExtensions(self):
        return [".fake"]

    def getSubPaths(self):
        return ["CARD/CLIP"]

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas


class _SideProvider:
    """Contributes to the SAME file without claiming the identity key.

    This is the shape the multi-provider contract is about (xdcam plus
    exif): it enriches the metadatas and declares a layout of its own, but
    it never overwrites `provider`. Deriving the matched set from
    `metadatas["provider"]` therefore cannot see it at all.
    """

    name = "Review Side Provider"
    machine_name = SIDE_NAME

    def getExtensions(self):
        return [".fake"]

    def getSubPaths(self):
        return ["SIDE"]

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        metadatas["side_enriched"] = True
        return metadatas


@pytest.fixture
def multi_providers():
    card, side = _CardProvider(), _SideProvider()
    Clip._PROVIDER_CACHE[CARD_NAME] = card
    Clip._PROVIDER_CACHE[SIDE_NAME] = side
    yield card, side
    Clip._PROVIDER_CACHE.pop(CARD_NAME, None)
    Clip._PROVIDER_CACHE.pop(SIDE_NAME, None)


def test_a_co_matching_providers_directory_is_consumed_not_descended_into(
    migrated_db, es_fake, es_page, tmp_path, storage_fake, multi_providers
):
    """The A3 wiring, end to end through the real walk.

    `metadatas["provider"]` says only "reviewcard", so the pre-fix matched
    set was {reviewcard} and `SIDE` — the second provider's declared
    layout — was left open. The walk descended into it and would have
    re-ingested whatever the side provider had already covered.
    """
    shoot = f"{ROOT}/AH_20260101_multi"
    _write(tmp_path, f"{shoot}/CARD/CLIP/A.fake")
    (tmp_path / shoot / "SIDE").mkdir(parents=True, exist_ok=True)
    (tmp_path / shoot / "OTHER").mkdir(parents=True, exist_ok=True)

    queried = []

    def respond(search_doc, first, number):
        folder = _queried_folder(search_doc)
        queried.append(folder)
        if folder == re.escape(shoot):
            return es_page([_source(f"{shoot}/CARD/CLIP/A.fake")], total=1)
        return es_page([], total=0)

    es_fake.route(respond)
    card, _side = multi_providers
    ctx = _ctx(
        tmp_path,
        storage_fake,
        card,
        providers=[CARD_NAME, SIDE_NAME],
        startwith=["AH_"],
    )

    run_result, _emitted = _run_tree(tmp_path, ctx)

    assert run_result.counters.hits == 1
    # Layer (a): the clip's own directory.
    assert re.escape(f"{shoot}/CARD") not in queried
    # Layer (b) from the provider that never wrote `provider` — the whole
    # point of feeding the matched set from every CONTRIBUTOR.
    assert re.escape(f"{shoot}/SIDE") not in queried
    # ...and an unrelated sibling is still walked, so the test is not
    # passing because descent stopped altogether.
    assert re.escape(f"{shoot}/OTHER") in queried

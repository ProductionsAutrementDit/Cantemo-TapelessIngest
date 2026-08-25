"""Tier 2 (story 2.8, FR-37): check_clips_in_folder completes a run.

This command has never worked. Ported verbatim in story 1.1 from a prod
script that carried two fatal defects — a ``folder.scan(dry_run=…,
replace=…)`` call the method does not accept (``TypeError`` on the first
folder), and a log line reading ingest-only keys off a scan response
(``KeyError`` if it ever got that far) — it has had a `deferred-work.md`
entry warning operators not to mistake it for a working tool.

This story does not patch those defects; it removes the conditions for
them. The command runs the SAME pipeline as its writing sibling, with
``ctx.options.dry_run`` forced on by construction, so:

* there is no ``folder.scan(dry_run=…, replace=…)`` call left anywhere —
  the run options travel on the context;
* all eight AD-13 counters exist on every ``WorkerResult``, so the folder
  line cannot read a key that is not there.

What this file pins is therefore the whole FR-37 claim: it completes a
run, it writes nothing, it reports all eight counters, and the two flags
it keeps for sibling symmetry announce themselves as inert exactly once.

The zero-write proof is 2.7's: a whitelist over every statement the run
executes (``connection.execute_wrapper`` sees ``bulk_create`` and
``queryset.update``, which ``CaptureQueriesContext`` can miss) plus row
DELTAS, because the sqlite ``:memory:`` DB is session-scoped and absolute
counts prove nothing.
"""

import importlib
import os
import re

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder

from tests.portal_stub import VidispineFake
from tests.sql_capture import captured_sql

STORAGE_ID = "VX-41"
ROOT = "2026"
SHOOT = f"{ROOT}/AH_20260101_check"

CHECK = "portal.plugins.TapelessIngest.management.commands.check_clips_in_folder"
SCAN = "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir"

DRYRUN_NOTICE = (
    "--dryrun has no effect here: this command is read-only by construction "
    "and always runs as a dry run"
)
REPLACE_NOTICE = (
    "--replace performs no writes here: this command is read-only by "
    "construction, but the flag still changes the would-be counters this "
    "run reports"
)

READ_ONLY_SQL = re.compile(
    r"^(SELECT|SAVEPOINT|RELEASE|ROLLBACK|BEGIN|PRAGMA)\b", re.IGNORECASE
)


# Sees every thread's connection via the connection_created signal
# (story 3.1) — the former local copy watched only the calling thread's.
_captured_sql = captured_sql


def _row_counts():
    return (
        Clip.objects.count(),
        ClipMetadata.objects.count(),
        Folder.objects.count(),
        Clip.folders.through.objects.count(),
    )


def _source(path):
    return {
        "path": path,
        "hash": f"hash-{path}",
        "storage": STORAGE_ID,
        "id": f"VX-41-{path}",
        "size": 1024,
    }


@pytest.fixture
def fixture_tree(tmp_path, es_fake, es_page, storage_fake):
    """One shoot folder holding two clips the pipeline really can find."""
    (tmp_path / SHOOT).mkdir(parents=True)
    names = ["CLIPONE", "CLIPTWO"]
    for name in names:
        (tmp_path / SHOOT / f"{name}.fake").write_bytes(b"clip data")
    # One page for the shoot folder itself, one for its (empty) descent —
    # the walk queries every folder it visits.
    es_fake.push(
        es_page([_source(f"{SHOOT}/{name}.fake") for name in names], total=len(names))
    )
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return tmp_path


@pytest.fixture
def command_user():
    user = User.objects.create(pk=4245, username="story28-fr37")
    yield user
    user.delete()  # keep the auth table empty for the unknown-user tests


def _args(*extra):
    return [
        "--storage",
        STORAGE_ID,
        "--path",
        ROOT,
        "--userId",
        "4245",
        "--providers",
        "faketest",
        *extra,
    ]


def _messages(module_path):
    return importlib.import_module(module_path).logger.messages


# --------------------------------------------------------------------------
# It completes a run
# --------------------------------------------------------------------------


def test_check_clips_in_folder_completes_a_run_and_writes_nothing(
    migrated_db,
    fixture_tree,
    fake_provider,
    command_user,
    monkeypatch,
    storage_fake,
):
    module = importlib.import_module(CHECK)
    # handle() reassigns these module globals; re-setting them to their
    # current values via monkeypatch restores them on teardown.
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)
    before = _row_counts()

    with _captured_sql() as statements:
        call_command("check_clips_in_folder", *_args())

    messages = module.logger.messages
    # It got all the way to the summary — the thing it had never done.
    assert messages[-2].startswith("DRY-RUN: 1 folders scanned, 0 failed in ")
    assert messages[-1].startswith("DRY-RUN: 2 clips found, 2 created, ")

    # And it wrote nothing, by construction rather than by flag.
    offending = [sql for sql in statements if not READ_ONLY_SQL.match(sql.lstrip())]
    assert not offending, offending
    assert _row_counts() == before
    assert VidispineFake.calls == []


def test_the_folder_line_reports_all_eight_counters(
    migrated_db,
    fixture_tree,
    fake_provider,
    command_user,
    monkeypatch,
):
    """Defect #2, structurally gone.

    The prod line read ``ingested``/``skipped``/``replaced`` off a
    ``Folder.scan`` response that never had them. They are AD-13 counter
    fields now, so the line cannot KeyError — and because the ladder runs
    in dry mode too (story 2.7), the numbers are the run's real verdict
    rather than placeholder zeros.
    """
    module = importlib.import_module(CHECK)
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)

    call_command("check_clips_in_folder", *_args())

    [line] = [
        message
        for message in module.logger.messages
        if message.startswith(f"found 2 clips in {SHOOT},")
    ]
    for fragment in (
        "0 already ingested",
        "2 created",
        "providers are faketest",
        "2 ingested",
        "0 skipped",
        "0 replaced",
        "0 errors encountered",
    ):
        assert fragment in line, line


# --------------------------------------------------------------------------
# The inert flags
# --------------------------------------------------------------------------


def test_each_inert_flag_notice_is_emitted_exactly_once(
    migrated_db,
    fixture_tree,
    fake_provider,
    command_user,
    monkeypatch,
):
    module = importlib.import_module(CHECK)
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)

    call_command("check_clips_in_folder", *_args("--dryrun", "--replace"))

    messages = module.logger.messages
    assert messages.count(DRYRUN_NOTICE) == 1
    assert messages.count(REPLACE_NOTICE) == 1
    # They lead the report: an operator reads why their flags did nothing
    # before they read what the run found.
    assert messages.index(DRYRUN_NOTICE) < messages.index(REPLACE_NOTICE)
    assert messages.index(REPLACE_NOTICE) < len(messages) - 1


def test_no_notice_is_emitted_when_the_flags_are_not_given(
    migrated_db,
    fixture_tree,
    fake_provider,
    command_user,
    monkeypatch,
):
    module = importlib.import_module(CHECK)
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)

    call_command("check_clips_in_folder", *_args())

    messages = module.logger.messages
    assert DRYRUN_NOTICE not in messages
    assert REPLACE_NOTICE not in messages


def test_the_writing_sibling_never_emits_the_notices(
    migrated_db,
    fixture_tree,
    fake_provider,
    command_user,
    monkeypatch,
):
    """The notices are guarded by the constant, not by the flag."""
    module = importlib.import_module(SCAN)
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)

    call_command("scan_tapeless_dir", *_args("--dryrun", "--replace"))

    messages = module.logger.messages
    assert DRYRUN_NOTICE not in messages
    assert REPLACE_NOTICE not in messages
    # ...and a --dryrun run of the WRITING command is still labelled.
    assert messages[-1].startswith("DRY-RUN: ")


# --------------------------------------------------------------------------
# Read-only by construction, not by argument
# --------------------------------------------------------------------------


def test_the_constant_is_the_only_source_difference():
    check_module = importlib.import_module(CHECK)
    scan_module = importlib.import_module(SCAN)
    assert check_module.FORCE_DRY_RUN is True
    assert scan_module.FORCE_DRY_RUN is False
    # `handle()` byte-identity is pinned in tests/tier1/test_commands_in_sync.


def test_the_run_really_would_have_written_without_the_constant(
    migrated_db,
    fixture_tree,
    fake_provider,
    command_user,
    monkeypatch,
):
    """The zero-write assertion above has to be discriminating.

    A fixture that finds nothing writes nothing too. The same tree, the
    same flags, run through the WRITING sibling, produces rows — so what
    the read-only command demonstrated is a property of ``FORCE_DRY_RUN``
    and not of an empty fixture.
    """
    module = importlib.import_module(SCAN)
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)
    # getCollection needs a Settings row and a live search backend; the
    # same PLUGIN double test_dry_run_purity uses (AD-11 untouched).
    monkeypatch.setattr(
        Folder, "getCollection", lambda self, user, dryrun=False: "VX-COLLECTION"
    )
    before = _row_counts()

    call_command("scan_tapeless_dir", *_args())

    assert Clip.objects.count() == before[0] + 2
    assert Folder.objects.count() == before[2] + 1


def test_a_dryrun_flag_cannot_make_the_writing_sibling_read_only_by_accident(
    migrated_db,
    fixture_tree,
    fake_provider,
    command_user,
    monkeypatch,
):
    """The flag and the constant reach ctx.options.dry_run the same way.

    `dry_run=args.dryrun or FORCE_DRY_RUN` is the whole of it — which is
    why `handle()` can be byte-identical in both files.
    """
    module = importlib.import_module(SCAN)
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)
    before = _row_counts()

    with _captured_sql() as statements:
        call_command("scan_tapeless_dir", *_args("--dryrun"))

    offending = [sql for sql in statements if not READ_ONLY_SQL.match(sql.lstrip())]
    assert not offending, offending
    assert _row_counts() == before

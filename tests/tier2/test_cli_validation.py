"""Tier 2: fail-fast CLI validation via call_command on both commands.

Order-independent via match= pinning: every `pytest.raises(CommandError)`
pins `match=` to the exact matrix message, so a validation regression that
reaches a later stage (e.g. the user lookup) fails the test instead of
passing on the wrong error, regardless of test execution order or the
session-scoped DB state left by earlier tests.
"""

import importlib
import re
from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.scan.context import ScanContext

COMMANDS = ["scan_tapeless_dir", "check_clips_in_folder"]

BASE_ARGS = ["--storage", "VX-41", "--path", "2026"]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("value", ["5h", "abc", "3", "m"])
def test_malformed_since_aborts(command, value):
    expected = (
        f"Invalid --since '{value}': expected <number><unit>, unit one of d/w/m/y"
    )
    with pytest.raises(CommandError, match=re.escape(expected)):
        call_command(command, *BASE_ARGS, "--userId", "1", "--since", value)


@pytest.mark.parametrize("command", COMMANDS)
def test_malformed_from_aborts(command):
    expected = "Invalid --from '2026-13-01': expected YYYY-MM-DD"
    with pytest.raises(CommandError, match=re.escape(expected)):
        call_command(command, *BASE_ARGS, "--userId", "1", "--from", "2026-13-01")


@pytest.mark.parametrize("command", COMMANDS)
def test_since_validated_even_when_from_wins(command):
    # --from wins over --since, but both are always validated first.
    expected = "Invalid --since '5h': expected <number><unit>, unit one of d/w/m/y"
    with pytest.raises(CommandError, match=re.escape(expected)):
        call_command(
            command,
            *BASE_ARGS,
            "--userId",
            "1",
            "--since",
            "5h",
            "--from",
            "2026-08-01",
        )


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("user_id", ["99999", "abc"])
def test_unknown_user_id_aborts(command, user_id, migrated_db):
    # migrated_db provides the auth tables; the table stays empty, so a
    # valid-args invocation must die at user resolution with this message
    # (covers both User.DoesNotExist and non-integer ValueError).
    expected = f"Unknown user id '{user_id}'"
    with pytest.raises(CommandError, match=re.escape(expected)):
        call_command(command, *BASE_ARGS, "--userId", user_id)


@pytest.mark.parametrize("command", COMMANDS)
def test_future_from_aborts(command):
    # Command-boundary check of the future-date rejection; no migrated_db —
    # a valid abort never reaches the DB.
    with pytest.raises(CommandError, match=re.escape("--from date is in the future")):
        call_command(command, *BASE_ARGS, "--userId", "1", "--from", "2999-01-01")


@pytest.mark.parametrize("command", COMMANDS)
def test_missing_required_userid_raises_commanderror(command):
    # call_command surfaces argparse's required-argument failure as a
    # CommandError (Django CommandParser), not SystemExit.
    with pytest.raises(CommandError, match=re.escape("--userId")):
        call_command(command, *BASE_ARGS)


class _CapturingLogger:
    """Stands in for the module's CustomLogger: no Slack WebClient, no I/O."""

    last = None

    def __init__(self):
        self.messages = []
        _CapturingLogger.last = self

    def log(self, message):
        self.messages.append(message)

    def send_messages_to_slack(self):
        pass


@pytest.mark.parametrize("command", COMMANDS)
def test_since_window_reaches_scan_date_window_filter(
    command, migrated_db, monkeypatch, storage_fake
):
    """End-to-end wiring: the computed window must reach the scan.

    Same intent as the story-1.4 test this replaces (the window computed
    in handle() must actually reach the walk, or the cron's --since filter
    is silently dead while every pure-helper test stays green).

    Story 2.6 changed the SEMANTICS: the window became its own depth-1
    filter instead of being appended in place onto `args.only`, which is
    an operator filter applying at every depth, so `only` must arrive
    UNTOUCHED. Story 2.8 changed only the CHANNEL again — both now travel
    on the run context as `RunOptions` fields (tuples, because the context
    is frozen) instead of as loose kwargs of a recursion that lived in the
    command module. `Folder.scan_tree` is where they arrive.

    `Folder.scan_tree` (PLUGIN code, not Portal) and the command module's
    OWN `CustomLogger` are minimally monkeypatched (sanctioned — since
    story 1.5 the real config read tolerates an absent portal.conf via
    fallback=None, so no ConfigParser stub is needed); since story 2.1 the
    storage resolves through the counting StorageHelper fake, so no storage
    cache preset is needed.
    """
    module = importlib.import_module(
        f"portal.plugins.TapelessIngest.management.commands.{command}"
    )
    captured = {}

    def fake_scan_tree(self, ctx, *, emit):
        captured["parent_folder"] = self
        captured["context"] = ctx
        captured["date_window"] = list(ctx.options.date_window)
        captured["only"] = list(ctx.options.only)
        return None

    monkeypatch.setattr(Folder, "scan_tree", fake_scan_tree)
    monkeypatch.setattr(module, "CustomLogger", _CapturingLogger)
    # No cache.set("storage:VX-41", ...) preset any more (story 2.1): tree
    # mode resolves the storage through the counting StorageHelper fake in
    # tests/portal_stub, and handle()'s log line reads the run context.
    storage_fake.set_root("VX-41", "/wired-root")

    user = User.objects.create(pk=4242, username=f"story14-wiring-{command}")
    now_before = datetime.now()
    try:
        call_command(command, *BASE_ARGS, "--userId", "4242", "--since", "1d")
    finally:
        user.delete()  # keep the auth table empty for the unknown-user tests
    now_after = datetime.now()

    # Two candidate windows tolerate a midnight rollover mid-test.
    candidates = [
        [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]
        for now in (now_before, now_after)
    ]
    # The window travels as its own depth-1 parameter...
    assert captured["date_window"] in candidates
    # ...and `only` is no longer mutated: --only was not given, so the
    # recursion receives the empty operator filter it was handed.
    assert captured["only"] == []

    # Story 2.1 wiring: handle() built one ScanContext carrying the
    # resolved root, threaded it into the recursion, and seeded the
    # top-level folder's memoized root from it (real once-per-run).
    context = captured["context"]
    assert isinstance(context, ScanContext)
    assert context.root_path_for("VX-41") == "/wired-root"
    assert captured["parent_folder"]._root_path == "/wired-root"
    assert storage_fake.get_storage_calls == {"VX-41": 1}

    # Exactly one range line, no per-day lines, no legacy "Scanning from" line.
    window_lines = [
        m
        for m in _CapturingLogger.last.messages
        if m.startswith("Scanning folders from ")
    ]
    assert window_lines == [
        f"Scanning folders from {captured['date_window'][0]} to "
        f"{captured['date_window'][-1]} (2 day folders)"
    ]
    assert not any(
        m.startswith("Scanning from ") for m in _CapturingLogger.last.messages
    )

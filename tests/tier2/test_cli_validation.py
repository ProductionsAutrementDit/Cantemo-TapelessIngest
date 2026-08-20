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
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

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
def test_since_window_reaches_scan_only_filter(command, migrated_db, monkeypatch):
    """End-to-end wiring: the computed window must reach the scan's `only`.

    Guards the aliasing contract — `only = only + date_window` instead of
    `only += date_window` would silently disable the cron's --since filter
    while every pure-helper test stayed green. The command module's OWN
    `scan_tapeless_dir` and `CustomLogger` attributes are minimally
    monkeypatched (our module, sanctioned — not portal mocking; since story
    1.5 the real config read tolerates an absent portal.conf via
    fallback=None, so no ConfigParser stub is needed), and the storage
    cache is pre-seeded so Folder.storage never calls the no-op stub
    StorageHelper.
    """
    module = importlib.import_module(
        f"portal.plugins.TapelessIngest.management.commands.{command}"
    )
    captured = {}

    def fake_scan(parent_folder, **kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(module, "scan_tapeless_dir", fake_scan)
    monkeypatch.setattr(module, "CustomLogger", _CapturingLogger)
    cache.set("storage:VX-41", "VX-41", 300)

    user = User.objects.create(pk=4242, username=f"story14-wiring-{command}")
    now_before = datetime.now()
    try:
        call_command(command, *BASE_ARGS, "--userId", "4242", "--since", "1d")
    finally:
        user.delete()  # keep the auth table empty for the unknown-user tests
        cache.delete("storage:VX-41")
    now_after = datetime.now()

    # Two candidate windows tolerate a midnight rollover mid-test.
    candidates = [
        [(now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")]
        for now in (now_before, now_after)
    ]
    # args.only starts [] and handle() appends the window in place, so the
    # very list object the scan received must carry the window values.
    assert captured["only"] in candidates

    # Exactly one range line, no per-day lines, no legacy "Scanning from" line.
    window_lines = [
        m
        for m in _CapturingLogger.last.messages
        if m.startswith("Scanning folders from ")
    ]
    assert window_lines == [
        f"Scanning folders from {captured['only'][0]} to {captured['only'][-1]} "
        f"(2 day folders)"
    ]
    assert not any(
        m.startswith("Scanning from ") for m in _CapturingLogger.last.messages
    )

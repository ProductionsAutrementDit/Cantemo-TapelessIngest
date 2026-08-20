"""Tier 2: fail-fast CLI validation via call_command on both commands.

Every `pytest.raises(CommandError)` pins `match=` to the exact matrix
message, so a validation regression that reaches a later stage (e.g. the
user lookup) fails the test instead of passing on the wrong error —
order-independent proof that the abort precedes any DB/config work.
"""

import re

import pytest
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
def test_missing_required_userid_raises_commanderror(command):
    # call_command surfaces argparse's required-argument failure as a
    # CommandError (Django CommandParser), not SystemExit.
    with pytest.raises(CommandError, match=re.escape("--userId")):
        call_command(command, *BASE_ARGS)

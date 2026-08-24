"""Tier 1: providers/red.py — REDline resolution and honest failure.

The regression these pin is a production one: cron runs with
``PATH=/sbin:/bin:/usr/sbin:/usr/bin`` (declared in ``/etc/crontab``),
REDline lives in ``/usr/local/bin``, so the bare ``REDline`` shell-out
exited 127 with an empty stdout. ``csv.DictReader`` then yielded no row,
``getAllClipMetadatas`` returned the metadatas untouched, and every R3D
file in the run died on ``Clip.extract_file_metadatas``'s generic
"No UMID found in file" — a message that accuses the media when the
media is fine. 68 clips of one shoot, zero ingested, no usable signal.

Two properties are pinned here, and they are independent:

- resolution: an operator-configured path wins, then ``shutil.which``,
  then the well-known install locations. A PATH that does not carry
  ``/usr/local/bin`` must NOT be able to break RED any more.
- honesty: no data row out of REDline raises an error naming REDline,
  its exit status and its stderr — never a silent metadatas dict that
  gets rewritten into a UMID complaint two frames up the stack.

The exit-status row is the trap in this fix: REDline exits **1 on
success** (verified against a real KOMODO 6K .R3D that prints a full
CSV row). Any guard written as ``returncode != 0`` breaks the working
path, so the emptiness of the output — not the exit code — is what
decides.
"""

import os
import stat

import pytest

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.providers import red as red_module

# A trimmed REDline --printMeta 3 CSV: only the columns the provider reads,
# in REDline's own order, with the trailing comma real output carries.
REDLINE_HEADER = (
    "Clip Name,Camera Model,Camera PIN,UUID,Date,Timestamp,Abs TC,"
)
REDLINE_ROW = (
    "K001_K001_0804TX,KOMODO 6K,KMDBK006080,"
    "42B681D6-2AB5-46A2-8DEA-B1C05BD0CA54,20260804,103910,10:39:10:00,"
)
REDLINE_CSV = f"{REDLINE_HEADER}\n{REDLINE_ROW}\n"


class _CompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def no_configured_path(monkeypatch):
    """No operator override: the DB is not reachable in Tier 1 anyway."""
    monkeypatch.setattr(red_module, "configured_redline_path", lambda: "")


def _make_executable(directory, name="REDline"):
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_configured_path_wins_over_everything(monkeypatch, tmp_path):
    """An operator-set Settings.redline_path is authoritative."""
    monkeypatch.setattr(
        red_module, "configured_redline_path", lambda: "/opt/custom/REDline"
    )
    monkeypatch.setattr(red_module.shutil, "which", lambda _: "/usr/bin/REDline")

    assert red_module.resolve_redline_path() == "/opt/custom/REDline"


def test_which_is_used_when_no_path_is_configured(
    monkeypatch, no_configured_path, tmp_path
):
    found = _make_executable(tmp_path)
    monkeypatch.setattr(red_module.shutil, "which", lambda _: found)

    assert red_module.resolve_redline_path() == found


def test_cron_path_without_usr_local_bin_still_resolves(
    monkeypatch, no_configured_path, tmp_path
):
    """THE production regression.

    ``shutil.which`` answers None because the cron PATH does not carry
    ``/usr/local/bin``; the well-known-location fallback is what keeps
    RED alive. Before the fix this is where the provider gave up and
    handed back a metadatas dict with no umid.
    """
    installed = _make_executable(tmp_path)
    monkeypatch.setenv("PATH", "/sbin:/bin:/usr/sbin:/usr/bin")
    monkeypatch.setattr(red_module, "REDLINE_FALLBACK_PATHS", (installed,))

    assert red_module.resolve_redline_path() == installed


def test_unresolvable_redline_raises_naming_the_binary(
    monkeypatch, no_configured_path
):
    monkeypatch.setattr(red_module.shutil, "which", lambda _: None)
    monkeypatch.setattr(red_module, "REDLINE_FALLBACK_PATHS", ())

    with pytest.raises(TapelessIngestException) as excinfo:
        red_module.resolve_redline_path()

    message = str(excinfo.value)
    assert "REDline" in message
    # The operator must learn WHERE to fix it, not just that it broke.
    assert "redline_path" in message


def test_non_executable_candidate_is_not_accepted(
    monkeypatch, no_configured_path, tmp_path
):
    """A readable-but-not-executable file is not a usable binary."""
    candidate = tmp_path / "REDline"
    candidate.write_text("not executable\n")
    candidate.chmod(0o644)
    monkeypatch.setattr(red_module.shutil, "which", lambda _: None)
    monkeypatch.setattr(red_module, "REDLINE_FALLBACK_PATHS", (str(candidate),))

    with pytest.raises(TapelessIngestException):
        red_module.resolve_redline_path()


# --------------------------------------------------------------------------
# Honest failure
# --------------------------------------------------------------------------


def test_success_row_is_parsed_despite_exit_status_one(monkeypatch, tmp_path):
    """REDline exits 1 on SUCCESS — the output decides, not the code."""
    monkeypatch.setattr(
        red_module, "resolve_redline_path", lambda: "/usr/local/bin/REDline"
    )
    monkeypatch.setattr(
        red_module.sp,
        "run",
        lambda *a, **kw: _CompletedProcess(stdout=REDLINE_CSV, returncode=1),
    )

    provider = red_module.Provider()
    metadatas = provider.getAllClipMetadatas("/mnt/x/K001_K001_0804TX_001.R3D", {})

    assert metadatas["umid"] == "42B681D6-2AB5-46A2-8DEA-B1C05BD0CA54"
    assert metadatas["clipname"] == "K001_K001_0804TX"
    assert metadatas["device_manufacturer"] == "RED"
    assert metadatas["device_model"] == "KOMODO 6K"
    assert metadatas["shooting_date"] == "2026-08-04T10:39:10"


def test_empty_output_raises_naming_redline_and_stderr(monkeypatch):
    """The exact production failure: exit 127, nothing on stdout.

    The old code returned `metadatas` untouched here, and the caller
    turned that into "No UMID found in file <path>". The provider must
    now say what actually happened.
    """
    monkeypatch.setattr(
        red_module, "resolve_redline_path", lambda: "/usr/local/bin/REDline"
    )
    monkeypatch.setattr(
        red_module.sp,
        "run",
        lambda *a, **kw: _CompletedProcess(
            stdout="", stderr="/bin/sh: REDline: command not found\n", returncode=127
        ),
    )

    provider = red_module.Provider()
    with pytest.raises(TapelessIngestException) as excinfo:
        provider.getAllClipMetadatas("/mnt/x/K001_K001_0804TX_001.R3D", {})

    message = str(excinfo.value)
    assert "REDline" in message
    assert "127" in message
    assert "command not found" in message
    # The misleading message must NOT be what the operator sees.
    assert "No UMID found" not in message


def test_header_without_data_row_raises(monkeypatch):
    """REDline printed its header and died — still no metadata."""
    monkeypatch.setattr(
        red_module, "resolve_redline_path", lambda: "/usr/local/bin/REDline"
    )
    monkeypatch.setattr(
        red_module.sp,
        "run",
        lambda *a, **kw: _CompletedProcess(
            stdout=f"{REDLINE_HEADER}\n",
            stderr="Unable to load clip\nReason: Failed to open file\n",
            returncode=1,
        ),
    )

    provider = red_module.Provider()
    with pytest.raises(TapelessIngestException) as excinfo:
        provider.getAllClipMetadatas("/mnt/x/missing.R3D", {})

    assert "Failed to open file" in str(excinfo.value)


def test_row_without_uuid_column_raises_instead_of_keyerror(monkeypatch):
    """A REDline whose CSV shape changed must not surface as a KeyError."""
    monkeypatch.setattr(
        red_module, "resolve_redline_path", lambda: "/usr/local/bin/REDline"
    )
    monkeypatch.setattr(
        red_module.sp,
        "run",
        lambda *a, **kw: _CompletedProcess(
            stdout="Clip Name,Date,Timestamp,\nK001_K001_0804TX,20260804,103910,\n",
            returncode=1,
        ),
    )

    provider = red_module.Provider()
    with pytest.raises(TapelessIngestException) as excinfo:
        provider.getAllClipMetadatas("/mnt/x/K001.R3D", {})

    assert "UUID" in str(excinfo.value)


def test_redline_is_invoked_without_a_shell(monkeypatch):
    """No shell: the resolved absolute path is argv[0], the media path argv-safe.

    Spaces in the media path (``AA - RUSHES TAPELESS`` on the production
    storage) were previously handled by ``shlex.quote`` into a shell
    string; passing a real argv removes the quoting question entirely.
    """
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _CompletedProcess(stdout=REDLINE_CSV, returncode=1)

    monkeypatch.setattr(
        red_module, "resolve_redline_path", lambda: "/usr/local/bin/REDline"
    )
    monkeypatch.setattr(red_module.sp, "run", _fake_run)

    provider = red_module.Provider()
    media = "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2026/AA_x/K001_001.R3D"
    provider.getAllClipMetadatas(media, {})

    assert captured["kwargs"].get("shell") is not True
    assert captured["cmd"][0] == "/usr/local/bin/REDline"
    assert media in captured["cmd"]

"""Tier 1: providers/braw.py — brawprobe resolution, mirroring REDline's.

Cron's PATH (`/sbin:/bin:/usr/sbin:/usr/bin`) does not carry
/usr/local/bin, so the binary must be found without it: the operator's
`Settings.brawprobe_path`, then PATH, then `/usr/local/bin/brawprobe` —
and what is run is always a path, never a bare name.
"""

import stat

import pytest

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.providers import braw as braw_module


@pytest.fixture
def no_configured_path(monkeypatch):
    monkeypatch.setattr(braw_module, "configured_brawprobe_path", lambda: "")


def _make_executable(directory, name="brawprobe"):
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_configured_path_wins_over_everything(monkeypatch, tmp_path):
    custom = tmp_path / "custom"
    custom.mkdir()
    configured = _make_executable(custom)
    monkeypatch.setattr(braw_module, "configured_brawprobe_path", lambda: configured)
    monkeypatch.setenv("PATH", str(tmp_path))
    _make_executable(tmp_path)
    assert braw_module.resolve_brawprobe_path() == configured


def test_path_lookup_comes_second(no_configured_path, monkeypatch, tmp_path):
    found = _make_executable(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert braw_module.resolve_brawprobe_path() == found


def test_the_known_location_is_found_under_crons_path(
    no_configured_path, monkeypatch, tmp_path
):
    installed = _make_executable(tmp_path)
    monkeypatch.setenv("PATH", "/sbin:/bin:/usr/sbin:/usr/bin")
    monkeypatch.setattr(braw_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(braw_module, "BRAWPROBE_FALLBACK_PATHS", (installed,))
    assert braw_module.resolve_brawprobe_path() == installed


def test_the_shipped_fallback_is_usr_local_bin():
    assert braw_module.BRAWPROBE_FALLBACK_PATHS == ("/usr/local/bin/brawprobe",)


def test_a_non_executable_fallback_is_skipped(
    no_configured_path, monkeypatch, tmp_path
):
    plain = tmp_path / "brawprobe"
    plain.write_text("")
    monkeypatch.setattr(braw_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(braw_module, "BRAWPROBE_FALLBACK_PATHS", (str(plain),))
    with pytest.raises(TapelessIngestException):
        braw_module.resolve_brawprobe_path()


def test_nothing_found_names_the_setting(no_configured_path, monkeypatch):
    monkeypatch.setattr(braw_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        braw_module, "BRAWPROBE_FALLBACK_PATHS", ("/nonexistent/brawprobe",)
    )
    with pytest.raises(TapelessIngestException) as raised:
        braw_module.resolve_brawprobe_path()
    message = str(raised.value)
    assert "brawprobe_path" in message
    assert "/nonexistent/brawprobe" in message


def test_a_settings_failure_degrades_to_discovery(monkeypatch):
    """A failing settings read (no row, no DB, an older schema) is "not
    configured", so resolution falls through to discovery."""
    calls = []

    class _Failing:
        def get(self, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("no settings table")

    monkeypatch.setattr(braw_module.Settings, "objects", _Failing())
    assert braw_module.configured_brawprobe_path() == ""
    assert calls == [{"pk": 1}]


@pytest.mark.parametrize(
    "bad", ["brawprobe", "bin/brawprobe", "/nonexistent/brawprobe"]
)
def test_a_configured_path_must_be_an_absolute_executable(monkeypatch, tmp_path, bad):
    monkeypatch.setattr(braw_module, "configured_brawprobe_path", lambda: bad)
    monkeypatch.setenv("PATH", str(tmp_path))
    _make_executable(tmp_path)  # discovery would succeed: it must not be reached
    with pytest.raises(TapelessIngestException, match="Settings.brawprobe_path"):
        braw_module.resolve_brawprobe_path()


def test_a_configured_non_executable_file_is_refused(monkeypatch, tmp_path):
    plain = tmp_path / "brawprobe"
    plain.write_text("")
    monkeypatch.setattr(braw_module, "configured_brawprobe_path", lambda: str(plain))
    with pytest.raises(TapelessIngestException, match="Settings.brawprobe_path"):
        braw_module.resolve_brawprobe_path()


def test_the_settings_model_carries_the_field():
    from portal.plugins.TapelessIngest.models.settings import Settings

    field = Settings._meta.get_field("brawprobe_path")
    assert field.max_length == 255
    assert field.blank and field.default == ""

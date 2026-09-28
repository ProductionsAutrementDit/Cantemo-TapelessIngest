"""Tier 1: the migration command registers every model its checks need.

`Clip.folders` references `Folder` lazily. On prod, Portal's plugin loading
never imports `models.folder`, so a command that does not import it itself
fails Django's system checks with fields.E307 before `handle` runs
(measured 2026-09-28). A fresh interpreter is used because the test session
has already imported `Folder` through other tests.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

PROBE = """
import sys
sys.path.insert(0, {root!r})
from tests.portal_stub import install
install()
import os
os.environ["DJANGO_SETTINGS_MODULE"] = "tests.tier2_settings"
import django
django.setup()
import portal.plugins.TapelessIngest.management.commands.migrate_wrapped_items  # noqa
print("portal.plugins.TapelessIngest.models.folder" in sys.modules)
"""


def test_importing_the_command_registers_folder():
    result = subprocess.run(
        [sys.executable, "-c", PROBE.format(root=str(REPO_ROOT))],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True", result.stdout + result.stderr

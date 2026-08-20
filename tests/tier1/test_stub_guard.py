"""Tier 1: portal_stub's clobber guard actually fires.

install() must refuse to overwrite a real (non-stub) already-imported
distribution. In this pytest process the stub is already installed, so the
guard is exercised in a subprocess mirroring build_golden_doc's bootstrap:
pre-insert a marker-less module under a stubbed root, then call install().
Without this test the guard would be silently dead if its polarity ever
regressed.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The seeding below is NOT Portal mocking (AD-11): the throwaway subprocess
# plants a marker-less "real" module precisely so install() refuses to run;
# no plugin code ever executes against it.
GUARD_SCRIPT = """
import sys
from types import ModuleType

sys.path.insert(0, {repo_root!r})

# A real, already-imported distribution: no __portal_stub__ marker.
sys.modules.update(VidiRest=ModuleType("VidiRest"))

from tests.portal_stub import install

install()
"""


def test_install_refuses_to_clobber_real_distribution():
    result = subprocess.run(
        [sys.executable, "-c", GUARD_SCRIPT.format(repo_root=str(REPO_ROOT))],
        capture_output=True,
        timeout=60,
    )
    stderr = result.stderr.decode(errors="replace")
    assert result.returncode != 0, (
        f"install() silently accepted a real already-imported distribution "
        f"(rc=0):\n{stderr}"
    )
    assert "refusing to stub" in stderr
    # The offending root must be named — a real pyxb must never be reported
    # as "portal".
    assert "VidiRest" in stderr

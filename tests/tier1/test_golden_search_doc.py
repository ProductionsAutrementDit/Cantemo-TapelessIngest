"""Tier 1 golden-doc pin (AD-3): build_search_doc output, byte-for-byte.

The doc is computed in a subprocess with PYTHONHASHSEED=0 so plain
`pytest tests/` stays green regardless of the operator's environment
(`list(set(...))` in build_search_doc makes list order hash-seed-dependent).
A mismatch is a test failure — never auto re-record; re-recording requires
human sign-off (a CPython upgrade may legitimately reorder the should-lists).
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDER = REPO_ROOT / "tests" / "tier1" / "build_golden_doc.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "golden_search_doc.json"

GOLDEN_STORAGE_ID = "VX-41"
GOLDEN_PATH = "2026/AH_20260101_golden"


def test_golden_doc_bytes_match_fixture():
    env = dict(os.environ, PYTHONHASHSEED="0")
    result = subprocess.run(
        [sys.executable, str(RECORDER)],
        env=env,
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"golden recorder failed (rc={result.returncode}):\n"
        f"{result.stderr.decode(errors='replace')}"
    )
    assert result.stdout == FIXTURE.read_bytes(), (
        "build_search_doc output diverged from the committed golden fixture "
        "(AD-3): never auto re-record — a diff means the legacy query changed "
        "or the interpreter's set ordering changed; human sign-off required"
    )


def test_fixture_contains_golden_inputs():
    """Self-check against a mis-bound recorder producing deterministic garbage."""
    text = FIXTURE.read_text()
    assert f'"storage": "{GOLDEN_STORAGE_ID}"' in text
    assert re.escape(GOLDEN_PATH) in text

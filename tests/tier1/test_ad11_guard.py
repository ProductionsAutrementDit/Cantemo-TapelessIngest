"""AD-11 guard: the prose stub rule as an executable invariant.

tests/portal_stub/ is the ONLY sanctioned mocking of Portal and the
Cantemo-only distributions. This test walks the test tree and fails on any
other Portal mocking: `mock.patch("portal...")`, `monkeypatch` applied to
the stubbed distributions, or direct `sys.modules` writes.
"""

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()

STUBBED_DISTS = r"(?:portal|VidiRest|RestAPIBase|pyxb)"

FORBIDDEN_PATTERNS = [
    # unittest.mock patching of any stubbed distribution
    re.compile(r"""mock\.patch(?:\.\w+)?\(\s*["']""" + STUBBED_DISTS + r"[.\"']"),
    re.compile(r"""\bpatch(?:\.\w+)?\(\s*["']""" + STUBBED_DISTS + r"[.\"']"),
    # monkeypatch applied to a stubbed distribution
    re.compile(r"""monkeypatch\.\w+\(\s*["']?""" + STUBBED_DISTS + r"""[."']"""),
    # monkeypatch reaching into sys.modules
    re.compile(r"monkeypatch\.\w+\(\s*sys\.modules"),
    # direct sys.modules writes (reads are fine, e.g. identity assertions)
    re.compile(r"sys\.modules\[[^\]]*\]\s*="),
    re.compile(r"del\s+sys\.modules\["),
]


def _scannable_files():
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if "portal_stub" in path.parts:
            continue
        if path.name == "conftest.py":
            continue
        if path == THIS_FILE:
            # This file necessarily spells out the forbidden patterns.
            continue
        yield path


def test_no_portal_mocking_outside_portal_stub():
    violations = []
    for path in _scannable_files():
        source = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    violations.append(f"{path}:{lineno}: {line.strip()}")
    assert not violations, (
        "AD-11 violation — Portal/Cantemo-dep mocking outside tests/portal_stub/:\n"
        + "\n".join(violations)
    )


def test_guard_actually_scans_something():
    assert any(_scannable_files()), "AD-11 guard found no test files to scan"

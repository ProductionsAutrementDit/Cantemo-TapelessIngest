"""Tier 1 (story 2.1): scan/context.py — Portal-free, frozen, canonical block.

The dataclasses and ``browse_root_path`` are exercised with plain local
duck-typed fakes (not Portal mocking — no stubbed distribution is touched);
Portal-freedom itself is proven in a bare subprocess with NO stub installed.
"""

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.scan.context import (
    MAX_WORKERS,
    PhaseTimings,
    RunOptions,
    ScanContext,
    StorageInfo,
    browse_root_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# cwd=repo root, no stub, no conftest: `python -c` puts the cwd on sys.path,
# so `scan.context` resolves to this repo's package in a bare interpreter.
PORTAL_FREEDOM_SCRIPT = (
    "import sys, scan.context; "
    "assert not [m for m in sys.modules if m == 'portal' or m.startswith('portal.')]"
)


class _Method:
    """Local duck-typed storage-method fake (getBrowse/getFirstURI)."""

    def __init__(self, browse, url=None):
        self._browse = browse
        self._url = url

    def getBrowse(self):
        return self._browse

    def getFirstURI(self):
        return {"url": self._url}


class _Storage:
    """Local duck-typed storage fake (getMethods)."""

    def __init__(self, methods):
        self._methods = methods

    def getMethods(self):
        return self._methods


def _context(storages=None, **options):
    return ScanContext(storages=storages or {}, options=RunOptions(**options))


def test_scan_context_imports_portal_free_in_subprocess():
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_FREEDOM_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan.context is not Portal-free in a bare interpreter (AD-1):\n"
        f"{result.stderr.decode(errors='replace')}"
    )


def test_browse_root_path_falsy_storage_is_none():
    assert browse_root_path(None) is None


def test_browse_root_path_no_browse_method_is_none():
    storage = _Storage([_Method(browse=False, url="/never")])
    assert browse_root_path(storage) is None
    assert browse_root_path(_Storage([])) is None


def test_browse_root_path_returns_browse_method_url():
    storage = _Storage(
        [_Method(browse=False, url="/not-this"), _Method(browse=True, url="/root")]
    )
    assert browse_root_path(storage) == "/root"


def test_browse_root_path_first_browse_method_wins():
    # Review-ruled contract: FIRST match. (The pre-2.1 property-site loops
    # let the LAST browse method win — deliberately unified; no storage in
    # practice has two browse methods.)
    storage = _Storage(
        [_Method(browse=True, url="/first"), _Method(browse=True, url="/second")]
    )
    assert browse_root_path(storage) == "/first"


def test_browse_root_path_tolerates_uri_without_url():
    class _NoURIMethod:
        def getBrowse(self):
            return True

        def getFirstURI(self):
            return None

    class _NoURLKeyMethod:
        def getBrowse(self):
            return True

        def getFirstURI(self):
            return {}

    assert browse_root_path(_Storage([_NoURIMethod()])) is None
    assert browse_root_path(_Storage([_NoURLKeyMethod()])) is None


def test_storage_info_and_run_options_are_frozen():
    info = StorageInfo(id="VX-41", root_path="/root")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.root_path = "/other"
    options = RunOptions(dry_run=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        options.dry_run = False


def test_scan_context_field_assignment_raises():
    ctx = _context()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.storages = {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.provider_registry = object()


def test_scan_context_storages_mapping_is_read_only():
    ctx = _context({"VX-41": StorageInfo(id="VX-41", root_path="/root")})
    with pytest.raises(TypeError):
        ctx.storages["VX-99"] = StorageInfo(id="VX-99", root_path=None)
    with pytest.raises(TypeError):
        del ctx.storages["VX-41"]


def test_timings_is_the_single_designated_mutable_slot():
    ctx = _context()
    assert ctx.timings == PhaseTimings()
    ctx.timings.discovery = 1.5
    ctx.timings.ingest = 0.25
    assert (ctx.timings.discovery, ctx.timings.ingest) == (1.5, 0.25)


def test_extension_points_default_none():
    ctx = _context()
    assert ctx.provider_registry is None
    assert ctx.extension_map is None


def test_root_path_for_and_absolute_path_for():
    ctx = _context(
        {
            "VX-41": StorageInfo(id="VX-41", root_path="/mnt/root"),
            "VX-EMPTY": StorageInfo(id="VX-EMPTY", root_path=""),
            "VX-NONE": StorageInfo(id="VX-NONE", root_path=None),
        }
    )
    assert ctx.root_path_for("VX-41") == "/mnt/root"
    assert ctx.root_path_for("VX-MISSING") is None
    assert ctx.root_path_for("VX-NONE") is None
    assert ctx.absolute_path_for("VX-41", "2026/AH_x") == "/mnt/root/2026/AH_x"
    # Falsy root ("" included) and misses resolve to None — callers fall
    # back to today's property chain with its truthiness semantics.
    assert ctx.absolute_path_for("VX-EMPTY", "2026/AH_x") is None
    assert ctx.absolute_path_for("VX-MISSING", "2026/AH_x") is None
    # A None path never reaches os.path.join.
    assert ctx.absolute_path_for("VX-41", None) is None


# --------------------------------------------------------------------------
# Retro-3 F4: the worker bound is enforced at the DATACLASS, not just the CLI
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workers",
    [0, -1, "4", 3.0, None, True, MAX_WORKERS + 1, 500],
    ids=["zero", "negative", "string", "float", "none", "bool", "over", "way-over"],
)
def test_run_options_refuses_an_invalid_worker_count(workers):
    """An invalid width must be UNCONSTRUCTABLE, not merely un-typeable.

    The CLI validates `--workers` at parse time, but the [1, MAX_WORKERS]
    bound lived ONLY there: a programmatic caller could smuggle 0, a
    negative, a bool or a string into the frozen options and fail deep
    inside the run — or, off a truthy `"0"`, silently take the sequential
    path and never fail at all. `True` is the discriminating case: it IS
    an `int` equal to 1, so only an explicit `bool` exclusion catches a
    caller who meant "yes, use workers" and got a one-worker run.
    """
    with pytest.raises(ValueError, match="workers must be"):
        RunOptions(workers=workers)


def test_run_options_names_the_field_and_the_bound_in_its_messages():
    """The DATACLASS's own two messages — deliberately not the CLI's.

    The CLI's argparse validator says `--workers must be >= 1`, naming
    the FLAG, because that is what its reader typed; this one names the
    FIELD, because its reader wrote Python. The two producers are
    independent and their wording differs on purpose. What they share is
    the bound and the verdict, and the CLI half is pinned separately in
    tests/tier2/test_cli_validation.py.
    """
    with pytest.raises(ValueError, match=r"workers must be an int >= 1 \(got 0\)"):
        RunOptions(workers=0)
    with pytest.raises(ValueError, match=r"workers must be <= 16 \(got 17\)"):
        RunOptions(workers=MAX_WORKERS + 1)


@pytest.mark.parametrize("workers", [1, 2, MAX_WORKERS])
def test_run_options_accepts_every_legal_width(workers):
    """Both ends of the range, and the pre-3.1 default, still construct."""
    assert RunOptions(workers=workers).workers == workers
    assert RunOptions().workers == 1
    # `replace` re-runs __post_init__ too — no back door around the bound.
    assert dataclasses.replace(RunOptions(), workers=workers).workers == workers

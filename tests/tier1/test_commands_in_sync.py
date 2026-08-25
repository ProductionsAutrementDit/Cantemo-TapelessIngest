"""Tier 1: the two commands stay line-for-line identical in shared blocks.

`scan_tapeless_dir` and `check_clips_in_folder` deliberately share their
validation helpers and their entire `Command.handle`; this source-equality
test enforces the Boundaries clause of story 1.4 so symmetric edits cannot
drift apart.
"""

import inspect

import pytest

from portal.plugins.TapelessIngest.management.commands import (
    check_clips_in_folder as check_mod,
)
from portal.plugins.TapelessIngest.management.commands import (
    scan_tapeless_dir as scan_mod,
)

SHARED_HELPERS = [
    "parse_since",
    "parse_from",
    "compute_date_window",
    "format_window_log",
    # Story 3.1: the --workers parse-time validator ([1, MAX_WORKERS]).
    "positive_worker_count",
    # Story 2.6 added `should_scan_entry` and `consumed_subdirs_from_results`
    # here as a stopgap: each command carried its own copy of the recursion,
    # and check_clips_in_folder's copy landed unexecuted and uncompared, so
    # the two drift-prone halves were extracted into helpers this test DOES
    # cover — explicitly "until 2.8/FR-37 rebuilds the surrounding call
    # site". That is this story. There is now exactly ONE walk, in
    # scan/coordinator.py, and neither command has a recursion to drift.
    # `should_scan_entry` moved there with it; the descent-authorization
    # read became `FolderOutcome.consumed_subdirs`. What is left here is
    # 1.4's four validators — plus `handle()` below, which is now the whole
    # of both commands' behavior and is compared in full.
]


@pytest.mark.parametrize("name", SHARED_HELPERS)
def test_shared_helper_sources_identical(name):
    assert inspect.getsource(getattr(scan_mod, name)) == inspect.getsource(
        getattr(check_mod, name)
    )


def test_since_re_patterns_identical():
    assert scan_mod.SINCE_RE.pattern == check_mod.SINCE_RE.pattern


def test_handle_sources_identical():
    assert inspect.getsource(scan_mod.Command.handle) == inspect.getsource(
        check_mod.Command.handle
    )

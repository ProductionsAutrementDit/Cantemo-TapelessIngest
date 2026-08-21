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
    # Story 2.6: the recursion's drift-prone halves. check_clips_in_folder's
    # own recursion lands unexecuted and uncompared (the sync test compares
    # helpers and handle(), never scan_tapeless_dir itself, and the command
    # still carries its two fatal preserved defects), so the filter decision
    # and the descent-authorization read are extracted into helpers this
    # test DOES cover, until 2.8/FR-37 rebuilds the surrounding call site.
    "should_scan_entry",
    "consumed_subdirs_from_results",
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

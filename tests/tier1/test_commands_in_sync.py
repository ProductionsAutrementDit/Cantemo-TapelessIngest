"""Tier 1: the two commands stay line-for-line identical in shared blocks.

`scan_tapeless_dir` and `check_clips_in_folder` deliberately share their
validation helpers and their entire `Command.handle`; this source-equality
test enforces the Boundaries clause of story 1.4 so symmetric edits cannot
drift apart.
"""

import inspect
import re

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


def test_add_arguments_sources_identical():
    """Retro-3 F4: the argument surface was the one shared block no pin covered.

    `handle()` is compared in full above, but a `--workers` (or any flag)
    diverging between the two parsers would ship silently — the twin
    commands' contract is that ONLY `FORCE_DRY_RUN` differs.
    """
    assert inspect.getsource(scan_mod.Command.add_arguments) == inspect.getsource(
        check_mod.Command.add_arguments
    )


# Constants that must live exactly once, in scan/context.py, and be
# IMPORTED by both commands. `LEGACY_STORAGES` joined `MAX_WORKERS` in the
# retro-3 review: it sat below each command's `logger = None` line, which
# is where the compared Slack band ends, so a mutation diverging the two
# per-file copies left all 719 tests green.
SHARED_CONSTANTS = ["MAX_WORKERS", "LEGACY_STORAGES"]


@pytest.mark.parametrize("name", SHARED_CONSTANTS)
def test_both_commands_use_the_shared_constant(name):
    """The constant lives ONCE, in scan/context.py.

    Value equality alone cannot catch a resurrected per-file copy (16 is
    a small interned int, and a duplicated literal list compares equal),
    so the pin is structural too: neither command module may carry its
    own binding of the name. The literal itself is deliberately NOT
    repeated here — asserting `== 16` would re-duplicate the very thing
    this test exists to de-duplicate. The three names are compared to
    each other, and scan/context.py is the only place the value appears.
    """
    from portal.plugins.TapelessIngest.scan import context

    shared = getattr(context, name)
    assert getattr(scan_mod, name) is shared
    assert getattr(check_mod, name) is shared

    # `(:[^=]+)?` catches an ANNOTATED rebinding (`MAX_WORKERS: int = 8`),
    # which the bare `\s*=` form sailed straight past.
    per_file_binding = re.compile(rf"^{name}\s*(:[^=]+)?=", re.MULTILINE)
    for module in (scan_mod, check_mod):
        assert not per_file_binding.search(inspect.getsource(module)), (
            f"{module.__name__} rebinds {name} instead of importing "
            f"the shared value from scan.context"
        )

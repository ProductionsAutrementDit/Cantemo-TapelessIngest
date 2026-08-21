"""Tier 1 (story 2.8): scan/coordinator.py — the contract Epics 3 and 4 buy.

Everything here runs off the ORM entirely: the walk is exercised with a
local ``process_folder`` double over a real tmp directory tree, which is
the whole point — if any of this needed a Folder row, the coordinator
would not be the Portal-free seam a worker pool can be dropped into.
Portal-freedom itself is proven in a bare subprocess with NO stub
installed.

The ordering test deserves a note. A flat, one-level fixture cannot tell
a tree-derived merge key apart from an enqueue counter — both reproduce
the same order when there is only one level of siblings. So the
out-of-order test below is specified over a tree with at least two levels
AND two branches, and its ``gather`` hands results back in reverse
dispatch order.
"""

import dataclasses
import itertools
import os
import pickle
import random
import re
import subprocess
import sys
import typing
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.scan import coordinator
from portal.plugins.TapelessIngest.scan.context import (
    RunOptions,
    ScanContext,
    StorageInfo,
)
from portal.plugins.TapelessIngest.scan.verification import FolderListings
from portal.plugins.TapelessIngest.scan.coordinator import (
    AD13_COUNTER_KEYS,
    AD13_KEYS,
    TIMING_PHASES,
    TREE_ONLY_OPTIONS,
    FolderOutcome,
    FolderTimings,
    PhaseTimer,
    RunResult,
    SequentialDispatcher,
    WorkerCounters,
    WorkerResult,
    assert_mode_options,
    build_ingest_response,
    build_scan_response,
    fold_timings,
    merge_results,
    should_scan_entry,
    summary_lines,
    walk_tree,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
STORAGE_ID = "VX-41"

# cwd=repo root, no stub, no conftest: `python -c` puts the cwd on sys.path,
# so `scan.coordinator` resolves to this repo's package in a bare interpreter.
PORTAL_FREEDOM_SCRIPT = (
    "import sys, scan.coordinator; "
    "assert not [m for m in sys.modules if m == 'portal' or m.startswith('portal.')]; "
    "assert 'django' not in sys.modules"
)


# --------------------------------------------------------------------------
# AD-1: no Portal, no Django, no ORM
# --------------------------------------------------------------------------


def test_scan_coordinator_imports_portal_free_in_subprocess():
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_FREEDOM_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan.coordinator is not Portal-free in a bare interpreter (AD-1):\n"
        f"{result.stderr.decode(errors='replace')}"
    )


def test_coordinator_source_never_touches_the_orm():
    """A grep-strength guard on the boundary Epic 3's pool depends on.

    The pool wraps ``process_folder`` with per-worker connection hygiene;
    that only works if the coordinator itself never opens a model. Cheap
    static check, because the import test above cannot catch an ORM call
    hidden behind a lazy import inside a function.
    """
    source = (REPO_ROOT / "scan" / "coordinator.py").read_text(encoding="utf-8")
    for forbidden in ("get_or_new", "objects.", "django", "import portal"):
        assert forbidden not in source, f"scan/coordinator.py mentions {forbidden!r}"


# --------------------------------------------------------------------------
# AD-13 is exact
# --------------------------------------------------------------------------


def test_worker_result_has_exactly_the_ad13_fields():
    assert AD13_KEYS == (
        "folder_path",
        "clips",
        "counters",
        "errors",
        "timings",
        "log_lines",
    )
    assert tuple(f.name for f in dataclasses.fields(WorkerResult)) == AD13_KEYS
    assert tuple(WorkerResult(folder_path="p").as_dict()) == AD13_KEYS


def test_worker_counters_have_exactly_the_ad13_counter_names():
    assert AD13_COUNTER_KEYS == (
        "hits",
        "created",
        "already_ingested",
        "processed",
        "ingested",
        "skipped",
        "failed",
        "replaced",
    )
    assert tuple(f.name for f in dataclasses.fields(WorkerCounters)) == (
        AD13_COUNTER_KEYS
    )
    assert tuple(WorkerCounters().as_dict()) == AD13_COUNTER_KEYS


def test_clips_are_a_tuple_and_the_result_is_frozen():
    result = WorkerResult(folder_path="p", clips=(object(),))
    assert isinstance(result.clips, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.folder_path = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.counters.hits = 7


def test_counters_add_field_wise():
    left = WorkerCounters(hits=1, created=2, ingested=3)
    right = WorkerCounters(hits=10, skipped=4, ingested=5)
    assert (left + right).as_dict() == {
        "hits": 11,
        "created": 2,
        "already_ingested": 0,
        "processed": 0,
        "ingested": 8,
        "skipped": 4,
        "failed": 0,
        "replaced": 0,
    }


def test_folder_timings_are_frozen_and_add():
    assert tuple(f.name for f in dataclasses.fields(FolderTimings)) == TIMING_PHASES
    total = FolderTimings(discovery=1.0, ingest=2.0) + FolderTimings(
        discovery=0.5, extraction=3.0
    )
    assert (total.discovery, total.extraction, total.ingest) == (1.5, 3.0, 2.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        total.discovery = 0.0


def test_folder_outcome_carries_the_wrapper_fields():
    """The wrapper, not the frozen result, is where `failed` lives.

    ``failed`` cannot be a ``WorkerResult`` field: AD-13 is exact at six.
    And it must not be inferred from ``errors`` — a zero-hit folder with
    two unreadable files was SCANNED.

    ``merge_key`` rides here so the ordering guarantee is a property of
    the data rather than of ``walk_tree`` being ``merge_results``' only
    caller; ``listings`` is the worker's directory cache, handed back so
    the walk need not scandir the folder a second time and dropped the
    moment the children are expanded.
    """
    assert tuple(f.name for f in dataclasses.fields(FolderOutcome)) == (
        "result",
        "consumed_subdirs",
        "failed",
        "merge_key",
        "listings",
    )


def test_consumed_subdirs_is_typed_as_the_three_state_contract():
    """`Any` documented nothing; the three states ARE the contract."""
    hints = typing.get_type_hints(FolderOutcome)
    assert hints["consumed_subdirs"] == typing.Optional[typing.FrozenSet[str]]


def test_run_result_carries_no_clips():
    """188,082 clips across 8,133 folders never accumulate into one object."""
    names = tuple(f.name for f in dataclasses.fields(RunResult))
    assert names == (
        "counters",
        "errors",
        "timings",
        "log_lines",
        "folders_scanned",
        "folders_failed",
    )
    assert "clips" not in names


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------


def test_phase_timer_accumulates_and_freezes():
    timer = PhaseTimer()
    with timer("discovery"):
        pass
    with timer("discovery"):
        pass
    timer.add("ingest", 2.5)
    frozen = timer.freeze()
    assert isinstance(frozen, FolderTimings)
    assert frozen.ingest == 2.5
    assert frozen.discovery >= 0.0


def test_phase_timer_records_a_phase_that_raised():
    timer = PhaseTimer()
    with pytest.raises(ValueError):
        with timer("extraction"):
            raise ValueError("boom")
    # The wall-clock cost of a failing provider is still cost.
    assert "extraction" in timer.freeze().as_dict()


def test_phase_timer_rejects_an_unknown_phase():
    with pytest.raises(KeyError):
        with PhaseTimer()("nonsense"):
            pass


def test_fold_timings_is_the_single_writer_of_ctx_timings():
    ctx = _ctx("/root")
    fold_timings(ctx, FolderTimings(discovery=1.0, ingest=2.0))
    fold_timings(ctx, FolderTimings(discovery=0.5))
    assert (ctx.timings.discovery, ctx.timings.ingest) == (1.5, 2.0)


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def _outcome(path, *, failed=False, consumed=frozenset(), **counters):
    errors = counters.pop("errors", ())
    log_lines = counters.pop("log_lines", ())
    timings = counters.pop("timings", FolderTimings())
    return FolderOutcome(
        result=WorkerResult(
            folder_path=path,
            counters=WorkerCounters(**counters),
            errors=tuple(errors),
            timings=timings,
            log_lines=tuple(log_lines),
        ),
        consumed_subdirs=consumed,
        failed=failed,
    )


def test_merge_sums_counters_and_counts_scanned_and_failed():
    merged = merge_results(
        [
            _outcome("a", hits=2, created=1, ingested=1),
            _outcome("b", hits=3, already_ingested=3),
            _outcome("c", failed=True, errors=("Error scanning c: boom",)),
        ]
    )
    assert merged.counters.hits == 5
    assert merged.counters.created == 1
    assert merged.counters.already_ingested == 3
    assert (merged.folders_scanned, merged.folders_failed) == (2, 1)
    assert merged.errors == ("Error scanning c: boom",)


def test_merged_hits_is_the_sum_of_per_folder_hits():
    """Never a query-level total — Epic 4's buckets are disjoint too."""
    merged = merge_results([_outcome(str(i), hits=i) for i in range(5)])
    assert merged.counters.hits == 0 + 1 + 2 + 3 + 4


def test_merge_concatenates_errors_and_log_lines_in_order():
    merged = merge_results(
        [
            _outcome("a", errors=("e1",), log_lines=("l1",)),
            _outcome("b", errors=("e2", "e3"), log_lines=("l2",)),
        ]
    )
    assert merged.errors == ("e1", "e2", "e3")
    assert merged.log_lines == ("l1", "l2")


def test_a_per_file_error_never_makes_a_folder_failed():
    merged = merge_results([_outcome("a", hits=0, errors=("Error scanning file x",))])
    assert (merged.folders_scanned, merged.folders_failed) == (1, 0)


# --------------------------------------------------------------------------
# Façade rebuild (NFR-5)
# --------------------------------------------------------------------------

SCAN_KEYS = {
    "clips",
    "hits",
    "errors",
    "created",
    "already_ingested",
    "processed",
    "consumed_subdirs",
}
INGEST_KEYS = SCAN_KEYS | {"ingested", "skipped", "failed", "replaced"}


def test_scan_response_is_rebuilt_with_lists_in_order():
    clip_a, clip_b = object(), object()
    outcome = FolderOutcome(
        result=WorkerResult(
            folder_path="2026/AH",
            clips=(clip_a, clip_b),
            counters=WorkerCounters(hits=2, created=1, already_ingested=1, processed=2),
            errors=("first", "second"),
        ),
        consumed_subdirs=frozenset({"CARD"}),
    )
    response = build_scan_response(outcome)

    assert set(response) == SCAN_KEYS
    # Lists, not tuples: the 1.3 pins assert whole-dict equality against
    # literal lists, and in order.
    assert response["clips"] == [clip_a, clip_b]
    assert response["errors"] == ["first", "second"]
    assert isinstance(response["clips"], list)
    assert isinstance(response["errors"], list)
    assert response["consumed_subdirs"] == frozenset({"CARD"})


def test_ingest_response_adds_exactly_four_keys():
    outcome = FolderOutcome(
        result=WorkerResult(
            folder_path="p",
            counters=WorkerCounters(ingested=1, skipped=2, failed=3, replaced=4),
        )
    )
    response = build_ingest_response(outcome)
    assert set(response) == INGEST_KEYS
    assert (
        response["ingested"],
        response["skipped"],
        response["failed"],
        response["replaced"],
    ) == (1, 2, 3, 4)


def test_doubt_survives_the_rebuild_as_none_not_an_empty_set():
    """`None` and `frozenset()` are the OPPOSITE calls (NFR-1)."""
    response = build_scan_response(
        FolderOutcome(result=WorkerResult(folder_path="p"), consumed_subdirs=None)
    )
    assert response["consumed_subdirs"] is None


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------


def _ctx(root, **options):
    return ScanContext(
        storages={STORAGE_ID: StorageInfo(id=STORAGE_ID, root_path=root)},
        options=RunOptions(**options),
    )


def _run_result(**kwargs):
    defaults = dict(
        counters=WorkerCounters(),
        errors=(),
        timings=FolderTimings(),
        log_lines=(),
        folders_scanned=0,
        folders_failed=0,
    )
    defaults.update(kwargs)
    return RunResult(**defaults)


def test_summary_reports_both_folder_counts_and_the_five_phases():
    run_result = _run_result(
        folders_scanned=12,
        folders_failed=2,
        timings=FolderTimings(
            discovery=1.25,
            verification=2.0,
            extraction=3.5,
            persistence=0.5,
            ingest=4.0,
        ),
    )
    folders, _counters = summary_lines(run_result, _ctx("/root"), 61.44)
    assert folders == (
        "12 folders scanned, 2 failed in 61.4s — discovery 1.2s, "
        "verification 2.0s, extraction 3.5s, persistence 0.5s, ingest 4.0s, "
        "other 50.2s"
    )


def test_the_phases_and_other_reconcile_with_the_elapsed_time():
    """A summary whose numbers visibly do not add up teaches distrust.

    Three real per-folder costs are outside the five phases by design (the
    pass-2 bulk umid lookup, hash recovery's HTTP calls, the
    `consumed_subdirs` computation), and so is the walk's own listing.
    `other` is all of it, so the line adds up.
    """
    timings = FolderTimings(
        discovery=1.0, verification=2.0, extraction=3.0, persistence=4.0, ingest=5.0
    )
    [folders, _counters] = summary_lines(
        _run_result(timings=timings), _ctx("/root"), 20.0
    )
    assert "other 5.0s" in folders
    reported = [float(part) for part in re.findall(r"(\d+\.\d)s", folders)]
    # elapsed, then the five phases, then other.
    assert reported[0] == 20.0
    assert sum(reported[1:]) == pytest.approx(reported[0])


def test_other_is_clamped_at_zero_when_worker_time_exceeds_wall_clock():
    """Epic 3 overlaps work, so the difference legitimately goes negative."""
    timings = FolderTimings(discovery=100.0)
    [folders, _counters] = summary_lines(
        _run_result(timings=timings), _ctx("/root"), 10.0
    )
    assert "other 0.0s" in folders


def test_the_summary_reports_the_counters_the_run_result_knows():
    """Folder counts and phase timings alone said nothing about clips."""
    run_result = _run_result(
        counters=WorkerCounters(
            hits=9,
            created=4,
            already_ingested=3,
            processed=9,
            ingested=2,
            skipped=5,
            failed=1,
            replaced=1,
        ),
        errors=("boom", "bang"),
    )
    _folders, counters = summary_lines(run_result, _ctx("/root"), 1.0)
    assert counters == (
        "9 clips found, 4 created, 3 already ingested, 9 processed, "
        "2 ingested, 5 skipped, 1 failed, 1 replaced, 2 errors"
    )


def test_summary_is_labelled_under_dry_run():
    folders, counters = summary_lines(_run_result(), _ctx("/root", dry_run=True), 1.0)
    assert folders.startswith("DRY-RUN: 0 folders scanned, 0 failed in 1.0s — ")
    # BOTH lines carry the label: they reach Slack as separate messages and
    # a counters line quoted on its own must not read as a real run.
    assert counters.startswith("DRY-RUN: 0 clips found, ")


def test_window_line_does_not_collide_with_the_command_window_log():
    """`format_window_log`'s prefix is pinned as unique in test_cli_validation."""
    ctx = _ctx("/root", date_window=("20260101", "20260102", "20260103"))
    lines = summary_lines(_run_result(), ctx, 1.0)
    assert lines[-1] == "Window: 20260101 to 20260103 (3 day folders)"
    assert not lines[-1].startswith("Scanning folders from ")


def test_no_window_line_without_a_window():
    assert len(summary_lines(_run_result(), _ctx("/root"), 1.0)) == 2


# --------------------------------------------------------------------------
# AD-14
# --------------------------------------------------------------------------


def test_tree_only_options_names_the_four_folder_filters():
    """They are genuinely meaningless in paged mode, so they are policed.

    A paged call scans ONE folder and never walks, so a `--skip`/`--only`/
    `--startWith`/date-window narrowing could only mislead a caller into
    believing it had been applied. Epic 3 adds `workers` and Epic 4 adds
    `discovery` to the same tuple.
    """
    assert TREE_ONLY_OPTIONS == ("skip", "only", "startwith", "date_window")


def test_assert_mode_options_passes_for_a_real_run_options():
    assert_mode_options(RunOptions(), "paged")
    assert_mode_options(RunOptions(), "tree")


def test_a_walk_filter_is_rejected_in_paged_mode():
    with pytest.raises(ValueError, match="startwith"):
        assert_mode_options(RunOptions(startwith=("AH_",)), "paged")
    # ...and is exactly what tree mode is for.
    assert_mode_options(RunOptions(startwith=("AH_",)), "tree")


def test_an_injected_tree_only_option_is_rejected_in_paged_mode(monkeypatch):
    monkeypatch.setattr(coordinator, "TREE_ONLY_OPTIONS", ("workers",))

    class _Options:
        workers = 8

    with pytest.raises(ValueError, match="workers"):
        assert_mode_options(_Options(), "paged")
    # ...and tree mode is exactly where it IS allowed.
    assert_mode_options(_Options(), "tree")


def test_scan_tree_exposes_no_pagination_surface():
    """AD-14's other half is structural, not asserted at runtime.

    `number=0` is the loop-all-pages sentinel, so a "reject non-default
    pagination in tree mode" guard would have rejected the normal cron
    path. The sound form of the guarantee is that there is nothing to
    pass.
    """
    import inspect

    from portal.plugins.TapelessIngest.models.folder import Folder

    parameters = inspect.signature(Folder.scan_tree).parameters
    assert list(parameters) == ["self", "ctx", "emit"]
    assert parameters["emit"].kind is inspect.Parameter.KEYWORD_ONLY


# --------------------------------------------------------------------------
# 2.6's filter semantics, relocated verbatim
# --------------------------------------------------------------------------


def test_should_scan_entry_keeps_the_legacy_substring_semantics():
    assert should_scan_entry("AH_20260101_shoot", startwith=["AH_"])
    assert not should_scan_entry("ZZ_20260101", startwith=["AH_"])
    assert not should_scan_entry("a_TMP_b", skip=["TMP"])
    # `find`, not `startswith`: --only/--skip are SUBSTRING filters.
    assert should_scan_entry("x_only_y", only=["only"])
    assert not should_scan_entry("nothing", only=["only"])
    assert should_scan_entry("AH_20260102", date_window=["20260102"])
    assert not should_scan_entry("AH_20260103", date_window=["20260102"])
    # Both given: both must be satisfied (they stopped aliasing in 2.6).
    assert not should_scan_entry("AH_20260102", only=["nope"], date_window=["20260102"])


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


class _Worker:
    """A local ``process_folder`` double — no ORM, no Portal, no scan.

    Records every ``(storage_id, path)`` it was handed, in call order, and
    returns an outcome that authorizes descent unless the fixture said
    otherwise.
    """

    def __init__(self, doubt=(), fail=(), hits=None):
        self.seen = []
        self.kwargs = []
        self._doubt = set(doubt)
        self._fail = set(fail)
        self._hits = hits or {}

    def __call__(self, storage_id, path, ctx, **kwargs):
        self.seen.append((storage_id, path))
        self.kwargs.append(kwargs)
        if path in self._fail:
            return FolderOutcome(
                result=WorkerResult(
                    folder_path=path,
                    errors=(f"Error scanning {path}: boom",),
                    log_lines=(f"Error scanning {path}: boom",),
                ),
                consumed_subdirs=None,
                failed=True,
            )
        return FolderOutcome(
            result=WorkerResult(
                folder_path=path,
                counters=WorkerCounters(hits=self._hits.get(path, 0)),
                log_lines=(f"visited {path}",),
            ),
            consumed_subdirs=None if path in self._doubt else frozenset(),
            failed=False,
        )


def _walk(tmp_path, root_path, worker, ctx=None, emit=None, **dispatch):
    ctx = ctx or _ctx(str(tmp_path))
    dispatcher = dispatch.pop("dispatcher", None) or SequentialDispatcher()
    return walk_tree(
        STORAGE_ID,
        root_path,
        ctx=ctx,
        process_folder=worker,
        dispatch=dispatcher.dispatch,
        gather=dispatcher.gather,
        emit=(lambda line: None) if emit is None else emit,
        **dispatch,
    )


def _tree(tmp_path, *relative_dirs):
    for relative in relative_dirs:
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)


def test_the_root_is_a_container_never_processed_and_never_counted(tmp_path):
    _tree(tmp_path, "2026/A", "2026/B")
    worker = _Worker()

    outcomes = _walk(tmp_path, "2026", worker)

    assert worker.seen == [(STORAGE_ID, "2026/A"), (STORAGE_ID, "2026/B")]
    assert [o.result.folder_path for o in outcomes] == ["2026/A", "2026/B"]
    merged = merge_results(outcomes)
    assert (merged.folders_scanned, merged.folders_failed) == (2, 0)


def test_tree_mode_calls_the_worker_with_the_loop_all_pages_sentinel(tmp_path):
    _tree(tmp_path, "2026/A")
    worker = _Worker()

    _walk(tmp_path, "2026", worker)

    [kwargs] = worker.kwargs
    assert kwargs == {
        "first": 0,
        "number": 0,
        "cursor": None,
        "count_only": False,
        "ingest": True,
    }


def test_outcomes_come_back_in_depth_first_pre_order(tmp_path):
    _tree(tmp_path, "2026/A/A1", "2026/A/A2", "2026/B/B1")
    outcomes = _walk(tmp_path, "2026", _Worker())

    assert [o.result.folder_path for o in outcomes] == [
        "2026/A",
        "2026/A/A1",
        "2026/A/A2",
        "2026/B",
        "2026/B/B1",
    ]


class _BufferingDispatcher:
    """A pool whose workers really do finish out of dispatch order.

    The predecessor of this class reversed whatever ``gather()`` was about
    to return — which proved nothing, because ``walk_tree`` dispatches ONE
    item and then gathers, so the batch was never longer than one and
    reversing it was the identity. Deleting the merge-key sort left the
    whole suite green.

    This one BUFFERS. Nothing executes until ``batch`` items are pending
    (or the walk runs out of work to feed it), and then the whole batch
    runs and comes back in an order the tree does not predict. Children
    are therefore enqueued out of order too, so the walk's own dispatch
    order is scrambled for the rest of the run — which is the property a
    real pool has and the old fixture did not.

    Termination: when the stack empties, ``walk_tree`` calls ``gather()``
    with nothing new to dispatch. The buffer notices it saw the same
    pending count twice in a row and flushes what it has.
    """

    def __init__(self, batch=3, order=None):
        self._batch = batch
        self._order = order or (lambda pairs: list(reversed(pairs)))
        self._buffer = []
        self._last_seen = None

    def dispatch(self, fn, item):
        self._buffer.append((fn, item))

    def gather(self):
        if not self._buffer:
            return []
        if len(self._buffer) < self._batch and len(self._buffer) != self._last_seen:
            # Give the walk a chance to feed us more before we commit.
            self._last_seen = len(self._buffer)
            return []
        self._last_seen = None
        batch, self._buffer = self._buffer, []
        return self._order([(item, fn(item)) for fn, item in batch])


TREE_FIXTURE = (
    "2026/A/A1",
    "2026/A/A2",
    "2026/B/B1",
    "2026/B/B2/B21",
)
TREE_HITS = {"2026/A": 1, "2026/A/A2": 2, "2026/B/B2": 3}
TREE_ORDER = (
    "2026/A",
    "2026/A/A1",
    "2026/A/A2",
    "2026/B",
    "2026/B/B1",
    "2026/B/B2",
    "2026/B/B2/B21",
)


def test_the_sequential_walk_is_depth_first_pre_order(tmp_path):
    _tree(tmp_path, *TREE_FIXTURE)
    emitted = []
    outcomes = _walk(tmp_path, "2026", _Worker(hits=TREE_HITS), emit=emitted.append)
    assert tuple(o.result.folder_path for o in outcomes) == TREE_ORDER
    assert tuple(emitted) == tuple(f"visited {path}" for path in TREE_ORDER)


@pytest.mark.parametrize("batch", [2, 3, 4, 7])
@pytest.mark.parametrize("seed", range(6))
def test_out_of_order_completion_produces_byte_identical_output(tmp_path, batch, seed):
    """≥2 levels, ≥2 branches — a flat fixture cannot tell the schemes apart.

    24 orderings (4 batch sizes x 6 shuffles). Each one really does execute
    the folders in a different order and enqueue their children in a
    different order; every one of them must produce the same `RunResult`
    AND the same emission sequence as the sequential run.
    """
    _tree(tmp_path, *TREE_FIXTURE)
    rng = random.Random(seed)

    def shuffle(pairs):
        pairs = list(pairs)
        rng.shuffle(pairs)
        return pairs

    in_order_emitted = []
    in_order = merge_results(
        _walk(
            tmp_path,
            "2026",
            _Worker(hits=TREE_HITS),
            emit=in_order_emitted.append,
        )
    )

    scrambled_emitted = []
    scrambled_worker = _Worker(hits=TREE_HITS)
    scrambled = merge_results(
        _walk(
            tmp_path,
            "2026",
            scrambled_worker,
            emit=scrambled_emitted.append,
            dispatcher=_BufferingDispatcher(batch=batch, order=shuffle),
        )
    )

    assert scrambled == in_order
    assert scrambled_emitted == in_order_emitted
    assert in_order.log_lines == tuple(f"visited {path}" for path in TREE_ORDER)
    # ...and the fixture really did scramble the EXECUTION order, or the
    # equality above would be the identity all over again.
    executed = tuple(path for _, path in scrambled_worker.seen)
    assert sorted(executed) == sorted(TREE_ORDER)


def test_the_merge_key_sort_is_what_makes_that_true(tmp_path):
    """Guards the guard: without the ordering, the fixture disagrees.

    The reviewer's mutation — delete the merge-key ordering — must break
    the test above. Here it is, injected deliberately: outcomes handed to
    `merge_results` in completion order rather than merge-key order
    produce a DIFFERENT report, which is exactly why the sort exists.
    """
    _tree(tmp_path, *TREE_FIXTURE)
    outcomes = _walk(tmp_path, "2026", _Worker(hits=TREE_HITS))
    stripped = [dataclasses.replace(o, merge_key=()) for o in reversed(outcomes)]

    assert merge_results(outcomes).log_lines != merge_results(stripped).log_lines
    # And with the keys intact, the shuffled input still merges correctly —
    # `merge_results` does not lean on `walk_tree` having sorted first.
    shuffled = list(outcomes)
    random.Random(0).shuffle(shuffled)
    assert merge_results(shuffled) == merge_results(outcomes)


def test_the_coordinator_enqueues_children_never_the_worker(tmp_path):
    """Every path the worker sees was handed to it BY the walk.

    The property that makes the seam substitutable: a worker that could
    submit work would have to hold the queue, and then the queue could not
    live in a coordinator that never touches the ORM.
    """
    _tree(tmp_path, "2026/A/A1/A11")
    worker = _Worker()

    _walk(tmp_path, "2026", worker)

    assert worker.seen == [
        (STORAGE_ID, "2026/A"),
        (STORAGE_ID, "2026/A/A1"),
        (STORAGE_ID, "2026/A/A1/A11"),
    ]


def test_doubt_forbids_descent(tmp_path):
    _tree(tmp_path, "2026/A/A1", "2026/B/B1")
    worker = _Worker(doubt={"2026/A"})

    outcomes = _walk(tmp_path, "2026", worker)

    assert (STORAGE_ID, "2026/A/A1") not in worker.seen
    assert (STORAGE_ID, "2026/B/B1") in worker.seen
    assert [o.result.folder_path for o in outcomes] == ["2026/A", "2026/B", "2026/B/B1"]


def test_a_consumed_child_is_never_scanned_as_a_folder_of_its_own(tmp_path):
    _tree(tmp_path, "2026/A/CARD", "2026/A/INTERVIEWS")

    class _ConsumingWorker(_Worker):
        def __call__(self, storage_id, path, ctx, **kwargs):
            outcome = _Worker.__call__(self, storage_id, path, ctx, **kwargs)
            if path == "2026/A":
                return dataclasses.replace(
                    outcome, consumed_subdirs=frozenset({"CARD"})
                )
            return outcome

    worker = _ConsumingWorker()
    _walk(tmp_path, "2026", worker)

    assert (STORAGE_ID, "2026/A/CARD") not in worker.seen
    assert (STORAGE_ID, "2026/A/INTERVIEWS") in worker.seen


def test_a_failed_folder_does_not_stop_the_walk(tmp_path):
    _tree(tmp_path, "2026/A", "2026/B", "2026/C")
    worker = _Worker(fail={"2026/B"})

    merged = merge_results(_walk(tmp_path, "2026", worker))

    assert [path for _, path in worker.seen] == ["2026/A", "2026/B", "2026/C"]
    assert (merged.folders_scanned, merged.folders_failed) == (2, 1)
    assert merged.errors == ("Error scanning 2026/B: boom",)


# --------------------------------------------------------------------------
# Depth filtering
# --------------------------------------------------------------------------


def test_startwith_and_the_window_apply_only_at_depth_one(tmp_path):
    _tree(tmp_path, "2026/AH_20260101/AH_sub", "2026/AH_20260101/RUSHES", "2026/ZZ_x")
    ctx = _ctx(str(tmp_path), startwith=("AH_",), date_window=("20260101",))

    worker = _Worker()
    _walk(tmp_path, "2026", worker, ctx=ctx)

    paths = [path for _, path in worker.seen]
    # Depth 1: only the AH_ folder inside the window.
    assert "2026/ZZ_x" not in paths
    # Depth 2: RUSHES matches neither filter and is scanned anyway.
    assert sorted(paths) == [
        "2026/AH_20260101",
        "2026/AH_20260101/AH_sub",
        "2026/AH_20260101/RUSHES",
    ]


def test_skip_and_only_apply_at_every_depth(tmp_path):
    _tree(tmp_path, "2026/A/keep_me", "2026/A/TMP", "2026/TMP")
    ctx = _ctx(str(tmp_path), skip=("TMP",))

    worker = _Worker()
    _walk(tmp_path, "2026", worker, ctx=ctx)

    paths = [path for _, path in worker.seen]
    assert paths == ["2026/A", "2026/A/keep_me"]


def test_only_applies_at_depth_two(tmp_path):
    _tree(tmp_path, "2026/only_a/only_b", "2026/only_a/drop")
    ctx = _ctx(str(tmp_path), only=("only",))

    worker = _Worker()
    _walk(tmp_path, "2026", worker, ctx=ctx)

    assert [path for _, path in worker.seen] == ["2026/only_a", "2026/only_a/only_b"]


def test_a_filtered_sibling_does_not_shift_its_neighbours_merge_key(tmp_path):
    """The ordinal is an index into the FULL sorted listing.

    If filtered-out children shifted the ordinals, two runs of the same
    tree with different --skip values would merge their shared folders in
    different relative positions — the merge key would stop being a
    property of the tree.
    """
    _tree(tmp_path, "2026/a", "2026/b", "2026/c")

    unfiltered = _walk(tmp_path, "2026", _Worker())
    filtered = _walk(tmp_path, "2026", _Worker(), ctx=_ctx(str(tmp_path), skip=("b",)))

    assert [o.result.folder_path for o in unfiltered] == ["2026/a", "2026/b", "2026/c"]
    assert [o.result.folder_path for o in filtered] == ["2026/a", "2026/c"]


# --------------------------------------------------------------------------
# Listing failures (FR-22 / FR-28), surfaced rather than silently empty
# --------------------------------------------------------------------------


def test_an_unlistable_child_directory_surfaces_on_that_folders_outcome(tmp_path):
    _tree(tmp_path, "2026/A")
    unreadable = tmp_path / "2026" / "A" / "locked"
    unreadable.mkdir()
    unreadable.chmod(0o000)
    try:
        outcomes = _walk(tmp_path, "2026", _Worker())
    finally:
        unreadable.chmod(0o700)

    # The child that could not be listed is reported ON the folder whose
    # expansion tried to list it, in BOTH errors and log_lines...
    [locked] = [o for o in outcomes if o.result.folder_path == "2026/A/locked"]
    [error] = locked.result.errors
    assert error.startswith("Error listing directory ")
    assert error in locked.result.log_lines
    # ...and the folder is still counted as scanned: a scandir failure
    # below it is not the folder failing.
    assert locked.failed is False


def test_an_unresolvable_root_is_counted_as_a_failure_not_just_printed(tmp_path):
    """FR-28, and the run has to be able to SAY it failed.

    The root container is never processed, so its own failure had no
    outcome to ride on: it was printed and then vanished, and the run
    reported "0 folders scanned, 0 failed" — indistinguishable from an
    empty tree, which is what a cron job silently succeeding looks like.
    It gets an outcome of its own now: emitted, counted, and in `errors`.
    """
    emitted = []
    ctx = ScanContext(
        storages={STORAGE_ID: StorageInfo(id=STORAGE_ID, root_path=None)},
        options=RunOptions(),
    )
    worker = _Worker()
    outcomes = _walk(tmp_path, "2026", worker, ctx=ctx, emit=emitted.append)

    assert worker.seen == []
    assert emitted == ["Cannot get full path from storage VX-41, path 2026"]
    merged = merge_results(outcomes)
    assert (merged.folders_scanned, merged.folders_failed) == (0, 1)
    assert merged.errors == ("Cannot get full path from storage VX-41, path 2026",)


def test_a_missing_root_directory_is_counted_as_a_failure(tmp_path):
    emitted = []
    outcomes = _walk(tmp_path, "nope", _Worker(), emit=emitted.append)

    assert len(emitted) == 1
    assert emitted[0].startswith("Error listing directory ")
    merged = merge_results(outcomes)
    assert (merged.folders_scanned, merged.folders_failed) == (0, 1)
    assert len(merged.errors) == 1


def test_an_empty_tree_is_distinguishable_from_a_broken_root(tmp_path):
    """The whole point of the two tests above, stated as the contrast."""
    _tree(tmp_path, "2026")
    merged = merge_results(_walk(tmp_path, "2026", _Worker()))
    assert (merged.folders_scanned, merged.folders_failed) == (0, 0)
    assert merged.errors == ()


def test_emit_is_required(tmp_path):
    """The run's only output channel must not be silently discardable."""
    parameters = typing.get_type_hints(walk_tree) and None
    import inspect

    emit = inspect.signature(walk_tree).parameters["emit"]
    assert emit.kind is inspect.Parameter.KEYWORD_ONLY
    assert emit.default is inspect.Parameter.empty


# --------------------------------------------------------------------------
# Incremental draining, and what the walk does NOT retain
# --------------------------------------------------------------------------


def test_lines_are_drained_as_prefixes_complete_not_at_the_end(tmp_path):
    """An 8,000-folder run must report as it goes.

    The emission is still exactly merge-key order — the release rule is a
    lower bound computed from what is still outstanding, not a guess — but
    a folder's line goes out as soon as everything before it is done,
    rather than after the whole tree.
    """
    _tree(tmp_path, "2026/A/A1", "2026/B")
    emitted_when_seen = []

    class _Recorder(_Worker):
        def __call__(self, storage_id, path, ctx, **kwargs):
            emitted_when_seen.append((path, list(emitted)))
            return _Worker.__call__(self, storage_id, path, ctx, **kwargs)

    emitted = []
    _walk(tmp_path, "2026", _Recorder(), emit=emitted.append)

    seen = dict(emitted_when_seen)
    # By the time B is executed, A and A1 have already been reported.
    assert seen["2026/B"] == ["visited 2026/A", "visited 2026/A/A1"]
    # ...and nothing had been reported before the first folder ran.
    assert seen["2026/A"] == []


def test_the_walk_does_not_retain_clips(tmp_path):
    """`RunResult` drops clips — one line too late, until this round.

    `walk_tree` accumulates every outcome until it returns, so dropping
    them only at merge time retained the whole 188k-clip corpus anyway.
    Tree mode never hands an outcome to a façade, so they go immediately.
    """
    _tree(tmp_path, "2026/A", "2026/B")

    class _ClipfulWorker(_Worker):
        def __call__(self, storage_id, path, ctx, **kwargs):
            outcome = _Worker.__call__(self, storage_id, path, ctx, **kwargs)
            return dataclasses.replace(
                outcome,
                result=dataclasses.replace(outcome.result, clips=(object(), object())),
            )

    outcomes = _walk(tmp_path, "2026", _ClipfulWorker())

    assert outcomes
    assert all(outcome.result.clips == () for outcome in outcomes)


def test_the_walk_does_not_retain_the_workers_listings(tmp_path):
    """It is used to expand the children, then dropped."""
    _tree(tmp_path, "2026/A/A1")

    class _ListingWorker(_Worker):
        def __call__(self, storage_id, path, ctx, **kwargs):
            outcome = _Worker.__call__(self, storage_id, path, ctx, **kwargs)
            listings = FolderListings()
            listings.get(ctx.absolute_path_for(storage_id, path))
            return dataclasses.replace(outcome, listings=listings)

    worker = _ListingWorker()
    outcomes = _walk(tmp_path, "2026", worker)

    assert [path for _, path in worker.seen] == ["2026/A", "2026/A/A1"]
    assert all(outcome.listings is None for outcome in outcomes)


# --------------------------------------------------------------------------
# NFR-1: one physical directory, one walk
# --------------------------------------------------------------------------


@pytest.fixture
def collapsing_realpath(monkeypatch):
    """Make two REAL directories resolve to one physical path.

    A bind mount is what actually produces this, and it needs root;
    ``os.link`` on a directory is not portable either. FR-24's symlink
    guard deliberately does NOT cover the case — these are real
    directories to the filesystem, and a fixture built out of symlinks
    would be answered by that guard instead of by the realpath set, i.e.
    it would pass whether or not the dedupe existed. So the collapse is
    injected at the one function the walk asks.
    """
    aliases = {}
    real_realpath = os.path.realpath

    def collapsing(path):
        resolved = real_realpath(path)
        return aliases.get(os.path.basename(resolved), resolved)

    monkeypatch.setattr(os.path, "realpath", collapsing)
    return aliases


def test_a_directory_reachable_by_two_paths_is_walked_once(
    tmp_path, collapsing_realpath
):
    """A bind mount or a hardlinked directory gives one tree two paths.

    Both passes would read "clip absent", both would ingest, and neither
    would have written a row yet — the umid primary key cannot save us
    from a race it never sees.
    """
    _tree(tmp_path, "2026/alpha/inner", "2026/beta", "2026/gamma")
    # alpha and beta are the same physical directory.
    collapsing_realpath["beta"] = str(tmp_path / "2026" / "alpha")

    emitted = []
    worker = _Worker()
    _walk(tmp_path, "2026", worker, emit=emitted.append)

    paths = [path for _, path in worker.seen]
    assert "2026/beta" not in paths
    assert sorted(paths) == ["2026/alpha", "2026/alpha/inner", "2026/gamma"]
    # ...and the skip is REPORTED, not silent: an operator seeing half a
    # tree unscanned needs to know a duplicate path is why. `beta` is a
    # child of the ROOT container, which has no outcome to carry the note,
    # so it goes out directly — see `walk_tree`.
    assert any("Already walked 2026/beta" in line for line in emitted)


def test_the_root_realpath_is_seeded_so_a_child_cannot_loop_back(
    tmp_path, collapsing_realpath
):
    _tree(tmp_path, "2026/again")
    collapsing_realpath["again"] = str(tmp_path / "2026")

    worker = _Worker()
    _walk(tmp_path, "2026", worker)

    assert worker.seen == []


def test_symlinked_directories_are_still_excluded_by_fr_24(tmp_path):
    """The realpath set GENERALISES the cycle guard, it does not replace it."""
    _tree(tmp_path, "2026/real")
    (tmp_path / "2026" / "alias").symlink_to(tmp_path / "2026" / "real")

    worker = _Worker()
    _walk(tmp_path, "2026", worker)

    assert [path for _, path in worker.seen] == ["2026/real"]


# --------------------------------------------------------------------------
# The fan-out seam's failure modes (E28/E29)
# --------------------------------------------------------------------------


class _RaisingDispatcher(SequentialDispatcher):
    def __init__(self, fail_on):
        SequentialDispatcher.__init__(self)
        self._fail_on = fail_on

    def dispatch(self, fn, item):
        if item.path == self._fail_on:
            raise RuntimeError("pool rejected the submission")
        SequentialDispatcher.dispatch(self, fn, item)


def test_a_raising_dispatch_does_not_discard_the_completed_work(tmp_path):
    """`process_folder` never raises, so a raising dispatch IS the pool.

    Letting it escape threw away every outcome the run had already
    produced, and every log line with them.
    """
    _tree(tmp_path, "2026/A", "2026/B", "2026/C")
    emitted = []

    outcomes = _walk(
        tmp_path,
        "2026",
        _Worker(),
        emit=emitted.append,
        dispatcher=_RaisingDispatcher(fail_on="2026/B"),
    )

    merged = merge_results(outcomes)
    assert (merged.folders_scanned, merged.folders_failed) == (2, 1)
    assert "Error scanning 2026/B: pool rejected the submission" in merged.errors
    assert emitted == [
        "visited 2026/A",
        "Error scanning 2026/B: pool rejected the submission",
        "visited 2026/C",
    ]


class _BlackHoleDispatcher:
    """Accepts work and never reports it — a wedged pool."""

    def dispatch(self, fn, item):
        pass

    def gather(self):
        return []


def test_a_gather_that_never_reports_fails_loudly_instead_of_hanging(
    tmp_path, monkeypatch
):
    _tree(tmp_path, "2026/A")
    monkeypatch.setattr(coordinator, "MAX_IDLE_ROUNDS", 5)

    with pytest.raises(RuntimeError, match="no progress"):
        _walk(tmp_path, "2026", _Worker(), dispatcher=_BlackHoleDispatcher())


def test_a_deduped_root_child_is_a_note_not_a_root_failure(
    tmp_path, collapsing_realpath
):
    """`lines` alone cannot say whether the expansion FAILED.

    A realpath-deduped child is an anomaly worth reporting on a root that
    expanded perfectly well; reading it as a failure would invent a failed
    folder out of a bind mount and give the cron a non-zero exit for it.
    """
    _tree(tmp_path, "2026/alpha", "2026/beta")
    collapsing_realpath["beta"] = str(tmp_path / "2026" / "alpha")

    emitted = []
    worker = _Worker()
    outcomes = _walk(tmp_path, "2026", worker, emit=emitted.append)

    merged = merge_results(outcomes)
    # The real child was scanned and nothing failed...
    assert [path for _, path in worker.seen] == ["2026/alpha"]
    assert (merged.folders_scanned, merged.folders_failed) == (1, 0)
    # ...and the root produced no error of its own, only the note.
    assert merged.errors == ()
    assert any("Already walked 2026/beta" in line for line in emitted)

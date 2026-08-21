"""The sequential coordinator: WorkerResult, the fan-out seam, the tree walk.

Stdlib-only by contract (AD-1), like ``scan.context``, ``scan.extraction``
and ``scan.verification``: this module must import in a bare interpreter
with no Portal stub installed, and it never touches the ORM. The walk
deals in ``(storage_id, path)`` pairs and derives absolute paths from
``ctx.absolute_path_for(...)``; the only thing that ever opens a
``Folder`` is the injected ``process_folder`` callable, which lives on the
model side of the boundary.

What lives here
---------------
* the AD-13 result types — ``WorkerCounters``, ``FolderTimings``,
  ``WorkerResult`` — all frozen, all picklable, none of them sharing
  mutable state with the run;
* ``FolderOutcome``, the wrapper the walk actually moves around: the
  frozen result plus the two things only the walk cares about (the
  descent authorization and whether the never-raising worker caught);
* ``RunResult``, the merged shape — a DIFFERENT type, carrying no clips
  (see ``spec-2-8-design-notes.md``: 188,082 clips across 8,133 folders
  would otherwise be retained for the whole run, and Epic 3 would
  multiply that per worker);
* ``walk_tree``, the explicit work queue: a LIFO stack of
  ``(storage_id, path, depth, merge_key)`` items handed to an injected
  ``dispatch``/``gather`` pair. **Workers never submit work** — when an
  outcome comes back, the coordinator enqueues that folder's eligible
  children.

Merge order is tree-derived, never timing-derived
-------------------------------------------------
Every folder carries a ``merge_key``: the tuple of sibling ordinals from
the root, each ordinal being that folder's index in its parent's
``sorted(listing.dirs)``. Sorting outcomes lexicographically by that key
reproduces today's depth-first pre-order and is identical for a
sequential run and a pooled one, because the key is a property of the
tree alone. An index assigned at enqueue time would not be: under
concurrency, enqueue order depends on which parents completed first —
exactly the completion-order dependence the key exists to eliminate.

The Epic 3 seam
---------------
``dispatch``/``gather`` become ``executor.submit``/``as_completed``;
``process_folder`` is wrapped with per-worker connection hygiene; the
merge key already guarantees byte-identical output under out-of-order
completion. No ``WorkerResult`` change is required for any of it.
"""

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from .verification import FolderListings

__all__ = [
    "AD13_COUNTER_KEYS",
    "AD13_KEYS",
    "TIMING_PHASES",
    "TREE_ONLY_OPTIONS",
    "FolderOutcome",
    "FolderTimings",
    "PhaseTimer",
    "RunResult",
    "SequentialDispatcher",
    "WorkItem",
    "WorkerCounters",
    "WorkerResult",
    "assert_mode_options",
    "build_ingest_response",
    "build_scan_response",
    "fold_timings",
    "merge_results",
    "should_scan_entry",
    "summary_lines",
    "walk_tree",
]

# AD-13, exact. These are the contract Epic 3 and Epic 4 consume unchanged;
# they are asserted against the dataclasses at import time (bottom of file).
AD13_KEYS = (
    "folder_path",
    "clips",
    "counters",
    "errors",
    "timings",
    "log_lines",
)
AD13_COUNTER_KEYS = (
    "hits",
    "created",
    "already_ingested",
    "processed",
    "ingested",
    "skipped",
    "failed",
    "replaced",
)

# The five phases every folder is timed in. `discovery` is Epic 4's
# replacement point (index discovery swaps what happens inside it).
TIMING_PHASES = (
    "discovery",
    "verification",
    "extraction",
    "persistence",
    "ingest",
)

# Options that only tree mode may carry. Ships EMPTY: Epic 3 adds
# `workers`, Epic 4 adds `discovery`. The constant exists now so the seam
# — and its test — ship with the story that freezes the contract.
TREE_ONLY_OPTIONS = ()


# ---------------------------------------------------------------------------
# AD-13: the frozen per-folder result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkerCounters:
    """The eight AD-13 counters, summable field-wise.

    Frozen rather than a dict: a typo'd counter name fails loudly at
    construction instead of silently creating a key nobody reads, and a
    result handed across a future process boundary cannot be mutated
    behind the coordinator's back.
    """

    hits: int = 0
    created: int = 0
    already_ingested: int = 0
    processed: int = 0
    ingested: int = 0
    skipped: int = 0
    failed: int = 0
    replaced: int = 0

    def __add__(self, other: "WorkerCounters") -> "WorkerCounters":
        if not isinstance(other, WorkerCounters):
            return NotImplemented
        return WorkerCounters(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in AD13_COUNTER_KEYS
            }
        )

    def as_dict(self) -> Dict[str, int]:
        """The dict-shaped contract AD-13 states, in AD-13 order."""
        return {name: getattr(self, name) for name in AD13_COUNTER_KEYS}


@dataclass(frozen=True)
class FolderTimings:
    """Per-folder phase durations in seconds, summable field-wise.

    Never the shared ``ScanContext.timings`` instance: that one is the
    run's single mutable accumulator with exactly one writer
    (``Folder.scan_tree``, through ``fold_timings``). These are values.
    """

    discovery: float = 0.0
    verification: float = 0.0
    extraction: float = 0.0
    persistence: float = 0.0
    ingest: float = 0.0

    def __add__(self, other: "FolderTimings") -> "FolderTimings":
        if not isinstance(other, FolderTimings):
            return NotImplemented
        return FolderTimings(
            **{
                name: getattr(self, name) + getattr(other, name)
                for name in TIMING_PHASES
            }
        )

    def as_dict(self) -> Dict[str, float]:
        return {name: getattr(self, name) for name in TIMING_PHASES}


@dataclass(frozen=True)
class WorkerResult:
    """One folder's result — the AD-13 shape, exactly six fields.

    ``clips`` carries the live ``Clip`` objects because the single-folder
    façade rebuild needs them (the story-1.3 pins assert
    ``response["clips"]`` on a one-folder scan). Honest scope: freezing
    prevents rebinding and prevents mutating the counters/timings; it does
    not deep-freeze the clips, which stay mutable model instances owned by
    exactly one folder's worker and never shared.
    """

    folder_path: str
    clips: Tuple[Any, ...] = ()
    counters: WorkerCounters = WorkerCounters()
    errors: Tuple[str, ...] = ()
    timings: FolderTimings = FolderTimings()
    log_lines: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        """The dict-shaped contract AD-13 states, in AD-13 order."""
        return {name: getattr(self, name) for name in AD13_KEYS}


@dataclass(frozen=True)
class FolderOutcome:
    """The walk's wrapper around a frozen ``WorkerResult``.

    ``failed`` is set **only** by the never-raising ``process_folder``
    wrapper, when it caught an exception at the folder boundary. Per-file
    errors never set it — a zero-hit folder with two unreadable files was
    scanned, and today's ``count += 1`` counted it.

    ``consumed_subdirs`` is 2.6's three-state descent authorization: a
    ``frozenset`` (descend into every child except these), or ``None``
    (DOUBT — descent is not authorized for this folder at all). The two
    are NEVER interchangeable.
    """

    result: WorkerResult
    consumed_subdirs: Any = None
    failed: bool = False


@dataclass(frozen=True)
class RunResult:
    """The merged shape — a different type, and it carries no clips."""

    counters: WorkerCounters
    errors: Tuple[str, ...]
    timings: FolderTimings
    log_lines: Tuple[str, ...]
    folders_scanned: int
    folders_failed: int


@dataclass(frozen=True)
class WorkItem:
    """One unit of the LIFO work queue.

    ``depth`` is the folder's own level below the root container (the
    root's children are depth 1); ``merge_key`` is the tuple of sibling
    ordinals from the root.
    """

    storage_id: str
    path: str
    depth: int
    merge_key: Tuple[int, ...]


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


class PhaseTimer:
    """Mutable per-folder accumulator, frozen into ``FolderTimings``.

    One instance per ``process_folder`` call. ``discovery`` accumulates
    across EVERY page of the same folder, which is why this is an
    accumulator rather than a single bracket.
    """

    __slots__ = ("_totals",)

    def __init__(self):
        self._totals = dict.fromkeys(TIMING_PHASES, 0.0)

    @contextmanager
    def __call__(self, phase: str):
        if phase not in self._totals:
            raise KeyError(f"unknown timing phase {phase!r}")
        started = time.monotonic()
        try:
            yield
        finally:
            # try/finally, not a bare bracket: a phase that raised still
            # cost the wall-clock time it burned, and the per-file error
            # wrapper sits INSIDE several of these.
            self._totals[phase] += time.monotonic() - started

    def add(self, phase: str, seconds: float) -> None:
        """Add a duration measured elsewhere (no context manager needed)."""
        if phase not in self._totals:
            raise KeyError(f"unknown timing phase {phase!r}")
        self._totals[phase] += seconds

    def freeze(self) -> FolderTimings:
        return FolderTimings(**self._totals)


def fold_timings(ctx, timings: FolderTimings):
    """Fold ``timings`` into the run's single mutable accumulator.

    ``ScanContext.timings`` has exactly one writer, and this is it —
    called once per run by ``Folder.scan_tree`` with the MERGED
    ``FolderTimings``, never per folder and never by a worker.
    """
    accumulator = ctx.timings
    for phase in TIMING_PHASES:
        setattr(
            accumulator, phase, getattr(accumulator, phase) + getattr(timings, phase)
        )
    return accumulator


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def merge_results(outcomes) -> RunResult:
    """Fold outcomes into one ``RunResult``, in the order given.

    ``walk_tree`` already returns its outcomes in merge-key order, so this
    concatenates rather than re-sorts: counters and timings sum field-wise,
    ``errors``/``log_lines`` concatenate in that order.

    Merged ``hits`` is the SUM of the per-folder ``counters.hits`` — never
    a query-level total. Epic 4's bucketed discovery matches that rule
    already: its ``regexp`` parent filter is anchored, so per-folder
    buckets are disjoint.
    """
    counters = WorkerCounters()
    timings = FolderTimings()
    errors: List[str] = []
    log_lines: List[str] = []
    folders_scanned = 0
    folders_failed = 0
    for outcome in outcomes:
        result = outcome.result
        counters = counters + result.counters
        timings = timings + result.timings
        errors.extend(result.errors)
        log_lines.extend(result.log_lines)
        if outcome.failed:
            folders_failed += 1
        else:
            folders_scanned += 1
    return RunResult(
        counters=counters,
        errors=tuple(errors),
        timings=timings,
        log_lines=tuple(log_lines),
        folders_scanned=folders_scanned,
        folders_failed=folders_failed,
    )


# ---------------------------------------------------------------------------
# Façade rebuild (NFR-5): the frozen response shapes, rebuilt from a result
# ---------------------------------------------------------------------------


def build_scan_response(outcome: FolderOutcome) -> Dict[str, Any]:
    """``Folder.scan``'s frozen response, rebuilt from one outcome.

    Values are LISTS, not tuples, and their order is preserved: the story
    1.3 pins assert whole-dict equality including the clip and error lists
    in order.
    """
    result = outcome.result
    counters = result.counters
    return {
        "clips": list(result.clips),
        "hits": counters.hits,
        "errors": list(result.errors),
        "created": counters.created,
        "already_ingested": counters.already_ingested,
        "processed": counters.processed,
        "consumed_subdirs": outcome.consumed_subdirs,
    }


def build_ingest_response(outcome: FolderOutcome) -> Dict[str, Any]:
    """``Folder.ingest``'s frozen response: the scan keys plus four."""
    counters = outcome.result.counters
    response = build_scan_response(outcome)
    response["ingested"] = counters.ingested
    response["skipped"] = counters.skipped
    response["failed"] = counters.failed
    response["replaced"] = counters.replaced
    return response


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def summary_lines(run_result: RunResult, ctx, elapsed: float) -> List[str]:
    """The end-of-run summary (supersedes 2.7's single-count string).

    The window line deliberately does NOT start with ``"Scanning folders
    from "``: that prefix belongs to the commands' ``format_window_log``,
    which is emitted once at the start of the run and pinned as the only
    line carrying it.
    """
    timings = run_result.timings
    prefix = "DRY-RUN: " if ctx.options.dry_run else ""
    lines = [
        f"{prefix}{run_result.folders_scanned} folders scanned, "
        f"{run_result.folders_failed} failed in {elapsed:.1f}s — "
        f"discovery {timings.discovery:.1f}s, "
        f"verification {timings.verification:.1f}s, "
        f"extraction {timings.extraction:.1f}s, "
        f"persistence {timings.persistence:.1f}s, "
        f"ingest {timings.ingest:.1f}s"
    ]
    date_window = getattr(ctx.options, "date_window", ()) or ()
    if date_window:
        lines.append(
            f"Window: {date_window[0]} to {date_window[-1]} "
            f"({len(date_window)} day folders)"
        )
    return lines


# ---------------------------------------------------------------------------
# AD-14: the half a mode check can really police
# ---------------------------------------------------------------------------


def assert_mode_options(options, mode: str) -> None:
    """Reject tree-only options in paged mode.

    The other half of AD-14 — "pagination params are illegal in tree mode"
    — needs no assertion: ``Folder.scan_tree(ctx, *, emit)`` exposes no
    pagination surface at all, so tree mode cannot be handed any.
    (``number=0`` inside the walk is the loop-all-pages sentinel, not a
    pagination argument, which is why the obvious guard would have
    rejected the normal cron path.)

    Truthiness, not mere presence: an option carrying its default is not
    a tree-only option being USED. Raises ``ValueError``; the model
    boundaries re-raise it as ``TapelessIngestException``.
    """
    if mode != "paged":
        return
    offending = [name for name in TREE_ONLY_OPTIONS if getattr(options, name, None)]
    if offending:
        raise ValueError(
            f"tree-only option(s) {', '.join(sorted(offending))} cannot be "
            f"used in paged mode"
        )


# ---------------------------------------------------------------------------
# 2.6's recursion semantics, relocated verbatim
# ---------------------------------------------------------------------------


def should_scan_entry(name, skip=None, only=None, startwith=None, date_window=None):
    """Do this subdirectory's filters allow it to be scanned?

    The legacy substring semantics verbatim (``str.find(...) != -1``, not
    ``in``) in the legacy order. Relocated unchanged from the two command
    modules by story 2.8: there is exactly one copy now, so the drift the
    sync test guarded against is structurally impossible.

    ``--skip``/``--only`` are the OPERATOR's filters and are evaluated at
    EVERY depth (FR-20), while ``startwith`` and ``date_window`` select
    shoot folders at depth 1 only — the caller simply does not pass them
    below that level.
    """
    if skip:
        # search in skip if name contains one of the values
        for skip_entry in skip:
            if name.find(skip_entry) != -1:
                return False
    if only:
        # search in only if name contains one of the values
        if not any(name.find(only_entry) != -1 for only_entry in only):
            return False
    if startwith:
        # search in startwith if name begins with one of the values
        if not any(name.startswith(prefix) for prefix in startwith):
            return False
    if date_window:
        # one YYYYMMDD value per window day, same substring semantics
        if not any(name.find(day) != -1 for day in date_window):
            return False
    return True


# ---------------------------------------------------------------------------
# The fan-out seam
# ---------------------------------------------------------------------------


class SequentialDispatcher:
    """The default fan-out: ``dispatch`` runs now, ``gather`` yields it.

    Epic 3 substitutes ``executor.submit``/``as_completed`` for these two
    bound methods and changes nothing else — the merge key already makes
    the output independent of completion order.
    """

    __slots__ = ("_done",)

    def __init__(self):
        self._done = []

    def dispatch(self, fn: Callable[[WorkItem], FolderOutcome], item: WorkItem) -> None:
        self._done.append((item, fn(item)))

    def gather(self):
        """Every ``(item, outcome)`` pair completed since the last call."""
        done, self._done = self._done, []
        return done


def _child_items(ctx, storage_id, path, depth, merge_key, consumed, options):
    """The eligible children of one folder, plus the lines to surface.

    2.6's rules, relocated verbatim: one ``FolderListings`` per walk
    level, ``sorted(listing.dirs)`` (symlinked directories excluded —
    FR-24's cycle guard), the ``consumed`` skip, and scandir failures
    surfaced instead of degrading to a silently empty directory (FR-22).

    The ordinal is the child's index in the FULL ``sorted(listing.dirs)``,
    assigned before the filters run: the merge key must be a property of
    the tree, not of which filters a particular run was given.
    """
    lines = []
    absolute_path = ctx.absolute_path_for(storage_id, path)
    if not absolute_path:
        # FR-28: an unresolvable storage is reported and this branch stops
        # here, instead of os.scandir(False) raising out of the run.
        lines.append(f"Cannot get full path from storage {storage_id}, path {path}")
        return (), lines
    listings = FolderListings()
    listing = listings.get(absolute_path)
    for listing_path, listing_error in sorted(listings.errors().items()):
        lines.append(f"Error listing directory {listing_path}: {listing_error}")
    if listing.error is not None:
        return (), lines
    # Depth-1 filters are simply not passed below depth 1 (FR-20); the
    # operator's own two apply at every depth.
    startwith = getattr(options, "startwith", ()) if depth == 1 else ()
    date_window = getattr(options, "date_window", ()) if depth == 1 else ()
    items = []
    for ordinal, name in enumerate(sorted(listing.dirs)):
        # NFR-1: a child this folder's own clips already covered is never
        # scanned again as a folder of its own — that IS the duplicate.
        if name in consumed:
            continue
        if not should_scan_entry(
            name,
            skip=getattr(options, "skip", ()),
            only=getattr(options, "only", ()),
            startwith=startwith,
            date_window=date_window,
        ):
            continue
        items.append(
            WorkItem(
                storage_id=storage_id,
                path=os.path.join(path, name),
                depth=depth,
                merge_key=merge_key + (ordinal,),
            )
        )
    return tuple(items), lines


def _with_extra_lines(outcome: FolderOutcome, lines) -> FolderOutcome:
    """Append walk-side lines to a frozen result's errors AND log_lines.

    A folder's own listing failure is discovered by the coordinator, after
    the worker returned, but it belongs to that folder — and it has to
    reach both the operator's report (``log_lines``) and the error
    accounting (``errors``), exactly like the folder-boundary templates.
    """
    if not lines:
        return outcome
    result = outcome.result
    return replace(
        outcome,
        result=replace(
            result,
            errors=result.errors + tuple(lines),
            log_lines=result.log_lines + tuple(lines),
        ),
    )


def walk_tree(
    root_storage_id,
    root_path,
    *,
    ctx,
    process_folder,
    dispatch,
    gather,
    depth: int = 1,
    emit: Optional[Callable[[str], None]] = None,
) -> List[FolderOutcome]:
    """Walk the tree under ``root_path``, returning outcomes in merge order.

    ``depth`` is the depth assigned to the ROOT's enqueued children, not
    the root's own: the root folder is a container, never handed to
    ``process_folder`` and never counted, so the default ``1`` gives its
    children depth 1 and the depth-1 filters apply to them exactly as they
    did before this story.

    Fan-out is an explicit work queue. Children are enqueued by the
    COORDINATOR when a parent's outcome authorizes descent — a worker
    never submits work, which is what lets ``dispatch``/``gather`` become
    a real pool in Epic 3 without touching anything else.

    ``emit`` is used for exactly one thing: surfacing the ROOT container's
    own listing failure, which has no ``FolderOutcome`` to ride on (the
    root is never processed) and by construction happens before any folder
    line. Every other line rides in a ``WorkerResult``.
    """
    options = ctx.options
    completed: List[Tuple[Tuple[int, ...], FolderOutcome]] = []

    def run(item: WorkItem) -> FolderOutcome:
        # Tree mode's calling convention: one complete pass over the whole
        # folder (`number=0` is the loop-all-pages sentinel) in the ingest
        # shape, so all eight counters exist. Whether ingestion actually
        # WRITES stays governed by ctx.options.dry_run.
        return process_folder(
            item.storage_id,
            item.path,
            ctx,
            first=0,
            number=0,
            cursor=None,
            count_only=False,
            ingest=True,
        )

    root_items, root_lines = _child_items(
        ctx,
        root_storage_id,
        root_path,
        depth,
        (),
        frozenset(),
        options,
    )
    if emit is not None:
        for line in root_lines:
            emit(line)
    # LIFO, children pushed reversed: the sequential run's DISPATCH order
    # is then today's depth-first pre-order. Output order does not depend
    # on it (that is the merge key's job), but a cron log that reads the
    # same as yesterday's is worth the two characters.
    stack: List[WorkItem] = list(reversed(root_items))
    pending = 0
    while stack or pending:
        if stack:
            dispatch(run, stack.pop())
            pending += 1
        for done_item, outcome in gather():
            pending -= 1
            if outcome.consumed_subdirs is None:
                # DOUBT: descent is not authorized for this folder. The
                # reason is already in its errors (2.6).
                completed.append((done_item.merge_key, outcome))
                continue
            children, lines = _child_items(
                ctx,
                done_item.storage_id,
                done_item.path,
                done_item.depth + 1,
                done_item.merge_key,
                outcome.consumed_subdirs,
                options,
            )
            completed.append((done_item.merge_key, _with_extra_lines(outcome, lines)))
            stack.extend(reversed(children))
    completed.sort(key=lambda pair: pair[0])
    return [outcome for _, outcome in completed]


# ---------------------------------------------------------------------------
# AD-13 is exact — proven here, not merely asserted in prose
# ---------------------------------------------------------------------------

assert tuple(field.name for field in fields(WorkerResult)) == AD13_KEYS
assert tuple(WorkerResult(folder_path="").as_dict()) == AD13_KEYS
assert tuple(field.name for field in fields(WorkerCounters)) == AD13_COUNTER_KEYS
assert tuple(WorkerCounters().as_dict()) == AD13_COUNTER_KEYS
assert tuple(field.name for field in fields(FolderTimings)) == TIMING_PHASES

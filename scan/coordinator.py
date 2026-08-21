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
from heapq import heappop, heappush
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from .verification import FolderListings

__all__ = [
    "AD13_COUNTER_KEYS",
    "AD13_KEYS",
    "MAX_IDLE_ROUNDS",
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

# Options that only tree mode may carry. The four folder filters are
# genuinely meaningless in paged mode — a paged call scans ONE folder and
# never walks, so a `--skip`/`--only`/`--startWith`/date-window narrowing
# could only mislead a caller into thinking it had been applied. Epic 3
# adds `workers` and Epic 4 adds `discovery` to this tuple.
TREE_ONLY_OPTIONS = ("skip", "only", "startwith", "date_window")

# Safety valve for the fan-out seam (E29). A `gather()` that never reports
# a dispatched item would otherwise spin forever with the stack empty and
# work outstanding — a hung nightly run with no output. The sequential
# dispatcher never idles at all, and Epic 3's blocking `as_completed`
# gather will not either; only a BROKEN dispatcher reaches this, and it
# fails loudly instead of hanging.
MAX_IDLE_ROUNDS = 100_000


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
    are NEVER interchangeable — which is exactly why the annotation is
    ``Optional[FrozenSet[str]]`` and not ``Any``: the three states are the
    contract, and a reader meets them here first.

    ``merge_key`` travels WITH the outcome so the ordering guarantee is a
    property of the data rather than of ``walk_tree`` happening to be the
    only caller of ``merge_results``. ``()`` is the root container's key
    and sorts first.

    ``listings`` is the worker's own ``FolderListings`` handed back so the
    walk can reuse it for this folder's subdirectory discovery instead of
    paying a second ``os.scandir`` on the same directory (and reporting
    the same failure twice). The walk drops it the moment it has expanded
    the children — it must never be retained for the run, and Epic 3 must
    not serialize it across a process boundary.
    """

    result: WorkerResult
    consumed_subdirs: Optional[FrozenSet[str]] = None
    failed: bool = False
    merge_key: Tuple[int, ...] = ()
    listings: Any = None


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
    """Fold outcomes into one ``RunResult``, in MERGE-KEY order.

    The sort happens HERE, not only in ``walk_tree``: the ordering
    guarantee has to be a property of the outcomes themselves, or it holds
    only for as long as ``walk_tree`` is the sole caller. Python's sort is
    stable, so a caller handing over outcomes that all carry the default
    ``()`` key gets its own order back untouched.

    Counters and timings sum field-wise; ``errors``/``log_lines``
    concatenate in merge-key order.

    Merged ``hits`` is the SUM of the per-folder ``counters.hits`` — never
    a query-level total. Epic 4's bucketed discovery matches that rule
    already: its ``regexp`` parent filter is anchored, so per-folder
    buckets are disjoint.

    INHERITED DEFECT, named rather than hidden: each per-folder ``hits``
    is the LAST PAGE's ``total``, not the folder's true match count — see
    ``tests/pinned-bugs.md`` ("hits reflects only the last page's total"),
    pinned by ``tests/tier1/test_scan_pagination.py``. Summing an
    already-wrong number does not make it right; the sum is exactly as
    accurate as the legacy per-folder figure the cron has always printed.
    Epic 4's `search_after` discovery is where that gets fixed.
    """
    counters = WorkerCounters()
    timings = FolderTimings()
    errors: List[str] = []
    log_lines: List[str] = []
    folders_scanned = 0
    folders_failed = 0
    for outcome in sorted(outcomes, key=lambda outcome: outcome.merge_key):
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

    Three lines at most, in this order:

    1. folders and time — including an ``other`` term, so the phases
       RECONCILE with the printed wall clock instead of visibly failing
       to. Three real per-folder costs sit outside the five timed phases
       by design (the pass-2 bulk umid lookup, hash recovery's HTTP calls,
       and the ``consumed_subdirs`` computation), and so does the walk's
       own directory listing; ``other`` is all of it plus whatever else
       the run spent. It is clamped at zero because under Epic 3 the phase
       sums are worker-time and the elapsed is wall-clock, so the
       difference legitimately goes negative once work overlaps — at which
       point ``other`` stops being meaningful and says so by reading 0.0s.
    2. the COUNTERS. `RunResult` knows all eight and the errors; a summary
       that reported only folder counts made the operator open the log to
       learn whether anything was ingested.
    3. the window, when one is set. It deliberately does NOT start with
       ``"Scanning folders from "``: that prefix belongs to the commands'
       ``format_window_log``, emitted once at the start of the run and
       pinned as the only line carrying it.
    """
    timings = run_result.timings
    counters = run_result.counters
    prefix = "DRY-RUN: " if ctx.options.dry_run else ""
    other = max(0.0, elapsed - sum(timings.as_dict().values()))
    lines = [
        f"{prefix}{run_result.folders_scanned} folders scanned, "
        f"{run_result.folders_failed} failed in {elapsed:.1f}s — "
        f"discovery {timings.discovery:.1f}s, "
        f"verification {timings.verification:.1f}s, "
        f"extraction {timings.extraction:.1f}s, "
        f"persistence {timings.persistence:.1f}s, "
        f"ingest {timings.ingest:.1f}s, "
        f"other {other:.1f}s",
        f"{prefix}{counters.hits} clips found, "
        f"{counters.created} created, "
        f"{counters.already_ingested} already ingested, "
        f"{counters.processed} processed, "
        f"{counters.ingested} ingested, "
        f"{counters.skipped} skipped, "
        f"{counters.failed} failed, "
        f"{counters.replaced} replaced, "
        f"{len(run_result.errors)} errors",
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


def _child_items(
    ctx,
    storage_id,
    path,
    depth,
    merge_key,
    consumed,
    options,
    *,
    listings=None,
    seen_real_paths=None,
):
    """The eligible children of one folder, plus the lines to surface.

    2.6's rules, relocated verbatim: ``sorted(listing.dirs)`` (symlinked
    directories excluded — FR-24's cycle guard), the ``consumed`` skip,
    and scandir failures surfaced instead of degrading to a silently empty
    directory (FR-22).

    The ordinal is the child's index in the FULL ``sorted(listing.dirs)``,
    assigned before the filters run: the merge key must be a property of
    the tree, not of which filters a particular run was given.

    ``listings`` is the worker's own cache when there is one. Reusing it
    saves the second ``os.scandir`` of a directory the worker has already
    listed for verification — and, because only listing errors this call
    NEWLY discovered are reported, it also stops the same
    ``Error listing directory`` string being appended twice.

    This ``get`` is SCAN-REQUIRED (the default): the walk needs this
    directory, so its failure is reported whatever the errno — including
    the ``ENOENT`` that is silenced for a provider's speculative sidecar
    probe. A folder that vanished between the index and the walk is a
    real divergence, not a card layout that is simply not there.

    ``seen_real_paths`` is the run-wide realpath set (NFR-1). A bind mount
    or a hardlinked directory gives one physical tree two distinct paths;
    walking both means two scans racing to ingest the same clips before
    either has written a row, which the umid primary key cannot save us
    from because both passes read "absent" first. FR-24's symlink guard
    does not cover this: these are real directories.
    """
    lines = []
    absolute_path = ctx.absolute_path_for(storage_id, path)
    if not absolute_path:
        # FR-28: an unresolvable storage is reported and this branch stops
        # here, instead of os.scandir(False) raising out of the run.
        lines.append(f"Cannot get full path from storage {storage_id}, path {path}")
        return (), lines, True
    if listings is None:
        listings = FolderListings()
    known_errors = set(listings.errors())
    listing = listings.get(absolute_path)
    for listing_path, listing_error in sorted(listings.errors().items()):
        if listing_path in known_errors:
            # The worker already reported it into this folder's errors.
            continue
        lines.append(f"Error listing directory {listing_path}: {listing_error}")
    if listing.error is not None:
        return (), lines, True
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
        if seen_real_paths is not None:
            real_path = os.path.realpath(os.path.join(absolute_path, name))
            if real_path in seen_real_paths:
                lines.append(
                    f"Already walked {os.path.join(path, name)} "
                    f"through another path ({real_path}); not scanning it twice"
                )
                continue
            seen_real_paths.add(real_path)
        items.append(
            WorkItem(
                storage_id=storage_id,
                path=os.path.join(path, name),
                depth=depth,
                merge_key=merge_key + (ordinal,),
            )
        )
    # Third value: whether the EXPANSION ITSELF failed. `lines` alone
    # cannot say — a realpath-dedupe note is an anomaly worth reporting on
    # a folder that expanded perfectly well, and treating it as a failure
    # would invent a failed root out of a bind mount.
    return tuple(items), lines, False


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


def _walk_failure(path, merge_key, lines) -> FolderOutcome:
    """A folder-shaped failure the WALK produced, not the worker.

    Used for the root container and for a ``dispatch()`` that raised. It
    is ``failed=True`` and its lines land in both ``errors`` and
    ``log_lines``, so the run reports it exactly like a worker-side
    folder-boundary failure — a root path that has gone missing must not
    read as "0 folders scanned, 0 failed", which is indistinguishable from
    an empty tree.
    """
    return FolderOutcome(
        result=WorkerResult(
            folder_path=path,
            errors=tuple(lines),
            log_lines=tuple(lines),
        ),
        consumed_subdirs=None,
        failed=True,
        merge_key=merge_key,
    )


def _drop_clips(outcome: FolderOutcome) -> FolderOutcome:
    """Strip the live ``Clip`` objects from an outcome the walk will keep.

    Prod holds 188,082 clips across 8,133 folders. ``RunResult`` was given
    no ``clips`` field for exactly that reason — but the walk accumulates
    every ``FolderOutcome`` until it returns, so dropping them only at
    merge time retained the whole corpus anyway. Tree mode never hands an
    outcome to a façade, so the clips are dead the moment the worker's
    own ``consumed_subdirs`` computation is done with them.
    """
    if not outcome.result.clips:
        return outcome
    return replace(outcome, result=replace(outcome.result, clips=()))


def walk_tree(
    root_storage_id,
    root_path,
    *,
    ctx,
    process_folder,
    dispatch,
    gather,
    emit: Callable[[str], None],
    depth: int = 1,
) -> List[FolderOutcome]:
    """Walk the tree under ``root_path``, returning outcomes in merge order.

    ``depth`` is the depth assigned to the ROOT's enqueued children, not
    the root's own: the root folder is a container, never handed to
    ``process_folder`` and never counted as scanned, so the default ``1``
    gives its children depth 1 and the depth-1 filters apply to them
    exactly as they did before this story. (A root that cannot be LISTED
    is a different matter — that is a run failure and is counted as one.)

    Fan-out is an explicit work queue. Children are enqueued by the
    COORDINATOR when a parent's outcome authorizes descent — a worker
    never submits work, which is what lets ``dispatch``/``gather`` become
    a real pool in Epic 3 without touching anything else.

    ``emit`` is REQUIRED, and lines are drained INCREMENTALLY: a folder's
    log lines go out as soon as every folder that sorts before it has
    completed. An 8,000-folder run that printed nothing until the walk
    returned gave the operator no way to tell a slow run from a hung one.
    The emission order is still exactly merge-key order, so a sequential
    run and a pooled one produce byte-identical output — the incremental
    release is a lower bound computed from what is still outstanding, not
    a guess.
    """
    options = ctx.options
    released: List[FolderOutcome] = []
    # merge_key -> outcome, for keys completed but not yet releasable.
    completed: Dict[Tuple[int, ...], FolderOutcome] = {}
    # Min-heaps over merge keys. `pending` is every key enqueued or in
    # flight; `ready` is every key completed and awaiting release. A key
    # may be released once it is smaller than every pending key, because
    # any key discovered LATER is a descendant of something pending and a
    # descendant always sorts after its ancestor.
    pending_heap: List[Tuple[int, ...]] = []
    pending_keys = set()
    ready: List[Tuple[int, ...]] = []
    # NFR-1: one physical directory is walked once, whichever path reached
    # it. Seeded with the root so a child that loops back cannot re-enter.
    seen_real_paths = set()

    def enqueue(items):
        for item in items:
            pending_keys.add(item.merge_key)
            heappush(pending_heap, item.merge_key)

    def record(outcome: FolderOutcome):
        pending_keys.discard(outcome.merge_key)
        completed[outcome.merge_key] = _drop_clips(replace(outcome, listings=None))
        heappush(ready, outcome.merge_key)

    def release():
        while pending_heap and pending_heap[0] not in pending_keys:
            heappop(pending_heap)  # lazily purge keys already completed
        floor = pending_heap[0] if pending_heap else None
        while ready and (floor is None or ready[0] < floor):
            outcome = completed.pop(heappop(ready))
            for line in outcome.result.log_lines:
                emit(line)
            released.append(outcome)

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

    root_absolute = ctx.absolute_path_for(root_storage_id, root_path)
    if root_absolute:
        seen_real_paths.add(os.path.realpath(root_absolute))
    root_items, root_lines, root_failed = _child_items(
        ctx,
        root_storage_id,
        root_path,
        depth,
        (),
        frozenset(),
        options,
        seen_real_paths=seen_real_paths,
    )
    if root_failed:
        # The root has no worker outcome to ride on, so its failure gets
        # one of its own — counted, not merely printed. A run over a root
        # that has gone missing must not read as "0 scanned, 0 failed".
        record(_walk_failure(root_path, (), root_lines))
    elif root_lines:
        # Notes, not a failure (a realpath-deduped child). They belong to
        # the container, which has no outcome, so they go out directly —
        # before any folder line, deterministically.
        for line in root_lines:
            emit(line)
    # LIFO, children pushed reversed: the sequential run's DISPATCH order
    # is then today's depth-first pre-order. Output order does not depend
    # on it (that is the merge key's job), but a cron log that reads the
    # same as yesterday's is worth the two characters.
    stack: List[WorkItem] = list(reversed(root_items))
    enqueue(root_items)
    release()
    idle_rounds = 0
    while stack or pending_keys:
        if stack:
            item = stack.pop()
            try:
                dispatch(run, item)
            except Exception as e:
                # A raising dispatcher must not take the run's completed
                # work with it. `process_folder` never raises, so this is
                # the POOL failing (a rejected submission, a dead worker),
                # and the honest report is one failed folder plus a run
                # that keeps going.
                record(
                    _walk_failure(
                        item.path,
                        item.merge_key,
                        [f"Error scanning {item.path}: {e}"],
                    )
                )
                release()
                continue
        batch = list(gather())
        if batch:
            idle_rounds = 0
        elif not stack:
            idle_rounds += 1
            if idle_rounds > MAX_IDLE_ROUNDS:
                raise RuntimeError(
                    f"walk_tree made no progress in {MAX_IDLE_ROUNDS} rounds "
                    f"with {len(pending_keys)} folder(s) still outstanding: "
                    f"gather() is not reporting dispatched work"
                )
        for done_item, outcome in batch:
            outcome = replace(outcome, merge_key=done_item.merge_key)
            if outcome.consumed_subdirs is None:
                # DOUBT: descent is not authorized for this folder. The
                # reason is already in its errors (2.6).
                record(outcome)
                continue
            children, lines, _failed = _child_items(
                ctx,
                done_item.storage_id,
                done_item.path,
                done_item.depth + 1,
                done_item.merge_key,
                outcome.consumed_subdirs,
                options,
                listings=outcome.listings,
                seen_real_paths=seen_real_paths,
            )
            enqueue(children)
            record(_with_extra_lines(outcome, lines))
            stack.extend(reversed(children))
        release()
    release()
    return released


# ---------------------------------------------------------------------------
# AD-13 is exact — proven here, not merely asserted in prose
# ---------------------------------------------------------------------------

assert tuple(field.name for field in fields(WorkerResult)) == AD13_KEYS
assert tuple(WorkerResult(folder_path="").as_dict()) == AD13_KEYS
assert tuple(field.name for field in fields(WorkerCounters)) == AD13_COUNTER_KEYS
assert tuple(WorkerCounters().as_dict()) == AD13_COUNTER_KEYS
assert tuple(field.name for field in fields(FolderTimings)) == TIMING_PHASES

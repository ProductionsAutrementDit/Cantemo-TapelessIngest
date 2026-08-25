"""Frozen per-run scan context and the canonical storage-root resolution.

Stdlib-only by contract (AD-1): this module must import with no Portal
stub installed. Anything that talks to Portal lives in ``scan.adapters``.

``ScanContext`` is immutable-after-fan-out (AD-4): every dataclass here is
frozen except ``PhaseTimings``, the single designated mutable slot
(structure only in 2.1 — story 2.8 writes it). ``storages`` is wrapped in
a read-only mapping proxy at construction.
"""

import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, List, Mapping, Optional, Tuple


def browse_root_path(storage) -> Optional[str]:
    """Canonical storage-methods -> browse -> first-URI-url block.

    Duck-typed over the opaque Vidispine storage object; a falsy storage or
    a storage with no browse-capable method resolves to ``None`` (callers
    keep today's truthiness/AttributeError semantics on top of that).

    FIRST-match is the contract (review-ruled): the first browse-capable
    method's URI wins. This deliberately unifies the pre-2.1 copies — the
    three property-site loops let the LAST browse method win, the provider
    copy the first; no storage in practice has two browse methods.
    """
    if not storage:
        return None
    for method in storage.getMethods():
        if method.getBrowse():
            return (method.getFirstURI() or {}).get("url")
    return None


@dataclass(frozen=True)
class StorageInfo:
    """One resolved storage: id, its browse root, and the opaque VS object."""

    id: str
    root_path: Optional[str]
    storage: Any = None


@dataclass(frozen=True)
class RunOptions:
    """The run-scoped options a passed context carries authoritatively.

    The four folder filters are AD-4 run options, not loose kwargs: they
    were an argparse artifact threaded through the old recursion by hand.
    They are TUPLES so a frozen context really is frozen — a list on a
    "frozen" dataclass is a mutable field with a promise on it — and they
    default to ``()`` so every pre-2.8 construction site still compiles.

    ``providers`` and ``legacy_storages`` are tuples for the same reason,
    and stopped being lists in the Epic 2 final review round: two mutable
    fields on a frozen dataclass whose four newest fields were tuples
    precisely to avoid that. ``None`` is preserved and is NOT ``()`` —
    for ``providers`` it means "the canonical registry", which is a
    different instruction from "no providers".

    The LEVEL at which each applies is not expressed here: a run-scoped
    object cannot say "depth 1 only". ``scan.coordinator.walk_tree`` owns
    that, through its explicit ``depth`` parameter — ``skip``/``only``
    apply at every depth, ``startwith``/``date_window`` only at depth 1.
    """

    dry_run: bool = False
    providers: Optional[Tuple[str, ...]] = None
    legacy_storages: Optional[Tuple[str, ...]] = None
    replace: bool = False
    user: Any = None
    skip: Tuple[str, ...] = ()
    only: Tuple[str, ...] = ()
    startwith: Tuple[str, ...] = ()
    date_window: Tuple[str, ...] = ()
    # Story 3.1: how many pool workers a TREE run may use. A scalar on the
    # frozen dataclass, defaulting to 1 so every pre-3.1 construction site
    # (build_default_context included) keeps paged mode inline and never
    # constructs an executor. Validated >= 1 at the command boundary
    # (AD-10); rejected > 1 in paged mode by assert_mode_options (AD-14).
    workers: int = 1


@dataclass
class PhaseTimings:
    """Mutable accumulator for per-phase durations (seconds).

    Structure only in 2.1; story 2.8 made it real. It has exactly ONE
    writer: ``Folder.scan_tree`` folds the run's merged ``FolderTimings``
    into it once, through ``scan.coordinator.fold_timings``. Workers time
    themselves into their own per-folder ``FolderTimings`` value and never
    touch this instance — which is what keeps it safe when Epic 3 turns
    the workers into a pool.
    """

    discovery: float = 0.0
    verification: float = 0.0
    extraction: float = 0.0
    persistence: float = 0.0
    ingest: float = 0.0


@dataclass(frozen=True)
class ScanContext:
    """One per-run context: storages resolved once, options, extension seams.

    ``provider_registry`` and ``extension_map`` are extension points for
    story 2.3 and stay ``None`` in 2.1.
    """

    storages: Mapping[str, StorageInfo]
    options: RunOptions
    provider_registry: Any = None
    extension_map: Any = None
    timings: PhaseTimings = field(default_factory=PhaseTimings)

    def __post_init__(self):
        # Frozen dataclass: bypass the frozen __setattr__ once to install
        # the read-only view; ctx.storages["X"] = ... raises TypeError.
        object.__setattr__(
            self, "storages", MappingProxyType(dict(self.storages or {}))
        )

    def root_path_for(self, storage_id) -> Optional[str]:
        """Resolved browse root for ``storage_id``, or ``None`` on a miss."""
        info = self.storages.get(storage_id)
        if info is None:
            return None
        return info.root_path

    def absolute_path_for(self, storage_id, path) -> Optional[str]:
        """Join ``path`` onto the resolved root.

        ``None`` on a storage miss, a falsy root, or a ``None`` path —
        never hand a ``None`` to ``os.path.join``.
        """
        root_path = self.root_path_for(storage_id)
        if not root_path or path is None:
            return None
        return os.path.join(root_path, path)

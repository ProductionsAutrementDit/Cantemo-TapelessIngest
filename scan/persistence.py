"""Pure persistence-plan layer: what a folder's write unit will write.

Stdlib-only by contract (AD-1), like ``scan.context``, ``scan.extraction``
and ``scan.verification``: this module must import with no Portal stub
installed, so it holds no Django, no ORM and no model imports. Clip
instances travel through it as opaque objects — the plan decides *what*
is written, ``models/folder.py``'s ``persist_scan_results`` executor
decides *how* (AD-6 write unit 1).

The split is coordinator-ready by construction: story 2.8 moves the
executor invocation into the coordinator without touching anything here.

Decisions this layer owns
-------------------------
* **umid dedup** — two files in one folder can carry the same umid (a
  provider's spanned/duplicate layouts). sqlite and PostgreSQL both
  reject the same conflict target twice inside one ``ON CONFLICT``
  statement, so the rows are deduped before they reach the executor:
  first appearance keeps its position, the LAST occurrence's clip wins
  (AD-8 backstop). The scan response keeps every clip it collected —
  dedup applies to the write plan only.
* **the existing-clip update set** — ``CLIP_UPDATE_FIELDS``. Scan
  mutates ``provider_name``/``file_id``/``reference_file`` on the clip
  *object*, but pre-2.4 scan never persisted them for an existing clip
  (it never called ``clip.save()``; only the metadata fan-out ran), so
  they stay out of the statement. ``clip_xml`` is in, and only in,
  because FR-12 makes the scan the place a clip's serialized sidecar
  first reaches the DB. Location columns (``path``, ``storage_id``,
  ``folder_path``) and every ingest-state column are deliberately
  excluded: a moved or re-carded file must never silently rewrite an
  existing clip's location, which is exactly today's behavior.
* **stale-key grouping** — the fan-out's per-clip
  ``exclude(name__in=...).delete()`` becomes one DELETE per distinct
  key-set. Clips of one folder share a provider and therefore a key-set,
  so this is O(1) statements per folder, never O(files) or O(keys).
* **the folder-save gate** — the Folder row is written at most once per
  scan invocation and only when at least one provider claimed a file.
* **recovered item ids** — a scan may learn a clip's Vidispine item id
  from a legacy-storage hash match. That id must reach the row (else the
  lookup repeats every run, forever) without ever overwriting an id the
  row already has, so it travels apart from the upsert, in
  ``recovered_item_ids``, for a fill-only-NULL statement.
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "BULK_BATCH_SIZE",
    "CLIP_UNIQUE_FIELDS",
    "CLIP_UPDATE_FIELDS",
    "FOLDER_SCAN_FIELDS",
    "ClipCandidate",
    "MetadataWrite",
    "PersistencePlan",
    "StaleDelete",
    "build_persistence_plan",
    "chunked",
    "dedupe_candidates",
    "group_stale_deletes",
]

# The Clip upsert's conflict target: the primary key itself.
CLIP_UNIQUE_FIELDS = ("umid",)

# The ONLY columns an existing clip's row may have rewritten by a scan.
# Ask-First territory: adding one here is a behavioral change, not a tweak.
# `item_id` is deliberately NOT here even though a scan can recover one:
# an unconditional ON CONFLICT SET would let a stale in-memory NULL
# overwrite an id a concurrent ingest just wrote. Recovered ids travel in
# `PersistencePlan.recovered_item_ids` instead, under a fill-only-NULL
# guard the ORM cannot express inside a conflict clause.
CLIP_UPDATE_FIELDS = ("clip_xml",)

# The Folder columns a scan owns. `clips_total` is assigned in the page
# loop from the index's hit total, so it is a scan output like the other
# two, not a stale read-back.
FOLDER_SCAN_FIELDS = ("provider_names", "scanned_on", "clips_total")

# Rows per statement. Every write in this pipeline is batched, and a
# batch is bounded: a folder accumulates candidates across ALL its pages,
# so a big card tree can otherwise exceed PostgreSQL's 65535
# bind-parameter ceiling and lose the whole folder's write. 500 rows ×
# ~10 columns leaves an order of magnitude of headroom.
BULK_BATCH_SIZE = 500


@dataclass(frozen=True)
class ClipCandidate:
    """One clip the scan collected, as the plan layer sees it.

    ``clip`` is opaque here — a Django model instance in production, a
    plain object in the Tier-1 units. ``created`` mirrors the scan
    counter (umid absent from the batched lookup), it does not drive the
    write: new and existing rows go through the same upsert statement.
    """

    umid: str
    clip: Any
    metadatas: Optional[Mapping[str, Any]] = None
    created: bool = False


@dataclass(frozen=True)
class MetadataWrite:
    """The full metadata key-set one clip must end the scan with."""

    umid: str
    metadatas: Mapping[str, Any]


@dataclass(frozen=True)
class StaleDelete:
    """Delete every metadata row of ``umids`` whose name is not kept."""

    umids: Tuple[str, ...]
    keep_names: Tuple[str, ...]


@dataclass(frozen=True)
class PersistencePlan:
    """Everything one folder's atomic write unit will do, and nothing more."""

    clip_rows: Tuple[Any, ...] = ()
    update_fields: Tuple[str, ...] = CLIP_UPDATE_FIELDS
    metadata_writes: Tuple[MetadataWrite, ...] = ()
    stale_deletes: Tuple[StaleDelete, ...] = ()
    folder_fields: Mapping[str, Any] = field(default_factory=dict)
    save_folder: bool = False
    # umid -> item id recovered from a legacy storage this scan. Written
    # by a statement that can only FILL a NULL, never overwrite an id, so
    # a clip whose ingest landed between this scan's read and its write
    # keeps the id it earned. Empty on the overwhelming majority of runs.
    recovered_item_ids: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self):
        # Frozen dataclass: bypass the frozen __setattr__ once to install a
        # read-only view; plan.folder_fields["x"] = ... raises TypeError.
        object.__setattr__(
            self, "folder_fields", MappingProxyType(dict(self.folder_fields or {}))
        )

    @property
    def is_empty(self) -> bool:
        """True when the executor would open a transaction for nothing."""
        return not (
            self.clip_rows
            or self.metadata_writes
            or self.stale_deletes
            or self.save_folder
            or self.recovered_item_ids
        )


def dedupe_candidates(
    candidates: Iterable[ClipCandidate],
) -> Tuple[ClipCandidate, ...]:
    """One candidate per umid: first appearance's slot, last one's value."""
    deduped = {}
    for candidate in candidates:
        deduped[candidate.umid] = candidate
    return tuple(deduped.values())


def group_stale_deletes(
    writes: Sequence[MetadataWrite],
) -> Tuple[StaleDelete, ...]:
    """Group the per-clip stale-key deletes by their key-set.

    Clips sharing a key-set (the normal case: one provider per folder)
    collapse into a single DELETE. ``keep_names`` is sorted so the
    grouping is deterministic whatever order the keys were extracted in;
    an EMPTY key-set is preserved as a group, and deletes every metadata
    row of its clips — exactly what the pre-2.4 fan-out did for a clip
    whose metadatas came back empty.
    """
    groups = {}
    for write in writes:
        keep_names = tuple(sorted(write.metadatas or {}))
        groups.setdefault(keep_names, []).append(write.umid)
    return tuple(
        StaleDelete(umids=tuple(umids), keep_names=keep_names)
        for keep_names, umids in groups.items()
    )


def chunked(items: Sequence[Any], size: int = BULK_BATCH_SIZE):
    """Yield ``items`` in slices of at most ``size`` (never an empty one)."""
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start : start + size]


def build_persistence_plan(
    candidates: Iterable[ClipCandidate],
    folder_fields: Optional[Mapping[str, Any]] = None,
    provider_hits: int = 0,
    update_fields: Sequence[str] = CLIP_UPDATE_FIELDS,
    recovered_item_ids: Optional[Mapping[str, str]] = None,
) -> PersistencePlan:
    """Turn the clips a scan collected into one folder's write plan.

    ``provider_hits`` is the number of distinct providers that claimed a
    file in this scan invocation; zero means the folder row is not
    written at all — a zero-hit folder leaves no trace, files that only
    errored included.

    ``recovered_item_ids`` maps umid -> the item id hash recovery found
    for it this run. Only entries with both parts truthy survive: a
    fill-only-NULL update of nothing is a statement for nothing.
    """
    deduped = dedupe_candidates(candidates)
    metadata_writes = tuple(
        MetadataWrite(umid=candidate.umid, metadatas=dict(candidate.metadatas))
        for candidate in deduped
        if candidate.metadatas is not None
    )
    return PersistencePlan(
        clip_rows=tuple(candidate.clip for candidate in deduped),
        update_fields=tuple(update_fields),
        metadata_writes=metadata_writes,
        stale_deletes=group_stale_deletes(metadata_writes),
        folder_fields=dict(folder_fields or {}),
        save_folder=bool(provider_hits),
        recovered_item_ids=tuple(
            (umid, item_id)
            for umid, item_id in (recovered_item_ids or {}).items()
            if umid and item_id
        ),
    )

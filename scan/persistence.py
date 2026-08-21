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
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

__all__ = [
    "CLIP_UNIQUE_FIELDS",
    "CLIP_UPDATE_FIELDS",
    "FOLDER_SCAN_FIELDS",
    "ClipCandidate",
    "MetadataWrite",
    "PersistencePlan",
    "StaleDelete",
    "build_persistence_plan",
    "dedupe_candidates",
    "group_stale_deletes",
]

# The Clip upsert's conflict target: the primary key itself.
CLIP_UNIQUE_FIELDS = ("umid",)

# The ONLY columns an existing clip's row may have rewritten by a scan.
# Ask-First territory: adding one here is a behavioral change, not a tweak.
CLIP_UPDATE_FIELDS = ("clip_xml",)

# The Folder columns a scan owns.
FOLDER_SCAN_FIELDS = ("provider_names", "scanned_on", "clips_total")


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


def build_persistence_plan(
    candidates: Iterable[ClipCandidate],
    folder_fields: Optional[Mapping[str, Any]] = None,
    provider_hits: int = 0,
    update_fields: Sequence[str] = CLIP_UPDATE_FIELDS,
) -> PersistencePlan:
    """Turn the clips a scan collected into one folder's write plan.

    ``provider_hits`` is the number of distinct providers that claimed a
    file in this scan invocation; zero means the folder row is not
    written at all — a zero-hit folder leaves no trace, files that only
    errored included.
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
    )

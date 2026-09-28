"""Read-only: classify one item and write down exactly what apply will do.

Reads the item's REAL original shape (never ``output_file``, which is
not always what is attached), locates every original on disk, on VX-41
and in P5, and records what is needed to undo the migration.
"""

import posixpath
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Sequence

from portal.plugins.TapelessIngest.wrapped import fields, verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.gateway import Gateway, Shape
from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile, to_absolute
from portal.plugins.TapelessIngest.wrapped.shape import mismatch

ROLLBACK_ITEM_FIELDS = (
    fields.DURATION_FIELD,
    fields.EXTERNAL_IDS_FIELD,
    fields.ARCHIVE_STATUS_FIELD,
    fields.ARCHIVE_TS_FIELD,
    fields.ARCHIVE_PLUGIN_FIELD,
    fields.ARCHIVE_POLICY_FIELD,
    fields.BARCODES_FIELD,
    fields.TAPE_LABELS_FIELD,
    fields.TAPE_NAMES_FIELD,
)


@dataclass
class PlanResult:
    verdict: str
    reason: str = ""
    plan: Dict[str, Any] = field(default_factory=dict)
    rollback: Dict[str, Any] = field(default_factory=dict)


def _rollback(item_id: str, shape: Shape, gateway: Gateway) -> Dict[str, Any]:
    return {
        "wrapped_shape_id": shape.shape_id,
        "shape_files": [asdict(f) for f in shape.files().values()],
        "component_metadata": {
            c.component_id: gateway.component_metadata(
                item_id, shape.shape_id, c.component_id
            )
            for c in shape.components
        },
        "item_fields": gateway.item_fields(item_id, ROLLBACK_ITEM_FIELDS),
        "lowres_shape_ids": gateway.shape_ids(item_id, fields.LOWRES_TAG),
    }


def _names_originals(shape: Shape, originals: Sequence[OriginalFile]) -> bool:
    files = shape.files().values()
    return all(f.storage_id == fields.RUSHES_STORAGE for f in files) and {
        f.path for f in files
    } == {o.relative for o in originals}


def _wrapped_problem(
    shape: Shape, originals: Sequence[OriginalFile], output_file: Optional[str]
) -> Optional[str]:
    files = shape.files()
    if len(files) != 1:
        return f"{len(files)} distinct files on the original shape, expected 1"
    (attached,) = files.values()
    if output_file and posixpath.basename(attached.path) != posixpath.basename(
        output_file
    ):
        return (
            f"attached file {attached.path} is not the wrapped output_file "
            f"{output_file}"
        )
    return mismatch(shape, sum(1 for o in originals if o.kind == "audio"))


def _locate(
    original: OriginalFile,
    gateway: Gateway,
    archive: CachedArchive,
    disk,
    file_id: Optional[str],
) -> Dict[str, Any]:
    entity_state = None
    if file_id is None:
        entity = gateway.find_file(fields.RUSHES_STORAGE, original.relative)
        file_id = entity.file_id if entity else None
        entity_state = entity.state if entity else None
    entry = archive.resolve(to_absolute(original.relative))
    return {
        "relative": original.relative,
        "kind": original.kind,
        "file_id": file_id,
        "entity_state": entity_state,
        "on_disk": disk.exists(original.relative),
        "entry": asdict(entry) if entry else None,
        "tapes": [asdict(archive.volume(v)) for v in entry.volumes] if entry else [],
    }


def plan_item(
    *,
    item_id: str,
    originals: Sequence[OriginalFile],
    spanned: bool,
    output_file: Optional[str],
    gateway: Gateway,
    archive: CachedArchive,
    disk,
) -> PlanResult:
    if spanned:
        return PlanResult(verdicts.SPANNED, "spanned P2 clip: a later slice")
    shapes = gateway.original_shapes(item_id)
    if len(shapes) != 1:
        return PlanResult(verdicts.UNEXPECTED, f"{len(shapes)} original shapes")
    (shape,) = shapes

    if _names_originals(shape, originals):
        by_path = {f.path: f.file_id for f in shape.files().values()}
        located = [
            _locate(o, gateway, archive, disk, by_path[o.relative]) for o in originals
        ]
        # For a complete plan, rollback records the current original shape
        # because the 2023 migration swapped files in place on the original shape,
        # and this is what apply will overwrite (metadata only).
        return PlanResult(
            verdicts.ALREADY_MIGRATED,
            plan={
                "kind": "complete",
                "new_shape_id": shape.shape_id,
                "originals": located,
            },
            rollback=_rollback(item_id, shape, gateway),
        )

    problem = _wrapped_problem(shape, originals, output_file)
    if problem:
        return PlanResult(verdicts.UNEXPECTED, problem)

    located = [_locate(o, gateway, archive, disk, None) for o in originals]
    missing = [o["relative"] for o in located if not o["on_disk"] and not o["entry"]]
    if missing:
        return PlanResult(
            verdicts.ORIGINALS_MISSING,
            "neither on disk nor in P5: " + ", ".join(missing),
        )
    for o in located:
        # A tape-only original is registered ARCHIVED; any other entity the
        # VX-41 index still holds for it (a shoot deleted from disk: LOST,
        # NOT_IMPORTED, CLOSED...) is stale and must not be reused.
        if not o["on_disk"] and o["file_id"] and o["entity_state"] != "ARCHIVED":
            return PlanResult(
                verdicts.UNEXPECTED,
                f"stale VX-41 entity {o['file_id']} ({o['entity_state']}) "
                f"for tape-only original {o['relative']}",
            )
    (wrapped_file,) = shape.files().values()
    return PlanResult(
        verdicts.READY,
        plan={
            "kind": "wrap",
            "wrapped_shape_id": shape.shape_id,
            "wrapped_shape": shape.to_document(),
            "wrapped_file": {
                "file_id": wrapped_file.file_id,
                "storage_id": wrapped_file.storage_id,
                "state": wrapped_file.state,
                "path": wrapped_file.path,
            },
            "originals": located,
        },
        rollback=_rollback(item_id, shape, gateway),
    )

"""Read-only: classify one item and write down exactly what apply will do.

Reads the item's REAL original shape (never ``output_file``, which is
not always what is attached), locates every original on disk, on VX-41
and in P5, and records what is needed to undo the migration.

An original shape whose technical description is a copy of the lowres
proxy is planned from its P2 format's template (``wrapped.templates``)
when there is one; the template and the clip's timing are stored in the
plan, so apply never depends on ``p2_templates.json``.
"""

import copy
import posixpath
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence

from portal.plugins.TapelessIngest.wrapped import fields, verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.gateway import Gateway, Shape, parse_shape
from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile, to_absolute
from portal.plugins.TapelessIngest.wrapped.shape import (
    ShapeMismatch,
    audio_time_base,
    build_document_from_template,
    mismatch,
)
from portal.plugins.TapelessIngest.wrapped.templates import (
    is_proxy_copy,
    template_key,
    timing,
)

_NO_METADATA: Mapping[str, Any] = MappingProxyType({})
PROXY_COPY = "proxy-copied technical description"
PROXY_COPY_INCOMPLETE = f"{PROXY_COPY}; P2 metadata incomplete for a template"
# durationSeconds is written with millisecond precision or better.
DURATION_TOLERANCE_S = 0.0005

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
    fields.CPAA_MIGRATION_FIELD,
)


@dataclass
class PlanResult:
    verdict: str
    reason: str = ""
    plan: Dict[str, Any] = field(default_factory=dict)
    rollback: Dict[str, Any] = field(default_factory=dict)


def _rollback(
    item_id: str,
    shape: Shape,
    gateway: Gateway,
    item_values: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if item_values is None:
        item_values = gateway.item_fields(item_id, ROLLBACK_ITEM_FIELDS)
    return {
        "wrapped_shape_id": shape.shape_id,
        "shape_files": [asdict(f) for f in shape.files().values()],
        "component_metadata": {
            c.component_id: gateway.component_metadata(
                item_id, shape.shape_id, c.component_id
            )
            for c in shape.components
        },
        "item_fields": item_values,
        "lowres_shape_ids": gateway.shape_ids(item_id, fields.LOWRES_TAG),
    }


def _names_originals(shape: Shape, originals: Sequence[OriginalFile]) -> bool:
    files = shape.files().values()
    return all(f.storage_id == fields.RUSHES_STORAGE for f in files) and {
        f.path for f in files
    } == {o.relative for o in originals}


def _attachment_problem(shape: Shape, output_file: Optional[str]) -> Optional[str]:
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
    return None


def _template_problem(template: Mapping[str, Any], audio_count: int) -> Optional[str]:
    """Checked at plan time so apply never finds a template it cannot
    build from (build_document_from_template keeps a backstop)."""
    if not isinstance(template.get("containerComponent"), Mapping):
        return "no containerComponent"
    videos = template.get("videoComponent") or []
    if len(videos) != 1:
        return f"{len(videos)} videoComponent(s), expected 1"
    if audio_count:
        audios = template.get("audioComponent")
        if not audios:
            return f"no audioComponent for {audio_count} audio original(s)"
        try:
            audio_time_base(audios[0])
        except ShapeMismatch as error:
            return str(error)
    return None


def _technical_source(
    shape: Shape,
    originals: Sequence[OriginalFile],
    clip_metadata: Mapping[str, Any],
    templates: Mapping[str, Any],
    item_values: Mapping[str, Any],
) -> Dict[str, Any]:
    """What the new shape's technical description is stated from, as the
    plan fields to add; ``{"problem": ...}`` when it cannot be stated."""
    audio_count = sum(1 for o in originals if o.kind == "audio")
    if not is_proxy_copy(shape):
        problem = mismatch(shape, audio_count)
        return {"problem": problem} if problem else {"technical_source": "wrapped"}
    marker = item_values.get(fields.CPAA_MIGRATION_FIELD) or [None]
    if marker[0] != fields.CPAA_MIGRATION_DONE:
        return {
            "problem": f"{PROXY_COPY} without the CPAA marker "
            f"({fields.CPAA_MIGRATION_FIELD})"
        }
    key = template_key(clip_metadata)
    if key is None:
        return {"problem": PROXY_COPY_INCOMPLETE}
    if key not in templates:
        return {"problem": f"{PROXY_COPY}; no template for {key}"}
    template = templates[key]["template"]
    if is_proxy_copy(parse_shape(template)):
        return {"problem": f"{PROXY_COPY}; template {key} is itself a proxy copy"}
    malformed = _template_problem(template, audio_count)
    if malformed:
        return {"problem": f"{PROXY_COPY}; template {key} is malformed: {malformed}"}
    try:
        clip_timing = timing(clip_metadata)
    except ValueError as error:
        return {"problem": f"{PROXY_COPY_INCOMPLETE}: {error}"}
    try:
        # A dry build: every duration this item needs is exact in the
        # template's time bases, so apply cannot fail on it after writes.
        build_document_from_template(template, "V", ["A"] * audio_count, clip_timing)
    except ShapeMismatch as error:
        return {
            "problem": f"{PROXY_COPY}; template {key} cannot state this item: "
            f"{error}"
        }
    return {
        "technical_source": f"template:{key}",
        "template": copy.deepcopy(template),
        "timing": asdict(clip_timing),
    }


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


def _duration_problem(
    item_values: Mapping[str, Any], clip_timing: Mapping[str, int]
) -> Optional[str]:
    """Before any write: the P2 duration must be the item's current
    durationSeconds (the proxy analysis), which apply leaves unchanged and
    verify re-checks. A disagreement means the ClipMetadata does not
    describe this item's essence."""
    values = item_values.get(fields.DURATION_FIELD)
    if not values:
        return f"{PROXY_COPY}; no durationSeconds to cross-check"
    current = values[0]
    p2 = clip_timing["frames"] * clip_timing["num"] / clip_timing["den"]
    try:
        agrees = abs(float(current) - p2) <= DURATION_TOLERANCE_S
    except ValueError:
        return f"{PROXY_COPY}; durationSeconds {current!r} is not a number"
    if not agrees:
        return f"{PROXY_COPY}; P2 duration {p2:.3f} s != durationSeconds {current}"
    return None


def plan_item(
    *,
    item_id: str,
    originals: Sequence[OriginalFile],
    spanned: bool,
    output_file: Optional[str],
    gateway: Gateway,
    archive: CachedArchive,
    disk,
    clip_metadata: Mapping[str, Any] = _NO_METADATA,
    templates: Mapping[str, Any] = _NO_METADATA,
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
                "technical_source": "existing",
                "new_shape_id": shape.shape_id,
                "originals": located,
            },
            rollback=_rollback(item_id, shape, gateway),
        )

    problem = _attachment_problem(shape, output_file)
    if problem:
        return PlanResult(verdicts.UNEXPECTED, problem)
    # One read, reused by rollback: it carries the CPAA marker and the
    # durationSeconds the template route is cross-checked against.
    item_values = gateway.item_fields(item_id, ROLLBACK_ITEM_FIELDS)
    technical = _technical_source(
        shape, originals, clip_metadata, templates, item_values
    )
    if "problem" in technical:
        return PlanResult(verdicts.UNEXPECTED, technical["problem"])
    if "timing" in technical:
        problem = _duration_problem(item_values, technical["timing"])
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
            **technical,
        },
        rollback=_rollback(item_id, shape, gateway, item_values),
    )

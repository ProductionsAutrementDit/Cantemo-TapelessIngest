"""Read-only: classify one item and write down exactly what apply will do.

Reads the item's REAL original shape (never ``output_file``, which is
not always what is attached), locates every original on disk, on VX-41
and in P5, and records what is needed to undo the migration.

An original shape whose technical description is a copy of the lowres
proxy, or that Vidispine never analysed (``binaryComponent`` only), is
planned from its P2 format's template (``wrapped.templates``) when there
is one; the template and the clip's timing are stored in the plan, so
apply never depends on ``p2_templates.json``.

Measured attachment shapes besides the usual one wrapped file: the
wrapped MXF attached once per online legacy storage (``wrapped_files``
in the plan), and a genuine shape naming no file at all (neither key;
its container duration is cross-checked against durationSeconds instead).
"""

import copy
import posixpath
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence

from portal.plugins.TapelessIngest.wrapped import fields, verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.gateway import (
    Gateway,
    Shape,
    ShapeFile,
    parse_shape,
)
from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile, to_absolute
from portal.plugins.TapelessIngest.wrapped.shape import (
    ShapeMismatch,
    audio_time_base,
    build_document_from_template,
    duration_seconds,
    mismatch,
)
from portal.plugins.TapelessIngest.wrapped.templates import (
    is_proxy_copy,
    template_key_with_source,
    timing,
)

_NO_METADATA: Mapping[str, Any] = MappingProxyType({})
PROXY_COPY = "proxy-copied technical description"
BINARY_ONLY = "binary-only original shape"
FILELESS = "fileless original shape"
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
    # Measured on prod (M10), 2026-09-29: also rewritten by
    # shape/create?updateItemMetadata=true, so a manual undo can restore them.
    fields.ITEM_ORIGINAL_FILENAME_FIELD,
    fields.ITEM_ORIGINAL_FORMAT_FIELD,
    fields.ITEM_ORIGINAL_VIDEO_CODEC_FIELD,
    fields.ITEM_ORIGINAL_AUDIO_CODEC_FIELD,
    fields.ITEM_ORIGINAL_WIDTH_FIELD,
    fields.ITEM_ORIGINAL_HEIGHT_FIELD,
    fields.ITEM_MIME_TYPE_FIELD,
    fields.ITEM_MEDIA_TYPE_FIELD,
    fields.ITEM_DURATION_TIMECODE_FIELD,
    fields.ITEM_START_TIMECODE_FIELD,
    fields.ITEM_START_SECONDS_FIELD,
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


def _legacy_copies(files: Sequence[ShapeFile], output_file: Optional[str]) -> bool:
    """Measured (182 items): one wrapped MXF attached once per online
    legacy storage, every copy named like ``output_file``."""
    if not output_file:
        return False
    name = posixpath.basename(output_file)
    storages = [f.storage_id for f in files]
    return (
        all(posixpath.basename(f.path) == name for f in files)
        and all(s in fields.ONLINE_LEGACY_STORAGES for s in storages)
        and len(set(storages)) == len(storages)
    )


def _attachment_problem(shape: Shape, output_file: Optional[str]) -> Optional[str]:
    files = shape.files()
    # A fileless shape is checked by _technical_source: nothing ties it to
    # this clip but its content, so its duration is cross-checked.
    if not files:
        return None
    if len(files) > 1:
        if _legacy_copies(list(files.values()), output_file):
            return None
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


def _is_binary_only(shape: Shape) -> bool:
    return bool(shape.components) and all(c.kind == "binary" for c in shape.components)


def _from_template(
    label: str,
    audio_count: int,
    clip_metadata: Mapping[str, Any],
    templates: Mapping[str, Any],
    item_values: Mapping[str, Any],
) -> Dict[str, Any]:
    """The template route, for a shape whose own technical description
    cannot be restated; ``label`` says why and prefixes every problem."""
    marker = item_values.get(fields.CPAA_MIGRATION_FIELD) or [None]
    if marker[0] != fields.CPAA_MIGRATION_DONE:
        return {
            "problem": f"{label} without the CPAA marker "
            f"({fields.CPAA_MIGRATION_FIELD})"
        }
    incomplete = f"{label}; P2 metadata incomplete for a template"
    key, inferred = template_key_with_source(clip_metadata)
    if key is None:
        return {"problem": incomplete}
    if key not in templates:
        return {"problem": f"{label}; no template for {key}"}
    template = templates[key]["template"]
    if is_proxy_copy(parse_shape(template)):
        return {"problem": f"{label}; template {key} is itself a proxy copy"}
    malformed = _template_problem(template, audio_count)
    if malformed:
        return {"problem": f"{label}; template {key} is malformed: {malformed}"}
    try:
        clip_timing = timing(clip_metadata)
    except ValueError as error:
        return {"problem": f"{incomplete}: {error}"}
    try:
        # A dry build: every duration this item needs is exact in the
        # template's time bases, so apply cannot fail on it after writes.
        build_document_from_template(template, "V", ["A"] * audio_count, clip_timing)
    except ShapeMismatch as error:
        return {"problem": f"{label}; template {key} cannot state this item: {error}"}
    problem = _duration_problem(
        label,
        item_values,
        "P2 duration",
        clip_timing.frames * clip_timing.num / clip_timing.den,
    )
    if problem:
        return {"problem": problem}
    result = {
        "technical_source": f"template:{key}",
        "template": copy.deepcopy(template),
        "timing": asdict(clip_timing),
    }
    if inferred:
        result["audio_bits_inferred"] = True
    return result


def _fileless_problem(
    shape: Shape, audio_count: int, item_values: Mapping[str, Any]
) -> Optional[str]:
    """A genuine shape that names no file is restated only when its own
    container duration is this item's durationSeconds: no file name ties
    it to this clip."""
    if is_proxy_copy(shape):
        return f"{FILELESS}: {PROXY_COPY}"
    if _is_binary_only(shape):
        return f"{FILELESS}: binary-only"
    problem = mismatch(shape, audio_count)
    if problem:
        return problem
    (container,) = shape.of_kind("container")
    seconds = duration_seconds(container.body.get("duration"))
    if seconds is None:
        return f"{FILELESS}; no container duration"
    return _duration_problem(
        FILELESS, item_values, "container duration", float(seconds)
    )


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
    if not shape.files():
        problem = _fileless_problem(shape, audio_count, item_values)
        return {"problem": problem} if problem else {"technical_source": "wrapped"}
    if is_proxy_copy(shape):
        label = PROXY_COPY
    elif _is_binary_only(shape):
        label = BINARY_ONLY
    else:
        problem = mismatch(shape, audio_count)
        return {"problem": problem} if problem else {"technical_source": "wrapped"}
    return _from_template(label, audio_count, clip_metadata, templates, item_values)


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
    label: str, item_values: Mapping[str, Any], name: str, seconds: float
) -> Optional[str]:
    """Before any write: the duration the new shape is stated from (``name``,
    ``seconds``) must be the item's current durationSeconds (the proxy
    analysis), which apply leaves unchanged and verify re-checks. A
    disagreement means that source does not describe this item's essence."""
    values = item_values.get(fields.DURATION_FIELD)
    if not values:
        return f"{label}; no durationSeconds to cross-check"
    current = values[0]
    try:
        agrees = abs(float(current) - seconds) <= DURATION_TOLERANCE_S
    except ValueError:
        return f"{label}; durationSeconds {current!r} is not a number"
    if not agrees:
        return f"{label}; {name} {seconds:.3f} s != durationSeconds {current}"
    return None


def _wrapped_files(shape: Shape) -> Dict[str, Any]:
    """The plan's record of the wrapped file(s) _finish keeps or deletes:
    ``wrapped_file`` for one (the format every earlier row has),
    ``wrapped_files`` ordered by storage for several, nothing for none."""
    files = [
        {
            "file_id": f.file_id,
            "storage_id": f.storage_id,
            "state": f.state,
            "path": f.path,
        }
        for f in sorted(shape.files().values(), key=lambda f: f.storage_id)
    ]
    if len(files) == 1:
        return {"wrapped_file": files[0]}
    return {"wrapped_files": files} if files else {}


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
    # durationSeconds the template and fileless routes are cross-checked
    # against.
    item_values = gateway.item_fields(item_id, ROLLBACK_ITEM_FIELDS)
    technical = _technical_source(
        shape, originals, clip_metadata, templates, item_values
    )
    if "problem" in technical:
        return PlanResult(verdicts.UNEXPECTED, technical["problem"])

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
    return PlanResult(
        verdicts.READY,
        plan={
            "kind": "wrap",
            "wrapped_shape_id": shape.shape_id,
            "wrapped_shape": shape.to_document(),
            **_wrapped_files(shape),
            "originals": located,
            **technical,
        },
        rollback=_rollback(item_id, shape, gateway, item_values),
    )

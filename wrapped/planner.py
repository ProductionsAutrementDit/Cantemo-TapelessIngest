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

A spanned take (``span``, master first) is planned as ONE original shape
naming every segment's files, plus the pad-assembly manifest pad_forge
reads; the segments must sum exactly to the wrapped duration.

A wrapped ``file`` item (``plan_file_item``) has ONE original, of which
the wrapped file is a byte-for-byte copy: a genuine original shape is
restated whole (``technical_source`` "copy") once the sizes prove the
copy. A proxy-copied or ambiguous description is stated from the
original's ffprobe instead (``technical_source`` "ffprobe"), for the
formats measured against Vidispine; without an ffprobe it stays
``unexpected``. A wrapped ``xdcam`` item has no ffprobe: its proxy-copied
or ambiguous description is stated from the Sony NRT XML instead
(``technical_source`` "nrt", ``wrapped.nrt``), and P5's size alone may
prove its tape-only original.
"""

import copy
import json
import posixpath
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from portal.plugins.TapelessIngest.wrapped import fields, verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.ffprobe import (
    AMBIGUOUS,
    FFPROBE_ROUTE_FORMATS,
    NO_SIGNATURE,
    PROXY,
    Signature,
    classify_copy,
    probe_disagreement,
    route_format,
    shape_signature,
)
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
    build_copy_document,
    build_document_from_template,
    build_ffprobe_document,
    build_span_document,
    duration_seconds,
    mismatch,
)
from portal.plugins.TapelessIngest.wrapped.span import Segment
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
# The ffprobe route compares two analyses of one file (prod 2026-10-03: within
# 0.12 s on all 9,196 targets; mpegts ffprobe is up to one frame longer).
FFPROBE_DURATION_TOLERANCE_S = 0.2

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
    # Written for spanned takes only; empty on every item before.
    fields.PAD_ASSEMBLY_FIELD,
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
    span: Optional[Sequence[Segment]] = None,
) -> Dict[str, Any]:
    """The template route, for a shape whose own technical description
    cannot be restated; ``label`` says why and prefixes every problem.
    For a ``span``, the key and start timecode are the master's, the
    timing is the whole take's."""
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
    if span:
        edit_unit = (clip_timing.num, clip_timing.den)
        if edit_unit != span[0].edit_unit:
            return {
                "problem": f"{label}; P2 EditUnit {clip_timing.num}/"
                f"{clip_timing.den} is not the take's "
                f"{span[0].edit_unit[0]}/{span[0].edit_unit[1]}"
            }
        clip_timing = replace(clip_timing, frames=sum(s.frames for s in span))
    try:
        # A dry build: every duration this item needs is exact in the
        # template's time bases, so apply cannot fail on it after writes.
        if span:
            build_span_document(
                _segments(span),
                ["V"] * len(span),
                [["A"] * audio_count] * len(span),
                template=template,
                timing=clip_timing,
            )
        else:
            build_document_from_template(
                template, "V", ["A"] * audio_count, clip_timing
            )
    except ShapeMismatch as error:
        return {"problem": f"{label}; template {key} cannot state this item: {error}"}
    if span:
        # The item's durationSeconds is what stands for the wrapped
        # duration when the wrapped shape's own is a proxy's.
        problem = _span_duration_problem(
            item_values, sum(s.seconds for s in span), "wrapped"
        )
    else:
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


def _segments(span: Sequence[Segment]) -> List[Dict[str, Any]]:
    return [
        {
            "name": s.name,
            "frames": s.frames,
            "num": s.edit_unit[0],
            "den": s.edit_unit[1],
        }
        for s in span
    ]


def _manifest(span: Sequence[Segment]) -> str:
    """The pad-assembly/1 document pad_forge's ``manifest.from_document``
    parses, serialised once here: apply writes this string verbatim and
    verify compares it exactly."""
    return json.dumps(
        {
            "schema": "pad-assembly/1",
            "clips": [
                {
                    "video": to_absolute(s.video.relative),
                    "audio": [to_absolute(a.relative) for a in s.audios],
                }
                for s in span
            ],
            "reel_audio": [],
        },
        sort_keys=False,
    )


def span_originals(span: Sequence[Segment]) -> List[OriginalFile]:
    """Every file of the take, segment by segment: video, then audios."""
    return [f for s in span for f in (s.video, *s.audios)]


def _span_duration_problem(
    item_values: Mapping[str, Any], total: Fraction, name: str
) -> Optional[str]:
    """Before any write, for every take: its segments must sum to the
    item's durationSeconds (the proxy analysis of the whole take), which
    apply leaves unchanged and verify re-checks. ``name`` is what that
    value stands for in the refusal."""
    values = item_values.get(fields.DURATION_FIELD)
    if not values:
        return "spanned take: no durationSeconds to cross-check"
    try:
        current = float(values[0])
    except ValueError:
        return f"spanned take: durationSeconds {values[0]!r} is not a number"
    return _span_sum_problem(total, current, name)


def _span_sum_problem(total: Fraction, wrapped: float, name: str) -> Optional[str]:
    if abs(float(total) - wrapped) <= DURATION_TOLERANCE_S:
        return None
    return (
        f"spanned take: segments sum to {float(total):.3f} s, "
        f"{name} is {wrapped:.3f} s"
    )


def _span_technical_source(
    shape: Shape,
    span: Sequence[Segment],
    clip_metadata: Mapping[str, Any],
    templates: Mapping[str, Any],
    item_values: Mapping[str, Any],
) -> Dict[str, Any]:
    """As ``_technical_source``, for a take stated as one multi-segment
    shape. Unconditional for every take: the segments must sum to the
    wrapped duration, which also catches a chain whose head no id proves."""
    audio_count = len(span[0].audios)
    for segment in span[1:]:
        if len(segment.audios) != audio_count:
            return {
                "problem": f"spanned take: segment {segment.name} has "
                f"{len(segment.audios)} audio file(s), the master {audio_count}"
            }
    if not shape.files():
        problem = _fileless_problem(shape, audio_count, item_values)
        if problem:
            return {"problem": problem}
    elif is_proxy_copy(shape):
        return _from_template(
            PROXY_COPY, audio_count, clip_metadata, templates, item_values, span
        )
    elif _is_binary_only(shape):
        return _from_template(
            BINARY_ONLY, audio_count, clip_metadata, templates, item_values, span
        )
    problem = mismatch(shape, audio_count)
    if problem:
        return {"problem": problem}
    (container,) = shape.of_kind("container")
    seconds = duration_seconds(container.body.get("duration"))
    if seconds is None:
        return {"problem": "spanned take: wrapped container has no duration"}
    total = sum(s.seconds for s in span)
    problem = _span_sum_problem(
        total, float(seconds), "wrapped"
    ) or _span_duration_problem(item_values, total, "durationSeconds")
    if problem:
        return {"problem": problem}
    try:
        build_span_document(
            _segments(span),
            ["V"] * len(span),
            [["A"] * audio_count] * len(span),
            wrapped=shape,
        )
    except ShapeMismatch as error:
        return {"problem": f"spanned take: {error}"}
    return {"technical_source": "wrapped"}


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
    label: str,
    item_values: Mapping[str, Any],
    name: str,
    seconds: float,
    tolerance: float = DURATION_TOLERANCE_S,
    separator: str = "; ",
) -> Optional[str]:
    """Before any write: the duration the new shape is stated from (``name``,
    ``seconds``) must be the item's current durationSeconds (the proxy
    analysis), which apply leaves unchanged and verify re-checks. A
    disagreement means that source does not describe this item's essence."""
    values = item_values.get(fields.DURATION_FIELD)
    if not values:
        return f"{label}{separator}no durationSeconds to cross-check"
    current = values[0]
    try:
        agrees = abs(float(current) - seconds) <= tolerance
    except ValueError:
        return f"{label}{separator}durationSeconds {current!r} is not a number"
    if not agrees:
        return f"{label}{separator}{name} {seconds:.3f} s != durationSeconds {current}"
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


def _complete(
    item_id: str,
    shape: Shape,
    originals: Sequence[OriginalFile],
    gateway: Gateway,
    archive: CachedArchive,
    disk,
    extra: Optional[Mapping[str, Any]] = None,
) -> PlanResult:
    """The shape already names every original on VX-41: only metadata is
    left to write (``extra`` adds plan keys, e.g. the provider)."""
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
            **(extra or {}),
        },
        rollback=_rollback(item_id, shape, gateway),
    )


def _located_problem(located: Sequence[Mapping[str, Any]]) -> Optional[PlanResult]:
    """Before any write: every original is on disk or in P5, and a
    tape-only one is not bound to a stale VX-41 entity."""
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
    span: Optional[Sequence[Segment]] = None,
    span_problem: Optional[str] = None,
) -> PlanResult:
    if spanned and span is None:
        return PlanResult(
            verdicts.SPANNED, span_problem or "spanned P2 clip: a later slice"
        )
    if span is not None:
        # One source of truth: the take's files come from the span alone.
        if originals:
            raise ValueError("originals are derived from span; pass none")
        originals = span_originals(span)
    shapes = gateway.original_shapes(item_id)
    if len(shapes) != 1:
        return PlanResult(verdicts.UNEXPECTED, f"{len(shapes)} original shapes")
    (shape,) = shapes

    if _names_originals(shape, originals):
        return _complete(item_id, shape, originals, gateway, archive, disk)

    problem = _attachment_problem(shape, output_file)
    if problem:
        return PlanResult(verdicts.UNEXPECTED, problem)
    # One read, reused by rollback: it carries the CPAA marker and the
    # durationSeconds the template and fileless routes are cross-checked
    # against.
    item_values = gateway.item_fields(item_id, ROLLBACK_ITEM_FIELDS)
    if span is None:
        technical = _technical_source(
            shape, originals, clip_metadata, templates, item_values
        )
    else:
        technical = _span_technical_source(
            shape, span, clip_metadata, templates, item_values
        )
    if "problem" in technical:
        return PlanResult(verdicts.UNEXPECTED, technical["problem"])

    located = [_locate(o, gateway, archive, disk, None) for o in originals]
    layout: Dict[str, Any] = {}
    if span is not None:
        index = [n for n, s in enumerate(span) for _ in (s.video, *s.audios)]
        for original, segment in zip(located, index):
            original["segment"] = segment
        layout = {"segments": _segments(span), "manifest": _manifest(span)}
    refused = _located_problem(located)
    if refused:
        return refused
    return PlanResult(
        verdicts.READY,
        plan={
            "kind": "wrap",
            "wrapped_shape_id": shape.shape_id,
            "wrapped_shape": shape.to_document(),
            **_wrapped_files(shape),
            "originals": located,
            **technical,
            **layout,
        },
        rollback=_rollback(item_id, shape, gateway, item_values),
    )


FILE = "file"
XDCAM = "xdcam"
# Measured on prod 2026-10-01: P5's inventory size is the true size plus a
# small overhead (+178 for 7,720 items, +190/275/276 for ~900).
P5_OVERHEAD_MAX = 512
NO_SIZE_PROOF = "no size proof"
ROUTE = "ffprobe route"
NRT_ROUTE = "NRT route"


def size_problem(
    exact: Mapping[str, Optional[int]],
    p5: Optional[int],
    label: str = FILE,
    p5_alone: bool = False,
) -> Optional[str]:
    """Byte identity of a wrapped ``file`` copy and its original, before
    any write. ``exact`` maps each source (``ffprobe``, ``wrapped <id>``,
    ``disk``) to its exact size, None when unknown; ``p5`` is P5's size of
    the original, None when unknown. Every known exact size must agree,
    and P5's, whenever known, must exceed it by less than P5_OVERHEAD_MAX.
    Two exact sources (all wrapped copies count as one), or one and P5's
    size, are a proof. With ``p5_alone`` (the NRT route only, accepted on
    2026-10-07 for tape-only xdcam originals whose wrapped copy is gone),
    P5's size is a proof when no exact size is known."""
    known = {label: size for label, size in exact.items() if size is not None}
    if len(set(known.values())) > 1:
        return "copy and original differ in size: " + ", ".join(
            f"{label} {size}" for label, size in known.items()
        )
    if not known:
        if p5_alone and p5 is not None:
            return None
        return f"{NO_SIZE_PROOF} ({label}): no exact size known"
    size = next(iter(known.values()))
    if p5 is not None and not 0 <= p5 - size < P5_OVERHEAD_MAX:
        return (
            f"P5 size {p5} is not the copy's ({label}): {p5 - size:+d} bytes from "
            f"{', '.join(known)} {size}, outside [0, {P5_OVERHEAD_MAX})"
        )
    if len({label.split(" ", 1)[0] for label in known}) >= 2 or p5 is not None:
        return None
    return f"{NO_SIZE_PROOF} ({label}): only {', '.join(known)} {size}, no P5 size"


def _p5_size(entry: Optional[Mapping[str, Any]]) -> Optional[int]:
    size = entry.get("size") if entry else None
    return size if isinstance(size, int) and size >= 0 else None


def _shared_entity(
    item_id: str,
    located: Sequence[Mapping[str, Any]],
    gateway: Gateway,
    label: str = FILE,
) -> Optional[str]:
    """A reused VX-41 entity must not already be another item's: two items
    would then share one original's entity (file route only)."""
    for original in located:
        if not original["file_id"]:
            continue
        others = [i for i in gateway.file_items(original["file_id"]) if i != item_id]
        if others:
            return (
                f"VX-41 entity {original['file_id']} already belongs to item "
                f"{', '.join(others)} ({label})"
            )
    return None


def _file_original(shape: Shape) -> Optional[OriginalFile]:
    """No ClipFile: the original is the shape's one file when it is on
    VX-41 (measured: 1,788 items)."""
    files = list(shape.files().values())
    if len(files) != 1 or files[0].storage_id != fields.RUSHES_STORAGE:
        return None
    kind = "video" if shape.of_kind("video") else "audio"
    return OriginalFile(files[0].path, kind)


def _copy_problem(
    item_id: str,
    shape: Shape,
    gateway: Gateway,
    ffprobe: Optional[Signature],
    described: bool = False,
    label: str = FILE,
) -> Tuple[Optional[str], bool]:
    """(problem, via_ffprobe): whether the shape's own description is the
    original's (option B). A proxy-copied or ambiguous one is no problem
    when the original's ffprobe is ``described`` in full: the shape is then
    stated from it (``via_ffprobe``)."""
    if _is_binary_only(shape):
        return f"{BINARY_ONLY} ({label})", False
    try:
        build_copy_document(shape, "")
    except ShapeMismatch as error:
        return f"original shape ({label}) cannot be restated: {error}", False
    lowres = [
        shape_signature(s) for s in gateway.tagged_shapes(item_id, fields.LOWRES_TAG)
    ]
    signature = shape_signature(shape)
    verdict = classify_copy(signature, lowres, ffprobe)
    if verdict in (PROXY, AMBIGUOUS) and described:
        return None, True
    if verdict == PROXY:
        if label != FILE:
            return f"{PROXY_COPY} ({label}); no ffprobe for this provider", False
        return f"{PROXY_COPY} ({label}); ffprobe route pending", False
    if verdict == AMBIGUOUS:
        return f"original shape equals the lowres ({label}); ambiguous", False
    if ffprobe is None and all(s == NO_SIGNATURE for s in lowres):
        # Nothing could have refuted a proxy copy.
        return f"no ffprobe and no lowres to tell a proxy copy ({label})", False
    # Unlike the lowres is not enough: when the original's ffprobe is known,
    # the description must also agree with it.
    disagreement = probe_disagreement(signature, ffprobe) if ffprobe else None
    if disagreement:
        return f"original shape disagrees with ffprobe ({label}): {disagreement}", False
    return None, False


def _route_problem(
    description: Mapping[str, Any],
    item_values: Mapping[str, Any],
    label: str = FILE,
) -> Optional[str]:
    """Before any write: the ffprobe is complete, of a format measured
    against Vidispine, and of the duration the item already has."""
    if description.get("problem"):
        return f"{ROUTE} ({label}): {description['problem']}"
    key = route_format(description)
    if key not in FFPROBE_ROUTE_FORMATS:
        return f"{ROUTE} ({label}): format {key} has no Vidispine reference"
    try:
        seconds = float(Fraction(description["duration"]))
    except (ValueError, ZeroDivisionError):
        return (
            f"{ROUTE} ({label}): duration {description['duration']!r} is not a number"
        )
    return _duration_problem(
        f"{ROUTE} ({label})",
        item_values,
        "ffprobe duration",
        seconds,
        FFPROBE_DURATION_TOLERANCE_S,
        separator=": ",
    )


def _stated(
    source: str,
    description: Dict[str, Any],
    item_values: Mapping[str, Any],
    label: str,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """(problem, technical plan keys) of a shape stated from ``description``
    (``source`` "ffprobe" or "nrt"). The container states the duration the
    item already has: apply and verify leave durationSeconds alone. A dry
    build, so apply cannot fail on it after writes."""
    microseconds = round(Fraction(item_values[fields.DURATION_FIELD][0]) * 1_000_000)
    try:
        build_ffprobe_document(description, "", microseconds)
    except ShapeMismatch as error:
        return f"{label}: {error}", {}
    return None, {
        "technical_source": source,
        source: description,
        "container_microseconds": microseconds,
    }


def _nrt_route_problem(
    description: Mapping[str, Any], item_values: Mapping[str, Any], label: str
) -> Optional[str]:
    """Before any write: the NRT's duration is the one the item already has."""
    return _duration_problem(
        label,
        item_values,
        "NRT duration",
        float(Fraction(description["duration"])),
        FFPROBE_DURATION_TOLERANCE_S,
        separator=": ",
    )


def plan_file_item(
    *,
    item_id: str,
    original: Optional[OriginalFile],
    output_file: Optional[str],
    gateway: Gateway,
    archive: CachedArchive,
    disk,
    ffprobe: Optional[Signature] = None,
    ffprobe_size: Optional[int] = None,
    ffprobe_description: Optional[Dict[str, Any]] = None,
    provider: str = FILE,
    nrt: Optional[Tuple[Optional[Dict[str, Any]], Optional[str]]] = None,
) -> PlanResult:
    """A wrapped ``file`` item (or ``xdcam``, the same copy route labelled
    ``provider`` and never given an ffprobe): its wrapped file is a byte-for-byte copy
    of ONE original, so a genuine original shape is restated whole onto
    it (``technical_source`` "copy"); a proxy-copied one is stated from the
    original's ffprobe (``technical_source`` "ffprobe", which stores the
    description and the container duration). ``original`` is the clip's
    ClipFile (None when it has none); ``ffprobe``/``ffprobe_size``/
    ``ffprobe_description`` come from the ffprobe XML stored in
    ``Clip.clip_xml``. ``nrt`` is an ``xdcam`` clip's (description, problem)
    from its NRT XML (``nrt.nrt_description``): a proxy-copied or ambiguous
    shape is then stated from it (``technical_source`` "nrt"), or refused
    with its problem."""
    shapes = gateway.original_shapes(item_id)
    if len(shapes) != 1:
        return PlanResult(verdicts.UNEXPECTED, f"{len(shapes)} original shapes")
    (shape,) = shapes
    provider_key = {"provider": provider}

    if original is None:
        original = _file_original(shape)
        if original is None:
            return PlanResult(
                verdicts.UNEXPECTED,
                "no ClipFile and the original shape does not name one VX-41 file",
            )
    if _names_originals(shape, [original]):
        result = _complete(
            item_id, shape, [original], gateway, archive, disk, provider_key
        )
        shared = _shared_entity(item_id, result.plan["originals"], gateway, provider)
        return PlanResult(verdicts.UNEXPECTED, shared) if shared else result

    if not shape.files():
        return PlanResult(
            verdicts.UNEXPECTED,
            f"{FILELESS} ({provider}): not measured for this provider",
        )
    problem = _attachment_problem(shape, output_file)
    via_nrt = provider == XDCAM and nrt is not None
    restated = False
    if not problem:
        problem, restated = _copy_problem(
            item_id,
            shape,
            gateway,
            ffprobe,
            ffprobe_description is not None or via_nrt,
            provider,
        )
    if problem:
        return PlanResult(verdicts.UNEXPECTED, problem)

    item_values = gateway.item_fields(item_id, ROLLBACK_ITEM_FIELDS)
    technical: Dict[str, Any] = {"technical_source": "copy"}
    if restated and via_nrt:
        description, problem = nrt
        if problem or description is None:
            return PlanResult(
                verdicts.UNEXPECTED, f"{PROXY_COPY} ({provider}); {problem}"
            )
        label = f"{NRT_ROUTE} ({provider})"
        problem = _nrt_route_problem(description, item_values, label)
        if not problem:
            problem, technical = _stated("nrt", description, item_values, label)
        if problem:
            return PlanResult(verdicts.UNEXPECTED, problem)
    elif restated:
        problem = _route_problem(ffprobe_description, item_values, provider)
        if not problem:
            problem, technical = _stated(
                "ffprobe", ffprobe_description, item_values, f"{ROUTE} ({provider})"
            )
        if problem:
            return PlanResult(verdicts.UNEXPECTED, problem)
    # Read-only, and the one P5 lookup: the size proof reuses its entry.
    located = [_locate(original, gateway, archive, disk, None)]
    (found,) = located
    exact: Dict[str, Optional[int]] = {"ffprobe": ffprobe_size}
    for wrapped in sorted(shape.files().values(), key=lambda f: f.storage_id):
        exact[f"wrapped {wrapped.file_id}"] = gateway.file_size(wrapped.file_id)
    if found["on_disk"]:
        exact["disk"] = disk.size(original.relative)
    p5 = _p5_size(found["entry"])
    p5_alone = technical["technical_source"] == "nrt"
    problem = size_problem(exact, p5, provider, p5_alone)
    if problem:
        return PlanResult(verdicts.UNEXPECTED, problem)
    known = {label: size for label, size in exact.items() if size is not None}
    size_proof: Dict[str, Any] = {"exact": known, "p5": p5}
    if not known:
        # Only reachable through p5_alone: P5's size is the whole proof.
        size_proof["p5_alone"] = True
    refused = _located_problem(located)
    if refused:
        return refused
    shared = _shared_entity(item_id, located, gateway, provider)
    if shared:
        return PlanResult(verdicts.UNEXPECTED, shared)
    return PlanResult(
        verdicts.READY,
        plan={
            "kind": "wrap",
            **provider_key,
            "wrapped_shape_id": shape.shape_id,
            "wrapped_shape": shape.to_document(),
            **_wrapped_files(shape),
            "originals": located,
            **technical,
            "size_proof": size_proof,
        },
        rollback=_rollback(item_id, shape, gateway, item_values),
    )

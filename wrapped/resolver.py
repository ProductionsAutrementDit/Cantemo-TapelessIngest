"""Which clips are in scope, and what their original files are.

P2: the originals are the clip's ClipFile rows, recorded at wrapping
time: one video MXF and N audio MXFs. ``order`` is 0 on every measured
row, so the audio order comes from the file names (``…00.MXF``,
``…01.MXF``…), which is the channel order P2 writes.

``file``: exactly one original (video, audio or still), one ClipFile row
(21,285 clips) or none at all (1,819 clips, measured 2026-10-01).
"""

from typing import Dict, List, Optional

from django.db.models import Q, QuerySet

from portal.plugins.TapelessIngest.models.clip import (
    Clip,
    ClipFile,
    ClipMetadata,
    SpannedClips,
)
from portal.plugins.TapelessIngest.wrapped.paths import (
    OriginalFile,
    UnknownPrefix,
    to_relative,
)
from portal.plugins.TapelessIngest.wrapped.span import (
    MasterIds,
    RowIds,
    Segment,
    SpanUnresolved,
    chain_from_rows,
    chain_from_xml,
    parse_edit_unit,
    parse_frames,
    prove_rows,
    require_head,
)

P2_PROVIDER = "panasonicP2"
FILE_PROVIDER = "file"
XDCAM_PROVIDER = "xdcam"


class ResolveError(Exception):
    """The clip's ClipFile rows do not describe one video plus N audio."""


def wrapped_p2_clips(
    item_id: Optional[str] = None, collection_id: Optional[str] = None
) -> QuerySet:
    return wrapped_clips(P2_PROVIDER, item_id, collection_id)


def wrapped_clips(
    provider: str, item_id: Optional[str] = None, collection_id: Optional[str] = None
) -> QuerySet:
    clips = (
        Clip.objects.filter(provider_name=provider)
        .exclude(Q(output_file__isnull=True) | Q(output_file=""))
        .exclude(Q(item_id__isnull=True) | Q(item_id=""))
    )
    if item_id:
        clips = clips.filter(item_id=item_id)
    if collection_id:
        clips = clips.filter(collection_id=collection_id)
    return clips.order_by("item_id")


def resolve_p2(clip: Clip) -> List[OriginalFile]:
    rows = list(ClipFile.objects.filter(clip=clip))
    strays = sorted({r.filetype for r in rows} - {"video", "audio"})
    if strays:
        raise ResolveError(f"ClipFile rows of unknown type {strays}")
    videos = [r for r in rows if r.filetype == "video"]
    if len(videos) != 1:
        raise ResolveError(f"{len(videos)} video ClipFile row(s), expected 1 video")
    audios = sorted((r for r in rows if r.filetype == "audio"), key=lambda r: r.path)
    try:
        return [OriginalFile(to_relative(videos[0].path), "video")] + [
            OriginalFile(to_relative(r.path), "audio") for r in audios
        ]
    except UnknownPrefix as error:
        raise ResolveError(str(error)) from error


def resolve_file(clip: Clip) -> Optional[OriginalFile]:
    """The ``file`` clip's one original, or None when it has no ClipFile
    (the original shape may then name it on VX-41)."""
    rows = list(ClipFile.objects.filter(clip=clip))
    if not rows:
        return None
    if len(rows) > 1:
        raise ResolveError(f"{len(rows)} ClipFile rows, expected 1 for a file clip")
    (row,) = rows
    if row.filetype not in ("video", "audio"):
        raise ResolveError(f"ClipFile row of unknown type {row.filetype!r}")
    try:
        return OriginalFile(to_relative(row.path), row.filetype)
    except UnknownPrefix as error:
        raise ResolveError(str(error)) from error


def resolve_span(clip: Clip, disk) -> List[Segment]:
    """The spanned take mastered by ``clip``, master first.

    The legacy ``SpannedClips`` rows win when they list a segment besides
    the master's own self row; otherwise the chain is walked through the
    P2 CLIP XMLs in the master's CONTENTS folder, read through ``disk``.
    """
    metadata = _metadata(clip)
    master = _segment(clip, metadata)
    ids = MasterIds(
        global_id=clip.umid,
        top_id=_relation(metadata, "Relation_Top_GlobalClipID"),
        next_name=_relation(metadata, "Relation_Next_ClipName"),
        next_id=_relation(metadata, "Relation_Next_GlobalClipID"),
        previous_id=_relation(metadata, "Relation_Previous_GlobalClipID"),
    )
    rows = (
        SpannedClips.objects.filter(master_clip=clip)
        .exclude(clip=clip)
        .select_related("clip")
        .order_by("order")
    )
    if rows:
        require_head(ids)
        segments, proofs = [], []
        for row in rows:
            row_metadata = _metadata(row.clip)
            segment = _segment(row.clip, row_metadata)
            segments.append(segment)
            proofs.append(_row_ids(row, segment, row_metadata))
        prove_rows(ids, master.frames, proofs)
        return chain_from_rows(master, segments)
    head, marker, _ = master.video.relative.rpartition("/VIDEO/")
    if not marker:
        raise SpanUnresolved(f"master video {master.video.relative} not in VIDEO/")
    return chain_from_xml(master, ids, head, _xml_reader(disk))


def _metadata(clip: Clip) -> Dict[str, str]:
    # One value per name: (clip, name) is unique since migration 0001.
    return dict(ClipMetadata.objects.filter(clip=clip).values_list("name", "value"))


def _relation(metadata: Dict[str, str], name: str) -> Optional[str]:
    # panasonicP2 stores getValueFromPath's False for an absent element as
    # the string "False": the plugin's own absent-value sentinel.
    value = (metadata.get(name) or "").strip()
    return None if value in ("", "False") else value


def _row_ids(row: SpannedClips, segment: Segment, metadata: Dict[str, str]) -> RowIds:
    def value(name: str) -> Optional[str]:
        return _relation(metadata, name)

    return RowIds(
        order=row.order,
        global_id=row.clip.umid,
        name=segment.name,
        frames=segment.frames,
        top_id=value("Relation_Top_GlobalClipID"),
        previous_id=value("Relation_Previous_GlobalClipID"),
        next_id=value("Relation_Next_GlobalClipID"),
        offset=value("Relation_OffsetInShot"),
    )


def _segment(clip: Clip, metadata: Optional[Dict[str, str]] = None) -> Segment:
    metadata = _metadata(clip) if metadata is None else metadata
    try:
        originals = resolve_p2(clip)
        name = (metadata.get("clipname") or "").strip()
        if not name:
            raise SpanUnresolved("no clipname")
        frames = parse_frames(metadata.get("duration"))
        edit_unit = parse_edit_unit(metadata.get("EditUnit"))
    except (ResolveError, SpanUnresolved) as error:
        raise SpanUnresolved(f"clip {clip.umid}: {error}") from None
    video, audios = originals[0], tuple(originals[1:])
    return Segment(name, video, audios, frames, edit_unit)


def _xml_reader(disk):
    def read_xml(relative: str) -> Optional[str]:
        try:
            return disk.read_text(relative)
        except UnicodeDecodeError:
            raise SpanUnresolved(f"{relative} is not UTF-8") from None

    return read_xml

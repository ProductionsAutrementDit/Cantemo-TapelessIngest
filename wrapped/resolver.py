"""Which clips are in scope, and what their original files are.

P2 only (spec, first slice). The originals are the clip's ClipFile rows,
recorded at wrapping time: one video MXF and N audio MXFs. ``order`` is
0 on every measured row, so the audio order comes from the file names
(``…00.MXF``, ``…01.MXF``…), which is the channel order P2 writes.
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
    Segment,
    SpanUnresolved,
    chain_from_rows,
    chain_from_xml,
    parse_edit_unit,
    parse_frames,
)

P2_PROVIDER = "panasonicP2"


class ResolveError(Exception):
    """The clip's ClipFile rows do not describe one video plus N audio."""


def wrapped_p2_clips(
    item_id: Optional[str] = None, collection_id: Optional[str] = None
) -> QuerySet:
    clips = (
        Clip.objects.filter(provider_name=P2_PROVIDER)
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


def resolve_span(clip: Clip, disk) -> List[Segment]:
    """The spanned take mastered by ``clip``, master first.

    The legacy ``SpannedClips`` rows win when they list a segment besides
    the master's own self row; otherwise the chain is walked through the
    P2 CLIP XMLs in the master's CONTENTS folder, read through ``disk``.
    """
    metadata = _metadata(clip)
    master = _segment(clip, metadata)
    rows = (
        SpannedClips.objects.filter(master_clip=clip)
        .exclude(clip=clip)
        .select_related("clip")
        .order_by("order")
    )
    if rows:
        return chain_from_rows(master, [_segment(row.clip) for row in rows])
    head, marker, _ = master.video.relative.rpartition("/VIDEO/")
    if not marker:
        raise SpanUnresolved(f"master video {master.video.relative} not in VIDEO/")
    ids = MasterIds(
        global_id=clip.umid,
        top_id=metadata.get("Relation_Top_GlobalClipID") or None,
        next_name=metadata.get("Relation_Next_ClipName") or None,
        next_id=metadata.get("Relation_Next_GlobalClipID") or None,
    )
    return chain_from_xml(master, ids, head, _xml_reader(disk))


def _metadata(clip: Clip) -> Dict[str, str]:
    return dict(ClipMetadata.objects.filter(clip=clip).values_list("name", "value"))


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

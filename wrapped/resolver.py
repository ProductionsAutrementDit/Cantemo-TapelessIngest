"""Which clips are in scope, and what their original files are.

P2 only (spec, first slice). The originals are the clip's ClipFile rows,
recorded at wrapping time: one video MXF and N audio MXFs. ``order`` is
0 on every measured row, so the audio order comes from the file names
(``…00.MXF``, ``…01.MXF``…), which is the channel order P2 writes.
"""

from typing import List, Optional

from django.db.models import Q, QuerySet

from portal.plugins.TapelessIngest.models.clip import Clip, ClipFile
from portal.plugins.TapelessIngest.wrapped.paths import (
    OriginalFile,
    UnknownPrefix,
    to_relative,
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

"""What a wrapped ``file`` item's original really is, and its lowres proxy.

The ``file`` provider stored ``ffprobe -print_format xml -show_format
-show_streams`` of the original in ``Clip.clip_xml`` (measured on prod:
21,967 clips; ``<format … size="N">`` equals the wrapped copy's size).
Here it is read for two things: the original's technical signature
(video codec, video resolution, audio codecs in stream order) and its
byte size.

A wrapped ``file`` shape whose signature equals the item's lowres
proxy's is a copy of the proxy's description (cpaa) unless ffprobe says
the original really is that; ``templates.is_proxy_copy`` cannot tell,
since ``file`` originals are MOV/MP4 themselves. Pure: no subprocess,
no Portal.
"""

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from portal.plugins.TapelessIngest.wrapped.gateway import Component, Shape

# (video codec, (width, height), audio codecs in stream order); the codec is
# None when there is no video, "" for a video whose codec is not named.
Signature = Tuple[Optional[str], Optional[Tuple[int, int]], Tuple[str, ...]]

GENUINE = "genuine"
PROXY = "proxy"
AMBIGUOUS = "ambiguous"
# No video, no audio: a shape that describes nothing to compare.
NO_SIGNATURE: Signature = (None, None, ())


@dataclass(frozen=True)
class Probe:
    signature: Signature
    size: Optional[int]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolution(width: Any, height: Any) -> Optional[Tuple[int, int]]:
    width, height = _int(width), _int(height)
    return None if width is None or height is None else (width, height)


def parse_ffprobe(xml: Optional[str]) -> Optional[Probe]:
    """None when there is no ffprobe document (empty, unparsable, or
    another provider's XML). Only the top-level ``<streams>`` count:
    ffprobe lists a program's streams again under ``<programs>``."""
    text = (xml or "").strip().lstrip("\ufeff").strip()
    if not text:
        return None
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError:
        return None
    if _local(root.tag) != "ffprobe":
        return None
    video_codec, resolution, audios = None, None, []
    size = None
    for child in root:
        name = _local(child.tag)
        if name == "format":
            size = _int(child.get("size"))
        if name != "streams":
            continue
        for stream in child:
            if _local(stream.tag) != "stream":
                continue
            kind = stream.get("codec_type")
            if kind == "video" and video_codec is None:
                video_codec = stream.get("codec_name") or ""
                resolution = _resolution(stream.get("width"), stream.get("height"))
            elif kind == "audio":
                audios.append(stream.get("codec_name") or "")
    return Probe((video_codec, resolution, tuple(audios)), size)


def _stream_order(component: Component):
    # as wrapped.shape: Vidispine may send the stream id as a string
    return (int(component.body.get("essenceStreamId", 0)), component.component_id)


def shape_signature(shape: Shape) -> Signature:
    videos = sorted(shape.of_kind("video"), key=_stream_order)
    video: Mapping[str, Any] = videos[0].body if videos else {}
    found = video.get("resolution")
    resolution = (
        _resolution(found.get("width"), found.get("height"))
        if isinstance(found, Mapping)
        else None
    )
    audios = sorted(shape.of_kind("audio"), key=_stream_order)
    return (
        str(video.get("codec") or "") if videos else None,
        resolution,
        tuple(str(a.body.get("codec") or "") for a in audios),
    )


def classify_copy(
    original: Signature, lowres: Sequence[Signature], probe: Optional[Signature]
) -> str:
    """PROXY: equal to a lowres and refuted by ffprobe. AMBIGUOUS: equal to
    a lowres and ffprobe agrees or is absent. GENUINE otherwise. A lowres
    without any description is no evidence either way."""
    if original not in [s for s in lowres if s != NO_SIGNATURE]:
        return GENUINE
    if probe is not None and probe != original:
        return PROXY
    return AMBIGUOUS


def _size(resolution: Optional[Tuple[int, int]]) -> str:
    return "unknown" if resolution is None else f"{resolution[0]}x{resolution[1]}"


def probe_disagreement(original: Signature, probe: Signature) -> Optional[str]:
    """What the shape's description says that ffprobe of the original
    refutes: a video on one side only (a still is a video on both), its
    video resolution, or its number of audio streams. Codec NAMES are never
    compared: Vidispine and ffprobe name the same essence differently
    (JPEG/mjpeg, hevc/unknown)."""
    if original[0] is not None and probe[0] is None:
        return "a video stream, ffprobe has none"
    if original[0] is None and probe[0] is not None:
        return "no video stream, ffprobe has one"
    if original[0] is not None:
        if original[1] != probe[1]:
            return (
                f"video resolution {_size(original[1])}, " f"ffprobe {_size(probe[1])}"
            )
    if len(original[2]) != len(probe[2]):
        return f"{len(original[2])} audio stream(s), ffprobe {len(probe[2])}"
    return None

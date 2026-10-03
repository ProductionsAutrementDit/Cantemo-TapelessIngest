"""What a wrapped ``file`` item's original really is, and its lowres proxy.

The ``file`` provider stored ``ffprobe -print_format xml -show_format
-show_streams`` of the original in ``Clip.clip_xml`` (measured on prod:
21,967 clips; ``<format … size="N">`` equals the wrapped copy's size),
in 97% of them wrapped in ``<Material umid=…>`` rather than ``<ffprobe>``.
Here it is read for two things: the original's technical signature
(video codec, video resolution, audio codecs in stream order) and its
byte size.

``parse_ffprobe_description`` keeps the rest of what Vidispine's own
analysis of the same file agrees with (container format, per-stream codec,
resolution, frame rate, time base, channels, sample rate) so a shape whose
description is a proxy copy can be stated from it, for the container /
codec combinations measured to agree (``FFPROBE_ROUTE_FORMATS``).

A wrapped ``file`` shape whose signature equals the item's lowres
proxy's is a copy of the proxy's description (cpaa) unless ffprobe says
the original really is that; ``templates.is_proxy_copy`` cannot tell,
since ``file`` originals are MOV/MP4 themselves. Pure: no subprocess,
no Portal.
"""

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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
    """None when there is no ffprobe document (empty, unparsable, or a
    root with no direct ``<streams>`` or ``<format>``: Sony's
    NonRealTimeMeta, another provider's XML). Only the top-level
    ``<streams>`` count: ffprobe lists a program's streams again under
    ``<programs>``."""
    text = (xml or "").strip().lstrip("\ufeff").strip()
    if not text:
        return None
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError:
        return None
    # The plain ``<ffprobe>`` root, or (97% on prod) ``<Material umid=…>``
    # wrapping the same top-level ``<streams>`` and ``<format>``.
    if not {_local(child.tag) for child in root} & {"streams", "format"}:
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


# (container format_name, first video codec_name or None, sorted distinct
# audio codec_names) of the formats whose ffprobe was compared with
# Vidispine's analysis of a byte-identical copy (prod 2026-10-03, 11,448 done
# ``file`` items): identity fields agree everywhere. Anything else (MXF h264,
# mpeg2, dvvideo, mpeg4) has no reference.
FFPROBE_ROUTE_FORMATS = frozenset(
    {
        ("mpegts", "h264", ("ac3",)),
        ("mpegts", "h264", ("pcm_bluray",)),
        ("mov,mp4,m4a,3gp,3g2,mj2", "h264", ("aac",)),
        ("mov,mp4,m4a,3gp,3g2,mj2", "h264", ("pcm_s16le",)),
        ("mov,mp4,m4a,3gp,3g2,mj2", "h264", ("pcm_s16be",)),
        ("mov,mp4,m4a,3gp,3g2,mj2", "h264", ()),
        ("mov,mp4,m4a,3gp,3g2,mj2", "prores", ("pcm_s24le",)),
        ("mov,mp4,m4a,3gp,3g2,mj2", "prores", ("pcm_s16le",)),
        ("mov,mp4,m4a,3gp,3g2,mj2", "prores", ()),
    }
)


def _ratio(value: Any, separator: str) -> Optional[List[int]]:
    """``"25/1"`` or ``"1:1"`` as [first, second]; None for anything else
    (absent, ``N/A``) or when either term is zero or negative."""
    parts = str(value or "").split(separator)
    if len(parts) != 2:
        return None
    first, second = _int(parts[0]), _int(parts[1])
    if first is None or second is None or first <= 0 or second <= 0:
        return None
    return [first, second]


def _stream_description(stream: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """(description, problem) of one video or audio ``<stream>``: the problem
    names the first required value it lacks."""
    kind = stream.get("codec_type")
    index = _int(stream.get("index"))
    found: Dict[str, Any] = {"index": index, "kind": kind}
    found["codec"] = stream.get("codec_name") or ""
    problem = None
    if index is None:
        problem = f"{kind} stream without an index"
    elif not found["codec"]:
        problem = f"{kind} stream {index} has no codec_name"
    if kind == "video":
        width, height = _int(stream.get("width")), _int(stream.get("height"))
        found["width"], found["height"] = width, height
        found["avg_frame_rate"] = _ratio(stream.get("avg_frame_rate"), "/")
        found["time_base"] = _ratio(stream.get("time_base"), "/")
        # ffprobe omits it, or says 0:1, when the file does not state one.
        found["sample_aspect_ratio"] = _ratio(stream.get("sample_aspect_ratio"), ":")
        missing = [
            name
            for name in ("width", "height", "avg_frame_rate", "time_base")
            if not found[name]
        ]
    else:
        channels, rate = _int(stream.get("channels")), _int(stream.get("sample_rate"))
        found["channels"], found["sample_rate"] = channels, rate
        missing = [name for name in ("channels", "sample_rate") if not found[name]]
    if problem is None and missing:
        problem = f"{kind} stream {index} has no usable {', '.join(missing)}"
    return found, problem


def parse_ffprobe_description(xml: Optional[str]) -> Optional[Dict[str, Any]]:
    """What the ffprobe route states a shape from, JSON-safe: ``{"format",
    "duration" (the ``<format>`` string), "streams"}`` with the video and
    audio streams in document order. A required value missing from the
    format or from a stream adds a ``"problem"`` string. None when there is
    no ffprobe document (the same documents ``parse_ffprobe`` accepts)."""
    text = (xml or "").strip().lstrip("\ufeff").strip()
    if not text:
        return None
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError:
        return None
    if not {_local(child.tag) for child in root} & {"streams", "format"}:
        return None
    description: Dict[str, Any] = {"format": "", "duration": "", "streams": []}
    problems: List[str] = []
    for child in root:
        name = _local(child.tag)
        if name == "format":
            description["format"] = child.get("format_name") or ""
            description["duration"] = child.get("duration") or ""
        if name != "streams":
            continue
        for stream in child:
            if _local(stream.tag) != "stream":
                continue
            if stream.get("codec_type") not in ("video", "audio"):
                continue
            found, problem = _stream_description(stream)
            description["streams"].append(found)
            if problem:
                problems.append(problem)
    if not description["format"]:
        problems.insert(0, "no format_name")
    elif not description["duration"]:
        problems.insert(0, "no format duration")
    if problems:
        description["problem"] = problems[0]
    return description


def route_format(description: Mapping[str, Any]) -> Tuple[str, Optional[str], Any]:
    """(container format, first video codec or None, sorted distinct audio
    codecs): the key into ``FFPROBE_ROUTE_FORMATS``."""
    streams = description.get("streams", [])
    videos = [s["codec"] for s in streams if s["kind"] == "video"]
    audios = sorted({s["codec"] for s in streams if s["kind"] == "audio"})
    return (
        description.get("format", ""),
        videos[0] if videos else None,
        tuple(audios),
    )

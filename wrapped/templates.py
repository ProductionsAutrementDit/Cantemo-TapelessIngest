"""P2 technical templates for proxy-copied wrapped shapes.

Thousands of legacy P2 items have an ``original`` shape that names the
right wrapped MXF but whose technical description is a copy of the
lowres proxy (MP4 container, h264 480x272, one AAC track: VX-10019).
Those values cannot be restated onto the originals. Genuine wrapped
shapes are homogeneous per P2 format, so a per-format template — the
content-level values of a genuine wrapped shape of the same format —
describes them instead, keyed by the clip's P2 metadata (ClipMetadata).

Pure: no Portal, no Django. ``p2_templates.json`` is generated on prod
by ``migrate_wrapped_items templates`` from genuine ready rows.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from portal.plugins.TapelessIngest.wrapped.gateway import Component, Shape, parse_shape

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "p2_templates.json")

# video_codec does not tell AVC-Intra 50 from 100: the bitrate does.
AVC_I_PREFIX = "AVC-I"
AVC_I_100_MIN_BPS = 80_000_000

_STRIPPED = (
    "id",
    "file",
    "metadata",
    "duration",
    "startTimestamp",
    "startTimecode",
    "firstSMPTETimecode",
    "essenceStreamId",
    "itemTrack",
    "pid",
    "mediaInfo",
)
_TIMECODE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}):(\d{2})$")


@dataclass(frozen=True)
class Timing:
    frames: int
    num: int
    den: int
    start_tc_frames: int


def _value(metadata: Mapping[str, Any], name: str) -> str:
    value = metadata.get(name)
    return value.strip() if isinstance(value, str) else ""


def _edit_unit(metadata: Mapping[str, Any]) -> Tuple[int, int]:
    """``"1/25"`` -> (1, 25); ValueError when it is not a positive ratio."""
    num, _, den = _value(metadata, "EditUnit").partition("/")
    num, den = int(num), int(den)
    if num <= 0 or den <= 0:
        raise ValueError(f"EditUnit {metadata.get('EditUnit')!r} is not positive")
    return num, den


def _frames(metadata: Mapping[str, Any]) -> int:
    frames = int(_value(metadata, "duration"))
    if frames <= 0:
        raise ValueError(f"duration {metadata.get('duration')!r} is not positive")
    return frames


def template_key(clip_metadata: Mapping[str, Any]) -> Optional[str]:
    codec = _value(clip_metadata, "video_codec")
    framerate = _value(clip_metadata, "framerate")
    if not codec or not framerate:
        return None
    try:
        frames = _frames(clip_metadata)
        num, den = _edit_unit(clip_metadata)
    except ValueError:
        return None
    key = f"{codec}|{framerate}"
    if not codec.startswith(AVC_I_PREFIX):
        return key
    try:
        data_size = int(_value(clip_metadata, "data_size"))
    except ValueError:
        return None
    # bitrate = data_size * 8 / (frames * num / den), in integers
    at_least_100 = data_size * 8 * den >= AVC_I_100_MIN_BPS * frames * num
    return f"{key}|AVC-I{'100' if at_least_100 else '50'}"


def timing(clip_metadata: Mapping[str, Any]) -> Timing:
    frames = _frames(clip_metadata)
    num, den = _edit_unit(clip_metadata)
    fps = round(den / num)
    timecode = _value(clip_metadata, "timecode_start")
    match = _TIMECODE.match(timecode)
    if not match:
        raise ValueError(f"timecode_start {timecode!r} is not HH:MM:SS:FF")
    hours, minutes, seconds, frame = (int(part) for part in match.groups())
    if minutes >= 60 or seconds >= 60 or frame >= fps:
        raise ValueError(f"timecode_start {timecode!r} is out of range at {fps} fps")
    start = ((hours * 60 + minutes) * 60 + seconds) * fps + frame
    return Timing(frames=frames, num=num, den=den, start_tc_frames=start)


def is_proxy_copy(shape: Shape) -> bool:
    containers = shape.of_kind("container")
    container_format = str(containers[0].body.get("format", "")) if containers else ""
    return container_format.startswith("mov,mp4") or "video/mp4" in shape.mime_types


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _stream_order(component: Component):
    # as wrapped.shape: Vidispine may send the stream id as a string
    return (int(component.body.get("essenceStreamId", 0)), component.component_id)


def _pick(component: Optional[Component], names: Tuple[str, ...]) -> Tuple:
    body = component.body if component else {}
    return tuple(_freeze(body.get(name)) for name in names)


def signature(shape: Shape) -> Tuple:
    """The content-level values two shapes of one P2 format share."""
    containers = shape.of_kind("container")
    videos = shape.of_kind("video")
    audios = sorted(shape.of_kind("audio"), key=_stream_order)
    video_names = ("codec", "resolution", "pixelFormat", "fieldOrder")
    video_names += ("averageFrameRate",)
    audio_names = ("codec", "channelCount", "timeBase", "sampleFormat")
    return (
        _pick(containers[0] if containers else None, ("format",)),
        tuple(shape.mime_types),
        _pick(videos[0] if videos else None, video_names),
        len(audios),
        tuple(_pick(audio, audio_names) for audio in audios),
    )


def _strip(body: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in body.items() if k not in _STRIPPED}


def strip_for_template(document: Mapping[str, Any]) -> Dict[str, Any]:
    shape = parse_shape(document)
    template: Dict[str, Any] = {}
    if shape.mime_types:
        template["mimeType"] = list(shape.mime_types)
    containers = shape.of_kind("container")
    if containers:
        template["containerComponent"] = _strip(containers[0].body)
    videos = shape.of_kind("video")
    if videos:
        template["videoComponent"] = [_strip(videos[0].body)]
    audios = sorted(shape.of_kind("audio"), key=_stream_order)
    if audios:
        template["audioComponent"] = [_strip(audios[0].body)]
    return template


def _common_keys(bodies) -> Tuple[set, set]:
    """(keys whose value is identical in every body, every key seen)."""
    seen = set().union(*(body.keys() for body in bodies)) if bodies else set()
    first = bodies[0] if bodies else {}
    same = {
        k for k in seen if all(k in body and body[k] == first.get(k) for body in bodies)
    }
    return same, seen


def common_template(
    documents: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
    """The first document's template, keeping in each component only the
    values identical across ALL documents (and, for audio, all their
    tracks): a value that varies is per-file (numberOfPackets, bitrate...)
    and must never be stated onto another item. Returns the template and
    the dropped keys per component."""
    template = strip_for_template(documents[0])
    dropped: Dict[str, List[str]] = {}
    for component in ("containerComponent", "videoComponent", "audioComponent"):
        bodies = []
        for document in documents:
            raw = document.get(component)
            for body in raw if isinstance(raw, list) else [raw] if raw else []:
                bodies.append(_strip(body))
        same, seen = _common_keys(bodies)
        dropped[component] = sorted(seen - same)
        kept = template.get(component)
        if isinstance(kept, list):
            template[component] = [
                {k: v for k, v in body.items() if k in same} for body in kept
            ]
        elif kept is not None:
            template[component] = {k: v for k, v in kept.items() if k in same}
    return template, dropped


def load_templates(path: str = DEFAULT_PATH) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {}

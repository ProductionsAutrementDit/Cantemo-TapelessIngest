"""What a wrapped ``xdcam`` original really is, from its Sony NRT XML.

The ``xdcam`` provider stored the clip's Sony NonRealTimeMeta (NRT) XML in
``Clip.clip_xml``, never an ffprobe. On prod (2026-10-07) 1,541 wrapped
xdcam original shapes are a copy of the lowres proxy's description while
their NRT names an MPEG-2 / AVC 1920x1080 original; the originals are on
tape only, so no ffprobe can be taken. The NRT is uniform per format, so
each measured format (``XDCAM_NRT_FORMATS``) maps to the description
``ffprobe.parse_ffprobe_description`` would return for it, and
``shape.build_ffprobe_document`` states the shape from it unchanged.

Vidispine numbers MXF streams from 1 (video 1, audio 2..N: ffprobe's index
plus one) and MP4 streams from 0 (ffprobe's index), measured on VX-41.
``Duration@value`` is in frames at 25 per second (also for 50i), which
``LtcChangeTable@tcFps`` "25" confirms. Pure: no subprocess, no Portal.
"""

import copy
import xml.etree.ElementTree as ElementTree
from typing import Any, Dict, List, Mapping, Optional, Tuple

NO_NRT = "no NRT XML"
NRT_FPS = 25
MP4_FORMAT = "mov,mp4,m4a,3gp,3g2,mj2"

# (Device@modelName, VideoFrame@videoCodec, VideoFrame@formatFps,
# every AudioRecPort@audioCodec in document order)
NrtKey = Tuple[Optional[str], Optional[str], Optional[str], Tuple[str, ...]]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _description(
    container: str,
    video: Tuple[int, str, List[int]],
    audio_indices: range,
    audio_codec: str,
    channels: int,
) -> Dict[str, Any]:
    index, codec, time_base = video
    streams: List[Dict[str, Any]] = [
        {
            "index": index,
            "kind": "video",
            "codec": codec,
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": [25, 1],
            "time_base": time_base,
            "sample_aspect_ratio": [1, 1],
        }
    ]
    streams += [
        {
            "index": n,
            "kind": "audio",
            "codec": audio_codec,
            "channels": channels,
            "sample_rate": 48000,
        }
        for n in audio_indices
    ]
    return {"format": container, "duration": "", "streams": streams}


_LPCM24_8 = ("LPCM24",) * 8
_LPCM16_2 = ("LPCM16",) * 2
_LPCM16_4 = ("LPCM16",) * 4
_MPEG2_EX = "MPEG2HD35_1920_1080_MP@HL"

# (format key + upper-case ClipFile extension) -> description; prod row
# counts 2026-10-07.
XDCAM_NRT_FORMATS: Mapping[Tuple[Any, ...], Dict[str, Any]] = {
    # 159 rows; measured (ffprobe of 4 disk files + VX-41 shapes)
    ("PXW-FS7", "AVC100CBG_1920_1080_H422IP@L41", "25p", _LPCM24_8, "MXF"): (
        _description("mxf", (1, "h264", [1, 25]), range(2, 10), "pcm_s24le", 1)
    ),
    # 365 rows; inferred from the FS7 MXF layout
    ("PMW-300", "MPEG2HD50CBR_1920_1080_422P@HL", "50i", _LPCM24_8, "MXF"): (
        _description("mxf", (1, "mpeg2video", [1, 25]), range(2, 10), "pcm_s24le", 1)
    ),
    # 120 rows; measured (266 stored ffprobe of XDCAM EX files)
    ("PMW-EX3", _MPEG2_EX, "25p", _LPCM16_2, "MP4"): _description(
        MP4_FORMAT, (0, "mpeg2video", [1, 2500]), range(1, 3), "pcm_s16be", 1
    ),
    # 569 rows; inferred from EX
    ("PMW-TD300", _MPEG2_EX, "25p", _LPCM16_4, "MP4"): _description(
        MP4_FORMAT, (0, "mpeg2video", [1, 2500]), range(1, 5), "pcm_s16be", 1
    ),
    # 15 rows; inferred from EX
    ("PMW-350", _MPEG2_EX, "50i", _LPCM16_4, "MP4"): _description(
        MP4_FORMAT, (0, "mpeg2video", [1, 2500]), range(1, 5), "pcm_s16be", 1
    ),
    # 283 rows; measured (ffprobe of 2 disk files + 3 VX-41 shapes)
    ("ILCE-7S", None, None, (), "MP4"): _description(
        MP4_FORMAT, (0, "h264", [1, 25000]), range(1, 2), "pcm_s16be", 2
    ),
}


def parse_nrt(xml: Optional[str]) -> Optional[Dict[str, Any]]:
    """``{"key", "frames", "tc_fps"}`` of a NonRealTimeMeta document (the
    first ``Device``, ``VideoFrame``, ``Duration`` and ``LtcChangeTable``,
    wherever nested; every ``AudioRecPort``); None for anything else
    (empty, unparsable, ffprobe, another root)."""
    text = (xml or "").strip().lstrip("﻿").strip()
    if not text:
        return None
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError:
        return None
    if _local(root.tag) != "NonRealTimeMeta":
        return None
    first: Dict[str, Any] = {}
    audios: List[str] = []
    for element in root.iter():
        name = _local(element.tag)
        if name == "AudioRecPort":
            audios.append(element.get("audioCodec") or "")
        elif name in ("Device", "VideoFrame", "Duration", "LtcChangeTable"):
            first.setdefault(name, element)

    def value(name: str, attribute: str) -> Optional[str]:
        element = first.get(name)
        return None if element is None else element.get(attribute)

    key: NrtKey = (
        value("Device", "modelName"),
        value("VideoFrame", "videoCodec"),
        value("VideoFrame", "formatFps"),
        tuple(audios),
    )
    return {
        "key": key,
        "frames": _int(value("Duration", "value")),
        "tc_fps": value("LtcChangeTable", "tcFps"),
    }


def nrt_description(
    nrt: Optional[Mapping[str, Any]], extension: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(description, None) for a measured format, the ``duration`` being
    the NRT's frames at 25 per second with 6 decimals; (None, problem)
    otherwise. ``extension`` is the ClipFile's, upper-case, without a dot."""
    if nrt is None:
        return None, NO_NRT
    key = (*nrt["key"], extension)
    if key not in XDCAM_NRT_FORMATS:
        return None, f"NRT format {key} has no reference"
    frames = nrt["frames"]
    if frames is None:
        return None, "NRT has no Duration"
    if nrt["tc_fps"] != str(NRT_FPS):
        return None, f"NRT timecode rate {nrt['tc_fps']} is not {NRT_FPS}"
    description = copy.deepcopy(XDCAM_NRT_FORMATS[key])
    description["duration"] = f"{frames / NRT_FPS:.6f}"
    return description, None

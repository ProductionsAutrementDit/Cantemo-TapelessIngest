"""Restate the wrapped shape against the original files.

The wrapped MXF and the originals are the same essence, so everything
that describes the CONTENT — duration, format, codec, resolution, audio
parameters — is copied from the wrapped shape. What identified the
wrapped file (component ids, file references, analysed metadata) is
dropped. Every original is a single-stream file, so each component's
``essenceStreamId`` becomes 0. Vidispine cannot analyse a tape-only
original, which is why these values have to be stated at all.

When the wrapped shape's technical description is a copy of the lowres
proxy, ``build_document_from_template`` states the content from the P2
format's template and the timing from the clip's P2 metadata instead.
"""

import copy
from typing import Any, Dict, List, Mapping, Optional, Sequence

from portal.plugins.TapelessIngest.wrapped.gateway import Component, Shape
from portal.plugins.TapelessIngest.wrapped.templates import Timing

_DROPPED = ("id", "file", "metadata")


class ShapeMismatch(Exception):
    """The wrapped shape cannot be restated against these originals."""


def mismatch(wrapped: Shape, audio_count: int) -> Optional[str]:
    containers = len(wrapped.of_kind("container"))
    videos = len(wrapped.of_kind("video"))
    audios = len(wrapped.of_kind("audio"))
    if containers != 1 or videos != 1:
        return f"{containers} container / {videos} video component(s), expected 1/1"
    if audios != audio_count:
        return f"{audios} audio component(s) for {audio_count} audio original(s)"
    return None


def _stream_order(component: Component):
    # Vidispine may send the stream id as a string: "10" must sort after "2".
    return (int(component.body.get("essenceStreamId", 0)), component.component_id)


def _restate(component: Component, file_id: str, stream: bool) -> Dict[str, Any]:
    body = {k: v for k, v in component.body.items() if k not in _DROPPED}
    body["file"] = [{"id": file_id}]
    if stream:
        body["essenceStreamId"] = 0
    return body


def build_document(
    wrapped: Shape, video_file_id: str, audio_file_ids: Sequence[str]
) -> Dict[str, Any]:
    problem = mismatch(wrapped, len(audio_file_ids))
    if problem:
        raise ShapeMismatch(problem)
    (container,) = wrapped.of_kind("container")
    (video,) = wrapped.of_kind("video")
    audios: List[Component] = sorted(wrapped.of_kind("audio"), key=_stream_order)
    document: Dict[str, Any] = {
        "containerComponent": _restate(container, video_file_id, stream=False),
        "videoComponent": [_restate(video, video_file_id, stream=True)],
        "audioComponent": [
            _restate(audio, file_id, stream=True)
            for audio, file_id in zip(audios, audio_file_ids)
        ],
    }
    if wrapped.mime_types:
        document["mimeType"] = list(wrapped.mime_types)
    return document


def build_document_from_template(
    template: Mapping[str, Any],
    video_file_id: str,
    audio_file_ids: Sequence[str],
    timing: Timing,
) -> Dict[str, Any]:
    if audio_file_ids and not template.get("audioComponent"):
        raise ShapeMismatch(
            f"template has no audio component for {len(audio_file_ids)} "
            f"audio original(s)"
        )

    def body(source: Mapping[str, Any], file_id: str) -> Dict[str, Any]:
        restated = copy.deepcopy(dict(source))
        restated["duration"] = {
            "samples": timing.frames,
            "timeBase": {"numerator": timing.num, "denominator": timing.den},
        }
        restated["file"] = [{"id": file_id}]
        return restated

    container = body(template["containerComponent"], video_file_id)
    container["startTimecode"] = timing.start_tc_frames
    video = body(template["videoComponent"][0], video_file_id)
    video["essenceStreamId"] = 0
    audios = []
    for n, file_id in enumerate(audio_file_ids, start=1):
        audio = body(template["audioComponent"][0], file_id)
        audio["essenceStreamId"] = 0
        audio["itemTrack"] = f"A{n}"
        audios.append(audio)
    document: Dict[str, Any] = {
        "containerComponent": container,
        "videoComponent": [video],
        "audioComponent": audios,
    }
    if template.get("mimeType"):
        document["mimeType"] = list(template["mimeType"])
    return document

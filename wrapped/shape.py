"""Restate the wrapped shape against the original files.

The wrapped MXF and the originals are the same essence, so everything
that describes the CONTENT — duration, format, codec, resolution, audio
parameters — is copied from the wrapped shape. What identified the
wrapped file (component ids, file references, analysed metadata) is
dropped. Every original is a single-stream file, so each component's
``essenceStreamId`` becomes 0. Vidispine cannot analyse a tape-only
original, which is why these values have to be stated at all.
"""

from typing import Any, Dict, List, Optional, Sequence

from portal.plugins.TapelessIngest.wrapped.gateway import Component, Shape

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
    return (component.body.get("essenceStreamId", 0), component.component_id)


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

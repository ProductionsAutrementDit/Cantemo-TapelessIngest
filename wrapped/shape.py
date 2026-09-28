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
from fractions import Fraction
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

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


MICROSECONDS = {"numerator": 1, "denominator": 1_000_000}


def _exact(numerator: int, denominator: int, unit: str) -> int:
    if numerator % denominator:
        raise ShapeMismatch(
            f"duration {numerator}/{denominator} is not a whole number of {unit}"
        )
    return numerator // denominator


def audio_time_base(body: Mapping[str, Any]) -> Tuple[int, int]:
    """(numerator, denominator) of an audio body's own sample rate."""
    time_base = body.get("timeBase")
    try:
        rate_num = int(time_base["numerator"])
        rate_den = int(time_base["denominator"])
    except (TypeError, KeyError, ValueError):
        raise ShapeMismatch("audioComponent has no timeBase") from None
    if rate_num <= 0 or rate_den <= 0:
        raise ShapeMismatch(f"audioComponent timeBase {time_base} is not positive")
    return rate_num, rate_den


def _durations(template: Mapping[str, Any], timing: Timing, audio: bool):
    """Each component's duration in the time base Vidispine itself uses
    for it (measured on genuine wrapped shapes): the container in
    microseconds, the video in frames at the EditUnit, each audio in
    samples at the template audio's own timeBase. Exact or refused."""
    frames, num, den = timing.frames, timing.num, timing.den
    container = {
        "samples": _exact(frames * num * 1_000_000, den, "microseconds"),
        "timeBase": dict(MICROSECONDS),
    }
    video = {"samples": frames, "timeBase": {"numerator": num, "denominator": den}}
    if not audio:
        return container, video, None
    rate_num, rate_den = audio_time_base(template["audioComponent"][0])
    sound = {
        "samples": _exact(frames * num * rate_den, den * rate_num, "audio samples"),
        "timeBase": {"numerator": rate_num, "denominator": rate_den},
    }
    return container, video, sound


def build_document_from_template(
    template: Mapping[str, Any],
    video_file_id: str,
    audio_file_ids: Sequence[str],
    timing: Timing,
) -> Dict[str, Any]:
    if not template.get("containerComponent"):
        raise ShapeMismatch("template has no containerComponent")
    if not template.get("videoComponent"):
        raise ShapeMismatch("template has no videoComponent")
    if audio_file_ids and not template.get("audioComponent"):
        raise ShapeMismatch(
            f"template has no audio component for {len(audio_file_ids)} "
            f"audio original(s)"
        )

    container_duration, video_duration, audio_duration = _durations(
        template, timing, bool(audio_file_ids)
    )

    def body(
        source: Mapping[str, Any], file_id: str, duration: Mapping[str, Any]
    ) -> Dict[str, Any]:
        restated = copy.deepcopy(dict(source))
        restated["duration"] = copy.deepcopy(dict(duration))
        restated["file"] = [{"id": file_id}]
        return restated

    container = body(template["containerComponent"], video_file_id, container_duration)
    container["startTimecode"] = timing.start_tc_frames
    video = body(template["videoComponent"][0], video_file_id, video_duration)
    video["essenceStreamId"] = 0
    video["itemTrack"] = "V1"
    audios = []
    for n, file_id in enumerate(audio_file_ids, start=1):
        audio = body(template["audioComponent"][0], file_id, audio_duration)
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


_COMPARED = ("containerComponent", "videoComponent", "audioComponent")


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def duration_seconds(duration: Any) -> Optional[Fraction]:
    """A Vidispine duration's exact value in seconds; None when malformed.
    Genuine shapes state one duration in several time bases (measured:
    audio as samples at 1/48000 or as frames at 1/25), so durations are
    compared by value, never by representation."""
    if not isinstance(duration, Mapping):
        return None
    samples, time_base = duration.get("samples"), duration.get("timeBase")
    if not _whole(samples) or not isinstance(time_base, Mapping):
        return None
    numerator, denominator = time_base.get("numerator"), time_base.get("denominator")
    if not _whole(numerator) or not _whole(denominator) or denominator <= 0:
        return None
    return Fraction(samples * numerator, denominator)


def _agrees(key: str, mine: Any, reference: Mapping[str, Any]) -> bool:
    if key not in reference:
        return False
    if key == "duration":
        ours, theirs = duration_seconds(mine), duration_seconds(reference[key])
        return ours is not None and ours == theirs
    return reference[key] == mine


def template_disagreements(
    template: Mapping[str, Any], wrapped: Shape, timing: Timing
) -> List[str]:
    """Rebuild a genuine wrapped shape from ``template`` and ``timing`` and
    name every value (``component.key``) where the result differs from
    restating the wrapped shape itself. File ids are not compared; a key
    the genuine restatement lacks counts as a difference; a ``duration``
    is compared by its exact value in seconds (a malformed one on either
    side differs), every other key by exact equality. Empty: the
    template and the timing derivation reproduce this reference."""
    audio_ids = [f"A{n}" for n in range(len(wrapped.of_kind("audio")))]
    try:
        genuine = build_document(wrapped, "V", audio_ids)
        rebuilt = build_document_from_template(template, "V", audio_ids, timing)
    except ShapeMismatch as error:
        return [f"unbuildable: {error}"]
    differs = set()
    if rebuilt.get("mimeType") != genuine.get("mimeType"):
        differs.add("mimeType")
    for component in _COMPARED:
        ours, theirs = rebuilt.get(component), genuine.get(component)
        ours = ours if isinstance(ours, list) else [ours] if ours else []
        theirs = theirs if isinstance(theirs, list) else [theirs] if theirs else []
        if len(ours) != len(theirs):
            differs.add(f"{component} count")
        for mine, reference in zip(ours, theirs):
            for key, value in mine.items():
                if key != "file" and not _agrees(key, value, reference):
                    differs.add(f"{component}.{key}")
    return sorted(differs)

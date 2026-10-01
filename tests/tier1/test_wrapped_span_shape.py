"""Tier 1: one multi-segment original ShapeDocument for a spanned take."""

import pytest

from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from portal.plugins.TapelessIngest.wrapped.shape import (
    MICROSECONDS,
    ShapeMismatch,
    build_document_from_template,
    build_span_document,
)
from portal.plugins.TapelessIngest.wrapped.templates import Timing
from tests.wrapped_fakes import genuine_p2_document, p2_template

SEGMENTS = [
    {"name": "0037OO", "frames": 100, "num": 1, "den": 25},
    {"name": "003876", "frames": 100, "num": 1, "den": 25},
    {"name": "0039EX", "frames": 50, "num": 1, "den": 25},
]
VIDEOS = ["VX-V0", "VX-V1", "VX-V2"]
AUDIOS = [[f"VX-A{i}{k}" for k in range(4)] for i in range(3)]
WHOLE = Timing(frames=250, num=1, den=25, start_tc_frames=1657612)


PER_FILE = {
    "startTimestamp": 1234,
    "startTimecode": 1657612,
    "firstSMPTETimecode": "18:25:04:12",
    "pid": 7,
    "numberOfPackets": 250,
}


def _per_file_document():
    """A genuine wrapped shape whose every body carries the whole-file
    values Vidispine measured on the wrapped MXF."""
    document = genuine_p2_document(frames=250)
    bodies = [document["containerComponent"]] + document["videoComponent"]
    for body in bodies + document["audioComponent"]:
        body.update(PER_FILE)
    return document


def _genuine(document=None, segments=SEGMENTS, audios=AUDIOS):
    wrapped = parse_shape(document or genuine_p2_document(frames=250))
    return build_span_document(
        segments, VIDEOS[: len(segments)], audios, wrapped=wrapped
    )


def _template(segments=SEGMENTS):
    return build_span_document(
        segments, VIDEOS, AUDIOS, template=p2_template(), timing=WHOLE
    )


def test_genuine_states_one_container_n_videos_and_4n_audios():
    document = _genuine()
    assert set(document) == {
        "containerComponent",
        "videoComponent",
        "audioComponent",
        "mimeType",
    }
    assert len(document["videoComponent"]) == 3
    assert len(document["audioComponent"]) == 12
    assert document["mimeType"] == ["application/mxf"]


def test_genuine_container_names_segment_one_and_keeps_the_total():
    container = _genuine()["containerComponent"]
    assert container["file"] == [{"id": "VX-V0"}]
    assert container["duration"] == {"samples": 10_000_000, "timeBase": MICROSECONDS}
    assert container["format"] == "mxf"
    assert container["startTimecode"] == 1657612
    assert "id" not in container and "metadata" not in container


def test_genuine_videos_name_each_segment_with_its_own_duration():
    videos = _genuine()["videoComponent"]
    assert [v["file"] for v in videos] == [[{"id": v}] for v in VIDEOS]
    assert [v["itemTrack"] for v in videos] == ["V1", "V2", "V3"]
    assert [v["essenceStreamId"] for v in videos] == [0, 0, 0]
    assert [v["duration"] for v in videos] == [
        {"samples": n, "timeBase": {"numerator": 1, "denominator": 25}}
        for n in (100, 100, 50)
    ]
    assert all(v["codec"] == "dvvideo" and "id" not in v for v in videos)


def test_genuine_audios_follow_segment_then_channel_order():
    audios = _genuine()["audioComponent"]
    assert [a["file"] for a in audios] == [
        [{"id": file_id}] for ids in AUDIOS for file_id in ids
    ]
    assert [a["itemTrack"] for a in audios] == [f"A{n}" for n in range(1, 13)]
    assert {a["essenceStreamId"] for a in audios} == {0}
    assert [a["duration"]["samples"] for a in audios] == ([192_000] * 8 + [96_000] * 4)
    assert {a["duration"]["timeBase"]["denominator"] for a in audios} == {48000}


def test_genuine_channel_k_is_the_kth_wrapped_audio_by_stream():
    document = genuine_p2_document(frames=250)
    for n, body in enumerate(document["audioComponent"]):
        body["channelCount"] = 10 + n
    document["audioComponent"].reverse()
    audios = _genuine(document)["audioComponent"]
    assert [a["channelCount"] for a in audios] == [10, 11, 12, 13] * 3


def test_genuine_refuses_a_segment_with_another_audio_count():
    with pytest.raises(ShapeMismatch, match="0039EX"):
        _genuine(audios=AUDIOS[:2] + [AUDIOS[2][:2]])


def test_genuine_refuses_an_inexact_segment_and_names_it():
    segments = [dict(SEGMENTS[0], frames=101), dict(SEGMENTS[1], frames=99)]
    document = genuine_p2_document(frames=200)
    for body in document["audioComponent"]:
        body["timeBase"] = {"numerator": 1, "denominator": 7}
    with pytest.raises(ShapeMismatch) as error:
        _genuine(document, segments, AUDIOS[:2])
    assert str(error.value) == (
        "segment 0037OO: duration 707/25 is not a whole number of audio samples"
    )


def test_template_container_states_the_whole_take():
    document = _template()
    whole = build_document_from_template(p2_template(), "VX-V0", [], WHOLE)
    assert document["containerComponent"] == whole["containerComponent"]
    assert document["containerComponent"]["duration"]["samples"] == 10_000_000
    assert document["mimeType"] == ["application/mxf"]


def test_template_states_each_segment_from_its_own_timing():
    document = _template()
    assert [v["itemTrack"] for v in document["videoComponent"]] == ["V1", "V2", "V3"]
    assert [v["file"] for v in document["videoComponent"]] == [
        [{"id": v}] for v in VIDEOS
    ]
    assert [v["duration"]["samples"] for v in document["videoComponent"]] == [
        100,
        100,
        50,
    ]
    audios = document["audioComponent"]
    assert [a["itemTrack"] for a in audios] == [f"A{n}" for n in range(1, 13)]
    assert [a["duration"]["samples"] for a in audios] == ([192_000] * 8 + [96_000] * 4)
    assert audios[5]["file"] == [{"id": "VX-A11"}]
    assert audios[0]["codec"] == "pcm_s24le"


def test_template_refuses_an_inexact_segment_and_names_it():
    template = p2_template()
    template["audioComponent"][0]["timeBase"] = {"numerator": 1, "denominator": 7}
    segments = [dict(SEGMENTS[0], frames=101), dict(SEGMENTS[1], frames=99)]
    with pytest.raises(ShapeMismatch, match="^segment 0037OO: "):
        build_span_document(
            segments,
            VIDEOS[:2],
            AUDIOS[:2],
            template=template,
            timing=Timing(200, 1, 25, 0),
        )


def test_segment_components_drop_the_wrapped_files_whole_take_values():
    document = _genuine(_per_file_document())
    for body in document["videoComponent"] + document["audioComponent"]:
        assert not set(PER_FILE) & set(body), body["itemTrack"]
        assert {"duration", "essenceStreamId", "itemTrack", "file"} <= set(body)
    assert document["videoComponent"][0]["codec"] == "dvvideo"
    container = document["containerComponent"]
    assert {k: container[k] for k in PER_FILE} == PER_FILE

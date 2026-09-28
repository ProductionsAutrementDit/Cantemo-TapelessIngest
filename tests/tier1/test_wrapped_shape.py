"""Tier 1: the original ShapeDocument restated from the wrapped shape."""

import pytest

from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from portal.plugins.TapelessIngest.wrapped.shape import (
    ShapeMismatch,
    build_document,
    build_document_from_template,
    mismatch,
)
from portal.plugins.TapelessIngest.wrapped.templates import Timing
from tests.wrapped_fakes import p2_template, wrapped_p2_document

AUDIO_IDS = ["VX-A0", "VX-A1", "VX-A2", "VX-A3"]


def _build(document=None, audio_ids=AUDIO_IDS):
    wrapped = parse_shape(document or wrapped_p2_document())
    return build_document(wrapped, "VX-V", audio_ids)


def test_container_and_video_name_the_video_original():
    document = _build()
    assert document["containerComponent"]["file"] == [{"id": "VX-V"}]
    assert [c["file"] for c in document["videoComponent"]] == [[{"id": "VX-V"}]]


def test_each_audio_original_gets_its_own_component():
    document = _build()
    assert [c["file"][0]["id"] for c in document["audioComponent"]] == AUDIO_IDS


def test_content_level_values_are_copied_from_the_wrapped_shape():
    document = _build()
    assert document["containerComponent"]["format"] == "mxf"
    assert document["containerComponent"]["duration"]["samples"] == 218
    assert document["videoComponent"][0]["codec"] == "dvvideo"
    assert document["videoComponent"][0]["resolution"] == {
        "width": 1440,
        "height": 1080,
    }
    assert document["mimeType"] == ["application/mxf"]


def test_wrapped_identity_is_never_copied():
    document = _build()
    for body in [document["containerComponent"], *document["videoComponent"]]:
        assert "id" not in body and "metadata" not in body
    for body in document["audioComponent"]:
        assert "id" not in body and "metadata" not in body


def test_every_original_is_a_single_stream_file():
    document = _build()
    streams = [c["essenceStreamId"] for c in document["audioComponent"]]
    assert streams == [0, 0, 0, 0]
    assert document["videoComponent"][0]["essenceStreamId"] == 0


def test_audio_components_follow_stream_order_not_list_order():
    shuffled = wrapped_p2_document()
    shuffled["audioComponent"].reverse()
    document = _build(shuffled)
    assert [c["itemTrack"] for c in document["audioComponent"]] == [
        "A1",
        "A2",
        "A3",
        "A4",
    ]
    assert [c["file"][0]["id"] for c in document["audioComponent"]] == AUDIO_IDS


def test_stream_order_is_numeric_even_when_vidispine_sends_strings():
    wrapped = wrapped_p2_document(audio_count=11)
    for body in wrapped["audioComponent"]:
        body["essenceStreamId"] = str(body["essenceStreamId"])
    wrapped["audioComponent"].reverse()
    ids = [f"VX-A{n}" for n in range(11)]
    document = _build(wrapped, audio_ids=ids)
    assert [c["itemTrack"] for c in document["audioComponent"]] == [
        f"A{n}" for n in range(1, 12)
    ]


def test_audio_count_mismatch_is_refused():
    wrapped = parse_shape(wrapped_p2_document(audio_count=1))
    assert "1 audio component(s) for 4 audio original(s)" in mismatch(wrapped, 4)
    with pytest.raises(ShapeMismatch):
        build_document(wrapped, "VX-V", AUDIO_IDS)


def test_a_buildable_shape_has_no_mismatch():
    assert mismatch(parse_shape(wrapped_p2_document()), 4) is None


# build_document_from_template

TIMING = Timing(frames=497, num=1, den=25, start_tc_frames=1657612)
DURATION = {"samples": 497, "timeBase": {"numerator": 1, "denominator": 25}}


def _from_template(audio_ids=AUDIO_IDS, template=None):
    return build_document_from_template(
        template or p2_template(), "VX-V", audio_ids, TIMING
    )


def test_template_container_and_video_name_the_video_original():
    document = _from_template()
    assert document["containerComponent"]["file"] == [{"id": "VX-V"}]
    assert [c["file"] for c in document["videoComponent"]] == [[{"id": "VX-V"}]]


@pytest.mark.parametrize("count", [4, 8])
def test_one_template_audio_component_per_audio_original(count):
    ids = [f"VX-A{n}" for n in range(count)]
    document = _from_template(ids)
    audios = document["audioComponent"]
    assert [c["file"] for c in audios] == [[{"id": i}] for i in ids]
    assert [c["itemTrack"] for c in audios] == [f"A{n}" for n in range(1, count + 1)]
    assert all(c["codec"] == "pcm_s24le" and c["channelCount"] == 1 for c in audios)
    assert [c["essenceStreamId"] for c in audios] == [0] * count


def test_timing_comes_from_the_clip_metadata_on_every_component():
    document = _from_template()
    bodies = [
        document["containerComponent"],
        *document["videoComponent"],
        *document["audioComponent"],
    ]
    assert all(body["duration"] == DURATION for body in bodies)
    assert document["containerComponent"]["startTimecode"] == 1657612
    assert "startTimecode" not in document["videoComponent"][0]


def test_template_content_and_mime_are_kept():
    document = _from_template()
    assert document["mimeType"] == ["application/mxf"]
    assert document["containerComponent"]["format"] == "mxf_d10"
    video = document["videoComponent"][0]
    assert video["codec"] == "h264" and video["pixelFormat"] == "yuv422p10le"
    assert video["resolution"] == {"width": 1920, "height": 1080}
    assert video["essenceStreamId"] == 0


def test_building_never_mutates_the_template():
    template = p2_template()
    _from_template(template=template)
    assert template == p2_template()


def test_the_audio_components_do_not_share_one_body():
    document = _from_template()
    document["audioComponent"][0]["duration"]["samples"] = 1
    assert document["audioComponent"][1]["duration"]["samples"] == 497
    assert document["videoComponent"][0]["duration"]["samples"] == 497


def test_a_template_without_audio_is_refused_for_audio_originals():
    template = p2_template()
    del template["audioComponent"]
    with pytest.raises(ShapeMismatch, match="no audio component"):
        _from_template(template=template)

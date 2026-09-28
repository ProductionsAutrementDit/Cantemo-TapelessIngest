"""Tier 1: the original ShapeDocument restated from the wrapped shape."""

import pytest

from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from portal.plugins.TapelessIngest.wrapped.shape import (
    ShapeMismatch,
    build_document,
    mismatch,
)
from tests.wrapped_fakes import wrapped_p2_document

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


def test_audio_count_mismatch_is_refused():
    wrapped = parse_shape(wrapped_p2_document(audio_count=1))
    assert "1 audio component(s) for 4 audio original(s)" in mismatch(wrapped, 4)
    with pytest.raises(ShapeMismatch):
        build_document(wrapped, "VX-V", AUDIO_IDS)


def test_a_buildable_shape_has_no_mismatch():
    assert mismatch(parse_shape(wrapped_p2_document()), 4) is None

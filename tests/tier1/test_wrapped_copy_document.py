"""Tier 1: a genuine ``file`` shape restated onto its original, body kept."""

import pytest

from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from portal.plugins.TapelessIngest.wrapped.shape import (
    ShapeMismatch,
    build_copy_document,
    build_ffprobe_document,
)
from tests.tier1.test_wrapped_ffprobe import PRORES_DESCRIPTION
from tests.wrapped_fakes import (
    binary_only_document,
    file_mov_document,
    file_wav_document,
)


def test_every_component_is_kept_with_its_stream_and_the_file_swapped():
    wrapped = file_mov_document()
    document = build_copy_document(parse_shape(wrapped), "VX-F9")

    assert document["mimeType"] == ["video/quicktime"]
    assert "binaryComponent" not in document
    container = document["containerComponent"]
    assert container["file"] == [{"id": "VX-F9"}]
    for key in ("id", "metadata", "mediaInfo"):
        assert key not in container
    expected = {
        k: v
        for k, v in wrapped["containerComponent"].items()
        if k not in ("id", "file", "metadata", "mediaInfo")
    }
    assert {k: v for k, v in container.items() if k != "file"} == expected

    (video,) = document["videoComponent"]
    assert video["essenceStreamId"] == 0 and video["itemTrack"] == "V1"
    assert video["duration"] == wrapped["videoComponent"][0]["duration"]
    assert "mediaInfo" not in video and "id" not in video
    assert video["file"] == [{"id": "VX-F9"}]

    audios = document["audioComponent"]
    assert [a["essenceStreamId"] for a in audios] == [1, 2]
    assert [a["itemTrack"] for a in audios] == ["A1", "A2"]
    assert all(a["file"] == [{"id": "VX-F9"}] for a in audios)
    assert audios[0]["timeBase"] == {"numerator": 1, "denominator": 48000}


def test_the_wrapped_document_is_not_mutated():
    wrapped = file_mov_document()
    shape = parse_shape(wrapped)
    build_copy_document(shape, "VX-F9")
    assert shape.of_kind("video")[0].body["mediaInfo"]


def test_an_audio_only_shape_has_no_video_component():
    document = build_copy_document(parse_shape(file_wav_document()), "VX-F9")
    assert "videoComponent" not in document
    (audio,) = document["audioComponent"]
    assert audio["codec"] == "pcm_s24le" and audio["file"] == [{"id": "VX-F9"}]
    assert document["containerComponent"]["format"] == "wav"


def test_a_binary_only_shape_is_refused():
    with pytest.raises(ShapeMismatch, match="no container, video or audio"):
        build_copy_document(parse_shape(binary_only_document()), "VX-F9")


def test_two_containers_are_refused():
    document = file_mov_document()
    second = dict(document["containerComponent"], id="VX-SW-C2")
    document["containerComponent"] = [document["containerComponent"], second]
    with pytest.raises(ShapeMismatch, match="2 container"):
        build_copy_document(parse_shape(document), "VX-F9")


def test_a_prores_description_is_stated_from_ffprobe_alone():
    document = build_ffprobe_document(PRORES_DESCRIPTION, "VX-F9", 8_720_000)

    file = [{"id": "VX-F9"}]
    assert document == {
        "containerComponent": {
            "format": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": {
                "samples": 8_720_000,
                "timeBase": {"numerator": 1, "denominator": 1_000_000},
            },
            "file": file,
        },
        "videoComponent": [
            {
                "codec": "prores",
                "resolution": {"width": 1920, "height": 1080},
                "averageFrameRate": {"numerator": 25, "denominator": 1},
                "timeBase": {"numerator": 1, "denominator": 25},
                "pixelAspectRatio": {"horizontal": 1, "vertical": 1},
                "essenceStreamId": 1,
                "file": file,
            }
        ],
        "audioComponent": [
            {
                "codec": "pcm_s24le",
                "channelCount": 1,
                "timeBase": {"numerator": 1, "denominator": 48000},
                "essenceStreamId": index,
                "file": file,
            }
            for index in (2, 3)
        ],
    }


def test_an_unknown_pixel_aspect_ratio_is_left_out():
    description = {
        **PRORES_DESCRIPTION,
        "streams": [
            {**PRORES_DESCRIPTION["streams"][0], "sample_aspect_ratio": None},
        ],
    }
    document = build_ffprobe_document(description, "VX-F9", 1)
    assert "pixelAspectRatio" not in document["videoComponent"][0]
    assert "audioComponent" not in document


def test_a_description_with_a_problem_or_no_stream_cannot_be_stated():
    with pytest.raises(ShapeMismatch, match="no sample_rate"):
        build_ffprobe_document(
            {**PRORES_DESCRIPTION, "problem": "no sample_rate"}, "", 1
        )
    with pytest.raises(ShapeMismatch, match="no video or audio stream"):
        build_ffprobe_document({**PRORES_DESCRIPTION, "streams": []}, "", 1)

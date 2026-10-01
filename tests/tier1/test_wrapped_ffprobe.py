"""Tier 1: the ffprobe XML a ``file`` clip stored, and the proxy-copy rule."""

import pytest

from portal.plugins.TapelessIngest.wrapped.ffprobe import (
    AMBIGUOUS,
    GENUINE,
    PROXY,
    classify_copy,
    parse_ffprobe,
    shape_signature,
)
from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from tests.wrapped_fakes import ffprobe_xml, file_mov_document, file_wav_document

MOV = ("prores", (1920, 1080), ("pcm_s24le", "pcm_s24le"))
PROXY_SIG = ("h264", (480, 272), ("aac",))


def test_a_mov_gives_its_video_its_audios_in_order_and_its_size():
    probe = parse_ffprobe(ffprobe_xml(size=987_654_321))
    assert probe.signature == MOV
    assert probe.size == 987_654_321


def test_a_wav_is_audio_only():
    probe = parse_ffprobe(
        ffprobe_xml(video=None, audio=("pcm_s16le",), format_name="wav")
    )
    assert probe.signature == (None, None, ("pcm_s16le",))


def test_a_jpeg_still_is_one_video_stream():
    probe = parse_ffprobe(
        ffprobe_xml(
            video=("mjpeg", 4000, 3000),
            audio=(),
            format_name="image2",
            data_stream=False,
            size=2_345_678,
        )
    )
    assert probe.signature == ("mjpeg", (4000, 3000), ())
    assert probe.size == 2_345_678


def test_a_format_without_size_has_an_unknown_size():
    assert parse_ffprobe(ffprobe_xml(size=None)).size is None


def test_a_namespaced_document_is_read_by_local_name():
    xml = ffprobe_xml().replace(
        "<ffprobe>", '<ffprobe xmlns="http://www.ffmpeg.org/schema/ffprobe">'
    )
    assert parse_ffprobe(xml).signature == MOV


@pytest.mark.parametrize("xml", [None, "", "   ", "<not xml", "<P2Main/>"])
def test_missing_or_foreign_xml_is_no_probe(xml):
    assert parse_ffprobe(xml) is None


def test_shape_signature_orders_audio_by_stream():
    document = file_mov_document(audio_codecs=("pcm_s24le", "aac"))
    document["audioComponent"].reverse()
    assert shape_signature(parse_shape(document)) == (
        "prores",
        (1920, 1080),
        ("pcm_s24le", "aac"),
    )


def test_shape_signature_of_an_audio_only_shape():
    assert shape_signature(parse_shape(file_wav_document())) == (
        None,
        None,
        ("pcm_s24le",),
    )


def test_equal_to_the_lowres_and_not_to_ffprobe_is_a_proxy_copy():
    assert classify_copy(PROXY_SIG, [PROXY_SIG], MOV) == PROXY


def test_different_from_the_lowres_is_genuine():
    assert classify_copy(MOV, [PROXY_SIG], MOV) == GENUINE
    assert classify_copy(MOV, [PROXY_SIG], None) == GENUINE


def test_equal_to_the_lowres_and_to_ffprobe_is_ambiguous():
    assert classify_copy(PROXY_SIG, [PROXY_SIG], PROXY_SIG) == AMBIGUOUS


def test_equal_to_the_lowres_without_ffprobe_is_ambiguous():
    assert classify_copy(PROXY_SIG, [PROXY_SIG], None) == AMBIGUOUS


def test_no_lowres_is_genuine():
    assert classify_copy(PROXY_SIG, [], MOV) == GENUINE


def test_a_lowres_without_any_description_is_no_evidence():
    assert classify_copy((None, None, ()), [(None, None, ())], None) == GENUINE

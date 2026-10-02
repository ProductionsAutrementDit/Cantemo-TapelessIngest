"""Tier 1: the ffprobe XML a ``file`` clip stored, and the proxy-copy rule."""

import pytest

from portal.plugins.TapelessIngest.wrapped.ffprobe import (
    AMBIGUOUS,
    GENUINE,
    PROXY,
    Probe,
    classify_copy,
    parse_ffprobe,
    probe_disagreement,
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


def test_leading_whitespace_and_a_bom_are_stripped():
    assert parse_ffprobe("\ufeff\n  " + ffprobe_xml()).signature == MOV
    assert parse_ffprobe("\n\t" + ffprobe_xml()).signature == MOV


def test_only_the_top_level_streams_are_counted():
    # ffprobe lists a program's streams again under <programs>.
    xml = ffprobe_xml().replace(
        "<streams>",
        "<programs><program><streams>"
        '<stream index="9" codec_name="aac" codec_type="audio"/>'
        '<stream index="8" codec_name="h264" codec_type="video" '
        'width="1" height="1"/>'
        "</streams></program></programs><streams>",
    )
    assert parse_ffprobe(xml).signature == MOV


def test_a_video_stream_without_codec_name_is_still_a_video():
    xml = ffprobe_xml().replace('codec_name="prores" ', "")
    assert parse_ffprobe(xml).signature == ("", (1920, 1080), MOV[2])


def test_ffprobe_agreeing_on_resolution_and_audio_count_is_no_disagreement():
    assert probe_disagreement(MOV, MOV) is None


def test_codec_names_are_never_compared():
    # JPEG vs mjpeg, hevc vs 'unknown': naming, not content.
    probe = ("unknown", (1920, 1080), ("aac", "unknown"))
    assert probe_disagreement(MOV, probe) is None


def test_a_resolution_mismatch_is_a_disagreement():
    problem = probe_disagreement(MOV, ("prores", (3840, 2160), MOV[2]))
    assert problem == "video resolution 1920x1080, ffprobe 3840x2160"


def test_an_audio_count_mismatch_is_a_disagreement():
    problem = probe_disagreement(MOV, ("prores", (1920, 1080), ("pcm_s24le",)))
    assert problem == "2 audio stream(s), ffprobe 1"


def test_a_video_on_one_side_only_is_a_disagreement():
    assert probe_disagreement((None, None, ("a",)), MOV[:2] + (("a",),)) == (
        "no video stream, ffprobe has one"
    )
    assert probe_disagreement(MOV, (None, None, MOV[2])) == (
        "a video stream, ffprobe has none"
    )


def test_a_still_is_video_on_both_sides():
    still = ("mjpeg", (4000, 3000), ())
    assert probe_disagreement(("jpeg", (4000, 3000), ()), still) is None


# Verbatim structure of prod's Clip.clip_xml for `file` clips (97%): the
# ffprobe <streams> and <format> wrapped in <Material umid="...">.
MATERIAL_XML = """<Material umid="de16f5f6-a9c3-4057-b734-4b40eaedba3a">
    <streams>
        <stream index="0" codec_type="data" codec_time_base="1/50" \
codec_tag_string="tmcd" codec_tag="0x64636d74"/>
        <stream index="1" codec_name="h264" codec_long_name="H.264" \
codec_type="video" codec_time_base="1/50" width="1920" height="1080" \
pix_fmt="yuv420p"/>
        <stream index="2" codec_name="pcm_s16be" codec_type="audio" \
sample_rate="48000" channels="2"/>
    </streams>
    <format filename="2015-05-05_09-58-21.mov" nb_streams="3" nb_programs="0" \
format_name="mov,mp4,m4a,3gp,3g2,mj2" format_long_name="QuickTime / MOV" \
start_time="0.000000" duration="9.135000" size="34346963" bit_rate="30079442" \
probe_score="100">
        <tag key="major_brand" value="qt  "/>
    </format>
</Material>
"""

NON_REAL_TIME_META = """<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00" \
lastUpdate="2015-05-05T09:58:21+02:00">
    <Duration value="457"/>
    <VideoFormat><VideoFrame videoCodec="AVC_1920_1080_HP@L42"/></VideoFormat>
</NonRealTimeMeta>
"""


def test_the_material_wrapper_is_read_as_ffprobe():
    probe = parse_ffprobe(MATERIAL_XML)
    assert probe.signature == ("h264", (1920, 1080), ("pcm_s16be",))
    assert probe.size == 34346963


def test_a_material_with_only_a_format_still_gives_its_size():
    xml = '<Material umid="x"><format size="12"/></Material>'
    assert parse_ffprobe(xml) == Probe((None, None, ()), 12)


def test_a_material_nesting_streams_deeper_is_not_ffprobe():
    xml = '<Material umid="x"><other><streams/><format size="1"/></other></Material>'
    assert parse_ffprobe(xml) is None


def test_sony_non_real_time_meta_is_not_ffprobe():
    assert parse_ffprobe(NON_REAL_TIME_META) is None

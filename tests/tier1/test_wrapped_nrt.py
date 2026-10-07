"""Tier 1: a proxy-copied ``xdcam`` shape stated from the Sony
NonRealTimeMeta XML stored in ``Clip.clip_xml`` (``wrapped.nrt``)."""

import pytest

from portal.plugins.TapelessIngest.wrapped.nrt import (
    XDCAM_NRT_FORMATS,
    nrt_description,
    parse_nrt,
)
from portal.plugins.TapelessIngest.wrapped.shape import build_ffprobe_document
from tests.wrapped_fakes import FS7_NRT, nrt_xml

MP4 = "mov,mp4,m4a,3gp,3g2,mj2"


# The A7S writes no VideoFormat/AudioFormat: only its Duration, timecode
# and Device.
A7S_NRT = nrt_xml(model="ILCE-7S", video=None)


def _frame(codec, fps, capture=None):
    return (
        f'<VideoFrame videoCodec="{codec}" '
        f'captureFps="{capture or fps}" formatFps="{fps}"/>'
    )


FORMATS = [
    # (xml, extension, key, format, video (index, codec, time_base),
    #  audio indices, audio codec, channels)
    (
        FS7_NRT,
        "MXF",
        ("PXW-FS7", "AVC100CBG_1920_1080_H422IP@L41", "25p", ("LPCM24",) * 8),
        "mxf",
        (1, "h264", [1, 25]),
        range(2, 10),
        "pcm_s24le",
        1,
    ),
    (
        nrt_xml(
            model="PMW-300",
            video=_frame("MPEG2HD50CBR_1920_1080_422P@HL", "50i", "50i"),
        ),
        "MXF",
        ("PMW-300", "MPEG2HD50CBR_1920_1080_422P@HL", "50i", ("LPCM24",) * 8),
        "mxf",
        (1, "mpeg2video", [1, 25]),
        range(2, 10),
        "pcm_s24le",
        1,
    ),
    (
        nrt_xml(
            model="PMW-EX3",
            video=_frame("MPEG2HD35_1920_1080_MP@HL", "25p"),
            audio_codec="LPCM16",
            ports=2,
        ),
        "MP4",
        ("PMW-EX3", "MPEG2HD35_1920_1080_MP@HL", "25p", ("LPCM16",) * 2),
        MP4,
        (0, "mpeg2video", [1, 2500]),
        range(1, 3),
        "pcm_s16be",
        1,
    ),
    (
        nrt_xml(
            model="PMW-TD300",
            video=_frame("MPEG2HD35_1920_1080_MP@HL", "25p"),
            audio_codec="LPCM16",
            ports=4,
        ),
        "MP4",
        ("PMW-TD300", "MPEG2HD35_1920_1080_MP@HL", "25p", ("LPCM16",) * 4),
        MP4,
        (0, "mpeg2video", [1, 2500]),
        range(1, 5),
        "pcm_s16be",
        1,
    ),
    (
        nrt_xml(
            model="PMW-350",
            video=_frame("MPEG2HD35_1920_1080_MP@HL", "50i"),
            audio_codec="LPCM16",
            ports=4,
        ),
        "MP4",
        ("PMW-350", "MPEG2HD35_1920_1080_MP@HL", "50i", ("LPCM16",) * 4),
        MP4,
        (0, "mpeg2video", [1, 2500]),
        range(1, 5),
        "pcm_s16be",
        1,
    ),
    (
        A7S_NRT,
        "MP4",
        ("ILCE-7S", None, None, ()),
        MP4,
        (0, "h264", [1, 25000]),
        range(1, 2),
        "pcm_s16be",
        2,
    ),
]


@pytest.mark.parametrize(
    "xml, extension, key, container, video, audio_indices, audio_codec, channels",
    FORMATS,
)
def test_each_measured_format_is_described(
    xml, extension, key, container, video, audio_indices, audio_codec, channels
):
    nrt = parse_nrt(xml)
    assert nrt == {"key": key, "frames": 218, "tc_fps": "25"}

    description, problem = nrt_description(nrt, extension)

    assert problem is None
    index, codec, time_base = video
    assert description == {
        "format": container,
        "duration": "8.720000",
        "streams": [
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
        + [
            {
                "index": n,
                "kind": "audio",
                "codec": audio_codec,
                "channels": channels,
                "sample_rate": 48000,
            }
            for n in audio_indices
        ],
    }


def test_the_table_has_the_six_measured_formats():
    assert len(XDCAM_NRT_FORMATS) == 6


def test_a_description_is_a_copy_the_plan_may_keep():
    description, _ = nrt_description(parse_nrt(FS7_NRT), "MXF")
    description["streams"][0]["codec"] = "changed"
    again, _ = nrt_description(parse_nrt(FS7_NRT), "MXF")
    assert again["streams"][0]["codec"] == "h264"


def test_the_duration_is_the_frames_at_25_per_second():
    nrt = parse_nrt(nrt_xml(duration='<Duration value="4791"/>'))
    description, _ = nrt_description(nrt, "MXF")
    assert description["duration"] == "191.640000"


def test_an_unknown_format_key_has_no_reference():
    nrt = parse_nrt(nrt_xml(model="PXW-Z190"))
    assert nrt_description(nrt, "MXF") == (
        None,
        "NRT format ('PXW-Z190', 'AVC100CBG_1920_1080_H422IP@L41', '25p', "
        "('LPCM24', 'LPCM24', 'LPCM24', 'LPCM24', 'LPCM24', 'LPCM24', 'LPCM24', "
        "'LPCM24'), 'MXF') has no reference",
    )


def test_the_extension_is_part_of_the_key():
    description, problem = nrt_description(parse_nrt(FS7_NRT), "MP4")
    assert description is None
    assert problem.startswith("NRT format ('PXW-FS7', ")
    assert problem.endswith("'MP4') has no reference")


def test_no_duration_is_a_problem():
    nrt = parse_nrt(nrt_xml(duration=""))
    assert nrt["frames"] is None
    assert nrt_description(nrt, "MXF") == (None, "NRT has no Duration")


def test_a_duration_that_is_not_a_frame_count_is_no_duration():
    nrt = parse_nrt(nrt_xml(duration='<Duration value="soon"/>'))
    assert nrt_description(nrt, "MXF") == (None, "NRT has no Duration")


def test_a_timecode_rate_other_than_25_is_a_problem():
    nrt = parse_nrt(nrt_xml(tc_fps="50"))
    assert nrt_description(nrt, "MXF") == (None, "NRT timecode rate 50 is not 25")


def test_no_timecode_rate_is_a_problem():
    nrt = parse_nrt(nrt_xml(tc_fps=None))
    assert nrt["tc_fps"] is None
    assert nrt_description(nrt, "MXF") == (None, "NRT timecode rate None is not 25")


@pytest.mark.parametrize(
    "xml",
    [
        None,
        "",
        "not xml <",
        '<ffprobe><streams/><format format_name="mxf"/></ffprobe>',
        '<Material umid="x"><format size="12"/></Material>',
    ],
)
def test_anything_but_a_nonrealtimemeta_is_not_nrt(xml):
    assert parse_nrt(xml) is None


def test_no_nrt_is_named():
    assert nrt_description(None, "MXF") == (None, "no NRT XML")


def test_an_fs7_mxf_document_numbers_mxf_streams_from_one():
    description, _ = nrt_description(parse_nrt(FS7_NRT), "MXF")
    document = build_ffprobe_document(description, "VX-F1", 8_720_000)
    assert document["containerComponent"] == {
        "format": "mxf",
        "duration": {
            "samples": 8_720_000,
            "timeBase": {"numerator": 1, "denominator": 1_000_000},
        },
        "file": [{"id": "VX-F1"}],
    }
    (video,) = document["videoComponent"]
    assert video == {
        "codec": "h264",
        "resolution": {"width": 1920, "height": 1080},
        "averageFrameRate": {"numerator": 25, "denominator": 1},
        "timeBase": {"numerator": 1, "denominator": 25},
        "pixelAspectRatio": {"horizontal": 1, "vertical": 1},
        "essenceStreamId": 1,
        "file": [{"id": "VX-F1"}],
    }
    assert [a["essenceStreamId"] for a in document["audioComponent"]] == list(
        range(2, 10)
    )
    assert {
        (a["codec"], a["channelCount"], a["timeBase"]["denominator"])
        for a in document["audioComponent"]
    } == {("pcm_s24le", 1, 48000)}

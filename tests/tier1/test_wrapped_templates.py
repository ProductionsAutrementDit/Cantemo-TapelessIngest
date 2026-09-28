"""Tier 1: P2 technical templates for proxy-copied wrapped shapes."""

import json

import pytest

from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
from portal.plugins.TapelessIngest.wrapped.templates import (
    Timing,
    is_proxy_copy,
    common_template,
    load_templates,
    signature_difference,
    signature,
    strip_for_template,
    template_key,
    timing,
)
from tests.wrapped_fakes import (
    p2_clip_metadata,
    proxy_copy_document,
    wrapped_p2_document,
)

# template_key


def test_avc_intra_at_100_mbps_or_more_is_class_100():
    assert template_key(p2_clip_metadata()) == "AVC-I_1080/50i|50i|AVC-I100"


def test_avc_intra_below_80_mbps_is_class_50():
    # 50 Mb/s over 497 frames at 1/25 = 19.88 s
    metadata = p2_clip_metadata(data_size=str(50_000_000 * 1988 // 800))
    assert template_key(metadata) == "AVC-I_1080/50i|50i|AVC-I50"


def test_the_class_threshold_is_80_mbps():
    at = p2_clip_metadata(data_size=str(80_000_000 * 1988 // 800))
    below = p2_clip_metadata(data_size=str(80_000_000 * 1988 // 800 - 1))
    assert template_key(at).endswith("|AVC-I100")
    assert template_key(below).endswith("|AVC-I50")


def test_the_bitrate_uses_the_edit_unit():
    # the same bytes over 497 frames at 1/50 (9.94 s) double the bitrate
    metadata = p2_clip_metadata(
        video_codec="AVC-I_720/50p",
        framerate="50p",
        EditUnit="1/50",
        data_size=str(50_000_000 * 1988 // 800),
    )
    assert template_key(metadata) == "AVC-I_720/50p|50p|AVC-I100"


def test_dv_formats_get_no_class_suffix_and_need_no_data_size():
    metadata = p2_clip_metadata(video_codec="DV100_1080/50i", data_size=None)
    assert template_key(metadata) == "DV100_1080/50i|50i"


@pytest.mark.parametrize(
    "missing", ["video_codec", "framerate", "duration", "EditUnit", "data_size"]
)
def test_incomplete_metadata_has_no_key(missing):
    assert template_key(p2_clip_metadata(**{missing: None})) is None
    assert template_key(p2_clip_metadata(**{missing: ""})) is None


@pytest.mark.parametrize("duration", ["0", "-3", "abc"])
def test_a_duration_that_is_not_positive_has_no_key(duration):
    assert template_key(p2_clip_metadata(duration=duration)) is None


@pytest.mark.parametrize("edit_unit", ["25", "1/0", "x/25"])
def test_a_malformed_edit_unit_has_no_key(edit_unit):
    assert template_key(p2_clip_metadata(EditUnit=edit_unit)) is None


# timing


def test_timing_of_vx_35313():
    assert timing(p2_clip_metadata()) == Timing(
        frames=497, num=1, den=25, start_tc_frames=1657612
    )


def test_timing_of_vx_10019():
    metadata = p2_clip_metadata(duration="39", timecode_start="00:30:59:23")
    assert timing(metadata) == Timing(frames=39, num=1, den=25, start_tc_frames=46498)


def test_timing_counts_timecode_frames_at_the_edit_unit_rate():
    metadata = p2_clip_metadata(EditUnit="1/50", timecode_start="00:00:01:49")
    assert timing(metadata).start_tc_frames == 99


@pytest.mark.parametrize(
    "timecode",
    ["18:25:04", "18:25:04:25", "18;25;04;12", "aa:25:04:12", "18:60:04:12", ""],
)
def test_a_malformed_timecode_is_refused(timecode):
    with pytest.raises(ValueError):
        timing(p2_clip_metadata(timecode_start=timecode))


def test_a_missing_timecode_is_refused():
    with pytest.raises(ValueError):
        timing(p2_clip_metadata(timecode_start=None))


# is_proxy_copy


def test_a_proxy_copied_shape_is_recognised():
    assert is_proxy_copy(parse_shape(proxy_copy_document()))


def test_the_mp4_mime_type_alone_marks_a_proxy_copy():
    document = wrapped_p2_document()
    document["mimeType"] = ["video/mp4"]
    assert is_proxy_copy(parse_shape(document))


def test_the_mov_mp4_container_alone_marks_a_proxy_copy():
    document = wrapped_p2_document()
    document["containerComponent"]["format"] = "mov,mp4,m4a,3gp,3g2,mj2"
    assert is_proxy_copy(parse_shape(document))


def test_a_genuine_wrapped_shape_is_not_a_proxy_copy():
    assert not is_proxy_copy(parse_shape(wrapped_p2_document()))


# signature


def test_identical_content_has_the_same_signature_whatever_its_identity():
    one = wrapped_p2_document(shape_id="VX-1", file_id="F1", name="A.MXF")
    two = wrapped_p2_document(shape_id="VX-2", file_id="F2", name="B.MXF")
    two["containerComponent"]["duration"] = {"samples": 999}
    assert signature(parse_shape(one)) == signature(parse_shape(two))


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["containerComponent"].update(format="mxf_d10"),
        lambda d: d.update(mimeType=["application/octet-stream"]),
        lambda d: d["videoComponent"][0].update(resolution={"width": 1920}),
        lambda d: d["videoComponent"][0].update(pixelFormat="yuv420p10le"),
        lambda d: d["audioComponent"][0].update(channelCount=2),
        lambda d: d["audioComponent"].pop(),
    ],
)
def test_a_content_difference_changes_the_signature(change):
    changed = wrapped_p2_document()
    change(changed)
    base = signature(parse_shape(wrapped_p2_document()))
    assert signature(parse_shape(changed)) != base
    hash(base)


# strip_for_template


def test_strip_keeps_container_video_first_audio_and_mime():
    stripped = strip_for_template(wrapped_p2_document())
    assert set(stripped) == {
        "mimeType",
        "containerComponent",
        "videoComponent",
        "audioComponent",
    }
    assert stripped["mimeType"] == ["application/mxf"]
    assert stripped["containerComponent"] == {"format": "mxf"}
    assert stripped["videoComponent"] == [
        {"codec": "dvvideo", "resolution": {"width": 1440, "height": 1080}}
    ]
    assert stripped["audioComponent"] == [{"codec": "pcm_s16le", "channelCount": 1}]


def test_strip_drops_every_identity_and_timing_key():
    document = wrapped_p2_document()
    extra = {
        "startTimestamp": 1,
        "startTimecode": 2,
        "firstSMPTETimecode": "x",
        "pid": 3,
        "mediaInfo": {"a": 1},
    }
    for body in [
        document["containerComponent"],
        *document["videoComponent"],
        *document["audioComponent"],
    ]:
        body.update(extra)
    stripped = strip_for_template(document)
    dropped = {
        "id",
        "file",
        "metadata",
        "duration",
        "essenceStreamId",
        "itemTrack",
        *extra,
    }
    for body in [
        stripped["containerComponent"],
        *stripped["videoComponent"],
        *stripped["audioComponent"],
    ]:
        assert not dropped & set(body)


def test_strip_takes_the_first_audio_in_stream_order():
    document = wrapped_p2_document()
    document["audioComponent"][0]["codec"] = "first"
    document["audioComponent"].reverse()
    assert strip_for_template(document)["audioComponent"] == [
        {"codec": "first", "channelCount": 1}
    ]


# load_templates


def test_a_missing_template_file_means_no_templates(tmp_path):
    assert load_templates(tmp_path / "absent.json") == {}


def test_templates_are_read_from_the_json_file(tmp_path):
    entry = {
        "template": strip_for_template(wrapped_p2_document()),
        "reference_item": "VX-1",
        "references": 30,
        "share": 1.0,
    }
    path = tmp_path / "p2_templates.json"
    path.write_text(json.dumps({"DV100_1080/50i|50i": entry}))
    assert load_templates(path) == {"DV100_1080/50i|50i": entry}


# common_template


def _varying(packets, bitrate, audio_packets=(10, 10, 10, 10)):
    document = wrapped_p2_document()
    document["containerComponent"].update(numberOfPackets=packets, bitrate=bitrate)
    document["videoComponent"][0].update(numberOfPackets=packets, bitrate=bitrate)
    for body, count in zip(document["audioComponent"], audio_packets):
        body["numberOfPackets"] = count
    return document


def test_values_that_vary_across_references_are_dropped():
    template, dropped = common_template(
        [_varying(218, 114_000_000), _varying(497, 113_500_000)]
    )
    expected = strip_for_template(wrapped_p2_document())
    # identical in both references: not per-file, so kept
    expected["audioComponent"][0]["numberOfPackets"] = 10
    assert template == expected
    assert dropped == {
        "containerComponent": ["bitrate", "numberOfPackets"],
        "videoComponent": ["bitrate", "numberOfPackets"],
        "audioComponent": [],
    }


def test_a_value_varying_between_tracks_of_one_reference_is_dropped():
    template, dropped = common_template([_varying(1, 2, audio_packets=(1, 1, 1, 2))])
    assert "numberOfPackets" not in template["audioComponent"][0]
    assert dropped["audioComponent"] == ["numberOfPackets"]
    assert template["containerComponent"]["numberOfPackets"] == 1


def test_a_value_missing_from_one_reference_is_dropped():
    one = _varying(1, 2)
    two = _varying(1, 2)
    del two["containerComponent"]["bitrate"]
    template, dropped = common_template([one, two])
    assert "bitrate" not in template["containerComponent"]
    assert dropped["containerComponent"] == ["bitrate"]


def test_identical_references_lose_nothing():
    template, dropped = common_template([_varying(1, 2), _varying(1, 2)])
    assert template == strip_for_template(_varying(1, 2))
    assert dropped == {
        "containerComponent": [],
        "videoComponent": [],
        "audioComponent": [],
    }


# signature_difference


def test_the_fields_two_signatures_differ_in_are_named():
    one = wrapped_p2_document()
    two = wrapped_p2_document()
    two["videoComponent"][0]["pixelFormat"] = "yuv420p10le"
    two["audioComponent"][1]["channelCount"] = 2
    assert signature_difference(
        signature(parse_shape(one)), signature(parse_shape(two))
    ) == ["audio.channelCount", "video.pixelFormat"]


def test_identical_signatures_differ_in_nothing():
    base = signature(parse_shape(wrapped_p2_document()))
    assert signature_difference(base, base) == []

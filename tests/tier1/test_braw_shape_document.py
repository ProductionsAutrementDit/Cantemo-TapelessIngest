"""Tier 1: the ShapeDocument `braw` posts, built from STORED metadatas.

Vidispine cannot read a `.braw`, so the plugin states the whole
`original` shape (the RED dotted-timecode route, `_post_shape_document`).
The builder is pure: the stored `ClipMetadata` strings in, a dict out —
brawprobe is never run at import.
"""

import json
import os

import pytest

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.providers import braw as braw_module

FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "braw"
)
ANCHOR = {"file_id": "VX-501", "path": "2024/AH_x/0979-Gimbal-T1096_09091448_C004.braw"}


def _stored(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as handle:
        return braw_module.metadatas_from_probe(json.load(handle), name)


def _build(metadatas, main_file=ANCHOR, extra_files=()):
    return braw_module.Provider.buildShapeDocument(
        main_file, list(extra_files), metadatas
    )


def test_the_6k_golden_document():
    """Happy-path row: video and audio, 25 fps playback of a 48 fps shoot."""
    assert _build(_stored("0979-Gimbal-T1096_09091448_C004")) == {
        "containerComponent": {
            "file": [{"id": "VX-501"}],
            "format": "braw",
            "duration": {
                "samples": 981,
                "timeBase": {"numerator": 1, "denominator": 25},
            },
        },
        "videoComponent": [
            {
                "file": [{"id": "VX-501"}],
                "duration": {
                    "samples": 981,
                    "timeBase": {"numerator": 1, "denominator": 25},
                },
                "resolution": {"width": 6048, "height": 3200},
                "codec": "braw",
                "averageFrameRate": {"numerator": 25, "denominator": 1},
                "fieldOrder": "progressive",
                "pixelAspectRatio": {"horizontal": 1, "vertical": 1},
            }
        ],
        "audioComponent": [
            {
                "file": [{"id": "VX-501"}],
                "codec": "pcm_s24le",
                "channelCount": 2,
                "blockAlign": 6,
                "bitrate": 2304000,
                "timeBase": {"numerator": 1, "denominator": 48000},
                "duration": {
                    "samples": 979200,
                    "timeBase": {"numerator": 1, "denominator": 48000},
                },
                "itemTrack": "A1",
            }
        ],
        "mimeType": ["video/x-braw"],
    }


def test_the_pyxis_document_has_no_audio_component():
    """No-audio row."""
    document = _build(_stored("1542-PyxisT2055_02210929_C001"))
    assert "audioComponent" not in document
    assert document["videoComponent"][0]["resolution"] == {
        "width": 8192,
        "height": 5360,
    }


@pytest.mark.parametrize(
    "name",
    [
        "0979-12K-T1273_09100925_C001",
        "1543-6k-600050_01190744_C001",
        "A009_09062022_C001",
    ],
)
def test_every_body_composes(name):
    document = _build(_stored(name))
    assert document["videoComponent"][0]["averageFrameRate"] == {
        "numerator": 25,
        "denominator": 1,
    }


def test_a_23976_clip_states_its_rational_rate():
    metadatas = _stored("A009_09062022_C001")
    metadatas.update(braw_probe_fps_num="24000", braw_probe_fps_den="1001")
    video = _build(metadatas)["videoComponent"][0]
    assert video["averageFrameRate"] == {"numerator": 24000, "denominator": 1001}
    assert video["duration"]["timeBase"] == {"numerator": 1001, "denominator": 24000}


def test_an_anamorphic_clip_states_no_pixel_aspect_ratio():
    metadatas = _stored("A009_09062022_C001")
    metadatas["braw_anamorphic_enable"] = "1"
    assert "pixelAspectRatio" not in _build(metadatas)["videoComponent"][0]


def test_no_anchor_file_id_is_refused():
    with pytest.raises(TapelessIngestException, match="file id"):
        _build(
            _stored("A009_09062022_C001"), main_file={"path": "x.braw", "file_id": None}
        )


def test_an_extra_file_is_refused():
    with pytest.raises(TapelessIngestException, match="extra media"):
        _build(
            _stored("A009_09062022_C001"),
            extra_files=[{"type": "audio", "path": "x.wav", "file_id": "VX-9"}],
        )


@pytest.mark.parametrize(
    "key",
    [
        "braw_probe_width",
        "braw_probe_height",
        "braw_probe_frame_count",
        "braw_probe_fps_num",
        "braw_probe_fps_den",
        "braw_probe_audio_channels",
    ],
)
@pytest.mark.parametrize("damage", ["delete", "zero", "junk"])
def test_a_missing_or_unusable_value_is_refused_by_name(key, damage):
    metadatas = _stored("0979-Gimbal-T1096_09091448_C004")
    if damage == "delete":
        del metadatas[key]
    elif damage == "zero":
        if key == "braw_probe_audio_channels":
            pytest.skip("zero channels is the no-audio row, not a refusal")
        metadatas[key] = "0"
    else:
        metadatas[key] = "n/a"
    with pytest.raises(TapelessIngestException, match=key):
        _build(metadatas)


@pytest.mark.parametrize(
    "key",
    [
        "braw_probe_audio_sample_rate",
        "braw_probe_audio_bits",
        "braw_probe_audio_samples",
    ],
)
def test_audio_without_its_parameters_is_refused(key):
    metadatas = _stored("0979-Gimbal-T1096_09091448_C004")
    metadatas[key] = ""
    with pytest.raises(TapelessIngestException, match=key):
        _build(metadatas)


def test_the_builder_runs_no_probe(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("brawprobe must not run at import")

    metadatas = _stored("0979-Gimbal-T1096_09091448_C004")
    monkeypatch.setattr(braw_module.sp, "run", _forbidden)
    monkeypatch.setattr(braw_module, "resolve_brawprobe_path", _forbidden)
    _build(metadatas)


@pytest.mark.parametrize("enable", ["", "0", "false", "FALSE", "off", "No", " 0 "])
@pytest.mark.parametrize("ratio", ["", "none", "None", "OFF"])
def test_these_values_mean_square_pixels(enable, ratio):
    metadatas = _stored("A009_09062022_C001")
    metadatas.update(braw_anamorphic_enable=enable, braw_anamorphic=ratio)
    video = _build(metadatas)["videoComponent"][0]
    assert video["pixelAspectRatio"] == {"horizontal": 1, "vertical": 1}


@pytest.mark.parametrize(
    "enable, ratio", [("1", ""), ("true", ""), ("on", "none"), ("0", "1.33x")]
)
def test_these_values_mean_anamorphic(enable, ratio):
    metadatas = _stored("A009_09062022_C001")
    metadatas.update(braw_anamorphic_enable=enable, braw_anamorphic=ratio)
    assert "pixelAspectRatio" not in _build(metadatas)["videoComponent"][0]


@pytest.mark.parametrize("extra", ["x.wav", None, 42, ["x"]])
def test_an_extra_that_is_not_a_dict_is_still_refused_by_name(extra):
    with pytest.raises(TapelessIngestException, match="extra media"):
        _build(_stored("A009_09062022_C001"), extra_files=[extra])


def test_audio_that_is_not_whole_bytes_is_refused():
    metadatas = _stored("0979-Gimbal-T1096_09091448_C004")
    metadatas.update(braw_probe_audio_bits="20", braw_probe_audio_channels="2")
    with pytest.raises(TapelessIngestException, match="20-bit"):
        _build(metadatas)

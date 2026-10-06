"""Tier 1: what `braw` stores from one brawprobe run.

The fixtures under `tests/fixtures/braw/` are REAL brawprobe outputs,
captured on the Mac from NAS clips of five bodies: Cinema Camera 6K
(`0979-Gimbal…C004`, `…C005`), URSA Mini Pro 12K, PYXIS 12K, Pocket
Cinema Camera 6K Pro and URSA Broadcast G2. brawprobe itself already
leaves the embedded LUT data out.

DB-free: `sp.run` is replaced by the canned JSON, as for RED.
"""

import copy
import json
import logging
import os
import uuid

import pytest

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.providers import braw as braw_module
from portal.plugins.TapelessIngest.providers.providers import (
    MAIN_FILE_YIELDS_VIDEO,
    main_file_declares_no_video,
)

FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "braw"
)
SIX_K = "0979-Gimbal-T1096_09091448_C004"
SIX_K_NEXT = "0979-Gimbal-T1096_09091449_C005"
PYXIS = "1542-PyxisT2055_02210929_C001"
G2 = "A009_09062022_C001"
ALL_BODIES = (
    SIX_K,
    "0979-12K-T1273_09100925_C001",
    PYXIS,
    "1543-6k-600050_01190744_C001",
    G2,
)
MEDIA = "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2024/AH_x/0979-Gimbal-T1096_09091448_C004.braw"


def _probe(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as handle:
        return json.load(handle)


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def brawprobe(monkeypatch):
    """Replace the binary with canned output; return the recorded calls."""
    calls = []

    def _install(stdout="", stderr="", returncode=0):
        def _fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return _Completed(stdout=stdout, stderr=stderr, returncode=returncode)

        monkeypatch.setattr(
            braw_module, "resolve_brawprobe_path", lambda: "/usr/local/bin/brawprobe"
        )
        monkeypatch.setattr(braw_module.sp, "run", _fake_run)
        return calls

    return _install


class _MediaFile:
    def __init__(self, name, file_id="VX-900"):
        self._name = name
        self._id = file_id

    def getFileName(self):
        return self._name

    def getId(self):
        return self._id


def _provider(monkeypatch):
    provider = braw_module.Provider()
    monkeypatch.setattr(
        provider,
        "get_file_absolute_path",
        lambda media_file, context=None: "/abs/" + media_file.getFileName(),
    )
    return provider


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_the_6k_offspeed_clip_stores_every_key_and_the_common_ones(brawprobe):
    """Happy-path row: 6K, audio, off-speed 48 at 25."""
    calls = brawprobe(stdout=json.dumps(_probe(SIX_K)))
    metadatas = braw_module.Provider().getAllClipMetadatas(MEDIA, {})

    assert calls[0][0] == ["/usr/local/bin/brawprobe", "--", MEDIA]
    assert "shell" not in calls[0][1]
    assert len(calls) == 1

    probe = _probe(SIX_K)
    for key in probe["metadata"]["clip"]:
        assert "braw_" + key in metadatas
    for key in probe["metadata"]["frame0"]:
        if key.startswith(
            ("lens_shading_", "lens_distortion_correction_polynomial")
        ) and isinstance(probe["metadata"]["frame0"][key], list):
            continue
        prefix = "braw_frame0_" if key in probe["metadata"]["clip"] else "braw_"
        assert prefix + key in metadatas

    assert metadatas["clipname"] == SIX_K
    assert metadatas["timecode"] == "14:48:08:06"
    assert metadatas["framerate"] == "25/1"
    assert metadatas["duration"] == "39.24"
    assert metadatas["shooting_date"] == "2024-09-09"
    assert metadatas["device_manufacturer"] == "Blackmagic Design"
    assert metadatas["device_model"] == "Blackmagic Cinema Camera 6K"
    assert metadatas["device_serial"] == "24e55415-41b1-4df5-977e-0102659a2290"
    assert metadatas["video_codec"] == "Blackmagic RAW 12:1"
    assert metadatas["aspect_ratio"] == "189:100"

    assert metadatas["braw_probe_sensor_rate_num"] == "48"
    assert metadatas["braw_probe_sensor_rate_den"] == "1"
    assert metadatas["braw_probe_offspeed"] == "true"
    assert metadatas["braw_probe_audio_channels"] == "2"
    assert metadatas["braw_probe_audio_sample_rate"] == "48000"
    assert metadatas["braw_probe_audio_bits"] == "24"
    assert metadatas["braw_crop_size"] == "6048x3200"
    assert metadatas["braw_sensor_rate"] == "48/1"
    assert metadatas["braw_frame0_analog_gain"] == "1"
    assert metadatas["braw_shutter_value"] == "180°"
    assert all(isinstance(v, str) and len(v) <= 200 for v in metadatas.values())


@pytest.mark.parametrize("body", ALL_BODIES + (SIX_K_NEXT,))
def test_every_fixture_flattens_to_strings_within_the_column(body):
    metadatas = braw_module.metadatas_from_probe(_probe(body), body)
    assert all(isinstance(v, str) for v in metadatas.values())
    assert all(len(k) <= 200 and len(v) <= 200 for k, v in metadatas.items())
    assert not any(
        k.startswith("braw_post_3dlut_") and k.endswith("_data") for k in metadatas
    )


def test_the_known_inventory_is_exactly_what_the_five_bodies_store():
    """`KNOWN_METADATA_KEYS` (what the mapping form offers before any scan)
    is measured, not invented: it equals the union over the fixtures."""
    stored = set()
    for body in ALL_BODIES:
        metadatas = braw_module.metadatas_from_probe(_probe(body), body)
        stored |= {
            k
            for k in metadatas
            if k.startswith("braw_") and not k.startswith("braw_probe_")
        }
    assert stored == set(braw_module.KNOWN_METADATA_KEYS)


def test_the_pyxis_records_no_audio():
    metadatas = braw_module.metadatas_from_probe(_probe(PYXIS), PYXIS)
    assert metadatas["braw_probe_audio_channels"] == "0"


def test_an_on_speed_clip_is_not_offspeed():
    metadatas = braw_module.metadatas_from_probe(_probe(G2), G2)
    assert metadatas["braw_probe_offspeed"] == "false"
    assert metadatas["framerate"] == "25/1"


def test_a_clip_whose_sensor_rate_came_from_a_later_frame_is_stored():
    """Damaged-clip row: frame 0 read, a later frame gave the sensor rate.
    The provider only needs the top-level fields."""
    probe = _probe(SIX_K)
    del probe["metadata"]["frame0"]["sensor_rate"]
    metadatas = braw_module.metadatas_from_probe(probe, SIX_K)
    assert metadatas["braw_probe_sensor_rate_num"] == "48"
    assert "braw_sensor_rate" not in metadatas

    probe["metadata"]["frame0"] = {}
    probe["frame0_readable"] = False
    metadatas = braw_module.metadatas_from_probe(probe, SIX_K)
    assert metadatas["braw_probe_frame0_readable"] == "false"
    assert metadatas["umid"]


def test_a_missing_technical_field_refuses_the_clip():
    probe = _probe(SIX_K)
    del probe["frame_count"]
    with pytest.raises(TapelessIngestException, match="frame_count"):
        braw_module.metadatas_from_probe(probe, SIX_K)


# --------------------------------------------------------------------------
# Snapping: brawdump's snapRate, exactly
# --------------------------------------------------------------------------


@pytest.mark.parametrize("num, den", braw_module.KNOWN_RATES)
def test_every_table_rate_snaps_to_itself(num, den):
    assert braw_module.snap_rate(num / den) == (num, den)


@pytest.mark.parametrize(
    "fps, expected",
    [
        (29.970032, (30000, 1001)),  # Pocket 6K G2's frame rate
        (29.970030, (30000, 1001)),  # ... and its sensor rate
        (23.98, (24000, 1001)),
        (23.976, (24000, 1001)),
        (24.005, (24, 1)),
        (59.94, (60000, 1001)),
        (72.0, (72, 1)),  # PYXIS sensor rate: not in the table
        (12.5, (25, 2)),
        (33.3125, (33313, 1000)),  # llround: half away from zero, not to even
        (24.02, (1201, 50)),  # just outside the tolerance
    ],
)
def test_snapping_matches_brawdump(fps, expected):
    assert braw_module.snap_rate(fps) == expected


def test_offspeed_is_brawprobes_sensor_rate_against_its_frame_rate():
    probe = _probe(G2)
    assert braw_module.metadatas_from_probe(probe, G2)["braw_probe_offspeed"] == "false"
    probe.update(sensor_rate_num=50, sensor_rate_den=1, sensor_rate_reported=50.0)
    assert braw_module.metadatas_from_probe(probe, G2)["braw_probe_offspeed"] == "true"


@pytest.mark.parametrize("body", ALL_BODIES + (SIX_K_NEXT,))
def test_the_python_snap_agrees_with_brawprobe_on_every_fixture(body):
    """One implementation of brawdump's rule is the authority (brawprobe);
    the Python port must agree with it on every real output."""
    probe = _probe(body)
    assert braw_module.snap_rate(probe["fps_reported"]) == (
        probe["fps_num"],
        probe["fps_den"],
    )
    assert braw_module.snap_rate(probe["sensor_rate_reported"]) == (
        probe["sensor_rate_num"],
        probe["sensor_rate_den"],
    )


def test_brawprobes_rate_is_stored_and_a_disagreement_only_warns(caplog):
    probe = _probe(G2)
    probe["fps_reported"] = 30.0  # would snap to 30/1 here
    with caplog.at_level(logging.WARNING, logger=braw_module.log.name):
        metadatas = braw_module.metadatas_from_probe(probe, G2)
    assert metadatas["framerate"] == "25/1"
    assert metadatas["braw_probe_fps_num"] == "25"
    assert any("brawprobe snapped fps" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "field", ["fps_num", "fps_den", "sensor_rate_num", "sensor_rate_den"]
)
@pytest.mark.parametrize("value", [0, -25, None, 2.5, "25"])
def test_an_unusable_snapped_rate_is_refused_not_divided_by(field, value):
    probe = _probe(G2)
    probe[field] = value
    with pytest.raises(TapelessIngestException, match=field):
        braw_module.metadatas_from_probe(probe, G2)


def test_a_tiny_reported_rate_does_not_crash_the_cross_check():
    """0.0004 snaps to 0/1 in Python: a check, never a division."""
    probe = _probe(G2)
    probe["fps_reported"] = 0.0004
    assert braw_module.metadatas_from_probe(probe, G2)["framerate"] == "25/1"


def test_a_numeric_timecode_is_coerced():
    probe = _probe(G2)
    probe["timecode"] = 12345
    assert braw_module.metadatas_from_probe(probe, G2)["timecode"] == "12345"


@pytest.mark.parametrize("value", [[1, 2], {"a": 1}, True])
def test_a_timecode_that_is_no_scalar_is_refused(value):
    probe = _probe(G2)
    probe["timecode"] = value
    with pytest.raises(TapelessIngestException, match="timecode"):
        braw_module.metadatas_from_probe(probe, G2)


def test_an_unparseable_date_keeps_the_umid_and_stores_no_shooting_date(caplog):
    probe = _probe(SIX_K)
    probe["metadata"]["clip"]["date_recorded"] = "unknown"
    with caplog.at_level(logging.WARNING, logger=braw_module.log.name):
        metadatas = braw_module.metadatas_from_probe(probe, SIX_K)
    assert metadatas["umid"]
    assert "shooting_date" not in metadatas
    assert any("unknown" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "raw", ["2024:09:09", "2024-09-09", "2024/09/09", " 2024:09:09 "]
)
def test_the_three_date_forms(raw):
    probe = _probe(SIX_K)
    probe["metadata"]["clip"]["date_recorded"] = raw
    assert braw_module.metadatas_from_probe(probe, SIX_K)["shooting_date"] == (
        "2024-09-09"
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("14.48.08.06", "14:48:08:06"),
        ("14:48:08:06", "14:48:08:06"),
        ("01:00:00;12", "01:00:00;12"),
    ],
)
def test_timecode_normalisation(raw, expected):
    assert braw_module.normalise_timecode(raw) == expected


# --------------------------------------------------------------------------
# Flattening, exclusions, truncation
# --------------------------------------------------------------------------


def test_flattening_trims_keys_and_prefixes_frame0_collisions_only():
    stored = braw_module.flatten_metadata(
        {" iso ": 400, "analog_gain": 1.0, "crop_size": [6048, 3200]},
        {"analog_gain": 2, "sensor_rate": [48, 1], "aperture ": "f4.0", "iso": 800},
    )
    assert stored == {
        "braw_iso": "400",
        "braw_analog_gain": "1",
        "braw_crop_size": "6048x3200",
        "braw_frame0_analog_gain": "2",
        "braw_sensor_rate": "48/1",
        "braw_aperture": "f4.0",
        "braw_frame0_iso": "800",
    }


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("crop_origin", [16, 8], "16x8"),
        ("safe_area", [0, 0, 0], "0x0x0"),
        ("x", [1, 2, 3, 4], "1x2x3x4"),
        ("sensor_rate", [50, 1], "50/1"),
        ("sensor_line_time", 5.70776272, "5.70776272"),
        ("good_take", "false", "false"),
        ("empty", None, ""),
        ("one", [7], "[7]"),
        ("five", [1, 2, 3, 4, 5], "[1,2,3,4,5]"),
        ("mixed", [1, "a"], '[1,"a"]'),
    ],
)
def test_stringify(key, value, expected):
    assert braw_module.stringify(key, value) == expected


def test_exclusions_drop_lut_data_and_lens_arrays_but_keep_their_scalars():
    stored = braw_module.flatten_metadata(
        {
            "post_3dlut_embedded_data": [0, 0, 60],
            "post_3dlut_sidecar_data": "x",
            "post_3dlut_embedded_name": "Gen 5.cube",
            "post_3dlut_embedded_title": "Gen 5",
            "post_3dlut_embedded_size": 33,
            "post_3dlut_mode": "Disabled",
            "lens_shading_enable": 1,
        },
        {
            "lens_shading_points": [0] * 112,
            "lens_shading_distances_in_millimetres": [0] * 112,
            "lens_distortion_correction_polynomial": [0] * 64,
            "lens_distortion_correction_polynomial_for_red": [0] * 64,
            "lens_distortion_correction_polynomial_unit_length_in_micrometres": 0,
        },
    )
    assert stored == {
        "braw_post_3dlut_embedded_name": "Gen 5.cube",
        "braw_post_3dlut_embedded_title": "Gen 5",
        "braw_post_3dlut_embedded_size": "33",
        "braw_post_3dlut_mode": "Disabled",
        "braw_lens_shading_enable": "1",
        "braw_lens_distortion_correction_polynomial_unit_length_in_micrometres": "0",
    }


def test_a_value_over_200_characters_is_truncated_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=braw_module.log.name):
        stored = braw_module.flatten_metadata(
            {"location": "x" * 250, "scene": "y" * 200}, {}
        )
    assert stored["braw_location"] == "x" * 200
    assert stored["braw_scene"] == "y" * 200
    warnings = [r for r in caplog.records if "braw_location" in r.getMessage()]
    assert len(warnings) == 1
    assert not any("braw_scene" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# The clip key
# --------------------------------------------------------------------------


def test_the_umid_is_uuid5_of_the_three_stripped_values():
    expected = str(
        uuid.uuid5(
            braw_module.BRAW_NAMESPACE,
            "24e55415-41b1-4df5-977e-0102659a2290|2024:09:09|0979-Gimbal-T1096_09091448_C004",
        )
    )
    assert braw_module.metadatas_from_probe(_probe(SIX_K), SIX_K)["umid"] == expected
    assert (
        braw_module.braw_umid(
            " 24e55415-41b1-4df5-977e-0102659a2290 ",
            "2024:09:09\n",
            "\t0979-Gimbal-T1096_09091448_C004",
        )
        == expected
    )


def test_the_namespace_is_frozen():
    """Changing it re-keys every BRAW clip ever ingested."""
    assert str(braw_module.BRAW_NAMESPACE) == "0405c014-24e6-58fc-98b2-b0ce1ffbe0f7"


def test_same_camera_different_clips_have_different_umids():
    first = _probe(SIX_K)["metadata"]["clip"]
    second = _probe(SIX_K_NEXT)["metadata"]["clip"]
    assert first["camera_id"] == second["camera_id"]
    assert first["clip_number"] != second["clip_number"]
    assert (
        braw_module.metadatas_from_probe(_probe(SIX_K), SIX_K)["umid"]
        != braw_module.metadatas_from_probe(_probe(SIX_K_NEXT), SIX_K_NEXT)["umid"]
    )


def test_the_umid_is_deterministic():
    assert (
        braw_module.metadatas_from_probe(_probe(SIX_K), "a")["umid"]
        == braw_module.metadatas_from_probe(copy.deepcopy(_probe(SIX_K)), "b")["umid"]
    )


@pytest.mark.parametrize("key", braw_module.UMID_KEYS)
@pytest.mark.parametrize("damage", ["delete", "empty", "blank"])
def test_a_missing_identity_key_refuses_the_clip_by_name(key, damage):
    """Missing-id row: no umid, never a hash fallback."""
    probe = _probe(SIX_K)
    if damage == "delete":
        del probe["metadata"]["clip"][key]
    else:
        probe["metadata"]["clip"][key] = "" if damage == "empty" else "   "
    with pytest.raises(TapelessIngestException, match=key):
        braw_module.metadatas_from_probe(probe, SIX_K)


# --------------------------------------------------------------------------
# The guard and the failure modes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["C004.braw", "C004.BRAW", "C004.Braw"])
def test_the_guard_is_case_insensitive(monkeypatch, brawprobe, name):
    """Wrong-case row: `.BRAW` is ingested the same way."""
    calls = brawprobe(stdout=json.dumps(_probe(SIX_K)))
    metadatas = _provider(monkeypatch).getMetadatasFromFile(_MediaFile(name), {}, {})
    assert metadatas["provider"] == "braw"
    assert metadatas["file_id"] == "VX-900"
    assert metadatas["umid"]
    assert calls[0][0] == ["/usr/local/bin/brawprobe", "--", "/abs/" + name]


@pytest.mark.parametrize("name", ["C004.mov", "C004.R3D", "C004.braw.xml"])
def test_other_files_are_not_claimed(monkeypatch, brawprobe, name):
    calls = brawprobe(stdout="{}")
    assert _provider(monkeypatch).getMetadatasFromFile(_MediaFile(name), {}, {}) == {}
    assert calls == []


def test_an_unreadable_clip_raises_with_the_stderr_excerpt(brawprobe):
    """Unreadable-clip row: brawprobe exits 4; nothing is ingested."""
    stderr = (
        'brawprobe: event=error stage=clip_load hr=0x80000008 message="the SDK '
        'could not open the clip: unspecified failure (E_FAIL)"'
    )
    brawprobe(stdout="", stderr=stderr, returncode=4)
    with pytest.raises(TapelessIngestException) as raised:
        braw_module.Provider().getAllClipMetadatas(MEDIA, {})
    message = str(raised.value)
    assert "exit 4" in message
    assert "could not open the clip" in message


def test_a_long_stderr_is_cut(brawprobe):
    brawprobe(stdout="", stderr="e" * 2000, returncode=3)
    with pytest.raises(TapelessIngestException) as raised:
        braw_module.Provider().getAllClipMetadatas(MEDIA, {})
    assert len(str(raised.value)) < 700


def test_output_that_is_not_json_raises(brawprobe):
    brawprobe(stdout="not json", returncode=0)
    with pytest.raises(TapelessIngestException, match="no JSON"):
        braw_module.Provider().getAllClipMetadatas(MEDIA, {})


def test_a_hung_probe_raises(monkeypatch):
    def _hang(cmd, **kwargs):
        assert kwargs["timeout"] == braw_module.BRAWPROBE_TIMEOUT_SECONDS
        raise braw_module.sp.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(braw_module, "resolve_brawprobe_path", lambda: "/x/brawprobe")
    monkeypatch.setattr(braw_module.sp, "run", _hang)
    with pytest.raises(TapelessIngestException, match="did not answer"):
        braw_module.Provider().getAllClipMetadatas(MEDIA, {})


# --------------------------------------------------------------------------
# The anchor
# --------------------------------------------------------------------------


class _Clip:
    def __init__(self, metadatas, file=None, path="2024/AH_x"):
        self.metadatas = metadatas
        self.file = file
        self.path = path


class _VSFile:
    def getId(self):
        return "VX-77"

    def getPath(self):
        return "2024/AH_x/C004.braw"


def test_the_anchor_declares_no_video_so_the_shape_route_is_taken():
    main = braw_module.Provider().getClipMainMediaFile(_Clip({}, file=_VSFile()))
    assert main[MAIN_FILE_YIELDS_VIDEO] is False
    assert main_file_declares_no_video(main)
    assert main["file_id"] == "VX-77"
    assert main["path"] == "2024/AH_x/C004.braw"


def test_a_rebuilt_anchor_path_uses_the_on_disk_name():
    clip = _Clip({"braw_probe_clip": "renamed.BRAW", "clipname": "A001_C004"})
    main = braw_module.Provider().getClipMainMediaFile(clip, rebuild=True)
    assert main["path"] == "2024/AH_x/renamed.BRAW"
    assert main["file_id"] is None


# --------------------------------------------------------------------------
# Name collisions, long names, values that must not be cut
# --------------------------------------------------------------------------


def test_a_raw_key_on_a_technical_name_never_overwrites_it(caplog):
    probe = _probe(G2)
    probe["metadata"]["clip"]["probe_width"] = "raw width"
    probe["metadata"]["clip"]["probe_clip"] = "raw clip"
    with caplog.at_level(logging.WARNING, logger=braw_module.log.name):
        metadatas = braw_module.metadatas_from_probe(probe, G2)
    assert metadatas["braw_probe_width"] == "3840"
    assert metadatas["braw_probe_width_raw"] == "raw width"
    assert metadatas["braw_probe_clip"] == probe["clip"]
    assert metadatas["braw_probe_clip_raw"] == "raw clip"
    assert any("braw_probe_width" in r.getMessage() for r in caplog.records)


def test_keys_equal_after_trimming_are_both_kept_deterministically(caplog):
    with caplog.at_level(logging.WARNING, logger=braw_module.log.name):
        stored = braw_module.flatten_metadata(
            {"scene": "1", " scene": "2", "scene ": "3"}, {}
        )
    assert stored == {"braw_scene": "1", "braw_scene_raw": "2", "braw_scene_raw2": "3"}
    assert len([r for r in caplog.records if "braw_scene" in r.getMessage()]) == 2


def test_a_raw_frame0_named_key_does_not_overwrite_a_frame0_collision():
    stored = braw_module.flatten_metadata(
        {"iso": 1, "frame0_iso": "clip says"}, {"iso": 2}
    )
    assert stored["braw_frame0_iso"] == "clip says"
    assert stored["braw_frame0_iso_raw"] == "2"


def test_a_name_over_200_characters_is_skipped_not_cut(caplog):
    key = "k" * 200
    with caplog.at_level(logging.WARNING, logger=braw_module.log.name):
        stored = braw_module.flatten_metadata({key: "1", "scene": "2"}, {})
    assert stored == {"braw_scene": "2"}
    assert any(("braw_" + key) in r.getMessage() for r in caplog.records)


def test_a_json_value_over_200_characters_is_skipped(caplog):
    with caplog.at_level(logging.WARNING, logger=braw_module.log.name):
        stored = braw_module.flatten_metadata(
            {"table": list(range(100)), "short": [1, 2, 3, 4, 5]}, {}
        )
    assert "braw_table" not in stored
    assert stored["braw_short"] == "[1,2,3,4,5]"
    assert any("braw_table" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.__setitem__("clip", "c" * 196 + ".braw"),
        lambda p: p["metadata"]["clip"].__setitem__("anamorphic", "a" * 201),
        lambda p: p["metadata"]["clip"].__setitem__("clip_number", "n" * 201),
    ],
    ids=["file-name", "anamorphic", "clipname"],
)
def test_a_value_read_back_at_import_is_never_cut(mutate):
    probe = _probe(G2)
    mutate(probe)
    with pytest.raises(TapelessIngestException, match="refused"):
        braw_module.metadatas_from_probe(probe, G2)


@pytest.mark.parametrize("path", [None, ""])
def test_a_rebuilt_anchor_with_no_folder_is_refused(path):
    clip = _Clip({"braw_probe_clip": "C004.braw"}, path=path)
    with pytest.raises(TapelessIngestException, match="no folder path"):
        braw_module.Provider().getClipMainMediaFile(clip, rebuild=True)

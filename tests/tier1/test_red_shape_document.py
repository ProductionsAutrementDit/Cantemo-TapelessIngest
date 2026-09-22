"""Tier 1: the `ShapeDocument` `red` composes when Vidispine deduces none.

On a dot-separated `Abs TC` Vidispine extracts no essence from the
`.R3D`: the item comes out with a `binaryComponent`, no duration, no
resolution, no codec and no proxy — `mediaType = 'data'` for a
single-segment clip, so no duration or type search ever finds it
(measured on prod 2026-09-22, `VX-216267` and `VX-216301`; around 40% of
RED ingests). `Provider.buildShapeDocument` states the shape the plugin
posts in its place.

The builder takes dicts and answers a dict — no Vidispine, no REDline,
no database, no clip row — which is the whole reason this file can pin
the document instead of a production item, and why every test here is
tier 1. It touches the filesystem exactly once, and only for a clip that
carries a separate `.wav`: `test_the_picture_side_still_opens_no_file`
pins that a picture-only clip reads nothing at all.

What is pinned, and the line each pin defends:

* the video components name `_001`...`_N`, in that order and with no
  hole. `manifest.py::_red_anchor` — pad_forge's reconstruction —
  requires exactly that set, and the shape Vidispine leaves behind
  starts at `_002` with the anchor buried in a `binaryComponent`, which
  pad_forge refuses with `segment(s) _001 missing`. That refusal is the
  reason this document exists;
* the CONTAINER names the anchor, never a segment;
* EVERY video component carries the whole take's duration, not its own
  segment's share (`Total Frames` on the anchor is already the whole
  take: `File Segments=2` -> `Total Frames=1012` while `Clip Out=1011`);
* the audio INSIDE the `.R3D` gets no component — the prod probe of
  2026-09-22 got a proxy WITH AAC audio from a document that declared
  none, because pad_forge builds its audio track from the file itself,
  and the reference shape's `48000` is not a captured column. Declaring
  one would be a guess;
* a SEPARATE `.wav` does get one, read off its own header. 322 RED clips
  on prod carry one (10 shoots, 2022-2026), so dropping it silently —
  or refusing those clips — was never an option, and the parameters
  belong to the `.wav`, which declares them itself;
* nothing invented. The per-clip numbers come from the REDline columns
  `REDLINE_TECHNICAL_COLUMNS` captured, and a column that is missing or
  unreadable REFUSES the clip rather than posting a shape as mute as the
  one it replaces. The unmeasured fields of the reference shape —
  `dropFrame` above all, never measured on a dot-separated anchor — are
  absent, not guessed.

The constants that are NOT REDline's (`R3D`, `r3d_raw`, `rgb48le`, bit
depth 16, `progressive`, 1:1 pixels, `video/x-raw-red`) were read off
`VX-455007`, the shape Vidispine built ITSELF for `VX-216302` — the
deducible twin of the broken clip, same card, 24 seconds apart.
"""

import wave

import pytest

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.providers import red as red_module
from portal.plugins.TapelessIngest.providers.providers import (
    Provider as BaseProvider,
)

FOLDER = "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2026/AA_x"
STEM = "K001_K067_0804BF"

# The eight technical columns as REDline PRINTS them: strings, verbatim,
# never floats. Converting them is this document's business; the capture
# copies (see `REDLINE_TECHNICAL_COLUMNS`).
METADATAS = {
    "clipname": STEM,
    "timecode": "03.17.13.27",
    "frame_width": "3840",
    "frame_height": "2160",
    "fps": "60.000",
    # DIFFERENT from `fps` on purpose: `Record FPS` is captured beside it
    # and must not reach the document, or duration and frame rate would
    # describe two different clips.
    "record_fps": "23.976",
    "total_frames": "1012",
    "file_segments": "2",
    "redcode": "5:1",
    "camera_audio_channels": "2",
}


def _segment(index):
    name = f"{STEM}_{index:03d}.R3D"
    return {
        "type": "video",
        "track": 1,
        "order": index - 1,
        "file_id": f"VX-41-{index:03d}",
        "path": f"{FOLDER}/{name}",
    }


def _anchor():
    return _segment(1)


def _extras(count):
    return [_segment(index) for index in range(2, count + 2)]


def _write_wav(
    directory, name, channels=2, sample_width=3, frame_rate=48000, frames=1000
):
    """A real `.wav` on disk — the only thing the builder ever reads.

    The frames are really written: `Wave_write.close()` rewrites the
    header with the count it actually wrote, so a `setnframes` alone
    would leave `getnframes()` at 0 and the test would be pinning the
    fixture's bug rather than the builder. 1000 silent frames is enough
    to prove the count is read; the reference clip's own 4 546 560 are
    pinned against the pure derivation instead of 27 MB of zeroes.
    """
    path = directory / name
    handle = wave.open(str(path), "wb")
    handle.setnchannels(channels)
    handle.setsampwidth(sample_width)
    handle.setframerate(frame_rate)
    handle.writeframes(b"\x00" * channels * sample_width * frames)
    handle.close()
    return str(path)


def _wav_extra(path=None, absolute_path=None, file_id="VX-41-WAV"):
    return {
        "type": "audio",
        "track": 1,
        "order": 1,
        "file_id": file_id,
        "path": path or f"{FOLDER}/{STEM}.wav",
        "absolute_path": absolute_path,
    }


def _build(main_file=None, extra_files=None, metadatas=None):
    return red_module.Provider.buildShapeDocument(
        _anchor() if main_file is None else main_file,
        _extras(1) if extra_files is None else extra_files,
        METADATAS if metadatas is None else metadatas,
    )


# --------------------------------------------------------------------------
# The video components: _001..._N, no hole, in name order
# --------------------------------------------------------------------------


@pytest.mark.parametrize("extras", [0, 1, 4])
def test_the_video_components_name_every_segment_in_order(extras):
    """One component per `.R3D`, anchor first, then `_002`...`_N`.

    A single-segment clip takes the SAME route and gets ONE video
    component — that is the `VX-216301` population, a third of the
    broken clips, and it is broken differently only because the
    single-component import path never read the verdict.

    Mutation killed: building the components from `extra_files` alone,
    which reproduces the `_002...`-only shape pad_forge refuses.
    """
    document = _build(extra_files=_extras(extras))

    ids = [component["file"][0]["id"] for component in document["videoComponent"]]
    assert ids == [f"VX-41-{index:03d}" for index in range(1, extras + 2)]


def test_the_order_comes_from_the_names_not_from_the_list():
    """`itemTrack` does not encode segment order — the names do.

    Vidispine gave `_002` track V1 and `_001` track V2 on the reference
    shape, and pad_forge reads the order off the file names. So a
    provider handing the extras back shuffled must still produce
    `_001`, `_002`, `_003`.
    """
    shuffled = [_segment(4), _segment(2), _segment(3)]

    document = _build(extra_files=shuffled)

    assert [component["file"][0]["id"] for component in document["videoComponent"]] == [
        "VX-41-001",
        "VX-41-002",
        "VX-41-003",
        "VX-41-004",
    ]


def test_no_track_number_is_stated_at_all():
    """Order is the names'. Track numbers are not "right" to be got right.

    The reference shape proves it: Vidispine put `_002` on V1 and `_001`
    on V2 and the clip is perfectly usable. Inventing a numbering here
    would be effort spent on a field nothing reads.
    """
    document = _build(extra_files=_extras(2))

    for component in document["videoComponent"]:
        assert "itemTrack" not in component
        assert "essenceStreamId" not in component


def test_a_hole_in_the_segment_numbering_is_refused():
    """`_001`, `_003` is not a take — it is an incomplete copy.

    pad_forge refuses it downstream naming only the shape; refusing here
    names the numbering, before a shape exists to confuse anyone.

    Mutation killed: dropping the contiguity check, which posts a shape
    whose reconstruction fails with `segment(s) _002 missing`.
    """
    with pytest.raises(TapelessIngestException) as excinfo:
        _build(extra_files=[_segment(3)])

    assert "001, 003" in str(excinfo.value)
    assert "001, 002" in str(excinfo.value)


def test_a_segment_that_is_not_numbered_is_refused():
    stray = dict(_segment(2), path=f"{FOLDER}/{STEM}.R3D")

    with pytest.raises(TapelessIngestException) as excinfo:
        _build(extra_files=[stray])

    assert f"{STEM}.R3D" in str(excinfo.value)


@pytest.mark.parametrize("missing", ["main", "extra"])
def test_a_media_file_with_no_vidispine_id_is_refused(missing):
    """A component naming nothing attaches nothing.

    Reachable: `getFileIdFromFullPath` answers `None` for a file
    Vidispine does not know, and the REST path can build a clip with no
    `file` at all.
    """
    main_file = _anchor()
    extra_files = _extras(1)
    if missing == "main":
        main_file["file_id"] = None
    else:
        extra_files[0]["file_id"] = None

    with pytest.raises(TapelessIngestException) as excinfo:
        _build(main_file=main_file, extra_files=extra_files)

    assert "no Vidispine file id" in str(excinfo.value)


def test_an_extra_that_is_neither_picture_nor_sound_is_refused():
    """This document declares a component for video and for audio.

    Anything else — a provider handing back a sidecar with a real file
    id, say — would be attached to nothing, and posting the shape
    anyway would ingest the picture and silently lose the rest. That is
    the failure mode this whole line of work exists to end.
    """
    stray = {
        "type": "metadatas",
        "file_id": "VX-41-XML",
        "path": f"{FOLDER}/{STEM}.xml",
    }

    with pytest.raises(TapelessIngestException) as excinfo:
        _build(extra_files=_extras(1) + [stray])

    assert f"{STEM}.xml" in str(excinfo.value)
    assert "silently drop" in str(excinfo.value)


def test_two_audio_files_are_refused_rather_than_ordered_by_guess():
    """Unreachable through a scan, and refused rather than assumed.

    `getClipAdditionalMediaFiles` collects at most one `.wav` — an
    exact-name query, first hit — and which of several would be `A1` has
    never been measured.
    """
    with pytest.raises(TapelessIngestException) as excinfo:
        _build(extra_files=[_wav_extra(path="a.wav"), _wav_extra(path="b.wav")])

    assert "never been measured" in str(excinfo.value)


# --------------------------------------------------------------------------
# The container, and the duration every component carries
# --------------------------------------------------------------------------


def test_the_container_names_the_anchor():
    """`_001`, never a segment — the first thing the reference settles."""
    document = _build(extra_files=_extras(3))

    assert document["containerComponent"]["file"] == [{"id": "VX-41-001"}]
    assert document["containerComponent"]["format"] == "R3D"


def test_every_video_component_carries_the_whole_takes_duration():
    """Not its own segment's share — the second thing the reference settles.

    `Total Frames` on the anchor is ALREADY the whole take
    (`File Segments=2` -> `1012` while `Clip Out=1011`), so nothing may
    divide it by the segment count or sum it over the segments.

    `1012 / 60 = 16.866666666666667` is bit for bit the
    `durationSeconds` Vidispine writes on the deducible twin.

    Mutation killed: dividing `samples` by the number of segments.
    """
    document = _build(extra_files=_extras(3))

    expected = {
        "samples": 1012,
        "timeBase": {"numerator": 1000, "denominator": 60000},
    }
    assert document["containerComponent"]["duration"] == expected
    assert [component["duration"] for component in document["videoComponent"]] == [
        expected
    ] * 4
    assert expected["samples"] / (
        expected["timeBase"]["denominator"] / expected["timeBase"]["numerator"]
    ) == pytest.approx(16.866666666666667)


def test_the_resolution_and_frame_rate_are_redlines_own_numbers():
    """Read, never assumed — and `Record FPS` is not the one that is read."""
    document = _build()

    component = document["videoComponent"][0]
    assert component["resolution"] == {"width": 3840, "height": 2160}
    assert component["averageFrameRate"] == {"numerator": 60000, "denominator": 1000}
    # `record_fps` is "23.976" in the fixture and must appear NOWHERE.
    assert "23976" not in repr(document)


@pytest.mark.parametrize(
    "fps,denominator",
    [("60.000", 60000), ("23.976", 23976), ("50.000", 50000), ("25", 25000)],
)
def test_the_frame_rate_keeps_redlines_three_decimals_exactly(fps, denominator):
    """`23.976` is `23976/1000`, not a float rounded twice.

    That is why the reference shape states 60 fps as `{60000, 1000}` and
    the duration as `{1000, 60000}` — the same rational, inverted.
    """
    document = _build(metadatas=dict(METADATAS, fps=fps))

    assert document["videoComponent"][0]["averageFrameRate"] == {
        "numerator": denominator,
        "denominator": 1000,
    }
    assert document["containerComponent"]["duration"]["timeBase"] == {
        "numerator": 1000,
        "denominator": denominator,
    }


# --------------------------------------------------------------------------
# Nothing invented
# --------------------------------------------------------------------------


def test_a_clip_with_no_separate_wav_declares_no_audio_component():
    """The audio INSIDE the `.R3D` stays undeclared, and that is measured.

    The prod probe's document had no audio component and the proxy still
    came out with AAC audio, because pad_forge builds
    `AudioTrack(anchor, 0)` from the file itself. The reference shape's
    `pcm_s32le` at `48000` is real, but no captured column gives that
    rate — `Camera Audio Channels` can read `2` while `WAV Filename` is
    empty — so declaring one would be a guess.

    A SEPARATE `.wav` is a different case entirely: it declares its own
    parameters, and it IS declared. See below.
    """
    document = _build(extra_files=_extras(2))

    assert "audioComponent" not in document


@pytest.mark.parametrize(
    "field",
    [
        "dropFrame",
        "startTimecode",
        "firstSMPTETimecode",
        "timeCodeTimeBase",
        "roundedTimeBase",
    ],
)
def test_the_unmeasured_timecode_fields_are_absent_not_guessed(field):
    """Measured on a COLON-separated anchor; never on a dot-separated one.

    `dropFrame` above all: the dot separator is treated throughout this
    provider as a SIGNAL, not as drop-frame semantics — the same flag
    appears at 50 fps, where drop-frame is not defined. Writing `true`
    there would be stating an unmeasured fact on every clip this
    document is ever built for.
    """
    document = _build(extra_files=_extras(2))

    assert field not in document["containerComponent"]
    assert field not in repr(document)


def test_the_format_constants_are_the_reference_shapes():
    """`VX-455007`'s own values, and nothing beyond them.

    Richness past what was measured is not required and must not be
    invented: the prod probe's document was thinner still and produced a
    working h264 proxy with audio, a thumbnail and a scrub sheet.
    """
    document = _build()

    component = document["videoComponent"][0]
    assert component["codec"] == "r3d_raw"
    assert component["pixelFormat"] == "rgb48le"
    assert component["bitDepth"] == 16
    assert component["fieldOrder"] == "progressive"
    assert component["pixelAspectRatio"] == {"horizontal": 1, "vertical": 1}
    assert document["mimeType"] == ["video/x-raw-red"]
    # The tag travels in the QUERY (`?tag=original`), not in the body:
    # `ItemAPI.createItemShape` posts the same body with no query at all
    # and leaves the item mute.
    assert "tag" not in document


@pytest.mark.parametrize("key", ["frame_width", "frame_height", "total_frames", "fps"])
@pytest.mark.parametrize("value", [None, "", "   ", "0", "n/a", "-3"])
def test_a_column_redline_did_not_give_refuses_the_clip(key, value):
    """A blank width or a blank duration posts a shape as mute as the
    one it replaces. Refusing NAMES the column; guessing names nothing.

    Mutation killed: defaulting a missing column to 0, which posts a
    document Vidispine accepts and no editor can use.
    """
    with pytest.raises(TapelessIngestException) as excinfo:
        _build(metadatas=dict(METADATAS, **{key: value}))

    assert key in str(excinfo.value)
    assert "nothing is posted" in str(excinfo.value)


def test_the_builder_touches_nothing_outside_its_arguments():
    """PURE: same inputs, same document, and the inputs come back intact.

    The whole point of the signature `(main_file, extra_files,
    metadatas) -> dict` is that the document a production item receives
    can be read in a unit test. A builder that reached for `self`, a
    clip row or a storage helper would not be readable here at all.
    """
    main_file = _anchor()
    extra_files = _extras(2)
    metadatas = dict(METADATAS)

    first = red_module.Provider.buildShapeDocument(main_file, extra_files, metadatas)
    second = red_module.Provider.buildShapeDocument(main_file, extra_files, metadatas)

    assert first == second
    assert main_file == _anchor()
    assert extra_files == _extras(2)
    assert metadatas == METADATAS


def test_a_provider_that_never_learned_the_question_composes_nothing():
    """The default hook answers `None`, and `None` keeps today's route.

    That is what holds this story at `red`: a provider that can answer
    the verdict but cannot describe its own essence must not be handed a
    route that would have to guess a codec on its behalf.
    """
    assert BaseProvider().buildShapeDocument({}, [], {}) is None


# --------------------------------------------------------------------------
# The separate `.wav`: READ, never estimated
# --------------------------------------------------------------------------
#
# 322 RED clips on prod carry one (10 shoots, 2022-2026, measured
# 2026-09-22), so a document that dropped it silently — or a route that
# refused those clips — was not tenable. The parameters belong to the
# `.wav`, not to the `.R3D`, and the `.wav` declares them in its header.


def test_the_wavs_header_is_what_the_audio_component_states(tmp_path):
    """Read off a real file, end to end through the builder.

    Every value below comes from the four numbers `wave` reports —
    nothing is defaulted, which a wrong rate or a wrong width would show
    immediately.

    Mutation killed: hardcoding 48000 or 2 channels, which the
    parametrized derivation test below then also catches.
    """
    path = _write_wav(tmp_path, "take.wav")

    document = _build(extra_files=_extras(1) + [_wav_extra(absolute_path=path)])

    assert document["audioComponent"] == [
        {
            "file": [{"id": "VX-41-WAV"}],
            "codec": "pcm_s24le",
            "channelCount": 2,
            "channelLayout": 0,
            "frameSize": 1,
            "blockAlign": 6,
            "bitrate": 2304000,
            "timeBase": {"numerator": 1, "denominator": 48000},
            "duration": {
                "samples": 1000,
                "timeBase": {"numerator": 1, "denominator": 48000},
            },
            "itemTrack": "A1",
            "essenceStreamId": 0,
            "sampleFormat": "AV_SAMPLE_FMT_S32",
        }
    ]


def test_the_reference_shapes_own_numbers_are_reproduced():
    """`A002_A021_0526HT`, read off prod 2026-09-22.

    A 9-segment RED clip with a separate `.wav`, whose `original` shape
    Vidispine built itself: 2 channels, 24-bit, 48000, 4 546 560 frames.
    This is the measurement the whole derivation answers to, pinned
    against the pure function so it costs no 27 MB fixture.
    """
    component = red_module.Provider._audio_component_from_header(
        "VX-41-WAV", 2, 3, 48000, 4546560
    )

    assert component["channelCount"] == 2
    assert component["channelLayout"] == 0
    assert component["sampleFormat"] == "AV_SAMPLE_FMT_S32"
    assert component["frameSize"] == 1
    assert component["blockAlign"] == 6
    assert component["codec"] == "pcm_s24le"
    assert component["timeBase"] == {"numerator": 1, "denominator": 48000}
    assert component["itemTrack"] == "A1"
    assert component["essenceStreamId"] == 0
    assert component["bitrate"] == 2304000
    assert component["duration"] == {
        "samples": 4546560,
        "timeBase": {"numerator": 1, "denominator": 48000},
    }


def test_the_picture_is_unchanged_by_the_presence_of_a_wav(tmp_path):
    """The `.wav` is a component of its own, not a segment.

    It must never reach the video components, and it must never shift
    the `_001`...`_N` numbering the reconstruction reads.
    """
    path = _write_wav(tmp_path, "take.wav")

    document = _build(extra_files=_extras(2) + [_wav_extra(absolute_path=path)])

    assert [c["file"][0]["id"] for c in document["videoComponent"]] == [
        "VX-41-001",
        "VX-41-002",
        "VX-41-003",
    ]
    assert document["containerComponent"]["file"] == [{"id": "VX-41-001"}]


@pytest.mark.parametrize(
    "channels,sample_width,frame_rate,codec,block_align,bitrate",
    [
        (2, 3, 48000, "pcm_s24le", 6, 2304000),
        (1, 2, 44100, "pcm_s16le", 2, 705600),
        (4, 3, 96000, "pcm_s24le", 12, 9216000),
    ],
)
def test_every_audio_field_is_arithmetic_over_the_header(
    channels, sample_width, frame_rate, codec, block_align, bitrate
):
    """The derivation, pinned without a file on disk.

    Pure, so it is pinned directly: `blockAlign` is channels x width,
    `bitrate` is rate x channels x width x 8, and the codec names the
    width in BITS.

    Mutation killed: `blockAlign = sample_width` (right for mono only);
    `bitrate` without the x8 (right for nothing).
    """
    component = red_module.Provider._audio_component_from_header(
        "VX-41-WAV", channels, sample_width, frame_rate, 1000
    )

    assert component["codec"] == codec
    assert component["channelCount"] == channels
    assert component["blockAlign"] == block_align
    assert component["bitrate"] == bitrate
    assert component["timeBase"] == {"numerator": 1, "denominator": frame_rate}
    assert component["duration"] == {
        "samples": 1000,
        "timeBase": {"numerator": 1, "denominator": frame_rate},
    }


@pytest.mark.parametrize("sample_width", [1, 2, 4, 8])
def test_an_unmeasured_sample_width_omits_the_sample_format(sample_width):
    """The same rule `dropFrame` follows.

    Only a 3-byte `.wav` has ever been measured, and it carried
    `AV_SAMPLE_FMT_S32` — FFmpeg widens 24-bit samples to 32, so the
    mapping is not the pattern it looks like and extrapolating it would
    be inventing a fact. An unmeasured width states nothing.
    """
    component = red_module.Provider._audio_component_from_header(
        "VX-41-WAV", 2, sample_width, 48000, 1000
    )

    assert "sampleFormat" not in component
    # ...and everything that IS arithmetic is still stated.
    assert component["codec"] == f"pcm_s{8 * sample_width}le"


def test_the_measured_sample_width_states_the_measured_format():
    component = red_module.Provider._audio_component_from_header(
        "VX-41-WAV", 2, 3, 48000, 1000
    )

    assert component["sampleFormat"] == "AV_SAMPLE_FMT_S32"


def test_a_wav_the_wave_module_refuses_fails_the_clip(tmp_path):
    """RF64 and floating-point WAV are not shapes this provider describes.

    A shape posted without its sound is exactly the silent partial
    ingest the refusal exists to prevent, so it fails by name instead.
    """
    path = tmp_path / "broken.wav"
    path.write_bytes(b"RF64" + b"\xff" * 4 + b"WAVE" + b"\x00" * 32)

    with pytest.raises(TapelessIngestException) as excinfo:
        _build(
            extra_files=_extras(1)
            + [_wav_extra(path="cards/broken.wav", absolute_path=str(path))]
        )

    # NAMED by its storage path, which is the one the operator knows and
    # the one the clip is filed under — not by a temp directory.
    assert "cards/broken.wav" in str(excinfo.value)
    assert "nothing is posted" in str(excinfo.value)


def test_a_wav_that_is_not_there_fails_the_clip(tmp_path):
    missing = str(tmp_path / "gone.wav")

    with pytest.raises(TapelessIngestException) as excinfo:
        _build(
            extra_files=_extras(1)
            + [_wav_extra(path="cards/gone.wav", absolute_path=missing)]
        )

    assert "cards/gone.wav" in str(excinfo.value)


def test_a_wav_with_no_resolvable_path_fails_the_clip():
    """An unresolvable storage root is not a reason to drop the sound."""
    with pytest.raises(TapelessIngestException) as excinfo:
        _build(extra_files=_extras(1) + [_wav_extra(absolute_path=None)])

    assert "no resolvable path on disk" in str(excinfo.value)


def test_a_wav_with_no_vidispine_id_fails_the_clip(tmp_path):
    path = _write_wav(tmp_path, "take.wav")

    with pytest.raises(TapelessIngestException) as excinfo:
        _build(extra_files=_extras(1) + [_wav_extra(absolute_path=path, file_id=None)])

    assert "no Vidispine file id" in str(excinfo.value)


@pytest.mark.parametrize(
    "channels,sample_width,frame_rate", [(0, 3, 48000), (2, 0, 48000), (2, 3, 0)]
)
def test_a_header_claiming_nothing_fails_the_clip(channels, sample_width, frame_rate):
    """A zero rate would make `timeBase` a division by nothing."""
    with pytest.raises(TapelessIngestException) as excinfo:
        red_module.Provider._audio_component_from_header(
            "VX-41-WAV", channels, sample_width, frame_rate, 1000
        )

    assert "nothing is posted" in str(excinfo.value)


def test_the_picture_side_still_opens_no_file(tmp_path):
    """A clip with no `.wav` reads nothing at all.

    The single filesystem read this builder makes is the `.wav` header,
    and it is bought by a clip that HAS one. A segmented picture-only
    clip — the measured population of this route — is composed from
    dicts alone, which is why every other test in this file needs no
    `tmp_path`.
    """
    import builtins

    opened = []
    real_open = builtins.open

    def counting_open(*args, **kwargs):
        opened.append(args[0] if args else kwargs.get("file"))
        return real_open(*args, **kwargs)

    builtins.open = counting_open
    try:
        _build(extra_files=_extras(3))
    finally:
        builtins.open = real_open

    assert opened == []

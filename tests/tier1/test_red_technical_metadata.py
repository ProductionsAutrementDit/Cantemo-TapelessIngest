"""Tier 1: the eight technical columns `red` now copies out of REDline.

`REDline --printMeta 3 --useMeta` already runs on every `.R3D` at every
scan and prints ~190 columns. The provider read seven. Measured on prod
2026-09-22, roughly 40% of RED ingests land on an item that cannot state
its own duration, resolution or codec because Vidispine's shape
deduction extracts nothing from a dot-separated `.R3D` — while REDline,
reading the same media, prints all of it and prints it IDENTICALLY on
the dot-separated clip (`K001_K067_0804BF_001.R3D`) and on the
colon-separated one (`K001_K068_0804LK_001.R3D`).

This file pins the CAPTURE and only the capture:

* the fifteen columns produce fifteen `metadatas` entries, each
  technical one a VERBATIM copy of what REDline printed — no int, no
  float, no quotient. `Total Frames / FPS` happens to reproduce
  Vidispine's own `durationSeconds` bit for bit, and deriving it is a
  later story's business, not this one's;
* a CSV missing one of the eight keeps RAISING and keeps NAMING it, as
  the original seven do — a shape this provider does not read must fail
  loudly, never persist blanks;
* `Abs TC` stays the SOLE input of `anchor_yields_video_component`. The
  eight must not become a second, competing verdict.

Plus the four traps measured on prod the same day, each of which would
otherwise be discovered in production:

1. `Total Frames` on the anchor covers the WHOLE take, not the anchor's
   segment (`File Segments=2` -> `Total Frames=1012`, `Clip Out=1011`).
2. The timecode separator varies per FIELD inside one clip: `End Abs TC`
   is dot-separated on a clip whose `Abs TC` is colon-separated, so a
   verdict read off `End Abs TC` would call every clip non-deducible.
3. The header repeats `Lens`, `Aperture` and `Focal Length`;
   `csv.DictReader` keeps the last of each.
4. `WAV Filename` is EMPTY while `Camera Audio Channels` is `2` — the
   audio is inside the `.R3D`.

DB-free: `sp.run` is replaced by a canned CSV, so no REDline, no media
and no database are touched.
"""

import logging

import pytest

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.models.clip import ClipMetadata
from portal.plugins.TapelessIngest.providers import red as red_module
from portal.plugins.TapelessIngest.providers.providers import (
    MAIN_FILE_YIELDS_VIDEO,
    Provider as BaseProvider,
)

MEDIA = "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2026/AA_x/K001_K067_0804BF_001.R3D"

# A `--printMeta 3` row in REDline's own column order, trimmed to what
# matters here and kept as (column, value) PAIRS so header and row can
# never drift apart. The trailing empty field real output carries is
# reproduced by `_csv`.
#
# The duplicated `Lens` / `Aperture` / `Focal Length` columns are real
# (trap 3) and deliberately carry DIFFERENT values, so a test asserting
# on them would see which one `csv.DictReader` kept. `WAV Filename` is
# empty beside `Camera Audio Channels=2` (trap 4), and `Clip Out=1011`
# sits beside `Total Frames=1012` on a two-segment take (trap 1).
REDLINE_ROW_PAIRS = (
    ("Clip Name", "K001_K067_0804BF"),
    ("Camera Model", "KOMODO 6K"),
    ("Camera PIN", "KMDBK006080"),
    ("UUID", "42B681D6-2AB5-46A2-8DEA-B1C05BD0CA54"),
    ("Date", "20260804"),
    ("Timestamp", "103910"),
    ("Abs TC", "03.17.13.27"),
    ("End Abs TC", "03.17.30.09"),
    ("Edge TC", "02:13:31:24"),
    ("End Edge TC", "02.13.48.20"),
    ("Frame Width", "3840"),
    ("Frame Height", "2160"),
    ("FPS", "60.000"),
    ("Record FPS", "60.000"),
    ("Total Frames", "1012"),
    ("Clip In", "0"),
    ("Clip Out", "1011"),
    ("File Segments", "2"),
    ("REDCODE", "5:1"),
    ("Camera Audio Channels", "2"),
    ("WAV Filename", ""),
    ("Lens", "RF24-105mm"),
    ("Aperture", "4.0"),
    ("Focal Length", "35"),
    ("Lens", "Sigma 18-35"),
    ("Aperture", "1.8"),
    ("Focal Length", "24"),
)

# What `getAllClipMetadatas` puts in the dict, and where each entry comes
# from. `shooting_date` and `device_manufacturer` are the only two that
# are NOT a straight copy of a column, and they predate this story.
TECHNICAL_KEYS_BY_COLUMN = dict(
    (column, key) for key, column in red_module.REDLINE_TECHNICAL_COLUMNS
)


class _CompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=1):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _csv(pairs=REDLINE_ROW_PAIRS, drop=(), **overrides):
    """Render `pairs` as REDline renders them, minus `drop`.

    `drop` removes a column from BOTH header and row — which is what a
    REDline whose output shape is not this provider's looks like, and
    what tells an absent column apart from an empty one.
    """
    kept = [(column, value) for column, value in pairs if column not in drop]
    kept = [(column, overrides.get(column, value)) for column, value in kept]
    header = ",".join(column for column, _value in kept)
    row = ",".join(value for _column, value in kept)
    # The trailing comma REDline really prints: DictReader turns it into
    # a final empty field, and nothing here may depend on its absence.
    return f"{header},\n{row},\n"


@pytest.fixture
def redline(monkeypatch):
    """Replace the binary with a canned CSV, and count the invocations.

    The call count is asserted rather than assumed: the whole premise of
    this story is that the measurement is ALREADY FREE, and a second
    `--printMeta` run per clip would silently double the cost of every
    scan.
    """
    calls = []

    def _run(stdout=None, returncode=1):
        payload = _csv() if stdout is None else stdout

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _CompletedProcess(stdout=payload, returncode=returncode)

        monkeypatch.setattr(
            red_module, "resolve_redline_path", lambda: "/usr/local/bin/REDline"
        )
        monkeypatch.setattr(red_module.sp, "run", _fake_run)
        return calls

    return _run


def _metadatas(redline, **kwargs):
    redline(**kwargs)
    return red_module.Provider().getAllClipMetadatas(MEDIA, {})


# --------------------------------------------------------------------------
# The capture
# --------------------------------------------------------------------------


def test_the_fifteen_columns_become_fifteen_metadata_entries(redline):
    """One entry per column this provider reads, and nothing else.

    The key SET is asserted, not just the eight new values: a stray key
    becomes a `ClipMetadata` row on every R3D of every scan, and a
    missing one is a value the later duration story will not find.

    Mutation killed: dropping any one assignment from the loop; adding a
    derived key (`duration`, `framerate`) here rather than in the story
    that owns the mapping.
    """
    metadatas = _metadatas(redline)

    assert set(metadatas) == {
        # The seven that predate this story.
        "clipname",
        "umid",
        "timecode",
        "shooting_date",
        "device_manufacturer",
        "device_model",
        "device_serial",
        # The eight this story adds.
        "frame_width",
        "frame_height",
        "fps",
        "record_fps",
        "total_frames",
        "file_segments",
        "redcode",
        "camera_audio_channels",
    }
    assert len(metadatas) == 15


def test_every_technical_value_is_the_printed_string(redline):
    """Copied, never computed — `60.000` stays `60.000`.

    Mutation killed: `int(row["Frame Width"])` or `float(row["FPS"])`.
    Both would "work" and both would destroy the only property this
    story sells: that the row is a TRANSCRIPTION of a second reader's
    measurement. `float("60.000")` is `60.0`, which REDline never
    printed and which no later comparison against REDline's own output
    would match.
    """
    metadatas = _metadatas(redline)

    assert metadatas["frame_width"] == "3840"
    assert metadatas["frame_height"] == "2160"
    assert metadatas["fps"] == "60.000"
    assert metadatas["record_fps"] == "60.000"
    assert metadatas["total_frames"] == "1012"
    assert metadatas["file_segments"] == "2"
    assert metadatas["redcode"] == "5:1"
    assert metadatas["camera_audio_channels"] == "2"

    for key in TECHNICAL_KEYS_BY_COLUMN.values():
        assert isinstance(metadatas[key], str), (
            f"{key} was coerced to {type(metadatas[key]).__name__}; the "
            f"contract is REDline's own printed string"
        )


def test_no_duration_is_derived_here(redline):
    """`Total Frames / FPS` = `16.866666666666667` — and it is NOT computed.

    That quotient is bit-for-bit the `durationSeconds` Vidispine writes
    when its deduction succeeds (VX-216302, measured 2026-09-22), which
    is exactly why it is tempting. Deriving it is the NEXT story, with
    the mapping and the Vidispine exposure that make it mean something;
    fabricating it here would put a computed number in a table whose
    other rows are transcriptions, with nothing reading it.

    Mutation killed: adding `metadatas["duration"]` or
    `metadatas["framerate"]` to the capture loop.
    """
    metadatas = _metadatas(redline)

    assert "duration" not in metadatas
    assert "framerate" not in metadatas
    assert not any(str(value).startswith("16.86") for value in metadatas.values())


def test_the_measurement_costs_no_second_redline_run(redline):
    """One `--printMeta 3` call per clip, as before this story."""
    calls = redline()

    red_module.Provider().getAllClipMetadatas(MEDIA, {})

    assert len(calls) == 1
    assert calls[0][-3:] == ["--printMeta", "3", "--useMeta"]


def test_a_success_row_is_still_parsed_despite_exit_status_one(redline):
    """Trap 5: REDline exits 1 ON SUCCESS, and no new guard may forget it.

    Mutation killed: gating the new columns behind `returncode == 0`,
    which breaks the working path on every clip.
    """
    metadatas = _metadatas(redline, returncode=1)

    assert metadatas["total_frames"] == "1012"


def test_every_captured_value_fits_a_clip_metadata_row(redline):
    """`ClipMetadata.value` is `max_length=200`; none of these comes close.

    Asserted against the FIELD rather than against a repeated literal,
    so a narrowed column is caught here instead of by a truncated value
    on prod.
    """
    limit = ClipMetadata._meta.get_field("value").max_length
    metadatas = _metadatas(redline)

    for key, value in metadatas.items():
        assert len(str(value)) <= limit, f"{key} would not fit a ClipMetadata row"


# --------------------------------------------------------------------------
# A missing column still fails loudly
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "column", [column for _key, column in red_module.REDLINE_TECHNICAL_COLUMNS]
)
def test_a_missing_technical_column_raises_naming_it(redline, column):
    """One tier, not two: the eight are REQUIRED exactly as the seven are.

    A CSV without one of them is a REDline whose output shape is not the
    one this provider reads, and the clip must be reported failed with
    the column named — never persisted with a blank that an editor
    would later read as "this clip has no resolution".

    Mutation killed: `row.get(column, "")`, or a second, non-fatal tier
    (which is the documented fallback IF a column turns out absent on an
    older REDline, not the default).
    """
    redline(stdout=_csv(drop=(column,)))

    with pytest.raises(TapelessIngestException) as excinfo:
        red_module.Provider().getAllClipMetadatas(MEDIA, {})

    message = str(excinfo.value)
    assert column in message
    assert MEDIA in message
    # The other seven are present, so none of them may be named.
    assert "Clip Name" not in message


def test_an_empty_technical_value_is_not_a_missing_column(redline):
    """Present-and-empty is a VALUE; the guard tests for absence only.

    `WAV Filename` is genuinely empty on both measured clips, so a guard
    written as `if not row.get(c)` would fail every real RED clip the
    day one of the eight prints an empty field.
    """
    metadatas = _metadatas(redline, stdout=_csv(**{"File Segments": ""}))

    assert metadatas["file_segments"] == ""


def test_the_eight_are_in_the_one_required_tuple(redline):
    """The guard covers them because they are IN `REDLINE_REQUIRED_COLUMNS`.

    Keeping one tuple is what makes the loud failure automatic; a
    separate "optional" tier would have to re-implement it.
    """
    required = red_module.REDLINE_REQUIRED_COLUMNS

    for _key, column in red_module.REDLINE_TECHNICAL_COLUMNS:
        assert column in required
    assert len(required) == len(set(required)) == 15


# --------------------------------------------------------------------------
# The traps measured on prod
# --------------------------------------------------------------------------


def test_total_frames_is_the_whole_take_not_the_anchors_segment(redline):
    """Trap 1: `File Segments=2` -> `Total Frames=1012`, `Clip Out=1011`.

    The number is the TAKE's, printed on the anchor. Whatever later
    divides it by `FPS` gets the take's duration — which is what
    Vidispine writes for a deducible clip. Summing the segments, or
    reading `Clip Out` instead, both give a different and wrong number.

    Mutation killed: multiplying by `File Segments`; preferring
    `Clip Out`; reading `Total Frames` off a segment rather than off the
    anchor.
    """
    metadatas = _metadatas(redline)

    assert metadatas["file_segments"] == "2"
    assert metadatas["total_frames"] == "1012"
    assert metadatas["total_frames"] != "1011"
    assert metadatas["total_frames"] != "2024"


def test_camera_audio_channels_survives_an_empty_wav_filename(redline):
    """Trap 4: two channels, no `.wav` — the audio is inside the `.R3D`.

    `WAV Filename` is empty on both clips measured on prod, and it is
    not one of the eight. `Camera Audio Channels` must not be made
    conditional on it: on a deducible clip Vidispine emits an
    `audioComponent` naming the `.R3D` itself.
    """
    metadatas = _metadatas(redline)

    assert metadatas["camera_audio_channels"] == "2"
    assert "wav_filename" not in metadatas


def test_the_repeated_header_columns_disturb_none_of_the_eight(redline):
    """Trap 3: `Lens`, `Aperture` and `Focal Length` each appear TWICE.

    `csv.DictReader` silently keeps the LAST of each. None of the eight
    is duplicated, so the capture is unaffected — but the fixture
    carries the duplicates so that a future ninth column lands on a row
    that actually reproduces REDline's header, and so that no guard
    added here starts refusing a CSV for having them.
    """
    columns = [column for column, _value in REDLINE_ROW_PAIRS]
    assert columns.count("Lens") == 2

    metadatas = _metadatas(redline)

    assert metadatas["frame_width"] == "3840"
    assert metadatas["redcode"] == "5:1"


# --------------------------------------------------------------------------
# `Abs TC` stays the only verdict
# --------------------------------------------------------------------------


class _AnchorClip:
    """The minimum `anchor_yields_video_component` dereferences."""

    def __init__(self, metadatas):
        self.file = None
        self.path = "2026/AA_x/K001_K067_0804BF_001.RDC"
        self.metadatas = metadatas


def test_a_dot_separated_clip_still_answers_false_with_every_column_present(
    redline, caplog
):
    """The 40% case, end to end: capture changes nothing about the budget.

    The clip whose `Abs TC` is `03.17.13.27` now carries its resolution,
    its frame rate and its codec — and Vidispine STILL extracts no video
    component from it, so the placeholder budget must still not declare
    a video slot for the anchor. Capture is capture.

    Mutation killed: letting a populated `frame_width` or `REDCODE`
    upgrade the verdict to `True` on the theory that "we know it is
    video now" — the silent over-declaration (defect A), which leaves
    every one of those clips on a placeholder for ever.
    """
    metadatas = _metadatas(redline)
    assert metadatas["timecode"] == "03.17.13.27"
    assert metadatas["frame_width"] == "3840"

    with caplog.at_level(logging.DEBUG):
        main_file = red_module.Provider().getClipMainMediaFile(
            _AnchorClip(metadatas), rebuild=True
        )

    assert main_file[MAIN_FILE_YIELDS_VIDEO] is False
    assert (
        red_module.Provider.anchor_yields_video_component(_AnchorClip(metadatas))
        is False
    )


@pytest.mark.parametrize(
    "key", [key for key, _column in red_module.REDLINE_TECHNICAL_COLUMNS]
)
def test_no_technical_column_can_move_the_verdict(key):
    """Remove any one of the eight and the answer is unchanged.

    `Abs TC` alone decides, in both directions. Eight parametrised
    removals is the cheapest statement of "these are not inputs".
    """
    base = dict(
        (k, "irrelevant") for k, _column in red_module.REDLINE_TECHNICAL_COLUMNS
    )

    for timecode, expected in (("03.17.13.27", False), ("03:17:37:20", True)):
        with_all = dict(base, timecode=timecode)
        without_one = dict(with_all)
        del without_one[key]

        verdict = red_module.Provider.anchor_yields_video_component
        assert verdict(_AnchorClip(with_all)) is expected
        assert verdict(_AnchorClip(without_one)) is expected


def test_the_verdict_is_unknown_without_abs_tc_however_many_columns_are_there(caplog):
    """Eight technical values are not a substitute for the one field read.

    A clip carrying resolution, frame rate, segment count and codec but
    no `timecode` still answers "I could not tell" — and the
    multi-component import still refuses to declare a budget on it.
    `Abs TC` is a REQUIRED column so this is unreachable through a scan;
    it is pinned because the capture makes the dict look informative.
    """
    metadatas = dict(
        (key, "irrelevant") for key, _column in red_module.REDLINE_TECHNICAL_COLUMNS
    )

    with caplog.at_level(logging.WARNING):
        verdict = red_module.Provider.anchor_yields_video_component(
            _AnchorClip(metadatas)
        )

    assert verdict is None


def test_end_abs_tc_is_not_the_field_that_decides(redline):
    """Trap 2: the separator varies per FIELD inside one clip.

    On the healthy, colon-separated clip measured on prod,
    `Abs TC`=`03:17:37:20` and `Edge TC`=`02:13:31:24` use colons while
    `End Abs TC`=`03.17.54.16` and `End Edge TC`=`02.13.48.20` use dots.
    A verdict read off `End Abs TC` would therefore classify EVERY clip
    as non-deducible — under-declaring the budget on the whole healthy
    majority, which is the loud `400 … VIDEO_COMPONENT`.

    Mutation killed: reading the verdict off any timecode field other
    than `Abs TC`.
    """
    metadatas = _metadatas(
        redline,
        stdout=_csv(
            **{
                "Abs TC": "03:17:37:20",
                "End Abs TC": "03.17.54.16",
                "End Edge TC": "02.13.48.20",
            }
        ),
    )

    assert metadatas["timecode"] == "03:17:37:20"
    # The dot-separated end timecodes are not captured at all, so they
    # cannot be mistaken for the field that decides.
    assert "end_timecode" not in metadatas
    assert (
        red_module.Provider.anchor_yields_video_component(_AnchorClip(metadatas))
        is True
    )


# --------------------------------------------------------------------------
# Nothing is exposed to Vidispine yet
# --------------------------------------------------------------------------


def test_the_new_keys_are_not_offered_as_mappable_metadata():
    """Capture only: declaring a key in `getAvailableMetadatas` makes it
    selectable in a `MetadataMapping`, i.e. writable into Portal — which
    is the Vidispine exposure this story explicitly defers to the next
    one (and which the spec marks *Ask First*).

    Mutation killed: adding the eight to the tuple "while we are here".
    """
    declared = dict(BaseProvider().getAvailableMetadatas())

    for key, _column in red_module.REDLINE_TECHNICAL_COLUMNS:
        assert key not in declared, (
            f"{key} became a mappable metadata key; exposing these to "
            f"Vidispine is the next story, not this one"
        )

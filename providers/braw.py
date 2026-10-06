# coding: utf-8
"""Blackmagic RAW (`.braw`) clips.

Neither Vidispine nor ffprobe can read a BRAW shape, so a placeholder
import of a `.braw` stays mute for ever. This provider therefore does
two things the others do not:

* at SCAN time it runs ``brawprobe`` (``tools/brawprobe``, built on the
  Portal server against the Blackmagic RAW SDK) ONCE per clip and stores
  EVERYTHING it prints — every clip-scope and frame-0 metadata key, as
  ``braw_<key>``, plus the technical fields as ``braw_probe_<field>``.
  Once a card is archived its metadata can never be read again, so the
  capture is exhaustive, not selective;
* at IMPORT time it declares that the anchor yields no video component
  (``MAIN_FILE_YIELDS_VIDEO: False``) and composes the whole `original`
  ShapeDocument from those stored values (``buildShapeDocument``), which
  ``Clip._post_shape_document`` posts and follows with the
  ``lowres-forge`` transcode — the route RED's dot-timecode clips take.
  ``brawprobe`` is never run at import.

The clip key is ``uuid5(BRAW_NAMESPACE, "camera_id|date_recorded|
clip_number")``. ``camera_id`` identifies the CAMERA, not the clip (the
SDK manual, and 15 clips over 5 bodies measured), and BRAW has no
per-clip id, so the three together are the identity. A clip missing any
of them is refused: falling back to a file hash would key the same clip
differently the day it is copied.
"""

import json
import logging
import math
import os
import re
import shutil
import subprocess as sp
import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from fractions import Fraction

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.models.settings import Settings
from portal.plugins.TapelessIngest.providers.providers import (
    MAIN_FILE_YIELDS_VIDEO,
    Provider as BaseProvider,
)

log = logging.getLogger(__name__)


BRAWPROBE_BINARY = "brawprobe"

# Where brawprobe is installed when it is not on the caller's PATH. Cron
# is the case that matters: /etc/crontab's PATH does not carry
# /usr/local/bin (see providers/red.py, REDLINE_FALLBACK_PATHS).
BRAWPROBE_FALLBACK_PATHS = ("/usr/local/bin/brawprobe",)

# A probe of a 12K clip takes ~2 s on a NAS mount. A hung SDK must cost
# the scan one clip, not the night.
BRAWPROBE_TIMEOUT_SECONDS = 300

BRAW_EXTENSION = ".braw"

# uuid5(NAMESPACE_URL, "https://pad.fr/TapelessIngest/providers/braw").
# FROZEN: changing it re-keys every BRAW clip ever ingested.
BRAW_NAMESPACE = uuid.UUID("0405c014-24e6-58fc-98b2-b0ce1ffbe0f7")

# The clip metadata keys the umid is derived from, in derivation order.
UMID_KEYS = ("camera_id", "date_recorded", "clip_number")

METADATA_PREFIX = "braw_"
FRAME0_PREFIX = "braw_frame0_"
PROBE_PREFIX = "braw_probe_"

# How long the stored `braw_*` names the mapping form offers are reused.
STORED_NAMES_TTL_SECONDS = 300
_stored_names_cache = None  # (expires at, names), see _stored_metadata_names

# `ClipMetadata.name` and `.value` are both CharField(max_length=200).
MAX_VALUE_LENGTH = 200
MAX_NAME_LENGTH = 200

# Never stored: the embedded 3D LUT's data (~430 000 bytes; brawprobe
# already leaves it out) and the lens-correction tables. The LUT's name,
# title, size and mode are ordinary keys and are kept, and so are the
# SCALAR lens keys (`lens_shading_enable`, `..._unit_length_in_micrometres`):
# the second pattern only drops ARRAYS.
EXCLUDED_KEYS = re.compile(r"post_3dlut_.*_data")
EXCLUDED_ARRAY_KEYS = re.compile(
    r"lens_distortion_correction_polynomial.*|lens_shading_.*"
)

# brawprobe's technical fields stored as `braw_probe_<field>`, with the
# label the mapping form shows. `path`, `sdk_dir` and `excluded` describe
# the run, not the clip, and are not stored.
PROBE_FIELDS = (
    ("clip", "File name"),
    ("camera", "Camera"),
    ("width", "Width"),
    ("height", "Height"),
    ("frame_count", "Frame count"),
    ("fps_num", "Frame rate numerator"),
    ("fps_den", "Frame rate denominator"),
    ("fps_reported", "Frame rate as reported"),
    ("sensor_rate_num", "Sensor rate numerator"),
    ("sensor_rate_den", "Sensor rate denominator"),
    ("sensor_rate_reported", "Sensor rate as reported"),
    ("offspeed", "Off-speed"),
    ("timecode", "Start timecode"),
    ("gamma_recorded", "Recorded gamma"),
    ("gamut", "Gamut"),
    ("audio_channels", "Audio channels"),
    ("audio_sample_rate", "Audio sample rate"),
    ("audio_bits", "Audio bit depth"),
    ("audio_samples", "Audio samples"),
    ("frame0_readable", "Frame 0 readable"),
    ("brawprobe_version", "brawprobe version"),
)

# The metadata keys measured over the five NAS bodies (Cinema Camera 6K,
# URSA Broadcast G2, URSA Mini Pro 12K, PYXIS 12K, Pocket Cinema Camera
# 6K Pro), already prefixed as they are stored. The mapping form offers
# these before any BRAW clip is scanned; keys a later camera adds are
# picked up from the stored rows (`getAvailableMetadatas`).
KNOWN_METADATA_KEYS = (
    "braw_analog_gain",
    "braw_analog_gain_is_constant",
    "braw_anamorphic",
    "braw_anamorphic_enable",
    "braw_braw_codec_bitrate",
    "braw_braw_compression_ratio",
    "braw_camera_id",
    "braw_camera_number",
    "braw_camera_operator",
    "braw_camera_type",
    "braw_clip_number",
    "braw_crop_origin",
    "braw_crop_size",
    "braw_date_recorded",
    "braw_day_night",
    "braw_director",
    "braw_encoder_device_manufacturer",
    "braw_environment",
    "braw_filters",
    "braw_firmware_version",
    "braw_format_frame_rate",
    "braw_frameguide_aspect_ratio",
    "braw_gamut_compression_enable",
    "braw_good_take",
    "braw_lens_chromatic_aberration_correction_enable",
    "braw_lens_distortion_correction_enable",
    "braw_lens_shading_enable",
    "braw_lens_type",
    "braw_location",
    "braw_manufacturer",
    "braw_multicard_volume_count",
    "braw_multicard_volume_number",
    "braw_offspeed",
    "braw_offspeed_frame_time",
    "braw_offspeed_is_constant",
    "braw_ois_enable",
    "braw_post_3dlut_embedded_bmd_gamma",
    "braw_post_3dlut_embedded_name",
    "braw_post_3dlut_embedded_size",
    "braw_post_3dlut_embedded_title",
    "braw_post_3dlut_mode",
    "braw_production_name",
    "braw_reel_name",
    "braw_rotation",
    "braw_safe_area",
    "braw_scene",
    "braw_sensor_area_captured",
    "braw_sensor_line_time",
    "braw_sensor_photosite_pitch_in_micrometres",
    "braw_shot_type",
    "braw_shutter_type",
    "braw_take",
    "braw_take_type",
    "braw_time_lapse_interval",
    "braw_tone_curve_contrast",
    "braw_tone_curve_highlights",
    "braw_tone_curve_midpoint",
    "braw_tone_curve_saturation",
    "braw_tone_curve_shadows",
    "braw_tone_curve_video_black_level",
    "braw_viewing_bmdgen",
    "braw_viewing_gamma",
    "braw_viewing_gamut",
    # Frame 0. `analog_gain` is also a clip key, hence the frame0 prefix.
    "braw_frame0_analog_gain",
    "braw_aperture",
    "braw_as_shot_kelvin",
    "braw_as_shot_tint",
    "braw_distance",
    "braw_exposure",
    "braw_focal_length",
    "braw_internal_nd",
    "braw_iso",
    "braw_lens_distortion_correction_polynomial_unit_length_in_micrometres",
    "braw_sensor_rate",
    "braw_shutter_value",
    "braw_white_balance_kelvin",
    "braw_white_balance_tint",
)

# ---------------------------------------------------------------------------
# The shape document
# ---------------------------------------------------------------------------
#
# Vidispine has never built a shape for a `.braw` (it cannot read one), so
# unlike RED's constants these are NOT read off a reference shape: they
# name the format and nothing else. Everything that varies per clip —
# frames, rate, resolution, audio — comes from the stored brawprobe
# fields, and a missing or unusable one REFUSES the clip. Fields no
# measurement supports (pixel format, bit depth, stream ids, timecode
# fields) are left out rather than guessed.
#
# pad_forge (`manifest.reconstruct_from_shape`) reads a single-file shape
# as one clip and orders its audio by `itemTrack`, which must therefore
# be stated: the BRAW's embedded sound is ONE component, `A1`.
BRAW_CONTAINER_FORMAT = "braw"
BRAW_VIDEO_CODEC = "braw"
BRAW_MIME_TYPE = "video/x-braw"
BRAW_FIELD_ORDER = "progressive"
BRAW_AUDIO_ITEM_TRACK = "A1"


# ---------------------------------------------------------------------------
# Rates and timecode — brawdump's, exactly
# ---------------------------------------------------------------------------

# brawdump's `snapRate` table (PAD Forge, agent-ffmpeg/brawdump/main.cpp),
# in its order: the order decides ties.
KNOWN_RATES = (
    (24000, 1001),
    (24, 1),
    (25, 1),
    (30000, 1001),
    (30, 1),
    (48, 1),
    (50, 1),
    (60000, 1001),
    (60, 1),
    (100, 1),
    (120, 1),
)
SNAP_TOLERANCE = 0.01


def snap_rate(fps):
    """``(num, den)`` of ``fps`` snapped as brawdump's ``snapRate`` does.

    The SDK answers rates with float error (29.970032 for a 29.97 clip),
    so a rate within 0.01 fps of a known one IS that one — the nearest,
    the first in table order on a tie. Anything else is ``fps * 1000``
    rounded half away from zero (C's ``llround``, not Python's banker's
    ``round``) over 1000, reduced.
    """
    fps = float(fps)
    best = None
    best_gap = 0.0
    for num, den in KNOWN_RATES:
        gap = abs(fps - num / den)
        if gap <= SNAP_TOLERANCE and (best is None or gap < best_gap):
            best, best_gap = (num, den), gap
    if best is not None:
        return best
    scaled = fps * 1000.0
    num = (
        int(math.floor(scaled + 0.5))
        if scaled >= 0
        else -int(math.floor(-scaled + 0.5))
    )
    den = 1000
    divisor = math.gcd(num, den)
    if divisor > 1:
        num, den = num // divisor, den // divisor
    return num, den


def normalise_timecode(timecode):
    """brawdump's rewrite: ``hh.mm.ss.ff`` becomes ``hh:mm:ss:ff``.

    Anything else is returned as it is — ``;`` included, which is the
    drop-frame separator.
    """
    if (
        isinstance(timecode, str)
        and len(timecode) == 11
        and timecode[2] == timecode[5] == timecode[8] == "."
    ):
        return f"{timecode[0:2]}:{timecode[3:5]}:{timecode[6:8]}:{timecode[9:11]}"
    return timecode


# ---------------------------------------------------------------------------
# The binary
# ---------------------------------------------------------------------------


def configured_brawprobe_path():
    """The operator's ``Settings.brawprobe_path``, or ``""`` — never raises."""
    try:
        return (Settings.objects.get(pk=1).brawprobe_path or "").strip()
    except Exception:
        log.debug("No configured brawprobe path available", exc_info=True)
        return ""


def _is_executable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_brawprobe_path():
    """The brawprobe to run: configured, then PATH, then known locations.

    Always an ABSOLUTE path (or the operator's own setting): cron's PATH
    does not carry /usr/local/bin, so a bare name is never handed on.

    Raises:
        TapelessIngestException: naming the binary and the setting to fill.
    """
    configured = configured_brawprobe_path()
    if configured:
        # Taken at its word only if it can be run as given: a relative
        # path would resolve against cron's working directory.
        if not os.path.isabs(configured) or not _is_executable(configured):
            raise TapelessIngestException(
                f"Settings.brawprobe_path is {configured!r}, which is not an "
                f"absolute path to an executable file — fix the TapelessIngest "
                f"setting, or empty it to auto-detect {BRAWPROBE_BINARY}"
            )
        return configured
    found = shutil.which(BRAWPROBE_BINARY)
    if found:
        return os.path.abspath(found)
    for candidate in BRAWPROBE_FALLBACK_PATHS:
        if _is_executable(candidate):
            return candidate
    raise TapelessIngestException(
        f"{BRAWPROBE_BINARY} not found: it is not on PATH "
        f"({os.environ.get('PATH', '')!r}), not at any of "
        f"{', '.join(BRAWPROBE_FALLBACK_PATHS)}, and no brawprobe_path is set "
        f"in the TapelessIngest settings (build it from tools/brawprobe)"
    )


# ---------------------------------------------------------------------------
# Pure helpers: probe JSON -> metadatas
# ---------------------------------------------------------------------------


def braw_umid(camera_id, date_recorded, clip_number):
    """The clip key. Each part stripped; an empty part refuses the clip."""
    parts = {
        "camera_id": camera_id,
        "date_recorded": date_recorded,
        "clip_number": clip_number,
    }
    stripped = {}
    for key in UMID_KEYS:
        value = parts[key]
        value = "" if value is None else str(value).strip()
        if not value:
            raise TapelessIngestException(
                f"the clip metadata has no {key}, which the BRAW clip key "
                f"(camera_id|date_recorded|clip_number) needs — refusing the "
                f"clip rather than keying it on anything else"
            )
        stripped[key] = value
    name = "|".join(stripped[key] for key in UMID_KEYS)
    return str(uuid.uuid5(BRAW_NAMESPACE, name))


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scalar_text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        # `1` and `1.0` are one value; brawprobe prints `%.9g`, which
        # already drops the `.0`, and a re-parsed float must agree.
        return str(int(value))
    return str(value)


def _encode(key, value):
    """``(text, is_json)`` — see ``stringify``."""
    if isinstance(value, list):
        if 2 <= len(value) <= 4 and all(_is_number(v) for v in value):
            separator = "/" if key.endswith("_rate") else "x"
            return separator.join(_scalar_text(v) for v in value), False
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False), True
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False), True
    return _scalar_text(value), False


def stringify(key, value):
    """A metadata value as the string `ClipMetadata` stores.

    Scalars as text. An array of 2 to 4 numbers is joined — ``/`` for a
    rate (``sensor_rate`` [48, 1] -> ``48/1``), ``x`` otherwise
    (``crop_size`` [6048, 3200] -> ``6048x3200``). Any other array or
    object is kept as compact JSON.
    """
    return _encode(key, value)[0]


# Values read back at import — to build the shape or the anchor's path.
# They are never truncated: one over 200 characters refuses the clip.
PROTECTED_VALUES = frozenset(
    [
        "clipname",
        PROBE_PREFIX + "clip",
        PROBE_PREFIX + "width",
        PROBE_PREFIX + "height",
        PROBE_PREFIX + "frame_count",
        PROBE_PREFIX + "fps_num",
        PROBE_PREFIX + "fps_den",
        PROBE_PREFIX + "audio_channels",
        PROBE_PREFIX + "audio_sample_rate",
        PROBE_PREFIX + "audio_bits",
        PROBE_PREFIX + "audio_samples",
        METADATA_PREFIX + "anamorphic",
        METADATA_PREFIX + "anamorphic_enable",
    ]
)


def _fit(name, text, is_json, where):
    """``text`` as it can be stored under ``name``, or ``None`` to skip.

    * a NAME over 200 characters is skipped (never truncated);
    * a value the import reads back is never truncated — refused;
    * JSON over 200 characters is skipped (a cut would be invalid JSON);
    * any other value over 200 characters is truncated.
    """
    if len(name) > MAX_NAME_LENGTH:
        log.warning(
            "braw: %s: the metadata name %s is %d characters long (the column "
            "holds %d); it is not stored",
            where,
            name,
            len(name),
            MAX_NAME_LENGTH,
        )
        return None
    if len(text) <= MAX_VALUE_LENGTH:
        return text
    if name in PROTECTED_VALUES:
        raise TapelessIngestException(
            f"{name} is {len(text)} characters long for {where}; it is read "
            f"back at import and the column holds {MAX_VALUE_LENGTH}, so the "
            f"clip is refused rather than stored with a cut value"
        )
    if is_json:
        log.warning(
            "braw: %s: %s is a %d-character JSON value; it is not stored "
            "(a cut would not be JSON)",
            where,
            name,
            len(text),
        )
        return None
    log.warning(
        "braw: %s: %s is %d characters long; only the first %d are stored",
        where,
        name,
        len(text),
        MAX_VALUE_LENGTH,
    )
    return text[:MAX_VALUE_LENGTH]


def _put(stored, name, text, is_json, where):
    """Store ``text`` under ``name``; never overwrite silently.

    A name already taken (a technical ``braw_probe_*`` key, or a raw key
    equal to an earlier one once trimmed) keeps its value: the newcomer is
    stored under ``<name>_raw`` (``_raw2``, ``_raw3``… if that is taken
    too), with a warning naming both.
    """
    target = name
    if target in stored:
        target = f"{name}_raw"
        attempt = 2
        while target in stored:
            target = f"{name}_raw{attempt}"
            attempt += 1
        log.warning(
            "braw: %s: %s is already stored; the colliding raw value is "
            "stored as %s",
            where,
            name,
            target,
        )
    fitted = _fit(target, text, is_json, where)
    if fitted is not None:
        stored[target] = fitted


def _is_excluded(key, value):
    if EXCLUDED_KEYS.fullmatch(key):
        return True
    return isinstance(value, list) and bool(EXCLUDED_ARRAY_KEYS.fullmatch(key))


def flatten_metadata(clip_metadata, frame0_metadata, where="brawprobe", stored=None):
    """Every clip and frame-0 key as ``braw_<key>`` -> string.

    Keys are whitespace-trimmed. A frame-0 key goes under
    ``braw_frame0_`` only when the clip carries the same (trimmed) key.
    ``stored`` may already hold names (the technical keys): those win, and
    a raw key landing on one is kept under a ``_raw`` name (see ``_put``).
    """
    clip_metadata = clip_metadata or {}
    frame0_metadata = frame0_metadata or {}
    stored = {} if stored is None else stored
    clip_keys = set()
    for raw_key, value in clip_metadata.items():
        key = str(raw_key).strip()
        if not key:
            continue
        clip_keys.add(key)
        if _is_excluded(key, value):
            continue
        _put(stored, METADATA_PREFIX + key, *_encode(key, value), where)
    for raw_key, value in frame0_metadata.items():
        key = str(raw_key).strip()
        if not key or _is_excluded(key, value):
            continue
        name = (FRAME0_PREFIX if key in clip_keys else METADATA_PREFIX) + key
        _put(stored, name, *_encode(key, value), where)
    return stored


def _positive_int(probe, field, where):
    value = probe.get(field)
    if (
        not _is_number(value)
        or not math.isfinite(value)
        or value <= 0
        or int(value) != value
    ):
        raise TapelessIngestException(
            f"brawprobe gave no usable {field} for {where} ({value!r}); its "
            f"output is not the shape this provider reads"
        )
    return int(value)


def aspect_ratio(width, height):
    divisor = math.gcd(int(width), int(height))
    return f"{int(width) // divisor}:{int(height) // divisor}"


def shooting_date(date_recorded):
    """``YYYY:MM:DD`` (the SDK's), ``YYYY-MM-DD`` or ``YYYY/MM/DD`` ->
    ``YYYY-MM-DD``; ``None`` for anything else."""
    text = "" if date_recorded is None else str(date_recorded).strip()
    for pattern in ("%Y:%m:%d", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _cross_check_rate(probe, field, num, den, where):
    """brawprobe's snapped rate is the authority; the Python ``snap_rate``
    of the SDK's reported float only checks it, and a disagreement is a
    warning — never an override."""
    reported = probe.get(f"{field}_reported")
    if not _is_number(reported) or not math.isfinite(reported) or reported <= 0:
        return
    if Fraction(*snap_rate(reported)) != Fraction(num, den):
        log.warning(
            "braw: %s: brawprobe snapped %s to %s/%s, but %r snaps to %s/%s "
            "here; brawprobe's is stored",
            where,
            field,
            num,
            den,
            reported,
            *snap_rate(reported),
        )


def _timecode(probe, where):
    value = probe.get("timecode")
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TapelessIngestException(
            f"brawprobe gave a timecode of type {type(value).__name__} for "
            f"{where} ({value!r}); its output is not the shape this provider reads"
        )
    return normalise_timecode(str(value).strip())


def metadatas_from_probe(probe, where="brawprobe"):
    """The whole contribution of one probe: identity, common keys, braw_*.

    Pure: a parsed brawprobe JSON in, a dict of strings out.

    Raises:
        TapelessIngestException: an identity key is missing, a technical
            field the shape needs is absent or unusable, or a value read
            back at import does not fit its column.
    """
    if not isinstance(probe, dict):
        raise TapelessIngestException(
            f"brawprobe printed {type(probe).__name__} for {where}, not a JSON object"
        )
    metadata = probe.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("clip"), dict):
        raise TapelessIngestException(
            f"brawprobe printed no metadata.clip object for {where}; its output "
            f"is not the shape this provider reads"
        )
    clip_metadata = metadata["clip"]
    frame0_metadata = (
        metadata.get("frame0") if isinstance(metadata.get("frame0"), dict) else {}
    )

    trimmed = {str(k).strip(): v for k, v in clip_metadata.items()}
    umid = braw_umid(*(trimmed.get(key) for key in UMID_KEYS))

    width = _positive_int(probe, "width", where)
    height = _positive_int(probe, "height", where)
    frames = _positive_int(probe, "frame_count", where)
    # brawprobe's snap is THE snap (one implementation of brawdump's rule).
    fps_num = _positive_int(probe, "fps_num", where)
    fps_den = _positive_int(probe, "fps_den", where)
    sensor_num = _positive_int(probe, "sensor_rate_num", where)
    sensor_den = _positive_int(probe, "sensor_rate_den", where)
    _cross_check_rate(probe, "fps", fps_num, fps_den, where)
    _cross_check_rate(probe, "sensor_rate", sensor_num, sensor_den, where)
    offspeed = Fraction(sensor_num, sensor_den) != Fraction(fps_num, fps_den)
    if isinstance(probe.get("offspeed"), bool) and probe["offspeed"] != offspeed:
        log.warning(
            "braw: %s: brawprobe says offspeed=%s, its own rates say %s; the "
            "rates are stored",
            where,
            probe["offspeed"],
            offspeed,
        )
    timecode = _timecode(probe, where)

    # The technical keys first: they win every collision (`_put`).
    stored = {}
    probe_values = dict(probe)
    probe_values.update(offspeed=offspeed, timecode=timecode)
    for field, _label in PROBE_FIELDS:
        if field in probe_values:
            _put(
                stored,
                PROBE_PREFIX + field,
                *_encode(field, probe_values[field]),
                where,
            )
    flatten_metadata(clip_metadata, frame0_metadata, where, stored=stored)

    duration = Fraction(frames * fps_den, fps_num)
    common = {
        "umid": umid,
        "clipname": str(trimmed.get("clip_number")).strip(),
        "framerate": f"{fps_num}/{fps_den}",
        "duration": repr(float(duration)),
        "aspect_ratio": aspect_ratio(width, height),
    }
    if timecode:
        common["timecode"] = timecode
    date = shooting_date(trimmed.get("date_recorded"))
    if date:
        common["shooting_date"] = date
    else:
        log.warning(
            "braw: %s: date_recorded %r is not a date; no shooting_date stored",
            where,
            trimmed.get("date_recorded"),
        )
    for key, source in (
        ("device_manufacturer", "manufacturer"),
        ("device_model", "camera_type"),
        ("device_serial", "camera_id"),
    ):
        value = trimmed.get(source)
        if value is not None and str(value).strip():
            common[key] = str(value).strip()
    ratio = trimmed.get("braw_compression_ratio")
    ratio = "" if ratio is None else str(ratio).strip()
    common["video_codec"] = f"Blackmagic RAW {ratio}" if ratio else "Blackmagic RAW"
    for key, value in common.items():
        fitted = _fit(key, value, False, where)
        if fitted is not None:
            stored[key] = fitted
    return stored


# `anamorphic_enable` values that mean "no", and `anamorphic` values that
# name no squeeze (compared lowercased, stripped).
NOT_ANAMORPHIC_ENABLE = frozenset(["", "0", "false", "off", "no"])
NOT_ANAMORPHIC_RATIO = frozenset(["", "none", "off"])


def is_anamorphic(metadatas):
    """Whether the stored clip metadata says the picture is squeezed."""
    enable = str(metadatas.get(METADATA_PREFIX + "anamorphic_enable") or "")
    ratio = str(metadatas.get(METADATA_PREFIX + "anamorphic") or "")
    return (
        enable.strip().lower() not in NOT_ANAMORPHIC_ENABLE
        or ratio.strip().lower() not in NOT_ANAMORPHIC_RATIO
    )


def _label(name):
    for prefix, title in (
        (PROBE_PREFIX, "BRAW probe"),
        (FRAME0_PREFIX, "BRAW frame 0"),
        (METADATA_PREFIX, "BRAW"),
    ):
        if name.startswith(prefix):
            return f"{title}: {name[len(prefix):]}"
    return name


class Provider(BaseProvider):
    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "Blackmagic RAW"
        self.machine_name = "braw"

    def getExtensions(self):
        # Lowercase is enough: discovery adds both `*.braw` and `*.BRAW`,
        # and the extraction pre-filter compares lowercased suffixes, so
        # this is a superset of the case-insensitive guard below.
        return [BRAW_EXTENSION]

    # -- scan -----------------------------------------------------------------

    def probe(self, media_absolute_path):
        """brawprobe's JSON for one clip. No shell, an absolute binary, and
        ``--`` before the path so no file name is read as an option."""
        binary = resolve_brawprobe_path()
        try:
            completed = sp.run(
                [binary, "--", media_absolute_path],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=BRAWPROBE_TIMEOUT_SECONDS,
            )
        except sp.TimeoutExpired:
            raise TapelessIngestException(
                f"{binary} did not answer within {BRAWPROBE_TIMEOUT_SECONDS} s "
                f"for {media_absolute_path}"
            )
        except OSError as error:
            raise TapelessIngestException(
                f"{binary} could not be run for {media_absolute_path}: {error}"
            )
        if completed.returncode != 0:
            raise TapelessIngestException(self._brawprobe_failure(binary, completed))
        try:
            return json.loads(completed.stdout or "")
        except ValueError as error:
            raise TapelessIngestException(
                f"{binary} exited 0 for {media_absolute_path} but printed no "
                f"JSON ({error})"
            )

    @staticmethod
    def _brawprobe_failure(binary, completed):
        stderr = (completed.stderr or "").strip()
        if len(stderr) > 500:
            stderr = stderr[:500] + "..."
        detail = f"; stderr: {stderr}" if stderr else " and said nothing on stderr"
        return f"{binary} refused the clip (exit {completed.returncode}){detail}"

    def getAllClipMetadatas(self, media_absolute_path, metadatas):
        probe = self.probe(media_absolute_path)
        metadatas.update(metadatas_from_probe(probe, media_absolute_path))
        return metadatas

    def getMetadatasFromFile(self, media_file, metadatas, context):
        filename, file_extension = os.path.splitext(media_file.getFileName())
        # Case-INSENSITIVE: `.BRAW` is the same format, and no other
        # provider claims it.
        if file_extension.lower() == BRAW_EXTENSION:
            metadatas["provider"] = self.machine_name
            metadatas["file_id"] = media_file.getId()
            metadatas["extension"] = file_extension
            media_absolute_path = self.get_file_absolute_path(media_file, context)
            metadatas = self.getAllClipMetadatas(media_absolute_path, metadatas)
        return metadatas

    # -- import ---------------------------------------------------------------

    def getClipMainMediaFile(self, clip, rebuild=False):
        if clip.file is None or rebuild:
            metadatas = clip.metadatas or {}
            name = metadatas.get(PROBE_PREFIX + "clip") or (
                (metadatas.get("clipname") or "")
                + (metadatas.get("extension") or BRAW_EXTENSION)
            )
            if not getattr(clip, "path", None) or name == BRAW_EXTENSION:
                raise TapelessIngestException(
                    f"clip {getattr(clip, 'umid', None)} has no folder path or "
                    f"no file name (path={getattr(clip, 'path', None)!r}), so "
                    f"its anchor cannot be located"
                )
            file_id = None
            path = os.path.join(clip.path, name)
        else:
            file_id = clip.file.getId()
            path = clip.file.getPath()
        return {
            "type": "video",
            "track": 1,
            "order": 0,
            "file_id": file_id,
            "path": path,
            # Vidispine deduces nothing from a `.braw`: the shape is stated
            # by `buildShapeDocument` instead.
            MAIN_FILE_YIELDS_VIDEO: False,
        }

    def getImportOptions(self):
        return {}

    @staticmethod
    def buildShapeDocument(main_file, extra_files, metadatas):
        """The whole `original` shape of a BRAW clip, from stored metadatas.

        Pure: dicts in, a dict out — no brawprobe, no Vidispine, no file.
        Container and video name the anchor; an audio component (``A1``)
        is declared only when the clip recorded sound.

        Raises:
            TapelessIngestException: no anchor file id, an extra file this
                document has no component for, or a stored technical value
                that is missing or unusable.
        """
        anchor_path = (main_file or {}).get("path") or "(no path)"
        anchor_id = (main_file or {}).get("file_id")
        if not anchor_id:
            raise TapelessIngestException(
                f"the anchor {anchor_path} has no Vidispine file id, so no "
                f"shape can name it"
            )
        if extra_files:
            named = ", ".join(
                str(f.get("path")) if isinstance(f, Mapping) else repr(f)
                for f in extra_files
            )
            raise TapelessIngestException(
                f"{len(extra_files)} extra media file(s) ({named}) and a "
                f"BRAW shape declares only the anchor — nothing is posted"
            )
        metadatas = metadatas or {}
        width = _stored_int(metadatas, "width", anchor_path)
        height = _stored_int(metadatas, "height", anchor_path)
        frames = _stored_int(metadatas, "frame_count", anchor_path)
        fps_num = _stored_int(metadatas, "fps_num", anchor_path)
        fps_den = _stored_int(metadatas, "fps_den", anchor_path)
        channels = _stored_int(
            metadatas, "audio_channels", anchor_path, allow_zero=True
        )

        duration = {
            "samples": frames,
            "timeBase": {"numerator": fps_den, "denominator": fps_num},
        }
        video = {
            "file": [{"id": anchor_id}],
            "duration": duration,
            "resolution": {"width": width, "height": height},
            "codec": BRAW_VIDEO_CODEC,
            "averageFrameRate": {"numerator": fps_num, "denominator": fps_den},
            "fieldOrder": BRAW_FIELD_ORDER,
        }
        # Square pixels unless the camera recorded anamorphic: the
        # de-squeeze factor of an anamorphic clip has not been measured.
        if not is_anamorphic(metadatas):
            video["pixelAspectRatio"] = {"horizontal": 1, "vertical": 1}
        document = {
            "containerComponent": {
                "file": [{"id": anchor_id}],
                "format": BRAW_CONTAINER_FORMAT,
                "duration": dict(duration),
            },
            "videoComponent": [video],
            "mimeType": [BRAW_MIME_TYPE],
        }
        if channels:
            rate = _stored_int(metadatas, "audio_sample_rate", anchor_path)
            bits = _stored_int(metadatas, "audio_bits", anchor_path)
            samples = _stored_int(metadatas, "audio_samples", anchor_path)
            if bits % 8:
                raise TapelessIngestException(
                    f"brawprobe gave {bits}-bit audio for {anchor_path}, which is "
                    f"not a whole number of bytes — nothing is posted"
                )
            time_base = {"numerator": 1, "denominator": rate}
            document["audioComponent"] = [
                {
                    "file": [{"id": anchor_id}],
                    "codec": f"pcm_s{bits}le",
                    "channelCount": channels,
                    "blockAlign": channels * bits // 8,
                    "bitrate": rate * channels * bits,
                    "timeBase": time_base,
                    "duration": {"samples": samples, "timeBase": dict(time_base)},
                    "itemTrack": BRAW_AUDIO_ITEM_TRACK,
                }
            ]
        return document

    # -- mapping --------------------------------------------------------------

    def getAvailableMetadatas(self):
        """The base keys, then every ``braw_*`` key this plugin can store.

        The measured inventory plus whatever the stored rows carry, so a
        key a newer camera adds becomes mappable as soon as one of its
        clips is scanned.
        """
        names = list(PROBE_PREFIX + field for field, _label in PROBE_FIELDS)
        names += list(KNOWN_METADATA_KEYS)
        names += self._stored_metadata_names()
        seen = set()
        braw = []
        for name in names:
            if name not in seen:
                seen.add(name)
                braw.append(name)
        labels = dict(
            (PROBE_PREFIX + f, f"BRAW probe: {label}") for f, label in PROBE_FIELDS
        )
        return tuple(BaseProvider.getAvailableMetadatas(self)) + tuple(
            (name, labels.get(name) or _label(name)) for name in braw
        )

    @staticmethod
    def _stored_metadata_names():
        """Distinct stored ``braw_*`` names of ``braw`` clips, memoized.

        The DISTINCT over `ClipMetadata` is cached for
        `STORED_NAMES_TTL_SECONDS` (a failure included, as "none"), so a
        key a new camera adds reaches the mapping form up to 5 minutes
        after its first clip is scanned.
        """
        global _stored_names_cache
        now = time.monotonic()
        cached = _stored_names_cache
        if cached is not None and cached[0] > now:
            return list(cached[1])
        try:
            from portal.plugins.TapelessIngest.models.clip import ClipMetadata

            names = sorted(
                ClipMetadata.objects.filter(
                    clip__provider_name="braw", name__startswith=METADATA_PREFIX
                )
                .values_list("name", flat=True)
                .distinct()
            )
        except Exception:
            # Cached too: a failing query must not be retried per row.
            log.debug("braw: stored metadata names unavailable", exc_info=True)
            names = []
        _stored_names_cache = (now + STORED_NAMES_TTL_SECONDS, tuple(names))
        return names


def _stored_int(metadatas, field, anchor_path, allow_zero=False):
    """``braw_probe_<field>`` as an int, or a refusal naming it."""
    key = PROBE_PREFIX + field
    raw = metadatas.get(key)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = -1
    if value < 0 or (value == 0 and not allow_zero):
        raise TapelessIngestException(
            f"no usable {key} is stored for {anchor_path} ({raw!r}), so the "
            f"shape would state nothing where it must state a number — "
            f"nothing is posted"
        )
    return value

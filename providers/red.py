# coding: utf-8

import logging

import subprocess as sp
import urllib
import shutil
import sys
import os
import csv
import re
import wave
from datetime import datetime
from decimal import Decimal, InvalidOperation
from io import StringIO
from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models.clip import (
    Clip,
    ClipFile,
    ClipMetadata,
    segment_stem,
)
from portal.plugins.TapelessIngest.models.settings import Settings

from portal.plugins.TapelessIngest.providers.providers import (
    MAIN_FILE_YIELDS_VIDEO,
    Provider as BaseProvider,
)

log = logging.getLogger(__name__)


REDLINE_BINARY = "REDline"

# Where REDline is installed when it is NOT on the caller's PATH. Cron is
# the case that matters: /etc/crontab declares
# PATH=/sbin:/bin:/usr/sbin:/usr/bin, which does not carry /usr/local/bin,
# so the nightly scan could not run the binary an interactive shell finds
# instantly. Every R3D in the run then failed with "No UMID found in file".
REDLINE_FALLBACK_PATHS = (
    "/usr/local/bin/REDline",
    "/usr/bin/REDline",
    "/opt/red/REDline",
)

# The two whole-field shapes REDline prints for `Abs TC`, and the ONLY
# two this provider claims to understand.
#
# `00:59:47:14` is the ordinary one. `00.48.41.06` — dot separators —
# carries the flag conventionally called drop-frame, and is deliberately
# treated as a SIGNAL rather than as drop-frame SEMANTICS: the same flag
# appears at 50 fps, where drop-frame is not defined. What it predicts,
# measured 110/110 on the 2026 STARLUX shoot, is that Vidispine's shape
# deduction extracts no essence from the `.R3D` and answers with a
# binaryComponent instead of a video one (open Codemill ticket on the
# decoder).
#
# Both patterns are anchored end to end on purpose: the WHOLE field is
# matched, never a substring. A substring test ("does it contain a dot")
# would classify a path, a date or a truncated field as non-deducible,
# and departing from the pre-existing declaration on a field this
# provider cannot actually read is a guess — so anything matching NEITHER
# pattern falls back to `True` (today's declaration) with a WARNING. That
# fallback is compatibility, not safety: over-declaring is the silent
# failure (defect A) and under-declaring is the loud 400. It is the right
# fallback only because a value this provider cannot parse is no evidence
# at all, and `Abs TC` is a REQUIRED column, so the case is unreachable
# through a real scan.
#
# `;` — the SMPTE separator conventionally used for drop-frame — is NOT
# matched, deliberately: across 110/110 `.R3D` files of the 2026 STARLUX
# shoot this REDline build printed dots and never a semicolon, so a `;`
# arm would be an unmeasured guess. It would land in the fallback above,
# warn, and keep today's declaration — which is the honest answer for a
# shape this provider has never observed.
DEDUCIBLE_TIMECODE = re.compile(r"\d{2}:\d{2}:\d{2}:\d{2}")
UNDEDUCIBLE_TIMECODE = re.compile(r"\d{2}\.\d{2}\.\d{2}\.\d{2}")

# The TECHNICAL columns, as `metadatas` key -> REDline column name.
#
# CAPTURE ONLY. Every value is copied out of the CSV exactly as REDline
# printed it — a string, never an int, never a float, never a quotient.
# `Total Frames / FPS` reproduces bit-for-bit the `durationSeconds`
# Vidispine writes when its shape deduction succeeds (`1012 / 60` =
# `16.866666666666667` on VX-216302, measured 2026-09-22), and
# `Frame Width x Frame Height` its `originalWidth x originalHeight` —
# but DERIVING either of them is a later story's business, and doing it
# here would put a computed number in a table whose other rows are
# transcriptions. What this story owes is the measurement, unaltered.
#
# All eight are printed by the `--printMeta 3` call `getAllClipMetadatas`
# already makes: no second REDline invocation, no probe, no decode. They
# were measured present and identically populated on BOTH a dot-separated
# clip (`K001_K067_0804BF_001.R3D`, the shape Vidispine extracts no
# essence from) and a colon-separated one (`K001_K068_0804LK_001.R3D`) —
# the media is not the problem, only Vidispine's decoder is.
#
# Two values mean less than they look like:
#
# - `Total Frames` on the anchor covers the WHOLE TAKE, not the anchor's
#   own segment (`File Segments=2` -> `Total Frames=1012` while
#   `Clip Out=1011`). Whatever later reads it must never sum the
#   segments.
# - `Camera Audio Channels` can be `2` while `WAV Filename` is EMPTY:
#   the audio is inside the `.R3D`, and on a deducible clip Vidispine
#   emits an `audioComponent` naming the `.R3D` itself.
#
# And none of them is a verdict. `Abs TC` stays the only field
# `anchor_yields_video_component` reads; a second opinion derived from
# `REDCODE` or `FPS` would be an unmeasured guess competing with a
# measurement.
REDLINE_TECHNICAL_COLUMNS = (
    ("frame_width", "Frame Width"),
    ("frame_height", "Frame Height"),
    ("fps", "FPS"),
    ("record_fps", "Record FPS"),
    ("total_frames", "Total Frames"),
    ("file_segments", "File Segments"),
    ("redcode", "REDCODE"),
    ("camera_audio_channels", "Camera Audio Channels"),
)

# The columns getAllClipMetadatas reads out of --printMeta 3.
#
# ONE tier, deliberately: a column this provider reads and cannot find
# means the CSV is not the shape this provider claims to understand, and
# that must fail loudly rather than persist a blank. A second,
# non-fatal tier is the documented fallback IF one of the eight turns
# out absent on a REDline older than prod's (2025-11-25) — not the
# default.
REDLINE_REQUIRED_COLUMNS = (
    "Clip Name",
    "UUID",
    "Abs TC",
    "Date",
    "Timestamp",
    "Camera Model",
    "Camera PIN",
) + tuple(column for _key, column in REDLINE_TECHNICAL_COLUMNS)

# Where a RED card's media sits, RELATIVE to the folder being scanned —
# and the whole of what "card structure" now means.
#
# Card structure is OPTIONAL and NON-DISCRIMINATING. A folder is a RED
# card folder because its name ENDS IN `.RDM` or `.RDC`, in any case —
# never because it spells a camera letter, a reel number and a
# six-character shoot id. BOTH levels are optional and BOTH accept either
# extension, and the copy suffix an `.RDC` may carry (`…_002.RDC`,
# `…_S000.RDC`, any) is never discriminating.
#
# The precise pattern this replaces —
# `[A-Z][0-9]{3}_[0-9A-Z]{6}.RDM/[A-Z][0-9]{3}_[A-Z][0-9]{3}_[0-9A-Z]{6}.RDC`
# — recognised exactly one on-disk shape. Rushes copied loose (no `.RDM`
# level, or a copy suffix on the `.RDC`) missed it and fell through to
# the extension declaration, where the bare `.r3d` matches EVERY segment
# and each one was probed as its own clip candidate. Measured on prod
# 2026-08-27: 132 such `.RDC` folders across five shoots spanning
# 2022-2026, of which 24 clips ended up anchored on a middle segment.
#
# "Ends in `.RDM`/`.RDC`" is the convention PAD already runs on: the
# `collections_ignore_folder` SETTING is configured with `.+\.RDM` and
# `.+\.RDC` on the production server. That is an operator configuration,
# not a shipped default (`models/settings.py` defaults the field to
# `""`), so it is corroboration for the convention — not an invariant
# this code may assume.
#
# Case-tolerant because the story exists for copies made any which way,
# and expressed with character classes only: `[^/]`, `[Rr]`, `+`, `?`
# and `\.` mean the same thing to Lucene (the legacy query) and to
# Python (`scan/discovery.py`'s client-side evaluator), so both discovery
# paths read it identically.
CARD_SUBPATH_REGEXP = r"[^/]+\.[Rr][Dd][MmCc](/[^/]+\.[Rr][Dd][MmCc])?"

# The runtime guard's extension, exactly. `getSegmentedExtensions` must
# declare the case the guard accepts and nothing wider — see the note
# there.
R3D_EXTENSION = ".R3D"

# `getFilesInStorage`'s hard page size when collecting a clip's segments.
# A RED segment is ~2 GB, so a take with more than this many files does
# not exist; the ceiling is here so that hitting it is REPORTED rather
# than silently truncating a clip's media.
SEGMENT_FILE_LIMIT = 1000

# ---------------------------------------------------------------------------
# The shape document for an anchor Vidispine deduces nothing from
# ---------------------------------------------------------------------------
#
# When `anchor_yields_video_component` answers False, Vidispine extracts
# no essence from the `.R3D` at all: the item comes out with a
# `binaryComponent`, no duration, no resolution, no codec — and, for a
# single-segment clip, `mediaType = 'data'`, so no duration or type
# search ever finds it (measured on prod 2026-09-22, VX-216267 and
# VX-216301). The plugin therefore states the shape itself.
#
# EVERY CONSTANT BELOW WAS READ OFF A SHAPE VIDISPINE BUILT ITSELF —
# `VX-455007`, the `original` shape of `VX-216302`, the deducible twin of
# the broken clip, same card, 24 seconds apart (prod, 2026-09-22). They
# are format facts about an `.R3D`, not guesses, and they are the only
# values in the document that do not come from REDline. Everything that
# varies per clip — duration, resolution, frame rate — is read from the
# columns `REDLINE_TECHNICAL_COLUMNS` captured, and a column that is
# missing or unreadable REFUSES the clip rather than inventing a number.
#
# What the reference shape carries and this document deliberately does
# NOT:
#
# * `startTimecode`, `firstSMPTETimecode`, `timeCodeTimeBase`,
#   `roundedTimeBase`, `dropFrame`. On a COLON-separated anchor these are
#   measured; on a DOT-separated one — the only population this document
#   is ever built for — they are not, and `dropFrame` in particular has
#   never been measured on such a clip. An unmeasured field written as a
#   fact is worse than an absent one, so they are omitted (spec: "Out of
#   Scope").
# * an `audioComponent` for the audio INSIDE the `.R3D`. The reference
#   has one (`pcm_s32le`, 2 channels, 48000), but no captured column
#   gives its sample rate, and the prod probe of 2026-09-22 proved it
#   unnecessary: a document declaring none still produced a proxy WITH
#   AAC audio, because pad_forge builds `AudioTrack(anchor, 0)` from the
#   file itself. `Camera Audio Channels` can read `2` while
#   `WAV Filename` is empty — that is this case, and it stays silent.
#
# A SEPARATE `.wav` beside the card is NOT that case and IS declared —
# see `_audio_component`. 322 RED clips on prod carry one (10 shoots,
# 2022-2026, measured 2026-09-22), so dropping it silently, or refusing
# the clip, were both wrong. Its parameters are not REDline's business
# and not guessed either: they belong to the `.wav`, and the `.wav`
# declares them in its own header.
R3D_CONTAINER_FORMAT = "R3D"
R3D_VIDEO_CODEC = "r3d_raw"
R3D_PIXEL_FORMAT = "rgb48le"
R3D_BIT_DEPTH = 16
R3D_FIELD_ORDER = "progressive"
R3D_MIME_TYPE = "video/x-raw-red"

# The separate `.wav`'s component, modelled on the shape Vidispine built
# ITSELF for `A002_A021_0526HT` — a 9-segment RED clip with a `.wav`,
# read off prod 2026-09-22. Everything else about that component is
# derived from the header (`_audio_component`); these four are the
# constants it carried.
WAV_ITEM_TRACK = "A1"
WAV_ESSENCE_STREAM_ID = 0
WAV_FRAME_SIZE = 1
WAV_CHANNEL_LAYOUT = 0

# `sampleFormat` is the ONE audio field that does not fall out of the
# header arithmetic, so it is a MEASUREMENT TABLE, not a formula: a
# 3-byte (24-bit) `.wav` carried `AV_SAMPLE_FMT_S32` on the reference
# shape — FFmpeg widens 24-bit samples to 32 — and nothing else has been
# measured. A width absent from this table omits the field rather than
# extrapolating the pattern, which is the same rule `dropFrame` follows.
WAV_SAMPLE_FORMATS = {3: "AV_SAMPLE_FMT_S32"}

# The denominator every frame rate is expressed over. The reference shape
# states 60 fps as `averageFrameRate = {60000, 1000}` and the take's
# duration as `timeBase = {1000, 60000}` — the same rational, inverted.
# 1000 is what makes REDline's three decimals exact: `23.976` becomes
# `23976/1000`, not a float rounded twice.
FRAME_RATE_SCALE = 1000


def configured_redline_path():
    """The operator's ``Settings.redline_path``, or ``""``.

    Every failure — no settings row, no DB, an older schema without the
    column — degrades to "not configured" so resolution falls through to
    discovery. Resolving a binary must never be what breaks a scan.
    """
    try:
        return (Settings.objects.get(pk=1).redline_path or "").strip()
    except Exception:
        log.debug("No configured REDline path available", exc_info=True)
        return ""


def _is_executable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def resolve_redline_path():
    """The REDline to run: configured, then PATH, then known locations.

    Raises:
        TapelessIngestException: naming the binary and the setting to fill
            in, so an operator reading the scan report knows where to act.
    """
    configured = configured_redline_path()
    if configured:
        return configured
    found = shutil.which(REDLINE_BINARY)
    if found:
        return found
    for candidate in REDLINE_FALLBACK_PATHS:
        if _is_executable(candidate):
            return candidate
    raise TapelessIngestException(
        f"{REDLINE_BINARY} not found: it is not on PATH "
        f"({os.environ.get('PATH', '')!r}), not at any of "
        f"{', '.join(REDLINE_FALLBACK_PATHS)}, and no redline_path is set in "
        f"the TapelessIngest settings"
    )


# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):
    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "RED"
        self.machine_name = "red"
        self.base_path = ""
        self.clips_path = ""
        self.index_xml = None
        self.card_xml_file = None
        self.clips_file_extension = ".RDC"

    def getExtensions(self):
        # Both forms on purpose. `.r3d` keeps the declaration a superset
        # of the runtime guard below (`== ".R3D"`), which is what
        # decides: drop it and an uppercase non-`_001` file flips to
        # `file` — a different umid. `_001.r3d` is now redundant with it
        # (nothing selects the anchor in the query any more, see
        # `getFilters`), and it is kept because narrowing a declaration
        # is the one direction that can silently change which provider
        # claims a file.
        return ["_001.r3d", ".r3d"]

    def getSegmentedExtensions(self):
        """The suffix whose files are segments of one take, not clips.

        A RED take longer than the card's file-size limit is written as
        ``X_001.R3D``, ``X_002.R3D`` … ``X_NNN.R3D`` — N files, ONE clip,
        one UUID. The query no longer picks the anchor out (see
        ``getFilters``), so the assembly rules in ``models/clip.py`` do:
        ``_001`` anchors, its siblings are extras
        ``getClipAdditionalMediaFiles`` re-attaches at ingest, and an
        increment with no ``_001`` and other increments beside it is an
        incomplete copy.

        UPPERCASE, matching the runtime guard below EXACTLY, and matched
        case-sensitively by the grouping. Declaring ``".r3d"`` here would
        make grouping suppress ``x_002.r3d`` — a file this provider's
        guard DECLINES, and which the `file` provider then claims as a
        clip of its own with no extras of any kind. Net effect of the
        wider declaration: one lonely `file` clip and every other segment
        of that card silently dropped. A grouping declaration must never
        be wider than the guard that will actually claim the anchor.

        This is the opposite direction from ``getExtensions()``, which
        must be a SUPERSET of the guard: that one decides who is OFFERED
        a file, this one decides who is DENIED one.

        These are EXTRA FILES, not spanned clips: `red` segments are one
        take's media split into files in one place (one row, N files),
        where ``Clip.spanned``/``getSpannedClips()`` are for a take split
        across several physical cards (N linked rows, one master). The
        two are complementary; nothing here touches a spanned field. See
        ``models/clip.py``'s grouping note.
        """
        return [R3D_EXTENSION]

    def getFilters(self, escaped_path):
        # `wildcard *.R3D`, not `*_001.R3D`: SELECTING the anchor was a
        # query concern only as long as the card pattern above was
        # precise enough to bound the reach. Grouping is an assembly
        # concern now, so discovery returns every segment and the
        # assembly rules decide which one anchors a clip. The name
        # clause stays — without it this filter would claim the `.wav`,
        # `.RMD` and `.mov` sidecars sitting beside the media in a card
        # folder, which no provider here would then claim.
        return [
            {
                "bool": {
                    "must": [
                        {
                            "regexp": {
                                "parent": os.path.join(
                                    escaped_path,
                                    CARD_SUBPATH_REGEXP,
                                )
                            }
                        },
                        # Either case, because the copies this story
                        # exists for were made any which way. A nested
                        # `should` inside the `must`, which is the same
                        # subset `scan/discovery.py` already models.
                        {
                            "bool": {
                                "should": [
                                    {"wildcard": {"name": "*.R3D"}},
                                    {"wildcard": {"name": "*.r3d"}},
                                ]
                            }
                        },
                    ]
                }
            },
        ]

    def getAllClipMetadatas(self, media_absolute_path, metadatas):
        """Read a clip's metadata off REDline's ``--printMeta 3`` CSV.

        No shell: the resolved binary is argv[0] and the media path is its
        own argument, so a storage root containing spaces
        (``AA - RUSHES TAPELESS``) needs no quoting round-trip.

        A run that yields no data row RAISES. It used to return
        ``metadatas`` untouched, which the caller two frames up rewrote
        into ``No UMID found in file <path>`` — a message that blames the
        media for what was really a missing binary, an unreadable file or
        a licence problem, and which cost a full production shoot before
        anyone could tell those apart.
        """
        redline = resolve_redline_path()
        cmd = [
            redline,
            "--i",
            media_absolute_path,
            "--printMeta",
            "3",
            "--useMeta",
        ]
        p = sp.run(cmd, capture_output=True, text=True)
        rows = list(csv.DictReader(StringIO(p.stdout or ""), delimiter=","))
        # REDline exits 1 on SUCCESS — a real KOMODO 6K .R3D that prints a
        # full CSV row still returns 1 — so the exit status cannot gate
        # parsing. Emptiness of the output is the only usable signal; the
        # status is kept only to put in the error message.
        if not rows:
            raise TapelessIngestException(self._redline_failure(redline, p))
        for row in rows:
            missing = [c for c in REDLINE_REQUIRED_COLUMNS if row.get(c) is None]
            if missing:
                raise TapelessIngestException(
                    f"{redline} returned a CSV without {', '.join(missing)} for "
                    f"{media_absolute_path} (exit {p.returncode}); its output "
                    f"shape is not the one this provider reads"
                )
            metadatas["clipname"] = row["Clip Name"]
            metadatas["umid"] = row["UUID"]
            metadatas["timecode"] = row["Abs TC"]
            metadatas["shooting_date"] = datetime.strptime(
                f"{row['Date']} {row['Timestamp']}", "%Y%m%d %H%M%S"
            ).isoformat()
            metadatas["device_manufacturer"] = "RED"
            metadatas["device_model"] = row["Camera Model"]
            metadatas["device_serial"] = row["Camera PIN"]
            # Copied, never computed: see REDLINE_TECHNICAL_COLUMNS.
            for key, column in REDLINE_TECHNICAL_COLUMNS:
                metadatas[key] = row[column]
        return metadatas

    @staticmethod
    def _redline_failure(redline, completed):
        """The message for a REDline run that produced no metadata row.

        Carries the three things the exit status alone cannot give an
        operator reading the nightly report: which binary ran, what it
        exited with, and what it said on stderr.
        """
        stderr = (completed.stderr or "").strip()
        if len(stderr) > 500:
            stderr = stderr[:500] + "..."
        detail = f"; stderr: {stderr}" if stderr else " and said nothing on stderr"
        return (
            f"{redline} returned no metadata row "
            f"(exit {completed.returncode}){detail}"
        )

    def getMetadatasFromFile(self, media_file, metadatas, context):
        filename, file_extension = os.path.splitext(media_file.getFileName())
        # The one guard, and the reason `getSegmentedExtensions()`
        # declares `.R3D` uppercase: it is case-SENSITIVE, so an
        # `x_001.r3d` is not this provider's and grouping must not
        # suppress its siblings on this provider's behalf.
        if file_extension == R3D_EXTENSION:
            metadatas["provider"] = self.machine_name
            metadatas["clipname"] = filename
            metadatas["file_id"] = media_file.getId()
            metadatas["extension"] = file_extension
            media_absolute_path = self.get_file_absolute_path(media_file, context)
            metadatas = self.getAllClipMetadatas(media_absolute_path, metadatas)
        return metadatas

    def getClipMainMediaFile(self, clip, rebuild=False):
        if clip.file is None or rebuild:
            file_id = None
            path = os.path.join(clip.path, clip.metadatas["clipname"])
        else:
            file_id = clip.file.getId()
            path = clip.file.getPath()
        return {
            "type": "video",
            "track": 1,
            "order": 0,
            "file_id": file_id,
            "path": path,
            MAIN_FILE_YIELDS_VIDEO: self.anchor_yields_video_component(clip, path),
        }

    @classmethod
    def anchor_yields_video_component(cls, clip, path=None):
        """Whether this anchor will fill a video slot of its own.

        Read off ``metadatas["timecode"]``, which is REDline's ``Abs TC``
        — a REQUIRED column (``REDLINE_REQUIRED_COLUMNS``), so a clip
        without it raised long before it got here — persisted as a
        ``ClipMetadata`` row by the scan. No new REDline call, no extra
        query (``getClipAdditionalMediaFiles`` already dereferences
        ``clip.metadatas`` on this same path), and no migration.

        Only the two shapes this provider has actually observed decide
        anything. A missing value, a non-string, or a string matching
        neither answers ``None`` — "could not tell" — WITH A WARNING. It
        used to answer ``True``, the pre-existing declaration, on the
        argument that a field this provider cannot parse is no evidence
        about the anchor's essence; that is true, and it is exactly why
        ``True`` was wrong: a budget declared without evidence is
        defect A (silent over-declaration) or the loud ``400 …
        VIDEO_COMPONENT`` (under-declaration), and a guess cannot know
        which. Ruled 2026-09-02 (spec D6): the provider says it could not
        tell, and the multi-component import refuses to declare on that
        — the clip is reported failed with the reason, nothing is
        imported. The single-component path never reads the verdict.

        The verdict on the DEDUCIBLE majority is logged at DEBUG: it is
        one line per clip on every non-RED-drop-frame ingest, and the
        line that had to exist is the one naming the departure.
        """
        metadatas = getattr(clip, "metadatas", None) or {}
        try:
            timecode = metadatas.get("timecode")
        except AttributeError:
            timecode = None
        if isinstance(timecode, str):
            # `.strip()`: REDline's CSV can carry surrounding whitespace,
            # and a stray space would send an otherwise perfectly
            # readable timecode to the fallback below — the SILENT
            # over-declaration.
            timecode = timecode.strip()
            if UNDEDUCIBLE_TIMECODE.fullmatch(timecode):
                log.info(
                    "red: anchor %s has timecode %r (dot-separated) — Vidispine "
                    "deduces no video component from it, so the placeholder "
                    "budget must not declare a video slot for it",
                    path,
                    timecode,
                )
                return False
            if DEDUCIBLE_TIMECODE.fullmatch(timecode):
                log.debug(
                    "red: anchor %s has timecode %r — it contributes a video "
                    "component of its own",
                    path,
                    timecode,
                )
                return True
        # `%s` names the CLIP, not only the path: on the REST path a
        # `Clip(**validated_data)` can arrive with a `metadatas` dict
        # that has no `timecode` at all, and that path has no operator
        # report — this line in `portal.log` is the only trace, so it has
        # to be greppable by umid.
        log.warning(
            "red: clip %s, anchor %s has an unreadable timecode %r — this "
            "provider cannot tell whether the anchor contributes a video "
            "component, so it declares nothing: a multi-component import of "
            "this clip will be refused rather than declare a guessed budget. "
            "Only %s and %s are understood",
            getattr(clip, "umid", None),
            path,
            timecode,
            DEDUCIBLE_TIMECODE.pattern,
            UNDEDUCIBLE_TIMECODE.pattern,
        )
        return None

    @staticmethod
    def segment_selector(anchor_name):
        """``(glob, pattern)`` selecting ``anchor_name``'s sibling segments.

        ``glob`` is what the storage query can express (``*``); ``pattern``
        is the exact rule the returned names are then held to. Both are
        derived from the ANCHOR'S OWN FILENAME, never from REDline's
        ``Clip Name`` — for renamed or re-wrapped rushes, which is this
        story's whole population, the two diverge and the CSV name selects
        nothing, silently costing the clip every segment but its first.

        The pattern is what stops a neighbouring clip being swallowed: a
        glob of ``A001_*`` matches ``A001_B_004.R3D`` as happily as
        ``A001_004.R3D`` whenever one stem prefixes another, which the new
        flat-folder shape makes reachable. Only ``<stem>_<three
        digits><same extension>`` survives.

        Returns ``None`` when the anchor carries no increment — a clip
        with no segments has no siblings to look for.
        """
        parsed = segment_stem(anchor_name)
        if parsed is None:
            return None
        stem, _index, extension = parsed
        glob = f"{stem}_*{extension}"
        pattern = re.compile(rf"{re.escape(stem)}_[0-9]{{3}}{re.escape(extension)}")
        return glob, pattern

    def getClipAdditionalMediaFiles(self, clip):
        files = []
        main_file_id = clip.file.getId()
        selector = self.segment_selector(os.path.basename(clip.file.getPath()))
        if selector is not None:
            glob, pattern = selector
            file_part_path = os.path.join(clip.path, glob)
            _ret = clip.get_storage_helper().getFilesInStorage(
                SEGMENT_FILE_LIMIT,
                0,
                path=urllib.parse.quote(file_part_path, safe="*/"),
                sort="filename",
            )
            _ret_files = _ret["files"]
            if len(_ret_files) >= SEGMENT_FILE_LIMIT:
                # Truncation here loses media from an item that will look
                # perfectly imported. Report it rather than trim quietly.
                log.error(
                    f"{clip.umid}: the segment listing for {file_part_path} hit "
                    f"the {SEGMENT_FILE_LIMIT}-file page limit, so this clip may "
                    f"be missing segments; its media is incomplete"
                )
            # Sorted by name here as well as in the query: the ORDER is
            # the shape's track order, and it must not depend on what a
            # storage backend chooses to mean by sort="filename".
            selected = sorted(
                (
                    _ret_file
                    for _ret_file in _ret_files
                    if _ret_file.getId() != main_file_id
                    and pattern.fullmatch(os.path.basename(_ret_file.getPath()))
                ),
                key=lambda _ret_file: os.path.basename(_ret_file.getPath()),
            )
            file_count = 2
            for _ret_file in selected:
                files.append(
                    {
                        "type": "video",
                        "track": 1,
                        "order": file_count,
                        "path": _ret_file.getPath(),
                        "file_id": _ret_file.getId(),
                    }
                )
                file_count += 1

        # Get audio file:
        audio_file_name = clip.metadatas["clipname"] + ".wav"
        audio_file_path = os.path.join(clip.path, audio_file_name)
        _ret_audio = clip.get_storage_helper().getFilesInStorage(
            1000,
            0,
            path=urllib.parse.quote(audio_file_path),
            sort="filename",
        )
        if len(_ret_audio["files"]):
            audio_file = _ret_audio["files"][0]
            files.append(
                {
                    "type": "audio",
                    "track": 1,
                    "order": 1,
                    "path": audio_file.getPath(),
                    "file_id": audio_file.getId(),
                    # The absolute path, carried so that
                    # `buildShapeDocument` can READ the `.wav`'s own
                    # header when it has to declare an audio component.
                    # A join, not a call: `clip.absolute_path` resolves
                    # off the storage object this clip already holds
                    # (memoized, and cached for 5 minutes), so the import
                    # route that does NOT need it pays nothing, and a
                    # `.wav` is never opened on a route that would not
                    # look at it.
                    "absolute_path": self._clip_absolute_path(clip, audio_file_name),
                }
            )
        return files

    @staticmethod
    def _clip_absolute_path(clip, file_name):
        """``<storage root>/<clip folder>/<file_name>``, or ``None``.

        ``None`` for every reason a root can be missing — an unresolvable
        browse method leaves ``Clip.root_path`` raising ``AttributeError``
        rather than answering — because a path this provider cannot build
        must not be what breaks collecting a clip's media. The caller
        that actually needs it (the shape document) refuses by name when
        it is absent; the import route never looks.
        """
        try:
            folder = clip.absolute_path
        except Exception:  # noqa: BLE001 - an unresolvable root is "no path"
            log.debug(
                "red: no absolute path for %s",
                getattr(clip, "umid", None),
                exc_info=True,
            )
            return None
        if not folder:
            return None
        return os.path.join(folder, file_name)

    def getImportOptions(self):
        return {}

    @staticmethod
    def buildShapeDocument(main_file, extra_files, metadatas):
        """The whole `original` shape for a dot-separated RED anchor.

        Dicts in, a dict out: no Vidispine, no REDline, no database, no
        clip row — so the document this posts can be read in a unit test
        instead of off a production item.

        ONE read of the filesystem, and only one: when the clip carries a
        separate `.wav`, its header is opened to state the audio
        component (`_audio_component`). Those parameters are not among
        REDline's columns and belong to the `.wav` anyway, which declares
        them itself; the alternatives were to guess a sample rate or to
        drop the sound of 322 prod clips. The read happens HERE and not
        in `getClipAdditionalMediaFiles`, so the ordinary import route —
        which never looks at these numbers — opens nothing, and a
        corrupt `.wav` cannot break a clip that was importing fine. The
        picture side stays free of I/O entirely, and the derivation
        itself is pure (`_audio_component_from_header`).

        THE WHOLE SHAPE, never a patch. `manifest.py::_red_anchor` — what
        pad_forge reconstructs the take from — requires the files named
        by the VIDEO components to be a `_001`...`_N` set: same folder,
        same stem, no hole. Completing the placeholder Vidispine left
        would put the anchor in a `binaryComponent` and start the video
        components at `_002`, which pad_forge refuses with
        `segment(s) _001 missing`. That refusal is why this method
        exists, so the numbering is CHECKED here and a gap is a refusal,
        not a document.

        Three things the reference shape settles, and this follows:

        1. the container names the ANCHOR (`_001`), never a segment;
        2. every video component carries the duration of the WHOLE take,
           not its own segment's share — which is also why
           `Total Frames` must never be summed over the segments;
        3. `itemTrack` does not encode segment order (Vidispine gave
           `_002` V1 and `_001` V2 on the reference), so no track number
           is stated at all. Order comes from the file names.

        Raises:
            TapelessIngestException: when the clip cannot be described
                honestly — a missing or unreadable REDline column, an
                anchor with no Vidispine file id, a segment set with a
                hole, a `.wav` that cannot be read, or an extra this
                document has no component for.
        """
        anchor_path = (main_file or {}).get("path") or "(no path)"
        anchor_id = (main_file or {}).get("file_id")
        if not anchor_id:
            raise TapelessIngestException(
                f"the anchor {anchor_path} has no Vidispine file id, so no "
                f"shape can name it"
            )

        segments, audio_files = Provider._ordered_segments(main_file, extra_files)
        frames = Provider._positive_int(metadatas, "total_frames", anchor_path)
        width = Provider._positive_int(metadatas, "frame_width", anchor_path)
        height = Provider._positive_int(metadatas, "frame_height", anchor_path)
        rate = Provider._frame_rate_scaled(metadatas, anchor_path)

        # One duration object, shared by the container and by EVERY video
        # component: `samples` is the whole take's frame count and the
        # time base is one frame. `1012 / 60 = 16.866666666666667` is
        # exactly the `durationSeconds` Vidispine writes for the twin it
        # can deduce.
        duration = {
            "samples": frames,
            "timeBase": {"numerator": FRAME_RATE_SCALE, "denominator": rate},
        }
        document = {
            "containerComponent": {
                "file": [{"id": anchor_id}],
                "format": R3D_CONTAINER_FORMAT,
                "duration": duration,
            },
            "videoComponent": [
                {
                    "file": [{"id": segment["file_id"]}],
                    "duration": duration,
                    "resolution": {"width": width, "height": height},
                    "codec": R3D_VIDEO_CODEC,
                    "pixelFormat": R3D_PIXEL_FORMAT,
                    "bitDepth": R3D_BIT_DEPTH,
                    "averageFrameRate": {
                        "numerator": rate,
                        "denominator": FRAME_RATE_SCALE,
                    },
                    "pixelAspectRatio": {"horizontal": 1, "vertical": 1},
                    "fieldOrder": R3D_FIELD_ORDER,
                }
                for segment in segments
            ],
            "mimeType": [R3D_MIME_TYPE],
        }
        if audio_files:
            document["audioComponent"] = [Provider._audio_component(audio_files[0])]
        return document

    @staticmethod
    def _ordered_segments(main_file, extra_files):
        """``(segments, audio_files)`` — the picture in `_001`...`_N` order.

        The ORDER is the shape's, and pad_forge reads it off the file
        names, so it is taken from the names here too rather than from
        whatever order the storage query answered in.

        The extras are SPLIT, not filtered: a card `.wav` is a component
        of its own (`_audio_component`), and 322 RED clips on prod carry
        one. Anything that is neither picture nor sound has no component
        in this document, and posting the shape without it would attach
        the picture and quietly drop the rest — so it is refused, exactly
        as a media file with no Vidispine file id is refused on the
        import route. A silent partial ingest is the one outcome this
        whole line of work exists to end.

        A HOLE in the picture numbering is refused too: it is what
        pad_forge refuses downstream, and refusing here names the missing
        segment where refusing there names only the shape.
        """
        audio_files = []
        strays = []
        video_files = []
        for media_file in extra_files or ():
            kind = media_file.get("type")
            if kind == "video":
                video_files.append(media_file)
            elif kind == "audio":
                audio_files.append(media_file)
            else:
                strays.append(media_file)
        if strays:
            raise TapelessIngestException(
                f"{len(strays)} media file(s) of this clip are neither video "
                f"nor audio ({', '.join(str(f.get('path')) for f in strays)}) "
                f"and this shape declares no component for them — posting it "
                f"would attach the picture and silently drop the rest, so "
                f"nothing is posted"
            )
        if len(audio_files) > 1:
            # `getClipAdditionalMediaFiles` collects at most ONE `.wav`
            # (an exact-name query, first hit), so this is unreachable
            # through a scan. It is refused rather than assumed because
            # which track is `A1` and which is `A2` has never been
            # measured, and picking one would be a guess.
            raise TapelessIngestException(
                f"this clip carries {len(audio_files)} audio files "
                f"({', '.join(str(f.get('path')) for f in audio_files)}) and "
                f"the track order of several has never been measured, so "
                f"nothing is posted"
            )

        segments = [main_file] + [
            media_file for media_file in video_files if media_file.get("path")
        ]
        indexed = []
        for media_file in segments:
            if not media_file.get("file_id"):
                raise TapelessIngestException(
                    f"the segment {media_file.get('path')} has no Vidispine "
                    f"file id, so no shape can name it"
                )
            parsed = segment_stem(os.path.basename(media_file.get("path") or ""))
            if parsed is None:
                raise TapelessIngestException(
                    f"the segment {media_file.get('path')} is not named "
                    f"<stem>_<three digits>.R3D, so its place in the take "
                    f"cannot be read from its name"
                )
            indexed.append((parsed[1], media_file))

        indexed.sort(key=lambda entry: entry[0])
        expected = [f"{index + 1:03d}" for index in range(len(indexed))]
        found = [index for index, _media_file in indexed]
        if found != expected:
            raise TapelessIngestException(
                f"this clip's segments are numbered {', '.join(found)} where a "
                f"complete take is {', '.join(expected)} — a shape with a hole "
                f"in its video components is refused by the reconstruction "
                f"(segment _001 first, then no gap), so nothing is posted"
            )
        return [media_file for _index, media_file in indexed], audio_files

    @staticmethod
    def _audio_component(audio_file):
        """The component for the card's separate `.wav`.

        The `.wav` declares its own parameters, so they are READ, never
        estimated: `wave` is in the standard library, the header is 44
        bytes, and this is the same kind of disk access REDline already
        makes on every `.R3D` at scan time.

        A `.wav` that cannot be read FAILS the clip by name — a format
        `wave` refuses (RF64, floating-point WAV), a path that is not
        there, a header claiming no channels or no rate. The alternative
        is a shape whose sound is silently missing, which is the outcome
        this document exists to stop producing.
        """
        path = audio_file.get("absolute_path")
        file_id = audio_file.get("file_id")
        named = audio_file.get("path") or path or "(no path)"
        if not file_id:
            raise TapelessIngestException(
                f"the audio file {named} has no Vidispine file id, so no "
                f"shape can name it"
            )
        if not path:
            raise TapelessIngestException(
                f"the audio file {named} has no resolvable path on disk, so "
                f"its format cannot be read and the shape would be posted "
                f"without its sound"
            )
        try:
            with wave.open(path, "rb") as handle:
                channels = handle.getnchannels()
                sample_width = handle.getsampwidth()
                frame_rate = handle.getframerate()
                frames = handle.getnframes()
        except Exception as error:  # noqa: BLE001 - any unreadable wav
            raise TapelessIngestException(
                f"the audio file {named} could not be read ({error}) — a "
                f"floating-point or RF64 WAV is not a shape this provider "
                f"can describe, so nothing is posted"
            )
        return Provider._audio_component_from_header(
            file_id, channels, sample_width, frame_rate, frames
        )

    @staticmethod
    def _audio_component_from_header(
        file_id, channels, sample_width, frame_rate, frames
    ):
        """The measured header, as Vidispine's `AudioComponentType`.

        PURE, and split out from the read on purpose: every field below
        is arithmetic over four numbers, so it can be pinned against the
        reference shape without a file on disk.

        Checked against `A002_A021_0526HT` (prod, 2026-09-22): 2
        channels, 3-byte samples at 48000 give `blockAlign` 6, `bitrate`
        2304000 and `pcm_s24le`, which is exactly what Vidispine wrote.

        `sampleFormat` is looked up, never computed: see
        `WAV_SAMPLE_FORMATS`. An unmeasured sample width omits the field
        rather than extrapolating.
        """
        if channels <= 0 or sample_width <= 0 or frame_rate <= 0:
            raise TapelessIngestException(
                f"the audio file {file_id} declares {channels} channel(s) at "
                f"{frame_rate} Hz in {sample_width}-byte samples, which is "
                f"not a format that can be described, so nothing is posted"
            )
        time_base = {"numerator": 1, "denominator": frame_rate}
        component = {
            "file": [{"id": file_id}],
            "codec": f"pcm_s{8 * sample_width}le",
            "channelCount": channels,
            "channelLayout": WAV_CHANNEL_LAYOUT,
            "frameSize": WAV_FRAME_SIZE,
            "blockAlign": channels * sample_width,
            "bitrate": frame_rate * channels * sample_width * 8,
            "timeBase": time_base,
            "duration": {"samples": frames, "timeBase": dict(time_base)},
            "itemTrack": WAV_ITEM_TRACK,
            "essenceStreamId": WAV_ESSENCE_STREAM_ID,
        }
        sample_format = WAV_SAMPLE_FORMATS.get(sample_width)
        if sample_format is not None:
            component["sampleFormat"] = sample_format
        return component

    @staticmethod
    def _positive_int(metadatas, key, anchor_path):
        """One REDline column, as the positive integer the shape needs.

        REDline's values are CAPTURED as strings (see
        `REDLINE_TECHNICAL_COLUMNS`); converting them is this document's
        business, not the capture's. A value that is absent, empty or not
        a positive integer refuses the clip: a `0` width or a blank
        duration would post a shape as mute as the one this replaces.
        """
        raw = (metadatas or {}).get(key)
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            raise TapelessIngestException(
                f"REDline gave no usable {key} for {anchor_path} ({raw!r}), so "
                f"the shape would state nothing where Vidispine states a "
                f"number — nothing is posted"
            )
        return value

    @staticmethod
    def _frame_rate_scaled(metadatas, anchor_path):
        """`FPS` as an integer over `FRAME_RATE_SCALE`.

        `Record FPS` is captured beside it and deliberately NOT used: the
        duration Vidispine writes on a deducible twin is
        `Total Frames / FPS` (measured), and mixing the two rates in one
        document would make the duration and the frame rate describe
        different clips.
        """
        raw = (metadatas or {}).get("fps")
        try:
            scaled = (Decimal(str(raw).strip()) * FRAME_RATE_SCALE).to_integral_value()
        except (InvalidOperation, ArithmeticError, TypeError, ValueError):
            scaled = 0
        if scaled <= 0:
            raise TapelessIngestException(
                f"REDline gave no usable fps for {anchor_path} ({raw!r}), so "
                f"neither the duration nor the frame rate of this shape could "
                f"be stated — nothing is posted"
            )
        return int(scaled)

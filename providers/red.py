# coding: utf-8

import logging

import subprocess as sp
import urllib
import shutil
import sys
import os
import csv
import re
from datetime import datetime
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

# The columns getAllClipMetadatas reads out of --printMeta 3.
REDLINE_REQUIRED_COLUMNS = (
    "Clip Name",
    "UUID",
    "Abs TC",
    "Date",
    "Timestamp",
    "Camera Model",
    "Camera PIN",
)

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
                }
            )
        return files

    def getImportOptions(self):
        return {}

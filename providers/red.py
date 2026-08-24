# coding: utf-8

import logging

import subprocess as sp
import urllib
import shutil
import sys
import os
import csv
from datetime import datetime
from io import StringIO
from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models.clip import (
    Clip,
    ClipFile,
    ClipMetadata,
)
from portal.plugins.TapelessIngest.models.settings import Settings

from portal.plugins.TapelessIngest.providers.providers import (
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
        # Both forms on purpose: `_001.r3d` is the narrow ES wildcard,
        # `.r3d` keeps the declaration a superset of the runtime guard
        # below (`== ".R3D"`), which is what decides. Drop `.r3d` and an
        # uppercase non-`_001` file flips to `file` — a different umid.
        return ["_001.r3d", ".r3d"]

    def getFilters(self, escaped_path):
        return [
            {
                "bool": {
                    "must": [
                        {
                            "regexp": {
                                "parent": os.path.join(
                                    escaped_path,
                                    "[A-Z][0-9]{3}_[0-9A-Z]{6}.RDM/[A-Z][0-9]{3}_[A-Z][0-9]{3}_[0-9A-Z]{6}.RDC",
                                )
                            }
                        },
                        {"wildcard": {"name": "*_001.R3D"}},
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
        if file_extension == ".R3D":
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
        }
        return None

    def getClipAdditionalMediaFiles(self, clip):
        files = []
        main_file_id = clip.file.getId()
        # Get video files:
        """
        file_part_name = (
            clip.metadatas["clipname"]
            + "_"
            + "{0:0=3d}".format(file_count)
            + ".R3D"
        )
        """
        file_part_name = f"{clip.metadatas['clipname']}_*.R3D"
        file_part_path = os.path.join(clip.path, file_part_name)
        _ret = clip.get_storage_helper().getFilesInStorage(
            1000,
            0,
            path=urllib.parse.quote(file_part_path, safe="*/"),
            sort="filename",
        )
        _ret_files = _ret["files"]
        file_count = 2
        for _ret_file in _ret_files:
            if _ret_file.getId() != main_file_id:
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

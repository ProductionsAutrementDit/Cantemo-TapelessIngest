# coding: utf-8

import logging

import subprocess as sp
import urllib
import pipes
import sys
import os
import csv
from datetime import datetime
from io import StringIO
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
        return ["_001.r3d"]

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
        cmd = [
            "REDline --i " + pipes.quote(media_absolute_path) + " --printMeta 3 --useMeta",
        ]
        p = sp.run(cmd, shell=True, capture_output=True, text=True)
        csvfile = p.stdout
        reader = csv.DictReader(StringIO(csvfile), delimiter=",")
        for row in reader:
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

    def getMetadatasFromFile(self, media_file, metadatas, context):
        filename, file_extension = os.path.splitext(media_file.getFileName())
        if file_extension == ".R3D":
            metadatas["provider"] = self.machine_name
            metadatas["clipname"] = filename
            metadatas["file_id"] = media_file.getId()
            metadatas["extension"] = file_extension
            media_absolute_path = self.get_file_absolute_path(media_file)
            metadatas = self.getAllClipMetadatas(media_absolute_path, metadatas)
        return metadatas, context

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

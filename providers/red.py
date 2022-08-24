# coding: utf-8

import logging

import subprocess as sp
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
            "REDline --i "
            + pipes.quote(media_absolute_path)
            + " --printMeta 3 --useMeta",
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
        filename, file_extension = os.path.splitext(media_file["name"])
        if file_extension == ".R3D":
            metadatas["provider"] = self.machine_name
            metadatas["clipname"] = media_file["name"]
            metadatas["file_id"] = media_file["vidispine_id"]
            metadatas["extension"] = file_extension
            media_absolute_path = os.path.join(
                context["folder"].root_path,
                media_file["parent"],
                media_file["name"],
            )
            metadatas = self.getAllClipMetadatas(media_absolute_path, metadatas)
        return metadatas, context

    def getClipMediaFiles(self, clip):
        files = []
        file_count = 1
        while True:
            file_part_name = (
                clip.metadatas["clipname"]
                + "_"
                + "{0:0=3d}".format(file_count)
                + ".R3D"
            )
            file_part_path = os.path.join(clip.absolute_path, file_part_name)
            if os.path.isfile(file_part_path):
                file = clip.get_storage_helper().getFileByPath(
                    clip.storage_id, os.path.join(clip.path, file_part_name)
                )
                files.append(
                    {
                        "type": "video",
                        "track": 1,
                        "order": file_count,
                        "path": file.getPath(),
                        "file_id": file.getId(),
                    }
                )
            else:
                break
            file_count += 1
        # As Vidispine doesn't support decoding RED, we try to import the proxy file

        return files

    def getImportOptions(self):
        return {"no-transcode": True}

    def getAdditionalShapesToImport(self, clip):
        tag_name = "red-proxy"
        prores_file_name = clip.metadatas["clipname"] + "_001.mov"
        prores_absolute_file_path = os.path.join(
            clip.absolute_path, prores_file_name
        )
        prores_file_path = os.path.join(clip.path, prores_file_name)
        prores_file_id = clip.get_storage_helper().getFileByPath(
            clip.storage_id, prores_file_path
        )
        if os.path.isfile(prores_absolute_file_path):
            return [{"tag": tag_name, "fileId": prores_file_id}]

        return None

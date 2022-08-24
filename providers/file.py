# coding: utf-8

import logging
import os
from datetime import datetime
import json
import subprocess as sp
import pipes

from portal.plugins.TapelessIngest.providers.providers import (
    Provider as BaseProvider,
)

log = logging.getLogger(__name__)


# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):
    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "FILE"
        self.machine_name = "file"
        self.base_path = ""
        self.clips_path = ""
        self.index_xml = None
        self.card_xml_file = None
        self.file_types = {
            ".mov": "video",
            ".avi": "video",
            ".mp4": "video",
            ".mts": "video",
            ".m2t": "video",
            ".mxf": "video",
            ".mpg": "video",
            ".wav": "audio",
            ".aiff": "audio",
            ".mp3": "audio",
            # "jpg": "image",
            # "png": "image",
            # "bmp": "image",
        }

    def getExtensions(self):
        return self.file_types.keys()

    def getSubPaths(self):
        return []

    def getAllClipMetadatas(self, metadatas, media_absolute_path):
        timestamp = os.path.getmtime(media_absolute_path)
        shooting_date = datetime.fromtimestamp(timestamp)
        metadatas["shooting_date"] = shooting_date.isoformat()

        cmd = [
            "ffprobe -loglevel quiet -show_format -show_streams -print_format json "
            + pipes.quote(media_absolute_path)
        ]
        p = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.PIPE, shell=True)
        output = json.loads(p.stdout.read())

        if "format" in output.keys():
            if "duration" in output["format"].keys():
                metadatas["duration"] = output["format"]["duration"]
            if "tags" in output["format"].keys():
                if "material_package_umid" in output["format"]["tags"].keys():
                    umid_hex = output["format"]["tags"]["material_package_umid"]
                    umid = umid_hex.lstrip("0x")
                    metadatas["umid"] = umid
                if "company_name" in output["format"]["tags"].keys():
                    metadatas["device_manufacturer"] = output["format"]["tags"][
                        "company_name"
                    ]
                if "timecode" in output["format"]["tags"].keys():
                    metadatas["timecode"] = output["format"]["tags"]["timecode"]
                if "modification_date" in output["format"]["tags"].keys():
                    date_patterns = [
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%dT%H:%M:%S.%f%z",
                    ]
                    for pattern in date_patterns:
                        try:
                            shooting_date = datetime.strptime(
                                output["format"]["tags"]["modification_date"],
                                pattern,
                            )
                            metadatas[
                                "shooting_date"
                            ] = shooting_date.isoformat()
                        except:
                            pass
                if "make" in output["format"]["tags"].keys():
                    metadatas["device_manufacturer"] = output["format"]["tags"][
                        "make"
                    ]
                if "encoder" in output["format"]["tags"].keys():
                    metadatas["device_model"] = output["format"]["tags"][
                        "encoder"
                    ]

        if "streams" in output.keys():
            for stream in output["streams"]:
                if "time_base" in stream.keys():
                    metadatas["framerate"] = stream["time_base"]
                if "tags" in stream.keys():
                    if (
                        "creation_time" in stream["tags"].keys()
                        and "shooting_date" not in metadatas.keys()
                    ):
                        try:
                            shooting_date = datetime.strptime(
                                stream["tags"]["creation_time"],
                                "%Y-%m-%d %H:%M:%S",
                            )
                            metadatas[
                                "shooting_date"
                            ] = shooting_date.isoformat()
                        except ValueError:
                            metadatas["shooting_date"] = None
                    if (
                        "timecode" in stream["tags"].keys()
                        and "timecode" not in metadatas.keys()
                    ):
                        metadatas["timecode"] = stream["tags"]["timecode"]

        return metadatas

    def getMetadatasFromFile(self, media_file, metadatas, context):
        if self.isMediaProvider(media_file, context):
            metadatas["provider"] = self.machine_name
            filename, file_extension = os.path.splitext(media_file["name"])
            file_type = self.file_types[file_extension.lower()]
            metadatas["clipname"] = media_file["name"]
            metadatas["file_id"] = media_file["vidispine_id"]
            metadatas["extension"] = file_extension
            metadatas["type"] = file_type
            media_absolute_path = os.path.join(
                context["folder"].root_path,
                media_file["parent"],
                media_file["name"],
            )
            if "hash" in media_file:
                metadatas["umid"] = media_file["hash"]
            metadatas = self.getAllClipMetadatas(metadatas, media_absolute_path)
        return metadatas, context

    def getClipMediaFiles(self, clip):
        metadatas = clip.metadatas
        files = [
            {
                "type": metadatas["type"],
                "track": 1,
                "order": 1,
                "path": clip.path,
                "file_id": clip.file_id,
            }
        ]
        return files

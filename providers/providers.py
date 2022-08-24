# coding: utf-8


import logging
import os
import re

from portal.vidispine.iitem import ItemHelper, IngestHelper
from portal.vidispine.istorage import StorageHelper
from portal.vidispine.iexception import NotFoundError, VSAPIError

from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models.settings import (
    Settings,
    MetadataMapping,
)
from portal.plugins.TapelessIngest.utilities import build_nested

log = logging.getLogger(__name__)

SERVER_CONNECTION = {
    "ps_protocol": "http",
    "ps_address": "portal.studiopad.fr",
    "ps_port": "8080",
    "ps_http_user": "admin",
    "ps_http_pwd": "13netpad$",
}


class Provider:
    def __init__(self, folder=None):
        self.name = "Provider Name"
        self.machine_name = "ProviderName"
        self.folder = folder
        self.MetadataMappingModel = None
        self.clips = {}
        self.clip_count = 0
        self.file_extensions = ()
        self.folders_to_ignore = []

    def getExtensions(self):
        return []

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def isMediaProvider(self, media_file, context):
        filename, file_extension = os.path.splitext(media_file["name"])
        if file_extension.lower() not in self.getExtensions():
            return False
        if len(self.getSubPaths()) == 0:
            return True
        for subpath in self.getSubPaths():
            if subpath is None:
                subpath = ""
            if re.match(
                os.path.join(context["folder"].path, subpath),
                media_file["parent"],
            ):
                return True
        return False

    def getClipStatus(self, clip):
        status = 0
        # Get the clip status
        if clip.item_id not in [None, ""]:
            _ith = ItemHelper()
            try:
                item = _ith.getItem(clip.item_id)
                if not item.isPlaceholder():
                    status = 4
                else:
                    status = 3
            except Exception as x:
                clip.item_id = ""

        if clip.item_id in [None, ""]:
            if clip.file_id not in [None, ""]:
                status = 2
            else:
                if clip.output_file not in [None, ""] and os.path.isfile(
                    clip.output_file
                ):
                    status = 1
                else:
                    status = 0

        return status

    def setSpannedClips(self, clips):
        pass

    def _createDictFromMetadataMapping(self, clip):
        metadata_dict = {}
        clip_metadatas = clip.metadatas

        for clip_metadata_key, clip_metadata_value in clip_metadatas.items():
            # Get metadata mappings
            metadatamappings = MetadataMapping.objects.filter(
                metadata_provider=clip_metadata_key
            )
            for metadatamapping in metadatamappings:
                metadata_dict[
                    metadatamapping.metadata_portal
                ] = clip_metadata_value

        return build_nested(metadata_dict)

    def mapMetadatas(self, clip_metadatas, values={}):
        for clip_metadata in clip_metadatas:
            # Get metadata mappings
            metadatamapping = MetadataMapping.objects.filter(
                metadata_provider=clip_metadata.name
            )
            if len(metadatamapping) > 0:
                log.debug(
                    "Field %s will have value: %s"
                    % (metadatamapping[0].metadata_portal, clip_metadata.value)
                )
                values[metadatamapping[0].metadata_portal] = clip_metadata.value
        return values

    def importClipToPlaceholder(self, clip):

        _ith = ItemHelper()
        _igh = IngestHelper()
        item = _ith.getItem(clip.item_id)

        file_uri = None

        if item.isPlaceholder():

            if clip.file_id is None:
                log.info(
                    "Attempting importation of %s (placehloder=%s)"
                    % (clip.output_file, clip.item_id)
                )
                file_uri = "file://" + clip.output_file
            else:
                log.info(
                    "Attempting importation of %s (placehloder=%s)"
                    % (clip.file_id, clip.item_id)
                )
            try:
                _res = _igh.importFileToPlaceholder(
                    clip.item_id,
                    uri=file_uri,
                    file_id=clip.file_id,
                    tags="lowres",
                )
                log.info("Import to placeholder job started")
                clip.status = 4
            except NotFoundError as e:
                log.info("Import to Placeholder failed: %s" % ("Not found"))
            except VSAPIError as e:
                log.info("Import to Placeholder failed: %s" % (e.reason))

        else:

            log.info("Import to placeholder: file already attached")
            clip.status = 4

        return clip

    def getSpannedClips(self, clip):
        return False

    def getAvailableMetadatas(self):
        return (
            ("clipname", "Clip name"),
            ("duration", "Clip duration"),
            ("timecode", "Timecode"),
            ("framerate", "Frame rate"),
            ("shooting_date", "Shooting date"),
            ("device_manufacturer", "Device manufacturer"),
            ("device_model", "Device Model"),
            ("device_serial", "Device Serial"),
            ("video_codec", "Codec Video"),
            ("aspect_ratio", "Aspect Ratio"),
            ("creation_date", "Creation date"),
            ("last_update_date", "Last update date"),
            ("user_clip_name", "User clip name"),
        )

    def getFileIdFromFullPath(self, path):
        _sh = StorageHelper()
        uri, storage_id = _sh.getStorageFromFullFileName(os.path.normpath(path))
        if not uri:
            log.warning("File uri for %s cannot be found" % path)
            return None
        try:
            file = _sh.getFileByPath(storage_id, path=uri)
            return file.getId()
        except NotFoundError:
            log.warning("File %s cannot be found" % uri)
            return None

    # PArse an XML file
    def parseXML(self, path):
        return XMLParser(path)

    def getImportOptions(self):
        return {}

    def getAdditionalShapesToImport(self, clip):
        return None

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
from portal.plugins.TapelessIngest.scan.context import browse_root_path
from portal.plugins.TapelessIngest.utilities import build_nested

log = logging.getLogger(__name__)


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
            except NotFoundError:
                log.warning(
                    "Item %s not found, resetting item_id for clip %s",
                    clip.item_id,
                    clip.umid,
                )
                clip.item_id = ""
            except VSAPIError as e:
                log.error("Vidispine API error checking item %s: %s", clip.item_id, e)
                clip.item_id = ""
            except Exception as e:
                log.error(
                    "Unexpected error checking clip status for %s: %s",
                    clip.umid,
                    e,
                    exc_info=True,
                )
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

    def isMasterClip(self, clip):
        return True

    def isSpannedClip(self, clip):
        return False

    def isSpannedClipComplete(self, clip):
        return True

    def get_file_absolute_path(self, file, context=None):
        # Context-first (story 2.1): the run's ScanContext resolves the root
        # with zero storage HTTP calls; the per-call resolution below is the
        # legacy fallback for context-less callers (e.g. providers/file.py
        # until 2.3's AD-7 contract threads context provider-internally).
        if context is not None:
            scan_context = context.get("scan_context")
            if scan_context is not None:
                absolute_path = scan_context.absolute_path_for(
                    file.getStorage(), file.getPath()
                )
                if absolute_path is not None:
                    return absolute_path
        sth = StorageHelper()
        storage_id = file.getStorage()
        storage = sth.getStorage(storage_id)
        root_path = browse_root_path(storage)
        if root_path is not None:
            return os.path.join(root_path, file.getPath())
        return None

    def _createDictFromMetadataMapping(self, clip):
        metadata_dict = {}
        clip_metadatas = clip.metadatas

        for clip_metadata_key, clip_metadata_value in clip_metadatas.items():
            # Get metadata mappings
            metadatamappings = MetadataMapping.objects.filter(
                metadata_provider=clip_metadata_key
            )
            for metadatamapping in metadatamappings:
                metadata_dict[metadatamapping.metadata_portal] = clip_metadata_value

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

    def getClipMainMediaFile(self, clip):
        return None

    def getClipMediaFiles(self, clip):
        media_file = self.getClipMainMediaFile(clip)
        if media_file:
            return [media_file]
        return None

    def getClipAdditionalMediaFiles(self, clip):
        return []

    def getAdditionalShapesToImport(self, clip):
        return None

    def getAvailableItem(self, clip):
        return None

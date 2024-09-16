# coding: utf-8

import logging

import os
from portal.vidispine.istorage import StorageHelper
from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.providers.providers import (
    Provider as BaseProvider,
)
from portal.vidispine.iexception import NotFoundError

log = logging.getLogger(__name__)


# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):
    def __init__(self, **kwargs):
        BaseProvider.__init__(self, **kwargs)
        self.name = "Panasonic P2"
        self.machine_name = "panasonicP2"
        self.clips_path = "CONTENTS/CLIP/"
        self.clips_file_extension = ".XML"
        self.video_path = "CONTENTS/VIDEO/"
        self.video_file_extension = ".MXF"
        self.audio_path = "CONTENTS/AUDIO/"

    def getExtensions(self):
        return [".mxf"]

    def getSubPaths(self):
        return [
            "CONTENTS/VIDEO",
            "CONTENTS/AVCLIP",
        ]

    # Extract metadatas from XML file
    def getAllClipMetadatas(self, metadatas, xml_clip):
        metadatas["umid"] = xml_clip.getValueFromPath("ClipContent/GlobalClipID")
        metadatas["clipname"] = xml_clip.getValueFromPath("ClipContent/ClipName")
        if not metadatas["clipname"]:
            metadatas["clipname"] = xml_clip.getValueFromPath(
                "ClipContent/ClipMetadata/UserClipName"
            )
        metadatas["timecode"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/StartTimecode"
        )
        metadatas["duration"] = xml_clip.getValueFromPath("ClipContent/Duration")
        metadatas["EditUnit"] = xml_clip.getValueFromPath("ClipContent/EditUnit")
        metadatas["Relation_OffsetInShot"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/OffsetInShot"
        )
        metadatas["Relation_GlobalShotID"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/GlobalShotID"
        )
        metadatas["Relation_Top_Clipname"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Top/ClipName"
        )
        metadatas["Relation_Top_GlobalClipID"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Top/GlobalClipID"
        )
        metadatas["Relation_Top_P2SerialNo"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Top/P2SerialNo."
        )
        metadatas["Relation_Next_ClipName"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Next/ClipName"
        )
        metadatas["Relation_Next_GlobalClipID"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Next/GlobalClipID"
        )
        metadatas["Relation_Next_P2SerialNo"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Previous/P2SerialNo."
        )
        metadatas["Relation_Previous_ClipName"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Previous/ClipName"
        )
        metadatas["Relation_Previous_GlobalClipID"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Previous/GlobalClipID"
        )
        metadatas["Relation_Previous_P2SerialNo"] = xml_clip.getValueFromPath(
            "ClipContent/Relation/Connection/Previous/P2SerialNo."
        )
        metadatas["VideoFormat"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/VideoFormat"
        )
        metadatas["video_codec"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/Codec"
        )
        metadatas["video_codec_class"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/Codec/@Class"
        )
        metadatas["framerate"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/FrameRate"
        )
        metadatas["timecode_start"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/StartTimecode"
        )
        metadatas["StartBinaryGroup"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/StartBinaryGroup"
        )
        metadatas["aspect_ratio"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/AspectRatio"
        )
        metadatas["StartByteOffset"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/VideoIndex/StartByteOffset"
        )
        metadatas["data_size"] = xml_clip.getValueFromPath(
            "ClipContent/EssenceList/Video/VideoIndex/DataSize"
        )
        metadatas["user_clip_name"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/UserClipName"
        )
        metadatas["data_source"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/DataSource"
        )
        metadatas["creation_date"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/Access/CreationDate"
        )
        metadatas["last_update_date"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/Access/LastUpdateDate"
        )
        metadatas["device_manufacturer"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/Device/Manufacturer"
        )
        metadatas["device_serial"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/Device/SerialNo."
        )
        metadatas["device_model"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/Device/ModelName"
        )
        metadatas["shooting_date"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/Shoot/StartDate"
        )
        metadatas["shooting_date_end"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/Shoot/EndDate"
        )
        metadatas["ProxyFormat"] = xml_clip.getValueFromPath(
            "ClipContent/ClipMetadata/Proxy/ProxyFormat"
        )

        audio_files = []
        audios_count = 0
        audio_essences = xml_clip.getValueFromPath("ClipContent/EssenceList/Audio")
        if audio_essences:
            for Audio in xml_clip.getValueFromPath("ClipContent/EssenceList/Audio"):
                format = xml_clip.getValueFromPath("AudioFormat", root=Audio)
                audio_file_name = (
                    metadatas["clipname"] + str(audios_count).zfill(2) + "." + format
                )
                audio_files.append(audio_file_name)
                audios_count += 1
            metadatas["audio_files"] = ";".join(audio_files)

        metadatas["provider"] = self.machine_name
        return metadatas

    def isMasterClip(self, clip):
        if clip.metadatas["Relation_Top_GlobalClipID"] == clip.metadatas["umid"]:
            return True
        return False

    def isSpannedClip(self, clip):
        if clip.metadatas["Relation_Top_GlobalClipID"] not in ["", None]:
            return True
        return False

    def isLastSpannedClip(self, clip):
        if clip.metadatas["Relation_Next_GlobalClipID"] in ["", None]:
            return True
        return False

    def isSpannedClipComplete(self, clip):
        spanned_clips = clip.get_spanned_clips()
        has_next = True
        # Load Master clip as first current clip
        current_clip = any(self.isMasterClip(clip) for clip in spanned_clips)
        # If master clip is not found, return False
        if not current_clip:
            return False
        while has_next:
            next_clip = None
            if current_clip.metadatas["Relation_Next_GlobalClipID"] in ["", None]:
                # It's last clip
                has_next = False
            else:
                # Search next clip
                for spanned_clip in spanned_clips:
                    if (
                        current_clip.metadatas["Relation_Next_GlobalClipID"]
                        == spanned_clip.metadatas["umid"]
                    ):
                        next_clip = spanned_clip
                        break
                if next_clip:
                    current_clip = next_clip
                else:
                    return False
        return True

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
            "order": 1,
            "file_id": file_id,
            "path": path,
        }

    # Get the media files from the clip
    def getClipAdditionalMediaFiles(self, clip, create=False):
        # TODO: get files from spanned clips
        # for spanned_clip in clip.spanned_clips.all():
        #    pass
        _sh = StorageHelper()
        files = []
        if "audio_files" in clip.metadatas:
            audios_count = 0
            audio_files = clip.metadatas["audio_files"].split(";")
            for audio_file in audio_files:
                audio_file_path = os.path.normpath(
                    os.path.join(
                        os.path.dirname(clip.file.getPath()),
                        "../AUDIO",
                        audio_file,
                    )
                )
                file = None
                try:
                    file = _sh.getFileByPath(clip.file.getStorage(), audio_file_path)
                except NotFoundError:
                    log.warning("File %s cannot be found" % audio_file_path)
                    if create:
                        file_infos = _sh.createFileEntity(
                            clip.file.getStorage(),
                            audio_file_path,
                            createOnly=True,
                            state="ARCHIVED",
                            return_format="json",
                        )
                        file = _sh.getFileById(file_infos["id"])
                if file:
                    files.append(
                        {
                            "type": "audio",
                            "track": audios_count,
                            "order": 1,
                            "path": file.getPath(),
                            "file_id": file.getId(),
                        }
                    )
                    audios_count += 1
        return files

    def isMasterClip(self, clip):
        if (
            clip.metadatas["Relation_Top_GlobalClipID"]
            and clip.metadatas["GlobalClipID"] == clip.metadatas["umid"]
        ):
            return True
        return False

    def getMetadatasFromFile(self, media_file, metadatas, context):
        filename, file_extension = os.path.splitext(media_file.getFileName())
        media_absolute_path = self.get_file_absolute_path(media_file)
        media_dirname = os.path.dirname(media_absolute_path)
        clip_xml = None
        # Get Metadata File
        metadata_file_path = os.path.normpath(
            os.path.join(media_dirname, "../CLIP/" + filename + ".XML")
        )
        if os.path.isfile(metadata_file_path):
            metadatas["clipname"] = filename
            metadatas["clip_xml_file"] = metadata_file_path
            clip_xml = XMLParser(metadata_file_path)
            metadatas = self.getAllClipMetadatas(metadatas, clip_xml)
        return metadatas, context

    def getThumbnail(self, clip):
        clip_name = clip.metadatas.filter(name="clipname")[0].value
        thumbnail = os.path.join(
            clip.folder_path, "CONTENTS/ICON/" + clip_name + ".BMP"
        )
        return thumbnail

    def getProxy(self, clip):
        clip_name = clip.metadatas.filter(name="clipname")[0].value
        clip_formats = clip.metadatas.filter(name="ProxyFormat")
        if len(clip_formats) > 0:
            clip_format = clip_formats[0].value
            if clip_format == "MOV":
                mime_type = "video/mp4"
            if clip_format == "MP4":
                mime_type = "video/mp4"
            proxy = os.path.join(
                clip.folder_path,
                "CONTENTS/PROXY/" + clip_name + "." + clip_format,
            )
            return proxy, mime_type
        else:
            return "", ""

    # Get clip from a list of clips by its UMID
    def _search_clip_by_umid(self, umid, clips):
        for clip in clips:
            if clip.umid == umid:
                return clip
        return False

    # Recursively build a list of all spanned clips
    def _get_spanned_clips_recursively(self, clip, clips, order):
        clips = []
        next_clip_ids = clip.metadatas["Relation_Next_GlobalClipID"]
        next_clip = self._search_clip_by_umid(next_clip_ids, clips)
        next_clip.spanned_order = order
        next_clip.spanned_id = clip.spanned_id
        if next_clip:
            clips.append(next_clip)
            order += 1
            clips += self._get_spanned_clips_recursively(next_clip, clips, order)
        return clips

    # Given all clips, nest spanned clips and return only masters
    def setSpannedClips(self, clips):
        master_clips = []
        for clip in clips:
            if clip.spanned and clip.master_clip:
                clip.spanned_id = clip.umid
                # spanned_clips = self._get_spanned_clips_recursively(clip, clips, 0)

        return master_clips

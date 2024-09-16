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
        self.name = "Ikegami"
        self.machine_name = "ikegami"

    def getExtensions(self):
        return [".mxf"]

    def getSubPaths(self):
        return [
            "BIN([0-9]{3})/VIDEO",
        ]

    # Extract metadatas from XML file
    def getAllClipMetadatas(self, metadatas, xml_clip):
        metadatas["No."] = xml_clip.getValueFromPath("Clip/No.")
        metadatas["clipname"] = xml_clip.getValueFromPath("Clip/Title")
        metadatas["No."] = xml_clip.getValueFromPath("Clip/No.")
        metadatas["No."] = xml_clip.getValueFromPath("Clip/No.")
        metadatas["shooting_date"] = xml_clip.getValueFromPath("Clip/StartDate")
        metadatas["shooting_date_end"] = xml_clip.getValueFromPath("Clip/EndDate")
        metadatas["duration"] = xml_clip.getValueFromPath("Clip/ClipDuration")
        metadatas["timecode"] = xml_clip.getValueFromPath("Clip/StartTC")
        metadatas["DropFrame"] = xml_clip.getValueFromPath("Clip/DropFrame")
        metadatas["UBits"] = xml_clip.getValueFromPath("Clip/UBits")
        metadatas["InputSource"] = xml_clip.getValueFromPath("Clip/InputSource")
        metadatas["device_manufacturer"] = xml_clip.getValueFromPath(
            "Clip/Device/Manufacturer"
        )
        metadatas["device_serial"] = xml_clip.getValueFromPath("Clip/Device/SerialNo.")
        metadatas["device_model"] = xml_clip.getValueFromPath("Clip/Device/Model")
        metadatas["VideoCodec"] = xml_clip.getValueFromPath("Clip/Video/VideoCodec")
        metadatas["GOPStructure"] = xml_clip.getValueFromPath("Clip/Video/GOPStructure")
        metadatas["ChromaFormat"] = xml_clip.getValueFromPath("Clip/Video/ChromaFormat")
        metadatas["Bitrate"] = xml_clip.getValueFromPath("Clip/Video/Bitrate")
        metadatas["DisplaySize"] = xml_clip.getValueFromPath("Clip/Video/DisplaySize")
        metadatas["AspectRatio"] = xml_clip.getValueFromPath("Clip/Video/AspectRatio")
        metadatas["FrameRate"] = xml_clip.getValueFromPath("Clip/Video/FrameRate")
        metadatas["VideoFileName"] = xml_clip.getValueFromPath(
            "Clip/Video/VideoFiles/File/FileName"
        )
        metadatas["umid"] = xml_clip.getValueFromPath("Clip/Video/VideoFiles/File/UMID")
        metadatas["AudioCodec"] = xml_clip.getValueFromPath("Clip/Audios/AudioCodec")
        metadatas["Channels"] = xml_clip.getValueFromPath("Clip/Audios/Channels")
        metadatas["BitsPerSample"] = xml_clip.getValueFromPath(
            "Clip/Audios/BitsPerSample"
        )
        metadatas["SamplesPerSec"] = xml_clip.getValueFromPath(
            "Clip/Audios/SamplesPerSec"
        )

        audio_files = []
        audios_count = 0
        audio_essences = xml_clip.getValueFromPath("Clip/Audios/Audio")
        if audio_essences:
            for Audio in xml_clip.getValueFromPath("Clip/Audios/Audio"):
                audio_file_name = xml_clip.getValueFromPath(
                    "AudioFiles/File/FileName", root=Audio
                )
                audio_files.append(audio_file_name)
                audios_count += 1
            metadatas["audio_files"] = ";".join(audio_files)

        metadatas["provider"] = self.machine_name
        return metadatas

    def getClipMainMediaFile(self, clip, rebuild=False):
        if clip.file is None or rebuild:
            file_id = None
            path = os.path.join(clip.path, clip.metadatas["VideoFileName"])
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

    def getMetadatasFromFile(self, media_file, metadatas, context):
        filename, file_extension = os.path.splitext(media_file.getFileName())
        media_absolute_path = self.get_file_absolute_path(media_file)
        media_dirname = os.path.dirname(media_absolute_path)
        clip_xml = None
        # Get Metadata File
        # Transform 0001V001.MXF to 0001
        if filename[-4] == "V":
            clipname = filename[:4]
        else:
            clipname = filename
        metadata_file_path = os.path.normpath(
            os.path.join(media_dirname, "../CLIPINF/CLIP" + clipname + ".XML")
        )
        if os.path.isfile(metadata_file_path):
            metadatas["clipname"] = clipname
            metadatas["clip_xml_file"] = metadata_file_path
            clip_xml = XMLParser(metadata_file_path)
            metadatas = self.getAllClipMetadatas(metadatas, clip_xml)
        return metadatas, context

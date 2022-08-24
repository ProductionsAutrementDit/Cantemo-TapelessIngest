# coding: utf-8

import logging
import os.path

from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.providers.providers import (
    Provider as BaseProvider,
)

log = logging.getLogger(__name__)


# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):
    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "XDCAM"
        self.machine_name = "xdcam"
        self.absolute_path = ""
        self.subfolder_path = ""
        self.index_xml = None
        self.card_xml_file = None
        self.clips_file_extension = "XML"
        self.folder = None
        self.clip_count = 0

    def getExtensions(self):
        return [".mxf", ".mp4"]

    def getSubPaths(self):
        return ["((PRIVATE/)?(M4ROOT/|XDROOT/))?(Clip|CLIP)"]

    def getMediaProMetadatas(self, metadatas, mediapro_xml):
        metadatas["extension"] = mediapro_xml.getValueFromPath(
            "Contents/Material[@umid='%s']/@type" % metadatas["umid"]
        )
        metadatas["proxy"] = mediapro_xml.getValueFromPath(
            "Contents/Material[@umid='%s']/Proxy/@uri" % metadatas["umid"]
        )
        metadatas["video_codec"] = mediapro_xml.getValueFromPath(
            "Contents/Material[@umid='%s']/@videoType" % metadatas["umid"]
        )
        metadatas["thumbnail"] = mediapro_xml.getValueFromPath(
            "Contents/Material[@umid='%s']/RelevantInfo[@type='JPG']/@uri"
            % metadatas["umid"]
        )
        metadatas["clip_xml_file"] = mediapro_xml.getValueFromPath(
            "Contents/Material[@umid='%s']/RelevantInfo[@type='XML']/@uri"
            % metadatas["umid"]
        )
        media_file = mediapro_xml.getValueFromPath(
            "Contents/Material[@umid='%s']/@uri" % metadatas["umid"]
        )
        clipname = os.path.basename(media_file)
        metadatas["media_file"] = media_file
        metadatas["clipname"] = clipname
        return metadatas

    def getAllClipMetadatas(self, metadatas, xml_clip):
        metadatas["umid"] = xml_clip.getValueFromPath("TargetMaterial/@umidRef")
        metadatas["timecode"] = xml_clip.getValueFromPath(
            "LtcChangeTable/LtcChange[@frameCount='0']/@value"
        )
        metadatas["duration"] = xml_clip.getValueFromPath("Duration/@value")
        metadatas["framerate"] = xml_clip.getValueFromPath(
            "LtcChangeTable/@tcFps"
        )
        metadatas["shooting_date"] = xml_clip.getValueFromPath(
            "CreationDate/@value"
        )
        metadatas["device_manufacturer"] = xml_clip.getValueFromPath(
            "Device/@manufacturer"
        )
        metadatas["device_model"] = xml_clip.getValueFromPath(
            "Device/@modelName"
        )
        metadatas["device_serial"] = xml_clip.getValueFromPath(
            "Device/@serialNo"
        )
        metadatas["latitude_ref"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='LatitudeRef']/@value"
        )
        metadatas["GPSlatitude"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='Latitude']/@value"
        )
        metadatas["GPSlongitude_ref"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='LongitudeRef']/@value"
        )
        metadatas["GPSlongitude"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='Longitude']/@value"
        )
        metadatas["GPSaltitude_ref"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='AltitudeRef']/@value"
        )
        metadatas["GPSaltitude"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='Altitude']/@value"
        )
        metadatas["GPStimestamp"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='TimeStamp']/@value"
        )
        metadatas["GPSstatus"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='Status']/@value"
        )
        metadatas["GPSmeasure_mode"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='MeasureMode']/@value"
        )
        metadatas["GPSdop"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='DOP']/@value"
        )
        metadatas["GPSmapdatum"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='MapDatum']/@value"
        )
        metadatas["GPSdatestamp"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='DateStamp']/@value"
        )
        metadatas["GPSdifferential"] = xml_clip.getValueFromPath(
            "AcquisitionRecord/Group[@name='ExifGPS']/Item[@name='Differential']/@value"
        )

        metadatas["provider"] = self.machine_name

        return metadatas

    # Create a clip object from an XML file
    def getMetadatasFromFile(self, media_file, metadatas, context):
        filename, file_extension = os.path.splitext(media_file["name"])
        media_absolute_path = os.path.join(
            context["folder"].root_path, media_file["parent"]
        )
        clip_xml = None
        # Get Metadata File
        if os.path.isfile(
            os.path.join(media_absolute_path, filename + "M01.XML")
        ):
            metadatas["clip_xml_file"] = f"./Clip/{filename}M01.XML"
            clip_xml = XMLParser(
                os.path.join(media_absolute_path, filename + "M01.XML")
            )
            metadatas = self.getAllClipMetadatas(metadatas, clip_xml)
            # Bonus: get MEDIAPRO_XML
            # there can be multiple media pro if multiple xdcam file structure are in the same folder
            # So the mediapro_xml context is an dict, keyed by mediapro paths
            if "mediapro_xml" not in context.keys():
                context["mediapro_xml"] = {}
            mediaproxml_path = os.path.join(
                media_absolute_path, "../MEDIAPRO.XML"
            )
            if mediaproxml_path not in context["mediapro_xml"].keys():
                if os.path.isfile(mediaproxml_path):
                    context["mediapro_xml"][mediaproxml_path] = XMLParser(
                        mediaproxml_path
                    )
            if mediaproxml_path in context["mediapro_xml"].keys():
                metadatas = self.getMediaProMetadatas(
                    metadatas, context["mediapro_xml"][mediaproxml_path]
                )

        return metadatas, context

        # TODO: add PD-EDL handling
        """"
        if mediapro_xml is not None:
            clip.card_xml = mediapro_xml
            # Search for SMI entries referencing current clip to check if it's master
            for smi_material in mediapro_xml.getValueFromPath(
                'Contents/Material[@type="PD-EDL"]', True
            ):
                spanned_id = smi_material.get("umid")
                smi_material_components = mediapro_xml.getValueFromPath(
                    "Component", True, root=smi_material
                )
                previous_component = None
                next_component = None
                for smi_material_component in smi_material_components:
                    if smi_material.get("umid") == umid and previous_component is None:
                        clip.master_clip = True
        """

    def getClipMediaFiles(self, clip):
        files = []
        """
        if ("extension" in clip.metadatas) and (
            clip.metadatas["extension"] == "PD-EDL"
        ):
            order = 1
            if len(extra_infos["components"]) == 1:
                # If there is only one component, no need to make spaned clips:
                component = extra_infos["components"][0]
                component_type = self.card_xml.getValueFromPath("@type", root=component)
                component_uri = self.card_xml.getValueFromPath("@uri", root=component)
                if component_type == "MP4":
                    media_file = os.path.join(self.clips_path, component_uri)
                    files.append({"type": "video", "track": 1, "path": media_file})
            else:
                # If it's an EDL clip
                clip.spanned = True
                clip.master_clip = True

                for component in extra_infos["components"]:
                    component_type = self.card_xml.getValueFromPath(
                        "@type", root=component
                    )

                    if component_type != "PD-EDL":
                        component_uri = self.card_xml.getValueFromPath(
                            "@uri", root=component
                        )
                        component_umid = self.card_xml.getValueFromPath(
                            "@umid", root=component
                        )
                        # We get (or create) the spanned clip component
                        spanned_clip, spanned_clip_created = Clip.objects.get_or_create(
                            umid=component_umid,
                            defaults={
                                "provider": self,
                                "folder_path": clip.folder_path,
                            },
                        )
                        spanned_clip.spanned = True
                        spanned_clip.save()
                        clip.spanned_clips.get_or_create(clip=spanned_clip, order=order)
                        clip.metadatas["extension"] = component_type
                        order += 1
        """
        media_file_relative_path = os.path.join(
            clip.path, clip.file.getFileName()
        )
        clip_xml_file_relative_path = os.path.join(
            clip.path, os.path.basename(clip.metadatas["clip_xml_file"])
        )
        files.append(
            {
                "type": "video",
                "track": 1,
                "order": 1,
                "path": os.path.join(clip.root_path, media_file_relative_path),
                "file_id": clip.file.getId(),
            }
        )
        files.append(
            {
                "type": "metadatas",
                "track": 1,
                "order": 1,
                "path": os.path.join(
                    clip.root_path, clip_xml_file_relative_path
                ),
                "file_id": self.getFileIdFromFullPath(
                    os.path.join(clip.root_path, clip_xml_file_relative_path)
                ),
            }
        )

        return files

    # Given all clips, nest spanned clips and return only masters
    def setSpannedClips(self, clips):
        # Search for SMI files
        # Build spanned sets from SMI files
        return clips

    def getThumbnail(self, clip):
        thumbnails = clip.metadatas.filter(name="thumbnail")
        if len(thumbnails) > 0:
            thumbnail = thumbnails[0].value
        else:
            return ""
        return os.path.join(clip.folder_path, thumbnail)

    def getProxy(self, clip):
        return "", ""

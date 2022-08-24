# coding: utf-8

import logging

from lxml import etree
from xml.dom import minidom
import subprocess as sp

import os
import uuid

from django.core.exceptions import ObjectDoesNotExist

from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models.clip import Clip, ClipFile, ClipMetadata, Reel
from portal.plugins.TapelessIngest.models.settings import Settings


from portal.plugins.TapelessIngest.providers.video_file import (
    Provider as VideoFileProvider,
)
from portal.plugins.TapelessIngest.providers.xdcam import Provider as XDCAMProvider

log = logging.getLogger(__name__)

# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(VideoFileProvider):
    def __init__(self):
        VideoFileProvider.__init__(self)
        self.name = "IMAGE"
        self.machine_name = "image_file"
        self.file_extensions = (".png", ".bmp", ".tiff", ".jpg")

    def getAllClips(self, path):
        clips = []

        infos, reel = self.getFilesInfos(path)
        for material in infos.findall("Contents/Material"):
            if material.get("has_sidecar"):
                Clip = self.createClipFromFileWithSidecar(path, material, reel)
            else:
                Clip = self.createClipFromFile(path, material, reel)
            if Clip is not False:
                clips.append(Clip)
            else:
                log.error("Unable to parse %s" % material)

        return clips

    def getClipFilePaths(self, clipname, path):
        return [os.path.join(path, clipname)]

    def getFilesInfos(self, path):

        self.card_xml_file = os.path.join(path, "cantemo_ingest_log.xml")

        # Check if there already is a reel entry for the given path
        try:
            reel = Reel.objects.filter(folder_path=path)[0]
        except IndexError:
            reel_umid = uuid.uuid4()
            reel = Reel(pk=reel_umid)
            reel.folder_path = path
            reel.save()

        if os.path.isfile(self.card_xml_file):
            root = etree.parse(self.card_xml_file)

        else:
            # Get all files in folder
            root = etree.Element("CantemoIngestLog")

            videos = etree.SubElement(root, "Contents")

            files = [
                f
                for f in os.listdir(path)
                if (
                    f.lower().endswith(self.file_extensions) and (not f.startswith("."))
                )
            ]

            for file in files:

                file_path = os.path.join(path, file)
                log.debug("Retrieving info for %s" % file_path)
                # Try to load a XML sidecar file (Celian...)
                file_xml_name = os.path.splitext(file)[0] + "M01.XML"
                file_xml = os.path.join(path, file_xml_name)
                if os.path.isfile(file_xml):
                    # An XDCAM xml has been found
                    xmldoc = minidom.parse(file_xml)
                    clip = xmldoc.getElementsByTagName("NonRealTimeMeta")[0]
                    TargetMaterial = xmldoc.getElementsByTagName("TargetMaterial")[0]
                    umid = TargetMaterial.attributes["umidRef"].value

                    clip = etree.fromstring("<Material />")

                    clip.set("filename", file)
                    clip.set("has_sidecar", "True")
                    clip.set("sidecar_file", file_xml_name)
                    clip.set("umid", umid)
                else:
                    umid = uuid.uuid4()
                    process = sp.Popen(
                        [
                            "/usr/bin/ffprobe",
                            "-v",
                            "quiet",
                            "-print_format",
                            "xml",
                            "-show_format",
                            "-show_streams",
                            file_path,
                        ],
                        stdout=sp.PIPE,
                        stderr=sp.STDOUT,
                    )
                    stdout, stderr = process.communicate()

                    clip = etree.fromstring(stdout)
                    format = clip.find("format")
                    format.set("filename", file)

                    if format is None:
                        continue
                    clip.set("umid", str(umid))

                    clip.tag = "Material"

                videos.append(clip)

            xml_output = etree.ElementTree(root)
            xml_output.write(
                self.card_xml_file, xml_declaration=True, encoding="utf-16"
            )

        reel.media_xml = etree.tostring(root)
        reel.save()
        return root, reel

    def createClipFromFileWithSidecar(self, path, material, reel):

        umid = material.get("umid")
        material_file = os.path.join(path, material.get("filename"))
        metadatas_file = os.path.join(path, material.get("sidecar_file"))
        xdcamprovider = XDCAMProvider()
        clip = xdcamprovider.createClipFromFile(
            path, umid, metadatas_file, material_file, extra_infos=[]
        )
        clip.provider = self

        return clip

    def createClipFromFile(self, path, material, reel):

        umid = material.get("umid")
        format = material.find("format")
        streams = material.find("streams")

        if len(streams.findall("stream")) == 0:
            return False

        clip, created = Clip.objects.get_or_create(
            umid=umid,
            defaults={"provider": self, "folder_path": path, "spanned": False},
        )
        if clip.reel_id is None:
            clip.reel = reel
            clip.save()

        log.debug("Create metadatas for %s" % umid)

        duration = 0

        video_streams = streams.findall("stream[@codec_type='video']")
        audio_streams = streams.findall("stream[@codec_type='audio']")
        data_streams = streams.findall("stream[@codec_type='data']")

        video_timecode = False
        data_timecode = False
        manufacturer = False

        if len(video_streams) > 0:

            file_type = "video"
            video_stream = video_streams[0]

            try:
                video_timecode = video_stream.findall("tag[@key='timecode']")[0].get(
                    "value"
                )
            except Exception as e:
                video_timecode = False
            try:
                time_base = video_stream.get("time_base")
            except Exception as e:
                time_base = "1/25"
            try:
                duration = video_stream.get("duration")
            except Exception as e:
                duration = 0
            try:
                video_codec = video_stream.findall("tag[@key='encoder']")[0].get(
                    "value"
                )
            except Exception as e:
                video_codec = "unknown"
            try:
                audio_codec = audio_streams[0].get("codec_name")[0].get("value")
            except Exception as e:
                audio_codec = "unknown"

        else:
            file_type = "audio"

            try:
                time_base = audio_streams[0].get("time_base")
            except Exception as e:
                time_base = "1/48000"
            try:
                duration = audio_streams[0].get("duration")
            except Exception as e:
                duration = 0
            try:
                video_codec = audio_streams[0].get("codec_name")[0].get("value")
            except Exception as e:
                video_codec = "unknown"

        if len(audio_streams) > 0:
            try:
                audio_codec = audio_streams[0].get("codec_name")[0].get("value")
            except Exception as e:
                audio_codec = "unknown"
        else:
            audio_codec = "unknown"

        if len(data_streams) > 0:
            data_stream = data_streams[0]
            try:
                data_timecode = data_stream.findall("tag[@key='timecode']")[0].get(
                    "value"
                )
            except Exception as e:
                data_timecode = False
            try:
                manufacturer = data_stream.findall("tag[@key='make']")[0].get("value")
            except Exception as e:
                manufacturer = False

        timecode = video_timecode or data_timecode or "00:00:00:00"

        from fractions import Fraction

        x = Fraction(time_base)
        frame_duration = float(x)
        frames = int(float(duration) / frame_duration) / 100

        framerate = int(1 / frame_duration)

        try:
            from dateutil import parser

            from_date = material.findall("tag[@key='creation_time']")[0].get("value")
            dt = parser.parse(from_date)
            shooting_date = dt.isoformat()
        except Exception as e:
            import datetime

            t = os.path.getmtime(os.path.join(path, format.get("filename")))
            shooting_date = datetime.datetime.fromtimestamp(t).isoformat()

        clip.metadatas["clipname"] = format.get("filename")
        clip.metadatas["timecode"] = timecode
        if manufacturer:
            clip.metadatas["device_manufacturer"] = manufacturer
        clip.metadatas["time_base"] = time_base
        clip.metadatas["duration"] = frames
        clip.metadatas["shooting_date"] = shooting_date
        clip.metadatas["video_codec"] = video_codec
        clip.metadatas["audio_codec"] = audio_codec

        clip.clip_xml = etree.tostring(material)

        clip.input_files.all().delete()
        InputFile = clip.input_files.create(
            filetype=file_type, path=os.path.join(path, format.get("filename"))
        )

        return clip

    def getThumbnail(self, clip):
        return ""

    def getProxy(self, clip):
        return "", ""

    def wrapClip(self, clip, storage_root_path, order=0):

        input_file = clip.input_files.all()[0]
        input_filename, input_file_extension = os.path.splitext(input_file.path)

        outputfile = os.path.join(storage_root_path, clip.umid + input_file_extension)
        command = ["rsync", "--checksum", input_file.path, outputfile]

        log.info("export clip %s with command %s" % (clip.umid, " ".join(command)))

        pipe = sp.Popen(command, stdout=sp.PIPE, bufsize=10 ** 8)

        output, err = pipe.communicate(
            b"input data that is passed to subprocess' stdin"
        )

        if os.path.isfile(outputfile):
            log.info("Successfully copied %s" % outputfile)
            clip.output_file = outputfile
            clip.save()
        else:
            log.error("Output file for %s not created" % clip.umid)

        return clip

    def doExportClip(self, clip, storage_root_path):

        clip = self.wrapClip(clip, storage_root_path)

        return clip

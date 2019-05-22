# coding: utf-8

import logging

from lxml import etree
from lxml import objectify

import subprocess as sp

import os

from django.core.exceptions import ObjectDoesNotExist

from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider
from portal.plugins.TapelessIngest.models import Clip, ClipFile, SpannedClips, Settings

log = logging.getLogger(__name__)


# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "Panasonic P2"
        self.machine_name = "panasonicP2"
        self.clips_path = "/CONTENTS/CLIP/"
        self.clips_file_extension = ".XML"
        self.video_path = "CONTENTS/VIDEO/"
        self.video_file_extension = ".MXF"
        self.audio_path = "/CONTENTS/AUDIO/"

    def checkPath(self, path):
        if os.path.isdir(path + self.clips_path) :
            return True
        else:
            return False


    def getClipFilePaths(self, clipname, folder_path):
        return [os.path.join(folder_path, self.video_path, clipname + self.video_file_extension)]

    def createClipFromFile(self, folder, clip_file):

        path = folder.path

        xml_clip = XMLParser(clip_file)

        if xml_clip.root is not None:


            GlobalClipID = xml_clip.getValueFromPath("ClipContent/GlobalClipID")

            clip, created = Clip.objects.get_or_create(umid = GlobalClipID, defaults={
              'provider': self,
              'folder_path': path,
            })


            self.update_or_create_metadata(clip, 'clipname', xml_clip.getValueFromPath("ClipContent/ClipName"))
            self.update_or_create_metadata(clip, 'timecode', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/StartTimecode"))
            self.update_or_create_metadata(clip, 'duration', xml_clip.getValueFromPath("ClipContent/Duration"))
            self.update_or_create_metadata(clip, 'EditUnit', xml_clip.getValueFromPath("ClipContent/EditUnit"))
            self.update_or_create_metadata(clip, 'Relation_OffsetInShot', xml_clip.getValueFromPath("ClipContent/Relation/OffsetInShot"))
            self.update_or_create_metadata(clip, 'Relation_GlobalShotID', xml_clip.getValueFromPath("ClipContent/Relation/GlobalShotID"))
            self.update_or_create_metadata(clip, 'Relation_Top_Clipname', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Top/ClipName"))
            self.update_or_create_metadata(clip, 'Relation_Top_GlobalClipID', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Top/GlobalClipID"))
            self.update_or_create_metadata(clip, 'Relation_Top_P2SerialNo', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Top/P2SerialNo."))
            self.update_or_create_metadata(clip, 'Relation_Next_ClipName', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Next/ClipName"))
            self.update_or_create_metadata(clip, 'Relation_Next_GlobalClipID', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Next/GlobalClipID"))
            self.update_or_create_metadata(clip, 'Relation_Next_P2SerialNo', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Previous/P2SerialNo."))
            self.update_or_create_metadata(clip, 'Relation_Previous_ClipName', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Previous/ClipName"))
            self.update_or_create_metadata(clip, 'Relation_Previous_GlobalClipID', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Previous/GlobalClipID"))
            self.update_or_create_metadata(clip, 'Relation_Previous_P2SerialNo', xml_clip.getValueFromPath("ClipContent/Relation/Connection/Previous/P2SerialNo."))
            self.update_or_create_metadata(clip, 'VideoFormat', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/VideoFormat"))
            self.update_or_create_metadata(clip, 'video_codec', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/Codec"))
            self.update_or_create_metadata(clip, 'video_codec_class', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/Codec/@Class"))
            self.update_or_create_metadata(clip, 'framerate', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/FrameRate"))
            self.update_or_create_metadata(clip, 'timecode_start', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/StartTimecode"))
            self.update_or_create_metadata(clip, 'StartBinaryGroup', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/StartBinaryGroup"))
            self.update_or_create_metadata(clip, 'aspect_ratio', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/AspectRatio"))
            self.update_or_create_metadata(clip, 'StartByteOffset', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/VideoIndex/StartByteOffset"))
            self.update_or_create_metadata(clip, 'data_size', xml_clip.getValueFromPath("ClipContent/EssenceList/Video/VideoIndex/DataSize"))
            self.update_or_create_metadata(clip, 'user_clip_name', xml_clip.getValueFromPath("ClipContent/ClipMetadata/UserClipName"))
            self.update_or_create_metadata(clip, 'data_source', xml_clip.getValueFromPath("ClipContent/ClipMetadata/DataSource"))
            self.update_or_create_metadata(clip, 'creation_date', xml_clip.getValueFromPath("ClipContent/ClipMetadata/Access/CreationDate"))
            self.update_or_create_metadata(clip, 'last_update_date', xml_clip.getValueFromPath("ClipContent/ClipMetadata/Access/LastUpdateDate"))
            self.update_or_create_metadata(clip, 'device_manufacturer', xml_clip.getValueFromPath("ClipContent/ClipMetadata/Device/Manufacturer"))
            self.update_or_create_metadata(clip, 'device_serial', xml_clip.getValueFromPath("ClipContent/ClipMetadata/Device/SerialNo."))
            self.update_or_create_metadata(clip, 'device_model', xml_clip.getValueFromPath("ClipContent/ClipMetadata/Device/ModelName"))
            self.update_or_create_metadata(clip, 'shooting_date', xml_clip.getValueFromPath("ClipContent/ClipMetadata/Shoot/StartDate"))
            self.update_or_create_metadata(clip, 'shooting_date_end', xml_clip.getValueFromPath("ClipContent/ClipMetadata/Shoot/EndDate"))
            self.update_or_create_metadata(clip, 'ProxyFormat', xml_clip.getValueFromPath("ClipContent/ClipMetadata/Proxy/ProxyFormat"))


            clip.input_files.all().delete()
            InputFile = clip.input_files.create(filetype = 'video', path = os.path.join(path, self.video_path, xml_clip.getValueFromPath("ClipContent/ClipName") + self.video_file_extension))

            audios_count = 0

            for Audio in xml_clip.getValueFromPath("ClipContent/EssenceList/Audio"):
                format = xml_clip.getValueFromPath("AudioFormat", root=Audio)
                audio_file_path = path + self.audio_path + xml_clip.getValueFromPath("ClipContent/ClipName") + str(audios_count).zfill(2) + "." + format
                if os.path.isfile(audio_file_path):
                    InputFile = clip.input_files.create(filetype = 'audio', path = audio_file_path)
                audios_count += 1

            # check for spanned clips
            if len(clip.metadatas.filter(name='Relation_Top_GlobalClipID')) > 0:
                Relation_Top_GlobalClipID = clip.metadatas.filter(name='Relation_Top_GlobalClipID')[0]
                if Relation_Top_GlobalClipID.value != 'False':
                    clip.spanned = True
                    #if it's the first clip
                    if Relation_Top_GlobalClipID.value == GlobalClipID:
                        clip.master_clip = True
                    else:
                        clip.master_clip = False
                    clip.save()

            clip.clip_xml = xml_clip.tostring()

            return clip

        else:
            log.error(etree.tostring(root))
            return False

    def getThumbnail(self, clip):
        clip_name = clip.metadatas.filter(name="clipname")[0].value
        thumbnail = os.path.join(clip.folder_path, "CONTENTS/ICON/" + clip_name + ".BMP")
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
            proxy = os.path.join(clip.folder_path, "CONTENTS/PROXY/" + clip_name + "." + clip_format)
            return proxy, mime_type
        else:
            return "", ""


    def setSpannedClips(self, clips):
        for clip in clips:

            if clip.spanned and clip.master_clip:
                order = 1
                current_clip = clip
                while True:
                    spanned_clip_set = SpannedClips.objects.get_or_create(master_clip = clip, clip = current_clip, order = order)
                    next_clip_ids = current_clip.metadatas.filter(name='Relation_Next_GlobalClipID')
                    if len(next_clip_ids) > 0:
                        try:
                            next_clip = Clip.objects.get(pk=next_clip_ids[0])
                        except ObjectDoesNotExist:
                            break
                        current_clip = next_clip
                    else:
                        break
                    order += 1

    def wrapClip(self, clip, output_path, order = 0):

        settings = Settings.objects.get(pk=1)
        bmxtranswrap_bin = settings.bmxtranswrap

        log.info("processing spanned clip n.%s: %s" % (order, clip.umid))
        if order is 0:
            outputfile = os.path.join(output_path, clip.umid + ".MXF")
        else:
            outputfile = os.path.join(output_path, clip.umid + "_SPANNED.MXF")

        command = [ bmxtranswrap_bin,
                '-p','-t','op1a',
                '-o', outputfile,'--group']

        input_files = clip.input_files.all()

        for input_file in input_files:
            command.append(input_file.path)

        log.info("Wrapping command will be: %s" % ' '.join(command))

        process = sp.Popen(command, stdout = sp.PIPE, stderr = sp.STDOUT, bufsize=10**8)
        stdoutdata, stderrdata = process.communicate()
        log.debug("Output is: %s, error is: %s" % (stdoutdata, stderrdata))

        if os.path.isfile(outputfile):
            log.info("Successfully wrapped %s" % outputfile)
            clip.output_file = outputfile
            clip.save()
        return clip

    def stitchClip(self, clip, output_path):
        if clip.spanned and clip.master_clip:

            settings = Settings.objects.get(pk=1)
            bmxtranswrap_bin = settings.bmxtranswrap

            log.info("processing spanned clips")
            outputfile = os.path.join(output_path, clip.umid + ".MXF")
            command = [ bmxtranswrap_bin,
                    '-p','-t','op1a',
                    '-o', outputfile]

            clip_sets = clip.spanned_clips.order_by("order")
            for clip_set in clip_sets:
                spanned_clip = clip_set.clip
                command.append(spanned_clip.output_file)

            log.info("Encoding command will be: %s" % ' '.join(command))
            process = sp.Popen(command, stdout = sp.PIPE, stderr = sp.STDOUT, bufsize=10**8)
            stdoutdata, stderrdata = process.communicate()
            log.info("Output is: %s, error is: %s" % (stdoutdata, stderrdata))

            if os.path.isfile(outputfile):
                for clip_set in clip_sets:
                    spanned_clip = clip_set.clip
                    os.remove(spanned_clip.output_file)
                log.info("Successfully encoded %s" % outputfile)
                clip.output_file = outputfile
                clip.save()

        return clip


    def doExportClip(self, clip, storage_root_path):

        if clip.spanned and clip.master_clip:

            settings = Settings.objects.get(pk=1)
            tmp_storage = settings.tmp_storage

            log.info("Clip is spanned and master")

            clips_to_process = []
            clip_sets = clip.spanned_clips.order_by("order")
            log.info("Found %s spanned clips" % len(clip_sets))
            for clip_set in clip_sets:
                self.wrapClip(clip_set.clip, tmp_storage, clip_set.order)
        else:
            log.info("Clip is not spanned or not master")
            clip = self.wrapClip(clip, storage_root_path)

        clip = self.stitchClip(clip, storage_root_path)

        return clip

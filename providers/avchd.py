# coding: utf-8

import logging

import subprocess as sp

import os.path
import uuid

from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models import Clip, ClipFile, ClipMetadata, Settings

from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider

log = logging.getLogger(__name__)

# Classe jvcprohd: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "AVCHD"
        self.machine_name = "avchd"
        self.base_path = ""
        self.clips_path = ""
        self.index_xml = None
        self.card_xml_file = None
        self.file_extensions = ('.mts', '.m2ts', '.m2t')

    def checkPath(self, path):
        if os.path.isdir(path + '/PRIVATE'):
            self.base_path = '/PRIVATE'
        paths = [
          "/AVCHD/",
        ]
        for possible_path in paths:
            if os.path.isdir(path + self.base_path + possible_path + 'BDMV/STREAM'):
                files = [ f for f in os.listdir(path) if f.lower().endswith(self.file_extensions) ]
                if len(files) > 0:
                    self.clips_path = path + self.base_path + possible_path
                    return True
        return False



    def getClipFilePaths(self, clipname, folder_path):
        possible_paths = []
        folders = [
            "AVCHD",
            "PRIVATE/AVCHD",
        ]
        for folder in folders:
            possible_paths.append(os.path.join(folder_path, folder, 'BDMV/STREAM', clipname))
        return possible_paths


    def getAllClips(self, folder):
        clips = []

        infos, reel = self.getFilesInfos(folder)
        for material in infos.findall('Contents/Material'):
            Clip = self.createClipFromFile(folder, material, reel)
            if Clip is not False:
                clips.append(Clip)
            else:
                log.error("Unable to parse %s" % clip_file)

        return clips

    def getFilesInfos(self, folder):

        path = folder.path

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

            files = [ f for f in os.listdir(path) if f.lower().endswith(self.file_extensions) ]

            for file in files:

                file_path = os.path.join(path, file)

                umid = uuid.uuid4()

                process = sp.Popen(['/usr/bin/ffprobe', '-v', 'quiet', '-print_format', 'xml', '-show_format', '-show_streams', file_path], stdout=sp.PIPE, stderr=sp.STDOUT)
                stdout, stderr = process.communicate()

                clip = etree.fromstring(stdout)
                format = clip.find('format')

                if format is not None:

                    clip.tag = "Material"
                    clip.set('umid',str(umid))

                    format.set('filename', file)

                    videos.append(clip)

            xml_output = etree.ElementTree(root)
            xml_output.write(self.card_xml_file, xml_declaration=True, encoding='utf-16')

        reel.media_xml = etree.tostring(root)
        reel.save()
        return root, reel


    def createClipFromFile(self, folder, umid, metadatas_file, material_file, extra_infos):

        path = folder.path

        xml_clip = XMLParser(metadatas_file)

        if xml_clip.root is not None:
            clip, created = Clip.objects.get_or_create(umid = umid, defaults={
              'provider': self,
              'folder_path': path
            })
            if clip.reel_id is None:
                clip.reel = reel
                clip.save()

            log.debug("Create metadatas for %s" % umid)

            timecode = extra_infos['timecode']
            timecode_array = [timecode[i:i+2] for i in range(0, len(timecode), 2)]


            self.update_or_create_metadata(clip, 'clipname', os.path.basename(material_file))
            self.update_or_create_metadata(clip, 'timecode', ':'.join(timecode_array))
            self.update_or_create_metadata(clip, 'duration', extra_infos['duration'])
            self.update_or_create_metadata(clip, 'shooting_date', extra_infos['shooting_date'])
            self.update_or_create_metadata(clip, 'device_manufacturer', xml_clip.getValueFromPath("MetaData/Device/@manufacturer"))
            self.update_or_create_metadata(clip, 'device_model', xml_clip.getValueFromPath("MetaData/Device/@modelName"))
            self.update_or_create_metadata(clip, 'device_serial', xml_clip.getValueFromPath("MetaData/Device/@serialNo"))
            self.update_or_create_metadata(clip, 'wrapping', extra_infos['wrapping'])
            self.update_or_create_metadata(clip, 'dataFormat', xml_clip.getValueFromPath("MetaData/Video/@dataFormat"))
            self.update_or_create_metadata(clip, 'Video_codecType', xml_clip.getValueFromPath("MetaData/Video/@codecType"))
            self.update_or_create_metadata(clip, 'Audio_codecType', xml_clip.getValueFromPath("MetaData/Audio/@codecType"))
            self.update_or_create_metadata(clip, 'Audio_channels', xml_clip.getValueFromPath("MetaData/Audio/@ch"))
            self.update_or_create_metadata(clip, 'Rec_mode_type', xml_clip.getValueFromPath("MetaData/RecMode/@type"))
            self.update_or_create_metadata(clip, 'Rec_mode_config', xml_clip.getValueFromPath("MetaData/RecMode/@config"))
            self.update_or_create_metadata(clip, 'Rec_mode_preRec', xml_clip.getValueFromPath("MetaData/RecMode/@preRec"))
            self.update_or_create_metadata(clip, 'Rec_mode_clipContinuous', xml_clip.getValueFromPath("MetaData/RecMode/@clipContinuous"))

            clip.input_files.all().delete()
            InputFile = clip.input_files.create(filetype = 'video', path = material_file)

            clip.clip_xml = xml_clip.tostring()

        else:
            return False

        return clip

    def getThumbnail(self, clip):
        return ""

    def getProxy(self, clip):
        return "", ""

    def wrapClip(self, clip, storage_root_path, order = 0):

        input_file = clip.input_files.all()[0]
        input_filename, input_file_extension = os.path.splitext(input_file.path)

        outputfile = os.path.join(storage_root_path, clip.umid + input_file_extension)
        command = [ 'rsync', '--checksum', input_file.path, outputfile ]

        log.info("export clip %s with command %s" % (clip.umid, ' '.join(command)))

        pipe = sp.Popen(command, stdout = sp.PIPE, bufsize=10**8)

        output, err = pipe.communicate(b"input data that is passed to subprocess' stdin")

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

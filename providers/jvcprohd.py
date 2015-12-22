# coding: utf-8

import logging

import subprocess as sp

import os.path
from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models import Clip, ClipFile, ClipMetadata, Settings

from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider

log = logging.getLogger(__name__)

# Classe jvcprohd: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "JVC PRO HD"
        self.machine_name = "jvcprohd"
        self.base_path = ""
        self.clips_path = ""
        self.index_xml = None
        self.card_xml_file = None

    def checkPath(self, path):
        if os.path.isdir(path + '/PRIVATE'):
            self.base_path = '/PRIVATE'
        paths = [
          "/JVC/CQAV/",
        ]
        for possible_path in paths:
            if os.path.isfile(path + self.base_path + possible_path + 'MEDIAINF.XML'):
                self.clips_path = path + self.base_path + possible_path
                self.card_xml_file = self.clips_path + "MEDIAINF.XML"
                return True
        return False
    
    def getAllClips(self, folder):
        # get all clip from MEDIAPRO.XML file
        clips = []
        path = folder.path
        self.clips_xml = XMLParser(self.card_xml_file)
        if self.clips_xml.root is not None:
            for material in self.clips_xml.getValueFromPath('Contents/ClipInfo/Individual'):

                metadatas_file = os.path.join(self.clips_path, self.clips_xml.getValueFromPath("@info", root=material))
                material_file = os.path.join(self.clips_path, self.clips_xml.getValueFromPath("@name", root=material))
                

                umid = self.clips_xml.getValueFromPath("@umid", root=material)
                extra_infos = {}
                
                extra_infos['wrapping'] = self.clips_xml.getValueFromPath("@wrapping", root=material)
                extra_infos['shooting_date'] = self.clips_xml.getValueFromPath("@creationDate", root=material)
                extra_infos['timecode'] = self.clips_xml.getValueFromPath("@startTc", root=material)
                extra_infos['duration'] = self.clips_xml.getValueFromPath("@duration", root=material)
                extra_infos['video_codec'] = self.clips_xml.getValueFromPath("@videoCodec", root=material)
                extra_infos['spanning_status'] = self.clips_xml.getValueFromPath("@spanningStatus", root=material)
                
                if os.path.isfile(material_file) and os.path.isfile(metadatas_file):
                    Clip = self.createClipFromFile(folder, umid, metadatas_file, material_file, extra_infos)
                    if Clip is not False:
                        Clip.media_xml = self.clips_xml.tostring()
                        clips.append(Clip)
                    else:
                        log.error("Unable to parse %s" % metadatas_file)
                else:
                    log.error("Listed clip %s has no video (%s, %s)" % (umid, material_file, metadatas_file))
        return clips

    
    def createClipFromFile(self, folder, umid, metadatas_file, material_file, extra_infos):
      
        path = folder.path
      
        xml_clip = XMLParser(metadatas_file)
            
        if xml_clip.root is not None:
            clip, created = Clip.objects.get_or_create(umid = umid, defaults={
              'provider': self,
              'folder_path': path
            })
            
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

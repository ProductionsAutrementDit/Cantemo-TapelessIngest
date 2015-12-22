# coding: utf-8

import logging

import subprocess as sp

import os
import uuid

from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models import Clip, ClipFile, ClipMetadata, Settings

from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider

log = logging.getLogger(__name__)

# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "FILE"
        self.machine_name = "video_file"
        self.base_path = ""
        self.clips_path = ""
        self.index_xml = None
        self.card_xml_file = None
        self.file_extensions = ('.mov', '.avi', '.mp4', '.mts')

    def checkPath(self, path):
        files = [ f for f in os.listdir(path) if f.lower().endswith(self.file_extensions) ]
        if len(files) > 0:
            return True
        else:
            return False
     
    def getAllClips(self, folder):
        clips = []

        infos = self.getFilesInfos(folder)
        for material in infos.findall('Contents/Material'):
            Clip = self.createClipFromFile(folder, material)
            if Clip is not False:
                Clip.media_xml = etree.tostring(infos)
                clips.append(Clip)
            else:
                log.error("Unable to parse %s" % clip_file)

        return clips
    
    def getFilesInfos(self, folder):
        
        from lxml import etree
        
        path = folder.path
        
        self.card_xml_file = os.path.join(path, "cantemo_ingest_log.xml")
        
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
                
                process = sp.Popen(['/usr/local/bin/ffprobe', '-v', 'quiet', '-print_format', 'xml', '-show_format', '-show_streams', file_path], stdout=sp.PIPE, stderr=sp.STDOUT)
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
        
        return root
    
    def createClipFromFile(self, folder, material):
      
        path = folder.path
            
        umid = material.get('umid')
        format = material.find('format')
        streams = material.find('streams')
        video_stream = streams.findall("stream[@codec_type='video']")[0]
        audio_streams = streams.findall("stream[@codec_type='audio']")

        clip, created = Clip.objects.get_or_create(umid = umid, defaults={
          'provider': self,
          'folder_path': path
        })
        
        log.debug("Create metadatas for %s" % umid)
        
        
        duration = 0
            
        try:
            timecode = video_stream.findall("tag[@key='timecode']")[0].get("value")
        except Exception as e:
            timecode = "00:00:00:00"
        try:
            time_base = video_stream.get("time_base")
        except Exception as e:
            time_base = "1/25"
        try:
            duration = video_stream.get("duration")
        except Exception as e:
            duration = 0

        from fractions import Fraction
        x = Fraction(time_base)
        frame_duration = float(x)
        frames = int(float(duration) / frame_duration)

        framerate = int(1 / frame_duration)
        
        try:
            from dateutil import parser
            from_date = video_stream.findall("tag[@key='creation_time']")[0].get("value")
            dt = parser.parse(from_date)
            shooting_date = dt.isoformat()
        except Exception as e:
            import datetime
            shooting_date = datetime.datetime.now().isoformat()
        try:
            video_codec = video_stream.findall("tag[@key='encoder']")[0].get("value")
        except Exception as e:
            video_codec = "unknown"

        self.update_or_create_metadata(clip, 'clipname', format.get('filename'))
        self.update_or_create_metadata(clip, 'timecode', timecode)
        self.update_or_create_metadata(clip, 'time_base', time_base)
        self.update_or_create_metadata(clip, 'duration', frames)
        self.update_or_create_metadata(clip, 'framerate', framerate)
        self.update_or_create_metadata(clip, 'shooting_date', shooting_date)
        self.update_or_create_metadata(clip, 'video_codec', video_codec)
        
        
        clip.clip_xml = etree.tostring(material)

        clip.input_files.all().delete()
        InputFile = clip.input_files.create(filetype = 'video', path = os.path.join(path, format.get('filename')))


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

# coding: utf-8

import logging

import subprocess as sp

import os.path
from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models import Clip, ClipFile, ClipMetadata, Settings

from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider

log = logging.getLogger(__name__)

# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "XDCAM"
        self.machine_name = "xdcam"
        self.base_path = ""
        self.clips_path = ""
        self.index_xml = None
        self.card_xml_file = None

    def checkPath(self, path):
        self.base_path = path
        if os.path.isdir(path + '/PRIVATE'):
            self.base_path = os.path.join(path, 'PRIVATE')

        folders = [
          "XDROOT",
          "BPAV",
          "M4ROOT",
          "",
        ]
        for possible_folder in folders:
            possible_path = os.path.join(self.base_path, possible_folder)
            if os.path.isfile(os.path.join(possible_path, 'MEDIAPRO.XML')):
                self.clips_path = possible_path
                self.card_xml_file = os.path.join(possible_path, 'MEDIAPRO.XML')
                return True
        return False
    
    def getAllClips(self, folder):
        # get all clip from MEDIAPRO.XML file
        clips = []
        path = folder.path
        # Parse MEDIAPOR.XML
        self.clips_xml = XMLParser(self.card_xml_file)
        # If parsing is successful
        if self.clips_xml.root is not None:
            for material in self.clips_xml.getValueFromPath('Contents/Material', True):
                metadatas_file = os.path.join(self.clips_path, self.clips_xml.getValueFromPath("RelevantInfo[@type='XML']/@uri", root=material))
                material_file = os.path.join(self.clips_path, self.clips_xml.getValueFromPath("@uri", root=material))
                umid = self.clips_xml.getValueFromPath("@umid", root=material)
                extra_infos = {}
                extra_infos['extension'] = self.clips_xml.getValueFromPath("@type", root=material)
                extra_infos['proxy'] = self.clips_xml.getValueFromPath("Proxy/@uri", root=material)
                extra_infos['video_codec'] = self.clips_xml.getValueFromPath("@videoType", root=material)
                
                thumbnail = self.clips_xml.getValueFromPath("RelevantInfo[@type='JPG']/@uri", root=material)
                if thumbnail:
                    extra_infos['thumbnail'] =  os.path.join(self.clips_path, thumbnail)
                else:
                    extra_infos['thumbnail'] = False

                if extra_infos['extension'] == 'PD-EDL':
                    # If ther is an EDL file, we have to set spanned clips
                    components = self.clips_xml.getValueFromPath("Component", root=material)
                    extra_infos['components'] = components
                    
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
                
            timecode = xml_clip.getValueFromPath("LtcChangeTable/LtcChange[@frameCount='0']/@value")
            timecode_array = reversed([timecode[i:i+2] for i in range(0, len(timecode), 2)])

            if extra_infos['extension'] == 'PD-EDL':
                # If it's an EDL clip
                clip.spanned = True
                clip.master_clip = True
                order = 1
                for component in extra_infos['components']:
                    component_type = self.clips_xml.getValueFromPath("@type", root=component)
                    component_uri = self.clips_xml.getValueFromPath("@uri", root=component)
                    component_umid = self.clips_xml.getValueFromPath("@umid", root=component)
                    
                    if component_type != 'PD-EDL':
                        # We get (or create) the spanned clip component from database
                        spanned_clip, spanned_clip_created = Clip.objects.get_or_create(umid = component_umid, defaults={
                            'provider': self,
                            'folder_path': path
                        })
                        spanned_clip.spanned = True
                        spanned_clip.save()
                        clip.spanned_clips.get_or_create(clip = spanned_clip, order = order)
                        self.update_or_create_metadata(clip, 'extension', component_type)
                        order += 1
            else:
                self.update_or_create_metadata(clip, 'clipname', os.path.basename(material_file))
                self.update_or_create_metadata(clip, 'timecode', ':'.join(timecode_array))
                self.update_or_create_metadata(clip, 'duration', xml_clip.getValueFromPath("Duration/@value"))
                self.update_or_create_metadata(clip, 'framerate', xml_clip.getValueFromPath("LtcChangeTable/@tcFps"))
                self.update_or_create_metadata(clip, 'shooting_date', xml_clip.getValueFromPath("CreationDate/@value"))
                self.update_or_create_metadata(clip, 'device_manufacturer', xml_clip.getValueFromPath("Device/@manufacturer"))
                self.update_or_create_metadata(clip, 'device_model', xml_clip.getValueFromPath("Device/@modelName"))
                self.update_or_create_metadata(clip, 'device_serial', xml_clip.getValueFromPath("Device/@serialNo"))
                self.update_or_create_metadata(clip, 'video_codec', extra_infos['video_codec'])
                self.update_or_create_metadata(clip, 'proxy', extra_infos['proxy'])
                self.update_or_create_metadata(clip, 'thumbnail', extra_infos['thumbnail'])
    
                clip.input_files.all().delete()
                InputFile = clip.input_files.create(filetype = 'video', path = material_file)
                
                clip.clip_xml = xml_clip.tostring()
            
        else:
            return False

        return clip

    def getThumbnail(self, clip):
        clip_name = clip.metadatas.filter(name="clipname")[0].value
        thumbnails = clip.metadatas.filter(name="thumbnail")
        if len(thumbnails) > 0:
            thumbnail = thumbnails[0].value
        else:
            return ""
        return os.path.join(clip.folder_path, thumbnail)

    def getProxy(self, clip):
        return "", ""
        
    def copyClip(self, clip, storage_root_path):
      
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
    
    def stitchClip(self, clip, storage_root_path):
        
        settings = Settings.objects.get(pk=1)
        bmxtranswrap_bin = settings.bmxtranswrap
        
        input_file_extension = clip.metadatas.filter(name="extension")[0].value
    
        log.info("processing spanned clips")
        outputfile = os.path.join(storage_root_path, clip.umid + "." + input_file_extension)
        
        log.info("Output file will be %s" % outputfile)
        
        command = [ bmxtranswrap_bin,
                '-p','-t','op1a',
                '-o', outputfile]
    
        clip_sets = clip.spanned_clips.order_by("order")
        for clip_set in clip_sets:
            spanned_clip = clip_set.clip
            input_files = spanned_clip.input_files.all()
            for input_file in input_files:
                command.append(input_file.path)

        log.info("Encoding command will be: %s" % ' '.join(command))
        process = sp.Popen(command, stdout = sp.PIPE, stderr = sp.STDOUT, bufsize=10**8)
        stdoutdata, stderrdata = process.communicate()
        log.info("Output is: %s, error is: %s" % (stdoutdata, stderrdata))

        if os.path.isfile(outputfile):
            log.info("Successfully encoded %s" % outputfile)
            clip.output_file = outputfile
            clip.save()
        else:
            log.error("Output file for %s not created" % clip.umid)

        return clip
    
    def doExportClip(self, clip, storage_root_path):
        if clip.spanned and clip.master_clip:
            clip = self.stitchClip(clip, storage_root_path)
        if not clip.spanned:
            clip = self.copyClip(clip, storage_root_path)
        
        return clip

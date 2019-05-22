# coding: utf-8

import logging

import subprocess as sp

import os
from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models import Clip, ClipFile, ClipMetadata, Settings

from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider

log = logging.getLogger(__name__)

# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(BaseProvider):

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "RED"
        self.machine_name = "red"
        self.base_path = ""
        self.clips_path = ""
        self.index_xml = None
        self.card_xml_file = None
        self.clips_file_extension = ".RDC"

    def checkPath(self, path):
        self.base_path = path
        if any(x.endwith('.RDC') for x in os.listdir(path)):
            return True
        return False
 
    def getClipFiles(self, folder):
        clip_files = []
        clip_filenames = [ f for f in listdir(folder.path + self.clips_path) if f.endswith(self.clips_file_extension) ]
        for clip_filename in clip_filenames:
            clip_files.append(folder.path + self.clips_path + clip_filename)
        return clip_files

    def getAllClips(self, folder):
        # Get XML files in folder
        
        clips = []

        clip_files = self.getClipFiles(folder)
        for clip_file in clip_files:
            Clip = self.createClipFromFile(folder, clip_file)
            if Clip is not False:
                clips.append(Clip)
            else:
                log.error("Unable to parse %s" % clip_file)

        return clips

    
    def createClipFromFile(self, folder, clip_folder):
        
        settings = Settings.objects.get(pk=1)
        redline_bin = settings.redline
        
        
        
        for x in os.listdir(clip_folder)):
      
        path = folder.path
            
        if xml_clip.root is not None:
            clip, created = Clip.objects.get_or_create(umid = umid, defaults={
              'provider': self,
              'folder_path': path
            })
            
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
        
        
        
        command = [ redline_bin,
                '--i','-t','op1a',
                '-o', outputfile]

        process = sp.Popen(command, cwd=folder, stdout = sp.PIPE, stderr = sp.STDOUT, bufsize=10**8)
        stdoutdata, stderrdata = process.communicate()

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

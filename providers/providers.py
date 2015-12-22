# coding: utf-8
from __future__ import unicode_literals

import logging
from portal_script import *
from os import listdir, remove
from os.path import isfile, isdir, basename

from django.conf import settings

from portal.vidispine.iitem import ItemHelper, IngestHelper
from portal.vidispine.istorage import StorageHelper
from portal.vidispine.iexception import NotFoundError

from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.models import Clip, ClipFile, MetadataMapping, Settings
from portal.plugins.TapelessIngest.storage_utilities import getStorageUri

log = logging.getLogger(__name__)

SERVER_CONNECTION = {
    "ps_protocol": "http",
    "ps_address": "portal.studiopad.fr",
    "ps_port": "8080",
    "ps_http_user": "admin",
    "ps_http_pwd": "13netpad$"
}

class Provider():
    def __init__(self):
        self.name = "Provider Name"
        self.machine_name = "ProviderName"
        self.MetadataMappingModel = None
        self.portal_server = None
        self.metadatas = []
 
    def getClipFiles(self, folder):
        clip_files = []
        clip_filenames = [ f for f in listdir(folder.path + self.clips_path) if f.endswith(self.clips_file_extension) ]
        for clip_filename in clip_filenames:
            clip_files.append(folder.path + self.clips_path + clip_filename)
        return clip_files
            
    def getClips(self, folder):
        clips = self.getAllClips(folder)
        self.setSpannedClips(clips)
        return clips

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
    
    def getClipStatus(self, clip):
        status = 0
        # Get the clip status
        if clip.item_id not in [None, '']:
            _ith = ItemHelper()
            try:
                item = _ith.getItem(clip.item_id)
                if not item.isPlaceholder():
                    status = 4
                else:
                    status = 3
            except Exception as x:
                clip.item_id = ""
        
        if clip.item_id in [None, '']:
            if clip.file_id not in [None, '']:
                status = 2
            else:
                if clip.output_file not in [None, ''] and isfile(clip.output_file):
                    status = 1
                else:
                    status = 0

        return status
    
    def setSpannedClips(self, clips):
        pass

    def getPortalServer(self):
        if self.portal_server is None:
            self.portal_server = PortalServer.connect(SERVER_CONNECTION)
        return self.portal_server
    
    def createPlaceHolder(self, clip, user):
          
        _ith = ItemHelper(runas=user)
      
        if clip.item_id not in [None, ""]:                   
            try:
                item = _ith.getItem(clip.item_id)
            except NotFoundError:
                clip.item_id = ""
      
        if clip.item_id in [None, ""]:
      
            from VidiRest.helpers.vidispine import createMetadataDocumentFromDict
            metadatagroupname = "Shot"
            
            clip_metadatas = clip.metadatas.all()
            
            media_item_title = clip.metadatas.get(name='clipname').value
            values = {
                "title": media_item_title,
            }
            log.debug("found %s clip_metadatas associated" % len(clip_metadatas))
            fields = self.mapMetadatas(clip_metadatas, values)
            
            _metadata = {'fields': {}}
    
            for field, value in fields.iteritems():
                _metadata['fields'][field] = {'type': 'text', 'value': value}
            
            mfg_fields = None
            
            md = createMetadataDocumentFromDict(_metadata, [metadatagroupname], mfg_fields, settings.TIME_ZONE)
            _res = _ith.createPlaceholder(md)
            
            clip.item_id = _res.getId()
            
            log.debug("Placeholder creation done (id=%s)" % clip.item_id)
            
            clip = self.addToCollection(clip)
        

        clip.user = user
        clip.status = 3
        
        return clip
    
    def mapMetadatas(self, clip_metadatas, values={}):
        for clip_metadata in clip_metadatas:
            # Get metadata mappings
            metadatamapping = MetadataMapping.objects.filter(metadata_provider=clip_metadata.name)
            if len(metadatamapping) > 0:
                log.debug("Field %s will have value: %s" % (metadatamapping[0].metadata_portal, clip_metadata.value))
                values[metadatamapping[0].metadata_portal] = clip_metadata.value
        return values
    
    def addToCollection(self, clip):
        from portal.vidispine.icollection import CollectionHelper
        ch = CollectionHelper()
        
        target_collections = clip.collection_id.split(",")
        if len(target_collections) > 0:

            for target_collection in target_collections:
                ch.addItemToCollection(collection_id=target_collection, item_id=clip.item_id)
                log.info("Item added to collection (id=%s)" % clip.collection_id)
        return clip
            
    
    def importClipToPlaceholder(self, clip):

        _ith = ItemHelper()
        item = _ith.getItem(clip.item_id)
        
        if item.isPlaceholder():
        
            # 1/ Connect to the server
            portal_server = self.getPortalServer()
    
            
            if clip.file_id is None:
                log.info("Attempting importation of %s (placehloder=%s)" % (clip.output_file, clip.item_id))
                result, response = PSImportToPlaceholder.execute(portal_server, clip.item_id, "file://" + clip.output_file, {"tags": "lowres"})
            else:
                log.info("Attempting importation of %s (placehloder=%s)" % (clip.file_id, clip.item_id))
                result, response = PSImportToPlaceholder.execute(portal_server, clip.item_id, "", {"fileId": clip.file_id,"tags": "lowres"})
            if result:
                log.info("Import to placeholder job started (%s)" % response.job_id)
                clip.status = 4
            else:
                log.info("Import to Placeholder failed: %s" % (response.describe_error()))
        
        else:
            
            log.info("Import to placeholder: file already attached")
            clip.status = 4


        return clip
    
    def update_or_create_metadata(self, clip, name, value):
        if value is not False:
            metadata, created = clip.metadatas.get_or_create(name=name, defaults={'value':value})
            if not created:
                metadata.value = value
                metadata.save()
    
    def getSpannedClips(self, clip):
        return False
    
    def getAvailableMetadatas(self):
        return list([
          ("name", "Clip name"),
          ("duration", "Clip duration"),
          ("timecode", "Timecode"),
          ("framerate", "Frame rate"),
          ("shooting_date", "Shooting date"),
          ("device_manufacturer", "Device manufacturer"),
          ("device_model", "Device Model"),
          ("device_serial", "Device Serial"),
          ("video_codec", "Codec Video"),
          ("aspect_ratio", "Aspect Ratio"),
          ("creation_date", "Creation date"),
          ("last_update_date", "Last update date"),
          ("user_clip_name", "User clip name")
        ])
      
    def update_item_metadatas(self, clip):
        portal_server = self.getPortalServer()
        clip_metadatas = clip.metadatas.all()
        values = self.mapMetadatas(clip_metadatas)
        result, response = PSUpdateItemMetadata.execute(portal_server, clip.item_id, values)
        if result:
            return 'Item updated'
        else:
            return result.describe_error()

    def registerOutputFile(self, clip):
        settings = Settings.objects.get(pk=1)
        
        _sh = StorageHelper()
        try:
            vsfile = _sh.getFileByURI(settings.storage_id, path=basename(clip.output_file))
            log.warning('File is already registered: %s' % vsfile.getId())
            clip.file_id = vsfile.getId()
        except NotFoundError:
            file_id = _sh.notifyStorageOfFile(settings.storage_id, basename(clip.output_file))
            log.info("File Id: %s" % file_id)
            clip.file_id = file_id
            clip.status = 2
            clip.save()

        return clip

    def exportClip(self, clip):
      
        if clip.output_file not in [None, ""]:
            if os.path.isfile(clip.output_file):
                clip.status = 1
                return clip
            else:
                clip.output_file = ""
        
        if clip.output_file in [None, ""]:
            settings = Settings.objects.get(pk=1)
            
            log.info("START ENCODING PROCESS FOR %s" % clip.umid)

            storage_root_path = getStorageUri(settings.storage_id)

            if storage_root_path is not False:
                clip = self.doExportClip(clip, storage_root_path)

            else:
                log.info("Can't find storage")

            if clip.output_file and os.path.isfile(clip.output_file):
                clip.status = 1
            else:
                log.info("Output file doesn't exists")
            
            return clip
      
    def doExportClip(self, clip, storage_root_path):
        return clip

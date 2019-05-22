# coding: utf-8
from __future__ import unicode_literals

import logging
import os

from django.conf import settings

from portal.vidispine.iitem import ItemHelper, IngestHelper
from portal.vidispine.istorage import StorageHelper
from portal.vidispine.iexception import NotFoundError, VSAPIError

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
        clip_filenames = [ f for f in os.listdir(folder.path + self.clips_path) if (f.endswith(self.clips_file_extension) and not f.startswith('.')) ]
        for clip_filename in clip_filenames:
            clip_files.append(folder.path + self.clips_path + clip_filename)
        return clip_files

    def getClips(self, folder):
        clips = self.getAllClips(folder)
        self.setSpannedClips(clips)
        return clips

    def getClipFilePaths(self, clipname, folder_path):
        return [os.path.join(folder_path, clipname)]

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
                if clip.output_file not in [None, ''] and os.path.isfile(clip.output_file):
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
            metadatagroupname = "Film"

            clip_metadatas = clip.metadatas.all()
            log.debug("found %s clip_metadatas associated" % len(clip_metadatas))

            media_item_title = clip.metadatas.get(name='clipname').value

            _metadata = {'fields':
               {'title': {u'type': u'text', u'value': media_item_title},
                'portal_mf860578': {u'type': u'text', u'value': 'shot'}},
                'groups':{}}

            values = {}
            fields = self.mapMetadatas(clip_metadatas, values)

            camera_informations = {u'fields':{}}

            for field, value in fields.iteritems():
                camera_informations[u'fields'][field] = {u'type': u'text',
                 u'value': value}

            original_path = ''

            log.info("Clip path is %s and name is %s" % (clip.folder_path, media_item_title ))

            original_paths = self.getClipFilePaths(media_item_title, clip.folder_path)
            if len(original_paths) > 0:
                original_path = original_paths[0]
            original_informations = {u'fields':
                {
                'portal_mf127234': {u'type': u'text', u'value': original_path}, # clip path
                'portal_mf932455': {u'type': u'text', u'value': media_item_title}, # clip name
                'portal_mf808363': {u'type': u'dropdown', u'value': 'yes'}, # is online
                'portal_mf493223': {u'type': u'text', u'value': self.machine_name}, # clip type
                }
            }

            _metadata[u'groups']['CameraInformations'] = [camera_informations]
            _metadata[u'groups']['OriginalInformations'] = [original_informations]

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
        _igh = IngestHelper()
        item = _ith.getItem(clip.item_id)

        file_uri = None

        if item.isPlaceholder():

            if clip.file_id is None:
                log.info("Attempting importation of %s (placehloder=%s)" % (clip.output_file, clip.item_id))
                file_uri = "file://" + clip.output_file
            else:
                log.info("Attempting importation of %s (placehloder=%s)" % (clip.file_id, clip.item_id))
            try:
                _res = _igh.importFileToPlaceholder(clip.item_id, uri=file_uri, file_id=clip.file_id, tags="lowres")
                log.info("Import to placeholder job started")
                clip.status = 4
            except NotFoundError as e:
                log.info("Import to Placeholder failed: %s" % ('Not found'))
            except VSAPIError as e:
                log.info("Import to Placeholder failed: %s" % (e.reason))


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

        from lxml import etree as ET
        import requests

        clip_metadatas = clip.metadatas.all()
        values = self.mapMetadatas(clip_metadatas)

        doc = ET.Element('MetadataDocument')
        root = get_element(doc, '/MetadataDocument')
        root.set('xmlns', 'http://xml.vidispine.com/schema/vidispine')
        ts_pan = ET.SubElement(root, 'timespan')
        ts_attributes = ts_pan.attrib
        ts_attributes['end'] = '+INF'
        ts_attributes['start'] = '-INF'
        group_arc = ET.SubElement(ts_pan, 'group')
        group_arc_name = ET.SubElement(group_arc, 'name')
        group_arc_name.text = 'CameraInformations'

        for field_name, value in values.items():
            et_field = ET.Element('field')
            et_field_name = ET.SubElement(et_field, 'name')
            et_field_name.text = field_name
            et_field_value = ET.SubElement(et_field, 'value')
            et_field_value.text = unicode(value)
            group_arc.append(et_field)

        url = 'http://127.0.0.1:8080/API/item/' + clip.item_id + '/metadata'

        config = ConfigParser.ConfigParser()
        config.read('/etc/cantemo/portal/portal.conf')
        portal_user = config.get('vidispine', 'VIDISPINE_USERNAME', 0)
        portal_pwd = config.get('vidispine', 'VIDISPINE_PASSWORD', 0)

        auth = (portal_user,portal_pwd)

        headers = {'content-type': 'application/xml',
         'Accept': 'application/xml'}

        try:
            r = requests.put(url, data=ET.tostring(doc), auth=auth, headers=headers)
            r.encoding = 'UTF-8'
        except requests.ConnectionError as e:
            result = False
            response = e.message.reason + ' ' + e.message.message
        else:
            response = r.text

        return response

    def registerOutputFile(self, clip):
        settings = Settings.objects.get(pk=1)

        _sh = StorageHelper()
        try:
            vsfile = _sh.getFileByURI(settings.storage_id, path=os.path.basename(clip.output_file))
            log.warning('File is already registered: %s' % vsfile.getId())
            clip.file_id = vsfile.getId()
            # Check if there is already a clip associated with this file:
            vsitems = vsfile.getFullItemReference()
            if len(vsitems) > 0:
                log.warning('File is already imported: %s' % vsitems[0]['item_id'])
                clip.item_id = vsitems[0]['item_id']
        except NotFoundError:
            file_id = _sh.notifyStorageOfFile(settings.storage_id, os.path.basename(clip.output_file))
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

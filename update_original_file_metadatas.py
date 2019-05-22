from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist

from portal.plugins.TapelessIngest.models import Clip
from portal.vidispine.iitem import ItemHelper, IngestHelper
from portal.search.models import SearchHistory
from portal.api import client
from VidiRest.helpers.vidispine import createMetadataDocumentFromDict
import subprocess as sp
import json
import sys

import os


NEW_GROUP = 'Film'
SUBGROUP = 'OriginalInformations'
ORIGINAL_FIELDS = {
  'PATH': 'portal_mf127234',
  'CLIP_NAME': 'portal_mf932455',
  'ARCHIVED': 'portal_mf634507',
  'ONLINE': 'portal_mf808363',
  'TYPE': 'portal_mf493223',
  'P5_HANDLE': 'portal_mf909117'
}

def update_item_orginial_metadatas(clipId):

    infos = get_fileinfo_from_clip(clipId)

    _metadata = {u'groups': {}}
    original_informations = {u'fields':{}}

    if infos['is_archived']:
        archived = 'yes'
    else:
        archived = 'no'

    if infos['is_online']:
        online = 'yes'
    else:
        online = 'no'

    original_informations['fields'] = {
        ORIGINAL_FIELDS['PATH'] : {u'type': u'text', u'value': infos['online_path']},
        ORIGINAL_FIELDS['CLIP_NAME'] : {u'type': u'text', u'value': infos['clip_name']},
        ORIGINAL_FIELDS['ARCHIVED'] : {u'type': u'text', u'value': archived},
        ORIGINAL_FIELDS['ONLINE'] : {u'type': u'text', u'value': online},
        ORIGINAL_FIELDS['TYPE'] : {u'type': u'text', u'value': infos['type']},
        ORIGINAL_FIELDS['P5_HANDLE'] : {u'type': u'text', u'value': infos['p5_handle']}
    }

    _metadata[u'groups'][SUBGROUP] = [original_informations]

    mfg_fields = None

    metadata_document = createMetadataDocumentFromDict(_metadata, [NEW_GROUP], mfg_fields, settings.TIME_ZONE)

    _ith = ItemHelper()
    item = _ith.getItem(clipId)

    _ith.setItemMetadata(clipId, metadata_document, skipForbidden=True, return_format='xml')

    print "Item %s has been updated\r" % clipId

def get_fileinfo_from_clip(clipId):
    nsdchat_bin = '/usr/local/aw/bin/nsdchat'
    replace_options = {
        '/Volumes': '/mnt',
        '/ActiveMedia': '/Infortrend'
    }
    try:
        clip = Clip.objects.get(item_id=clipId)
        clipname = clip.metadatas.get(name='clipname').value
        type = clip.provider.machine_name
        possible_paths = clip.provider.getClipFilePaths(clipname, clip.folder_path)
        all_possible_paths = []
        for path in possible_paths:
            new_path = path
            for key, value in replace_options.items():
                new_path = new_path.replace(key, value)
            all_possible_paths.append(new_path)
        is_online = False
        is_archived = False
        p5_handle = ''
        online_path = ''
        for path in all_possible_paths:
            if os.path.isfile(path):
                online_path = path
                is_online = True
                break
        for path in all_possible_paths:
            get_handle_command = [ nsdchat_bin,'-c','ArchiveEntry','handle','localhost', '{%s}' % path, 'AirbusHelicopters']
            process = sp.Popen(get_handle_command, stdout = sp.PIPE, stderr = sp.STDOUT, bufsize=10**8)
            stdoutdata, stderrdata = process.communicate()
            if stdoutdata.replace("\n", "") != '':
                is_archived = True
                p5_handle = stdoutdata.replace("\n", "")
                if online_path == '':
                    online_path = path
                break
        return {
            'clip_name': clipname,
            'is_online': is_online,
            'is_archived':is_archived,
            'type':type,
            'p5_handle': p5_handle,
            'online_path': online_path
        }
    except ObjectDoesNotExist:
        return {
            'clip_name': None,
            'is_online': False,
            'is_archived': False,
            'type': None,
            'p5_handle': None,
            'online_path': None
        }


def apply_to_saved_search(search_id):

    from portal.vidispine.icollection import CollectionHelper
    from portal.search.elastic import query_elastic, postprocess_search
    from portal.api.v2.search.utilities import build_query_from_criteria
    from portal.vidispine.vsavedsearch import create_elastic_search_from_saved_search, update_search_with_optional_parameters

    ch = CollectionHelper()

    search_criteria_doc = ch.getCollectionMetadataByLabel(search_id, 'criteria_document')
    if search_criteria_doc:
        criteria = json.loads(search_criteria_doc)
        elastic_query = build_query_from_criteria(criteria=criteria, username='admin')
    else:

        lib_id = None
        for c in collection.getContent():
            if c.getType() == 'library':
                lib_id = c.getId()

        ith = ItemHelper()
        lib_settings = ith.getLibrarySettings(lib_id)
        search = create_elastic_search_from_saved_search(lib_settings, 'admin')
        search, params = update_search_with_optional_parameters(search=search, collection_id=search_id, lib_settings=lib_settings, collection_helper=ch, querydict=None, request=None)
        elastic_query = search.to_dict()


    first = 0
    number = 100
    total = 0
    count = 0

    while True:
        elastic_results = query_elastic(
            query=elastic_query,
            first=first,
            number=number,
            fields=None
        )

        for hit in elastic_results['hits']['hits']:
            if hit["_type"] != 'item':
                continue
            item_id = hit["_id"]
            try:
                update_item_orginial_metadatas(item_id)
            except:
                print "Unexpected error for %s: %s" % (item_id, sys.exc_info()[0])
        if len(elastic_results['hits']['hits']) < number:
            count = elastic_results['hits']['total']
            break
        first = first + number

def apply_to_search_history(search_id, user):

    try:
        criteria = SearchHistory.objects.get(id=search_id).criteria_document
    except SearchHistory.DoesNotExist:
        raise ValueError('No search history with ID: %s found' % search_id)

    criteria = json.loads(criteria)

    page = 1
    results_per_page = 1000
    total = 0

    while True:
        results = client.put("/API/v2/search/",
            user=user,
            params={
                'page': page,
                'page_size': results_per_page
            },
            json=criteria
        )

        for hit in results.data['results']:
            if hit["_type"] != 'item':
                continue
            item_id = hit["id"]
            try:
                update_item_orginial_metadatas(item_id)
            except:
                print "Unexpected error for %s: %s" % (item_id, sys.exc_info()[0])
        if results.data['has_next'] == False:
            break
        page = page + 1

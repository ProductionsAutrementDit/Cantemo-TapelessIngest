from django.core.cache import cache
from urllib import quote_plus

import logging
log = logging.getLogger(__name__)

PROVIDERS_LIST = [
    "panasonicP2",
    "xdcam",
    "jvcprohd",
    "video_file"
]

TAGS_FIELD = 'portal_mf245404'
COLLECTION_TAGS_FIELD = 'portal_mf423577'

def generate_folder_clips(sender, instance, created, **kwargs):
    # Get all available providers
    providers = getProvidersList()
    for machine_name, Provider in providers.items():
        if Provider.checkPath(instance.path):
            clips = Provider.getClips(instance)
            for clip in clips:
                clip.folders.add(instance)
                clip.save()

def clip_saved(sender, instance, created, **kwargs):
    base_path = instance.ingest_base_path
    folder_path = instance.folder_path
    folder_end_path = folder_path.replace(base_path, '')
    while True:
        cache_key = "tapelessIngest_folder_%s_clips_count" % quote_plus(folder_end_path.encode('utf-8'))
        cache.delete(cache_key)
        if (len(folder_end_path.rsplit('/', 1)) > 0) & (folder_end_path.rsplit('/', 1)[0] != ''):
            folder_end_path = folder_end_path.rsplit('/', 1)[0]
        else:
            break;


def getProvidersByName(provider_name):

    moduleName = "portal.plugins.TapelessIngest.providers." + provider_name
    className = "Provider"

    module = __import__(moduleName, {}, {}, className)

    Provider = getattr(module, className )()

    return Provider


def getProvidersList():

    providers = {}

    for name in PROVIDERS_LIST:
        Provider = getProvidersByName(name)
        providers[Provider.machine_name] = Provider

    return providers

def getDefaultProvider():
    return getProvidersByName(PROVIDERS_LIST[0])

def ClipResetWithItemDeletion(_deleted_resource_name):
    from portal.plugins.TapelessIngest.models import Clip
    """
    When deleting an item in Vidispine, check the corresponding item in Clips and delete it.
    """
    log.info('Updating all Clip associated with ID %s' % _deleted_resource_name)
    try:
        clips = Clip.objects.filter(item_id=_deleted_resource_name)
        for clip in clips:
            clip.file_id = None
            clip.item_id = ""
            clip.output_file = None
            clip.job.delete()
            clip.save()
            log.info('Put clip %s status as Not Imported' % clip.umid)
    except Exception as e:
        log.info('Automatic Clip item deletion based upon Media deletion failed because %s' % e)

def get_collection_tags(collection):
    metadata_field_group = collection.getMetadata()[0]
    field = metadata_field_group.getFieldByName(TAGS_FIELD)
    return field.getFieldValues()

def update_collection_tags(collection_id, tags):

    from portal.vidispine.icollection import CollectionHelper
    import VidiRest.schemas.xmlSchema as VSXMLSchema

    ch = CollectionHelper()
    collection = ch.getCollection(collection_id, query='content=metadata')
    name = collection.getName()

    existing_values = []

    # If collection has no metadat group, creat one
    if len(ch.getCollectionMetadataFieldGroups(collection_id)) < 1:
        ch.setCollectionMetadataFieldGroup(collection_id, "Collection")
    else:
        existing_values = get_collection_tags(collection)

    values = []

    for tag in tags:
        if tag not in existing_values:
            values.append(VSXMLSchema.MetadataValueType(tag, mode=VSXMLSchema.MetadataModeType.add))


    if len(values) > 0:
        field = VSXMLSchema.MetadataFieldValueType(name=TAGS_FIELD)
        field.value_ = values
        timespan = VSXMLSchema.MetadataTypeTimespan(start='-INF', end='+INF', field=[field])
        metadata_document = VSXMLSchema.MetadataDocument(timespan)

        ch.setCollectionMetadata(collection_id, metadata_document)

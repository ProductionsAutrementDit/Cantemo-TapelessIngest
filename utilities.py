import logging
log = logging.getLogger(__name__)

PROVIDERS_LIST = [
    "panasonicP2",
    "xdcam",
    "jvcprohd",
    "video_file"
]

def generate_folder_clips(sender, instance, created, **kwargs):
    # Get all available providers
    providers = getProvidersList()
    for machine_name, Provider in providers.items():
        if Provider.checkPath(instance.path):
            clips = Provider.getClips(instance)
            for clip in clips:
                clip.folders.add(instance)
                clip.save()



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


                
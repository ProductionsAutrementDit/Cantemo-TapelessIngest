""" Helper class for folders """
    
from django.core.cache import cache
from os.path import isdir, basename
from portal.plugins.TapelessIngest.models import Clip

FOLDER_LIST = cache.get('tapeless_paths', {})

def addFolder(path):
    import portal.plugins.TapelessIngest.providers.providers as Providers
    
    messages = []
    error = False
    
    # remove trailing slashe if needed
    if path.endswith('/'):
        path = path[:-1]
    # check whether it's already in the list:
    if path in FOLDER_LIST:
        error = True
        messages.append('Path already present')
    else:
        # check whether the path is actually a directory:
        if isdir(path):
            # Get all available providers
            providers = Providers.getProvidersList()
            for machine_name, Provider in providers.items():
                if Provider.checkPath(path):
                    Provider.getClips(path)
            FOLDER_LIST[path] = basename(path)
        else:
            error = True
            messages.append('Not valid path')

    # store the paths to cache
    cache.set('tapeless_paths', FOLDER_LIST)
    
    return (error, messages)

def removeFolder(path):
    if path in FOLDER_LIST:
        del FOLDER_LIST[path]
        cache.set('tapeless_paths', FOLDER_LIST)

def getFolders():
    return FOLDER_LIST

def getFoldersClips():
    clips = Clip.objects.filter(folder_path__in=FOLDER_LIST).order_by('folder_path')
    return clips


def searchForSpannedClips():
    for folder_path, listed_folder in FOLDER_LIST.items():
        for clip in listed_folder.clips.items():
            test = True
    
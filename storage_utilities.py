def getStorageUri(storage_id):
    from portal.vidispine.istorage import StorageHelper
    
    storage_root_path = False

    storage_helper = StorageHelper()

    storage = storage_helper.getStorage(storage_id)
    if storage:
        storage_methods = storage.getMethods()
        for s in storage_methods:
            if s.getBrowse():
                storage_root_path = s.getFirstURI()[u'url']
    
    return storage_root_path
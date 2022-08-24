# coding: utf-8
from portal.plugins.TapelessIngest.providers.file import Provider as FileProvider


# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(FileProvider):
    def __init__(self):
        FileProvider.__init__(self)
        self.name = "ZOOM"
        self.machine_name = "zoom"
            
    def getExtensions(self):
        return [".wav"]
                        
    def getSubPaths(self):
        return [
                "FOLDER[0-9]{2}/ZOOM[0-9]{4}"
            ]

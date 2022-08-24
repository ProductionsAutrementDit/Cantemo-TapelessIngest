# coding: utf-8
from portal.plugins.TapelessIngest.providers.file import Provider as FileProvider


# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(FileProvider):
    def __init__(self):
        FileProvider.__init__(self)
        self.name = "ATOMOS"
        self.machine_name = "atomos"
            
    def getExtensions(self):
        return [".mov", ".mp4"]
                        
    def getSubPaths(self):
        return [
                "[^/]*_S[0-9]{3}_S[0-9]{3}_T[0-9]{3}"
            ]

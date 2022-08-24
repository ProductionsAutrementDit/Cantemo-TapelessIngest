# coding: utf-8
from portal.plugins.TapelessIngest.providers.file import Provider as FileProvider


# Classe jvcprohd: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(FileProvider):
    def __init__(self):
        FileProvider.__init__(self)
        self.name = "AVCHD"
        self.machine_name = "avchd"
            
    def getExtensions(self):
        return [".mts", ".m2ts", ".m2t"]

    def getSubPaths(self):
        return [
                "((PRIVATE/)?AVCHD/)?BDMV/STREAM"
            ]

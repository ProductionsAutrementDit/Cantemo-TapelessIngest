# coding: utf-8
from portal.plugins.TapelessIngest.providers.file import Provider as FileProvider

class Provider(FileProvider):
    def __init__(self):
        FileProvider.__init__(self)
        self.name = "HDSLR"
        self.machine_name = "hdslr"
    
    def getExtensions(self):
        return [".mxf", ".mp4", ".mov"]
                
    def getSubPaths(self):
        return [
                "DCIM/([0-9]{3})(GOPRO|EOS7D|_PANA|MEDIA)"
            ]

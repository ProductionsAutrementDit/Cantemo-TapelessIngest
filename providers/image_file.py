# coding: utf-8

import logging

from lxml import etree
from xml.dom import minidom
import subprocess as sp

import os
import uuid

from django.core.exceptions import ObjectDoesNotExist

from portal.plugins.TapelessIngest.providers.file import Provider as FileProvider

log = logging.getLogger(__name__)


# Classe ProviderP2: Récupère les clips à partir des fichiers XML du dossier CLIP
class Provider(FileProvider):
    def __init__(self):
        FileProvider.__init__(self)
        self.name = "IMAGE"
        self.machine_name = "image_file"
        self.file_types = {
            ".jpg": "image",
            ".png": "image",
            ".bmp": "image",
            ".gif": "image",
            ".tif": "image",
            ".tiff": "image",
        }

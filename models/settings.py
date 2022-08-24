import logging

from django.db import models
from portal.vidispine.iitem import ItemHelper

from django.utils.translation import ugettext_lazy as _

log = logging.getLogger(__name__)


class Settings(models.Model):
    storage_id = models.CharField(max_length=255, blank=True, default="", db_column="storage")
    tmp_storage = models.CharField(max_length=255, blank=True, default="")
    bmxtranswrap = models.CharField(max_length=255, blank=True, default="")
    mxf2raw = models.CharField(max_length=255, blank=True, default="")
    ffmpeg_path = models.CharField(max_length=255, blank=True, default="")
    base_folder = models.CharField(max_length=255, blank=True, default="")

    collections_ignore_folder_str = models.TextField(blank=True, default="", db_column="collections_ignore_folder")
    collections_rename_folder_str = models.TextField(blank=True, default="", db_column="collections_rename_folder")

    @property
    def storage(self):
        if not hasattr(self, "_storage"):
            from portal.vidispine.istorage import StorageHelper

            sth = StorageHelper()
            self._storage = sth.getStorage(self.storage_id)
        return self._storage

    @storage.setter
    def storage(self, storage_id):
        from portal.vidispine.istorage import StorageHelper

        sth = StorageHelper()
        self._storage = sth.getStorage(storage_id)
        self.storage_id = storage_id

    @property
    def collections_ignore_folder(self):
        if not hasattr(self, "_collections_ignore_folder"):
            self._collections_ignore_folder = self.collections_ignore_folder_str.split(",")
        return self._collections_ignore_folder

    @collections_ignore_folder.setter
    def collections_ignore_folder(self, folder_list):
        self.collections_ignore_folder_str = ",".join(folder_list)
        delattr(self, "_collections_ignore_folder")

    @property
    def collections_rename_folder(self):
        if not hasattr(self, "_ccollections_rename_folder"):
            self._collections_rename_folder = {}
            lines = self.collections_rename_folder_str.splitlines()
            for line in lines:
                if line != "":
                    key, value = line.split(":")
                    self._collections_rename_folder[key] = value
        return self._collections_rename_folder

    @collections_rename_folder.setter
    def collections_rename_folder(self, rename_folders):
        rename_folders_list = []
        for key, value in rename_folders.items():
            rename_folders_list.append(f"{key}:{value}")
        self.collections_rename_folder_str = "\r\n".join(rename_folders_list)
        delattr(self, "_collections_rename_folder")


class MetadataMapping(models.Model):
    metadata_provider = models.CharField(max_length=200)
    metadata_portal = models.CharField(max_length=100, blank=True, null=True, default="")
    
    @property
    def metadata_portal_vfield(self):
        ith = ItemHelper()
        field_name_path = self.metadata_portal.split(":")
        field_name = field_name_path[len(field_name_path) - 1]
        return ith.getMetadataField(field_name)

"""
********************************************************
Interface for Ingest
********************************************************

Ingest helpers for the tapeless clips.

.. Copyright 2020 PAD
"""
import os
import re
import logging
import urllib.parse

from django.core.cache import cache

from portal.api import client
from portal.vidispine.icollection import CollectionHelper
from portal.vidispine.istorage import StorageHelper

from portal.plugins.TapelessIngest.models.settings import Settings


log = logging.getLogger(__name__)
PROVIDERS_LIST = [
    "red",
    "panasonicP2",
    "xdcam",
    "hdslr",
    "zoom",
    "avchd",
    "atomos",
    "file",
]


class TapelessIngestException(Exception):
    pass


class TapelessIngestPath(object):
    def __init__(self, storage_id, path):
        self.path = path
        self.storage_id = storage_id
        self.storage = None
        self.root_path = None

        storage_helper = StorageHelper()
        self.storage = storage_helper.getStorage(storage_id)
        if self.storage:
            storage_methods = self.storage.getMethods()
            for s in storage_methods:
                if s.getBrowse():
                    self.root_path = s.getFirstURI()["url"]

    @property
    def absolute_path(self):
        if self.root_path:
            return os.path.join(self.root_path, self.path)
        return False

    def add_subpath(self, subpath):
        self.path = os.path.join(self.path, subpath)

    def __str__(self):
        return f"{self.storage_id}: {self.path}"


class TapelessIngestHelper:
    def __init__(self):
        self.ingest_paths = []
        self.clips = []

    def add_path(self, storage, path):
        ti_path = TapelessIngestPath(storage, path)
        self.ingest_paths.append(ti_path)

    @staticmethod
    def get_provider_by_name(provider_name):

        moduleName = f"portal.plugins.TapelessIngest.providers.{provider_name}"
        className = "Provider"

        module = __import__(moduleName, {}, {}, className)

        Provider = getattr(module, className)()

        return Provider

    def _get_provider_list(self):

        providers = {}

        for name in PROVIDERS_LIST:
            Provider = TapelessIngestHelper.get_provider_by_name(name)
            providers[Provider.machine_name] = Provider

        return providers

    def get_all_clips(self):
        if len(self.ingest_paths) < 1:
            return []

        provider_status_list = []
        providers = self._get_provider_list()

        from portal.plugins.TapelessIngest.providers.file import (
            Provider as FileProvider,
        )

        for machine_name, Provider in list(providers.items()):
            provider_status_list.append(
                {"name": machine_name, "status": "inactive"}
            )

        for ti_path in self.ingest_paths:
            clips_in_path = []
            full_path = ti_path.absolute_path
            if full_path:
                # Get all available providers
                for machine_name, Provider in list(providers.items()):
                    provider_clips = Provider.getAllClipsInPath(ti_path)
                    if len(provider_clips) > 0:
                        for provider_status in provider_status_list:
                            if provider_status["name"] == machine_name:
                                provider_status.update({"status": "active"})
                    clips_in_path += provider_clips
                # If no providers returned clips, get all files instead
                if len(clips_in_path) == 0:
                    file_provider = FileProvider()
                    clips_in_path = file_provider.getAllClipsInPath(ti_path)
                self.clips += clips_in_path
            else:
                raise TapelessIngestException(
                    f"Cannot get full path from {ti_path}"
                )

        return self.clips

    @staticmethod
    def get_collection_from_path(path, user):
        settings = Settings.objects.get(pk=1)
        path_items = path.split(os.sep)
        ignore_list = settings.collections_ignore_folder
        combined = "(" + ")|(".join(ignore_list) + ")"
        renames = settings.collections_rename_folder
        filtered_path_items = []
        for path_item in path_items:
            if re.match(combined, path_item):
                continue
            # Change path item name according to renaming rules
            if path_item in renames.keys():
                filtered_path_items.append(renames[path_item])
                continue
            filtered_path_items.append(path_item)

        filtered_path = os.sep.join(filtered_path_items)

        ch = CollectionHelper(runas=user)

        cache_key = urllib.parse.quote(
            f"tapelessingest_path_collection_{filtered_path}"
        )
        collection_id = cache.get(cache_key)
        if collection_id is not None:
            return collection_id

        parent_id = None
        for index, path_item in enumerate(filtered_path_items):
            cache_key_subcollection = urllib.parse.quote(
                f"tapelessingest_subpath_collection_{parent_id}_{path_item}"
            )
            cached_parent_id = cache.get(cache_key_subcollection)
            if cached_parent_id is not None:
                parent_id = cached_parent_id
                continue
            # find if collection already exists
            query_doc = {
                "doc_types": ["collection"],
                "fields": ["id"],
                "query": "*",
                "filter": {
                    "operator": "AND",
                    "terms": [
                        {"name": "portal_deleted", "missing": True},
                        {"name": "title", "value": path_item, "exact": True},
                    ],
                },
            }
            if parent_id is None:
                query_doc["filter"]["terms"].append(
                    {"name": "parent_collection", "missing": True}
                )
            else:
                query_doc["filter"]["terms"].append(
                    {
                        "name": "parent_collection",
                        "value": parent_id,
                        "exact": True,
                    }
                )
            log.info(query_doc)
            response = client.put(
                "/API/v2/search/",
                user=user,
                params={"page_size": 1},
                json=query_doc,
            )
            log.info(response)
            if response.status_code == 200:
                # data now contains the return value from the API endpoint
                data = response.data
                total_hits = data["hits"]
                if total_hits:
                    parent_id = data["results"][0]["id"]
                else:
                    collection = ch.createCollection(
                        collection_name=path_item, settingsprofile_id="VX-6"
                    )
                    if parent_id is not None:
                        ch.addCollectionToCollection(
                            parent_id, collection.getId()
                        )
                    parent_id = collection.getId()
                cache.set(cache_key_subcollection, parent_id)
            else:
                break
        cache.set(cache_key, parent_id, 60)
        return parent_id

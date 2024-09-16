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

from django.conf import settings
from django.core.cache import cache

from VidiRest.itemapi import ItemAPI

from portal.api import client
from portal.vidispine.iitem import ItemHelper, IngestHelper
from portal.vidispine.icollection import CollectionHelper
from portal.vidispine.istorage import StorageHelper
from portal.vidispine import signals
from RestAPIBase.utility import RestAPIBaseComError

from portal.items.cache import invalidate_item_cache
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


class TapelessIngestItemAPI(ItemAPI):
    def doImportToPlaceholder(
        self,
        item_id,
        query=None,
        matrix=None,
        component="container",
        ingestprofile_groups=None,
        ignore_sidecars=False,
        runasuser=None,
        return_format="json",
    ):
        log.info(
            f"doImportToPlaceholder for {item_id}: query={query} matrix={matrix} component={component} ingestprofile_groups={ingestprofile_groups} ignore_sidecars={ignore_sidecars}  runasuser={runasuser} return_format={return_format}"
        )
        return super().doImportToPlaceholder(
            item_id,
            query=query,
            matrix=matrix,
            component=component,
            ingestprofile_groups=ingestprofile_groups,
            ignore_sidecars=ignore_sidecars,
            runasuser=runasuser,
            return_format=return_format,
        )


class TapelessIngestHelper(IngestHelper):
    def provideItemAPI(self):
        if not hasattr(self, "itemapi"):
            self.itemapi = TapelessIngestItemAPI(self._vsapi)

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

        cache_key = urllib.parse.quote(f"tapelessingest_path_collection_{filtered_path}")
        collection_id = cache.get(cache_key)
        if collection_id is not None:
            return collection_id

        parent_id = None
        for index, path_item in enumerate(filtered_path_items):
            cache_key_subcollection = urllib.parse.quote(f"tapelessingest_subpath_collection_{parent_id}_{path_item}")
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
                query_doc["filter"]["terms"].append({"name": "parent_collection", "missing": True})
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
                    collection = ch.createCollection(collection_name=path_item, settingsprofile_id="VX-6")
                    if parent_id is not None:
                        ch.addCollectionToCollection(parent_id, collection.getId())
                    parent_id = collection.getId()
                cache.set(cache_key_subcollection, parent_id)
            else:
                break
        cache.set(cache_key, parent_id, 60)
        return parent_id

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
import threading
import urllib.parse

from django.core.cache import cache  # type: ignore

from VidiRest.itemapi import ItemAPI  # type: ignore

from portal.api import client  # type: ignore
from portal.vidispine.iitem import IngestHelper  # type: ignore
from portal.vidispine.icollection import CollectionHelper  # type: ignore
from portal.vidispine.istorage import StorageHelper  # type: ignore

from portal.plugins.TapelessIngest.models.settings import Settings  # type: ignore
from portal.plugins.TapelessIngest.scan.context import browse_root_path

log = logging.getLogger(__name__)

# Story 3.1: collection resolution is check-then-create over a 60 s cache.
# Two pool workers resolving overlapping paths could both miss the cache
# and both create the same Vidispine collection; the lock serializes the
# whole resolve-or-create walk so the second worker finds what the first
# cached. IN-PROCESS only, deliberately: the cross-process race (cron run
# beside a Portal UI ingest) predates the pool and belongs to the
# deferred mutual-exclusion story (see deferred-work.md).
_collection_resolution_lock = threading.Lock()

# Bounded acquire (3.1 review): one resolver wedged inside a hung
# Vidispine call must not block every other worker forever. On timeout
# the folder fails honestly at its boundary (process_folder catches),
# instead of the whole pool silently queueing behind one dead call.
COLLECTION_RESOLUTION_LOCK_TIMEOUT = 300


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
        # Canonical block (scan/context.py): None when no browse-capable
        # method. root_path is always SET, as before (the old code
        # initialized it to None above, then possibly overwrote it).
        # Deliberate unification: first browse method wins now; the old
        # inline loop let the last one win (no prod storage has two).
        self.root_path = browse_root_path(self.storage)

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
    def get_collection_from_path(path, user, dryrun=False):
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

        # Double-checked fast path (3.1 review): a warm cache answers
        # WITHOUT touching the lock, so concurrent workers serialize only
        # on real resolve-or-create work, never on pure cache hits.
        cache_key = urllib.parse.quote(
            f"tapelessingest_path_collection_{filtered_path}"
        )
        collection_id = cache.get(cache_key)
        if collection_id is not None:
            return collection_id

        ch = CollectionHelper(runas=user)

        if not _collection_resolution_lock.acquire(
            timeout=COLLECTION_RESOLUTION_LOCK_TIMEOUT
        ):
            raise TapelessIngestException(
                f"collection resolution for {filtered_path} timed out after "
                f"{COLLECTION_RESOLUTION_LOCK_TIMEOUT}s waiting for another "
                f"resolver — is a Vidispine call hung?"
            )
        try:
            return TapelessIngestHelper._resolve_collection(
                ch, filtered_path_items, filtered_path, user
            )
        finally:
            _collection_resolution_lock.release()

    @staticmethod
    def _resolve_collection(ch, filtered_path_items, filtered_path, user):
        """The locked resolve-or-create walk over the collection cache."""
        # The second half of the double check: a worker that queued on the
        # lock behind the resolver finds the freshly cached id here.
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
                        ch.addCollectionToCollection(parent_id, collection.getId())
                    parent_id = collection.getId()
                cache.set(cache_key_subcollection, parent_id)
            else:
                break
        cache.set(cache_key, parent_id, 60)
        return parent_id

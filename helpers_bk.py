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


class TapelessIngestHelper(IngestHelper):
    @staticmethod
    def get_clip_from_file(file, provider_list=None, context=None):
        from portal.plugins.TapelessIngest.models.clip import Clip

        if provider_list is None:
            provider_list = TapelessIngestHelper._get_provider_list()
        if context is None:
            context = {}
        metadatas = {}
        for provider in provider_list:
            metadatas, context = provider.getMetadatasFromFile(file, metadatas, context)
            if "provider" in metadatas.keys():
                break
            if "umid" not in metadatas.keys():
                break
        umid = metadatas["umid"]
        clip, created = Clip.get_or_new(
            umid=umid,
            defaults={
                "path": os.path.dirname(file.getPath()),
                "storage_id": file.getStorage(),
                "spanned": False,
            },
        )
        clip.provider_name = metadatas["provider"]
        clip.metadatas = metadatas
        clip.file = file
        clip.reference_file = file.getId()

        return clip, context, created

    def _findExtraFilesInSequence(self, clip):
        """Returns a hash with the additional video and audio files which belongs to this sequence.

        This function currently only handles RED files, but could
        be extended to other sequence formats later on, e.g. P2
        file structures.
        """
        file_component_list = {"video": [], "audio": []}
        extraFiles = clip.provider.getClipAdditionalMediaFiles(clip)
        for extraFile in extraFiles:
            file_component_list[extraFile["type"]].append(extraFile["file_id"])

        return file_component_list

    def importFileToPlaceholder(
        self,
        item_id,
        clip,
        tags=None,
        ingestprofile_groups=None,
        createPosters=None,
        notification_id=None,
        fastStartLength=None,
        ignore_sidecars=False,
        jobmetadata=None,
        noTranscode=None,
    ):
        """
        Import a file given a file ID or a URI, to an existing placeholder

        Args:
            * item_id: The placeholder ID.
            * clip: The item's associated tapaless clip.
            * tags: Shape tags to be transcoded to. Optional.
            * ingestprofile_groups: A list of groups for which the ingest profiles will be used for this ingest
            * createPosters: If set, poster images will be created
            * notification_id = If set, the notification template will be used to send notifications for this job
            * jobmetadata: override the jobmetadata sent to vidispine (overrides ingestprofile_groups)
            * noTranscode: If set to true, then no transcoding takes place when ingesting. Valid options are "true" or "false"
            * ignore_sidecars: If True, the job will not try to find sidecar files to the imported file. Default is
               False.

        Returns:
            * A Vidispine object for the job created

        Signals raised:
            * Pre ingest: sender=self.__class__, instance=item_id, method="importFileToPlaceholder", query=_query
            * Post ingest: sender=self.__class__, instance=item_id, method="importFileToPlaceholder"
        """
        file_component = clip.provider.getClipMainMediaFile(clip)
        file_id = clip.file_id
        if not ingestprofile_groups:
            ingestprofile_groups = []
        else:
            _query = {}
            if file_id:
                _query["fileId"] = file_id
            if tags:
                _query["tag"] = tags
            if noTranscode:
                _query["no-transcode"] = noTranscode
            if jobmetadata:
                _query["jobmetadata"] = jobmetadata
            else:
                jobmetadatas = []
                if len(ingestprofile_groups) > 0:
                    jobmetadatas.append("portal_groups:StringArray%3d" + ",".join(ingestprofile_groups))
                if ignore_sidecars:
                    jobmetadatas.append("ignoreSidecar:String%3dtrue")
                _query["jobmetadata"] = jobmetadatas
            if createPosters:
                _query["createPosters"] = createPosters
            if notification_id:
                _query["notification"] = notification_id
            if settings.INGEST_GROWING:
                _query["growing"] = "true"
                if fastStartLength is None:
                    fastStartLength = 3600
            if fastStartLength is not None:
                _query["overrideFastStart"] = "true"
                _query["requireFastStart"] = "true"
                _query["fastStartLength"] = fastStartLength
            extraFileIds = self._findExtraFilesInSequence(clip)
            if extraFileIds["video"] or extraFileIds["audio"]:
                _ith = ItemHelper(runas=(self.runas))
                _item = _ith.getItem(item_id, content={"content": ["metadata"]})
                _shape_id = _item.getMetadataFieldValueByName("__shape")
                _video_count = len(extraFileIds["video"])
                _audio_count = len(extraFileIds["audio"])
                if file_component["type"] == "video":
                    _video_count + 1
                if file_component["type"] == "audio":
                    _audio_count + 1
                if _shape_id:
                    self.itemapi.updatePlaceholderComponentCount(
                        item_id,
                        _shape_id,
                        container=1,
                        video=(_video_count),
                        audio=(_audio_count),
                    )
            try:
                signals.vidispine_pre_ingest.send(
                    sender=(IngestHelper),
                    instance=item_id,
                    method="importFileToPlaceholder",
                    query=_query,
                )
                if extraFileIds["video"] or extraFileIds["audio"]:
                    for extra_fid in extraFileIds["video"]:
                        q = {"fileId": extra_fid}
                        self.itemapi.doImportToPlaceholder(
                            item_id=item_id,
                            query=q,
                            component=file_component["type"],
                            runasuser=(self.runas),
                        )
                log.info(f"Send query {_query} to doImportToPlaceholder for item {item_id}")
                res = self.itemapi.doImportToPlaceholder(item_id=item_id, query=_query, runasuser=(self.runas))
                invalidate_item_cache(item_id)
                signals.vidispine_post_ingest.send(
                    sender=(IngestHelper),
                    instance=item_id,
                    method="importFileToPlaceholder",
                )
                return res
            except RestAPIBaseComError as e:
                handleRestAPIError(e)
            except Exception as e:
                log.error(("Couldn't get library: %s" % str(e)), exc_info=True)
                raise

    @staticmethod
    def get_provider_by_name(provider_name):
        moduleName = f"portal.plugins.TapelessIngest.providers.{provider_name}"
        className = "Provider"

        module = __import__(moduleName, {}, {}, className)

        Provider = getattr(module, className)()

        return Provider

    @staticmethod
    def _get_provider_list(providers=None):
        filtered_providers = []

        if providers is None:
            providers = PROVIDERS_LIST

        for name in providers:
            Provider = TapelessIngestHelper.get_provider_by_name(name)
            filtered_providers.append(Provider)

        return filtered_providers

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

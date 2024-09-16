import logging
import traceback
import uuid
import os
import re
from django.db import models
from django.utils import timezone
from django.conf import settings
from elasticsearch_dsl import Search, Q

from VidiRest.objects.storage import VSFile

from portal.search.elastic import query_elastic
from portal.api import client
from portal.vidispine.istorage import StorageHelper
from portal.vidispine.icollection import CollectionHelper
from portal.vidispine.iexception import NotFoundError


from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.helpers import (
    TapelessIngestHelper,
    TapelessIngestException,
)

log = logging.getLogger(__name__)


class Folder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_on = models.DateTimeField(auto_now=True)
    scanned_on = models.DateTimeField(null=True)
    path = models.TextField()
    clips_total = models.IntegerField(null=False, default=0)
    storage_id = models.CharField(max_length=255, null=True)
    collection_id = models.TextField(null=True)
    provider_names = models.CharField(max_length=100, db_column="providers")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.error = None

    def __unicode__(self):
        return f"{self.id}: {self.path}"

    def __str__(self):
        return f"{self.id}: {self.path}"

    class Meta:
        unique_together = ["path", "storage_id"]

    @classmethod
    def get_or_new(cls, defaults=None, **kwargs):
        try:
            return cls.objects.get(**kwargs), False
        except cls.DoesNotExist:
            defaults = defaults or {}
            params = {k: v for k, v in kwargs.items()}
            params.update(defaults)
            # Try to create an object using passed params.
            return cls(**params), True

    def get_storage_helper(self):
        if not hasattr(self, "_sth"):
            self._sth = StorageHelper()
        return self._sth

    @property
    def storage(self):
        if not hasattr(self, "_storage"):
            if self.storage_id is None:
                return None
            _sth = self.get_storage_helper()
            try:
                self._storage = _sth.getStorage(self.storage_id)
            except NotFoundError:
                return None
        return self._storage

    @storage.setter
    def storage(self, storage):
        self._storage = storage
        self.storage_id = storage.getId()

    @property
    def root_path(self):
        if not hasattr(self, "_root_path"):
            if self.storage:
                storage_methods = self.storage.getMethods()
                for s in storage_methods:
                    if s.getBrowse():
                        self._root_path = s.getFirstURI()["url"]
        return self._root_path

    @property
    def absolute_path(self):
        if self.root_path:
            return os.path.join(self.root_path, self.path)
        return False

    def getFile(self, path):
        subpath = os.path.join(self.path, path)
        vsfile = self._sth.getFileByPath(self.storage_id, path=subpath)
        return vsfile

    def getFiles(self, path, number=0, first=0, user=None):
        subpath = os.path.join(self.path, path)
        files = []
        page = 1
        has_next = True
        while has_next:
            has_next = False
            response = client.get(
                "/API/v2/files/",
                user=user,
                params={
                    "page": page,
                    "storage": self.storage_id,
                    "path": subpath,
                    "item_type": "file",
                },
            )
            if response.status_code != 200:
                break
            data = response.data
            has_next = data["meta"]["has_next"]
            page = data["meta"]["next"]
            files += data["objects"]
        return files

    @property
    def collection(self):
        if not hasattr(self, "_collection"):
            if self.collection_id is None:
                return None
            ch = CollectionHelper()
            try:
                self._collection = ch.getCollection(self.collection_id)
            except NotFoundError:
                return None
        return self._collection

    def get_clips(self, first=0, number=25, cursor=None):
        return self.scan(first=first, number=number, cursor=cursor)

    @property
    def clips_ingested(self):
        return self.clip_set.exclude(item_id__exact="")

    @property
    def providers(self):
        if hasattr(self, "_providers"):
            return self._providers
        else:
            self._providers = None
            if self.provider_names != "":
                self._providers = []
                provider_names = self.provider_names.split(",")
                for provider_name in provider_names:
                    self._providers.append(Clip.get_provider_by_name(provider_name))
        return self._providers

    @providers.setter
    def providers(self, providers_objects):
        self._providers = providers_objects
        providers_names = []
        for provider_object in providers_objects:
            providers_names.append(provider_object.machine_name)
        self.provider_names = ",".join(providers_names)

    def getCollection(self, user):
        return TapelessIngestHelper.get_collection_from_path(self.path, user)

    def get_helper(self):
        if hasattr(self, "_tih"):
            return self._tih
        self._tih = TapelessIngestHelper()
        return self._tih

    def get_metadatas_from_file(self, file, context, provider_list):
        metadatas = {}
        for provider in provider_list:
            metadatas, context = provider.getMetadatasFromFile(file, metadatas, context)
            if "provider" in metadatas.keys():
                break
        return metadatas

    def build_search_doc(self, provider_list):
        extensions = []
        subpaths = []
        filters = []
        escaped_path = re.escape(self.path)
        for provider in provider_list:
            # Get all extensions
            extensions += provider.getExtensions()
            subpaths += provider.getSubPaths()
            filters += provider.getFilters(escaped_path)
        search = Search()
        # Only files in this storage
        search = search.filter("term", storage=self.storage_id)
        # Only file which are not LOST or MISSING
        search = search.filter(
            "bool", must_not=[Q("term", state="LOST"), Q("term", state="MISSING")]
        )
        parent_filter = None
        parent_filters = []
        parent_filters.append(Q("regexp", parent=escaped_path))
        for subpath in list(set(subpaths)):
            parent_filters.append(
                Q("regexp", parent=os.path.join(escaped_path, subpath))
            )
        if len(parent_filters):
            parent_filter = Q("bool", should=parent_filters)
        extension_filter = None
        extension_filters = []
        for extension in list(set(extensions)):
            extension_filters.append(Q("wildcard", name="*" + extension.lower()))
            extension_filters.append(Q("wildcard", name="*" + extension.upper()))
        if len(extension_filters):
            extension_filter = Q("bool", should=extension_filters)
        raw_filter = None
        raw_filters = []
        for filter in filters:
            raw_filters.append(Q(filter))
        if len(raw_filters):
            raw_filter = Q("bool", should=raw_filters)
        parent_and_extension_filter = None
        if parent_filter and extension_filter:
            parent_and_extension_filter = Q(
                "bool", must=[parent_filter, extension_filter]
            )
        else:
            if parent_filter:
                parent_and_extension_filter = parent_filter
            if extension_filter:
                parent_and_extension_filter = extension_filter
        search = search.filter(
            Q(
                "bool",
                should=[
                    i
                    for i in [parent_and_extension_filter, raw_filter]
                    if i is not None
                ],
            )
        )
        search_doc = search.to_dict()
        return search_doc

    def count(self, user=None, providers=None):
        provider_list = Clip._get_provider_list()
        response = self.scan(
            first=0, number=0, user=user, providers=providers, count_only=True
        )
        return response["hits"]

    def getSubfolders(self, user):
        if hasattr(self, "_folders"):
            return self._folders
        result = client.get(
            f"/API/v2/storages/{self.storage_id}/content",
            user=user,
            params={
                "format": "json",
                "item_type": "directory",
                "path": self.path,
                "sort": "name_asc",
                "page_size": 1000,
                "page": 1,
            },
        )
        folders = []
        if result.status_code == 200:
            data = result.data
            objects = data["objects"]
            for object in objects:
                folder, is_new = Folder.get_or_new(
                    storage_id=object["storage"], path=object["path"]
                )
                folders.append(folder)
        self._folders = folders
        return self._folders

    def scan(
        self,
        first=0,
        number=25,
        cursor=None,
        user=None,
        providers=None,
        count_only=False,
        legacy_storages=[],
    ):
        provider_list = Clip._get_provider_list(providers)
        providers = []
        response = {
            "clips": [],
            "hits": 0,
            "errors": [],
            "created": 0,
            "already_ingested": 0,
            "processed": 0,
        }
        if self.absolute_path:
            search_doc = self.build_search_doc(provider_list)
            has_next = True
            while has_next:
                has_next = False
                result_number = number
                if number == 0:
                    result_number = 100
                search_result = query_elastic(
                    search_doc,
                    doc_type=["file"],
                    first=first,
                    number=result_number,
                )
                response["hits"] = search_result["hits"]["total"]["value"]
                self.clips_total = response["hits"]
                if number == 0 and len(search_result["hits"]["hits"]) == result_number:
                    has_next = True
                    first += result_number
                context = {"folder": self, "clips": []}
                if not count_only:
                    for result in search_result["hits"]["hits"]:
                        try:
                            file = VSFile(
                                result["_source"], settings.VIDISPINE_REPLACE_URLS
                            )
                            # Does file exists?
                            file_absolute_path = os.path.join(
                                self.root_path, file.getPath()
                            )
                            if os.path.exists(file_absolute_path) is False:
                                raise TapelessIngestException(
                                    f"File {file} does not exists ({file_absolute_path})"
                                )
                            (
                                clip,
                                context,
                                created,
                            ) = Clip.get_clip_from_file(
                                file,
                                provider_list,
                                context,
                                legacy_storages=legacy_storages,
                            )
                            if created:
                                response["created"] += 1
                            if clip.file is not None:
                                response["already_ingested"] += 1
                            if clip.metadatas["provider"] not in providers:
                                providers.append(clip.metadatas["provider"])
                            response["clips"].append(clip)
                            context["clips"].append(clip)
                        except Exception as e:
                            traceback.print_exc()
                            response["errors"].append(
                                f"Error scanning file {result['_source']['path']}: {e}"
                            )

                        response["processed"] += 1
                    self.provider_names = ",".join(providers)
                    self.scanned_on = timezone.now()
                if len(providers) > 0:
                    self.save()
        else:
            self.error = (
                f"Cannot get full path from storage {self.storage_id}, path {self.path}"
            )
            response["errors"].append(self.error)
        return response

    def ingest(
        self,
        first=0,
        number=25,
        cursor=None,
        user=None,
        providers=None,
        replace=False,
        legacy_storages=[],
        dry_run=False,
    ):
        response = self.scan(
            first=first,
            number=number,
            cursor=cursor,
            user=user,
            providers=providers,
            legacy_storages=legacy_storages,
        )
        for key in ["ingested", "skipped", "failed", "replaced"]:
            response[key] = 0
        if not dry_run and len(response["clips"]) > 0:
            self.collection_id = self.getCollection(user)
            self.save()
            for index, clip in enumerate(response["clips"]):
                if providers is not None and clip.provider_name not in providers:
                    continue
                try:
                    result = clip.ingest(
                        user=user,
                        collection_id=self.collection_id,
                        folder=self,
                        replace=replace,
                        legacy_storages=legacy_storages,
                    )
                    for key, value in result.items():
                        if key not in response.keys():
                            response[key] = 0
                        if value is True:
                            response[key] += 1
                except Exception as e:
                    log.error(
                        f"Error ingesting clip {clip}: {e}",
                        exc_info=True,
                    )
                    response["errors"].append(f"Error ingesting clip {clip}: {e}")

        return response

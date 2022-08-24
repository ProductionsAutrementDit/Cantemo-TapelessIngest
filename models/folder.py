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
                    self._providers.append(
                        self.get_helper().get_provider_by_name(provider_name)
                    )
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
            metadatas, context = provider.getMetadatasFromFile(
                file, metadatas, context
            )
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
        search = search.filter("term", storage=self.storage_id)
        parent_filters = []
        parent_filters.append(Q("regexp", parent=escaped_path))
        for subpath in list(set(subpaths)):
            parent_filters.append(
                Q("regexp", parent=os.path.join(escaped_path, subpath))
            )
        parent_filter = Q("bool", should=parent_filters)
        extension_filters = []
        for extension in list(set(extensions)):
            extension_filters.append(
                Q("wildcard", name="*" + extension.lower())
            )
            extension_filters.append(
                Q("wildcard", name="*" + extension.upper())
            )
        extension_filter = Q("bool", should=extension_filters)
        raw_filters = []
        for filter in filters:
            raw_filters.append(Q(filter))
        raw_filter = Q("bool", should=raw_filters)
        parent_and_extension_filter = Q(
            "bool", must=[parent_filter, extension_filter]
        )
        search = search.filter(
            Q("bool", should=[parent_and_extension_filter, raw_filter])
        )
        search_doc = search.to_dict()
        return search_doc

    def scan(self, first=0, number=25, cursor=None, user=None):
        _tih = self.get_helper()
        provider_list = list(_tih._get_provider_list().values())
        providers = []
        clips = []
        self.clips_total = 0
        if self.absolute_path:
            try:
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
                    if (
                        number == 0
                        and len(search_result["hits"]["hits"]) == result_number
                    ):
                        has_next = True
                        first += result_number
                    context = {"folder": self, "clips": []}
                    for result in search_result["hits"]["hits"]:
                        file = result["_source"]
                        metadatas = self.get_metadatas_from_file(
                            file, context, provider_list
                        )
                        if "umid" not in metadatas.keys():
                            break
                        umid = metadatas["umid"]
                        clip, created = Clip.get_or_new(
                            umid=umid,
                            defaults={
                                "folder_path": self.absolute_path,
                                "path": file["parent"],
                                "storage_id": self.storage_id,
                                "spanned": False,
                            },
                        )
                        clip.provider_name = metadatas["provider"]
                        clip.metadatas = metadatas
                        clip.file = VSFile(
                            file, settings.VIDISPINE_REPLACE_URLS
                        )
                        clip.reference_file = file["vidispine_id"]
                        if metadatas["provider"] not in providers:
                            providers.append(metadatas["provider"])
                        clips.append(clip)
                        context["clips"].append(clip)

                self.provider_names = ",".join(providers)
                self.scanned_on = timezone.now()

                if len(providers) > 0:
                    self.save()

                return {
                    "clips": clips,
                    "hits": search_result["hits"]["total"]["value"],
                }

            except FileNotFoundError:
                self.error = f"No such file or directory: {self.absolute_path}"
                raise TapelessIngestException(
                    f"No such file or directory: {self.absolute_path}"
                )
        else:
            raise TapelessIngestException(
                f"Cannot get full path from storage {self.storage_id}, path {self.path}"
            )

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

    def ingest(self, user=None, providers=None):
        clips = []
        response = self.scan(first=0, number=0, cursor=None, user=user)
        if len(response["clips"]) > 0:
            self.collection_id = self.getCollection(user)
            self.save()
            for clip in response["clips"]:
                if (
                    providers is not None
                    and clip.provider_name not in providers
                ):
                    continue
                try:
                    clip.ingest(
                        user=user, collection_id=self.collection_id, folder=self
                    )
                    clips.append(clip)
                except Exception as e:
                    print(f"Clip Id {clip.umid}: {e}")
        return clips

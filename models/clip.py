import logging

import os
import urllib
import pyxb.utils, simplejson as json
from django.urls import reverse
from django.db import models
from django.contrib.auth.models import User
from django.conf import settings

from RestAPIBase.resturl import RestURL
from RestAPIBase.utility import perform_request, prepare_request, RestAPIBaseComError

from VidiRest.itemapi import ItemAPI
from VidiRest.objects.shape import VSShape
from VidiRest.helpers.vidispine import (
    createMetadataDocumentFromDict,
    createMergedBatchItemMetadataDocument,
)


from portal.api.v2.utils import format_datetime
from portal.vidispine import signals
from portal.vidispine.ijob import JobHelper
from portal.vidispine.iitem import ItemHelper
from portal.vidispine.icollection import CollectionHelper
from portal.vidispine.igroup import GroupHelper
from portal.vidispine.istorage import StorageHelper
from portal.vidispine.iuser import UserHelper
from portal.vidispine.iexception import handleRestAPIError, NotFoundError, VSAPIError
from portal.vidispine.igeneral import performVSAPICall
from portal.items.cache import invalidate_item_cache
from portal.utils.templatetags.vidispinetags import (
    getJobStatusLabel,
    getJobTypeLabel,
)
from portal.utils.templatetags.datetimeformatting import datetimeobject
from portal.plugins.TapelessIngest.helpers import (
    TapelessIngestHelper,
    TapelessIngestException,
)

from portal.plugins.TapelessIngest.models.settings import (
    Settings,
    MetadataMapping,
)

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


class ItemAPIEnhanced(ItemAPI):
    def getItemShapeIdsFromNames(
        self,
        item_id,
        shape_names,
        runasuser=None,
        return_format="json",
        placeholder=False,
    ):
        rest_url = RestURL("%sAPI/item/%s/shape" % (self.vsapi.super_url, item_id))
        query = {"tag": shape_names}
        if placeholder:
            query["placeholder"] = "true"
        rest_url.addQuery(query)
        param_dict = prepare_request(
            (self.vsapi.base64string),
            (rest_url.geturl()),
            runasuser=runasuser,
            return_format=return_format,
        )
        result = perform_request(**param_dict)
        if result:
            if return_format == "json":
                result = json.loads(result)
        return result


class ItemHelperExtended(ItemHelper):
    def provideItemAPI(self):
        if not hasattr(self, "itemapi"):
            self.itemapi = ItemAPIEnhanced(self._vsapi)

    def getItemShapesFromNames(self, item_id, shape_names, placeholder=False):
        """
        Get a list of item shapes, give a list of shape names

        Args:
            * item_id = The item ID
            * shape_names = A comma separated list of shape names

        Returns:
            * A list of VSShape objects representing the shapes
        """
        try:
            sr = self.itemapi.getItemShapeIdsFromNames(
                item_id, shape_names, runasuser=(self.runas), placeholder=placeholder
            )
            shapes = []
            for shape_id in sr.get("uri", []):
                res = self.itemapi.getItemShape(
                    item_id=item_id, shape_id=shape_id, runasuser=(self.runas)
                )
                shapes.append(VSShape(res, settings.VIDISPINE_REPLACE_URLS))

            return shapes
        except RestAPIBaseComError as e:
            handleRestAPIError(e)
        except Exception as e:
            log.error(("Couldn't get shape ids, reason: %s" % str(e)), exc_info=True)
            raise


class Reel(models.Model):
    umid = models.CharField(primary_key=True, max_length=100)
    created_on = models.DateTimeField(auto_now=True)
    folder_path = models.TextField()
    media_xml = models.TextField()


class Clip(models.Model):
    NOT_IMPORTED = "Not imported"
    WRAPPED = "Wrapped"
    REGISTERED = "Registered"
    PLACHOLDER_CREATED = "Placeholder created"
    IMPORTED = "Imported"
    STATUS_NOT_IMPORTED = 0
    STATUS_WRAPPED = 1
    STATUS_REGISTERED = 2
    STATUS_PLACHOLDER_CREATED = 3
    STATUS_IMPORTED = 4
    STATUS = {
        STATUS_NOT_IMPORTED: NOT_IMPORTED,
        STATUS_WRAPPED: WRAPPED,
        STATUS_REGISTERED: REGISTERED,
        STATUS_PLACHOLDER_CREATED: PLACHOLDER_CREATED,
        STATUS_IMPORTED: IMPORTED,
    }

    umid = models.CharField(primary_key=True, max_length=100)
    created_on = models.DateTimeField(auto_now=True)
    imported_on = models.DateTimeField(null=True)
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    folders = models.ManyToManyField("Folder", null=True)
    folder_path = models.TextField()
    path = models.TextField()
    storage_id = models.CharField(max_length=255, null=True)
    output_file = models.TextField(null=True)
    file_id = models.CharField(max_length=255, null=True)
    reference_file = models.CharField(max_length=255, null=False)
    status = models.IntegerField(blank=True, default=0)
    progress = models.CharField(max_length=255, null=True)
    spanned = models.BooleanField(blank=True, default=False)
    spanned_order = models.IntegerField(blank=True, default=0)
    spanned_id = models.CharField(max_length=100, null=True)
    master_clip = models.BooleanField(default=False)
    provider_name = models.CharField(max_length=100, db_column="provider")
    collection_id = models.TextField(null=True)
    item_id = models.CharField(max_length=10, null=True, blank=True)
    job_id = models.CharField(max_length=10, null=True, blank=True)
    clip_xml = models.TextField()
    reel = models.ForeignKey(Reel, null=True, on_delete=models.CASCADE)

    def __str__(self):
        if self.umid:
            return self.umid
        elif self.absolute_path:
            return self.absolute_path
        else:
            return "Unknown clip"

    def __init__(self, *args, **kwargs):
        if "metadatas" in kwargs:
            metadatas = kwargs.pop("metadatas")
            self._metadatas = metadatas
        super(Clip, self).__init__(*args, **kwargs)

    @classmethod
    def get_provider_by_name(cls, provider_name, clip=None):
        moduleName = f"portal.plugins.TapelessIngest.providers.{provider_name}"
        className = "Provider"
        module = __import__(moduleName, {}, {}, className)
        Provider = getattr(module, className)()
        return Provider

    @classmethod
    def _get_provider_list(cls, providers=None):
        filtered_providers = []
        if providers is None:
            providers = PROVIDERS_LIST
        for name in providers:
            Provider = cls.get_provider_by_name(name)
            filtered_providers.append(Provider)
        return filtered_providers

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

    @classmethod
    def get_clip_from_file(
        cls, file, provider_list=None, context=None, legacy_storages=[]
    ):
        if provider_list is None:
            provider_list = cls._get_provider_list()
        if context is None:
            context = {}
        metadatas = {}
        for provider in provider_list:
            provider_metadatas, context = provider.getMetadatasFromFile(
                file, metadatas, context
            )
            if "provider" not in metadatas.keys():
                continue
            if "umid" not in metadatas.keys():
                continue
            metadatas = provider_metadatas
        if "umid" not in metadatas.keys():
            raise TapelessIngestException("No UMID found in file %s" % file.getPath())
        umid = metadatas["umid"]
        item_id = None
        # Try to recover from file hash
        sh = StorageHelper()
        hash = file.getHash()
        for legacy_storage in legacy_storages:
            results = sh.storageapi.getFilesInStorage(
                legacy_storage, query={"hash": [hash], "includeItem": "true"}
            )
            if results["hits"] > 0:
                # We found a file with same hash
                existing_file = results["file"][0]
                if "item" in existing_file.keys() and len(existing_file["item"]) > 0:
                    item_id = existing_file["item"][0]["id"]
                    break
        clip, created = cls.get_or_new(
            umid=umid,
            defaults={
                "path": os.path.dirname(file.getPath()),
                "storage_id": file.getStorage(),
                "spanned": False,
                "item_id": item_id,
            },
        )
        clip.provider_name = metadatas["provider"]
        clip.metadatas = metadatas
        clip.file = file
        clip.reference_file = file.getId()

        return clip, context, created

    @classmethod
    def get_clip_from_item(self, item, provider=None):
        clip, created = self.get_or_new(
            item_id=item.getId(),
            defaults={
                "path": os.path.dirname(item.getPath()),
                "storage_id": item.getStorage(),
                "spanned": False,
            },
        )
        pass

    def get_storage_helper(self):
        if not hasattr(self, "_sth"):
            self._sth = StorageHelper(slug=self.storage_id)
        return self._sth

    def get_spanned_clips(self):
        if self.spanned:
            return self.spanned_clips.all()
        else:
            return []

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

    @property
    def ingest_base_path(self):
        ti_settings = Settings.objects.get(pk=1)
        return ti_settings.base_folder

    @property
    def file(self):
        if not hasattr(self, "_file"):
            if self.file_id is None:
                return None
            _sth = StorageHelper()
            try:
                self._file = _sth.getFileById(self.file_id)
            except NotFoundError:
                return None
        return self._file

    @file.setter
    def file(self, file):
        self._file = file
        self.file_id = file.getId()

    @property
    def provider(self):
        if not hasattr(self, "_provider"):
            self.provider = self.__class__.get_provider_by_name(
                self.provider_name, clip=self
            )
            self._provider.MetadataMappingModel = MetadataMapping
        return self._provider

    @provider.setter
    def provider(self, Provider):
        self._provider = Provider
        self.provider_name = Provider.machine_name

    @property
    def xml(self):
        if not hasattr(self, "_xml"):
            if os.path.splitext(self.reference_file)[1] not in [".xml", ".XML"]:
                return None
            xml_file = os.path.join(self.folder_path, self.reference_file)
            if not os.path.isfile(xml_file):
                return None
            self._xml = self.provider.parseXML(xml_file)
        return self._xml

    @xml.setter
    def xml(self, value):
        self._xml = value

    @property
    def metadatas(self):
        if not hasattr(self, "_metadatas"):
            self._metadatas = {}
            # First we try to get the metadatas from db
            for metadata in self.clipmetadata_set.all():
                self._metadatas[metadata.name] = metadata.value
        return self._metadatas

    @metadatas.setter
    def metadatas(self, new_metadatas):
        self._metadatas = new_metadatas
        if not self._state.adding:
            # Then we update the database
            for key, value in new_metadatas.items():
                self.clipmetadata_set.update_or_create(
                    name=key, defaults={"value": value}
                )

    @property
    def media_files(self):
        if not hasattr(self, "_media_files"):
            self._media_files = self.provider.getClipMediaFiles(self)
        return self._media_files

    @property
    def spanned_clips(self):
        if not hasattr(self, "_spanned_clips"):
            self._spanned_clips = self.provider.getSpannedClips(self)
        return self._spanned_clips

    @property
    def item(self):
        if not hasattr(self, "_item"):
            if self.item_id is None:
                return None
            _ith = ItemHelper()
            try:
                self._item = _ith.getItem(self.item_id)
            except NotFoundError:
                return None
        return self._item

    @item.setter
    def item(self, item):
        if item:
            self._item = item
            self.item_id = item.getId()

    @property
    def job(self):
        if not hasattr(self, "_job"):
            if self.job_id is None:
                return None
            _ijh = JobHelper()
            try:
                self._job = _ijh.getJob(self.job_id)
            except NotFoundError:
                return None
        return self._job

    @job.setter
    def job(self, job):
        self._job = job
        self.job_id = job.getId()

    @property
    def collections(self):
        if not hasattr(self, "_collections"):
            if self.collection_id is None:
                return None
            ch = CollectionHelper()
            try:
                collections = self.collection_id.split(",")
                for collection in collections:
                    self._collections.append(ch.getCollection(self.collection_id))
            except NotFoundError:
                return None
        return self._collections

    def get_thumbnail_url(self):
        return reverse("clip_thumbnail", None, [str(self.umid)])

    def get_state(self):
        if hasattr(self, "jobs"):
            if self.jobs.status == "Ingest finished":
                return "IMPORTED"
            elif self.jobs.status == "Ingest failed":
                return "FAILED"
            else:
                return "PROCESSING"
        else:
            if self.status is self.STATUS_IMPORTED:
                return "IMPORTED"
            else:
                return "NOTIMPORTED"

    @property
    def error(self):
        if hasattr(self, "error"):
            return self.error
        else:
            return ""

    @error.setter
    def error(self, error):
        self.error = error

    def get_readable_duration(self):
        from portal.plugins.TapelessIngest.templatetags.tapelessingest_extras import (
            frame_to_time,
        )

        total_duration = 0
        if self.spanned and self.master_clip:
            clips_ids = []
            spanned_clips = self.spanned_clips.all()
            for spanned_clip in spanned_clips:
                clips_ids.append(spanned_clip.clip.umid)
            durations = ClipMetadata.objects.filter(clip__in=clips_ids, name="duration")
            for duration in durations:
                total_duration += int(float(duration.value))
        else:
            if "duration" in self.metadatas and self.metadatas["duration"] is not None:
                total_duration = int(float(self.metadatas["duration"]))
        return frame_to_time(total_duration)

    def get_readable_status(self):
        return self.STATUS[self.status]

    def get_absolute_url(self):
        # from django.urls import reverse
        return ""
        # return reverse("clips-detail", args=[str(self.umid)])

    def get_resource_uri(self):
        # from django.urls import reverse
        return ""
        # return reverse("clips-detail", args=[str(self.umid)])

    def get_related_jobs(self):
        if self.item_id:
            # check if there is vidispine jobs related
            jh = JobHelper()
            _jobs = jh.getAllJobsForItem(self.item_id)
            _pretty_jobs = []
            for _j in _jobs:
                joblink = reverse("vs_job", kwargs={"slug": _j.getId()})
                _pretty_jobs.append(
                    {
                        "id": _j.getId(),
                        "type": getJobTypeLabel(None, _j.getType()),
                        "state": getJobStatusLabel(None, _j.getStatus()),
                        "rawstatus": _j.getStatus(),
                        "user": _j.getUser(),
                        "startTime": format_datetime(
                            datetimeobject(_j.getStarted())
                        ).replace(" ", "&nbsp"),
                        "targetitem": _j.getTargetItem(),
                        "joblink": joblink,
                        "in_progress": _j.inProgress(),
                        "priority": _j.getPriority(),
                        "filename": _j.getFilename(),
                        "transcodeProgress": _j.getTranscodeProgress(),
                        "sourceFilePath": _j.getSourceFilePath(),
                    }
                )

            return self.json_response(
                {
                    "jobs": _pretty_jobs,
                },
                200,
            )
        else:
            return False

    def __unicode__(self):
        return "%s" % self.umid

    # Create a new item with metadatas
    def create_item(
        self,
        user=None,
        collection_id=None,
        replace=False,
        metadatagroupname="Film",
        gh=GroupHelper(),
        uh=UserHelper(),
        ith=ItemHelper(),
        ch=CollectionHelper(),
    ):
        created = False

        ingestgroups, default_ingest_group = gh.getUserIngestGroups()
        ingestgroupname = default_ingest_group.name

        log.debug("found %s clip_metadatas associated" % len(self.metadatas))

        _metadata = self.provider._createDictFromMetadataMapping(self)

        settingsprofile_id = uh.getUserSettingsProfile(basegroup=ingestgroupname)

        md = createMetadataDocumentFromDict(_metadata, [metadatagroupname])

        if self.item is None:
            # If item doesn't exist, we create it
            created = True
            log.info("Creating placeholder...")
            self.item = ith.createPlaceholder(md, settingsprofile_id=settingsprofile_id)
        else:
            # If item already exists, we check if we have to replace it
            if replace:
                # If we have to replace it, we merge existing metadatas with new ones
                custom_metadata = self.item.getMetadata()[0]
                md_to_set = createMergedBatchItemMetadataDocument(
                    md, custom_metadata, "replace"
                )
                ith.setItemMetadata(self.item_id, metadata_document=md_to_set)
                log.info("Item already exists, replacing new metadatas")
            else:
                # If we don't have to replace it, we simply return existing item
                return self.item, created

        ith.setItemMetadataFieldGroup(self.item_id, metadatagroupname)

        log.info("Placeholder creation done (id=%s)" % self.item_id)

        # Add item to collection is not neccessary anymore as there's a plugin which will do it
        """
        if collection_id is None:
            collection_id = TapelessIngestHelper.get_collection_from_path(
                self.path, user
            )
            if collection_id:
                log.info(
                    f"Created collection {collection_id} from path {self.path}, adding item {self.item_id} to it"
                )
                ch.addItemToCollection(collection_id, self.item_id)
        else:
            log.info(
                f"Found collection {collection_id} from path {self.path}, adding item {self.item_id} to it"
            )
            ch.addItemToCollection(collection_id, self.item_id)
        """
        if collection_id:
            log.info(
                f"Found collection {collection_id} from path {self.path}, adding item {self.item_id} to it"
            )
            ch.addItemToCollection(collection_id, self.item_id)
        return self.item, created

    # Attempt to import file to placeholder
    def import_file(
        self, collection_id=None, user=None, replace=False, legacy_storages=[]
    ):
        # self.item = self.provider.getAvailableItem(self)
        result = {
            "skipped": False,
            "failed": False,
            "replaced": False,
            "ingested": False,
        }

        _igh = TapelessIngestHelper(runas=user)
        _ijh = JobHelper(runas=user)
        _ith = ItemHelperExtended(runas=user)
        _gh = GroupHelper(runas=user)
        _ch = CollectionHelper(runas=user)
        _sh = StorageHelper(runas=user)

        # Create item if it doesn't exist
        item, created = self.create_item(
            user=user,
            collection_id=collection_id,
            replace=replace,
            ith=_ith,
            gh=_gh,
            ch=_ch,
        )
        if not replace and not created:
            # Item already exists and we don't have to replace it, skip import
            log.info("Item already exists, skipping it")
            result["skipped"] = True
            return result

        if self.item is not None:
            if not self.file:
                # If we don't have a file to replace
                log.info(f"Importing {self.item_id}: No file to replace, skipping it")
                result["skipped"] = True
                return result
            # We get all original shapes
            original_shapes = _ith.getItemShapesFromNames(self.item_id, ["original"])

            for original_shape in original_shapes:
                original_files = original_shape.getAllFiles()
                if len(original_files) >= 0 and replace == False:
                    log.info(
                        f"Importing {self.item_id}: Item already exists and has an original file, skipping it"
                    )
                    result["skipped"] = True
                    return result
                # If we have a file to replace but it is not in original files
                if self.file.getId() in [f.getId() for f in original_files]:
                    log.info(
                        f"Importing {self.item_id}: File is already in original files, skipping it"
                    )
                    result["skipped"] = True
                    return result
                # If we have a file to replace and it is not in original files
                if self.file.getStorage() in [f.getStorage() for f in original_files]:
                    log.info(
                        f"Importing {self.item_id}: File is on same storage, skipping it"
                    )
                    result["skipped"] = True
                    return result
                # if any of original files is on legacy storage, we don't replace it
                if any(f.getStorage() in legacy_storages for f in original_files):
                    log.info(
                        f"Importing {self.item_id}: File is on legacy storage, skipping it"
                    )
                    result["skipped"] = True
                    return result
                for _file in original_files:
                    log.info(
                        f"Importing {self.item_id}: Removing file {_file.getId()} from original shape"
                    )
                    _sh.removeFileItemRelationship(_file.getStorage(), _file.getId())
                log.info(
                    f"Importing {self.item_id}: Removing original shape {original_shape.getId()}"
                )
                _ith.itemapi.removeItemShape(
                    self.item_id, (original_shape.getId()), runasuser=user
                )

                result["replaced"] = True

        self.user = user
        self.status = self.STATUS_PLACHOLDER_CREATED

        # if self.provider.isSpanned(self):
        #     if self.provider.isSpannedClipComplete(self):
        #         log.info(f"Importing {self.item_id}: Spanned clip is complete, importing it")

        # files = self.provider.getClipMediaFiles(self)
        extraFiles = self.provider.getClipAdditionalMediaFiles(self)
        main_file = self.provider.getClipMainMediaFile(self)
        options = self.provider.getImportOptions()

        main_file_id = main_file["file_id"]

        notification_id = None

        noTranscode = options.get("no-transcode", None)
        if result["replaced"]:
            # if we have only replaced original file, that there is a lowres shape already
            noTranscode = True

        ingestgroups, default_ingest_group = _gh.getUserIngestGroups()
        user_groups = [urllib.parse.quote(str(default_ingest_group))]

        original_shapes = _ith.getItemShapesFromNames(
            self.item_id, ["original"], placeholder=True
        )
        if original_shapes is None or len(original_shapes) == 0:
            log.info(f"Importing {self.item_id}: No original shape found, creating one")
            response = _ith.itemapi.createPlaceholderShape(self.item_id, runasuser=user)
            _shape_id = response.decode("UTF-8")
        else:
            _shape = original_shapes[0]
            if len(_shape.getAllFiles()) > 0:
                log.info(f"Importing {self.item_id}: Shape is not a placeholder")
                result["failed"] = True
                return result
            _shape_id = _shape.getId()

        if len(extraFiles) == 0:
            log.info(
                f"Importing {self.item_id}: Start importing single-component shape"
            )
            res = _igh.importFileToPlaceholder(
                self.item_id,
                file_id=main_file_id,
                ingestprofile_groups=user_groups,
                notification_id=notification_id,
                noTranscode=noTranscode,
                ignore_sidecars=True,
            )
            if "jobId" in res:
                self.job = _ijh.getJob(res["jobId"])
                result["ingested"] = True
                return result
        else:
            log.info(f"Importing {self.item_id}: Start importing multi-component shape")
            _query = {
                "fileId": main_file_id,
                "tag": "lowres",
            }
            if noTranscode:
                _query["no-transcode"] = noTranscode
            allFiles = extraFiles + [main_file]
            audio_file_count = sum(file["type"] == "audio" for file in allFiles)
            video_file_count = sum(file["type"] == "video" for file in allFiles)
            if audio_file_count == 0:
                audio_file_count = None
            if video_file_count == 0:
                video_file_count = None

            if _shape_id:
                log.info(
                    f"Importing {self.item_id}: Found shape {_shape_id}, updating components count to {video_file_count} video components and {audio_file_count} audio component"
                )
                _ith.itemapi.updatePlaceholderComponentCount(
                    self.item_id,
                    _shape_id,
                    container=1,
                    video=video_file_count,
                    audio=audio_file_count,
                )
            signals.vidispine_pre_ingest.send(
                sender=(ItemHelper),
                instance=self.item_id,
                method="importFileToPlaceholder",
                query=_query,
            )
            for extraFile in extraFiles:
                if extraFile["type"] in ["audio", "video"]:
                    q = {"fileId": extraFile["file_id"], "tag": "lowres"}
                    log.info(
                        f"Importing {self.item_id}: Import file {extraFile['file_id']}:{extraFile['path']} to component {extraFile['type']}..."
                    )
                    component_res = _ith.itemapi.doImportToPlaceholder(
                        item_id=self.item_id,
                        query=q,
                        component=extraFile["type"],
                        runasuser=user,
                        ignore_sidecars=True,
                        ingestprofile_groups=user_groups,
                    )
                    if "jobId" in component_res:
                        log.info(f"... and got job {component_res['jobId']}")
                    else:
                        log.info("... but got no job in response")
            log.info(
                f"Finally, import file {main_file_id} to item {self.item_id}...(user groups are {user_groups})"
            )
            res = _ith.itemapi.doImportToPlaceholder(
                item_id=self.item_id,
                query=_query,
                runasuser=user,
                ignore_sidecars=True,
                ingestprofile_groups=user_groups,
            )
            invalidate_item_cache(self.item_id)
            signals.vidispine_post_ingest.send(
                sender=(ItemHelper),
                instance=self.item_id,
                method="importFileToPlaceholder",
            )
            if "jobId" in res:
                self.job = _ijh.getJob(res["jobId"])
            log.info(
                f"Retranscoding shape with item {self.item_id} and shape {_shape_id}"
            )
        result["ingested"] = True
        return result

    def ingest(
        self,
        collection_id=None,
        user=None,
        folder=None,
        replace=False,
        legacy_storages=[],
    ):
        result = self.import_file(
            user=user,
            collection_id=collection_id,
            replace=replace,
            legacy_storages=legacy_storages,
        )
        self.save()
        if folder:
            self.folders.add(folder)
        return result

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if hasattr(self, "xml") and self.xml is not None:
            self.clip_xml = self.xml.tostring()

        if hasattr(self, "_metadatas"):
            for name, value in self._metadatas.items():
                ClipMetadata.objects.update_or_create(
                    clip=self, name=name, defaults={"value": value}
                )
            ClipMetadata.objects.filter(clip=self).exclude(
                name__in=self._metadatas.keys()
            ).delete()


class ClipMetadata(models.Model):
    clip = models.ForeignKey(Clip, max_length=100, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    value = models.CharField(max_length=200, blank=True, null=True, default="")

    class Meta:
        unique_together = (("clip", "name"),)

    def __unicode__(self):
        return "%s" % self.value


class ClipFile(models.Model):
    clip = models.ForeignKey(Clip, max_length=100, on_delete=models.CASCADE)
    path = models.TextField()
    filetype = models.CharField(max_length=100)
    order = models.IntegerField(blank=True, default=0)

    class Meta:
        unique_together = (("clip", "path"),)


class SpannedClips(models.Model):
    master_clip = models.ForeignKey(
        Clip,
        related_name="spanned_clips",
        max_length=100,
        on_delete=models.CASCADE,
    )
    clip = models.ForeignKey(Clip, max_length=100, on_delete=models.CASCADE)
    order = models.IntegerField(blank=True, default=0)

    class Meta:
        unique_together = (("master_clip", "order"),)
        ordering = ["order"]

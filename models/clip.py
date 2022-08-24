import logging

import os

from django.urls import reverse
from django.db import models
from django.contrib.auth.models import User

from portal.api.v2.utils import format_datetime
from portal.vidispine.ijob import JobHelper
from portal.vidispine.iitem import ItemHelper, IngestHelper
from portal.vidispine.icollection import CollectionHelper
from portal.vidispine.istorage import StorageHelper
from portal.vidispine.iexception import NotFoundError
from portal.items.cache import invalidate_item_cache
from portal.utils.templatetags.vidispinetags import (
    getJobStatusLabel,
    getJobTypeLabel,
)
from portal.utils.templatetags.datetimeformatting import datetimeobject
from VidiRest.helpers.vidispine import createMetadataDocumentFromDict

from portal.plugins.TapelessIngest.helpers import TapelessIngestHelper

from portal.plugins.TapelessIngest.models.settings import (
    Settings,
    MetadataMapping,
)

log = logging.getLogger(__name__)


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

    def __init__(self, *args, **kwargs):
        if "metadatas" in kwargs:
            metadatas = kwargs.pop("metadatas")
            self._metadatas = metadatas
        super(Clip, self).__init__(*args, **kwargs)

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
            self._provider = TapelessIngestHelper.get_provider_by_name(
                self.provider_name
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
                    self._collections.append(
                        ch.getCollection(self.collection_id)
                    )
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

    def get_error(self):
        if hasattr(self, "error"):
            return self.error
        else:
            return ""

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
            durations = ClipMetadata.objects.filter(
                clip__in=clips_ids, name="duration"
            )
            for duration in durations:
                total_duration += int(float(duration.value))
        else:
            if (
                "duration" in self.metadatas
                and self.metadatas["duration"] is not None
            ):
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

    def create_placeholder(self, collection_id=None, user=None):

        _ith = ItemHelper(runas=user)
        _ch = CollectionHelper(runas=user)

        if self.item is not None:
            return self.item

        metadatagroupname = "Film"

        log.debug("found %s clip_metadatas associated" % len(self.metadatas))

        _metadata = self.provider._createDictFromMetadataMapping(self)

        md = createMetadataDocumentFromDict(_metadata, [metadatagroupname])
        self.item = _ith.createPlaceholder(md)

        log.debug("Placeholder creation done (id=%s)" % self.item_id)

        if collection_id is None:
            collection_id = TapelessIngestHelper.get_collection_from_path(
                self.path, user
            )
            if collection_id:
                _ch.addItemToCollection(collection_id, self.item_id)
        else:
            _ch.addItemToCollection(collection_id, self.item_id)

        self.user = user
        self.status = self.STATUS_PLACHOLDER_CREATED

        return self.item

    def import_files_to_placeholder(self, user=None, replace=False):
        _ith = ItemHelper(runas=user)
        _igh = IngestHelper(runas=user)
        _ijh = JobHelper(runas=user)

        files = self.provider.getClipMediaFiles(self)
        options = self.provider.getImportOptions()

        shapes = self.provider.getAdditionalShapesToImport(self)

        noTranscode = options.get("no-trasncode", False)

        # Check how much video and audio files are there.
        media_file_count = sum(
            file["type"] in ["video", "audio"] for file in files
        )

        if media_file_count == 1:
            file = files[0]
            tag = "lowres"
            if file["type"] == "audio":
                tag = "lowaudio"
            if file["type"] == "image":
                tag = "lowimage"
            res = _igh.importFileToPlaceholder(
                self.item_id,
                file_id=file["file_id"],
                ingestprofile_groups=["Admin"],
                tags=[tag],
                ignore_sidecars=True,
                noTranscode=noTranscode,
            )
            if "jobId" in res:
                self.job = _ijh.getJob(res["jobId"])
        else:
            shape_json = {
                "tag": ["original"],
                "containerComponent": {},
                "audioComponent": [],
                "binaryComponent": [],
            }
            log.info(
                f"Start importing multi-component shape on item {self.item_id}"
            )
            audio_file_count = sum(file["type"] == "audio" for file in files)
            video_file_count = sum(file["type"] == "video" for file in files)
            log.info(
                f"Found {video_file_count} video components"
                " and {audio_file_count} audio component"
            )
            _item = _ith.getItem(self.item_id)
            _shape_id = _item.getMetadataFieldValueByName("__shape")
            if _shape_id:
                log.info(f"Found shape {_shape_id}")
                _ith.itemapi.updatePlaceholderComponentCount(
                    self.item_id,
                    _shape_id,
                    container=1,
                    video=video_file_count,
                    audio=audio_file_count,
                )

            for file in files:
                if file["type"] == "video":
                    shape_json["containerComponent"] = {
                        "file": [{"id": file["file_id"]}]
                    }
                if file["type"] == "audio":
                    q = {"fileId": file["file_id"]}
                    _ith.itemapi.doImportToPlaceholder(
                        item_id=self.item_id,
                        query=q,
                        component=file["type"],
                        runasuser=user,
                    )
                    shape_json["audioComponent"].append(
                        {"file": [{"id": file["file_id"]}]}
                    )
                """
                if file["type"] == "metadatas":
                    shape_json["binaryComponent"].append(
                        {"file": [{"id": file["file_id"]}]}
                    )
                """
            _query = {
                "fileId": files[0]["file_id"],
                "no-transcode": noTranscode,
                "tag": "lowres",
                "jobmetadata": "ignoreSidecar:String%3dtrue",
            }
            res = _ith.itemapi.doImportToPlaceholder(
                item_id=self.item_id, query=_query, runasuser=user
            )
            invalidate_item_cache(self.item_id)
            if "jobId" in res:
                self.job = _ijh.getJob(res["jobId"])
            log.info(
                f"Retranscoding shape with item {self.item_id} and shape {_shape_id}"
            )
            """
            if shapes:
                for shape in shapes:
                    _igh.importNewShape(
                        self.item_id, file_id=shape["fileId"], tag=shape["tag"]
                    )
            """

    def ingest(self, collection_id=None, user=None, folder=None):
        if not self.item:
            self.create_placeholder(user=user, collection_id=collection_id)
            self.import_files_to_placeholder(user)
            self.save()
        self.folders.add(folder)
        return self.item

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if hasattr(self, "xml") and self.xml is not None:
            self.clip_xml = self.xml.tostring()

        if hasattr(self, "_media_files"):
            self.clipfile_set.all().delete()
            for media_file in self._media_files:
                media_file.save()

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

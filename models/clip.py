import logging
from typing import Optional, Dict, List, Tuple, Any

import os
import urllib
import pyxb.utils, simplejson as json
from django.urls import reverse
from django.db import models
from django.contrib.auth.models import User
from django.conf import settings
from django.core.cache import cache

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
from portal.plugins.TapelessIngest.metadatas import XMLParser
from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
from portal.plugins.TapelessIngest.scan.context import browse_root_path
from portal.plugins.TapelessIngest.scan.extraction import extract_metadatas
from portal.plugins.TapelessIngest.scan.persistence import (
    MetadataWrite,
    group_stale_deletes,
)

log = logging.getLogger(__name__)

# Alias over the canonical membership tuple. Registry order is load
# bearing: it decides which provider claims a file when several are
# applicable. See docs/adding-a-provider.md.
PROVIDERS_LIST = list(PROVIDER_NAMES)


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

    # Class-level provider cache for performance
    _PROVIDER_CACHE: Dict[str, Any] = {}

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
    status = models.IntegerField(blank=True, default=STATUS_NOT_IMPORTED)
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
    def get_provider_by_name(
        cls, provider_name: str, clip: Optional["Clip"] = None
    ) -> Any:
        """Dynamically load and instantiate a provider by name with caching.

        Args:
            provider_name: Name of the provider module (e.g., 'red', 'xdcam', 'p2')
            clip: Optional clip instance to associate with the provider

        Returns:
            Provider instance for the specified camera format
        """
        # Check cache first
        if provider_name in cls._PROVIDER_CACHE:
            return cls._PROVIDER_CACHE[provider_name]

        # Load and instantiate provider
        moduleName = f"portal.plugins.TapelessIngest.providers.{provider_name}"
        className = "Provider"
        module = __import__(moduleName, {}, {}, className)
        Provider = getattr(module, className)()

        # Cache the instance
        cls._PROVIDER_CACHE[provider_name] = Provider
        return Provider

    @classmethod
    def _get_provider_list(cls, providers: Optional[List[str]] = None) -> List[Any]:
        """Get list of provider instances from provider names.

        Args:
            providers: Optional list of provider names. If None, uses all configured providers

        Returns:
            List of provider instances
        """
        filtered_providers = []
        if providers is None:
            providers = PROVIDERS_LIST
        for name in providers:
            Provider = cls.get_provider_by_name(name)
            filtered_providers.append(Provider)
        return filtered_providers

    @classmethod
    def get_or_new(
        cls, defaults: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> Tuple["Clip", bool]:
        """Get existing clip or create a new one without saving to database.

        Args:
            defaults: Optional dictionary of default values for new clip
            **kwargs: Query parameters to find existing clip

        Returns:
            Tuple of (clip instance, is_new flag) where is_new is True if clip was created
        """
        try:
            return cls.objects.get(**kwargs), False
        except cls.DoesNotExist:
            defaults = defaults or {}
            params = {k: v for k, v in kwargs.items()}
            params.update(defaults)
            # Try to create an object using passed params.
            return cls(**params), True

    @classmethod
    def extract_file_metadatas(
        cls,
        file: Any,
        provider_list: Optional[List[Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        legacy_storages: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Pass 1 of the scan's two-pass lookup: everything before the DB.

        Providers, identity validation and hash recovery — the whole
        pre-2.4 head of ``get_clip_from_file``, byte-for-byte in the same
        order, so a file that raised "No UMID"/"No hash" still raises it
        at the same point with the same message. Split out so a page's
        umids can be collected first and looked up in ONE query (AD-6)
        instead of one ``get`` per file.

        Hash recovery is deliberately left per-file here — story 2.5 owns
        the Vidispine call discipline.

        Args:
            file: VSFile instance to extract metadata from
            provider_list: Optional list of provider instances. If None, uses all providers
            context: Optional context dictionary shared across provider calls; it is
                mutated in place by the providers, never replaced
            legacy_storages: Optional list of legacy storage IDs to check for existing items

        Returns:
            Tuple of (metadatas dict, recovered item_id or None)

        Raises:
            TapelessIngestException: If no UMID or hash found in file
        """
        if provider_list is None:
            provider_list = cls._get_provider_list()
        if context is None:
            context = {}
        # `provider_list` is already pre-filtered to the applicable
        # providers by the caller; every one of them runs and merges.
        metadatas = extract_metadatas(file, provider_list, {}, context)
        if "umid" not in metadatas.keys():
            raise TapelessIngestException("No UMID found in file %s" % file.getPath())
        item_id = None
        # Try to recover from file hash
        sh = StorageHelper()
        hash = file.getHash()
        if not hash:
            raise TapelessIngestException("No hash found in file %s" % file.getPath())
        if legacy_storages:
            for legacy_storage in legacy_storages:
                try:
                    results = sh.storageapi.getFilesInStorage(
                        legacy_storage, query={"hash": [hash], "includeItem": "true"}
                    )
                except Exception as e:
                    log.error(
                        f"Error getting files with hash {hash} in storage {legacy_storage}: {e}"
                    )
                    continue
                if results["hits"] > 0:
                    # We found a file with same hash
                    existing_file = results["file"][0]
                    if (
                        "item" in existing_file.keys()
                        and len(existing_file["item"]) > 0
                    ):
                        item_id = existing_file["item"][0]["id"]
                        break
        return metadatas, item_id

    @classmethod
    def new_clip_defaults(
        cls, file: Any, metadatas: Dict[str, Any], item_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """The column values a NEVER-SEEN clip is created with.

        The one copy of the new-row shape: both the per-file
        ``get_or_new`` path and the scan's batched path build a new clip
        from exactly this dict, so the two can never drift. Note the
        recovered ``item_id`` lands here, i.e. on the insert path only —
        an existing clip's ingest state is never rewritten by a scan.
        """
        return {
            "umid": metadatas["umid"],
            "path": os.path.dirname(file.getPath()),
            "storage_id": file.getStorage(),
            "spanned": False,
            "item_id": item_id,
        }

    def attach_file_metadatas(self, file: Any, metadatas: Dict[str, Any]) -> "Clip":
        """Pass 2 decoration: bind the scanned file and its metadatas.

        Memo-only for ``metadatas`` since 2.4 — the setter no longer
        writes rows, the folder's write unit does, once, in batch.
        ``provider_name``/``file_id``/``reference_file`` are set on the
        instance exactly as before; for an EXISTING clip they still do
        not reach the DB during a scan (see ``CLIP_UPDATE_FIELDS``).
        """
        self.provider_name = metadatas["provider"]
        self.metadatas = metadatas
        self.file = file
        self.reference_file = file.getId()
        return self

    @classmethod
    def get_clip_from_file(
        cls,
        file: Any,
        provider_list: Optional[List[Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        legacy_storages: Optional[List[str]] = None,
    ) -> Tuple["Clip", bool]:
        """Extract clip metadata from file and get or create clip instance.

        The per-file composition of the two passes, kept for callers
        outside the batched scan path (one lookup query per file).

        Args:
            file: VSFile instance to extract metadata from
            provider_list: Optional list of provider instances. If None, uses all providers
            context: Optional context dictionary shared across provider calls; it is
                mutated in place by the providers, never replaced
            legacy_storages: Optional list of legacy storage IDs to check for existing items

        Returns:
            Tuple of (clip instance, created flag)

        Raises:
            TapelessIngestException: If no UMID or hash found in file
        """
        metadatas, item_id = cls.extract_file_metadatas(
            file,
            provider_list=provider_list,
            context=context,
            legacy_storages=legacy_storages,
        )
        clip, created = cls.get_or_new(
            umid=metadatas["umid"],
            defaults=cls.new_clip_defaults(file, metadatas, item_id),
        )
        clip.attach_file_metadatas(file, metadatas)

        return clip, created

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

    def get_storage_helper(self) -> Any:
        """Get or create StorageHelper instance for this clip.

        Returns:
            StorageHelper instance for accessing Vidispine storage API
        """
        if not hasattr(self, "_sth"):
            self._sth = StorageHelper(slug=self.storage_id)
        return self._sth

    def get_spanned_clips(self) -> List["Clip"]:
        """Get all related spanned clips if this clip is part of a spanned set.

        Returns:
            List of spanned clip instances, or empty list if not spanned
        """
        if self.spanned:
            return self.spanned_clips.all()
        else:
            return []

    @property
    def storage(self) -> Optional[Any]:
        """Get Vidispine storage object for this clip with Redis caching.

        Returns:
            Storage object or None if storage_id is not set or not found
        """
        if not hasattr(self, "_storage"):
            if self.storage_id is None:
                return None

            # Try to get from cache first
            cache_key = f"storage:{self.storage_id}"
            cached_storage = cache.get(cache_key)
            if cached_storage is not None:
                self._storage = cached_storage
                return self._storage

            # If not in cache, fetch from API
            _sth = self.get_storage_helper()
            try:
                self._storage = _sth.getStorage(self.storage_id)
                # Cache for 5 minutes
                cache.set(cache_key, self._storage, 300)
            except NotFoundError:
                return None
        return self._storage

    @storage.setter
    def storage(self, storage: Any) -> None:
        """Set storage object and update storage_id.

        Args:
            storage: Vidispine storage object
        """
        self._storage = storage
        self.storage_id = storage.getId()

    @property
    def root_path(self) -> Optional[str]:
        """Get root path from storage browse URI.

        Returns:
            Root path string or None if not available
        """
        if not hasattr(self, "_root_path"):
            # Canonical block (scan/context.py); no resolvable root leaves
            # _root_path unassigned -> AttributeError, exactly as before.
            # Deliberate unification: first browse method wins now; the old
            # inline loop let the last one win (no prod storage has two).
            root_path = browse_root_path(self.storage)
            if root_path is not None:
                self._root_path = root_path
        return self._root_path

    @property
    def absolute_path(self):
        """Get absolute filesystem path by joining root_path and relative path.

        Returns:
            Absolute path string or False if root_path not available
        """
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

            # Try to get from cache first
            cache_key = f"file:{self.file_id}"
            cached_file = cache.get(cache_key)
            if cached_file is not None:
                self._file = cached_file
                return self._file

            # If not in cache, fetch from API
            _sth = StorageHelper()
            try:
                self._file = _sth.getFileById(self.file_id)
                # Cache for 5 minutes
                cache.set(cache_key, self._file, 300)
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
        """Parsed clip XML: memo, then the stored column, then the file.

        FR-12 read order. The pre-2.4 property went straight to the
        filesystem, and ``save()`` probed it on EVERY save — so a clip
        whose XML was already serialized in ``clip_xml`` was re-read and
        re-parsed from disk each time. A non-empty ``clip_xml`` is now
        authoritative and never re-parsed from the file.
        """
        if hasattr(self, "_xml"):
            return self._xml
        if self.clip_xml:
            try:
                self._xml = XMLParser.from_string(self.clip_xml)
            except Exception:
                # A corrupt stored column must not take a clip down; fall
                # through to the file, exactly as if nothing were stored.
                log.error(
                    f"Cannot parse the stored clip_xml of {self.umid}", exc_info=True
                )
            else:
                return self._xml
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

    def load_clip_xml(self, metadatas: Optional[Dict[str, Any]] = None) -> bool:
        """FR-12: serialize the provider's sidecar into ``clip_xml`` ONCE.

        Called by the scan write unit's plan-prep, before the row is
        inserted — the pre-2.4 ``save()`` override assigned ``clip_xml``
        AFTER ``super().save()``, so the serialized XML never reached the
        DB on a first save and the column was never read back.

        Only fills an empty column, and only from a ``clip_xml_file`` the
        providers already resolved; the sidecar is located WITHOUT
        touching Vidispine (the memoized root only), so a scan buys no
        extra HTTP call. A parse failure is swallowed and logged: an
        optional column must never cost a clip its ingest.

        Returns:
            True when the column was filled by this call.
        """
        if self.clip_xml:
            return False
        if metadatas is None:
            metadatas = getattr(self, "_metadatas", None) or {}
        sidecar = metadatas.get("clip_xml_file")
        if not sidecar:
            return False
        xml_file = self._sidecar_absolute_path(sidecar)
        if xml_file is None:
            return False
        try:
            parsed = self.provider.parseXML(xml_file)
            serialized = parsed.tostring()
        except Exception:
            log.error(
                f"Cannot parse the clip XML {xml_file} of {self.umid}", exc_info=True
            )
            return False
        if isinstance(serialized, bytes):
            # lxml serializes to bytes; the column is text. Decoding here
            # is what makes the stored value re-parseable by `xml` above.
            serialized = serialized.decode("utf-8", "replace")
        self.clip_xml = serialized
        self._xml = parsed
        return True

    def _sidecar_absolute_path(self, sidecar: str) -> Optional[str]:
        """Absolute path of a provider-declared ``clip_xml_file``.

        Providers declare it either absolute (panasonicP2, ikegami) or
        relative to the clip's own directory (xdcam's ``./Clip/...``
        form, and the MEDIAPRO URIs) — the same basename-against-the-clip
        resolution ``xdcam.getClipFiles`` uses. Resolution reads the
        MEMOIZED root only: no storage lookup, and no AttributeError from
        the pin-#5 ``root_path`` chain.
        """
        if os.path.isabs(sidecar):
            return sidecar
        root_path = getattr(self, "_root_path", None)
        if not root_path:
            return None
        return os.path.join(root_path, self.path, os.path.basename(sidecar))

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
        # Memo only since 2.4 (AD-6). Assigning metadatas used to fan out
        # one upsert query PER KEY for an already-saved clip, on top
        # of the identical fan-out in save(); persistence is now the
        # batched write unit's job — models/folder.persist_scan_results
        # for the scan path, persist_metadatas() for everyone else.
        self._metadatas = new_metadatas

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

            # Try to get from cache first
            cache_key = f"item:{self.item_id}"
            cached_item = cache.get(cache_key)
            if cached_item is not None:
                self._item = cached_item
                return self._item

            # If not in cache, fetch from API
            _ith = ItemHelper()
            try:
                self._item = _ith.getItem(self.item_id)
                # Cache for 3 minutes
                cache.set(cache_key, self._item, 180)
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
        return getattr(self, "_error", "")

    @error.setter
    def error(self, value):
        self._error = value

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
        return ""

    def get_resource_uri(self):
        return ""

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

    def create_item(
        self,
        user: Optional[User] = None,
        collection_id: Optional[str] = None,
        replace: bool = False,
        metadatagroupname: str = "Film",
        gh: Any = GroupHelper(),
        uh: Any = UserHelper(),
        ith: Any = ItemHelper(),
        ch: Any = CollectionHelper(),
    ) -> Tuple[Any, bool]:
        """Create a new Vidispine item with metadata or update existing item.

        Args:
            user: User performing the operation
            collection_id: Optional collection ID to add the item to
            replace: Whether to replace existing metadata if item exists
            metadatagroupname: Name of the metadata group to use
            gh: GroupHelper instance for user group operations
            uh: UserHelper instance for user settings
            ith: ItemHelper instance for item operations
            ch: CollectionHelper instance for collection operations

        Returns:
            Tuple of (item object, created flag) where created is True if new item was created
        """
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

        if collection_id:
            log.info(
                f"Found collection {collection_id} from path {self.path}, adding item {self.item_id} to it"
            )
            ch.addItemToCollection(collection_id, self.item_id)
        return self.item, created

    def _should_replace_original_files(
        self,
        original_files: List[Any],
        replace: bool,
        legacy_storages: Optional[List[str]],
    ) -> bool:
        """Check if original files should be replaced based on criteria.

        Args:
            original_files: List of existing original files for the item
            replace: Whether to replace existing files
            legacy_storages: List of legacy storage IDs to check against

        Returns:
            True if files should be replaced, False otherwise
        """
        if not self.file:
            return False

        if len(original_files) >= 0 and replace == False:
            log.info(
                f"Importing {self.item_id}: Item already exists and has an original file, skipping it"
            )
            return False

        if self.file.getId() in [f.getId() for f in original_files]:
            log.info(
                f"Importing {self.item_id}: File is already in original files, skipping it"
            )
            return False

        if self.file.getStorage() in [f.getStorage() for f in original_files]:
            log.info(f"Importing {self.item_id}: File is on same storage, skipping it")
            return False

        if legacy_storages:
            if any(f.getStorage() not in legacy_storages for f in original_files):
                log.info(
                    f"Importing {self.item_id}: File is not on legacy storage, skipping it"
                )
                return False

        return True

    def _remove_original_shapes(
        self, user: Optional[User], item_helper: Any, storage_helper: Any
    ) -> None:
        """Remove existing original shapes before replacement.

        Args:
            user: User performing the operation
            item_helper: ItemHelperExtended instance
            storage_helper: StorageHelper instance
        """
        original_shapes = item_helper.getItemShapesFromNames(self.item_id, ["original"])

        for original_shape in original_shapes:
            original_files = original_shape.getAllFiles()
            for _file in original_files:
                log.info(
                    f"Importing {self.item_id}: Removing file {_file.getId()} from original shape"
                )
                storage_helper.removeFileItemRelationship(
                    _file.getStorage(), _file.getId()
                )

            log.info(
                f"Importing {self.item_id}: Removing original shape {original_shape.getId()}"
            )
            item_helper.itemapi.removeItemShape(
                self.item_id, original_shape.getId(), runasuser=user
            )

    def _get_or_create_placeholder_shape(
        self, user: Optional[User], item_helper: Any
    ) -> Optional[str]:
        """Get existing placeholder shape or create a new one.

        Args:
            user: User performing the operation
            item_helper: ItemHelperExtended instance

        Returns:
            Shape ID string if successful, None if shape is not a placeholder
        """
        original_shapes = item_helper.getItemShapesFromNames(
            self.item_id, ["original"], placeholder=True
        )

        if original_shapes is None or len(original_shapes) == 0:
            log.info(f"Importing {self.item_id}: No original shape found, creating one")
            response = item_helper.itemapi.createPlaceholderShape(
                self.item_id, runasuser=user
            )
            return response.decode("UTF-8")

        shape = original_shapes[0]
        if len(shape.getAllFiles()) > 0:
            log.info(f"Importing {self.item_id}: Shape is not a placeholder")
            return None

        return shape.getId()

    def _import_single_component(
        self,
        main_file_id: str,
        user_groups: List[str],
        no_transcode: Optional[bool],
        ingest_helper: Any,
        job_helper: Any,
    ) -> bool:
        """Import a single-component (no extra files) clip.

        Args:
            main_file_id: File ID of the main media file
            user_groups: List of user groups for ingest profile
            no_transcode: Whether to skip transcoding
            ingest_helper: TapelessIngestHelper instance
            job_helper: JobHelper instance

        Returns:
            True if import was successful, False otherwise
        """
        log.info(f"Importing {self.item_id}: Start importing single-component shape")

        res = ingest_helper.importFileToPlaceholder(
            self.item_id,
            file_id=main_file_id,
            ingestprofile_groups=user_groups,
            notification_id=None,
            noTranscode=no_transcode,
            ignore_sidecars=True,
        )

        if "jobId" in res:
            self.job = job_helper.getJob(res["jobId"])
            return True

        return False

    def _count_media_components(
        self, all_files: List[Dict[str, Any]]
    ) -> Tuple[Optional[int], Optional[int]]:
        """Count audio and video components in file list.

        Args:
            all_files: List of file dictionaries with 'type' keys

        Returns:
            Tuple of (audio_count, video_count), where counts are None if zero
        """
        audio_count = sum(file["type"] == "audio" for file in all_files)
        video_count = sum(file["type"] == "video" for file in all_files)

        return (
            None if audio_count == 0 else audio_count,
            None if video_count == 0 else video_count,
        )

    def _import_multi_component(
        self,
        main_file: Dict[str, Any],
        extra_files: List[Dict[str, Any]],
        shape_id: str,
        user_groups: List[str],
        no_transcode: Optional[bool],
        user: Optional[User],
        item_helper: Any,
        job_helper: Any,
    ) -> bool:
        """Import a multi-component (with extra files) clip.

        Args:
            main_file: Main media file dictionary with 'file_id' and 'path'
            extra_files: List of extra media file dictionaries
            shape_id: Shape ID to import into
            user_groups: List of user groups for ingest profile
            no_transcode: Whether to skip transcoding
            user: User performing the operation
            item_helper: ItemHelperExtended instance
            job_helper: JobHelper instance

        Returns:
            True if import was successful
        """
        log.info(f"Importing {self.item_id}: Start importing multi-component shape")

        main_file_id = main_file["file_id"]
        query = {"fileId": main_file_id, "tag": "lowres"}
        if no_transcode:
            query["no-transcode"] = no_transcode

        all_files = extra_files + [main_file]
        audio_count, video_count = self._count_media_components(all_files)

        if shape_id:
            log.info(
                f"Importing {self.item_id}: Found shape {shape_id}, updating components count to {video_count} video components and {audio_count} audio component"
            )
            item_helper.itemapi.updatePlaceholderComponentCount(
                self.item_id,
                shape_id,
                container=1,
                video=video_count,
                audio=audio_count,
            )

        signals.vidispine_pre_ingest.send(
            sender=ItemHelper,
            instance=self.item_id,
            method="importFileToPlaceholder",
            query=query,
        )

        for extra_file in extra_files:
            if extra_file["type"] in ["audio", "video"]:
                q = {"fileId": extra_file["file_id"], "tag": "lowres"}
                log.info(
                    f"Importing {self.item_id}: Import file {extra_file['file_id']}:{extra_file['path']} to component {extra_file['type']}..."
                )
                component_res = item_helper.itemapi.doImportToPlaceholder(
                    item_id=self.item_id,
                    query=q,
                    component=extra_file["type"],
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
        res = item_helper.itemapi.doImportToPlaceholder(
            item_id=self.item_id,
            query=query,
            runasuser=user,
            ignore_sidecars=True,
            ingestprofile_groups=user_groups,
        )

        invalidate_item_cache(self.item_id)
        signals.vidispine_post_ingest.send(
            sender=ItemHelper,
            instance=self.item_id,
            method="importFileToPlaceholder",
        )

        if "jobId" in res:
            self.job = job_helper.getJob(res["jobId"])

        log.info(f"Retranscoding shape with item {self.item_id} and shape {shape_id}")
        return True

    def import_file(
        self,
        collection_id: Optional[str] = None,
        user: Optional[User] = None,
        replace: bool = False,
        legacy_storages: Optional[List[str]] = None,
    ) -> Dict[str, bool]:
        """Import clip files into Vidispine, creating or updating an item.

        This method handles the complete import workflow including:
        - Creating or updating the Vidispine item
        - Handling file replacement for legacy storage migration
        - Importing single or multi-component media files
        - Managing placeholder shapes and transcoding

        Args:
            collection_id: Optional collection ID to add the item to
            user: User performing the import operation
            replace: Whether to replace existing original files
            legacy_storages: List of storage IDs considered as legacy for replacement

        Returns:
            Dictionary with status flags:
                - skipped: True if import was skipped
                - failed: True if import failed
                - replaced: True if original files were replaced
                - ingested: True if import succeeded
        """
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
        _, created = self.create_item(
            user=user,
            collection_id=collection_id,
            replace=replace,
            ith=_ith,
            gh=_gh,
            ch=_ch,
        )
        if not replace and not created:
            log.info("Item already exists, skipping it")
            result["skipped"] = True
            return result

        # Handle existing item replacement
        if self.item is not None:
            if not self.file:
                log.info(
                    f"Importing {self.item_id}: No file to replace with, skipping it"
                )
                result["skipped"] = True
                return result

            original_shapes = _ith.getItemShapesFromNames(self.item_id, ["original"])
            if original_shapes:
                for original_shape in original_shapes:
                    original_files = original_shape.getAllFiles()

                    if not self._should_replace_original_files(
                        original_files, replace, legacy_storages
                    ):
                        result["skipped"] = True
                        return result

                    self._remove_original_shapes(user, _ith, _sh)
                    result["replaced"] = True

        self.user = user
        self.status = self.STATUS_PLACHOLDER_CREATED

        # Get media files and import options
        extra_files = self.provider.getClipAdditionalMediaFiles(self)
        main_file = self.provider.getClipMainMediaFile(self)
        options = self.provider.getImportOptions()
        main_file_id = main_file["file_id"]

        no_transcode = options.get("no-transcode", None)
        if result["replaced"]:
            no_transcode = True

        _, default_ingest_group = _gh.getUserIngestGroups()
        user_groups = [urllib.parse.quote(str(default_ingest_group))]

        shape_id = self._get_or_create_placeholder_shape(user, _ith)
        if shape_id is None:
            result["failed"] = True
            return result

        # Import based on component count
        if len(extra_files) == 0:
            if self._import_single_component(
                main_file_id, user_groups, no_transcode, _igh, _ijh
            ):
                result["ingested"] = True
                return result
        else:
            if self._import_multi_component(
                main_file,
                extra_files,
                shape_id,
                user_groups,
                no_transcode,
                user,
                _ith,
                _ijh,
            ):
                result["ingested"] = True
                return result

        result["ingested"] = True
        return result

    def ingest(
        self,
        collection_id: Optional[str] = None,
        user: Optional[User] = None,
        folder: Optional[Any] = None,
        replace: bool = False,
        legacy_storages: Optional[List[str]] = None,
    ) -> Dict[str, bool]:
        """Convenience method that wraps import_file for ingest operations.

        Args:
            collection_id: Optional collection ID to add the item to
            user: User performing the ingest operation
            folder: Folder object containing the clip
            replace: Whether to replace existing original files
            legacy_storages: List of storage IDs considered as legacy for replacement

        Returns:
            Dictionary with status flags from import_file operation
        """
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

    def persist_metadatas(self) -> None:
        """Write this clip's memoized metadatas, batched.

        The explicit replacement for the deleted ``save()`` fan-out, and
        the SAME code path the scan write unit uses — two statements
        whatever the key count, instead of one upsert query per metadata
        key plus a delete. A clip that never had metadatas assigned
        writes nothing at all (pre-2.4 ``save()`` guarded on the memo the
        same way); an EMPTY metadatas mapping still clears the clip's
        rows, also exactly as before.
        """
        if not hasattr(self, "_metadatas"):
            return
        writes = (MetadataWrite(umid=self.pk, metadatas=self._metadatas),)
        type(self).persist_metadatas_bulk(writes, group_stale_deletes(writes))

    @classmethod
    def persist_metadatas_bulk(
        cls,
        writes: Any,
        stale_deletes: Any,
    ) -> None:
        """Upsert every clip's metadata rows, then drop the stale ones.

        ``writes`` must hold at most one entry per umid — the plan layer
        (``scan.persistence.dedupe_candidates``) guarantees it, and a
        repeated conflict target inside one ``ON CONFLICT`` statement is
        rejected by both sqlite and PostgreSQL. ``stale_deletes`` comes
        from ``group_stale_deletes`` over the SAME writes, so the two can
        never disagree about which keys survive.
        """
        rows = [
            ClipMetadata(clip_id=write.umid, name=name, value=value)
            for write in writes
            for name, value in write.metadatas.items()
        ]
        if rows:
            ClipMetadata.objects.bulk_create(
                rows,
                update_conflicts=True,
                unique_fields=["clip", "name"],
                update_fields=["value"],
            )
        for stale in stale_deletes:
            ClipMetadata.objects.filter(clip_id__in=stale.umids).exclude(
                name__in=stale.keep_names
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

import logging
from collections.abc import Mapping
from typing import Optional, Dict, List, Tuple, Any

import os
import urllib
import pyxb.utils, simplejson as json
from django.urls import reverse
from django.db import models, transaction
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
from portal.plugins.TapelessIngest.scan.ingestion import needs_hash_recovery
from portal.plugins.TapelessIngest.scan.persistence import (
    BULK_BATCH_SIZE,
    MetadataWrite,
    chunked,
    group_stale_deletes,
)

log = logging.getLogger(__name__)

# Alias over the canonical membership tuple. Registry order is load
# bearing: it decides which provider claims a file when several are
# applicable. See docs/adding-a-provider.md.
PROVIDERS_LIST = list(PROVIDER_NAMES)


def job_id_from_response(response: Any) -> Optional[str]:
    """The import job id in a Vidispine import response, or None.

    FR-36 turns "no job id" into a failure verdict, so the check must
    survive the shapes a helper can actually answer with: a mapping
    without ``jobId``, an empty body, ``None``, or a non-mapping. A bare
    ``"jobId" in response`` raises TypeError on the last two — out of
    ``import_file``, past the per-clip catch, and into the folder's
    error list instead of the failed counter.
    """
    if not isinstance(response, Mapping):
        return None
    return response.get("jobId") or None


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

        # setdefault, not a plain assignment (story 3.1): two worker
        # threads can both miss the check above and both instantiate, and
        # the old check-then-set handed each its own instance. Instance
        # identity is load-bearing — ExtensionMap ranks providers by
        # id(provider) — so whichever write lands first wins for BOTH
        # callers; the loser's instance is discarded, never handed out.
        return cls._PROVIDER_CACHE.setdefault(provider_name, Provider)

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
        matched: Optional[List[Any]] = None,
    ) -> Dict[str, Any]:
        """Pass 1 of the scan's two-pass lookup: everything before the DB.

        Providers and identity validation only, so a page's umids can be
        collected first and looked up in ONE query (AD-6) instead of one
        ``get`` per file. Hash recovery is NOT here: it needs the clip's
        ``item_id``, i.e. the lookup's answer (see ``recover_item_id``).

        Args:
            file: VSFile instance to extract metadata from
            provider_list: Optional list of provider instances. If None, uses all providers
            context: Optional context dictionary shared across provider calls; it is
                mutated in place by the providers, never replaced
            matched: Optional out-list. Every provider that CONTRIBUTED to
                this file is appended to it — the multi-provider contract
                (AD-7/FR-14) made observable, so a caller deciding what a
                clip consumed does not have to read it back off
                ``metadatas["provider"]``, which is last-writer-wins.

        Returns:
            The merged metadatas dict

        Raises:
            TapelessIngestException: If no UMID found in file
        """
        if provider_list is None:
            provider_list = cls._get_provider_list()
        if context is None:
            context = {}
        # `provider_list` is already pre-filtered to the applicable
        # providers by the caller; every one of them runs and merges.
        metadatas = extract_metadatas(file, provider_list, {}, context, matched=matched)
        if "umid" not in metadatas.keys():
            raise TapelessIngestException("No UMID found in file %s" % file.getPath())
        return metadatas

    def recover_item_id(
        self, file: Any, legacy_storages: Optional[List[str]] = None
    ) -> Optional[str]:
        """Find this clip's item on a legacy storage, by file hash — if useful.

        The gate is ``scan.ingestion.needs_hash_recovery``: a clip that
        already has an ``item_id``, a file Cantemo has not hashed yet, or
        a run with no legacy storages configured all cost ZERO HTTP calls
        (FR-8).

        A recovered id is assigned to the clip whether the row is new or
        pre-existing — recovery only fires when there is no ``item_id``,
        so it can never overwrite a known one — and the scan's write unit
        persists it under a fill-only-NULL guard
        (``PersistencePlan.recovered_item_ids``).

        Every step is defensive: a storage that errors, a response of an
        unexpected shape and an unreadable hash must all cost the clip
        nothing more than this recovery attempt. Losing the clip here
        would drop it from the write plan and leave no row at all.

        Returns:
            The recovered item id, or None when the gate closed or no
            legacy storage knew the hash.
        """
        try:
            hash = file.getHash()
        except Exception:
            log.error(
                f"Cannot read the hash of {self.umid}'s scanned file",
                exc_info=True,
            )
            return None
        if not needs_hash_recovery(self.item_id, hash, legacy_storages):
            return None
        sh = StorageHelper()
        for legacy_storage in legacy_storages:
            try:
                results = sh.storageapi.getFilesInStorage(
                    legacy_storage, query={"hash": [hash], "includeItem": "true"}
                )
                item_id = self._item_id_from_hash_hits(results)
            except Exception as e:
                log.error(
                    f"Error getting files with hash {hash} in storage {legacy_storage}: {e}"
                )
                continue
            if item_id:
                self.item_id = item_id
                log.info(
                    f"Recovered item {item_id} for {self.umid} from "
                    f"storage {legacy_storage} by hash {hash}"
                )
                return item_id
        return None

    @staticmethod
    def _item_id_from_hash_hits(results: Any) -> Optional[str]:
        """The item id inside a ``getFilesInStorage`` answer, or None.

        Every level is optional on purpose: a hit count without a ``file``
        list, a file without an ``item``, an item without an ``id`` — any
        of them used to raise KeyError/IndexError out of the scan's
        per-file wrapper and cost the clip its row.
        """
        if not isinstance(results, dict):
            return None
        if not results.get("hits"):
            return None
        files = results.get("file") or []
        if not files:
            return None
        items = (files[0] or {}).get("item") or []
        if not items:
            return None
        return (items[0] or {}).get("id") or None

    @classmethod
    def new_clip_defaults(
        cls, file: Any, metadatas: Dict[str, Any], item_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """The column values a NEVER-SEEN clip is created with.

        The one copy of the new-row shape: both the per-file
        ``get_or_new`` path and the scan's batched path build a new clip
        from exactly this dict, so the two can never drift. ``item_id``
        stays None here — recovery runs after the row is known
        (``recover_item_id``), which is what lets a pre-existing row
        receive a recovered id too.
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

        Instance state only: ``metadatas`` is memoized (the folder's
        write unit persists it, once, in batch) and
        ``provider_name``/``file_id``/``reference_file`` do not reach the
        DB during a scan for an EXISTING clip (``CLIP_UPDATE_FIELDS``).
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
            TapelessIngestException: If no UMID found in file
        """
        metadatas = cls.extract_file_metadatas(
            file,
            provider_list=provider_list,
            context=context,
        )
        # Lookup FIRST, recovery second (2.5): the clip's own item_id is
        # what decides whether a legacy-storage hash lookup is worth any
        # HTTP call at all.
        clip, created = cls.get_or_new(
            umid=metadatas["umid"],
            defaults=cls.new_clip_defaults(file, metadatas),
        )
        clip.attach_file_metadatas(file, metadatas)
        clip.recover_item_id(file, legacy_storages)

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
    def cached_file_hash(self):
        """This clip's file hash, from the scan memo ONLY — never over HTTP.

        The ingest ladder's ``has_hash`` input (``scan.ingestion``): it
        reads ``_file``, the file the scan attached, and not the ``file``
        property, which issues a ``getFileById`` call when the memo is
        cold — one call per clip to decide the clip must not cost any.

        A hash that cannot be read is treated as absent: under NFR-1 that
        means "skip and retry next run", never ingest without the dedup
        key.
        """
        file = getattr(self, "_file", None)
        if file is None:
            return None
        try:
            return file.getHash()
        except Exception:
            log.error(
                f"Cannot read the hash of {self.umid}'s scanned file",
                exc_info=True,
            )
            return None

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

        FR-12 read order: a non-empty ``clip_xml`` is authoritative and
        is never re-parsed from the card. A corrected sidecar on disk is
        therefore ignored for the life of the row — declared in
        tests/fr4-waivers.md.
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
        inserted.

        Fills an EMPTY column only — that guard is what keeps a re-scan
        from re-reading and re-parsing every card sidecar — and only from
        a ``clip_xml_file`` the providers already resolved; the sidecar is
        located WITHOUT touching Vidispine (the memoized root only), so a
        scan buys no extra HTTP call. A parse failure is swallowed and
        logged: an optional column must never cost a clip its ingest.

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
        MEMOIZED root only, so it costs no storage lookup and cannot
        raise out of the ``root_path`` chain.
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
        # Memo only (AD-6): persisting is the batched write unit's job —
        # models/folder.persist_scan_results for the scan path,
        # persist_metadatas() for everyone else.
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
            # Falsiness, not `is None`: the item-deletion listener writes
            # `""`, which must answer None here rather than reach
            # `getItem("")`.
            if not self.item_id:
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
            # Falsiness, not `is None`: the item-deletion listener writes
            # `""`, which must answer None here rather than reach
            # `getJob("")` — the same rule as `Clip.item`.
            if not self.job_id:
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
        gh: Any = None,
        uh: Any = None,
        ith: Any = None,
        ch: Any = None,
    ) -> Tuple[Any, bool]:
        """Create a new Vidispine item with metadata or update existing item.

        Args:
            user: User performing the operation
            collection_id: Optional collection ID to add the item to
            replace: Whether to replace existing metadata if item exists
            metadatagroupname: Name of the metadata group to use
            gh: GroupHelper for user group operations. ``None`` (the
                default) means "build one per call, as ``user``".
            uh: UserHelper for user settings. ``None`` (the default)
                means "build one per call, as ``user``".
            ith: ItemHelper for item operations. ``None`` (the default)
                means "build one per call, as ``user``". A caller with a
                subclass (``import_file`` passes an
                ``ItemHelperExtended``) passes it explicitly and it is
                used as given.
            ch: CollectionHelper for collection operations. ``None`` (the
                default) means "build one per call, as ``user``".

        Returns:
            Tuple of (item object, created flag) where created is True if new item was created
        """
        created = False

        if self.item_id and self.item is None:
            # The row NAMES an item Vidispine cannot resolve — `Clip.item`
            # answers None on NotFoundError too (the accident commit
            # 632bd4c closed), so falling through would take the create
            # branch and make a SECOND placeholder beside the one the row
            # already names. Refuse instead, BEFORE the preparatory round
            # trips (a wedged row recurs every run by design, and should
            # cost one getItem, not the full ingest-group/settings/metadata
            # preamble): the per-clip handler counts this clip failed with
            # the reason below, every run, until the row is healed
            # (operator tooling is the maintenance story's).
            raise TapelessIngestException(
                f"clip {self.umid} names item {self.item_id}, which "
                f"Vidispine cannot resolve — refusing to create a "
                f"second placeholder for it; heal or reset the row"
            )

        # BELOW the wedged-row refusal above, not before it (retro-3
        # review): story 3.0's frozen intent is that a wedged row costs
        # ONE getItem, not the full preamble, and four helper
        # constructors ahead of the refusal quietly broke that — it
        # recurs every run by design, so its cost is a per-run cost.
        #
        # Story 3.1 review: `uh` defaults to None and is built PER CALL,
        # as the run's user. The old class-definition-time
        # ``UserHelper()`` default was one shared instance — with
        # ``runas=None`` — for every caller in the process; a future
        # caller omitting ``uh`` must not silently inherit it back.
        if uh is None:
            uh = UserHelper(runas=user)
        # Retro-3 F4: `gh`/`ith`/`ch` were the SAME defect class — one
        # shared class-definition-time instance each, with ``runas=None``,
        # for every caller in the process (cross-thread shared state under
        # the pool). Per call, as the run's user, exactly like ``uh`` and
        # like ``import_file`` already builds the ones it passes in. An
        # explicitly passed helper is used AS GIVEN, never rebuilt.
        if gh is None:
            gh = GroupHelper(runas=user)
        if ith is None:
            ith = ItemHelper(runas=user)
        if ch is None:
            ch = CollectionHelper(runas=user)

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
            if not self.item_id:
                # The `item` setter silently no-ops on a falsy return, so
                # without this guard the combined write below would stamp
                # PLACEHOLDER_CREATED beside an EMPTY item_id — fabricating
                # the one cell the ladder refuses to act on — and execution
                # would carry on into setItemMetadataFieldGroup(None).
                raise TapelessIngestException(
                    f"createPlaceholder returned no item for {self.umid}"
                )
            # NFR-1 (story 3.0): make the placeholder durable NOW — one
            # targeted UPDATE writing `item_id` and `status` TOGETHER,
            # before any further Vidispine call. A death anywhere after
            # this statement leaves exactly the FR-36 incomplete-import
            # cell (item_id, no job_id, PLACEHOLDER_CREATED), which the
            # existing retry rung recovers into this same placeholder next
            # run. By pk, and never a save() fallback, which would INSERT
            # a partial row before the import ran. The OTHER writer of
            # these columns is `persist_ingest_state` (INGEST_STATE_FIELDS),
            # which runs after `import_file` returns and re-writes both
            # along with the job id. Deliberate side effect of `.update()`:
            # it bypasses `created_on`'s `auto_now` (a misnamed
            # last-modified column), so the FR-36 cell keeps its scan-time
            # timestamp.
            self.status = self.STATUS_PLACHOLDER_CREATED
            updated = (
                type(self)
                .objects.filter(pk=self.pk)
                .update(item_id=self.item_id, status=self.status)
            )
            if not updated and not self._state.adding:
                # Two different absences: an UNSAVED clip (the REST path)
                # legitimately has no row and stays a silent no-op, but a
                # PERSISTED clip whose row vanished mid-run means the
                # placeholder's name was just lost — a death before
                # persist_ingest_state re-inserts it would orphan it.
                log.error(
                    f"clip {self.umid}: the combined item_id/status UPDATE "
                    f"matched no row — a persisted clip's row was deleted "
                    f"concurrently, so placeholder {self.item_id} is not "
                    f"durably recorded until persist_ingest_state re-inserts "
                    f"the row"
                )
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

        # FR-35: this length test used to be `>=`, i.e. always true, so
        # the rung swallowed every non-replace call whatever the shape
        # held. An item whose original shape has NO file is not "already
        # imported" — it falls through to the checks below, which are the
        # ones that can actually tell.
        if len(original_files) > 0 and replace == False:
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

        job_id = job_id_from_response(res)
        if job_id:
            self.job = job_helper.getJob(job_id)
            return True

        log.error(
            f"Importing {self.item_id}: single-component import response "
            f"carried no job id ({res!r}) — no import job was started"
        )
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

        # Codemill declares the component count first, then imports every extra
        # component, and imports the main file LAST — the main import is what
        # closes the placeholder, so it must see a complete component set. Keep
        # that order. It differs on the count itself: Codemill only ever handles
        # spanned video (``video=len(extra) + 1``), whereas an extra here can be
        # a P2 audio track, so the count is taken per type.
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

        # Components carry no shape tag and no ingest profile, matching
        # Codemill's ``ItemHelper.importFileToPlaceholder``, which sends a bare
        # ``{'fileId': ...}`` for every extra component and puts the tag and the
        # ``jobmetadata`` groups on the main import alone. A tag here asks
        # Vidispine to derive a shape from one component in isolation — for a P2
        # clip that means transcoding a single audio track to a video preset.
        # ``ignore_sidecars`` has no Codemill counterpart: it postdates 2.2.0 and
        # is what the single-component path already passes.
        for extra_file in extra_files:
            if extra_file["type"] in ["audio", "video"]:
                q = {"fileId": extra_file["file_id"]}
                log.info(
                    f"Importing {self.item_id}: Import file {extra_file['file_id']}:{extra_file['path']} to component {extra_file['type']}..."
                )
                component_res = item_helper.itemapi.doImportToPlaceholder(
                    item_id=self.item_id,
                    query=q,
                    component=extra_file["type"],
                    runasuser=user,
                    ignore_sidecars=True,
                )
                component_job_id = job_id_from_response(component_res)
                if component_job_id:
                    log.info(f"... and got job {component_job_id}")
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

        job_id = job_id_from_response(res)
        if job_id:
            self.job = job_helper.getJob(job_id)

        log.info(f"Retranscoding shape with item {self.item_id} and shape {shape_id}")

        # FR-36: no job id means Vidispine started no import job. Returning
        # True here — as this method unconditionally did — is how a clip
        # could be reported ingested with a NULL job_id and no import.
        if not job_id:
            log.error(
                f"Importing {self.item_id}: multi-component import response "
                f"carried no job id ({res!r}) — no import job was started"
            )
            return False
        return True

    def import_file(
        self,
        collection_id: Optional[str] = None,
        user: Optional[User] = None,
        replace: bool = False,
        legacy_storages: Optional[List[str]] = None,
        retry_incomplete: bool = False,
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
            retry_incomplete: Whether to look past "the item already
                exists" for a clip whose previous import created a
                placeholder and never started a job. Deliberately NOT
                ``replace``: it only lifts the blanket early return, so
                an item that really holds original files still comes back
                ``skipped`` instead of having its shape removed and
                re-imported.

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
        # Per-call, like every helper above (story 3.1). This call site
        # passes all four explicitly and always did — `_ith` in
        # particular is an `ItemHelperExtended`, which no default could
        # supply. What changed is on the other side: `create_item`'s
        # `gh`/`uh`/`ith`/`ch` now DEFAULT to None and are built per call
        # as the run's user (retro-3 F4), where they used to be four
        # shared instances built at class-definition time with
        # runas=None. Passing them here is no longer what saves a future
        # caller from that — but it is still what puts the subclass and
        # this run's user in.
        _uh = UserHelper(runas=user)

        # Create item if it doesn't exist
        _, created = self.create_item(
            user=user,
            collection_id=collection_id,
            replace=replace,
            ith=_ith,
            gh=_gh,
            uh=_uh,
            ch=_ch,
        )
        if not replace and not created and not retry_incomplete:
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
            imported = self._import_single_component(
                main_file_id, user_groups, no_transcode, _igh, _ijh
            )
        else:
            imported = self._import_multi_component(
                main_file,
                extra_files,
                shape_id,
                user_groups,
                no_transcode,
                user,
                _ith,
                _ijh,
            )

        # FR-36: an import with no job id is a FAILURE, never an ingest.
        # The unconditional `result["ingested"] = True` that used to close
        # this method reported success for exactly the responses the two
        # helpers had just rejected.
        if imported:
            result["ingested"] = True
        else:
            log.error(
                f"Importing {self.item_id}: no import job was started, counting "
                f"this clip failed"
            )
            result["failed"] = True
        return result

    # The columns an ingest owns, and the only ones it writes back. Every
    # column `import_file` mutates is here: item_id (create_item's
    # placeholder), job_id (the import job), status, user — plus file_id,
    # which the scan attached and the deleted full save() also persisted.
    #
    # `user_id`, not `user`: reading `self.user` on a row whose FK is not
    # already loaded issues a SELECT to fetch the User object, once per
    # clip, inside the submission loop the whole AD-6 effort exists to
    # keep query-free — and the UPDATE only ever needed the id. The two
    # write the same column.
    INGEST_STATE_FIELDS = ("item_id", "job_id", "status", "file_id", "user_id")

    def ingest(
        self,
        collection_id: Optional[str] = None,
        user: Optional[User] = None,
        folder: Optional[Any] = None,
        replace: bool = False,
        legacy_storages: Optional[List[str]] = None,
        expect_persisted: bool = True,
        retry_incomplete: bool = False,
    ) -> Dict[str, bool]:
        """Convenience method that wraps import_file for ingest operations.

        Args:
            collection_id: Optional collection ID to add the item to
            user: User performing the ingest operation
            folder: Folder object containing the clip
            replace: Whether to replace existing original files
            legacy_storages: List of storage IDs considered as legacy for replacement
            expect_persisted: Whether the caller guarantees the clip's row
                already exists (the scan path does; the REST endpoint,
                which ingests a clip built from a request body, does not)
            retry_incomplete: Whether this clip's last import left a
                placeholder and no job (see ``import_file``)

        Returns:
            Dictionary with status flags from import_file operation
        """
        result = self.import_file(
            user=user,
            collection_id=collection_id,
            replace=replace,
            legacy_storages=legacy_storages,
            retry_incomplete=retry_incomplete,
        )
        self.persist_ingest_state(expect_persisted=expect_persisted)
        if folder:
            # M2M row insert: a declared AD-6 deviation, dry-run gated by
            # the caller, tracked for 2.7 / the coordinator.
            self.folders.add(folder)
        return result

    def persist_ingest_state(self, expect_persisted: bool = True) -> None:
        """Write back what the import decided, and nothing else (AD-6).

        ONE targeted UPDATE of ``INGEST_STATE_FIELDS``. Second writer of
        ``item_id``/``status``: ``create_item``'s combined write already
        made the placeholder durable mid-import (story 3.0); this one
        re-writes both alongside the job id. A full ``save()``
        would rewrite every column of a row the scan just wrote,
        including the location columns the scan deliberately leaves alone
        (``scan.persistence.CLIP_UPDATE_FIELDS``).

        Two ways the row may not be there: an unsaved clip (the REST
        endpoint builds one from the request body — legitimate, hence
        ``expect_persisted``), or a row that vanished between the scan's
        write unit and now. Both fall back to ``save()``: an ingest whose
        state is not persisted is an ingest that runs again next scan.
        """
        if not self._state.adding:
            updated = (
                type(self)
                .objects.filter(pk=self.pk)
                .update(
                    **{
                        field: getattr(self, field)
                        for field in self.INGEST_STATE_FIELDS
                    }
                )
            )
            if updated:
                return
            log.error(
                f"clip {self.umid}: ingest-state UPDATE matched no row — the "
                f"row was deleted concurrently; re-inserting it so the ingest "
                f"is not silently lost"
            )
        elif expect_persisted:
            log.error(
                f"clip {self.umid} reached ingest unsaved — the scan's write "
                f"unit should have written it"
            )
        self.save()

    def persist_metadatas(self) -> None:
        """Write this clip's memoized metadatas, batched.

        The SAME code path the scan write unit uses — a bounded number of
        statements whatever the key count, instead of one upsert per
        metadata key plus a delete. A clip that never had metadatas
        assigned writes nothing at all; an EMPTY metadatas mapping still
        clears the clip's rows.
        """
        if not hasattr(self, "_metadatas"):
            return
        writes = (MetadataWrite(umid=self.pk, metadatas=self._metadatas),)
        # Atomic like its scan-path twin (models/folder.persist_scan_results):
        # the upsert and the stale-key DELETE are one edit of this clip's
        # key-set, and half of it is worse than none. Nested inside a
        # caller's transaction this is a savepoint, so a failure here
        # cannot poison an outer request transaction.
        with transaction.atomic():
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
            # batch_size, not "one statement whatever the size": a folder
            # accumulates candidates across every page, and one card tree
            # can carry more placeholders than PostgreSQL's 65535
            # bind-parameter ceiling — which fails the whole folder's
            # write, not just the overflowing rows.
            ClipMetadata.objects.bulk_create(
                rows,
                update_conflicts=True,
                unique_fields=["clip", "name"],
                update_fields=["value"],
                batch_size=BULK_BATCH_SIZE,
            )
        for stale in stale_deletes:
            for umids in chunked(stale.umids, BULK_BATCH_SIZE):
                ClipMetadata.objects.filter(clip_id__in=umids).exclude(
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

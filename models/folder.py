import logging
import traceback
import uuid
import os
import re
from typing import Optional, Dict, List, Any
from django.db import DatabaseError, models, transaction

# `Q` in this module is opensearch-dsl's (the search doc); the ORM's is
# aliased so the two can never be confused at a call site.
from django.db.models import Case, F, Value, When
from django.db.models import Q as DBQ
from django.utils import timezone
from django.conf import settings
from django.core.cache import cache
from opensearch_dsl import Search, Q

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
from portal.plugins.TapelessIngest.scan.adapters import (
    build_default_context,
    build_provider_registry,
)
from portal.plugins.TapelessIngest.scan.context import browse_root_path
from portal.plugins.TapelessIngest.scan.extraction import (
    applicable_providers,
    consumed_subdirs,
)
from portal.plugins.TapelessIngest.scan.ingestion import (
    SKIP_ALREADY_INGESTED,
    SKIP_NO_HASH,
    is_incomplete_import,
    select_clips_to_ingest,
)
from portal.plugins.TapelessIngest.scan.persistence import (
    BULK_BATCH_SIZE,
    CLIP_UNIQUE_FIELDS,
    FOLDER_SCAN_FIELDS,
    ClipCandidate,
    build_persistence_plan,
    chunked,
)
from portal.plugins.TapelessIngest.scan.verification import FolderListings

log = logging.getLogger(__name__)


def persist_scan_results(folder, plan, dry_run=False):
    """Execute one folder's persistence plan — the AD-6 write unit.

    The Portal/ORM half of the plan/executor split: ``scan.persistence``
    decided WHAT to write with no Django in sight, this decides how, in
    one ``transaction.atomic()`` per folder, in a fixed order — Clip
    upsert, recovered item ids, ClipMetadata upsert + stale-key delete,
    then the Folder row. The statement count is bounded per folder: never
    one per file, never one per metadata key.

    ``dry_run`` gates the whole unit.

    The clips are not passed separately: ``plan.clip_rows`` is the
    deduped, authoritative row set, and a second clip list travelling
    beside it could only ever disagree with it.

    Returns:
        True when the transaction ran, False when there was nothing to
        write or the run is a dry run.
    """
    if dry_run or plan.is_empty:
        return False
    with transaction.atomic():
        if plan.clip_rows:
            # Upsert, not insert: a concurrent scan (or a duplicate umid
            # the plan collapsed) must never raise a constraint error.
            # update_fields is the narrow allow-list from the plan layer,
            # so an EXISTING clip's location and ingest state survive
            # untouched — see scan.persistence.CLIP_UPDATE_FIELDS.
            Clip.objects.bulk_create(
                list(plan.clip_rows),
                update_conflicts=True,
                unique_fields=list(CLIP_UNIQUE_FIELDS),
                update_fields=list(plan.update_fields),
                batch_size=BULK_BATCH_SIZE,
            )
        _persist_recovered_item_ids(plan.recovered_item_ids)
        if plan.metadata_writes or plan.stale_deletes:
            Clip.persist_metadatas_bulk(plan.metadata_writes, plan.stale_deletes)
        if plan.save_folder:
            for field_name, value in plan.folder_fields.items():
                setattr(folder, field_name, value)
            if folder._state.adding or not plan.folder_fields:
                folder.save()
            else:
                # The narrowness FOLDER_SCAN_FIELDS advertises, enforced:
                # a scan owns three columns of this row and must not write
                # back whatever else the instance is carrying (a
                # collection_id an ingest resolved, say).
                folder.save(update_fields=list(plan.folder_fields))
    return True


def _incomplete_import(clip):
    """Did this clip's last import leave a placeholder and no job?

    The state ``import_file`` writes when Vidispine answers without a job
    id: ``create_item`` already assigned the placeholder's ``item_id``
    and the status was already moved, but nothing was ever imported.
    Narrow on purpose — a clip whose ``item_id`` came from hash RECOVERY
    also has no job, and re-examining those every run is exactly the
    per-clip HTTP cost FR-8 removed.
    """
    return is_incomplete_import(
        clip.item_id,
        clip.job_id,
        clip.status == Clip.STATUS_PLACHOLDER_CREATED,
    )


def _ingest_state(clip):
    """The ``(clip, has_hash, import_incomplete)`` triple the ladder reads."""
    return (clip, bool(clip.cached_file_hash), _incomplete_import(clip))


# One message per skip token. A mapping rather than an if/else so a new
# token added to scan.ingestion cannot silently log as the wrong reason.
_SKIP_REASON_MESSAGES = {
    SKIP_NO_HASH: lambda clip: "no hash yet — will retry next run",
    SKIP_ALREADY_INGESTED: lambda clip: f"already ingested as {clip.item_id}",
}


def _skip_reason_message(clip, reason):
    render = _SKIP_REASON_MESSAGES.get(reason)
    if render is None:
        log.error(f"unknown ingest skip reason {reason!r} for clip {clip}")
        return f"skipped ({reason})"
    return render(clip)


def _persist_recovered_item_ids(recovered_item_ids):
    """Fill in item ids hash recovery found — only where the row has none.

    Without this the recovered id lives on the in-memory object and dies
    with it: the ingest ladder skips a clip the moment its ``item_id`` is
    truthy, so ``Clip.ingest`` — the only other writer of that column —
    is never reached for exactly these clips. The row would keep its NULL
    and every future scan would pay the legacy-storage lookup again.

    The ``item_id IS NULL`` filter is the safety half: recovery reads the
    row, decides, and writes later, so between the two a real ingest may
    have written a real id. That id always wins.
    """
    if not recovered_item_ids:
        return
    for batch in chunked(recovered_item_ids, BULK_BATCH_SIZE):
        Clip.objects.filter(pk__in=[umid for umid, _ in batch]).filter(
            DBQ(item_id__isnull=True) | DBQ(item_id="")
        ).update(
            item_id=Case(
                *[When(pk=umid, then=Value(item_id)) for umid, item_id in batch],
                # A row that matches no branch keeps what it has instead
                # of being nulled — the pk filter makes that unreachable,
                # but a CASE without a default is a loaded gun.
                default=F("item_id"),
            )
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

    def __unicode__(self):
        return f"{self.id}: {self.path}"

    def __str__(self):
        return f"{self.id}: {self.path}"

    class Meta:
        unique_together = ["path", "storage_id"]

    @classmethod
    def get_or_new(
        cls, defaults: Optional[Dict[str, Any]] = None, **kwargs: Any
    ) -> tuple["Folder", bool]:
        """Get existing folder or create a new one without saving to database.

        Args:
            defaults: Optional dictionary of default values for new folder
            **kwargs: Query parameters to find existing folder

        Returns:
            Tuple of (folder instance, is_new flag) where is_new is True if folder was created
        """

        def _new_instance():
            params = {k: v for k, v in kwargs.items()}
            params.update(defaults or {})
            # Try to create an object using passed params.
            return cls(**params), True

        try:
            return cls.objects.get(**kwargs), False
        except cls.DoesNotExist:
            return _new_instance()
        except cls.MultipleObjectsReturned:
            # FR-27, defensive only. The (path, storage_id) unique
            # constraint is live and enforced on prod (audited 2026-08-21:
            # zero duplicate rows, zero NULL storage_ids), so the ONLY way
            # in is the SQL loophole where NULLs are distinct — two rows
            # with the same path and a NULL storage_id. A scan always
            # passes a storage_id, so it can only ever READ such a pair.
            # Deterministic first-by-pk beats an unhandled exception that
            # would take the whole folder down.
            log.warning(
                f"Duplicate {cls.__name__} rows for {kwargs!r}; using the "
                f"first by pk — a NULL storage_id lets the unique "
                f"constraint through"
            )
            existing = cls.objects.filter(**kwargs).order_by("pk").first()
            if existing is not None:
                return existing, False
            # The duplicates vanished between the get() and this read.
            # Returning (None, False) would hand the caller a folder that
            # is not a folder; a brand-new instance is what the row set
            # now says, and is what DoesNotExist would have produced.
            log.warning(
                f"Duplicate {cls.__name__} rows for {kwargs!r} disappeared "
                f"before they could be read; building a new instance"
            )
            return _new_instance()

    def get_storage_helper(self) -> Any:
        """Get or create StorageHelper instance for this folder.

        Returns:
            StorageHelper instance for accessing Vidispine storage API
        """
        if not hasattr(self, "_sth"):
            self._sth = StorageHelper()
        return self._sth

    @property
    def storage(self) -> Optional[Any]:
        """Get Vidispine storage object for this folder with Redis caching.

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
    def root_path(self):
        if not hasattr(self, "_root_path"):
            # Canonical block (scan/context.py). FR-28 (story 2.6): an
            # unresolvable storage returns None instead of letting an
            # unassigned _root_path raise AttributeError (retired pin #5).
            # None is NOT memoized — a storage that resolves on a later
            # call still gets its root.
            root_path = browse_root_path(self.storage)
            if root_path is None:
                return None
            self._root_path = root_path
        return self._root_path

    def _absolute_path_from_root(self, root_path):
        """The one copy of the absolute-path truthiness semantics.

        Falsy root ("" included, never `is None`) -> False, exactly like
        the pre-2.1 absolute_path property; scan() reuses this with its
        context-resolved root.
        """
        if root_path:
            return os.path.join(root_path, self.path)
        return False

    @property
    def absolute_path(self):
        return self._absolute_path_from_root(self.root_path)

    def getFile(self, path: str) -> Any:
        """Get a file from storage by relative path.

        Args:
            path: Relative path to the file within the folder

        Returns:
            VSFile object for the requested file
        """
        subpath = os.path.join(self.path, path)
        vsfile = self._sth.getFileByPath(self.storage_id, path=subpath)
        return vsfile

    def getFiles(
        self, path: str, number: int = 0, first: int = 0, user: Optional[Any] = None
    ) -> List[Dict[str, Any]]:
        """Get all files in a folder path with pagination.

        Args:
            path: Relative path within the folder
            number: Maximum number of files to return (0 for all)
            first: Starting index for pagination
            user: User context for API calls

        Returns:
            List of file dictionaries
        """
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
        """Get Vidispine collection object for this folder with Redis caching.

        Returns:
            Collection object or None if collection_id is not set or not found
        """
        if not hasattr(self, "_collection"):
            if self.collection_id is None:
                return None

            # Try to get from cache first
            cache_key = f"collection:{self.collection_id}"
            cached_collection = cache.get(cache_key)
            if cached_collection is not None:
                self._collection = cached_collection
                return self._collection

            # If not in cache, fetch from API
            ch = CollectionHelper()
            try:
                self._collection = ch.getCollection(self.collection_id)
                # Cache for 3 minutes
                cache.set(cache_key, self._collection, 180)
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
                # Same resolution path as a scan's registry: one instance
                # per name out of the provider cache. Not the scan
                # context's registry, though — this reads the names a PAST
                # scan persisted on the row, which is a different (and
                # possibly stale) set from the run's --providers.
                self._providers = list(
                    build_provider_registry(self.provider_names.split(","))
                )
        return self._providers

    @providers.setter
    def providers(self, providers_objects):
        self._providers = providers_objects
        providers_names = []
        for provider_object in providers_objects:
            providers_names.append(provider_object.machine_name)
        self.provider_names = ",".join(providers_names)

    def getCollection(self, user, dryrun=False):
        return TapelessIngestHelper.get_collection_from_path(self.path, user, dryrun)

    def get_helper(self):
        if hasattr(self, "_tih"):
            return self._tih
        self._tih = TapelessIngestHelper()
        return self._tih

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
        *,
        context=None,
    ):
        scan_context = context
        if scan_context is None:
            # Paged callers keep today's signature: a default context is
            # built inline from the folder's cached properties (AD-14).
            scan_context = build_default_context(
                self,
                user=user,
                providers=providers,
                legacy_storages=legacy_storages,
            )
        else:
            # A passed context's options are authoritative (the commands
            # pass the same values as kwargs — equal by construction; if a
            # caller's kwargs ever diverge, the context wins by design).
            user = scan_context.options.user
            providers = scan_context.options.providers
            legacy_storages = scan_context.options.legacy_storages
        # The context carries the registry and its extension map. When the
        # registry could not be built (an unresolvable provider name), this
        # fallback is where that name raises, as it always has.
        provider_list = scan_context.provider_registry
        if provider_list is None:
            provider_list = Clip._get_provider_list(providers)
        extension_map = scan_context.extension_map
        providers = []
        response = {
            "clips": [],
            "hits": 0,
            "errors": [],
            "created": 0,
            "already_ingested": 0,
            "processed": 0,
            # Story 2.6 / FR-19, additive (NFR-5): the immediate child
            # names this folder's clips consumed, which the recursion must
            # not scan again. `None` is DOUBT — "descent is not authorized
            # for this folder" — and is the value every non-complete pass
            # keeps: count_only, paged (number != 0), no absolute path.
            "consumed_subdirs": None,
        }
        # FR-19: only a COMPLETE pass over the folder may authorize the
        # recursion to descend. count_only never assembles a clip, and a
        # paged call has seen one page out of an unknown number (or starts
        # part-way in), so neither can say what was consumed — both stay
        # DOUBT. Captured here because the page loop mutates `first`.
        complete_pass = not count_only and number == 0 and first == 0
        root_path = scan_context.root_path_for(self.storage_id)
        if not root_path:
            # Context miss / falsy root falls back to today's property
            # chain — truthiness semantics, pin #5's AttributeError included.
            root_path = self.root_path
        absolute_path = self._absolute_path_from_root(root_path)
        if absolute_path:
            # One listings cache per scan invocation (AD-4): batched
            # verification, one os.scandir per unique directory.
            listings = FolderListings()
            search_doc = self.build_search_doc(provider_list)
            # Named provider_context (not `context`) so the mutable provider
            # dict never shadows the ScanContext kwarg. One per scan()
            # invocation, spanning every page: the providers' sidecar caches
            # live in it. It never escapes the invocation.
            provider_context = {
                "folder": self,
                "clips": [],
                "scan_context": scan_context,
                "listings": listings,
            }
            # The write unit's input, accumulated across every page of
            # this invocation and persisted ONCE after the loop (AD-6).
            candidates = []
            # umid -> the clip object this invocation is building for it,
            # across pages. Two files (possibly on two different pages)
            # can carry one umid, and the umid is the primary key: they
            # are one clip, they must share one object, and `created` must
            # count once — the write plan dedupes them anyway.
            invocation_clips = {}
            # umid -> item id hash recovery found this run, for the
            # fill-only-NULL write at the end (a recovered id that never
            # reaches the row makes every future scan pay the lookup).
            recovered_item_ids = {}
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
                hits = search_result["hits"]["hits"]
                response["hits"] = search_result["hits"]["total"]["value"]
                self.clips_total = response["hits"]
                if number == 0 and len(hits) == result_number:
                    has_next = True
                    first += result_number
                # Deterministic per-page iteration order. The key tolerates
                # a malformed hit so a missing `_source` can never raise out
                # here, outside the per-file error wrapper.
                hits = sorted(
                    hits, key=lambda hit: hit.get("_source", {}).get("path") or ""
                )
                if not count_only:
                    # Pass 1: extract every file's metadatas. One record
                    # per hit, in hit order, each holding either its
                    # extraction result or the error it died of — which is
                    # what keeps the response's error/clip ordering intact
                    # with the DB lookup sitting between the two passes.
                    records = []
                    for result in hits:
                        record = {"result": result, "error": None}
                        try:
                            file = VSFile(
                                result["_source"], settings.VIDISPINE_REPLACE_URLS
                            )
                            # Validate file exists on filesystem (batched:
                            # one scandir per directory, DC-2 guard intact).
                            # A membership miss is confirmed by one real
                            # check, so a file created after the listing
                            # snapshot is still found; a file deleted after
                            # the snapshot passes here and errors downstream
                            # — the one residual TOCTOU direction,
                            # deliberate under AD-4.
                            file_absolute_path = os.path.join(root_path, file.getPath())
                            if not listings.exists(file_absolute_path):
                                raise TapelessIngestException(
                                    f"File {file} does not exist ({file_absolute_path})"
                                )
                            # Pre-filter: only providers that may claim this
                            # filename are invoked; each provider's own guard
                            # still decides. A missing or unusable map must
                            # never starve a file — fall back to the whole
                            # list, which is what running unfiltered means.
                            if not extension_map:
                                file_providers = provider_list
                            else:
                                file_providers = applicable_providers(
                                    file.getFileName(), extension_map
                                )
                            metadatas = Clip.extract_file_metadatas(
                                file,
                                file_providers,
                                provider_context,
                            )
                            record["file"] = file
                            record["metadatas"] = metadatas
                        except Exception as e:
                            traceback.print_exc()
                            record["error"] = (
                                f"Error scanning file {result['_source']['path']}: {e}"
                            )
                        records.append(record)

                        response["processed"] += 1
                    # ONE lookup per page for the whole umid set, instead
                    # of one query per file. `created` keeps its pinned
                    # meaning: the umid was absent from this map.
                    umids = [
                        record["metadatas"]["umid"]
                        for record in records
                        if record["error"] is None
                    ]
                    existing_clips = Clip.objects.in_bulk(umids) if umids else {}
                    # Pass 2: assemble the clips, in hit order.
                    for record in records:
                        if record["error"] is not None:
                            response["errors"].append(record["error"])
                            continue
                        try:
                            file = record["file"]
                            metadatas = record["metadatas"]
                            umid = metadatas["umid"]
                            clip = existing_clips.get(umid)
                            if clip is None:
                                clip = invocation_clips.get(umid)
                            created = clip is None
                            if created:
                                clip = Clip(**Clip.new_clip_defaults(file, metadatas))
                            invocation_clips[umid] = clip
                            clip.attach_file_metadatas(file, metadatas)
                            # Hash recovery, gated: only a clip with no
                            # item_id, a hashed file and configured legacy
                            # storages costs a getFilesInStorage call —
                            # and a recovered id lands on the clip whether
                            # its row is new or pre-existing.
                            recovered = clip.recover_item_id(file, legacy_storages)
                            if recovered:
                                recovered_item_ids[umid] = recovered
                            # Seed the clip's memo attributes from the run
                            # context so ingest-time clip.root_path (xdcam)
                            # stops re-resolving — same seam the paged tests
                            # already use (preset _root_path).
                            storage_info = scan_context.storages.get(clip.storage_id)
                            if storage_info is not None:
                                if storage_info.storage is not None:
                                    clip._storage = storage_info.storage
                                if storage_info.root_path:
                                    clip._root_path = storage_info.root_path
                            # FR-12 plan-prep: the sidecar is serialized
                            # into clip_xml BEFORE the row is written, and
                            # only while the column is empty.
                            clip.load_clip_xml(metadatas)
                            if created:
                                response["created"] += 1
                            # FR-23: already-ingested IS item_id presence,
                            # read after recovery.
                            if clip.item_id:
                                response["already_ingested"] += 1
                            if clip.metadatas["provider"] not in providers:
                                providers.append(clip.metadatas["provider"])
                            response["clips"].append(clip)
                            provider_context["clips"].append(clip)
                            candidates.append(
                                ClipCandidate(
                                    umid=umid,
                                    clip=clip,
                                    metadatas=metadatas,
                                    created=created,
                                )
                            )
                        except Exception as e:
                            traceback.print_exc()
                            response["errors"].append(
                                f"Error scanning file "
                                f"{record['result']['_source']['path']}: {e}"
                            )
                    self.provider_names = ",".join(providers)
                    self.scanned_on = timezone.now()
            # The one write unit for this folder (AD-6): every page's
            # clips, one transaction, a bounded number of statements —
            # and the Folder row written at most once per invocation.
            try:
                persist_scan_results(
                    self,
                    build_persistence_plan(
                        candidates,
                        folder_fields={
                            field_name: getattr(self, field_name)
                            for field_name in FOLDER_SCAN_FIELDS
                        },
                        provider_hits=len(providers),
                        recovered_item_ids=recovered_item_ids,
                    ),
                    dry_run=scan_context.options.dry_run,
                )
            except DatabaseError as e:
                # The transaction rolled back: this folder wrote nothing.
                # Reporting it is the whole point — an escaping exception
                # would take the folder (and, in tree mode, the run's
                # remaining work for it) down silently, with the scan's
                # own counters already claiming success.
                traceback.print_exc()
                response["errors"].append(
                    f"Error persisting scan results for {self.path}: {e}"
                )
            # FR-22: story 2.2 recorded every failed scandir on the
            # listings cache and left them unsurfaced. They join this
            # folder's errors, path-sorted so a cron log is deterministic.
            for listing_path, listing_error in sorted(listings.errors().items()):
                response["errors"].append(
                    f"Error listing directory {listing_path}: {listing_error}"
                )
            if complete_pass:
                # `providers` holds the matched provider NAMES this pass
                # accumulated; layer (b) needs the instances behind them.
                matched_names = set(providers)
                matched_providers = [
                    provider
                    for provider in provider_list
                    if getattr(provider, "machine_name", None) in matched_names
                ]
                reasons = []
                try:
                    consumed = consumed_subdirs(
                        response["clips"],
                        matched_providers,
                        # STORAGE-ROOT-RELATIVE, never absolute_path: the
                        # same coordinate system as Clip.path and
                        # VSFile.getPath(). See consumed_subdirs.
                        self.path,
                        reasons=reasons,
                    )
                except Exception as e:
                    # NFR-1 tie-break: anything unexpected here is DOUBT —
                    # never a partial set, and never a crashed folder.
                    traceback.print_exc()
                    consumed = None
                    reasons.append(str(e))
                if consumed is None:
                    reason = reasons[0] if reasons else "unknown reason"
                    response["errors"].append(
                        f"Cannot compute consumed subdirs for {self.path}: {reason}"
                    )
                response["consumed_subdirs"] = consumed
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
        *,
        context=None,
    ):
        if context is not None:
            # A passed context's options are authoritative (the commands
            # pass the same values as kwargs — equal by construction; if a
            # caller's kwargs ever diverge, the context wins by design).
            user = context.options.user
            providers = context.options.providers
            legacy_storages = context.options.legacy_storages
            replace = context.options.replace
            dry_run = context.options.dry_run
        else:
            # Paged mode: build the default context HERE so it carries this
            # call's actual dry_run/replace/user/providers — scan's own
            # default build could not know them and would misreport the
            # options on the provider dict's scan_context.
            context = build_default_context(
                self,
                user=user,
                dry_run=dry_run,
                providers=providers,
                legacy_storages=legacy_storages,
                replace=replace,
            )
        response = self.scan(
            first=first,
            number=number,
            cursor=cursor,
            user=user,
            providers=providers,
            legacy_storages=legacy_storages,
            context=context,
        )
        # scan()'s own dict is returned, so `consumed_subdirs` (story 2.6)
        # reaches the recursion through ingest() unchanged — the ingest
        # counters are added ON TOP of the scan keys, none is replaced.
        for key in ["ingested", "skipped", "failed", "replaced"]:
            response[key] = 0
        if not dry_run:
            # The ladder (AD-15): decide who is worth a Vidispine call
            # BEFORE spending any. `has_hash` comes from the scan-cached
            # file only — Clip.file would buy one getFileById per clip.
            to_ingest, skipped_reasons = select_clips_to_ingest(
                (_ingest_state(clip) for clip in response["clips"]),
                providers=providers,
                replace=replace,
            )
            for clip, reason in skipped_reasons:
                # An item_id-bearing clip counted `skipped` before too —
                # import_file's early return did it, several HTTP calls
                # later. The hash-less one is new (NFR-1): no exception,
                # no legacy match, no ingest, retried next run.
                response["skipped"] += 1
                log.info(f"Skipping clip {clip}: {_skip_reason_message(clip, reason)}")
            if to_ingest:
                # Collection resolution (a VS search/create per path level)
                # is worth its calls only once a clip will actually ingest.
                self.collection_id = self.getCollection(user)
                # The row exists: the scan's write unit wrote it before
                # this point. Targeted update, not a full save (AD-6).
                self.save(update_fields=["collection_id"])
            for clip in to_ingest:
                try:
                    result = clip.ingest(
                        user=user,
                        collection_id=self.collection_id,
                        folder=self,
                        replace=replace,
                        legacy_storages=legacy_storages,
                        # A clip whose previous import left a placeholder
                        # and no job would otherwise hit import_file's
                        # "item already exists" early return and be
                        # skipped forever. This lifts THAT return only —
                        # an item holding real files is still skipped.
                        retry_incomplete=_incomplete_import(clip),
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

import logging
import time
import traceback
import uuid
import os
import re
from typing import Optional, Dict, List, Any
from django.db import DatabaseError, connections, models, transaction

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
from portal.plugins.TapelessIngest.scan.coordinator import (
    FolderOutcome,
    PhaseTimer,
    PoolDispatcher,
    SequentialDispatcher,
    WorkerCounters,
    WorkerResult,
    assert_mode_options,
    build_ingest_response,
    build_scan_response,
    fold_timings,
    merge_results,
    summary_lines,
    walk_tree,
)
from portal.plugins.TapelessIngest.scan.extraction import (
    applicable_providers,
    consumed_subdirs,
    unclaimed_hits_doubt,
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


def providers_for_worker(ctx):
    """The provider registry THIS worker must use (AD-7).

    Story 3.1 kept this the SHARED run registry, by proof rather than by
    per-worker instantiation: Epic 2 moved per-run provider state off
    ``self`` and into the per-invocation ``provider_context``, and the
    tier-1 guard (``tests/tier1/test_provider_write_once.py``) AST-scans
    every registry provider class for a ``self.<attr>`` write outside
    ``__init__`` and fails on any hit — so the shared instances are
    write-once and thread-safe. Per-worker instances were rejected for a
    load-bearing reason: ``ExtensionMap`` ranks providers by
    ``id(provider)``, so fresh instances would silently require
    per-worker extension maps too. (``jvcprohd`` really does mutate
    itself, and is outside the registry; see the guard's docstring.)
    """
    return ctx.provider_registry


def _folder_worker(folder, ctx, *, first, number, cursor, count_only, ingest):
    """Run one folder's pipeline and freeze it into a ``FolderOutcome``.

    The body BOTH entry modes execute: tree mode reaches it through
    ``process_folder`` (which opens its own ``Folder`` first), the paged
    façades call it with the instance they were invoked on, so a
    Portal/UI caller keeps every side effect the pre-2.8 ``scan()`` left
    on it (``provider_names``, ``scanned_on``, ``clips_total``,
    ``collection_id`` — ``views.py`` serializes the folder afterwards).

    It may RAISE: the paged façades must keep failing where they always
    failed, and it is ``process_folder`` — the tree-mode wrapper — that
    owns the never-raising contract.
    """
    timer = PhaseTimer()
    if ingest:
        response = folder._ingest_pass(
            ctx, first=first, number=number, cursor=cursor, timer=timer
        )
    else:
        response = folder._scan_pass(
            ctx,
            first=first,
            number=number,
            cursor=cursor,
            count_only=count_only,
            timer=timer,
        )
    errors = tuple(response["errors"])
    counters = WorkerCounters(
        hits=response["hits"],
        created=response["created"],
        already_ingested=response["already_ingested"],
        processed=response["processed"],
        ingested=response.get("ingested", 0),
        skipped=response.get("skipped", 0),
        failed=response.get("failed", 0),
        replaced=response.get("replaced", 0),
    )
    log_lines = ()
    # FR-22: log whenever there is anything to report. Before 2.6 the whole
    # line was gated on hits > 0, so a zero-hit folder's errors were
    # swallowed together with it. The line is BUILT here and EMITTED by the
    # coordinator (AD-12), which is what makes ordered draining possible.
    if counters.hits > 0 or errors:
        error_message = ""
        if errors:
            error_string = "\n".join(errors)
            error_message += f": {error_string}"
        log_lines = (
            f"found {counters.hits} clips in {folder.path}, "
            f"{counters.already_ingested} already ingested, "
            f"{counters.created} created, providers are {folder.provider_names}, "
            f"{counters.ingested} ingested, {counters.skipped} skipped, "
            f"{counters.replaced} replaced, {len(errors)} errors "
            f"encountered{error_message}",
        )
    return FolderOutcome(
        result=WorkerResult(
            folder_path=folder.path,
            clips=tuple(response["clips"]),
            counters=counters,
            errors=errors,
            timings=timer.freeze(),
            log_lines=log_lines,
        ),
        # `.get()`, not `[...]`: a response that cannot say what it
        # consumed must never be trusted to authorize a descent (2.6).
        consumed_subdirs=response.get("consumed_subdirs"),
        # Per-file errors never fail a FOLDER — today's `count += 1` counted
        # a zero-hit folder with two unreadable files as scanned.
        failed=False,
        # The worker's own directory cache, so the walk does not scandir
        # this folder a second time to find its children. The coordinator
        # drops it as soon as it has expanded them.
        listings=response.get("_listings"),
    )


def _failed_folder_outcome(path, message):
    """The zero-counter outcome a caught folder-boundary exception yields.

    The template lands in BOTH ``errors`` and ``log_lines``: the operator's
    cron/Slack report is built from log lines, the error accounting from
    errors, and before 2.8 the same string served both. ``log.error(...,
    exc_info=True)`` is ADDITIONAL — a traceback in portal.log is not a
    substitute for the line the operator actually reads.
    """
    log.error(message, exc_info=True)
    return FolderOutcome(
        result=WorkerResult(
            folder_path=path,
            errors=(message,),
            log_lines=(message,),
        ),
        # DOUBT: a folder that died can say nothing about what it consumed.
        consumed_subdirs=None,
        failed=True,
    )


def process_folder(
    storage_id,
    path,
    ctx,
    *,
    first=0,
    number=25,
    cursor=None,
    count_only=False,
    ingest=False,
):
    """One folder, end to end — and it NEVER raises.

    Module-level rather than a bound method because the coordinator that
    calls it is ORM-free by contract: it hands over a ``(storage_id,
    path)`` pair and this function opens the ``Folder`` on the model side
    of the boundary. Story 3.1's pool wraps exactly this call —
    ``_process_folder_with_connection_hygiene`` below — with per-worker
    connection hygiene, which is why the ORM must not cross it.

    ``ingest`` selects the scan or ingest SHAPE; whether ingestion writes
    anything stays governed by ``ctx.options.dry_run``.

    Only ``Exception`` is caught. ``BaseException`` — ``KeyboardInterrupt``,
    ``SystemExit`` — deliberately propagates, so an operator can still
    stop a run that is doing the wrong thing to 8,000 folders.
    """
    try:
        folder, _is_new = Folder.get_or_new(storage_id=storage_id, path=path)
        # Seed the memoized root from the run context BEFORE any property
        # access, so neither this folder's scan nor the walk's own listing
        # re-resolves the storage (real once-per-run, FR-7).
        root_path = ctx.root_path_for(storage_id)
        if root_path:
            folder._root_path = root_path
        return _folder_worker(
            folder,
            ctx,
            first=first,
            number=number,
            cursor=cursor,
            count_only=count_only,
            ingest=ingest,
        )
    except FileNotFoundError:
        # Formatted from the (storage_id, path) this worker was HANDED.
        # The pre-2.8 handler read `folder.path` from a name that is
        # unbound whenever get_or_new itself was what raised — one folder
        # taking the run down with a NameError inside the error path.
        return _failed_folder_outcome(path, f"Path doesn't exists anymore: {path}")
    except TapelessIngestException as e:
        return _failed_folder_outcome(path, f"Error ingesting {path}: {e}")
    except Exception as e:
        return _failed_folder_outcome(path, f"Error scanning {path}: {e}")


def _process_folder_with_connection_hygiene(
    storage_id,
    path,
    ctx,
    *,
    first,
    number,
    cursor,
    count_only,
    ingest,
):
    """``process_folder`` plus the per-worker connection hygiene (AD-5).

    The pool-mode worker body, and ONLY the pool's: ``workers=1`` keeps
    calling ``process_folder`` bare, so the sequential path is
    byte-identical to 2.8's. The parameters are mandatory on purpose —
    the walk always passes all five (``number=0`` is the loop-all-pages
    sentinel), and a default here (the first cut carried ``number=25``)
    could only mislead a reader about what a pool worker actually runs.

    Each pool worker runs on a thread of its own, and Django connections
    are per-thread — closing them in a ``finally`` (per task, success or
    failure alike) means no worker thread ever holds a stale connection
    across folders, and the pool's threads leave nothing open behind
    them. The main thread's connection is untouched: ``close_all()``
    only closes the CALLING thread's connections. Trade-off, accepted
    for the AC's literal wording: per-FOLDER close means per-folder
    reconnect on the next folder the thread picks up; if that cost ever
    shows, ``close_old_connections()`` + ``CONN_MAX_AGE`` is the
    optimization (recorded in deferred-work.md).

    The close itself is guarded: ``process_folder`` never raises, so an
    exception out of this ``finally`` would CLOBBER a perfectly good
    folder outcome into a `_walk_failure` — a close hiccup is worth a
    warning in portal.log, never a failed folder.
    """
    try:
        return process_folder(
            storage_id,
            path,
            ctx,
            first=first,
            number=number,
            cursor=cursor,
            count_only=count_only,
            ingest=ingest,
        )
    finally:
        try:
            connections.close_all()
        except Exception:
            log.warning(
                f"per-worker connection close failed after {path}; the "
                f"folder's outcome is kept",
                exc_info=True,
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
        """Paged mode (AD-14): today's signature, today's response.

        A thin rebuilder since story 2.8 — resolve the context, run the one
        shared worker body, rebuild the frozen NFR-5 response from its
        ``WorkerResult``. Keys, templates and types are unchanged, and the
        values are LISTS in order, exactly as the story-1.3 pins assert.
        """
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
        # AD-14's policeable half, immediately after ctx resolution.
        try:
            assert_mode_options(scan_context.options, "paged")
        except ValueError as e:
            raise TapelessIngestException(str(e)) from e
        return build_scan_response(
            _folder_worker(
                self,
                scan_context,
                first=first,
                number=number,
                cursor=cursor,
                count_only=count_only,
                ingest=False,
            )
        )

    def _scan_pass(self, scan_context, *, first, number, cursor, count_only, timer):
        """The scan pipeline over one folder, timed. Raises.

        A passed context's options are AUTHORITATIVE. Before 2.8 the
        commands passed the same values as kwargs as well (equal by
        construction); with the walk relocated into the coordinator the
        context is the only channel left, so there is nothing to diverge.

        ``timer`` is this folder's own accumulator — never the run's shared
        ``ctx.timings``, which has exactly one writer.
        """
        requested_providers = scan_context.options.providers
        legacy_storages = scan_context.options.legacy_storages
        # The context carries the registry and its extension map. When the
        # registry could not be built (an unresolvable provider name), this
        # fallback is where that name raises, as it always has.
        provider_list = providers_for_worker(scan_context)
        if provider_list is None:
            provider_list = Clip._get_provider_list(requested_providers)
        extension_map = scan_context.extension_map
        # Two DIFFERENT things, and before this round they shared the name
        # `providers`: the run's requested provider FILTER (above) and the
        # provider NAMES this pass actually saw claim a clip (below, the
        # `Folder.providers` column and the log line). One name for two
        # opposite meanings is how a reader mistakes the second for the
        # first — which is exactly the confusion that let layer (b) be fed
        # from a last-writer-wins name.
        matched_provider_names = []
        # The provider INSTANCES that contributed to any clip in this
        # folder, registry-ordered and deduplicated by identity. This is
        # what consumed_subdirs' layer (b) needs: a file can legitimately
        # be claimed by SEVERAL providers (AD-7/FR-14 — xdcam plus exif is
        # the standing example), and `metadatas["provider"]` records only
        # the last one to write it, so deriving the matched set from that
        # name silently dropped every co-matching provider's sub-paths and
        # left their directories open to descent.
        matched_providers = []
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
        #
        # Loop EXIT is not the same thing as completeness, which is what
        # this used to rely on: the page loop also ends when a page comes
        # back SHORT, which is what a truncated or shifting index looks
        # like. That left an incomplete consumed set authorizing descent —
        # under-consumption, the duplicate direction. `seen_hits` below
        # counts what was actually consumed and the check moves to the end.
        may_be_complete = not count_only and number == 0 and first == 0
        seen_hits = 0
        # Files that PASSED the real-filesystem guard and reached
        # extraction. Deliberately not `seen_hits`: a ghost row (index
        # entry, no file behind it) is a hit the DC-2 guard absorbs by
        # design, and counting it would make an ordinary desync look like
        # a folder whose contents are unknown. See unclaimed_hits_doubt.
        verified_hits = 0
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
                # The `discovery` phase, ACCUMULATING across every page of
                # this folder — Epic 4 replaces what happens inside it.
                with timer("discovery"):
                    search_result = query_elastic(
                        search_doc,
                        doc_type=["file"],
                        first=first,
                        number=result_number,
                    )
                hits = search_result["hits"]["hits"]
                response["hits"] = search_result["hits"]["total"]["value"]
                self.clips_total = response["hits"]
                seen_hits += len(hits)
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
                            with timer("verification"):
                                verified = listings.exists(file_absolute_path)
                            if not verified:
                                raise TapelessIngestException(
                                    f"File {file} does not exist ({file_absolute_path})"
                                )
                            verified_hits += 1
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
                            # `matched` is an out-parameter: every provider
                            # that CONTRIBUTED to this file, not just the
                            # last one to write `metadatas["provider"]`.
                            matched = []
                            with timer("extraction"):
                                metadatas = Clip.extract_file_metadatas(
                                    file,
                                    file_providers,
                                    provider_context,
                                    matched=matched,
                                )
                            record["file"] = file
                            record["metadatas"] = metadatas
                            record["matched"] = matched
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
                            if clip.metadatas["provider"] not in matched_provider_names:
                                matched_provider_names.append(
                                    clip.metadatas["provider"]
                                )
                            # Layer (b)'s real input: the union of every
                            # provider that contributed to any clip here,
                            # by IDENTITY (two instances of one class must
                            # rank separately, as ExtensionMap already
                            # assumes).
                            for provider in record["matched"]:
                                if not any(
                                    known is provider for known in matched_providers
                                ):
                                    matched_providers.append(provider)
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
                    self.provider_names = ",".join(matched_provider_names)
                    self.scanned_on = timezone.now()
            # The listings cache is handed back to the coordinator so the
            # walk can reuse it for subdirectory discovery instead of
            # scanning this directory a second time (and reporting the same
            # failure twice). Private key: the façades never see it —
            # `build_scan_response` rebuilds from the WorkerResult.
            response["_listings"] = listings
            # The one write unit for this folder (AD-6): every page's
            # clips, one transaction, a bounded number of statements —
            # and the Folder row written at most once per invocation.
            persistence_failed = False
            try:
                with timer("persistence"):
                    persist_scan_results(
                        self,
                        build_persistence_plan(
                            candidates,
                            folder_fields={
                                field_name: getattr(self, field_name)
                                for field_name in FOLDER_SCAN_FIELDS
                            },
                            provider_hits=len(matched_provider_names),
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
                persistence_failed = True
                response["errors"].append(
                    f"Error persisting scan results for {self.path}: {e}"
                )
            # FR-22: story 2.2 recorded every failed scandir on the
            # listings cache and left them unsurfaced. They join this
            # folder's errors, path-sorted so a cron log is deterministic.
            #
            # `errors()` excludes the directories only a speculative
            # provider probe ever asked for and that turned out not to
            # exist — a card provider looking for a layout this card does
            # not have. Reporting those made the error COUNT worthless
            # (one phantom line per non-matching card folder, thousands a
            # week); they stay visible at DEBUG. Everything else is
            # reported, a probe into an unreadable directory included.
            probe_absences = listings.probe_absences()
            if probe_absences:
                log.debug(
                    "%s: %d sidecar probe(s) found no directory (not errors): %s",
                    self.path,
                    len(probe_absences),
                    ", ".join(sorted(probe_absences)),
                )
            for listing_path, listing_error in sorted(listings.errors().items()):
                response["errors"].append(
                    f"Error listing directory {listing_path}: {listing_error}"
                )
            if persistence_failed:
                # NFR-1. A rolled-back write unit means this folder has NO
                # rows — so:
                #  * the clips must not travel on. `_ingest_pass` would
                #    otherwise submit them, and `Clip.ingest`'s
                #    `persist_ingest_state` would fall back to a full
                #    `save()` — writing rows through the path AD-6 exists
                #    to avoid, and, far worse, creating a Vidispine item
                #    whose `item_id` no row records. The next run would
                #    then ingest the same clip again;
                #  * and descent is NOT authorized. A folder that did not
                #    record what it found cannot vouch for what it
                #    consumed, and descending on that would be the
                #    under-consuming direction.
                response["clips"] = []
                response["consumed_subdirs"] = None
                response["errors"].append(
                    f"Not descending into {self.path}: its scan results were "
                    f"not written, so what its clips consumed is unknown"
                )
            elif may_be_complete and seen_hits >= response["hits"]:
                # Loop exit alone is not completeness (a SHORT page ends the
                # loop too); this folder authorized descent only after
                # actually consuming every hit the index reported.
                #
                # Before asking WHAT the clips consumed, ask whether this
                # folder produced an answer worth trusting. A card folder
                # whose extractor broke reaches here with hits it failed to
                # claim and zero clips, which `consumed_subdirs` would
                # answer with an empty set — "descend into everything" —
                # and the walk would go through the card's internals.
                # Measured on the REDline outage: 118 folders where a
                # correct run reports 49.
                doubt = unclaimed_hits_doubt(
                    verified_hits, len(response["clips"]), len(response["errors"])
                )
                if doubt:
                    response["consumed_subdirs"] = None
                    response["errors"].append(
                        f"Not descending into {self.path}: {doubt}"
                    )
                else:
                    reasons = []
                    try:
                        with timer("extraction"):
                            consumed = consumed_subdirs(
                                response["clips"],
                                # The per-clip matched provider INSTANCES,
                                # not the last-writer-wins names: several
                                # providers legitimately claim one file
                                # (AD-7/FR-14).
                                matched_providers,
                                # STORAGE-ROOT-RELATIVE, never
                                # absolute_path: the same coordinate system
                                # as Clip.path and VSFile.getPath(). See
                                # consumed_subdirs.
                                self.path,
                                reasons=reasons,
                            )
                    except Exception as e:
                        # NFR-1 tie-break: anything unexpected here is
                        # DOUBT — never a partial set, and never a crashed
                        # folder.
                        traceback.print_exc()
                        consumed = None
                        reasons.append(str(e))
                    if consumed is None:
                        reason = reasons[0] if reasons else "unknown reason"
                        response["errors"].append(
                            f"Cannot compute consumed subdirs for {self.path}: {reason}"
                        )
                    response["consumed_subdirs"] = consumed
            elif may_be_complete:
                # The pages stopped short of the reported total.
                response["errors"].append(
                    f"Not descending into {self.path}: the index reported "
                    f"{response['hits']} files but only {seen_hits} were "
                    f"returned, so what its clips consumed is unknown"
                )
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
        """Paged mode (AD-14): the scan keys plus the four ingest counters.

        A thin rebuilder since story 2.8, like ``scan``.
        """
        if context is None:
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
        # AD-14's policeable half, immediately after ctx resolution.
        try:
            assert_mode_options(context.options, "paged")
        except ValueError as e:
            raise TapelessIngestException(str(e)) from e
        return build_ingest_response(
            _folder_worker(
                self,
                context,
                first=first,
                number=number,
                cursor=cursor,
                count_only=False,
                ingest=True,
            )
        )

    def _ingest_pass(self, context, *, first, number, cursor, timer):
        """The scan pipeline plus the 2.5 ladder and the submission leg.

        A passed context's options are authoritative — see ``_scan_pass``.
        """
        user = context.options.user
        providers = context.options.providers
        legacy_storages = context.options.legacy_storages
        replace = context.options.replace
        dry_run = context.options.dry_run
        response = self._scan_pass(
            context,
            first=first,
            number=number,
            cursor=cursor,
            count_only=False,
            timer=timer,
        )
        # scan()'s own dict is returned, so `consumed_subdirs` (story 2.6)
        # reaches the recursion through ingest() unchanged — the ingest
        # counters are added ON TOP of the scan keys, none is replaced.
        for key in ["ingested", "skipped", "failed", "replaced"]:
            response[key] = 0
        # The `ingest` phase: the ladder, the collection resolution it
        # gates, and the submission leg. The ladder is timed with them
        # deliberately — it is what a dry run's ingest phase CONSISTS of,
        # and a rehearsal reporting 0.0s would be lying about its cost.
        with timer("ingest"):
            # The ladder (AD-15): decide who is worth a Vidispine call
            # BEFORE spending any. `has_hash` comes from the scan-cached
            # file only — Clip.file would buy one getFileById per clip.
            #
            # Story 2.7: the ladder is PURE, so it runs in both modes and
            # its verdict is counted in both. A dry run is a rehearsal that
            # reports what a real run would do, not a mute one.
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
            if dry_run:
                # WOULD-BE ingests. Every ladder-selected clip is one this
                # run would have submitted — replacements included, because
                # under dry-run there is no replacement OUTCOME to report:
                # nothing was replaced. `failed`/`replaced` and the share of
                # `skipped` that `Clip.import_file` decides on its own stay
                # 0 for the same structural reason — no submission occurred.
                # So dry-run `ingested` is an UPPER bound on a real run's
                # and dry-run `skipped` a LOWER bound; they coincide exactly
                # when every submission succeeds.
                response["ingested"] += len(to_ingest)
            else:
                if to_ingest:
                    # Collection resolution (a VS search/create per path
                    # level) is worth its calls only once a clip will
                    # actually ingest.
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
                        # A clip whose submission raised is a FAILED clip
                        # (story 3.0): before, it was counted nowhere —
                        # not ingested, not skipped, not failed — so the
                        # summary's arithmetic quietly lost it. The
                        # unresolvable-item refusal (TapelessIngestException
                        # from create_item) lands here too, by design.
                        response["failed"] += 1
                        response["errors"].append(f"Error ingesting clip {clip}: {e}")

        return response

    def scan_tree(self, ctx, *, emit):
        """Tree mode's entry point — and the coordinator's (AD-12/AD-14).

        This IS the coordinator: it brackets the run with
        ``time.monotonic()``, runs the walk — which drains every folder's
        log lines through ``emit`` in merge-key order AS THEY BECOME
        RELEASABLE, so an 8,000-folder run reports as it goes instead of
        printing nothing for hours — folds the merged timings into
        ``ctx.timings`` (the only writer of that slot) and emits the
        summary. ``handle()`` does none of it — it calls this and then
        makes its single Slack call.

        ``emit`` is keyword-only and deliberately NOT a field on
        ``ScanContext``: workers receive ``ctx``, so keeping the sink off
        it makes worker emission structurally impossible rather than
        merely forbidden.

        There is no pagination surface here, which is the structural form
        of AD-14's "pagination params are illegal in tree mode" —
        ``number=0`` inside the walk is the loop-all-pages sentinel, not a
        pagination argument.

        Returns the merged ``RunResult``, never a tuple.
        """
        try:
            assert_mode_options(ctx.options, "tree")
        except ValueError as e:
            raise TapelessIngestException(str(e)) from e
        if ctx.options.user is None:
            # The pre-2.8 walk logged "User has to be provided" and carried
            # on, so a real run reaching the pipeline with no user failed
            # per clip, deep inside `Clip.ingest`, with no explanation. A
            # run that will WRITE fails fast instead: `getCollection(user)`
            # and every `clip.ingest(user=...)` need one. A dry run
            # genuinely does not — it submits nothing — so it keeps the
            # warning and rehearses.
            if ctx.options.dry_run:
                emit("User has to be provided (dry run: nothing would be submitted)")
            else:
                raise TapelessIngestException(
                    "User has to be provided: an ingesting run cannot resolve "
                    "a collection or submit a clip without one"
                )
        started = time.monotonic()
        # The fan-out seam (story 3.1). `workers > 1` swaps the dispatcher
        # for a real pool and wraps the worker body with per-thread
        # connection hygiene — the two bound methods and the worker are
        # the WHOLE substitution: the walk, the phases and the merge are
        # identical, and the tree-derived merge key already makes the
        # output independent of completion order. `workers=1` never
        # constructs an executor and stays the sequential code path.
        workers = getattr(ctx.options, "workers", 1) or 1
        if workers > 1:
            dispatcher = PoolDispatcher(max_workers=workers)
            folder_worker = _process_folder_with_connection_hygiene
        else:
            dispatcher = SequentialDispatcher()
            folder_worker = process_folder
        try:
            outcomes = walk_tree(
                self.storage_id,
                self.path,
                ctx=ctx,
                process_folder=folder_worker,
                dispatch=dispatcher.dispatch,
                gather=dispatcher.gather,
                emit=emit,
            )
        except BaseException:
            # The run is dying — a raising emit, or the operator's
            # Ctrl-C. Release the pool WITHOUT waiting: stalling a
            # KeyboardInterrupt behind in-flight folders is exactly the
            # un-stoppable run process_folder's BaseException pass-through
            # exists to prevent. Queued folders are dropped either way.
            if workers > 1:
                dispatcher.shutdown(wait=False)
            raise
        if workers > 1:
            # Normal completion waits: every future was already gathered,
            # so this only reaps idle threads.
            dispatcher.shutdown()
        # `walk_tree` already emitted every folder line, incrementally and
        # in merge-key order; `run_result.log_lines` is the same sequence
        # kept for callers that want it as data. Emitting it here too would
        # print the whole run twice.
        run_result = merge_results(outcomes)
        fold_timings(ctx, run_result.timings)
        for line in summary_lines(run_result, ctx, time.monotonic() - started):
            emit(line)
        return run_result

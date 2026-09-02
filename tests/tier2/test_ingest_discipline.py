"""Tier 2 (story 2.5): Vidispine call discipline and honest ingest counters.

The consequences of ``scan/ingestion.py``'s ladder, end to end through a
real ``scan()``/``ingest()``:

- an already-ingested clip costs ZERO Vidispine calls — no hash lookup on
  the scan side, no collection resolution and no ``import_file`` on the
  ingest side (FR-8, FR-23, AD-15);
- a recovered ``item_id`` lands on a PRE-EXISTING clip row too (ratified);
- a hash-less file is skipped with a retry-next-run reason, never matched
  against a legacy storage and never ingested (NFR-1) — where it used to
  raise ``TapelessIngestException`` and lose its scan record;
- the collection is resolved once, only when at least one clip will
  really ingest;
- an import response without a job id is ``failed``, never ``ingested``
  (FR-36);
- ``Clip.ingest`` writes ingest-state columns and nothing else (AD-6
  writer 2).

``Folder.getCollection`` is replaced with a local double throughout: it
is PLUGIN code (not Portal — AD-11 is untouched) and it needs a Settings
row plus a live search backend, neither of which says anything about the
ladder. ``Clip.import_file`` is doubled only where the test is about the
COUNTERS; the tests under "the real import path" run it for real against
the portal_stub Vidispine doubles, because a verdict nothing executes is
a verdict nothing pins.
"""

import logging
import os
import re
import time

import pytest
from django.db import DatabaseError, connection
from django.test.utils import CaptureQueriesContext

from VidiRest.objects.storage import VSFile

from portal.vidispine.iexception import NotFoundError

from portal.plugins.TapelessIngest.models import clip as clip_module
from portal.plugins.TapelessIngest.models import folder as folder_module
from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder, persist_scan_results
from portal.plugins.TapelessIngest.providers.providers import (
    MAIN_FILE_YIELDS_VIDEO,
    Provider as BaseProvider,
)
from portal.plugins.TapelessIngest.scan.persistence import build_persistence_plan
from portal.plugins.TapelessIngest.serializers import ClipSerializer

from tests.portal_stub import (
    ComponentBudgetExceeded,
    InjectedVidispineFault,
    ItemAPIFake,
    JobHelperFake,
    UnknownShape,
    VidispineFake,
)

STORAGE_ID = "VX-41"
LEGACY_STORAGE = "VX-LEGACY"
MISLABELLED_NAME = "fakemislabelled"
FIXED_UMID_NAME = "fakefixedumid2"
FIXED_UMID = "FIXED-UMID-INGEST"
INGESTABLE_NAME = "fakeingestable"


class MislabelledProvider:
    """Registered under one name, stamps another ``provider`` on its clips.

    The only way to produce a clip the run's provider filter rejects: the
    filter compares ``clip.provider_name`` (which comes from the
    metadatas) against the run's ``--providers`` names.
    """

    machine_name = MISLABELLED_NAME

    def getExtensions(self):
        return [".fake"]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        metadatas["provider"] = "someotherprovider"
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas


@pytest.fixture
def mislabelled_provider():
    provider = MislabelledProvider()
    Clip._PROVIDER_CACHE[MISLABELLED_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(MISLABELLED_NAME, None)


class FixedUmidProvider(MislabelledProvider):
    """Every file it claims is the SAME clip — the duplicate-umid case."""

    machine_name = FIXED_UMID_NAME

    def getMetadatasFromFile(self, media_file, metadatas, context):
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = FIXED_UMID
        return metadatas


@pytest.fixture
def fixed_umid_provider():
    provider = FixedUmidProvider()
    Clip._PROVIDER_CACHE[FIXED_UMID_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(FIXED_UMID_NAME, None)


class IngestableProvider(BaseProvider):
    """A provider whose clips can go through the REAL ``import_file``.

    Subclasses the base so ``_createDictFromMetadataMapping`` (a plain
    MetadataMapping query) is the real one; the media-file accessors are
    the minimum ``import_file`` reads.
    """

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "Fake Ingestable Provider"
        self.machine_name = INGESTABLE_NAME

    def getExtensions(self):
        return [".ing"]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas

    def getClipMainMediaFile(self, clip):
        return {"file_id": clip.file_id, "path": clip.path, "type": "video"}

    def getClipAdditionalMediaFiles(self, clip):
        return []

    def getImportOptions(self):
        return {}


@pytest.fixture
def ingestable_provider():
    provider = IngestableProvider()
    Clip._PROVIDER_CACHE[INGESTABLE_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(INGESTABLE_NAME, None)


def _source(path, file_id, file_hash=None):
    return {
        "path": path,
        "hash": f"hash-{file_id}" if file_hash is None else file_hash,
        "storage": STORAGE_ID,
        "id": file_id,
        "size": 1024,
    }


def _folder(tmp_path, rel_path):
    folder = Folder(storage_id=STORAGE_ID, path=rel_path)
    # Presetting _root_path bypasses StorageHelper/cache entirely.
    folder._root_path = str(tmp_path)
    return folder


def _write_clips(tmp_path, rel, names, hashes=None, suffix=".fake"):
    (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    sources = []
    for name in names:
        (tmp_path / rel / f"{name}{suffix}").write_bytes(b"clip data")
        sources.append(
            _source(
                f"{rel}/{name}{suffix}",
                f"VX-41-{name}",
                file_hash=(hashes or {}).get(name),
            )
        )
    return sources


def _rescanned_folder(tmp_path, rel):
    """The production re-scan path: fetch the row the first scan wrote."""
    folder, is_new = Folder.get_or_new(storage_id=STORAGE_ID, path=rel)
    assert is_new is False
    folder._root_path = str(tmp_path)
    return folder


def _folder_writes(captured):
    return [
        query["sql"]
        for query in captured
        if "TapelessIngest_folder" in query["sql"]
        and query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE"))
    ]


@pytest.fixture
def collection_seam(monkeypatch):
    """Count ``Folder.getCollection`` without resolving a real collection."""
    calls = []

    def fake_get_collection(self, user, dryrun=False):
        calls.append(self.path)
        return "VX-COLLECTION"

    monkeypatch.setattr(Folder, "getCollection", fake_get_collection)
    return calls


@pytest.fixture
def ingest_seams(monkeypatch, collection_seam):
    """Count the two plugin seams ``Folder.ingest`` drives.

    The doubled ``import_file`` writes the ingest state a real import
    would, so ``Clip.ingest``'s targeted UPDATE is still exercised for
    real. Tests about the import VERDICT use the real thing instead —
    see "the real import path" below.
    """
    calls = {"collection": collection_seam, "import_file": []}
    result = {"skipped": False, "failed": False, "replaced": False, "ingested": True}

    def fake_import_file(
        self,
        collection_id=None,
        user=None,
        replace=False,
        legacy_storages=None,
        retry_incomplete=False,
        component_wait_seconds=None,
        component_wait_deadline=None,
    ):
        calls["import_file"].append(self.umid)
        if result["ingested"]:
            self.item_id = self.item_id or f"VX-ITEM-{len(calls['import_file'])}"
            self.job_id = f"VX-JOB-{len(calls['import_file'])}"
            self.status = Clip.STATUS_PLACHOLDER_CREATED
        return dict(result)

    monkeypatch.setattr(Clip, "import_file", fake_import_file)
    calls["result"] = result
    return calls


# --------------------------------------------------------------------------
# (a) Hash recovery: gated, and assigned to pre-existing rows
# --------------------------------------------------------------------------


def _vsfile(path, file_id, file_hash="hash-1"):
    return VSFile(_source(path, file_id, file_hash=file_hash), {})


def test_existing_item_id_costs_no_hash_lookup(
    migrated_db, fake_provider, storage_fake
):
    """The story's headline: a re-scan of an ingested clip is HTTP-free.

    Pre-2.5 the legacy-storage loop ran BEFORE the clip was looked up, so
    this clip paid one getFilesInStorage per legacy storage, every run,
    forever.
    """
    umid = "2026/AH_20260101_recov/CLIPSEEN"
    Clip(umid=umid, item_id="VX-100").save()

    clip, created = Clip.get_clip_from_file(
        _vsfile(f"{umid}.fake", "VX-41-SEEN"),
        [fake_provider],
        {},
        legacy_storages=[LEGACY_STORAGE],
    )

    assert (created, clip.item_id) == (False, "VX-100")
    assert storage_fake.get_files_in_storage_calls == []


def test_recovery_assigns_the_item_id_to_a_pre_existing_row(
    migrated_db, fake_provider, storage_fake
):
    """RATIFIED: recovery is not an insert-path-only privilege.

    Recovery fires only when the clip has NO item_id, so it can never
    overwrite a known one — which is what makes assigning it to an
    existing row safe.
    """
    umid = "2026/AH_20260101_recov/CLIPOLD"
    Clip(umid=umid).save()
    storage_fake.set_hash_item("hash-old", "VX-500")

    clip, created = Clip.get_clip_from_file(
        _vsfile(f"{umid}.fake", "VX-41-OLD", file_hash="hash-old"),
        [fake_provider],
        {},
        legacy_storages=[LEGACY_STORAGE],
    )

    assert created is False
    assert clip.item_id == "VX-500"
    assert storage_fake.get_files_in_storage_calls == [(LEGACY_STORAGE, "hash-old")]


def test_hash_less_file_is_never_matched_against_legacy_storages(
    migrated_db, fake_provider, storage_fake
):
    """NFR-1: no dedup key, no lookup — and no exception either.

    Pre-2.5 this raised TapelessIngestException("No hash found in file"),
    which cost the file its clip, its metadata and its scan record.
    """
    umid = "2026/AH_20260101_recov/CLIPNOHASH"

    clip, created = Clip.get_clip_from_file(
        _vsfile(f"{umid}.fake", "VX-41-NOHASH", file_hash=""),
        [fake_provider],
        {},
        legacy_storages=[LEGACY_STORAGE],
    )

    assert (created, clip.item_id) == (True, None)
    assert storage_fake.get_files_in_storage_calls == []


def test_recovery_without_legacy_storages_makes_no_call(
    migrated_db, fake_provider, storage_fake
):
    umid = "2026/AH_20260101_recov/CLIPNOLEGACY"

    clip, _ = Clip.get_clip_from_file(
        _vsfile(f"{umid}.fake", "VX-41-NOLEG"), [fake_provider], {}, legacy_storages=[]
    )

    assert clip.item_id is None
    assert storage_fake.get_files_in_storage_calls == []


def test_a_failing_hash_lookup_never_costs_the_clip(
    migrated_db, fake_provider, storage_fake
):
    """The storage-by-storage try/except survives the reorder."""
    umid = "2026/AH_20260101_recov/CLIPERR"
    storage_fake.set_hash_error("hash-err", storage_id="VX-BROKEN")
    storage_fake.set_hash_item("hash-err", "VX-600", storage_id=LEGACY_STORAGE)

    clip, _ = Clip.get_clip_from_file(
        _vsfile(f"{umid}.fake", "VX-41-ERR", file_hash="hash-err"),
        [fake_provider],
        {},
        legacy_storages=["VX-BROKEN", LEGACY_STORAGE],
    )

    # The broken storage was tried, logged and stepped over.
    assert clip.item_id == "VX-600"
    assert storage_fake.get_files_in_storage_calls == [
        ("VX-BROKEN", "hash-err"),
        (LEGACY_STORAGE, "hash-err"),
    ]


def test_scan_recovers_and_counts_it_already_ingested(
    migrated_db, es_fake, es_page, fake_provider, storage_fake, tmp_path
):
    """The whole page path: one lookup for the new clip, none for the known one."""
    rel = "2026/AH_20260101_scanrecov"
    sources = _write_clips(
        tmp_path, rel, ["CLIPFOUND", "CLIPSEEN"], hashes={"CLIPFOUND": "hash-found"}
    )
    Clip(umid=f"{rel}/CLIPSEEN", item_id="VX-100").save()
    storage_fake.set_hash_item("hash-found", "VX-700")
    es_fake.push(es_page(sources, total=2))

    response = _folder(tmp_path, rel).scan(
        providers=[fake_provider.machine_name], legacy_storages=[LEGACY_STORAGE]
    )

    assert response["errors"] == []
    # Only the clip with no item_id was looked up.
    assert storage_fake.get_files_in_storage_calls == [(LEGACY_STORAGE, "hash-found")]
    found, seen = response["clips"]
    assert (found.item_id, seen.item_id) == ("VX-700", "VX-100")
    # FR-23: both carry an item_id post-recovery, so both count.
    assert response["already_ingested"] == 2
    assert response["created"] == 1


# --------------------------------------------------------------------------
# (b) The hash-less file, end to end: skipped, logged, retried next run
# --------------------------------------------------------------------------


def test_hash_less_file_is_skipped_with_a_retry_reason(
    migrated_db, es_fake, es_page, fake_provider, storage_fake, tmp_path, caplog
):
    rel = "2026/AH_20260101_nohash"
    sources = _write_clips(tmp_path, rel, ["CLIPNOHASH"], hashes={"CLIPNOHASH": ""})
    es_fake.push(es_page(sources, total=1))

    with caplog.at_level("INFO"):
        response = _folder(tmp_path, rel).ingest(
            providers=[fake_provider.machine_name], legacy_storages=[LEGACY_STORAGE]
        )

    # Scanned normally — it is a retry, not an error.
    assert response["errors"] == []
    assert (response["processed"], response["created"]) == (1, 1)
    assert (
        response["skipped"],
        response["ingested"],
        response["failed"],
        response["replaced"],
    ) == (1, 0, 0, 0)
    assert response["already_ingested"] == 0
    assert storage_fake.get_files_in_storage_calls == []
    assert any(
        "no hash yet — will retry next run" in record.message
        for record in caplog.records
    ), caplog.text
    # The clip is materialized, so next run's retry is a plain re-scan.
    assert Clip.objects.get(pk=f"{rel}/CLIPNOHASH").item_id is None


# --------------------------------------------------------------------------
# (c) Collection resolution and the will-ingest selection
# --------------------------------------------------------------------------


def test_ingest_resolves_the_collection_once_and_skips_the_known_clip(
    migrated_db, es_fake, es_page, fake_provider, storage_fake, tmp_path, ingest_seams
):
    rel = "2026/AH_20260101_ladder"
    sources = _write_clips(tmp_path, rel, ["CLIPNEW", "CLIPSEEN"])
    Clip(umid=f"{rel}/CLIPSEEN", item_id="VX-100").save()
    es_fake.push(es_page(sources, total=2))

    response = _folder(tmp_path, rel).ingest(
        providers=[fake_provider.machine_name], legacy_storages=[LEGACY_STORAGE]
    )

    assert response["errors"] == []
    assert (response["ingested"], response["skipped"]) == (1, 1)
    assert response["already_ingested"] == 1
    # Exactly one collection resolution for the whole folder...
    assert ingest_seams["collection"] == [rel]
    # ...and the already-ingested clip never reached the helper chain.
    assert ingest_seams["import_file"] == [f"{rel}/CLIPNEW"]
    # Nor the hash lookup: the only recovery call is CLIPNEW's.
    assert storage_fake.get_files_in_storage_calls == [
        (LEGACY_STORAGE, "hash-VX-41-CLIPNEW")
    ]


def test_all_skipped_folder_never_resolves_a_collection(
    migrated_db, es_fake, es_page, fake_provider, storage_fake, tmp_path, ingest_seams
):
    rel = "2026/AH_20260101_allskipped"
    sources = _write_clips(tmp_path, rel, ["CLIPA", "CLIPB"])
    for name in ("CLIPA", "CLIPB"):
        Clip(umid=f"{rel}/{name}", item_id=f"VX-{name}").save()
    es_fake.push(es_page(sources, total=2))

    folder = _folder(tmp_path, rel)
    with CaptureQueriesContext(connection) as captured:
        response = folder.ingest(
            providers=[fake_provider.machine_name], legacy_storages=[LEGACY_STORAGE]
        )

    assert response["skipped"] == len(response["clips"]) == 2
    assert (response["ingested"], response["failed"]) == (0, 0)
    assert ingest_seams["collection"] == []
    assert ingest_seams["import_file"] == []
    assert folder.collection_id is None
    # The only Folder write is the scan's own single write unit — the
    # ingest side adds none of its own.
    assert len(_folder_writes(captured.captured_queries)) == 1
    assert storage_fake.get_files_in_storage_calls == []


def test_provider_filtered_clips_are_not_counted_skipped(
    migrated_db, es_fake, es_page, mislabelled_provider, tmp_path, ingest_seams
):
    """Unchanged behavior: a clip of another provider is nobody's business.

    It is filtered out BEFORE the ladder, so it lands in neither counter —
    exactly what the pre-2.5 loop's `continue` did.
    """
    rel = "2026/AH_20260101_otherprovider"
    sources = _write_clips(tmp_path, rel, ["CLIPX"])
    es_fake.push(es_page(sources, total=1))

    response = _folder(tmp_path, rel).ingest(providers=[MISLABELLED_NAME])

    assert [clip.provider_name for clip in response["clips"]] == ["someotherprovider"]
    assert (response["ingested"], response["skipped"], response["failed"]) == (0, 0, 0)
    assert ingest_seams["import_file"] == []
    assert ingest_seams["collection"] == []


# --------------------------------------------------------------------------
# (d) FR-36: no job id in the response is a failure, never an ingest
# --------------------------------------------------------------------------


class _NoJobIngestHelper:
    """importFileToPlaceholder answering without a jobId."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def importFileToPlaceholder(self, item_id, **kwargs):
        self.calls.append(item_id)
        return self.response


class _ItemAPIFake:
    def __init__(self, response, component_response=None):
        self.response = response
        # The EXTRA components' response, when a test needs it to differ
        # from the anchor's: an extra that starts no job now stops the
        # anchor, so "the container answered without a jobId" can only be
        # exercised with the components answering WITH one.
        self.component_response = (
            response if component_response is None else component_response
        )
        self.calls = []
        # The declared VALUES, which nothing recorded before — the hole
        # defect A lived in.
        self.counts = []
        self.imports = []

    def updatePlaceholderComponentCount(self, item_id, shape_id, **kwargs):
        self.calls.append(("count", item_id, shape_id))
        self.counts.append(kwargs)

    def doImportToPlaceholder(self, item_id=None, **kwargs):
        component = kwargs.get("component", "container")
        self.calls.append(("import", item_id, component))
        self.imports.append(
            {
                "component": component,
                "query": dict(kwargs.get("query") or {}),
                "kwargs": kwargs,
            }
        )
        return self.response if component == "container" else self.component_response


class _ShapeFileFake:
    def __init__(self, file_id):
        self._file_id = file_id

    def getId(self):
        return self._file_id


class _ShapeFake:
    def __init__(self, shape_id, file_ids):
        self._shape_id = shape_id
        self._file_ids = list(file_ids)

    def getId(self):
        return self._shape_id

    def getAllFiles(self):
        return [_ShapeFileFake(file_id) for file_id in self._file_ids]


class _ItemHelperFake:
    """The itemapi, plus the shape read the anchor's WAIT performs.

    ``landed`` is the file-id set the placeholder shape reports: the
    default ``None`` means "whatever the extras asked for is already
    there", which keeps every pre-existing multi-component pin — the
    Codemill ORDER pin included — passing unchanged.

    ``promoted`` is which STATE of the three-state ``placeholder``
    filter the shape answers on: ``False`` (the default) keeps it a
    placeholder, so the un-filtered query sees nothing; ``True`` is a
    shape the anchor's job has promoted, so the ``placeholder=True``
    query sees nothing. Before 2026-09-02 the fake answered the same
    shape on both states, which is a shape that is a placeholder AND
    promoted at once — and the promotion check after the wait (D7)
    needs the two told apart.
    """

    def __init__(self, response, component_response=None, landed=None, promoted=False):
        self.itemapi = _ItemAPIFake(response, component_response)
        self.landed = landed
        self.promoted = promoted
        self.shape_reads = 0

    def getItemShapesFromNames(self, item_id, names, placeholder=False):
        self.shape_reads += 1
        if bool(placeholder) == bool(self.promoted):
            return []
        if self.landed is not None:
            return [_ShapeFake("VX-100-SHAPE", self.landed)]
        attached = [
            entry["query"]["fileId"]
            for entry in self.itemapi.imports
            if entry["component"] != "container"
        ]
        return [_ShapeFake("VX-100-SHAPE", attached)]


class _UnreadableShapeItemHelper:
    """``getItemShapesFromNames`` answers NOTHING on both states of the
    three-state placeholder filter.

    That is `_placeholder_file_ids`'s "could not tell" — distinct from
    "the shape holds no file", which `_ItemHelperFake(landed=[])` models
    and which the wait must report with a different reason entirely.
    """

    def __init__(self):
        self.itemapi = None
        self.shape_reads = 0

    def getItemShapesFromNames(self, item_id, names, placeholder=False):
        self.shape_reads += 1
        return []


class _JobFake:
    """A job double with BOTH readings of "is it still coming".

    `status` is what the plugin actually reads; `in_progress` is only the
    fallback for a job object that reports no status. Keeping both, and
    letting them DISAGREE, is what makes the difference observable: on
    the 6.2.1 server `inProgress()` answers False for WAITING.
    """

    def __init__(self, job_id, in_progress=False, status=None):
        self._job_id = job_id
        self._in_progress = in_progress
        self._status = status

    def getId(self):
        return self._job_id

    def inProgress(self):
        return self._in_progress

    def getStatus(self):
        return self._status


class _JobHelperFake:
    """Jobs that are already terminal — the ordinary case.

    ``in_progress`` makes every component job report itself still
    running, which is how the anchor's WAIT is driven to its bound with
    no thread and no real clock. ``error``/``missing`` are the two arms
    of "the job cannot be read".
    """

    def __init__(
        self,
        in_progress=False,
        error=None,
        missing=False,
        in_progress_until=0,
        status=None,
    ):
        self.in_progress = in_progress
        self.error = error
        self.missing = missing
        # A STATUS STRING, the way Vidispine reports one. `WAITING` is
        # the case that matters: `inProgress()` is False for it, so a
        # double that only carried the boolean could never tell the
        # correct reading from the one measured wrong.
        self.status = status
        # How many polls PER JOB report "still running" before it settles:
        # a wait that really loops is the only way past it.
        self.in_progress_until = in_progress_until
        self.polled = []

    def getJob(self, job_id):
        self.polled.append(job_id)
        if self.error is not None:
            raise self.error
        if self.missing:
            return None
        running = self.in_progress or (
            self.polled.count(job_id) <= self.in_progress_until
        )
        return _JobFake(job_id, in_progress=running, status=self.status)


def test_single_component_import_without_a_job_id_is_not_an_ingest(migrated_db):
    clip = Clip(umid="FR36-SINGLE", item_id="VX-100")

    assert (
        clip._import_single_component(
            "VX-41-FILE", [], None, _NoJobIngestHelper({}), _JobHelperFake()
        )
        is False
    )
    assert clip.job_id is None


def test_single_component_import_with_a_job_id_records_the_job(migrated_db):
    clip = Clip(umid="FR36-SINGLE-OK", item_id="VX-100")

    assert (
        clip._import_single_component(
            "VX-41-FILE",
            [],
            None,
            _NoJobIngestHelper({"jobId": "VX-9"}),
            _JobHelperFake(),
        )
        is True
    )
    assert clip.job_id == "VX-9"


def test_multi_component_import_without_a_job_id_is_not_an_ingest(migrated_db):
    """The bug FR-36 names: this path returned True unconditionally."""
    clip = Clip(umid="FR36-MULTI", item_id="VX-100")
    # The extra answers WITH a job so the anchor is reached at all: a
    # job-less component is its own (new) failure, pinned separately by
    # ::test_an_extra_component_without_a_job_id_stops_the_anchor.
    item_helper = _ItemHelperFake({}, component_response={"jobId": "VX-EXTRA"})

    imported = clip._import_multi_component(
        {"file_id": "VX-41-MAIN", "path": "main.mxf", "type": "video"},
        [{"file_id": "VX-41-EXTRA", "path": "extra.wav", "type": "audio"}],
        "VX-100-SHAPE",
        [],
        None,
        None,
        item_helper,
        _JobHelperFake(),
    )

    assert imported is False
    assert clip.job_id is None
    # The import really was attempted — this is a response check, not an
    # early return.
    assert ("import", "VX-100", "container") in item_helper.itemapi.calls


def test_multi_component_import_with_a_job_id_is_an_ingest(migrated_db):
    clip = Clip(umid="FR36-MULTI-OK", item_id="VX-100")

    imported = clip._import_multi_component(
        {"file_id": "VX-41-MAIN", "path": "main.mxf", "type": "video"},
        [],
        "VX-100-SHAPE",
        [],
        None,
        None,
        _ItemHelperFake({"jobId": "VX-77"}),
        _JobHelperFake(),
    )

    assert imported is True
    assert clip.job_id == "VX-77"


def _multi_component_import(item_helper, extras):
    """Drive ``_import_multi_component`` with a P2-shaped clip."""
    clip = Clip(umid="CODEMILL-ORDER", item_id="VX-100")
    clip._import_multi_component(
        {"file_id": "VX-41-MAIN", "path": "main.mxf", "type": "video"},
        extras,
        "VX-100-SHAPE",
        ["Admin"],
        None,
        None,
        item_helper,
        _JobHelperFake(),
    )
    return item_helper.itemapi


def test_components_are_declared_then_imported_before_the_main_file(migrated_db):
    """Codemill's order: declare the count, import components, main file LAST.

    The main import closes the placeholder, so it has to see a complete
    component set. Reversing this leaves Vidispine deriving a shape from a
    partial set.
    """
    api = _multi_component_import(
        _ItemHelperFake({"jobId": "VX-77"}),
        [
            {"file_id": "VX-41-A0", "path": "a0.wav", "type": "audio"},
            {"file_id": "VX-41-A1", "path": "a1.wav", "type": "audio"},
        ],
    )

    assert api.calls == [
        ("count", "VX-100", "VX-100-SHAPE"),
        ("import", "VX-100", "audio"),
        ("import", "VX-100", "audio"),
        ("import", "VX-100", "container"),
    ]


def test_extra_components_carry_a_bare_file_id(migrated_db):
    """Codemill sends ``{'fileId': ...}`` per component — no tag, no profile.

    A shape tag on a component asks Vidispine to derive a shape from that
    component alone; for a P2 clip that is one audio track against a video
    preset. The tag and the ingest profile belong to the main import.
    """
    api = _multi_component_import(
        _ItemHelperFake({"jobId": "VX-77"}),
        [{"file_id": "VX-41-A0", "path": "a0.wav", "type": "audio"}],
    )

    component = next(i for i in api.imports if i["component"] == "audio")
    assert component["query"] == {"fileId": "VX-41-A0"}
    assert "ingestprofile_groups" not in component["kwargs"]


def test_the_main_import_still_carries_the_tag_and_the_ingest_groups(migrated_db):
    """The counterpart: stripping the components must not strip the main file."""
    api = _multi_component_import(
        _ItemHelperFake({"jobId": "VX-77"}),
        [{"file_id": "VX-41-A0", "path": "a0.wav", "type": "audio"}],
    )

    main = next(i for i in api.imports if i["component"] == "container")
    assert main["query"]["fileId"] == "VX-41-MAIN"
    assert main["query"]["tag"] == "lowres"
    assert main["kwargs"]["ingestprofile_groups"] == ["Admin"]


def test_a_failed_import_counts_failed_and_never_ingested(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, ingest_seams
):
    """Folder-level: import_file's failure verdict reaches the counters."""
    rel = "2026/AH_20260101_nojob"
    sources = _write_clips(tmp_path, rel, ["CLIPFAIL"])
    es_fake.push(es_page(sources, total=1))
    ingest_seams["result"].update(ingested=False, failed=True)

    response = _folder(tmp_path, rel).ingest(providers=[fake_provider.machine_name])

    assert (response["failed"], response["ingested"]) == (1, 0)
    assert response["errors"] == []


# --------------------------------------------------------------------------
# (e) AD-6 writer 2: the ingest write is one targeted UPDATE
# --------------------------------------------------------------------------


def test_ingest_writes_only_ingest_state_columns(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, ingest_seams
):
    """A full save() would rewrite every column of the row the scan wrote."""
    rel = "2026/AH_20260101_targeted"
    sources = _write_clips(tmp_path, rel, ["CLIPUPD"])
    umid = f"{rel}/CLIPUPD"
    Clip.objects.create(
        umid=umid,
        path="2019/ORIGINAL_CARD",
        folder_path="/mnt/legacy/2019/ORIGINAL_CARD",
        storage_id="VX-LEGACY-STORAGE",
        reference_file="LEGACY-FILE-ID",
        provider_name="legacyprovider",
        file_id="LEGACY-FILE-ID",
    )
    es_fake.push(es_page(sources, total=1))

    response = _folder(tmp_path, rel).ingest(providers=[fake_provider.machine_name])

    assert response["ingested"] == 1
    reloaded = Clip.objects.get(pk=umid)
    # Ingest state: written.
    assert reloaded.item_id == "VX-ITEM-1"
    assert reloaded.job_id == "VX-JOB-1"
    assert reloaded.status == Clip.STATUS_PLACHOLDER_CREATED
    assert reloaded.file_id == "VX-41-CLIPUPD"
    # Everything else: exactly as the row was, poisoned values included.
    assert reloaded.path == "2019/ORIGINAL_CARD"
    assert reloaded.folder_path == "/mnt/legacy/2019/ORIGINAL_CARD"
    assert reloaded.storage_id == "VX-LEGACY-STORAGE"
    assert reloaded.provider_name == "legacyprovider"
    assert reloaded.reference_file == "LEGACY-FILE-ID"
    assert Clip.objects.count() == 1
    # The M2M membership still happens (declared AD-6 deviation).
    assert list(reloaded.folders.values_list("path", flat=True)) == [rel]


def test_an_unsaved_clip_reaching_ingest_is_logged_and_saved(
    migrated_db, tmp_path, ingest_seams, caplog
):
    """The invariant guard: the scan's write unit should have written it."""
    clip = Clip(
        umid="UNSAVED-1", path="2026/X", storage_id=STORAGE_ID, reference_file="F"
    )

    result = clip.ingest()

    assert result["ingested"] is True
    assert Clip.objects.filter(pk="UNSAVED-1").exists()
    assert any(
        record.levelname == "ERROR" and "reached ingest unsaved" in record.message
        for record in caplog.records
    ), caplog.text


def test_the_rest_path_saves_an_unsaved_clip_without_crying_wolf(
    migrated_db, ingest_seams, caplog
):
    """views.py builds its clip from a request body — that is not a bug.

    The invariant is about the SCAN path. Logging an ERROR for the REST
    endpoint's normal shape trains operators to ignore the message that
    means something really went wrong.
    """
    clip = Clip(umid="RESTCLIP-1", path="2026/X", storage_id=STORAGE_ID)

    clip.ingest(expect_persisted=False)

    assert Clip.objects.filter(pk="RESTCLIP-1").exists()
    assert not [
        record for record in caplog.records if record.levelname == "ERROR"
    ], caplog.text


def test_a_row_deleted_under_an_ingest_is_re_inserted(migrated_db, caplog):
    """A targeted UPDATE that matches nothing is silence, not persistence.

    Without the row-count check the ingest state evaporates and the next
    scan ingests the same clip again — the duplicate-ingest class NFR-1
    governs.
    """
    clip = Clip.objects.create(
        umid="VANISHED-1", path="2026/X", storage_id=STORAGE_ID, reference_file="F"
    )
    Clip.objects.filter(pk="VANISHED-1").delete()
    clip.item_id = "VX-900"
    clip.job_id = "VX-JOB-900"
    clip.status = Clip.STATUS_PLACHOLDER_CREATED

    clip.persist_ingest_state()

    reloaded = Clip.objects.get(pk="VANISHED-1")
    assert (reloaded.item_id, reloaded.job_id) == ("VX-900", "VX-JOB-900")
    assert any(
        record.levelname == "ERROR" and "matched no row" in record.message
        for record in caplog.records
    ), caplog.text


# --------------------------------------------------------------------------
# (A1) One umid is one clip is one ingest
# --------------------------------------------------------------------------


def test_two_files_one_umid_are_ingested_once(
    migrated_db, es_fake, es_page, fixed_umid_provider, tmp_path, ingest_seams
):
    """The umid is the primary key: two files mapping to it are one clip.

    The scan response still reports both files (unchanged), but the
    ladder acts on one — ingesting both would create two Vidispine
    placeholders for one clip.
    """
    rel = "2026/AH_20260101_dupingest"
    sources = _write_clips(tmp_path, rel, ["FIRST", "LAST"])
    es_fake.push(es_page(sources, total=2))

    response = _folder(tmp_path, rel).ingest(providers=[FIXED_UMID_NAME])

    assert len(response["clips"]) == 2
    assert ingest_seams["import_file"] == [FIXED_UMID]
    assert (response["ingested"], response["skipped"]) == (1, 0)
    assert Clip.objects.count() == 1


# --------------------------------------------------------------------------
# (A2) A recovered item_id reaches the row — and never overwrites one
# --------------------------------------------------------------------------


def test_a_recovered_item_id_is_persisted_on_the_row(
    migrated_db, es_fake, es_page, fake_provider, storage_fake, tmp_path
):
    """Otherwise the id dies with the object and every scan pays again.

    The ingest ladder skips a clip the moment its ``item_id`` is truthy,
    so ``Clip.ingest`` — the only other writer of that column — is never
    reached for exactly the clips whose id was recovered.
    """
    rel = "2026/AH_20260101_recovpersist"
    sources = _write_clips(tmp_path, rel, ["CLIPREC"], hashes={"CLIPREC": "hash-rec"})
    Clip(umid=f"{rel}/CLIPREC", path=rel, storage_id=STORAGE_ID).save()
    storage_fake.set_hash_item("hash-rec", "VX-800")
    es_fake.push(es_page(sources, total=1))

    response = _folder(tmp_path, rel).scan(
        providers=[fake_provider.machine_name], legacy_storages=[LEGACY_STORAGE]
    )

    assert response["errors"] == []
    assert Clip.objects.get(pk=f"{rel}/CLIPREC").item_id == "VX-800"


def test_a_second_scan_no_longer_pays_the_recovery_lookup(
    migrated_db, es_fake, es_page, fake_provider, storage_fake, tmp_path
):
    """FR-8, end to end: recovery happens ONCE, not once per scan."""
    rel = "2026/AH_20260101_recovonce"
    sources = _write_clips(tmp_path, rel, ["CLIPONCE"], hashes={"CLIPONCE": "hash-1x"})
    storage_fake.set_hash_item("hash-1x", "VX-810")
    providers = [fake_provider.machine_name]

    es_fake.push(es_page(sources, total=1))
    _folder(tmp_path, rel).scan(providers=providers, legacy_storages=[LEGACY_STORAGE])
    es_fake.push(es_page(sources, total=1))
    _rescanned_folder(tmp_path, rel).scan(
        providers=providers, legacy_storages=[LEGACY_STORAGE]
    )

    assert storage_fake.get_files_in_storage_calls == [(LEGACY_STORAGE, "hash-1x")]


def test_a_recovered_id_never_overwrites_an_id_the_row_already_has(migrated_db):
    """The fill-only-NULL guard, at the statement level.

    Recovery reads the row, decides, and writes at the end of the folder;
    an ingest that lands in between must keep the id it earned.
    """
    Clip.objects.create(
        umid="RACE-1",
        path="2026/X",
        storage_id=STORAGE_ID,
        reference_file="F",
        item_id="VX-REAL",
    )
    Clip.objects.create(
        umid="RACE-2", path="2026/X", storage_id=STORAGE_ID, reference_file="F"
    )

    persist_scan_results(
        Folder(storage_id=STORAGE_ID, path="2026/X"),
        build_persistence_plan(
            [],
            recovered_item_ids={"RACE-1": "VX-RECOVERED", "RACE-2": "VX-RECOVERED-2"},
        ),
    )

    assert Clip.objects.get(pk="RACE-1").item_id == "VX-REAL"
    assert Clip.objects.get(pk="RACE-2").item_id == "VX-RECOVERED-2"


# --------------------------------------------------------------------------
# (12) A failed write unit is reported, not swallowed
# --------------------------------------------------------------------------


def test_a_failing_write_unit_lands_in_errors_and_rolls_back(
    migrated_db, es_fake, es_page, fake_provider, tmp_path, monkeypatch
):
    """A DB failure must not take the folder down silently.

    Pre-fix the exception escaped ``scan()`` with the counters already
    claiming success, so a cron run reported clips it had not written.
    """
    rel = "2026/AH_20260101_dbfail"
    sources = _write_clips(tmp_path, rel, ["CLIPDB1", "CLIPDB2"])
    es_fake.push(es_page(sources, total=2))

    def boom(writes, stale_deletes):
        raise DatabaseError("metadata upsert exploded")

    monkeypatch.setattr(Clip, "persist_metadatas_bulk", staticmethod(boom))

    response = _folder(tmp_path, rel).scan(providers=[fake_provider.machine_name])

    assert response["processed"] == 2
    assert any("Error persisting scan results" in error for error in response["errors"])
    # The transaction rolled back: the clip rows written before the
    # exploding statement are gone too.
    assert Clip.objects.count() == 0
    assert Folder.objects.count() == 0


# --------------------------------------------------------------------------
# (16) `created` counts clips, not files
# --------------------------------------------------------------------------


def test_a_umid_spanning_two_pages_is_created_once(
    migrated_db, es_fake, es_page, fixed_umid_provider, tmp_path
):
    """`created` is incremented in the page loop, the write is deduped
    after it — so a umid appearing on two pages used to be counted twice
    and written once."""
    rel = "2026/AH_20260101_pagedup"
    first_page = _write_clips(tmp_path, rel, [f"P{index:03d}" for index in range(100)])
    second_page = _write_clips(tmp_path, rel, ["TAIL"])
    es_fake.push(es_page(first_page, total=101))
    es_fake.push(es_page(second_page, total=101))

    response = _folder(tmp_path, rel).scan(number=0, providers=[FIXED_UMID_NAME])

    assert response["processed"] == 101
    assert response["created"] == 1
    assert Clip.objects.count() == 1


# --------------------------------------------------------------------------
# (B6/A3) The real import path: verdicts nothing else executes
# --------------------------------------------------------------------------


def _ingestable_page(tmp_path, rel, names):
    return _write_clips(tmp_path, rel, names, suffix=".ing")


def test_a_real_import_without_a_job_id_is_failed_never_ingested(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """FR-36 where it is DECIDED, through the real ``import_file``.

    The two component helpers were pinned before; their verdict's
    consumption was not, and reverting ``import_file``'s closing ladder
    to the pre-2.5 unconditional ``ingested = True`` left the suite green.
    """
    rel = "2026/AH_20260101_realnojob"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPRAW"]), total=1))
    VidispineFake.set_import_response({})

    response = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (response["failed"], response["ingested"]) == (1, 0)
    assert response["errors"] == []
    # The import really was attempted.
    assert "importFileToPlaceholder" in VidispineFake.call_names()


def test_a_real_import_with_a_job_id_is_ingested(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    rel = "2026/AH_20260101_realjob"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPOK"]), total=1))
    VidispineFake.set_import_response({"jobId": "VX-JOB-77"})

    response = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (response["ingested"], response["failed"]) == (1, 0)
    stored = Clip.objects.get(pk=f"{rel}/CLIPOK")
    assert stored.job_id == "VX-JOB-77"
    assert stored.item_id
    assert stored.status == Clip.STATUS_PLACHOLDER_CREATED


def test_a_failed_import_leaves_a_row_that_says_so(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """The persisted state after a job-id-less import — nothing observed
    it before, and it is what the next run's ladder reads."""
    rel = "2026/AH_20260101_failstate"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPSTUCK"]), total=1))
    VidispineFake.set_import_response({})

    _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    stored = Clip.objects.get(pk=f"{rel}/CLIPSTUCK")
    # The placeholder exists in Vidispine, so its id is kept: forgetting
    # it would orphan the placeholder and make the next run create
    # another one.
    assert stored.item_id
    # ...but nothing was imported, and the row says exactly that.
    assert stored.job_id is None
    assert stored.status == Clip.STATUS_PLACHOLDER_CREATED


def test_an_incomplete_import_is_retried_next_run_not_skipped_forever(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """A3: the placeholder its failed import left must not strand it.

    Skipping on ``item_id`` alone would make this clip invisible for the
    rest of its life — only an explicit --replace run would rescue it.
    The retry goes in AS a replace, because ``import_file``'s "item
    already exists" early return skips every non-replace call.
    """
    rel = "2026/AH_20260101_retrystuck"
    sources = _ingestable_page(tmp_path, rel, ["CLIPRETRY"])

    es_fake.push(es_page(sources, total=1))
    VidispineFake.set_import_response({})
    first = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])
    assert first["failed"] == 1
    placeholder_id = Clip.objects.get(pk=f"{rel}/CLIPRETRY").item_id

    es_fake.push(es_page(sources, total=1))
    VidispineFake.set_import_response({"jobId": "VX-JOB-RETRY"})
    second = _rescanned_folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (second["ingested"], second["skipped"]) == (1, 0)
    stored = Clip.objects.get(pk=f"{rel}/CLIPRETRY")
    assert stored.job_id == "VX-JOB-RETRY"
    # The SAME placeholder was reused — a second one would be a duplicate
    # item for one clip.
    assert stored.item_id == placeholder_id


def test_a_recovered_item_id_is_not_mistaken_for_an_incomplete_import(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """A hash-recovered clip has no job either — and must stay skipped.

    Its item already holds the file (that is what the hash match proved);
    re-examining it every run would buy back exactly the per-clip HTTP
    cost FR-8 removed.
    """
    rel = "2026/AH_20260101_recovnotstuck"
    sources = _ingestable_page(tmp_path, rel, ["CLIPRECOV"])
    Clip(
        umid=f"{rel}/CLIPRECOV",
        path=rel,
        storage_id=STORAGE_ID,
        item_id="VX-RECOVERED",
        status=Clip.STATUS_NOT_IMPORTED,
    ).save()
    es_fake.push(es_page(sources, total=1))

    response = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (response["skipped"], response["ingested"]) == (1, 0)
    assert VidispineFake.calls == []
    assert collection_seam == []


def test_an_empty_original_shape_does_not_block_the_retry(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """FR-35 where it is REACHABLE: the retry path, not the operator's
    --replace.

    A previous import can leave a placeholder SHAPE holding no file. The
    old `len(original_files) >= 0` guard was always true, so such an item
    was declared "already exists and has an original file" and skipped —
    forever. With `> 0` the empty shape falls through to the checks that
    can actually tell, the empty shape is removed, and the clip imports.
    """
    rel = "2026/AH_20260101_emptyshape"
    sources = _ingestable_page(tmp_path, rel, ["CLIPEMPTY"])
    Clip(
        umid=f"{rel}/CLIPEMPTY",
        path=rel,
        storage_id=STORAGE_ID,
        item_id="VX-EMPTY",
        status=Clip.STATUS_PLACHOLDER_CREATED,
    ).save()
    VidispineFake.set_item("VX-EMPTY")
    VidispineFake.set_original_shape("VX-EMPTY", "VX-EMPTY-SHAPE", files=[])
    VidispineFake.set_import_response({"jobId": "VX-JOB-EMPTY"})
    es_fake.push(es_page(sources, total=1))

    response = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (response["ingested"], response["skipped"]) == (1, 0)
    assert response["replaced"] == 1
    assert Clip.objects.get(pk=f"{rel}/CLIPEMPTY").job_id == "VX-JOB-EMPTY"


def test_an_item_that_already_holds_the_file_is_not_re_imported(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """The safety net under the retry rung, through the real import.

    A clip that reaches ``import_file`` with an item whose original shape
    already carries files must come back `skipped`, never re-imported —
    this is the check that makes letting anything through the ladder safe.
    """
    rel = "2026/AH_20260101_alreadyfiled"
    sources = _ingestable_page(tmp_path, rel, ["CLIPDONE"])
    umid = f"{rel}/CLIPDONE"
    Clip(
        umid=umid,
        path=rel,
        storage_id=STORAGE_ID,
        item_id="VX-DONE",
        status=Clip.STATUS_PLACHOLDER_CREATED,
    ).save()
    VidispineFake.set_item("VX-DONE")
    VidispineFake.set_original_shape(
        "VX-DONE",
        "VX-DONE-SHAPE",
        files=[{"id": "VX-OTHER-FILE", "storage": "VX-OTHERSTORAGE"}],
    )
    es_fake.push(es_page(sources, total=1))

    response = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    # The ladder let it through (no job id on the row), the import path
    # itself refused it.
    assert (response["skipped"], response["ingested"], response["failed"]) == (1, 0, 0)
    assert "importFileToPlaceholder" not in VidispineFake.call_names()
    assert "doImportToPlaceholder" not in VidispineFake.call_names()


# --------------------------------------------------------------------------
# (3.0) The combined write: no orphaned placeholders after createPlaceholder
# --------------------------------------------------------------------------
#
# The death model needs no special fixture: `Clip.ingest` runs
# `persist_ingest_state` only after `import_file` RETURNS, so an exception
# raised by a fault leaves the row holding exactly what the database held
# at that moment — the same state a killed process leaves. A caught
# in-process exception and a process death are therefore ONE cell here,
# which is the whole point of the design: every post-write interruption
# lands on the FR-36 incomplete-import cell the existing retry rung
# already recovers.


def _fr36_cell(umid):
    """The row the combined write leaves: named, job-less, at PLACEHOLDER."""
    stored = Clip.objects.get(pk=umid)
    assert stored.item_id, "the combined write should have named the placeholder"
    assert not stored.job_id
    assert stored.status == Clip.STATUS_PLACHOLDER_CREATED
    return stored


@pytest.mark.parametrize(
    "window",
    ["setItemMetadataFieldGroup", "addItemToCollection", "createPlaceholderShape"],
)
def test_a_death_after_the_combined_write_retries_into_the_same_placeholder(
    migrated_db,
    es_fake,
    es_page,
    ingestable_provider,
    tmp_path,
    collection_seam,
    window,
):
    """The story's promise, at every window between the combined write and
    the import call.

    Each of these is a Vidispine call the old code performed with the row
    still reading `item_id` empty / NOT_IMPORTED, so the next run
    concluded "not ingested" and created a SECOND placeholder, orphaning
    the first. Now the row is already the FR-36 cell when the fault
    fires, and the next run's retry rung imports into the SAME
    placeholder. Both halves are asserted: exactly one `createPlaceholder`
    across the two runs (the negative) AND run 2 really ingesting into it
    (the positive) — a fix that merely refused everything would pass the
    negative alone.
    """
    rel = f"2026/AH_20260101_death_{window.lower()}"
    sources = _ingestable_page(tmp_path, rel, ["CLIPDEAD"])
    umid = f"{rel}/CLIPDEAD"

    es_fake.push(es_page(sources, total=1))
    VidispineFake.fail_next(window)
    first = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    # Today's catch: the per-clip handler logged it, counted it failed
    # and carried on.
    assert (first["failed"], first["ingested"]) == (1, 0)
    assert len(first["errors"]) == 1
    assert VidispineFake.call_names().count("createPlaceholder") == 1
    placeholder_id = _fr36_cell(umid).item_id

    es_fake.push(es_page(sources, total=1))
    VidispineFake.set_import_response({"jobId": "VX-JOB-RECOVERED"})
    second = _rescanned_folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (second["ingested"], second["skipped"], second["failed"]) == (1, 0, 0)
    # Still ONE placeholder in Vidispine, across both runs.
    assert VidispineFake.call_names().count("createPlaceholder") == 1
    stored = Clip.objects.get(pk=umid)
    assert stored.item_id == placeholder_id
    assert stored.job_id == "VX-JOB-RECOVERED"
    assert stored.status == Clip.STATUS_PLACHOLDER_CREATED


def test_a_death_at_the_import_call_itself_comes_back_skipped_not_doubled(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """The last window, whose honest answer is `skipped`, not `ingested`.

    Vidispine attached the file and turned the placeholder shape into a
    real one; the process died before the row learned the job id. The
    next run finds an item whose original shape already holds files and
    refuses to touch it — no second placeholder, no second import. The
    job id is lost for good (the row keeps reading job-less), which is
    accepted: NFR-1 is about duplicate ITEMS, not about job bookkeeping.
    """
    rel = "2026/AH_20260101_deathatimport"
    sources = _ingestable_page(tmp_path, rel, ["CLIPDONEDEAD"])
    umid = f"{rel}/CLIPDONEDEAD"

    es_fake.push(es_page(sources, total=1))
    VidispineFake.set_import_response({"jobId": "VX-JOB-LOST"})
    VidispineFake.fail_next("importFileToPlaceholder")
    first = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (first["failed"], first["ingested"]) == (1, 0)
    placeholder_id = _fr36_cell(umid).item_id

    es_fake.push(es_page(sources, total=1))
    second = _rescanned_folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (second["skipped"], second["ingested"], second["failed"]) == (1, 0, 0)
    assert VidispineFake.call_names().count("createPlaceholder") == 1
    assert VidispineFake.call_names().count("importFileToPlaceholder") == 1
    stored = Clip.objects.get(pk=umid)
    assert stored.item_id == placeholder_id


def test_a_recovered_submission_reuses_the_placeholder_shape_it_left(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """RULED: the un-parameterized shape query at `import_file` must NOT
    see the placeholder shape the interrupted run created.

    `placeholder` is a three-state FILTER on the shape-list endpoint and
    its default returns only NON-placeholder shapes, so the replace loop
    never runs and `_get_or_create_placeholder_shape` — which asks for
    placeholder shapes only — finds the file-less shape and reuses it.
    Making that shape visible to the first query is the one change that
    would divert the retry into `_should_replace_original_files`, which
    for an empty file list falls through to `return True`: the item would
    be stripped and the recovered clip would lose its proxy.
    """
    rel = "2026/AH_20260101_shapereuse"
    sources = _ingestable_page(tmp_path, rel, ["CLIPSHAPE"])

    es_fake.push(es_page(sources, total=1))
    VidispineFake.fail_next("createPlaceholderShape")
    _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])
    assert VidispineFake.call_names().count("createPlaceholderShape") == 1

    es_fake.push(es_page(sources, total=1))
    VidispineFake.set_import_response({"jobId": "VX-JOB-SHAPE"})
    second = _rescanned_folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert second["ingested"] == 1
    # The shape was REUSED, not re-created...
    assert VidispineFake.call_names().count("createPlaceholderShape") == 1
    # ...and the item was never stripped: nothing was replaced, so the
    # `no_transcode` override that costs the clip its proxy never fired.
    assert second["replaced"] == 0
    assert "removeItemShape" not in VidispineFake.call_names()


def test_a_death_inside_create_placeholder_is_the_accepted_residual(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """THE RESIDUAL, pinned as a document, not as a promise.

    The item was created server-side and the response was lost, so the
    combined write never ran and the row is exactly as the scan wrote it.
    The next run cannot know, and creates a second placeholder — the
    unnamed-orphan window is ONE statement wide, not zero. Accepted by
    the spec; the orphan itself is owned by the dedicated maintenance
    story. If this test ever fails because the second run stopped making
    a second placeholder, the residual has been closed — retire this pin
    alongside that story, do not paper over it.
    """
    rel = "2026/AH_20260101_deathinside"
    sources = _ingestable_page(tmp_path, rel, ["CLIPLOST"])
    umid = f"{rel}/CLIPLOST"

    es_fake.push(es_page(sources, total=1))
    VidispineFake.fail_next("createPlaceholder")
    first = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (first["failed"], len(first["errors"])) == (1, 1)
    # The row never learned anything: no name, no status move.
    untouched = Clip.objects.get(pk=umid)
    assert not untouched.item_id
    assert untouched.status == Clip.STATUS_NOT_IMPORTED

    es_fake.push(es_page(sources, total=1))
    VidispineFake.set_import_response({"jobId": "VX-JOB-SECOND"})
    second = _rescanned_folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    # The clip ingests — into a SECOND placeholder. The first is orphaned.
    assert second["ingested"] == 1
    assert VidispineFake.call_names().count("createPlaceholder") == 2


def test_a_death_before_create_placeholder_retries_cleanly(
    migrated_db,
    es_fake,
    es_page,
    ingestable_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """Preparatory round trips create nothing, so a death there must
    leave a clip that retries normally next run — no name, no status
    move, no refusal."""
    from tests.portal_stub import UserHelperFake

    original = UserHelperFake.getUserSettingsProfile
    died = []

    def boom_once(self, basegroup=None):
        if not died:
            died.append(True)
            raise RuntimeError("the process died resolving the settings profile")
        return original(self, basegroup=basegroup)

    monkeypatch.setattr(UserHelperFake, "getUserSettingsProfile", boom_once)
    rel = "2026/AH_20260101_earlydeath"
    sources = _ingestable_page(tmp_path, rel, ["CLIPEARLY"])
    umid = f"{rel}/CLIPEARLY"

    es_fake.push(es_page(sources, total=1))
    first = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (first["failed"], len(first["errors"])) == (1, 1)
    assert "createPlaceholder" not in VidispineFake.call_names()
    untouched = Clip.objects.get(pk=umid)
    assert not untouched.item_id
    assert untouched.status == Clip.STATUS_NOT_IMPORTED

    es_fake.push(es_page(sources, total=1))
    VidispineFake.set_import_response({"jobId": "VX-JOB-EARLY"})
    second = _rescanned_folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (second["ingested"], second["failed"]) == (1, 0)
    assert VidispineFake.call_names().count("createPlaceholder") == 1


def test_a_row_naming_an_unresolvable_item_is_refused_not_recreated(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """A deleted item must not fall into the create branch.

    `Clip.item` answers None on NotFoundError too (the accident commit
    632bd4c closed), so a row at the incomplete-import cell whose item
    was deleted OUTSIDE the listener would otherwise be "recovered" into
    a brand-new placeholder — the exact second item this story exists to
    prevent. It is refused instead: `TapelessIngestException`, counted
    failed with the reason in `errors`, recurring every run until the row
    is healed (the maintenance story owns the tooling). `--replace`, the
    operator's biggest hammer, must not break the refusal either.
    """
    rel = "2026/AH_20260101_gone"
    sources = _ingestable_page(tmp_path, rel, ["CLIPGONE"])
    umid = f"{rel}/CLIPGONE"
    Clip(
        umid=umid,
        path=rel,
        storage_id=STORAGE_ID,
        item_id="VX-GONE",
        status=Clip.STATUS_PLACHOLDER_CREATED,
    ).save()
    # VX-GONE is deliberately unconfigured: getItem raises NotFoundError.

    for run, replace in enumerate((False, True)):
        folder = (
            _folder(tmp_path, rel) if run == 0 else _rescanned_folder(tmp_path, rel)
        )
        es_fake.push(es_page(sources, total=1))
        response = folder.ingest(providers=[INGESTABLE_NAME], replace=replace)

        assert (response["failed"], response["ingested"]) == (1, 0)
        [error] = response["errors"]
        assert "VX-GONE" in error and umid in error
        assert "createPlaceholder" not in VidispineFake.call_names()
        # The row is untouched: it waits for a human, it does not heal
        # itself and it does not get worse.
        stored = Clip.objects.get(pk=umid)
        assert (stored.item_id, stored.status) == (
            "VX-GONE",
            Clip.STATUS_PLACHOLDER_CREATED,
        )


def test_an_empty_item_id_never_reaches_getitem(migrated_db, ingestable_provider):
    """Falsiness, not `is None`: the deletion listener writes `""`.

    Under the old `is None` test an emptied row sent `getItem("")` down
    the wire on every `.item` read.
    """
    clip = Clip(umid="EMPTY-ID", path="a", provider_name=INGESTABLE_NAME, item_id="")

    assert clip.item is None
    assert VidispineFake.calls == []


def test_a_rest_ingest_of_a_new_clip_never_inserts_before_the_import(
    migrated_db, ingestable_provider
):
    """The combined write NO-OPS when there is no row.

    The REST endpoint (views.py) builds an UNSAVED Clip from the request
    body and calls `ingest(expect_persisted=False)`. A `save()` fallback
    in the combined write would INSERT a partial row before the import
    ran — so a death mid-import must leave NO row at all.
    """
    clip = Clip(
        umid="REST-DEAD",
        path="2026/X",
        storage_id=STORAGE_ID,
        provider_name=INGESTABLE_NAME,
        file_id="VX-41-RESTDEAD",
    )
    clip._file = _vsfile("2026/X/RESTDEAD.ing", "VX-41-RESTDEAD")
    VidispineFake.fail_next("setItemMetadataFieldGroup")

    # The ARMED fault specifically: a bare `Exception` would let a
    # TypeError thrown before the fault window pass this test.
    with pytest.raises(InjectedVidispineFault):
        clip.ingest(expect_persisted=False)

    assert VidispineFake.call_names().count("createPlaceholder") == 1
    assert Clip.objects.filter(pk="REST-DEAD").count() == 0


def test_a_rest_ingest_of_a_new_clip_proceeds_without_crying_wolf(
    migrated_db, ingestable_provider, caplog
):
    """The happy REST half: the no-op write is silent and the ingest
    lands — one placeholder, the row written once at the end, no
    "reached ingest unsaved" ERROR."""
    clip = Clip(
        umid="REST-NEW",
        path="2026/X",
        storage_id=STORAGE_ID,
        provider_name=INGESTABLE_NAME,
        file_id="VX-41-RESTNEW",
    )
    clip._file = _vsfile("2026/X/RESTNEW.ing", "VX-41-RESTNEW")
    VidispineFake.set_import_response({"jobId": "VX-JOB-REST"})

    result = clip.ingest(expect_persisted=False)

    assert result["ingested"] is True
    assert VidispineFake.call_names().count("createPlaceholder") == 1
    stored = Clip.objects.get(pk="REST-NEW")
    assert (stored.job_id, stored.status) == (
        "VX-JOB-REST",
        Clip.STATUS_PLACHOLDER_CREATED,
    )
    assert not [r for r in caplog.records if r.levelname == "ERROR"], caplog.text


def test_makemigrations_sees_no_model_change(migrated_db):
    """Story 3.0 changes no model shape — verified, not asserted."""
    from django.core.management import call_command

    call_command("makemigrations", "TapelessIngest", check=True, dry_run=True)


def test_the_settings_profile_is_resolved_as_the_runs_user(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """`import_file` builds a PER-CALL UserHelper(runas=user) (story 3.1).

    `create_item`'s old default was ONE UserHelper built at
    class-definition time — with runas=None — shared by every caller in
    the process: cross-thread shared state under the pool, and the
    settings profile silently resolved as nobody. The stub records
    `runas` on getUserSettingsProfile precisely so this run can prove the
    profile was resolved AS THE RUN'S USER.
    """
    from django.contrib.auth.models import User

    rel = "2026/AH_20260101_runas"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPUH"]), total=1))
    VidispineFake.set_import_response({"jobId": "VX-JOB-UH"})
    user = User.objects.create(pk=4245, username="story31-runas")
    try:
        response = _folder(tmp_path, rel).ingest(user=user, providers=[INGESTABLE_NAME])

        assert response["ingested"] == 1
        profile_calls = [
            details
            for name, details in VidispineFake.calls
            if name == "getUserSettingsProfile"
        ]
        assert profile_calls, VidispineFake.call_names()
        assert all(details["runas"] is user for details in profile_calls)
    finally:
        user.delete()  # keep the auth table empty for the unknown-user tests


def test_create_item_builds_every_vidispine_helper_per_call(
    migrated_db, ingestable_provider, monkeypatch
):
    """Retro-3 F4: `gh`/`ith`/`ch` were story 3.1's `uh` defect, three more times.

    A helper built as a DEFAULT ARGUMENT is constructed once, at class
    definition time, and shared by every caller in the process — with
    `runas=None`, so the run's user is silently nobody, and as mutable
    cross-thread state once the pool runs several folders at once. Only
    `uh` was fixed in 3.1; `gh`, `ith` and `ch` kept the shape. This
    calls `create_item` the way a future caller would — omitting all
    four — and proves each call gets FRESH instances, as the run's user.
    """
    from django.contrib.auth.models import User

    from portal.plugins.TapelessIngest.models import clip as clip_module

    HELPERS = ("GroupHelper", "ItemHelper", "CollectionHelper", "UserHelper")
    built = []

    def _recording(name):
        base = getattr(clip_module, name)

        class Recording(base):
            def __init__(self, *args, **kwargs):
                base.__init__(self, *args, **kwargs)
                built.append((name, self, kwargs.get("runas")))

        return Recording

    # Patched AFTER import on purpose: a class-definition-time default is
    # already built by now, so it can never show up in `built`.
    for name in HELPERS:
        monkeypatch.setattr(clip_module, name, _recording(name))

    user = User.objects.create(pk=4246, username="retro3-per-call-helpers")
    try:
        for umid in ("HELPERS-A", "HELPERS-B"):
            clip = Clip.objects.create(
                umid=umid,
                path="2026/HELPERS",
                storage_id=STORAGE_ID,
                provider_name=INGESTABLE_NAME,
                metadatas={},
            )
            _item, created = clip.create_item(user=user)
            assert created is True

        first_call, second_call = built[:4], built[4:]
        assert len(built) == 8, built

        # Each call built one of each of the four, itself...
        for call in (first_call, second_call):
            assert sorted(name for name, _obj, _runas in call) == sorted(HELPERS)

        # ...as a DISTINCT instance (eight calls, eight objects — nothing
        # is shared between the two calls, let alone process-wide)...
        assert len({id(obj) for _name, obj, _runas in built}) == 8

        # ...and as the run's user, not as nobody.
        assert all(runas is user for _name, _obj, runas in built), built
    finally:
        Clip.objects.filter(path="2026/HELPERS").delete()
        user.delete()  # keep the auth table empty for the unknown-user tests


def test_create_item_honours_explicitly_passed_helpers(
    migrated_db, ingestable_provider, monkeypatch
):
    """The other half of the per-call default: an explicit helper is USED.

    Freshness alone is only half the contract. `import_file` passes all
    four helpers it built as the run's user, and `ith` is an
    `ItemHelperExtended` — a subclass no default could supply. A
    `create_item` that rebuilt them unconditionally would silently
    discard the caller's subclass and its deliberate `runas`, and every
    per-call-freshness assertion in the sibling test above would still
    pass. So: pass all four, prove they are the objects used, and prove
    nothing was constructed behind them.
    """
    from django.contrib.auth.models import User

    from portal.plugins.TapelessIngest.models import clip as clip_module

    used = []

    class _SpyGroup(clip_module.GroupHelper):
        def getUserIngestGroups(self):
            used.append("gh")
            return clip_module.GroupHelper.getUserIngestGroups(self)

    class _SpyUser(clip_module.UserHelper):
        def getUserSettingsProfile(self, basegroup=None):
            used.append("uh")
            return clip_module.UserHelper.getUserSettingsProfile(
                self, basegroup=basegroup
            )

    class _SpyItem(clip_module.ItemHelper):
        def createPlaceholder(self, *args, **kwargs):
            used.append("ith")
            return clip_module.ItemHelper.createPlaceholder(self, *args, **kwargs)

    class _SpyCollection(clip_module.CollectionHelper):
        def addItemToCollection(self, *args, **kwargs):
            used.append("ch")
            return clip_module.CollectionHelper.addItemToCollection(
                self, *args, **kwargs
            )

    passed = {
        "gh": _SpyGroup(),
        "uh": _SpyUser(),
        "ith": _SpyItem(),
        "ch": _SpyCollection(),
    }

    # Anything create_item constructs for itself from here on is visible.
    built = []

    def _recording(name):
        base = getattr(clip_module, name)

        class Recording(base):
            def __init__(self, *args, **kwargs):
                base.__init__(self, *args, **kwargs)
                built.append(name)

        return Recording

    for name in ("GroupHelper", "ItemHelper", "CollectionHelper", "UserHelper"):
        monkeypatch.setattr(clip_module, name, _recording(name))

    user = User.objects.create(pk=4247, username="retro3-explicit-helpers")
    try:
        clip = Clip.objects.create(
            umid="HELPERS-EXPLICIT",
            path="2026/HELPERS_EXPLICIT",
            storage_id=STORAGE_ID,
            provider_name=INGESTABLE_NAME,
            metadatas={},
        )

        _item, created = clip.create_item(
            user=user, collection_id="VX-COLLECTION-EXPLICIT", **passed
        )

        assert created is True
        # Every one of the four was the object that did the work...
        assert sorted(used) == ["ch", "gh", "ith", "uh"]
        # ...and not one of them was rebuilt behind the caller's back.
        assert built == []
    finally:
        Clip.objects.filter(path="2026/HELPERS_EXPLICIT").delete()
        user.delete()  # keep the auth table empty for the unknown-user tests


# --------------------------------------------------------------------------
# (RED multi-component) The component budget and the anchor race
# --------------------------------------------------------------------------
#
# Two defects, one method. Both were measured on prod between 2026-08-29
# and 2026-09-01 on the 2026 STARLUX shoot (110 RED clips), and both are
# present in Codemill's own ``importFileToPlaceholder``:
#
# * A — the budget was over-declared by one. ``video=len(extras) + 1``
#   assumes the anchor contributes a video component; an anchor Vidispine
#   cannot decode yields a binaryComponent instead, which fills the
#   container slot and NO video slot, so one declared slot is never
#   filled and the shape stays a placeholder for ever.
# * B — the anchor was imported without waiting for the extras. The
#   anchor's job is the only thing that evaluates the placeholder, and
#   nothing re-evaluates it afterwards.
#
# Neither was observable before: the multi-component branch was dead in
# the whole suite, the declared VALUES were recorded and never asserted,
# and the stub promoted on any ``jobId``.


def _multi_component_run(
    item_helper, extras, job_helper=None, main_file=None, attached=frozenset(), **kwargs
):
    """``_multi_component_import``, but handing back the CLIP too.

    The new failures are reported on ``Clip.error``, which the returned
    ``itemapi`` cannot show.
    """
    clip = Clip(umid="MULTI-COMPONENT", item_id="VX-100")
    imported = clip._import_multi_component(
        main_file or {"file_id": "VX-41-MAIN", "path": "main.mxf", "type": "video"},
        extras,
        "VX-100-SHAPE",
        ["Admin"],
        None,
        None,
        item_helper,
        job_helper or _JobHelperFake(),
        attached_file_ids=attached,
        **kwargs,
    )
    return clip, imported, item_helper.itemapi


def _video_extras(count):
    return [
        {"file_id": f"VX-41-S{index:03d}", "path": f"s{index:03d}.R3D", "type": "video"}
        for index in range(1, count + 1)
    ]


def _containers(api):
    return [call for call in api.calls if call[2] == "container"]


# --- (1) the conditional budget -------------------------------------------


def test_a_deducible_anchor_declares_a_video_slot_of_its_own(migrated_db):
    """``video = len(extras) + 1`` STAYS correct when the anchor deduces.

    Mutation killed: turning the branch into the constant
    ``video=len(extra_files)``. Vidispine then refuses the anchor with
    ``400 … VIDEO_COMPONENT`` (measured 2026-08-31) — the fix is a
    branch, never a constant in either direction.
    """
    _clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-77"}), _video_extras(3)
    )

    assert imported is True
    assert api.counts == [{"container": 1, "video": 4, "audio": None}]


def test_a_non_deducible_anchor_declares_one_video_slot_fewer(migrated_db):
    """Defect A. The anchor fills the CONTAINER slot with a binary
    component and no video slot at all, so declaring one for it leaves
    the shape a placeholder for ever.

    Mutation killed: counting ``extra_files + [main_file]``
    unconditionally, i.e. the inherited Codemill arithmetic.
    """
    _clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-77"}),
        _video_extras(3),
        main_file={
            "file_id": "VX-41-MAIN",
            "path": "main.R3D",
            "type": "video",
            MAIN_FILE_YIELDS_VIDEO: False,
        },
    )

    assert imported is True
    assert api.counts == [{"container": 1, "video": 3, "audio": None}]


def test_a_non_deducible_anchor_with_an_audio_extra_declares_both(migrated_db):
    """The PRODUCTION shape: N `.R3D` span files plus the card's `.wav`.

    Both counters are non-``None`` at once, and the video conditional
    must not disturb the audio one.

    Mutation killed: applying the anchor's verdict to the audio count.
    """
    _clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-77"}),
        _video_extras(3) + [{"file_id": "VX-41-WAV", "path": "a.wav", "type": "audio"}],
        main_file={
            "file_id": "VX-41-MAIN",
            "path": "main.R3D",
            "type": "video",
            MAIN_FILE_YIELDS_VIDEO: False,
        },
    )

    assert imported is True
    assert api.counts == [{"container": 1, "video": 3, "audio": 1}]


def test_the_anchors_own_audio_track_is_still_counted(migrated_db):
    """The conditional is about the VIDEO slot only: a P2 clip whose
    anchor is an audio track keeps today's audio count."""
    _clip, _imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-77"}),
        [{"file_id": "VX-41-A1", "path": "a1.wav", "type": "audio"}],
        main_file={"file_id": "VX-41-A0", "path": "a0.wav", "type": "audio"},
    )

    assert api.counts == [{"container": 1, "video": None, "audio": 2}]


def test_a_provider_dict_without_a_type_never_raises_in_the_count(migrated_db):
    """`.get("type")`, not `["type"]`: the count also feeds the diagnostic
    printed when a placeholder is a dead end, and a KeyError there would
    replace the message with a traceback."""
    clip = Clip(umid="NO-TYPE", item_id="VX-100")

    assert clip._count_media_components({"file_id": "VX-41-M"}, [{"file_id": "X"}]) == (
        None,
        None,
    )


# --- (2) the wait: landed, not stopped ------------------------------------


def test_the_anchor_waits_for_the_extras_and_then_proceeds(migrated_db, monkeypatch):
    """Defect B, at the seam: the extras' job ids are COLLECTED and
    polled until they settle — where they used to be logged and dropped —
    and the anchor is then imported.

    Mutations killed: deleting the wait (nothing is polled at all);
    replacing the loop with a single pass, or setting the bound to 0
    (the anchor is never imported).
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    job_helper = _JobHelperFake(in_progress_until=2)

    _clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-EXTRA"}),
        _video_extras(1),
        job_helper=job_helper,
    )

    assert imported is True
    # The loop really looped: three reads of the same component job, and
    # only then the anchor's own job, which `Clip.job` fetches.
    assert job_helper.polled == ["VX-EXTRA", "VX-EXTRA", "VX-EXTRA", "VX-MAIN"]
    assert len(_containers(api)) == 1


def test_a_component_job_that_stops_without_attaching_is_not_landed(
    migrated_db, monkeypatch
):
    """A job that reaches a TERMINAL status attaches nothing when it
    ended FAILED_TOTAL or ABORTED. Treating "terminal" as success
    re-creates the incomplete component set this story removes.

    Mutation killed: making the wait's verdict the job status alone.
    """
    # Both sleeps, or this test burns the poll interval AND the two
    # confirmation floors in real seconds.
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 0.0)

    clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-FAILED"},
            # The job stopped; the shape stayed empty.
            landed=[],
        ),
        _video_extras(1),
    )

    assert imported is False
    assert _containers(api) == []
    assert "VX-41-S001" in clip.error
    assert "never attached their file" in clip.error


def test_the_wait_re_reads_the_shape_only_when_the_pending_set_moves(
    migrated_db, monkeypatch
):
    """The shape read is bought by a job FINISHING, not by a poll.

    Nothing but a job finishing attaches a file, so a pass in which every
    job is still running has nothing new to see. The poll backs off; the
    shape read did not, so an un-gated read spent one shape query per
    couple of seconds of a 300 s wait on an answer it already had — and
    a wedged Vidispine is precisely the case this runs for.

    Mutation killed: replacing
    `if last_pending_count is None or len(pending) != last_pending_count:`
    with `if True:`, which hammers the shape once per pass with the
    suite otherwise green.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    item_helper = _ItemHelperFake(
        {"jobId": "VX-MAIN"}, component_response={"jobId": "VX-EXTRA"}
    )
    job_helper = _JobHelperFake(in_progress_until=2)

    _clip, imported, _api = _multi_component_run(
        item_helper, _video_extras(1), job_helper=job_helper
    )

    assert imported is True
    # THREE passes of the wait...
    assert job_helper.polled == ["VX-EXTRA", "VX-EXTRA", "VX-EXTRA", "VX-MAIN"]
    # ...and TWO shape reads: the opening one, and the one the pending
    # set emptying earned. Not one per pass.
    assert item_helper.shape_reads == 2


def test_the_wait_names_an_unreadable_shape_rather_than_guessing(
    migrated_db, monkeypatch
):
    """The FIFTH reason, and the one the operator's Troubleshooting table
    resolves to "look at the shape", not "look at the job queue".

    Saying components "never attached although every job stopped" would
    name a cause this run never observed: it could not read the shape at
    all, so whether they attached is unknown — and an anchor closing a
    component set on an unknown state is what manufactures the
    placeholder nothing can promote.

    Mutation killed: garbling the "the placeholder shape could not be
    read" string, which USER_GUIDE.md tells operators to match on.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 0.0)
    clip = Clip(umid="WAIT-UNREADABLE-SHAPE", item_id="VX-100")
    item_helper = _UnreadableShapeItemHelper()

    reason = clip._wait_for_components_to_land(
        ["VX-STOPPED"], {"VX-41-S001"}, _JobHelperFake(), item_helper, 0.01
    )

    assert reason is not None
    assert "the placeholder shape could not be read" in reason
    assert "1 extra component(s)" in reason
    # NOT the other verdict: nothing was observed to have failed to
    # attach, so the reason must not say so.
    assert "never attached" not in reason


@pytest.mark.parametrize(
    "job_ids, says, must_not_say",
    [
        (
            ["VX-STOPPED"],
            "although every import job has stopped",
            "no import job was running for them",
        ),
        (
            [],
            "no import job was running for them",
            "although every import job has stopped",
        ),
    ],
)
def test_the_wait_says_which_of_the_two_unlanded_states_it_is_in(
    migrated_db, monkeypatch, job_ids, says, must_not_say
):
    """Two DIFFERENT operator actions behind one shape of failure.

    "Every job stopped" sends the operator to the Vidispine job queue to
    read why they ended FAILED_TOTAL or ABORTED; "no import job was
    running" says there was nothing to look at there and the components
    were never sent. The reason reaches the operator verbatim, in the
    run's error list, and it is all they have to tell the two apart.

    Mutation killed: inverting `if job_ids` in the arm selector, which
    swaps the two and sends the operator to the job queue for a clip
    that started no job at all.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 0.0)
    clip = Clip(umid="WAIT-ARMS", item_id="VX-100")
    # The shape READS — it simply holds nothing.
    item_helper = _ItemHelperFake({"jobId": "VX-MAIN"}, landed=[])

    reason = clip._wait_for_components_to_land(
        job_ids, {"VX-41-S001"}, _JobHelperFake(), item_helper, 0.01
    )

    assert reason is not None
    assert "1 extra component(s) never attached their file" in reason
    assert says in reason
    assert must_not_say not in reason


@pytest.mark.parametrize(
    # Factories, for the same reason as the listing arms above.
    "build_job_helper, arm",
    [
        (lambda: _JobHelperFake(error=RuntimeError("vidispine is wedged")), "raises"),
        (lambda: _JobHelperFake(missing=True), "answers None"),
    ],
)
def test_a_job_that_cannot_be_read_counts_as_still_running(
    migrated_db, monkeypatch, build_job_helper, arm
):
    """ONE rule, BOTH arms: ``getJob`` raising and ``getJob`` answering
    ``None``. "I could not tell" is not "it finished".

    Mutation killed: catching only the exception (or only the ``None``)
    and letting the other arm fall through as landed.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 0.0)

    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-UNK"}),
        _video_extras(1),
        job_helper=build_job_helper(),
    )

    assert imported is False, arm
    assert _containers(api) == []
    assert "still running" in clip.error


def test_an_expired_wait_does_not_import_the_anchor(migrated_db, monkeypatch):
    """ "A wait that expires is a failure, not a fallback."

    Importing the anchor anyway is exactly the race being removed, so the
    clip is counted failed and the message names the pending jobs.

    Mutation killed: falling through to the main import when the bound
    expires.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)

    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-STUCK"}),
        _video_extras(1),
        job_helper=_JobHelperFake(in_progress=True),
    )

    assert imported is False
    assert clip.job_id is None
    assert _containers(api) == []
    assert "VX-STUCK" in clip.error


def test_the_rest_entry_point_does_not_get_the_scans_patience(migrated_db, monkeypatch):
    """The bound is PER ENTRY POINT: `views.py` holds a request thread and
    a database connection for the whole of the wait.

    Mutation killed: ignoring ``component_wait_seconds`` and always using
    the scan's ``EXTRA_COMPONENT_WAIT_SECONDS``.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    # Distinguishable from the REST bound, and finite: a mutant that
    # ignores `component_wait_seconds` waits THIS long and then reports
    # "after 5s" instead of "after 0s".
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 5.0)

    clip, imported, _api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-STUCK"}),
        _video_extras(1),
        job_helper=_JobHelperFake(in_progress=True),
        component_wait_seconds=0.0,
    )

    assert imported is False
    assert "after 0s" in clip.error


def test_the_rest_bound_is_shorter_than_the_scans():
    """The two constants, unpatched: `views.py` holds a request thread and
    a database connection for the whole wait; the cron holds nothing."""
    assert clip_module.REST_EXTRA_COMPONENT_WAIT_SECONDS < (
        clip_module.EXTRA_COMPONENT_WAIT_SECONDS
    )


def test_an_extra_component_without_a_job_id_stops_the_anchor(migrated_db):
    """No job means the file will never attach, so the set the anchor is
    about to close can never be complete.

    Mutation killed: keeping the old ``log.info("... but got no job in
    response")`` and importing the anchor regardless.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={}),
        _video_extras(1),
    )

    assert imported is False
    assert _containers(api) == []
    assert "VX-41-S001" in clip.error


def test_an_already_attached_component_is_not_imported_twice(migrated_db):
    """RESUME, at the seam: a component a previous run attached is skipped,
    because importing it again adds a second component of its type and
    overflows the budget just declared.

    Mutation killed: ignoring ``attached_file_ids``.
    """
    _clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-EXTRA"},
            # S001 was attached by the previous run; S002 lands in this one.
            landed=["VX-41-S001", "VX-41-S002"],
        ),
        _video_extras(2),
        attached=frozenset({"VX-41-S001"}),
    )

    assert imported is True
    assert [entry["query"]["fileId"] for entry in api.imports] == [
        "VX-41-S002",
        "VX-41-MAIN",
    ]


# --- (3) the three placeholder states -------------------------------------


class _ShapeItemHelperFake:
    def __init__(self, shapes):
        self.shapes = shapes
        self.itemapi = None

    def getItemShapesFromNames(self, item_id, names, placeholder=False):
        return self.shapes


def _placeholder(clip, shapes, main_file=None, extra_files=None):
    return clip._get_or_create_placeholder_shape(
        None,
        _ShapeItemHelperFake(shapes),
        main_file=main_file,
        extra_files=extra_files,
    )


def test_the_classifier_survives_an_extra_with_no_file_id(migrated_db):
    """A media-typed extra Vidispine has no file id for is a DEFECT TO
    REPORT, not a filter criterion — `importable_extras` deliberately
    keeps it so `_import_multi_component` can fail the clip BY NAME
    rather than ingest it silently short of media.

    That decision runs through the classifier first, and there the
    missing id is a `None` in a set of strings. `sorted(expected -
    attached)` on it raises `TypeError`, which turns the deliberate named
    failure into an unhandled exception one rung earlier — the clip never
    reaches the message written for it.

    Mutation killed: removing `if media_file.get("file_id")` from
    `_expected_file_ids`. The only other test supplying
    `{"file_id": None}` goes through `_import_multi_component` directly
    and never touches the classifier, so the mutation left the suite
    green.
    """
    clip = Clip(umid="EXPECTED-NAMELESS", item_id="VX-NAMELESS")
    main_file = {"file_id": "VX-41-MAIN", "path": "main.R3D", "type": "video"}
    extra_files = [
        {"file_id": "VX-41-S001", "path": "s001.R3D", "type": "video"},
        # Vidispine does not know this one yet.
        {"file_id": None, "path": "s002.R3D", "type": "video"},
        {"file_id": "VX-41-S003", "path": "s003.R3D", "type": "video"},
    ]

    resolved = _placeholder(
        clip,
        # A previous run got one component on: this is a RESUME, which is
        # the only arm that computes `missing`.
        [_ShapeFake("VX-NAMELESS-SHAPE", ["VX-41-S001"])],
        main_file=main_file,
        extra_files=extra_files,
    )

    assert resolved.shape_id == "VX-NAMELESS-SHAPE"
    assert resolved.attached_file_ids == frozenset({"VX-41-S001"})
    # The id-less extra is not EXPECTED of the shape — no import will
    # ever attach it, so it could only ever read as permanently missing.
    assert clip._expected_file_ids(main_file, extra_files) == frozenset(
        {"VX-41-MAIN", "VX-41-S001", "VX-41-S003"}
    )


def test_an_empty_placeholder_is_still_reused_silently(migrated_db, caplog):
    """The FR-36 incomplete-import retry rung must not become noisy."""
    clip = Clip(umid="EMPTY-PLACEHOLDER", item_id="VX-EMPTY")

    with caplog.at_level(logging.ERROR):
        resolved = _placeholder(clip, [_ShapeFake("VX-EMPTY-SHAPE", [])])

    assert resolved.shape_id == "VX-EMPTY-SHAPE"
    assert resolved.attached_file_ids == frozenset()
    assert clip.error == ""
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_a_partially_filled_placeholder_is_resumed_not_condemned(migrated_db, caplog):
    """A transient Vidispine slowdown must not permanently poison an item.

    Mutation killed: the pre-story blanket "non-empty means not a
    placeholder", which turned every interrupted import into manual work.
    """
    clip = Clip(umid="PARTIAL-PLACEHOLDER", item_id="VX-PARTIAL")

    with caplog.at_level(logging.ERROR):
        resolved = _placeholder(
            clip,
            [_ShapeFake("VX-PARTIAL-SHAPE", ["VX-41-S001"])],
            main_file={"file_id": "VX-41-MAIN", "path": "main.R3D", "type": "video"},
            extra_files=_video_extras(2),
        )

    assert resolved.shape_id == "VX-PARTIAL-SHAPE"
    assert resolved.attached_file_ids == frozenset({"VX-41-S001"})
    assert clip.error == ""
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_a_complete_but_unpromotable_placeholder_reports_through_the_error_channel(
    migrated_db, caplog
):
    """The silence this story removes, and the ONE state a re-run cannot fix.

    ``log.info("Shape is not a placeholder")`` + a bare ``failed`` is how
    31 stuck clips reproduced the same invisible exit on every retry.

    Mutation killed: reverting to ``log.info`` and dropping ``self.error``.
    """
    clip = Clip(umid="STUCK-PLACEHOLDER", item_id="VX-STUCK")
    extras = _video_extras(2)

    with caplog.at_level(logging.ERROR):
        resolved = _placeholder(
            clip,
            [
                _ShapeFake(
                    "VX-STUCK-SHAPE",
                    ["VX-41-MAIN"] + [extra["file_id"] for extra in extras],
                )
            ],
            main_file={"file_id": "VX-41-MAIN", "path": "main.R3D", "type": "video"},
            extra_files=extras,
        )

    assert resolved.shape_id is None
    assert "VX-STUCK-SHAPE" in clip.error
    # The slots it is short of are NAMED, not merely counted.
    assert "video=3" in clip.error
    # The advice has to be followable: `--replace` is NOT, because its
    # shape query runs `placeholder=False` and a stuck item exposes no
    # shape to remove.
    assert "--replace" not in clip.error
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


# --- (4) end to end, through the real import and the stub's budget --------


SPANNED_NAME = "fakespanned"


class SpannedIngestableProvider(IngestableProvider):
    """`red`-shaped: an anchor plus N span files, one clip.

    ``deducible`` is what ``red.anchor_yields_video_component`` answers
    off REDline's ``Abs TC``; everything else about the provider is
    ``IngestableProvider``.
    """

    def __init__(self, extras=2, deducible=True):
        IngestableProvider.__init__(self)
        self.name = "Fake Spanned Provider"
        self.machine_name = SPANNED_NAME
        self.extras = extras
        self.deducible = deducible

    def getClipMainMediaFile(self, clip):
        return {
            "file_id": clip.file_id,
            "path": clip.path,
            "type": "video",
            MAIN_FILE_YIELDS_VIDEO: self.deducible,
        }

    def getClipAdditionalMediaFiles(self, clip):
        return [
            {
                "file_id": f"{clip.file_id}-S{index:03d}",
                "path": f"{clip.path}/s{index:03d}.ing",
                "type": "video",
                "track": 1,
                "order": index + 1,
            }
            for index in range(1, self.extras + 1)
        ]


@pytest.fixture
def spanned_provider():
    """Registers a spanned provider; the test picks its shape."""

    def _register(extras=2, deducible=True):
        provider = SpannedIngestableProvider(extras=extras, deducible=deducible)
        Clip._PROVIDER_CACHE[SPANNED_NAME] = provider
        return provider

    yield _register
    Clip._PROVIDER_CACHE.pop(SPANNED_NAME, None)


def _declared_counts():
    """The VALUES passed to ``updatePlaceholderComponentCount``.

    Recorded but never asserted before this story — the hole defect A
    lived in on the stub side as well as on the double side.
    """
    return [
        {key: details[key] for key in ("container", "video", "audio")}
        for name, details in VidispineFake.calls
        if name == "updatePlaceholderComponentCount"
    ]


def _imported_components():
    return [
        details["component"]
        for name, details in VidispineFake.calls
        if name == "doImportToPlaceholder"
    ]


def _queue_import_jobs(*job_ids):
    for job_id in job_ids:
        VidispineFake.set_import_response({} if job_id is None else {"jobId": job_id})


def test_a_non_deducible_anchor_promotes_its_shape_end_to_end(
    migrated_db, es_fake, es_page, spanned_provider, tmp_path, collection_seam
):
    """Defect A, through the REAL import against the stub's budget.

    The anchor lands in a BINARY slot; the declaration has to leave that
    slot to it, or the shape never promotes.

    Mutation killed: ``video=len(extras) + 1`` unconditionally — the
    third slot is then declared, never filled, and the placeholder
    survives the import with all of the clip's media on it.
    """
    spanned_provider(extras=2, deducible=False)
    rel = "2026/AH_20260101_binaryanchor"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPBIN"]), total=1))
    VidispineFake.set_non_deducible("VX-41-CLIPBIN")
    _queue_import_jobs("VX-JOB-B1", "VX-JOB-B2", "VX-JOB-B3")

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (response["ingested"], response["failed"]) == (1, 0)
    assert response["errors"] == []
    assert _declared_counts() == [{"container": 1, "video": 2, "audio": None}]
    item_id = Clip.objects.get(pk=f"{rel}/CLIPBIN").item_id
    # PROMOTED: the shape stopped being a placeholder.
    assert VidispineFake.placeholder_shape(item_id) is None


def test_a_deducible_anchor_promotes_its_shape_end_to_end(
    migrated_db, es_fake, es_page, spanned_provider, tmp_path, collection_seam
):
    """The other direction of the same branch.

    Mutation killed: ``video=len(extras)`` — the anchor's own video
    component then overflows the budget and the stub answers the 400
    Vidispine answered on 2026-08-31.
    """
    spanned_provider(extras=2, deducible=True)
    rel = "2026/AH_20260101_videoanchor"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPVID"]), total=1))
    _queue_import_jobs("VX-JOB-V1", "VX-JOB-V2", "VX-JOB-V3")

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (response["ingested"], response["failed"]) == (1, 0)
    assert _declared_counts() == [{"container": 1, "video": 3, "audio": None}]
    item_id = Clip.objects.get(pk=f"{rel}/CLIPVID").item_id
    assert VidispineFake.placeholder_shape(item_id) is None


def test_the_anchor_is_deferred_until_the_extras_have_landed(
    migrated_db, es_fake, es_page, spanned_provider, tmp_path, collection_seam
):
    """Defect B, end to end and thread-free.

    The extras' jobs settle only when POLLED, so the anchor's import
    either waited for them — and finds a complete component set to
    promote — or it did not, and leaves the item holding all its media on
    a placeholder nothing will ever re-evaluate.

    Mutation killed: removing the wait.
    """
    spanned_provider(extras=2, deducible=False)
    rel = "2026/AH_20260101_race"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPRACE"]), total=1))
    VidispineFake.set_non_deducible("VX-41-CLIPRACE")
    VidispineFake.hold_jobs(settle_after_polls=1)
    _queue_import_jobs("VX-JOB-R1", "VX-JOB-R2", "VX-JOB-R3")

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (response["ingested"], response["failed"]) == (1, 0)
    item_id = Clip.objects.get(pk=f"{rel}/CLIPRACE").item_id
    assert VidispineFake.placeholder_shape(item_id) is None


def test_an_expired_wait_leaves_a_failed_clip_and_a_reusable_placeholder(
    migrated_db,
    es_fake,
    es_page,
    spanned_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """The expiry cell, end to end: failed, the anchor never sent, the
    reason in the OPERATOR'S REPORT, and the item still ingestable.

    Mutation killed: appending to ``response["errors"]`` only in the
    ``except`` arm, which is how the run printed "N failed, 0 errors".
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    spanned_provider(extras=2, deducible=False)
    rel = "2026/AH_20260101_stuckjob"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPSTUCKJOB"]), total=1))
    VidispineFake.hold_jobs(settle_after_polls=None)
    # Two extras and NO anchor: a third response would leak, which is
    # exactly the assertion the autouse reset makes.
    _queue_import_jobs("VX-JOB-S1", "VX-JOB-S2")

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (response["failed"], response["ingested"]) == (1, 0)
    assert _imported_components() == ["video", "video"]
    [error] = response["errors"]
    assert "still running" in error
    item_id = Clip.objects.get(pk=f"{rel}/CLIPSTUCKJOB").item_id
    # RESUMABLE: the placeholder survives, so the next run finishes it
    # instead of declaring the item manual work.
    assert VidispineFake.placeholder_shape(item_id) is not None


def test_the_next_run_resumes_a_partially_imported_placeholder(
    migrated_db, es_fake, es_page, spanned_provider, tmp_path, collection_seam
):
    """The whole point of change 3: a failure leaves a RESUMABLE item.

    Run one attaches the first span file and then fails (the second
    component's import starts no job). Run two imports what is missing —
    and only what is missing — then the anchor, and the shape promotes.

    Mutation killed: treating any non-empty placeholder as a dead end,
    which turns a transient Vidispine hiccup into permanent manual work.
    """
    spanned_provider(extras=2, deducible=False)
    rel = "2026/AH_20260101_resume"
    sources = _ingestable_page(tmp_path, rel, ["CLIPRESUME"])
    VidispineFake.set_non_deducible("VX-41-CLIPRESUME")
    es_fake.push(es_page(sources, total=1))
    es_fake.push(es_page(sources, total=1))
    # Run 1: S001 gets a job, S002 gets none. Run 2: S002, then the anchor.
    _queue_import_jobs("VX-JOB-P1", None, "VX-JOB-P2", "VX-JOB-P3")

    # ONE folder object for both runs: a second unsaved instance would
    # trip the (path, storage_id) unique constraint, not the resume.
    folder = _folder(tmp_path, rel)

    first = folder.ingest(providers=[SPANNED_NAME])

    assert (first["failed"], first["ingested"]) == (1, 0)
    assert len(first["errors"]) == 1
    item_id = Clip.objects.get(pk=f"{rel}/CLIPRESUME").item_id
    shape = VidispineFake.placeholder_shape(item_id)
    assert [entry["id"] for entry in shape["files"]] == ["VX-41-CLIPRESUME-S001"]

    second = folder.ingest(providers=[SPANNED_NAME])

    assert (second["failed"], second["ingested"]) == (0, 1)
    assert second["errors"] == []
    # Only the MISSING component was re-imported, then the anchor.
    assert _imported_components() == ["video", "video", "video", "container"]
    assert VidispineFake.placeholder_shape(item_id) is None


def test_a_complete_unpromotable_placeholder_reaches_the_run_summary(
    migrated_db,
    es_fake,
    es_page,
    ingestable_provider,
    tmp_path,
    collection_seam,
    caplog,
):
    """End to end: the state the two defects left on 31 production clips.

    Nothing can promote it, and the operator has to be TOLD — in
    ``response["errors"]``, which is what the folder line and the run
    summary count, not only in ``portal.log``.
    """
    rel = "2026/AH_20260101_stuckshape"
    sources = _ingestable_page(tmp_path, rel, ["CLIPSTUCKSHAPE"])
    Clip(
        umid=f"{rel}/CLIPSTUCKSHAPE",
        path=rel,
        storage_id=STORAGE_ID,
        item_id="VX-STUCKSHAPE",
        status=Clip.STATUS_PLACHOLDER_CREATED,
    ).save()
    VidispineFake.set_item("VX-STUCKSHAPE")
    VidispineFake.set_original_shape(
        "VX-STUCKSHAPE",
        "VX-STUCKSHAPE-SHAPE",
        files=[{"id": "VX-41-CLIPSTUCKSHAPE", "storage": "VX-41"}],
        placeholder=True,
    )
    es_fake.push(es_page(sources, total=1))

    with caplog.at_level(logging.ERROR):
        response = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (response["failed"], response["ingested"]) == (1, 0)
    [error] = response["errors"]
    assert "STILL a placeholder" in error
    assert any(
        "STILL a placeholder" in record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.ERROR
    )
    # Nothing was imported into it: the point is that a retry cannot fix
    # this state, not that it should try harder.
    assert "doImportToPlaceholder" not in VidispineFake.call_names()


# --- (5) the stub itself: without these, the pins above can pass for the
# --- wrong reason


def _stub_placeholder(item_id="VX-STUB"):
    api = ItemAPIFake()
    api.createPlaceholderShape(item_id)
    return api, VidispineFake.placeholder_shape(item_id)["id"]


def test_the_stub_promotes_only_when_every_declared_slot_is_filled(migrated_db):
    """Mutation killed: the stub promoting the placeholder on any ``jobId``.

    That is what made both production defects structurally unobservable.
    """
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=2)
    VidispineFake.set_non_deducible("VX-STUB-MAIN")
    _queue_import_jobs("VX-J1", "VX-J2", "VX-J3")

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S1"}, component="video"
    )
    api.doImportToPlaceholder("VX-STUB", query={"fileId": "VX-STUB-MAIN"})

    # One video slot short: the anchor's binary component filled the
    # container and nothing else.
    assert VidispineFake.placeholder_shape("VX-STUB") is not None

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S2"}, component="video"
    )
    # ... and NOTHING re-evaluates a placeholder after the anchor's job.
    assert VidispineFake.placeholder_shape("VX-STUB") is not None


def test_the_stub_promotes_when_the_anchor_lands_on_a_full_budget(migrated_db):
    """The counterpart: the same imports in the order the fix produces."""
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=2)
    VidispineFake.set_non_deducible("VX-STUB-MAIN")
    _queue_import_jobs("VX-J1", "VX-J2", "VX-J3")

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S1"}, component="video"
    )
    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S2"}, component="video"
    )
    api.doImportToPlaceholder("VX-STUB", query={"fileId": "VX-STUB-MAIN"})

    assert VidispineFake.placeholder_shape("VX-STUB") is None


def test_the_stub_refuses_a_component_over_the_declared_budget(migrated_db):
    """Vidispine's ``400 … VIDEO_COMPONENT``, measured 2026-08-31.

    It is what makes "declare one fewer, always" wrong, so the suite has
    to be able to answer it.
    """
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=1)
    _queue_import_jobs("VX-J1", "VX-J2")

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S1"}, component="video"
    )
    with pytest.raises(ComponentBudgetExceeded) as excinfo:
        api.doImportToPlaceholder(
            "VX-STUB", query={"fileId": "VX-STUB-S2"}, component="video"
        )

    assert "VIDEO_COMPONENT" in str(excinfo.value)


def test_the_stub_lands_a_non_deducible_anchor_in_a_binary_slot(migrated_db):
    """The signature measured on all 31 stuck clips: the container slot
    is satisfied by a binaryComponent, and no video slot is."""
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=0)
    VidispineFake.set_non_deducible("VX-STUB-MAIN")
    _queue_import_jobs("VX-J1")

    api.doImportToPlaceholder("VX-STUB", query={"fileId": "VX-STUB-MAIN"})

    shape = VidispineFake.item_shapes["VX-STUB"][0]
    assert shape["landed"] == {"container": 1, "binary": 1}
    assert shape["placeholder"] is False


def test_the_stub_refuses_a_budget_declared_against_an_unknown_shape(migrated_db):
    """A declaration that lands nowhere cannot hold anything to it —
    which is exactly what the pre-story no-op did."""
    api, _shape_id = _stub_placeholder()

    with pytest.raises(UnknownShape):
        api.updatePlaceholderComponentCount(
            "VX-STUB", "VX-NOT-A-SHAPE", container=1, video=1
        )


def test_an_import_that_starts_no_job_consumes_nothing(migrated_db):
    """A response without a ``jobId`` started nothing, so the next
    attempt must find the slot free — otherwise the resume path this
    story adds would be refused by a budget the failed attempt had
    already spent. Nothing is consumed before a LANDING (measured
    2026-09-01), and there is no reservation to leak.

    Mutation killed: counting the request rather than the landing.
    """
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=1)
    _queue_import_jobs(None, "VX-J1")

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S1"}, component="video"
    )
    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S1"}, component="video"
    )

    shape = VidispineFake.item_shapes["VX-STUB"][0]
    assert shape["landed"] == {"video": 1}
    assert "claimed" not in shape


def test_two_in_flight_imports_are_both_accepted_and_both_land(migrated_db):
    """The two measured halves of the budget, together. 2026-09-01: a
    second import fired while the first is still in flight is ACCEPTED —
    nothing is consumed before a landing. 2026-09-02: when the second
    lands into the budget the first has meanwhile filled, it ATTACHES
    ANYWAY — both jobs ``FINISHED``, no error, no warning, two video
    components on a shape declared for one.

    This is the fact the fake got wrong until 2026-09-02: it claimed a
    slot at request time, so the second import was refused with a 400
    that Vidispine never sends at that moment — and that manufactured
    the proof of a wrong resume guard (spec D5).

    Mutation killed: checking ``landed`` in ``_land_component`` (the
    second file is then dropped or refused); any reservation at request
    time (the second import is then refused).
    """
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=1)
    VidispineFake.hold_jobs(settle_after_polls=None)
    _queue_import_jobs("VX-J1", "VX-J2")

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S1"}, component="video"
    )
    # ACCEPTED: nothing has landed yet.
    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S2"}, component="video"
    )
    VidispineFake.settle_component_job("VX-J1")
    VidispineFake.settle_component_job("VX-J2")

    shape = VidispineFake.item_shapes["VX-STUB"][0]
    assert shape["landed"] == {"video": 2}
    assert [entry["id"] for entry in shape["files"]] == ["VX-STUB-S1", "VX-STUB-S2"]
    # And only NOW, with one landed too many, is a further import refused.
    _queue_import_jobs("VX-J3")
    with pytest.raises(ComponentBudgetExceeded) as excinfo:
        api.doImportToPlaceholder(
            "VX-STUB", query={"fileId": "VX-STUB-S3"}, component="video"
        )
    assert "video=1, 2 landed" in str(excinfo.value)


def test_the_poll_dedupes_job_ids_and_logs_only_when_the_set_changes(
    migrated_db, monkeypatch, caplog
):
    """A wedged Vidispine is the case this loop RUNS for, so it must not
    be hammered and must not drown the report.

    Two extras answering with the SAME job id are polled once per pass,
    not twice; the interval BACKS OFF; and the "waiting for" line is
    emitted when the pending set CHANGES, not once per iteration.

    Mutations killed: dropping the ``dict.fromkeys`` dedupe; a constant
    poll interval; logging inside the loop unconditionally.
    """
    slept = []
    # The MODULE's own name, never `time.sleep`: patching the attribute
    # on the shared `time` module would mutate it for the whole
    # interpreter, which is a cross-test hazard rather than a seam.
    monkeypatch.setattr(clip_module, "_sleep", slept.append)
    job_helper = _JobHelperFake(in_progress_until=3)

    with caplog.at_level(logging.INFO):
        _clip, imported, _api = _multi_component_run(
            _ItemHelperFake(
                {"jobId": "VX-MAIN"}, component_response={"jobId": "VX-SAME"}
            ),
            _video_extras(2),
            job_helper=job_helper,
        )

    assert imported is True
    # Deduped: one read per PASS for the one distinct job, not two.
    assert job_helper.polled.count("VX-SAME") == 4
    # Backed off, strictly.
    assert slept == sorted(slept) and slept[0] < slept[-1]
    # One line for the whole wait: the pending set never changed.
    assert (
        len(
            [
                record
                for record in caplog.records
                if "waiting for" in record.getMessage()
            ]
        )
        == 1
    )


# --------------------------------------------------------------------------
# (RED multi-component, review iteration 2) The gaps the second review found
# --------------------------------------------------------------------------
#
# Three of them, and each one turns a case the story exists to FIX back
# into the case it exists to PREVENT:
#
# * the resume read only the files ATTACHED to the shape, so a run that
#   started while the previous run's component jobs were still in flight
#   re-imported them — and a slot is consumed when a component LANDS
#   (measured 2026-09-01 with a control arm), so the duplicate is either
#   refused with the 400 the resume exists to avoid (first one landed) or
#   accepted and landed TWICE, silently (first one still in flight —
#   measured 2026-09-02);
# * the REST bound was per CLIP, so a 50-clip folder held a DRF thread
#   and its database connection for 50 x 30 s;
# * a placeholder holding somebody else's file was treated as a resume.


class _ListedJobFake:
    """One row of ``getAllJobsForItem`` — the resume path's other input.

    Modelled on the two REAL ``PLACEHOLDER_IMPORT`` jobs measured on the
    6.2.1 server 2026-09-01 (VX-696013, VX-696024 on item VX-216268):
    the source is exposed through ``getSourceFilePath()`` as a
    PERCENT-ENCODED ``file://`` URI, and NOT through the job ``data``,
    which carries no ``sourceFileId`` and no ``fileIds`` at all.
    """

    def __init__(
        self,
        job_id,
        in_progress=True,
        source=None,
        filename=None,
        target=None,
        raises=None,
        status=None,
    ):
        self._job_id = job_id
        self._in_progress = in_progress
        self._status = status
        self._source = source
        self._filename = filename
        self._target = target
        self._raises = raises

    def getId(self):
        return self._job_id

    def inProgress(self):
        if self._raises is not None:
            raise self._raises
        return self._in_progress

    def getStatus(self):
        if self._raises is not None:
            raise self._raises
        return self._status

    def getSourceFilePath(self):
        return self._source

    def getFilename(self):
        return self._filename

    def getTargetItem(self):
        return self._target


class _JobListingHelperFake(_JobHelperFake):
    """``_JobHelperFake`` plus the item's job LISTING.

    ``listing_error`` is a different arm from ``_JobHelperFake``'s
    ``error``: one costs the resume its knowledge of what is in flight,
    the other is the wait's unreadable-job rule.
    """

    def __init__(self, jobs=(), listing_error=None, **kwargs):
        _JobHelperFake.__init__(self, **kwargs)
        self.jobs = list(jobs)
        self.listing_error = listing_error
        self.listed = []

    def getAllJobsForItem(self, item_id, job_type=None, max_hits=0):
        self.listed.append((item_id, job_type))
        if self.listing_error is not None:
            raise self.listing_error
        return list(self.jobs)


# The production shape, measured: an absolute path under a storage root
# WITH SPACES, percent-encoded into a `file://` URI. The provider's own
# `path` for the same file is storage-RELATIVE, so the two never compare
# equal and the encoding has to come off first.
MEASURED_SOURCE_URI = (
    "file:///mnt/PAD_Storage/AA%20-%20RUSHES%20TAPELESS/2026/"
    "K001_K003_0804O6.RDC/K001_K003_0804O6_002.R3D"
)
MEASURED_RELATIVE_PATH = "2026/K001_K003_0804O6.RDC/K001_K003_0804O6_002.R3D"


def _r3d_extra(path, file_id="VX-41-S002"):
    return [{"file_id": file_id, "path": path, "type": "video"}]


def _in_flight(clip, job_helper, media_files):
    return clip._in_flight_component_files(job_helper, media_files)


def test_a_running_import_job_is_identified_by_its_source_path(migrated_db):
    """The whole mechanism, on the MEASURED job shape.

    A `PLACEHOLDER_IMPORT` job's `data` carries no `sourceFileId` and no
    `fileIds` on this server — the first version of this code read those
    two keys, which appear in the vendor's own test fixture, and would
    have answered "nothing in flight" for ever, silently degrading to
    the re-import the skip exists to prevent. The source comes off
    `getSourceFilePath()`.

    Mutations killed: reading the job DATA instead of the accessor;
    comparing the RAW URI, which never matches because this storage root
    contains spaces and the URI is percent-encoded.
    """
    clip = Clip(umid="INFLIGHT", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[
            _ListedJobFake(
                "VX-696013",
                source=MEASURED_SOURCE_URI,
                filename="K001_K003_0804O6_002.R3D",
                target="VX-216268",
            )
        ]
    )

    assert _in_flight(clip, job_helper, _r3d_extra(MEASURED_RELATIVE_PATH)) == {
        "VX-41-S002": ["VX-696013"]
    }
    # Filtered to placeholder imports: a transcode job for the same item
    # is not a component import and must not be read as one.
    assert job_helper.listed == [("VX-216268", clip_module.PLACEHOLDER_IMPORT_JOB_TYPE)]


def test_the_percent_encoding_is_what_makes_the_naive_comparison_fail(migrated_db):
    """The isolated proof that the unquote is load bearing, on the two
    space-carrying segments of the real root.

    Mutation killed: dropping the `unquote` — the normalised path then
    keeps `%20` and matches neither the relative path nor the basename.
    """
    normalised = clip_module._normalised_source_uri(MEASURED_SOURCE_URI)

    assert normalised == (
        "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2026/"
        "K001_K003_0804O6.RDC/K001_K003_0804O6_002.R3D"
    )
    assert "%20" not in normalised
    assert normalised.endswith("/" + MEASURED_RELATIVE_PATH)


def test_a_job_that_has_stopped_is_not_in_flight(migrated_db):
    """A finished job has already attached its file (or failed): the
    shape's own file set decides it, not this listing.

    Mutation killed: dropping the ``inProgress()`` filter, which would
    make every past import look in flight and stop a resume for ever.
    """
    clip = Clip(umid="INFLIGHT-DONE", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[_ListedJobFake("VX-DONE", in_progress=False, source=MEASURED_SOURCE_URI)]
    )

    assert _in_flight(clip, job_helper, _r3d_extra(MEASURED_RELATIVE_PATH)) == {}


def test_the_suffix_match_is_component_aligned(migrated_db):
    """The job's path is absolute under the storage root and the
    provider's is relative to it, so the match is a SUFFIX — and a bare
    `endswith` would let a neighbouring clip's span file satisfy this
    one, which is precisely the stem-prefix hazard the RED grouping
    rules already had to close.

    Mutation killed: `source.endswith(path)` without the separator.
    """
    clip = Clip(umid="INFLIGHT-ALIGNED", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[
            _ListedJobFake(
                "VX-NEIGHBOUR",
                source="file:///mnt/root/2026/CARD/xA001_002.R3D",
                filename="xA001_002.R3D",
                target="VX-216268",
            )
        ]
    )

    assert _in_flight(clip, job_helper, _r3d_extra("2026/CARD/A001_002.R3D")) == {}


def test_the_basename_is_the_last_resort_when_the_roots_disagree(migrated_db):
    """`getSourceFilePath()` is absolute under whatever root Vidispine
    mounted; `VSFile.getPath()` is relative to the storage. When the two
    cannot be aligned, equal basenames decide — safe here because the
    jobs are already this ITEM's and the candidates already this CLIP's,
    and two of one clip's media files never share a basename.

    Mutation killed: dropping the basename rule, after which a storage
    whose mount point differs from its Vidispine path resumes nothing.
    """
    clip = Clip(umid="INFLIGHT-BASENAME", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[
            _ListedJobFake(
                "VX-ELSEWHERE",
                source="file:///some/other/mount/A001_002.R3D",
                filename="A001_002.R3D",
                target="VX-216268",
            )
        ]
    )

    assert _in_flight(clip, job_helper, _r3d_extra("2026/CARD/A001_002.R3D")) == {
        "VX-41-S002": ["VX-ELSEWHERE"]
    }


def test_a_job_targeting_another_item_is_never_counted(migrated_db):
    """`getAllJobsForItem` already filters, but a job that NAMES another
    item is not this item's by any reading — and skipping a component on
    its word is the one unsafe direction here."""
    clip = Clip(umid="INFLIGHT-OTHER-ITEM", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[
            _ListedJobFake("VX-OTHER", source=MEASURED_SOURCE_URI, target="VX-999999")
        ]
    )

    assert _in_flight(clip, job_helper, _r3d_extra(MEASURED_RELATIVE_PATH)) == {}


@pytest.mark.parametrize(
    # FACTORIES, not instances. A double built in the parametrize list is
    # constructed once at COLLECTION and shared by every run of the test,
    # so its `polled`/`listed` history accumulates across them and the
    # arms become order-dependent.
    "build_job_helper, arm",
    [
        (
            lambda: _JobListingHelperFake(listing_error=RuntimeError("wedged")),
            "the listing raises",
        ),
        (
            lambda: _JobListingHelperFake(
                jobs=[_ListedJobFake("VX-X", raises=RuntimeError())]
            ),
            "one job cannot be read",
        ),
        (
            lambda: _JobListingHelperFake(jobs=[_ListedJobFake("VX-X", source=None)]),
            "the job cannot name its source",
        ),
        (
            lambda: _JobListingHelperFake(jobs=[_ListedJobFake("VX-X", source="")]),
            "the source is empty",
        ),
    ],
)
def test_an_unreadable_job_listing_leaves_the_resume_on_attached_files(
    migrated_db, build_job_helper, arm, caplog
):
    """ "I could not tell" here is the OPPOSITE of the wait's rule, and
    deliberately so. The wait treats an unreadable job as still running
    because closing the component set on an unknown outcome is what
    manufactures the unpromotable placeholder. Here the unknown answer
    costs nothing but knowledge: the caller falls back to today's
    behaviour (import it), which is at worst the pre-existing 400 — loud,
    reported, and recoverable — rather than a component that is never
    imported because an accessor was missing or a path unreadable.

    Mutation killed: letting the exception escape, or treating a job
    with no readable source as matching whatever is left.
    """
    clip = Clip(umid="INFLIGHT-UNREADABLE", item_id="VX-216268")
    job_helper = build_job_helper()

    with caplog.at_level(logging.WARNING):
        assert (
            _in_flight(clip, job_helper, _r3d_extra(MEASURED_RELATIVE_PATH)) == {}
        ), arm


def test_a_job_object_without_the_accessors_is_not_in_flight(migrated_db):
    """The accessors are read with ``getattr``: a job object that has
    none of them says nothing, rather than raising inside an ingest."""
    clip = Clip(umid="INFLIGHT-NO-ACCESSOR", item_id="VX-216268")

    class _BareJob:
        def getId(self):
            return "VX-BARE"

        def inProgress(self):
            return True

    assert (
        _in_flight(
            clip,
            _JobListingHelperFake(jobs=[_BareJob()]),
            _r3d_extra(MEASURED_RELATIVE_PATH),
        )
        == {}
    )


def test_a_component_whose_job_is_still_in_flight_is_not_re_imported(migrated_db):
    """The iteration-2 gap, at the seam.

    S001 is not on the shape yet — its job from the previous run has not
    attached it — so the attached-files test alone would import it again.
    A slot is consumed when a component LANDS, so the duplicate lands too
    and Vidispine answers the measured ``400 … VIDEO_COMPONENT``: the
    resumable case becomes the dead end the resume exists to avoid.

    Mutation killed: ignoring ``in_flight_component_files``.
    """
    _clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-EXTRA"},
            landed=["VX-41-S001", "VX-41-S002"],
        ),
        _video_extras(2),
        in_flight_component_files={"VX-41-S001": ["VX-STILL-RUNNING"]},
    )

    assert imported is True
    # S001 was NOT re-imported; only S002 and then the anchor.
    assert [entry["query"]["fileId"] for entry in api.imports] == [
        "VX-41-S002",
        "VX-41-MAIN",
    ]


def test_the_in_flight_job_is_waited_for_like_any_other(migrated_db, monkeypatch):
    """Not re-importing it is only half: the anchor must still not close
    the component set before that job has landed.

    Mutation killed: skipping the file AND its job, which would import
    the anchor into exactly the race defect B is.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    job_helper = _JobHelperFake(in_progress=True)

    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-EXTRA"}),
        _video_extras(1),
        job_helper=job_helper,
        in_flight_component_files={"VX-41-S001": ["VX-STILL-RUNNING"]},
    )

    assert imported is False
    assert _containers(api) == []
    assert "VX-STILL-RUNNING" in clip.error
    assert job_helper.polled == ["VX-STILL-RUNNING"]


def test_a_fresh_placeholder_buys_no_job_listing(
    migrated_db, es_fake, es_page, spanned_provider, tmp_path, collection_seam
):
    """A shape this run just MINTED cannot have an earlier run's jobs on
    it, so the happy path keeps exactly the Vidispine calls it made
    before this story.

    Mutation killed: listing the item's jobs unconditionally, which adds
    one query per clip to every scan for a case that cannot occur.
    """
    spanned_provider(extras=1, deducible=False)
    rel = "2026/AH_20260101_freshshape"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPFRESH"]), total=1))
    VidispineFake.set_non_deducible("VX-41-CLIPFRESH")
    _queue_import_jobs("VX-JOB-F1", "VX-JOB-F2")

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (response["ingested"], response["failed"]) == (1, 0)
    assert "getAllJobsForItem" not in VidispineFake.call_names()


def test_a_resume_racing_an_in_flight_job_finishes_on_the_run_after(
    migrated_db,
    es_fake,
    es_page,
    spanned_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """The iteration-2 gap, end to end, in the three runs it really takes.

    Run 1: S001's job stalls, S002 lands, the wait expires — failed,
    resumable. Run 2: S002 is attached and S001's job is STILL RUNNING,
    so NOTHING is imported (importing S001 again is the 400) and the run
    fails again with its reason in the operator's report. Run 3: the job
    has landed, only the anchor is missing, and the shape promotes.

    Mutation killed: resuming on attached files alone — run 2 then
    re-imports S001 and the item is poisoned for good.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    spanned_provider(extras=2, deducible=False)
    rel = "2026/AH_20260101_inflight"
    sources = _ingestable_page(tmp_path, rel, ["CLIPINFLIGHT"])
    VidispineFake.set_non_deducible("VX-41-CLIPINFLIGHT")
    # The paths Vidispine knows the two span files by. The plugin's
    # import only ever sends a `fileId`, so this is knowledge the fake
    # has and the plugin does not — which is exactly the asymmetry that
    # makes `getSourceFilePath()` the resume's only identifier.
    for index in (1, 2):
        VidispineFake.set_file_path(
            f"VX-41-CLIPINFLIGHT-S{index:03d}", f"{rel}/s{index:03d}.ing"
        )
    for _run in range(3):
        es_fake.push(es_page(sources, total=1))
    # S001's job never settles on its own; S002's settles on first poll.
    VidispineFake.stall_jobs("VX-JOB-I1")
    _queue_import_jobs("VX-JOB-I1", "VX-JOB-I2", "VX-JOB-I3")

    folder = _folder(tmp_path, rel)

    first = folder.ingest(providers=[SPANNED_NAME])

    assert (first["failed"], first["ingested"]) == (1, 0)
    item_id = Clip.objects.get(pk=f"{rel}/CLIPINFLIGHT").item_id
    assert [
        entry["id"] for entry in VidispineFake.placeholder_shape(item_id)["files"]
    ] == ["VX-41-CLIPINFLIGHT-S002"]

    second = folder.ingest(providers=[SPANNED_NAME])

    assert (second["failed"], second["ingested"]) == (1, 0)
    [error] = second["errors"]
    assert "VX-JOB-I1" in error
    # NOTHING new was imported: two component imports in run 1, none in
    # run 2. A re-imported S001 would be a third.
    assert _imported_components() == ["video", "video"]

    VidispineFake.settle_component_job("VX-JOB-I1")
    third = folder.ingest(providers=[SPANNED_NAME])

    assert (third["failed"], third["ingested"]) == (0, 1)
    assert third["errors"] == []
    assert _imported_components() == ["video", "video", "container"]
    assert VidispineFake.placeholder_shape(item_id) is None


# --- the placeholder holding somebody else's file -------------------------


def test_a_placeholder_holding_a_foreign_file_is_refused_not_resumed(
    migrated_db, caplog
):
    """``attached - expected`` is not a resume in any sense: importing
    into it would attach this clip's media to another clip's item.

    Mutation killed: computing only ``expected - attached``, which reads
    a shape full of somebody else's files as "everything still to do".
    """
    clip = Clip(umid="FOREIGN-PLACEHOLDER", item_id="VX-FOREIGN")

    with caplog.at_level(logging.ERROR):
        resolved = _placeholder(
            clip,
            [_ShapeFake("VX-FOREIGN-SHAPE", ["VX-41-SOMEONE-ELSE"])],
            main_file={"file_id": "VX-41-MAIN", "path": "main.R3D", "type": "video"},
            extra_files=_video_extras(1),
        )

    assert resolved.shape_id is None
    assert "VX-41-SOMEONE-ELSE" in clip.error
    assert "not this clip's" in clip.error
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_a_caller_that_names_no_files_still_gets_the_dead_end_verdict(
    migrated_db, caplog
):
    """The foreign-file rung is gated on KNOWING what was expected: with
    no media files supplied, a non-empty placeholder keeps the pre-story
    verdict rather than being called foreign.

    Mutation killed: dropping the ``and expected`` guard, which turns
    every such call into a foreign-file refusal with a misleading
    message.
    """
    clip = Clip(umid="NO-FILES-PLACEHOLDER", item_id="VX-NOFILES")

    with caplog.at_level(logging.ERROR):
        resolved = _placeholder(clip, [_ShapeFake("VX-NOFILES-SHAPE", ["VX-41-X"])])

    assert resolved.shape_id is None
    assert "STILL a placeholder" in clip.error


def test_a_created_placeholder_says_it_was_created(migrated_db):
    """``created`` is what gates the job listing, so it has to be true
    exactly when the shape was minted here."""
    clip = Clip(umid="CREATED-PLACEHOLDER", item_id="VX-NEW")

    created = clip._get_or_create_placeholder_shape(None, _CreatingItemHelperFake())
    reused = _placeholder(clip, [_ShapeFake("VX-NEW-SHAPE", [])])

    assert (created.shape_id, created.created) == ("VX-NEW-SHAPE", True)
    assert (reused.shape_id, reused.created) == ("VX-NEW-SHAPE", False)


class _CreatingItemAPIFake:
    def createPlaceholderShape(self, item_id, runasuser=None):
        return b"VX-NEW-SHAPE"


class _CreatingItemHelperFake:
    def __init__(self):
        self.itemapi = _CreatingItemAPIFake()

    def getItemShapesFromNames(self, item_id, names, placeholder=False):
        return []


# --- promoted, unreadable, and the shape read -----------------------------


class _ShapeReadFake:
    """The two shape queries the wait's landing check makes, scripted.

    ``placeholder=True`` and the default (non-placeholder) are a
    three-state FILTER on the real endpoint, not an include flag, so an
    item whose shape has been PROMOTED answers the first with nothing.
    """

    def __init__(self, placeholder=None, promoted=None, error=None):
        self.placeholder = placeholder
        self.promoted = promoted
        self.error = error
        self.queries = []

    def getItemShapesFromNames(self, item_id, names, placeholder=False):
        self.queries.append(placeholder)
        if self.error is not None:
            raise self.error
        shapes = self.placeholder if placeholder else self.promoted
        return [] if shapes is None else [_ShapeFake("VX-SHAPE", shapes)]


def test_the_landing_check_reads_the_placeholder_shapes_files(migrated_db):
    clip = Clip(umid="LANDING-PLACEHOLDER", item_id="VX-100")
    helper = _ShapeReadFake(placeholder=["VX-41-S001"])

    assert clip._placeholder_file_ids(helper) == {"VX-41-S001"}
    # The second query is not bought when the first answered.
    assert helper.queries == [True]


def test_a_promoted_shape_is_landed_not_unreadable(migrated_db):
    """A shape that has been PROMOTED answers the placeholder query with
    nothing, and its files are all there — the opposite of unknown.

    Mutation killed: returning ``None`` on an empty placeholder listing,
    which fails a clip whose import actually succeeded.
    """
    clip = Clip(umid="LANDING-PROMOTED", item_id="VX-100")
    helper = _ShapeReadFake(placeholder=None, promoted=["VX-41-S001", "VX-41-MAIN"])

    assert clip._placeholder_file_ids(helper) == {"VX-41-S001", "VX-41-MAIN"}
    assert helper.queries == [True, False]


def test_no_shape_at_all_is_unreadable(migrated_db):
    """Neither a placeholder nor a promoted original: nothing is known,
    and the wait must treat that as not landed."""
    clip = Clip(umid="LANDING-NOTHING", item_id="VX-100")

    assert clip._placeholder_file_ids(_ShapeReadFake()) is None


def test_a_shape_read_that_raises_is_unreadable(migrated_db, caplog):
    """One rule with the job arms: a read that fails is "I could not
    tell", never "it landed"."""
    clip = Clip(umid="LANDING-RAISES", item_id="VX-100")

    with caplog.at_level(logging.WARNING):
        assert (
            clip._placeholder_file_ids(_ShapeReadFake(error=RuntimeError("wedged")))
            is None
        )


# --- the bounds: the default, the ceiling and the whole-request budget ----


def test_the_default_bound_is_the_scans(migrated_db, monkeypatch):
    """``component_wait_seconds=None`` means the scan's generous bound,
    which is what the cron passes by passing nothing.

    Mutation killed: defaulting to the REST bound, which would cut every
    cron ingest to 30 s without a single test noticing.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)

    clip, imported, _api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-STUCK"}),
        _video_extras(1),
        job_helper=_JobHelperFake(in_progress=True),
        component_wait_seconds=None,
    )

    assert imported is False
    # The patched SCAN constant is what expired, so the message names it.
    assert "after 0s" in clip.error


def test_the_poll_interval_stops_at_the_ceiling(migrated_db, monkeypatch):
    """The backoff has a CEILING: a five-minute wait must not end up
    polling once every two minutes and missing the landing by most of it.

    Mutation killed: removing the ``EXTRA_COMPONENT_POLL_MAX_SECONDS``
    clamp, after which the intervals grow without bound.
    """
    slept = []
    monkeypatch.setattr(clip_module, "_sleep", slept.append)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_MAX_SECONDS", 3.0)

    _clip, imported, _api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-SLOW"}),
        _video_extras(1),
        job_helper=_JobHelperFake(in_progress_until=8),
    )

    assert imported is True
    assert max(slept) == 3.0
    assert slept[-1] == 3.0


def test_the_whole_request_budget_clamps_the_per_clip_bound(migrated_db, monkeypatch):
    """The iteration-2 REST gap: the bound was per CLIP, so a 50-clip
    folder held a DRF thread for 50 x 30 s.

    An ALREADY-EXPIRED request deadline leaves nothing for this clip,
    whatever its own bound says.

    Mutation killed: ignoring ``component_wait_deadline``, after which
    this clip waits its full 5 s.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)

    clip, imported, _api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-STUCK"}),
        _video_extras(1),
        job_helper=_JobHelperFake(in_progress=True),
        component_wait_seconds=5.0,
        component_wait_deadline=time.monotonic() - 1.0,
    )

    assert imported is False
    assert "after 0s" in clip.error
    assert "component-wait budget" in clip.error


def test_a_deadline_further_out_than_the_bound_does_not_shorten_it(
    migrated_db, monkeypatch
):
    """The clamp is a MINIMUM, not a replacement: a request with budget
    to spare leaves the per-clip bound exactly as the entry point set it.

    Mutation killed: always using the remaining budget, which would give
    the FIRST clip of a request the whole request's patience.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)

    clip, imported, _api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-STUCK"}),
        _video_extras(1),
        job_helper=_JobHelperFake(in_progress=True),
        component_wait_seconds=0.0,
        component_wait_deadline=time.monotonic() + 3600.0,
    )

    assert imported is False
    assert "after 0s" in clip.error
    assert "component-wait budget" not in clip.error


def test_the_rest_request_budget_is_bigger_than_one_clips_bound():
    """A request budget below the per-clip bound would make the per-clip
    bound dead code; one far above it would not bound the request."""
    assert (
        clip_module.REST_EXTRA_COMPONENT_WAIT_SECONDS
        < clip_module.REST_COMPONENT_WAIT_BUDGET_SECONDS
        < clip_module.EXTRA_COMPONENT_WAIT_SECONDS
    )


def _rest_ingest_request(payload):
    """A DRF request through the real ``ClipsInPathsView``.

    The repo has no request harness, which is why this endpoint's wiring
    used to be pinned by reading `views.py` as TEXT. A source guard can
    only see that the kwarg NAMES appear: swapping
    `REST_EXTRA_COMPONENT_WAIT_SECONDS` for the scan's 300 s constant, or
    the budget for `None`, kept the whole suite green while the story's
    headline REST property regressed. So the view is driven for real and
    the VALUES are asserted.
    """
    from rest_framework.test import APIRequestFactory, force_authenticate

    from portal.plugins.TapelessIngest.views import ClipsInPathsView

    request = APIRequestFactory().post("/api/clips/", payload, format="json")
    force_authenticate(request, user=None)
    return ClipsInPathsView.as_view()(request)


@pytest.fixture
def rest_ingest_kwargs(monkeypatch):
    """Record what the view really hands `Folder.ingest`/`Clip.ingest`."""
    recorded = {}

    def fake_folder_ingest(self, **kwargs):
        recorded["folder"] = kwargs
        return {"clips": []}

    def fake_clip_ingest(self, **kwargs):
        recorded["clip"] = kwargs
        return {"ingested": True, "failed": False, "skipped": False}

    monkeypatch.setattr(Folder, "ingest", fake_folder_ingest)
    monkeypatch.setattr(Clip, "ingest", fake_clip_ingest)
    monkeypatch.setattr(Clip, "persist_metadatas", lambda self: None)
    return recorded


def _folder_payload():
    """The smallest body `FolderSerializer` accepts.

    Two of these values are dictated by the serializer rather than chosen:
    ``provider_names`` is required, and ``error`` is a plain
    ``CharField()``, so it is required AND may not be blank. A caller
    therefore cannot ingest a folder without claiming it carries an error
    message — a pre-existing wart of `serializers.py`, not something this
    story introduces, and left alone deliberately. It is recorded here
    because the payload otherwise looks arbitrary: with ``error: ""`` the
    view answers 500, not 400, on `folder` never being assigned.
    """
    return {
        "umid": "2026/AH_20260101_rest",
        "path": "2026/AH_20260101_rest",
        "storage_id": STORAGE_ID,
        "provider_names": "file",
        "error": "none",
    }


def test_the_rest_folder_branch_passes_the_short_bound_and_the_budget(
    migrated_db, rest_ingest_kwargs
):
    """VALUES, not names: the folder branch gets the REST per-clip bound
    and the shared component-wait BUDGET.

    Mutations killed: passing `EXTRA_COMPONENT_WAIT_SECONDS` (the cron's
    300 s) as the per-clip bound; passing `None` for the budget; passing
    a DEADLINE instead of a duration (a monotonic instant is thousands of
    seconds, not 60).
    """
    response = _rest_ingest_request({"folder": _folder_payload(), "clips": "__all__"})

    assert response.status_code == 201, getattr(response, "data", response)
    assert rest_ingest_kwargs["folder"] == {
        "user": rest_ingest_kwargs["folder"]["user"],
        "component_wait_seconds": clip_module.REST_EXTRA_COMPONENT_WAIT_SECONDS,
        "component_wait_budget": clip_module.REST_COMPONENT_WAIT_BUDGET_SECONDS,
    }


def test_the_rest_clip_branch_passes_the_short_bound_and_a_live_deadline(
    migrated_db, rest_ingest_kwargs
):
    """The per-clip branch has no scan pass, so it opens the deadline
    itself — and it must be a LIVE one, opened just before the loop.

    Mutations killed: the cron's bound here; no deadline at all; a
    deadline so far out it bounds nothing.
    """
    opened_at = time.monotonic()
    _rest_ingest_request(
        {
            "folder": _folder_payload(),
            "clips": [
                {
                    # `folder_path`, `reference_file` and `provider_name`
                    # are not decoration: they are the Clip model's
                    # non-null, default-less columns, so ClipSerializer
                    # requires them and the branch silently skips the clip
                    # without them — which is how this test first read as
                    # "the view never called ingest".
                    "umid": "2026/AH_20260101_rest/CLIPREST",
                    "path": "2026/AH_20260101_rest",
                    "folder_path": "2026/AH_20260101_rest",
                    "reference_file": "CLIPREST.mxf",
                    "provider_name": "file",
                    "storage_id": STORAGE_ID,
                    "metadatas": {},
                }
            ],
        }
    )

    closed_at = time.monotonic()
    kwargs = rest_ingest_kwargs["clip"]
    assert kwargs["component_wait_seconds"] == (
        clip_module.REST_EXTRA_COMPONENT_WAIT_SECONDS
    )
    # BRACKETED, not bounded from one side. The view opens its deadline at
    # some instant inside the request, so the only exact statement is
    # `opened_at + BUDGET <= deadline <= closed_at + BUDGET`. Comparing
    # against `opened_at` alone fails by however long the request took —
    # which is how this assertion first read `60.0005 <= 60.0`.
    budget = clip_module.REST_COMPONENT_WAIT_BUDGET_SECONDS
    deadline = kwargs["component_wait_deadline"]
    assert opened_at + budget <= deadline <= closed_at + budget


def test_the_component_wait_deadline_opens_after_the_scan_pass(
    migrated_db,
    es_fake,
    es_page,
    spanned_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """The budget is a DURATION and the deadline opens on the INGEST leg.

    `Folder.ingest` runs a full scan pass first. A deadline taken by the
    caller would already have been spent by discovery on a large healthy
    folder, handing every multi-component clip a 0 s bound and reporting
    a wedged Vidispine that is not wedged.

    Mutation killed: `component_wait_deadline = time.monotonic() +
    budget` computed in `Folder.ingest` (before `_scan_pass`) instead of
    inside `_ingest_pass` — the slow scan below then eats the budget.

    The scan pass is made to take longer than the whole budget by
    charging the clock inside the provider's own metadata hook.
    """
    spanned_provider(extras=1, deducible=False)
    rel = "2026/AH_20260101_slowscan"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPSLOW"]), total=1))
    VidispineFake.set_non_deducible("VX-41-CLIPSLOW")
    _queue_import_jobs("VX-JOB-SS1", "VX-JOB-SS2")

    # A monotonic clock the SCAN advances past the budget.
    charged = {"now": 1000.0}
    real_provider = Clip._PROVIDER_CACHE[SPANNED_NAME]
    real_hook = real_provider.getMetadatasFromFile

    def slow_hook(media_file, metadatas, context):
        charged["now"] += 120.0  # twice the budget, spent in DISCOVERY
        return real_hook(media_file, metadatas, context)

    monkeypatch.setattr(real_provider, "getMetadatasFromFile", slow_hook)
    monkeypatch.setattr(clip_module.time, "monotonic", lambda: charged["now"])
    monkeypatch.setattr(folder_module.time, "monotonic", lambda: charged["now"])

    response = _folder(tmp_path, rel).ingest(
        providers=[SPANNED_NAME],
        component_wait_seconds=30.0,
        component_wait_budget=60.0,
    )

    # The clip ingested: its bound was NOT clamped to 0 by a budget the
    # scan had already spent.
    assert (response["ingested"], response["failed"]) == (1, 0)
    assert response["errors"] == []


# --- the anchor's own job-less rung, and the report it has to reach -------


def test_the_anchors_own_job_less_response_reaches_the_run_summary(
    migrated_db, es_fake, es_page, spanned_provider, tmp_path, collection_seam
):
    """The last rung that recorded nothing: the extras all landed, the
    ANCHOR's import answered without a ``jobId``, and the clip was
    counted failed with the operator's report saying nothing at all.

    Mutation killed: dropping ``self.error`` from that rung — the run
    then prints "1 failed, 0 errors" again, which is the silence this
    story is named for.
    """
    spanned_provider(extras=1, deducible=False)
    rel = "2026/AH_20260101_anchornojob"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPANCHOR"]), total=1))
    VidispineFake.set_non_deducible("VX-41-CLIPANCHOR")
    # The extra gets a job; the anchor does not.
    _queue_import_jobs("VX-JOB-A1", None)

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (response["failed"], response["ingested"]) == (1, 0)
    [error] = response["errors"]
    assert "ANCHOR's import response carried no job id" in error
    # ... and it is honest about the components that DID start jobs, so
    # the operator is not told to look for an import that never began.
    assert "next run resumes this item" in error


def test_the_error_channel_is_capped_but_says_it_was_capped(
    migrated_db,
    es_fake,
    es_page,
    spanned_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """A wedged Vidispine fails every clip of a card the same way, and
    one line per clip would bury the rest of the run's report — and the
    Slack message built from it — under N identical lines.

    Mutation killed: removing the cap (the report grows without bound) or
    removing the closing count (the report is silently short).
    """
    monkeypatch.setattr(folder_module, "INGEST_ERROR_REPORT_CAP", 2)
    spanned_provider(extras=1, deducible=False)
    rel = "2026/AH_20260101_flood"
    names = ["CLIPF1", "CLIPF2", "CLIPF3", "CLIPF4"]
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, names), total=len(names)))
    # Every clip's extra import answers without a job: four identical
    # failures, none of which reaches the anchor.
    _queue_import_jobs(*([None] * len(names)))

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert response["failed"] == 4
    assert len(response["errors"]) == 3
    assert "2 further failed clip(s)" in response["errors"][-1]


def test_a_second_import_does_not_inherit_the_first_ones_reason(migrated_db):
    """``self.error`` is what `Folder.ingest` now puts in the report, and
    one clip object can be imported twice in a process.

    Mutation killed: dropping the reset at the top of ``import_file``,
    after which a clip that failed once and then SUCCEEDED still carries
    the old reason for anything that reads it.
    """
    from portal.plugins.TapelessIngest.helpers import TapelessIngestException

    clip = Clip(umid="ERROR-RESET", item_id="VX-100")
    clip.error = "a previous attempt's reason"

    # This row names an item Vidispine cannot resolve, so the import
    # refuses at `create_item` — which is BELOW the reset, and that is
    # the point: the reset is the first statement of the method, so no
    # verdict of this attempt can be attributed to the last one.
    with pytest.raises(TapelessIngestException):
        clip.import_file()

    assert clip.error == ""


# --- the stub's measured semantics ----------------------------------------


def test_re_declaring_the_budget_replaces_it_and_frees_nothing(migrated_db):
    """Measured on prod 2026-09-01 WITH A CONTROL ARM: re-declaring the
    same budget REPLACES the declaration and does NOT give a consumed
    slot back (control: A lands, B refused; measured: B refused too).

    The whole resume path depends on it — a resume re-declares before
    importing what is missing — so the stub has to hold it.

    Mutation killed: clearing ``landed`` on re-declaration, which would
    let the resume import a component the budget has already spent and
    make a test pass that production would refuse.
    """
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=1)
    _queue_import_jobs("VX-J1", "VX-J2")

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S1"}, component="video"
    )
    # The resume's re-declaration, byte for byte the same numbers.
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=1)

    with pytest.raises(ComponentBudgetExceeded):
        api.doImportToPlaceholder(
            "VX-STUB", query={"fileId": "VX-STUB-S2"}, component="video"
        )


def test_the_stub_lists_an_items_component_jobs(migrated_db):
    """Without this the resume path's other input does not exist in the
    suite at all, and "a component whose job is still in flight is not
    re-imported" could only be pinned at the seam.
    """
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, video=1)
    VidispineFake.stall_jobs("VX-J1")
    VidispineFake.set_file_path("VX-STUB-S1", "2026/CARD/s001.ing")
    _queue_import_jobs("VX-J1")

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-S1"}, component="video"
    )

    [job] = JobHelperFake().getAllJobsForItem(
        "VX-STUB", job_type=clip_module.PLACEHOLDER_IMPORT_JOB_TYPE
    )
    assert job.getId() == "VX-J1"
    assert job.inProgress() is True
    assert job.getTargetItem() == "VX-STUB"
    # The MEASURED shape: a percent-encoded `file://` URI under a root
    # that contains spaces. A fake answering a bare path would let the
    # missing `unquote` pass.
    assert job.getSourceFilePath() == (
        "file:///mnt/PAD_Storage/AA%20-%20RUSHES%20TAPELESS/2026/CARD/s001.ing"
    )
    assert job.getFilename() == "s001.ing"
    # ... and the plugin resolves it back to the file it is importing.
    clip = Clip(umid="STUB-INFLIGHT", item_id="VX-STUB")
    assert clip._in_flight_component_files(
        JobHelperFake(),
        [{"file_id": "VX-STUB-S1", "path": "2026/CARD/s001.ing", "type": "video"}],
    ) == {"VX-STUB-S1": ["VX-J1"]}


def _stalled_multi_component_folder(tmp_path, rel, name, es_fake, es_page):
    """A one-clip folder whose extra component job never settles."""
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, [name]), total=1))
    VidispineFake.set_non_deducible(f"VX-41-{name}")
    VidispineFake.hold_jobs(settle_after_polls=None)
    # One extra, and NO anchor response: the anchor is never imported,
    # which is what the wait's expiry means.
    _queue_import_jobs("VX-JOB-W1")
    return _folder(tmp_path, rel)


def test_folder_ingest_hands_the_entry_points_bound_to_every_clip(
    migrated_db,
    es_fake,
    es_page,
    spanned_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """Every hop between the entry point and the wait, in one run.

    `views.py` -> `Folder.ingest` -> `_folder_worker` -> `_ingest_pass`
    -> `Clip.ingest` -> `Clip.import_file` -> `_import_multi_component`.

    Mutation killed: dropping ``component_wait_seconds`` from ANY of
    those hops. The chain still type-checks — every hop defaults to
    ``None`` — and the clip silently gets the cron's five-minute
    patience, here observable as the patched 5 s instead of the 0 s the
    caller asked for. The bound travels as a PARAMETER for exactly this
    reason: folder state would make the same deletion a ``getattr`` that
    answers ``None`` and no signature a test can watch.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    spanned_provider(extras=1, deducible=False)
    rel = "2026/AH_20260101_boundhop"
    folder = _stalled_multi_component_folder(
        tmp_path, rel, "CLIPBOUND", es_fake, es_page
    )

    response = folder.ingest(providers=[SPANNED_NAME], component_wait_seconds=0.0)

    assert (response["failed"], response["ingested"]) == (1, 0)
    [error] = response["errors"]
    assert "after 0s" in error


def test_folder_ingest_hands_the_whole_request_budget_to_every_clip(
    migrated_db,
    es_fake,
    es_page,
    spanned_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """The same chain for the REQUEST's budget, which is what stops a
    50-clip folder costing 50 x the per-clip bound on a held DRF thread.

    Mutation killed: dropping ``component_wait_deadline`` from any hop —
    this clip then spends its full per-clip bound although the request's
    budget was already gone.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 5.0)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    spanned_provider(extras=1, deducible=False)
    rel = "2026/AH_20260101_budgethop"
    folder = _stalled_multi_component_folder(
        tmp_path, rel, "CLIPBUDGET", es_fake, es_page
    )

    response = folder.ingest(
        providers=[SPANNED_NAME],
        component_wait_seconds=5.0,
        # A BUDGET, in seconds. `_ingest_pass` opens the deadline itself,
        # at the start of the ingest leg — 0.0 leaves this clip nothing.
        component_wait_budget=0.0,
    )

    assert (response["failed"], response["ingested"]) == (1, 0)
    [error] = response["errors"]
    assert "component-wait budget" in error


def test_an_undeclared_budget_refuses_nothing(migrated_db):
    """A shape nobody declared a budget for must not be held to one.

    The single-component path never calls
    ``updatePlaceholderComponentCount``, so a default of ``video: 0``
    would have the fake refuse an import for a rule production has never
    applied to it — and every single-component pin in this file would
    then be passing against a fake that is stricter than Vidispine.

    Mutation killed: defaulting the undeclared budget to
    ``{"container": 1, "video": 0, "audio": 0}``.
    """
    api, _shape_id = _stub_placeholder()
    _queue_import_jobs("VX-J1")

    api.doImportToPlaceholder("VX-STUB", query={"fileId": "VX-STUB-MAIN"})

    # It landed, it filled the container slot, and the placeholder is gone.
    assert VidispineFake.placeholder_shape("VX-STUB") is None


def test_an_audio_anchor_fills_an_audio_slot_not_a_video_one(migrated_db):
    """A P2 clip's anchor can BE an audio track, and
    ``_count_media_components`` counts it in the AUDIO budget.

    Mutation killed: deducing "video" from every container import, after
    which the audio branch of the count could be reverted with the suite
    still green — the shape would promote on a video slot the anchor
    never filled.
    """
    api, shape_id = _stub_placeholder()
    api.updatePlaceholderComponentCount("VX-STUB", shape_id, container=1, audio=2)
    VidispineFake.set_audio_anchor("VX-STUB-A0")
    _queue_import_jobs("VX-J1", "VX-J2")

    api.doImportToPlaceholder(
        "VX-STUB", query={"fileId": "VX-STUB-A1"}, component="audio"
    )
    api.doImportToPlaceholder("VX-STUB", query={"fileId": "VX-STUB-A0"})

    shape = VidispineFake.item_shapes["VX-STUB"][0]
    assert shape["landed"] == {"audio": 2, "container": 1}
    assert shape["placeholder"] is False


# --------------------------------------------------------------------------
# (RED multi-component, review iteration 4) The claims that defended nothing
# --------------------------------------------------------------------------
#
# Not wrong code — unpinned code. Each block below is a property the
# story states and that could be reverted with the suite green.


# --- (1) the STATUS layer, which nothing executed ------------------------


def test_a_waiting_job_is_still_coming_although_inprogress_says_no(migrated_db):
    """The whole reason the status layer exists.

    Verified in the vendor bytecode on the 6.2.1 server: `inProgress()`
    is True only for STARTED, READY and STARTED_ASYNCHRONOUS, so a
    WAITING job — an ordinary status on a busy Vidispine, and exactly the
    condition this wait runs for — reads as stopped.

    The double below reports the two DISAGREEING, which is the only way
    to tell the correct reading from the measured-wrong one.

    Mutation killed: `_job_has_stopped` returning `not job.inProgress()`.
    """
    clip = Clip(umid="STATUS-WAITING", item_id="VX-100")
    waiting = _JobFake("VX-WAITING", in_progress=False, status="WAITING")

    assert waiting.inProgress() is False
    assert clip._job_has_stopped(waiting) is False


@pytest.mark.parametrize("status", sorted(clip_module.JOB_STATUSES_TERMINAL))
def test_a_terminal_status_stops_the_job(migrated_db, status):
    clip = Clip(umid="STATUS-TERMINAL", item_id="VX-100")

    # `in_progress=True` on purpose: the STATUS decides, not the boolean.
    assert (
        clip._job_has_stopped(_JobFake("VX-T", in_progress=True, status=status)) is True
    )


@pytest.mark.parametrize("status", sorted(clip_module.JOB_STATUSES_STILL_COMING))
def test_a_still_coming_status_keeps_the_job(migrated_db, status):
    clip = Clip(umid="STATUS-COMING", item_id="VX-100")

    assert (
        clip._job_has_stopped(_JobFake("VX-C", in_progress=False, status=status))
        is False
    )


def test_an_unmodelled_status_is_neither(migrated_db):
    """A status neither set knows must not be GUESSED into one of them:
    the two callers resolve the unknown in opposite directions, and only
    `None` lets them."""
    clip = Clip(umid="STATUS-UNKNOWN", item_id="VX-100")

    assert clip._job_has_stopped(_JobFake("VX-U", status="VIDINET_JOB")) is None


def test_a_job_with_no_status_falls_back_to_inprogress(migrated_db):
    """The fallback is still reachable, and still correct for a job
    object that reports no status at all."""
    clip = Clip(umid="STATUS-NONE", item_id="VX-100")

    assert clip._job_has_stopped(_JobFake("VX-N", in_progress=True)) is False
    assert clip._job_has_stopped(_JobFake("VX-N", in_progress=False)) is True


def test_the_wait_keeps_polling_a_waiting_job(migrated_db, monkeypatch):
    """End of the same rule, at the seam: a WAITING component keeps the
    wait pending instead of being declared stopped on the first pass.

    Mutation killed: reading `inProgress()` in the wait — the component
    is then "stopped without landing" after two confirmations, and the
    clip fails while its import was merely queued.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 0.0)

    clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"}, component_response={"jobId": "VX-QUEUED"}, landed=[]
        ),
        _video_extras(1),
        job_helper=_JobHelperFake(in_progress=False, status="WAITING"),
    )

    assert imported is False
    assert _containers(api) == []
    # The bound is what stopped it — not "every import job has stopped".
    assert "still running" in clip.error
    assert "never attached" not in clip.error


def test_a_waiting_job_is_counted_in_flight_by_the_resume(migrated_db):
    """And the resume's half: a WAITING job is still coming, so its
    component must not be re-imported.

    Mutation killed: `inProgress()` here — the component is re-imported
    while its first import is merely queued, which lands twice and earns
    the measured 400.
    """
    clip = Clip(umid="INFLIGHT-WAITING", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[
            _ListedJobFake(
                "VX-QUEUED",
                in_progress=False,
                status="WAITING",
                source=MEASURED_SOURCE_URI,
                target="VX-216268",
            )
        ]
    )

    assert _in_flight(clip, job_helper, _r3d_extra(MEASURED_RELATIVE_PATH)) == {
        "VX-41-S002": ["VX-QUEUED"]
    }


def test_an_unmodelled_status_is_running_for_the_wait_and_absent_for_the_resume(
    migrated_db, monkeypatch, caplog
):
    """The unknown resolves in OPPOSITE directions, by design: the wait
    must not close the set on an unknown outcome, and the resume must not
    refuse an import on one.

    Mutation killed: making both sides agree — whichever way, one of the
    two fail-safes is lost.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 0.0)
    clip = Clip(umid="STATUS-BOTH", item_id="VX-216268")

    # The RESUME: not in flight, so the caller falls back to importing.
    assert (
        _in_flight(
            clip,
            _JobListingHelperFake(
                jobs=[
                    _ListedJobFake(
                        "VX-ODD",
                        status="SOMETHING_NEW",
                        source=MEASURED_SOURCE_URI,
                        target="VX-216268",
                    )
                ]
            ),
            _r3d_extra(MEASURED_RELATIVE_PATH),
        )
        == {}
    )

    # The WAIT: still running, so the anchor is not imported.
    with caplog.at_level(logging.WARNING):
        waited, imported, api = _multi_component_run(
            _ItemHelperFake(
                {"jobId": "VX-MAIN"},
                component_response={"jobId": "VX-ODD"},
                landed=[],
            ),
            _video_extras(1),
            job_helper=_JobHelperFake(status="SOMETHING_NEW"),
        )

    assert imported is False
    assert _containers(api) == []
    assert "still running" in waited.error
    assert any("unmodelled status" in r.getMessage() for r in caplog.records)


# --- (2) importable_extras, with the shape it was written for ------------


def _xdcam_shaped_extras():
    """A span file plus `xdcam`'s sidecar dict — the real defect shape."""
    return [
        {"file_id": "VX-41-S001", "path": "card/s001.mxf", "type": "video"},
        # `providers/xdcam.py` really builds this, with a REAL file id.
        {
            "file_id": "VX-41-XML",
            "path": "card/clip.xml",
            "type": "metadatas",
            "track": 1,
            "order": 1,
        },
    ]


def test_a_non_media_extra_is_neither_budgeted_nor_expected(migrated_db):
    """`xdcam`'s `metadatas` dict carries a real file id and no import
    ever sends it.

    Mutation killed: filtering the import loop but not `_expected_file_ids`
    — the sidecar's id then sits in `expected` for ever, `missing` is
    never empty, the shape never reaches the dead-end verdict and every
    run re-enters the resume.
    """
    clip = Clip(umid="XDCAM-SHAPE", item_id="VX-100")
    main_file = {"file_id": "VX-41-MAIN", "path": "card/main.mxf", "type": "video"}

    assert clip._count_media_components(main_file, _xdcam_shaped_extras()) == (
        None,
        2,
    )
    assert clip._expected_file_ids(main_file, _xdcam_shaped_extras()) == frozenset(
        {"VX-41-MAIN", "VX-41-S001"}
    )


def test_a_shape_holding_only_the_importable_files_is_the_dead_end(migrated_db, caplog):
    """The consequence, at the rung that decides resumability: with the
    sidecar excluded, a shape holding the anchor and the span file is
    COMPLETE — a dead end to report — not an eternal resume.
    """
    clip = Clip(umid="XDCAM-DEADEND", item_id="VX-XDCAM")
    main_file = {"file_id": "VX-41-MAIN", "path": "card/main.mxf", "type": "video"}

    with caplog.at_level(logging.ERROR):
        resolved = _placeholder(
            clip,
            [_ShapeFake("VX-XDCAM-SHAPE", ["VX-41-MAIN", "VX-41-S001"])],
            main_file=main_file,
            extra_files=_xdcam_shaped_extras(),
        )

    assert resolved.shape_id is None
    assert "STILL a placeholder" in clip.error
    # And the sidecar is NOT reported as a missing component.
    assert "VX-41-XML" not in clip.error


def test_a_media_extra_with_no_file_id_is_reported_not_filtered(migrated_db):
    """The other half, and the opposite verdict: a VIDEO extra with no
    file id is a defect, not a filter criterion.

    Excluding it made it budget nothing, expect nothing, import nothing
    and report nothing — and the clip came back INGESTED with a span file
    missing from the item.

    Mutation killed: `and media_file.get("file_id")` back in
    `importable_extras`.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-E"}),
        [
            {"file_id": "VX-41-S001", "path": "card/s001.R3D", "type": "video"},
            {"file_id": None, "path": "card/s002.R3D", "type": "video"},
        ],
    )

    assert imported is False
    assert api.calls == []
    assert "card/s002.R3D" in clip.error
    assert "no Vidispine file id" in clip.error


# --- (3) LANDING_CONFIRMATIONS, which nothing observed -------------------


class _LaggingShapeItemHelperFake(_ItemHelperFake):
    """A shape whose file list LAGS the job that attached it.

    Every other double answers a constant shape, and the stub lands the
    file inside the very call that settles the job — so the shape can
    never lag, and `LANDING_CONFIRMATIONS = 1` changed nothing anywhere.
    This one is empty on the first read after the jobs go terminal and
    complete on the second, which is the race the confirmations exist to
    absorb.
    """

    def __init__(self, *args, complete_on_read=2, **kwargs):
        _ItemHelperFake.__init__(self, *args, **kwargs)
        self.complete_on_read = complete_on_read

    def getItemShapesFromNames(self, item_id, names, placeholder=False):
        self.shape_reads += 1
        landed = ["VX-41-S001"] if self.shape_reads >= self.complete_on_read else []
        return [_ShapeFake("VX-100-SHAPE", landed)]


def test_a_file_that_lands_between_two_reads_is_still_a_success(
    migrated_db, monkeypatch
):
    """The confirmations' whole purpose.

    Mutation killed: `LANDING_CONFIRMATIONS = 1` — the single read then
    catches the shape mid-attachment and the clip fails although every
    component landed.
    """
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 0.0)

    clip, imported, api = _multi_component_run(
        _LaggingShapeItemHelperFake(
            {"jobId": "VX-MAIN"}, component_response={"jobId": "VX-LAG"}
        ),
        _video_extras(1),
    )

    assert imported is True
    assert clip.error == ""
    assert len(_containers(api)) == 1


def test_the_confirmation_floor_is_not_clamped_away_by_an_expired_bound(
    migrated_db, monkeypatch
):
    """Past the deadline the ordinary poll sleep clamps to 0, so without
    a floor of its own the two confirmations run back to back and absorb
    none of the race they exist for.

    Mutation killed: `_sleep(max(0.0, min(interval, deadline - now)))` in
    the confirmation branch too.
    """
    slept = []
    monkeypatch.setattr(clip_module, "_sleep", slept.append)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 7.0)

    _clip, imported, _api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"}, component_response={"jobId": "VX-DEAD"}, landed=[]
        ),
        _video_extras(1),
    )

    assert imported is False
    # The bound was already 0 and the floor survived it.
    assert slept == [7.0]


# --- (4) the poll-sleep clamp -------------------------------------------


def test_the_poll_sleep_never_overshoots_the_remaining_bound(migrated_db, monkeypatch):
    """A 15 s ceiling on a 30 s REST bound would otherwise overshoot the
    deadline by most of a poll — and on that path the overshoot is a
    request thread and its database connection held past the budget.

    Every other test either patches the interval to 0 or leaves the 300 s
    bound, so `deadline - now` never won the `min` and `_sleep(interval)`
    stayed green.

    Mutation killed: `_sleep(interval)`.
    """
    slept = []
    monkeypatch.setattr(clip_module, "_sleep", slept.append)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 60.0)
    monkeypatch.setattr(clip_module, "LANDING_CONFIRMATION_SECONDS", 0.0)

    _clip, imported, _api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-SLOW"}),
        _video_extras(1),
        job_helper=_JobHelperFake(in_progress=True),
        component_wait_seconds=0.25,
    )

    assert imported is False
    # Never longer than the bound itself, whatever the interval says.
    assert slept and max(slept) <= 0.25


# --- (5) the report cap on the RAISING arm, and shared between the two ---


def test_the_cap_also_bounds_the_raising_arm(
    migrated_db,
    es_fake,
    es_page,
    ingestable_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """The `except` arm predates this story and was uncapped, so a folder
    whose every clip RAISED still flooded the operator's report — and the
    cap test only ever drove the non-raising path.

    Mutation killed: appending unconditionally in the `except` arm.
    """
    monkeypatch.setattr(folder_module, "INGEST_ERROR_REPORT_CAP", 2)
    names = ["CLIPR1", "CLIPR2", "CLIPR3", "CLIPR4"]
    rel = "2026/AH_20260101_raising"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, names), total=len(names)))

    def boom(self, **kwargs):
        raise RuntimeError("vidispine is wedged")

    monkeypatch.setattr(Clip, "ingest", boom)

    response = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert response["failed"] == 4
    assert len(response["errors"]) == 3
    assert "2 further failed clip(s)" in response["errors"][-1]


def test_the_two_failure_arms_share_one_counter(
    migrated_db,
    es_fake,
    es_page,
    ingestable_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """One report, one cap. Counted separately, a folder that fails both
    ways would print up to 2 x the cap.

    Mutation killed: a second counter for the `except` arm.
    """
    monkeypatch.setattr(folder_module, "INGEST_ERROR_REPORT_CAP", 2)
    names = ["CLIPM1", "CLIPM2", "CLIPM3", "CLIPM4"]
    rel = "2026/AH_20260101_mixed"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, names), total=len(names)))

    real_ingest = Clip.ingest
    seen = {"n": 0}

    def half_and_half(self, **kwargs):
        seen["n"] += 1
        if seen["n"] % 2:
            raise RuntimeError("this one raised")
        self.error = "this one reported a reason"
        return {"ingested": False, "failed": True, "skipped": False, "replaced": False}

    monkeypatch.setattr(Clip, "ingest", half_and_half)

    response = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert response["failed"] == 4
    # Two reasons of EITHER kind, then the count — not two of each.
    assert len(response["errors"]) == 3
    assert "2 further failed clip(s)" in response["errors"][-1]
    assert real_ingest is not None  # the real method is restored by monkeypatch


# --- (6) the endpoint's own contract ------------------------------------


def test_an_invalid_folder_body_is_a_400_naming_the_fields(migrated_db):
    """It used to fall through with `folder` unbound and answer 500 on an
    UnboundLocalError, blaming the server for the client's payload.

    Mutation killed: dropping the `is_valid` guard, or answering 500.
    """
    response = _rest_ingest_request({"folder": {"path": ""}, "clips": "__all__"})

    assert response.status_code == 400
    # It says WHICH fields, so the caller can fix the body.
    assert set(response.data) >= {"umid", "path"}


def test_the_endpoint_builds_a_folder_from_the_model_fields_only(
    migrated_db, rest_ingest_kwargs
):
    """`FolderSerializer` declares `error`, which `Folder` has no field
    for, so `Folder(**validated_data)` raised `TypeError` for EVERY
    accepted body — this endpoint could not succeed for any input.

    The client's own identity must still survive the filter: `umid` maps
    to the model's `id` through the serializer's `source`.

    Mutation killed: passing `validated_data` straight to the
    constructor; or filtering so aggressively that `id`/`path` are lost.
    """
    payload = _folder_payload()

    response = _rest_ingest_request({"folder": payload, "clips": "__all__"})

    assert response.status_code == 201
    assert "folder" in rest_ingest_kwargs


def test_the_clip_serializer_accepts_raise_exception_as_a_keyword(migrated_db):
    """`super().is_valid(raise_exception)` passed it POSITIONALLY, which
    DRF 3.16 refuses — `is_valid` is keyword-only there, so every call
    through this override raised `TypeError` before reaching validation.

    Mutation killed: reverting any of the three call sites to positional.
    """
    from rest_framework.exceptions import ValidationError

    payload = {
        "umid": "2026/AH_20260101_ser/CLIPSER",
        "path": "2026/AH_20260101_ser",
        "storage_id": STORAGE_ID,
        "metadatas": {},
    }

    # A BOOL, not a `TypeError`. Whether this particular body validates
    # is the serializer's business; that the override can be CALLED at
    # all is what regressed — DRF 3.16's `is_valid` is keyword-only, so
    # every call through the three `super()` sites raised before it ever
    # reached validation.
    assert isinstance(ClipSerializer(data=payload).is_valid(), bool)
    assert isinstance(ClipSerializer(data=payload).is_valid(False), bool)

    # And the keyword really reaches DRF: invalid data raises
    # ValidationError, not TypeError.
    with pytest.raises(ValidationError):
        ClipSerializer(data={"umid": ""}).is_valid(raise_exception=True)


# --- (7-11) the guards the fourth review found undefended ----------------


def test_the_anchor_is_not_re_imported_when_it_is_already_attached(migrated_db):
    """`attached_file_ids` was consulted only inside the extras loop, so
    the anchor import was UNCONDITIONAL: a placeholder whose container
    import had landed (an interrupted run whose anchor went first) was
    classified incomplete, resumed, and the anchor imported a SECOND
    time — the duplicate component and the measured 400.

    Mutation killed: dropping the guard in `_import_multi_component`.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-E"}),
        _video_extras(1),
        attached=frozenset({"VX-41-MAIN"}),
    )

    assert imported is False
    assert _containers(api) == []
    assert "already attached" in clip.error


def test_the_classifier_calls_an_attached_anchor_a_dead_end(migrated_db, caplog):
    """And the rung above it: whatever else is or is not attached, an
    anchor already on a shape that is STILL a placeholder means the
    placeholder has been evaluated and nothing re-evaluates one.

    Mutation killed: classifying it as an incomplete resume, which sends
    the run back to re-import the anchor for ever.
    """
    clip = Clip(umid="ANCHOR-LANDED", item_id="VX-ANCHOR")

    with caplog.at_level(logging.ERROR):
        resolved = _placeholder(
            clip,
            # The anchor landed; one span file never did.
            [_ShapeFake("VX-ANCHOR-SHAPE", ["VX-41-MAIN"])],
            main_file={"file_id": "VX-41-MAIN", "path": "main.R3D", "type": "video"},
            extra_files=_video_extras(1),
        )

    assert resolved.shape_id is None
    assert "already holds this clip's anchor file" in clip.error
    assert "VX-41-S001" in clip.error


def test_an_anchor_whose_job_is_still_running_is_waited_for_not_re_imported(
    migrated_db,
):
    """The other guard on the same import: its job is still in flight, so
    a second container import would duplicate the component. Wait for the
    one that is already running.

    Mutation killed: consulting `in_flight` only inside the extras loop.
    """
    item_helper = _ItemHelperFake(
        {"jobId": "VX-MAIN"},
        component_response={"jobId": "VX-E"},
        landed=["VX-41-S001", "VX-41-MAIN"],
        promoted=True,
    )
    job_helper = _JobHelperFake()

    clip, imported, api = _multi_component_run(
        item_helper,
        _video_extras(1),
        job_helper=job_helper,
        in_flight_component_files={"VX-41-MAIN": ["VX-ANCHOR-JOB"]},
    )

    assert imported is True
    assert _containers(api) == []
    # It was WAITED for, and it is the job the row now records.
    assert "VX-ANCHOR-JOB" in job_helper.polled
    assert clip.job_id == "VX-ANCHOR-JOB"


def test_every_running_job_for_one_file_is_waited_for(migrated_db, monkeypatch):
    """`setdefault` kept the FIRST job per file and dropped the rest —
    and a file with two running imports is exactly the case that most
    needs both waited on, since duplicate imports are the hazard.

    Mutation killed: `in_flight.setdefault(file_id, job_id)`.
    """
    clip = Clip(umid="TWO-JOBS", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[
            _ListedJobFake("VX-FIRST", source=MEASURED_SOURCE_URI, target="VX-216268"),
            _ListedJobFake("VX-SECOND", source=MEASURED_SOURCE_URI, target="VX-216268"),
        ]
    )

    assert _in_flight(clip, job_helper, _r3d_extra(MEASURED_RELATIVE_PATH)) == {
        "VX-41-S002": ["VX-FIRST", "VX-SECOND"]
    }


def test_a_provider_path_containing_a_percent_sequence_is_not_unquoted(migrated_db):
    """The provider's `path` comes from `VSFile.getPath()` — a bare
    filesystem path that was never encoded — so unquoting it REWRITES a
    real filename containing a `%` sequence into something that is not
    the file.

    Mutation killed: one normaliser for both sides.
    """
    # A real file whose name contains `%20`, and a job importing it.
    provider_path = "2026/CARD/100%20BIS.R3D"
    clip = Clip(umid="PERCENT", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[
            _ListedJobFake(
                "VX-PCT",
                # The URI encodes the literal `%` as `%25`.
                source="file:///mnt/root/2026/CARD/100%2520BIS.R3D",
                target="VX-216268",
            )
        ]
    )

    assert _in_flight(
        clip, job_helper, _r3d_extra(provider_path, file_id="VX-41-PCT")
    ) == {"VX-41-PCT": ["VX-PCT"]}


def test_a_media_file_that_is_not_a_mapping_does_not_abort_the_resume(migrated_db):
    """`getClipAdditionalMediaFiles` is a PROVIDER hook, so its answer is
    whatever a provider returned — the base hook's `None`, a stray
    string, a list with a hole in it.

    Every other unknown in this lookup is caught and resolved to "not in
    flight" (the fail-safe: import it, worst case the loud 400). This one
    is built OUTSIDE the method's `try`, so an `AttributeError` here does
    not degrade — it escapes into the folder's except arm and costs the
    clip its ingest, for a media file the resume did not even need.

    Mutation killed: removing the `isinstance(media_file, Mapping)` guard
    from `_in_flight_component_files`'s candidate comprehension.
    """
    clip = Clip(umid="INFLIGHT-NON-MAPPING", item_id="VX-216268")
    job_helper = _JobListingHelperFake(
        jobs=[
            _ListedJobFake(
                "VX-696013",
                source=MEASURED_SOURCE_URI,
                target="VX-216268",
            )
        ]
    )
    # A provider that answered badly, beside one that answered well.
    media_files = ["/not/a/dict.R3D", None, 17] + _r3d_extra(MEASURED_RELATIVE_PATH)

    # No raise, and the well-formed file is still resolved.
    assert _in_flight(clip, job_helper, media_files) == {"VX-41-S002": ["VX-696013"]}


def test_a_single_component_clip_does_not_start_a_second_import(migrated_db):
    """The single-component path was outside BOTH resume guards: an
    interrupted import leaves the placeholder still EMPTY while its job
    runs, the empty placeholder is reused silently, and the
    `retry_incomplete` rung brings the next scan straight back to fire a
    second container import.

    Mutation killed: `_import_single_component` importing anyway when it
    was HANDED a running job — i.e. deleting the `if running:` arm.

    NOT the wiring. This test calls `_import_single_component` DIRECTLY
    with a hard-coded `in_flight_jobs`, so it never reaches the code in
    `import_file` that COMPUTES `in_flight` and passes it; deleting that
    argument at the call site left this test green. The wiring is pinned,
    end to end, by
    `test_a_one_file_clip_does_not_start_a_second_container_import`.
    """
    clip = Clip(umid="SINGLE-INFLIGHT", item_id="VX-100")
    ingest_helper = _NoJobIngestHelper({"jobId": "VX-NEW"})

    imported = clip._import_single_component(
        "VX-41-MAIN",
        [],
        None,
        ingest_helper,
        _JobHelperFake(),
        in_flight_jobs=["VX-RUNNING"],
    )

    assert imported is True
    # NOTHING was sent, and the row records the job that is really doing it.
    assert ingest_helper.calls == []
    assert clip.job_id == "VX-RUNNING"


@pytest.mark.parametrize(
    "build_job_helper, arm, wording",
    [
        (
            lambda: _JobHelperFake(missing=True),
            "answers None",
            "is not known to Vidispine (purged? getJob answered nothing)",
        ),
        (
            lambda: _JobHelperFake(error=NotFoundError("VX-RUNNING is gone")),
            "raises NotFoundError",
            "is not known to Vidispine (purged? VX-RUNNING is gone)",
        ),
        (
            lambda: _JobHelperFake(error=RuntimeError("vidispine is wedged")),
            "raises",
            "could not be read (vidispine is wedged)",
        ),
    ],
)
def test_the_recorded_job_id_survives_a_job_that_cannot_be_fetched(
    migrated_db, build_job_helper, arm, wording, caplog
):
    """The `job` SETTER does `self.job_id = job.getId()`.

    The GETTER catches `NotFoundError`; the setter never did — so a
    purged job aborted the clip's ingest with an `AttributeError` on the
    SUCCESS path of the resume, in the branch that had just correctly
    decided NOT to start a second import. The id is the only part the row
    keeps (`INGEST_STATE_FIELDS` persists `job_id`, not the object), and
    losing it leaves the row job-less — which the `retry_incomplete` rung
    reads as "never imported" and brings straight back to fire the second
    import this branch just refused to send.

    Each arm has its OWN warning wording, and the wording is pinned:
    the `except NotFoundError` arm was otherwise unpinnable, since
    deleting it lets the broad arm catch the same error with the suite
    still green. "Vidispine said the job is gone" and "the read failed"
    are different facts for the operator reading the log.

    Mutation killed: `self.job = job_helper.getJob(running[0])`; deleting
    the `except NotFoundError` arm.
    """
    clip = Clip(umid="RESUME-PURGED-JOB", item_id="VX-100")
    ingest_helper = _NoJobIngestHelper({"jobId": "VX-NEW"})

    with caplog.at_level(logging.WARNING):
        imported = clip._import_single_component(
            "VX-41-MAIN",
            [],
            None,
            ingest_helper,
            build_job_helper(),
            in_flight_jobs=["VX-RUNNING"],
        )

    assert imported is True
    # Nothing was sent, and the id is on the clip regardless.
    assert ingest_helper.calls == []
    assert clip.job_id == "VX-RUNNING"
    assert any(
        f"job VX-RUNNING {wording}" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_a_one_file_clip_does_not_start_a_second_container_import(
    migrated_db, es_fake, es_page, ingestable_provider, tmp_path, collection_seam
):
    """The single-component resume, through the REAL `import_file`.

    Run 1 starts the container import and dies before the row learns the
    job id, with the job still IN FLIGHT — so the placeholder is still a
    placeholder and still holds NO file. Run 2 finds exactly that state:
    nothing attached, so the classifier calls it a clean empty
    placeholder and hands it back, and the ONLY evidence that an import
    is already running is the item's job listing.

    Mutation killed: deleting `in_flight_jobs=in_flight.get(main_file_id)
    or (),` from the `_import_single_component` call in `import_file`.
    The helper then receives no running jobs and fires a SECOND container
    import while the first is still in flight. That second request is
    ACCEPTED — the budget is checked at request time against LANDED files
    only (measured 2026-09-02), and nothing has landed yet — so the
    container lands TWICE, silently; the 400 would only come once the
    first had landed. Caught by counting the `importFileToPlaceholder`
    calls: exactly one.

    The direct-call test above cannot see this: it supplies
    `in_flight_jobs` itself.
    """
    rel = "2026/AH_20260101_soloinflight"
    umid = f"{rel}/CLIPSOLO"
    sources = _ingestable_page(tmp_path, rel, ["CLIPSOLO"])

    # The ANCHOR's own job is held — the one import that can never be
    # duplicated safely, and the one the stub could not put in flight
    # until `hold_jobs` learned to hold containers.
    VidispineFake.hold_jobs(settle_after_polls=None, containers=True)

    es_fake.push(es_page(sources, total=1))
    # ONE job queued, on purpose: the conftest asserts the queue is
    # drained, so a second import would have to fall through to the
    # fake's default response — and there must not BE a second import.
    _queue_import_jobs("VX-JOB-SOLO1")
    # The process dies with the Vidispine write already committed and
    # nothing written back: item named, job id lost, status PLACEHOLDER.
    VidispineFake.fail_next("importFileToPlaceholder")
    first = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert (first["failed"], first["ingested"]) == (1, 0)
    stored = _fr36_cell(umid)
    item_id = stored.item_id
    # Still a placeholder, still EMPTY: the job has not landed the file.
    assert VidispineFake.placeholder_shape(item_id)["files"] == []
    # THE MEASURED VENDOR CONTRADICTION, in the stub. `VSJob.inProgress()`
    # answers False for `WAITING` (verified in the 6.2.1 bytecode), which
    # is an ordinary status on a busy Vidispine — so a merely QUEUED
    # import looks STOPPED to it and the resume would re-import the file.
    # `fail_component_job` is used here for its `inProgress()` half alone
    # (it never lands anything), to put the two readings in exactly that
    # disagreement: terminal to `inProgress()`, still coming to
    # `getStatus()`. The resume can only see this job by READING THE
    # STATUS, which is what makes the stub's status layer load-bearing
    # rather than merely present.
    VidispineFake.fail_component_job("VX-JOB-SOLO1")
    VidispineFake.set_job_status("VX-JOB-SOLO1", "WAITING")
    # The path Vidispine knows the anchor by — knowledge the fake has and
    # the plugin does not, which is why `getSourceFilePath()` is the
    # resume's only identifier for a running job.
    VidispineFake.set_file_path(stored.file_id, stored.path)

    es_fake.push(es_page(sources, total=1))
    second = _rescanned_folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    # NOTHING new was sent: exactly the one import run 1 started.
    assert VidispineFake.call_names().count("importFileToPlaceholder") == 1
    assert (second["ingested"], second["failed"]) == (1, 0)
    assert second["errors"] == []
    # ...and the row records the job that is really doing the import, so
    # the clip stops coming back through the retry rung.
    assert Clip.objects.get(pk=umid).job_id == "VX-JOB-SOLO1"


def test_a_resume_does_not_re_declare_the_component_budget(migrated_db):
    """Re-declaring REPLACES (measured, with a control arm) and frees no
    consumed slot, so a later run that computes a DIFFERENT verdict would
    overwrite the declaration the already-claimed slots were claimed
    against — the permanently unpromotable placeholder this story
    removes. The verdict really can move: it is derived from
    `metadatas["timecode"]`, which a re-extraction rewrites and an
    unreadable value sends to the `True` fallback.

    Nothing is lost by staying silent: the run that started the import
    declared BEFORE importing anything (Codemill's order, kept), so a
    shape holding files necessarily has a declaration.

    Mutation killed: declaring unconditionally.
    """
    _clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-S001", "VX-41-S002"],
        ),
        _video_extras(2),
        attached=frozenset({"VX-41-S001"}),
    )

    assert imported is True
    assert api.counts == []
    assert [call[0] for call in api.calls] == ["import", "import"]


def test_a_resume_whose_jobs_are_still_in_flight_re_declares(migrated_db):
    """A shape holding NO file has consumed nothing, so re-declaring is
    FREE — and a run whose only evidence of prior work is a running job
    must declare, because the previous run may never have.

    The measured rule (2026-09-01, control arm): a slot is consumed when
    a file LANDS; re-declaring REPLACES and frees nothing. An in-flight
    job has therefore claimed nothing, and there is no declaration any
    slot was consumed against: overwriting it costs nothing. Gating the
    declaration on the running job — the previous version of this pin —
    was a reasoning error with a hard consequence: the single-component
    path never declares, so a run that took it (no extras visible) and
    left its container job in flight made the next run, now seeing the
    extras, SKIP the declaration and import them into a shape whose
    budget was never set.

    Mutation killed: `prior_run_declared = bool(attached_file_ids) or
    bool(in_flight)`.
    """
    _clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-S001", "VX-41-S002"],
        ),
        _video_extras(2),
        # NOTHING attached yet — the only prior work is the running job.
        in_flight_component_files={"VX-41-S001": ["VX-RUNNING"]},
    )

    assert imported is True
    # Declared, exactly as a first run would have.
    assert api.counts == [{"container": 1, "video": 3, "audio": None}]
    # ...and the running component was still not re-imported.
    assert [entry["query"]["fileId"] for entry in api.imports] == [
        "VX-41-S002",
        "VX-41-MAIN",
    ]


def test_an_attached_anchor_is_refused_before_any_extra_is_imported(migrated_db):
    """The refusal is a REFUSAL, so it has to come first.

    Stated after the extras loop — where it was — the method declared the
    component count and sent every missing extra import into a shape it
    was about to call a dead end. Those components LAND, and a landed
    component consumes a slot on a placeholder nothing can ever promote:
    the verdict was right and the harm was already done. They are real
    production imports, not a wasted call.

    Mutation killed: moving the `main_file_id in attached_file_ids`
    refusal back below the extras loop.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-MAIN"],
        ),
        _video_extras(2),
        attached=frozenset({"VX-41-MAIN"}),
    )

    assert imported is False
    assert "already attached to shape" in clip.error
    # NOTHING was sent: not the two missing extras, not the anchor.
    assert api.calls == []
    assert api.counts == []


def test_the_budget_report_does_not_present_an_unknown_verdict_as_a_count(
    migrated_db,
):
    """The refusals above end with `_component_budget_report`, which
    counts through `_count_media_components` — and that count folds a
    ``None`` verdict into ``True``. Reported as a bare number, the fold
    became a fact the operator would act on (iteration 5).

    Mutation killed: dropping the `main_file_verdict_is_unknown` arm
    of `_component_budget_report`.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-MAIN"],
        ),
        _video_extras(2),
        main_file={
            "file_id": "VX-41-MAIN",
            "path": "main.R3D",
            "type": "video",
            MAIN_FILE_YIELDS_VIDEO: None,
        },
        attached=frozenset({"VX-41-MAIN"}),
    )

    assert imported is False
    assert "already attached to shape" in clip.error
    assert "container=1, video=3, audio=0" in clip.error
    assert "which the provider could NOT vouch for" in clip.error
    assert api.calls == []


def test_the_anchor_no_job_message_counts_the_extras_an_earlier_run_attached(
    migrated_db,
):
    """The anchor's response carries no job id on a RESUME: one extra
    was attached by the earlier run, one is covered by its still-running
    job. The message used to count only the second, so an operator read
    "1 extra component" on a two-extra clip (iteration 5).

    Mutation killed: not incrementing `already_attached`.
    """
    clip, imported, _api = _multi_component_run(
        _ItemHelperFake(
            {},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-S001", "VX-41-S002"],
        ),
        _video_extras(2),
        attached=frozenset({"VX-41-S001"}),
        in_flight_component_files={"VX-41-S002": ["VX-RUNNING"]},
    )

    assert imported is False
    assert (
        "1 extra component(s) are covered by import job(s) VX-RUNNING, " in clip.error
    )
    assert "1 were already attached by an earlier run" in clip.error


def test_one_component_job_is_counted_once_in_the_failure_message(migrated_db):
    """`_in_flight_component_files` lists every running import per FILE,
    so ONE job covering two files arrives twice, and the anchor's own job
    can already be in the list from the extras loop.

    The wait dedupes internally, so the over-count was invisible there —
    but the operator-facing message counts the list, and told them more
    component jobs had started than Vidispine ever ran.

    The message counts COMPONENTS (the extras a job covers) and lists
    the JOB ids apart: `len(component_job_ids)` counted jobs and called
    them components, and "did start" was wrong for a job an EARLIER run
    started.

    Mutations killed: dropping `component_job_ids =
    list(dict.fromkeys(component_job_ids))` (the id then appears twice);
    reporting `len(component_job_ids)` as the component count.
    """
    clip, imported, _api = _multi_component_run(
        # The ANCHOR's import answers without a job id.
        _ItemHelperFake(
            {},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-S001", "VX-41-S002"],
        ),
        _video_extras(2),
        # ONE job, reported for BOTH files.
        in_flight_component_files={
            "VX-41-S001": ["VX-DUP"],
            "VX-41-S002": ["VX-DUP"],
        },
    )

    assert imported is False
    # Two components, one job — named once — and the attached count is
    # reported beside it (iteration 5), zero here.
    assert "2 extra component(s) are covered by import job(s) VX-DUP, " in clip.error
    assert "0 were already attached by an earlier run" in clip.error
    assert clip.error.count("VX-DUP") == 1
    assert "did start" not in clip.error


def test_a_first_run_still_declares_the_budget(migrated_db):
    """The counterpart: nothing attached is not a resume, and the
    declaration is exactly where Codemill puts it."""
    _clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-E"}),
        _video_extras(2),
    )

    assert imported is True
    assert api.counts == [{"container": 1, "video": 3, "audio": None}]


# --- the measured rule, end to end: single → multi across two runs ---------


def test_a_run_that_first_saw_no_extras_declares_when_the_next_run_sees_them(
    migrated_db,
    es_fake,
    es_page,
    spanned_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """The transition the in-flight gate broke, through the REAL
    `import_file` against the stub's budget.

    Run 1 sees NO extras (the span files are not yet listed), takes the
    single-component path — which never declares a budget — starts the
    anchor's container import and dies with the job still in flight.
    Run 2 sees the extras: the shape is still an empty placeholder and
    the only prior work is the running anchor job. Gated on that job,
    the declaration was SKIPPED, and the extras were imported into a
    shape whose budget nobody had ever set. Gated on attached files
    alone, run 2 declares exactly what a first run would: a landed file
    is the only thing that consumes a slot, and re-declaring replaces
    (measured 2026-09-01, control arm), so declaring here costs nothing
    and is the only thing that gives the extras a slot to land in.

    What run 2 CANNOT do is promote the shape: run 1's anchor job
    evaluates the placeholder when it lands — one poll before the
    extras, on the fake exactly as on Vidispine — and nothing
    re-evaluates a placeholder (D7). Until 2026-09-02 (iteration 5) the
    clip was counted INGESTED here on the strength of the landed anchor,
    with a shape that never promotes. It is now the loud dead end: the
    declaration and the extras are exactly as before, the clip is
    counted FAILED with the reason, and the job is NOT recorded, so the
    clip is reported on every run until someone acts.

    Mutation killed: `prior_run_declared = bool(attached_file_ids) or
    bool(in_flight)` — `_declared_counts()` is then empty; dropping the
    promotion check after the wait — `(1, 0)` and `job_id ==
    "VX-JOB-G1"` again.
    """
    provider = spanned_provider(extras=0, deducible=False)
    rel = "2026/AH_20260101_grows"
    umid = f"{rel}/CLIPGROW"
    sources = _ingestable_page(tmp_path, rel, ["CLIPGROW"])
    VidispineFake.set_non_deducible("VX-41-CLIPGROW")
    # Every job — the anchor's container job included — settles only
    # once it has been polled twice: in flight across the two runs,
    # landed inside the second.
    VidispineFake.hold_jobs(settle_after_polls=2, containers=True)

    es_fake.push(es_page(sources, total=1))
    _queue_import_jobs("VX-JOB-G1")
    VidispineFake.fail_next("importFileToPlaceholder")
    first = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (first["failed"], first["ingested"]) == (1, 0)
    # The single path declared nothing, and the shape is still empty.
    assert _declared_counts() == []
    stored = _fr36_cell(umid)
    item_id = stored.item_id
    assert VidispineFake.placeholder_shape(item_id)["files"] == []
    VidispineFake.set_file_path(stored.file_id, stored.path)

    # Run 2: the span files are now visible.
    provider.extras = 2
    _queue_import_jobs("VX-JOB-G2", "VX-JOB-G3")
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(clip_module, "EXTRA_COMPONENT_POLL_SECONDS", 0.0)
    es_fake.push(es_page(sources, total=1))
    second = _rescanned_folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (second["ingested"], second["failed"]) == (0, 1), second["errors"]
    assert len(second["errors"]) == 1
    assert "VX-JOB-G1" in second["errors"][0]
    assert "still a placeholder" in second["errors"][0]
    assert "cannot be promoted by a re-run" in second["errors"][0]
    # Declared ONCE, by run 2, with what THIS clip needs: a binary anchor
    # and two video spans.
    assert _declared_counts() == [{"container": 1, "video": 2, "audio": None}]
    # The extras were sent; the anchor was NOT sent a second time.
    assert _imported_components() == ["video", "video"]
    assert VidispineFake.call_names().count("importFileToPlaceholder") == 1
    # ...and every file landed INTO the declared budget: the extras had
    # slots to land in. The shape is NOT promoted, truthfully: run 1's
    # anchor job was polled once by run 2's job listing, so it lands one
    # poll before the extras, evaluates the placeholder short, and
    # nothing re-evaluates it — the race a resumed anchor cannot close,
    # on the fake exactly as on Vidispine. Which is why the clip is
    # counted failed, not ingested.
    shape = VidispineFake.placeholder_shape(item_id)
    assert shape is not None
    assert sorted(entry["id"] for entry in shape["files"]) == [
        "VX-41-CLIPGROW",
        "VX-41-CLIPGROW-S001",
        "VX-41-CLIPGROW-S002",
    ]
    assert shape["budget"] == {"container": 1, "video": 2, "audio": 0}
    # No job recorded: `is_incomplete_import` would otherwise skip the
    # clip, and the next run must report it again (the anchor-attached
    # rung does, before any import).
    assert Clip.objects.get(pk=umid).job_id is None


# --- the fresh-import sites go through `_record_job` -----------------------


class _MissingSomeJobsHelper(_JobHelperFake):
    """``getJob`` answers None for the NAMED ids only.

    The multi-component wait treats an unreadable job as still running,
    so a helper answering None for every id could never reach the
    anchor's own import — the one site under test.
    """

    def __init__(self, *missing_ids):
        _JobHelperFake.__init__(self)
        self.missing_ids = set(missing_ids)

    def getJob(self, job_id):
        if job_id in self.missing_ids:
            self.polled.append(job_id)
            return None
        return _JobHelperFake.getJob(self, job_id)


def test_a_fresh_single_import_keeps_its_job_id_when_the_job_cannot_be_read(
    migrated_db,
):
    """The import has been SENT: an unreadable job must cost a warning,
    never the id — a job-less row here is a duplicate container import
    on the next run, through the `retry_incomplete` rung.

    Mutation killed: `self.job = job_helper.getJob(job_id)` at the fresh
    single-component site — `AttributeError` on `None.getId()`.
    """
    clip = Clip(umid="FRESH-SINGLE-UNREADABLE", item_id="VX-100")
    ingest_helper = _NoJobIngestHelper({"jobId": "VX-NEW"})

    imported = clip._import_single_component(
        "VX-41-MAIN", [], None, ingest_helper, _JobHelperFake(missing=True)
    )

    assert imported is True
    assert ingest_helper.calls == ["VX-100"]
    assert clip.job_id == "VX-NEW"
    assert clip.job is None


def test_a_fresh_anchor_import_keeps_its_job_id_when_the_job_cannot_be_read(
    migrated_db,
):
    """The same site on the multi-component path, after
    `doImportToPlaceholder` of the anchor.

    Mutation killed: `self.job = job_helper.getJob(job_id)` at the
    anchor's fresh import site.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-E"}),
        _video_extras(2),
        job_helper=_MissingSomeJobsHelper("VX-MAIN"),
    )

    assert imported is True
    assert [entry["component"] for entry in api.imports] == [
        "video",
        "video",
        "container",
    ]
    assert clip.job_id == "VX-MAIN"
    assert clip.job is None


# --- `_record_job`'s cache ---------------------------------------------------


def test_a_swallowed_job_read_failure_does_not_come_back_on_the_next_read(
    migrated_db,
):
    """`_record_job` swallows a `RuntimeError` from `getJob`; the `job`
    getter is `hasattr(self, "_job")`-gated and refetches through a
    fresh helper catching only `NotFoundError`. With the cache left
    UNSET on the failing arm, the next `clip.job` read — the serializer,
    on the REST path — refetched, and a helper still wedged re-raised
    where the record had just decided not to.

    Mutation killed: dropping `self._job = None` from `_record_job`'s
    failure arms — the getter then goes back to Vidispine.
    """
    clip = Clip(umid="RECORD-SWALLOWED", item_id="VX-100")

    clip._record_job("VX-WEDGED", _JobHelperFake(error=RuntimeError("wedged")))

    assert clip.job_id == "VX-WEDGED"
    assert clip.job is None
    # The getter did NOT go back to Vidispine for it.
    assert "getJob" not in VidispineFake.call_names()


def test_a_second_record_does_not_keep_the_first_calls_job_object(migrated_db):
    """A clip object CAN be imported twice in one process. With the
    cache written only on the success arm, a second `_record_job` whose
    fetch fails left the FIRST call's job object under the second
    call's id — `clip.job.getId()` disagreed with `clip.job_id`.

    Mutation killed: writing `self._job = None` only on the failure
    arms (or not at all) instead of before the fetch.
    """
    clip = Clip(umid="RECORD-TWICE", item_id="VX-100")
    clip._record_job("VX-FIRST", _JobHelperFake())
    assert clip.job.getId() == "VX-FIRST"

    clip._record_job("VX-SECOND", _JobHelperFake(missing=True))

    assert clip.job_id == "VX-SECOND"
    assert clip.job is None


class _NormalisingJobHelper:
    """A helper whose job objects answer a DIFFERENT id than they were
    asked for — what a helper that normalises ids would do."""

    def getJob(self, job_id):
        return _JobFake(job_id.lower())


def test_the_recorded_job_id_is_the_one_sent_not_the_one_read_back(migrated_db):
    """The `job` SETTER does `self.job_id = job.getId()`; `_record_job`
    assigns the cache directly and keeps the id AS SENT.

    Mutation killed: `self.job = job` on the success arm.
    """
    clip = Clip(umid="RECORD-AS-SENT", item_id="VX-100")

    clip._record_job("VX-UPPER", _NormalisingJobHelper())

    assert clip.job_id == "VX-UPPER"
    assert clip.job.getId() == "vx-upper"


# --- the stub's contract -----------------------------------------------------


def test_hold_jobs_arguments_are_sticky_within_a_test():
    """`hold_jobs(settle_after_polls=None)` then `hold_jobs(containers=True)`
    must keep "never settles"; the reverse order must keep the container
    hold. Both were plain assignments, so the second call silently undid
    the first — and a test holding the anchor "for ever" had it settle
    after one poll.

    Mutation killed: `cls.settle_after_polls = settle_after_polls` with
    a default of 1; `cls.hold_container_jobs = containers`.
    """
    VidispineFake.hold_jobs(settle_after_polls=None)
    VidispineFake.hold_jobs(containers=True)
    assert VidispineFake.settle_after_polls is None
    assert VidispineFake.hold_container_jobs is True

    VidispineFake.hold_jobs(settle_after_polls=3)
    VidispineFake.hold_jobs()
    assert VidispineFake.settle_after_polls == 3
    assert VidispineFake.hold_container_jobs is True


def test_the_stub_refuses_a_duplicate_import_into_a_promoted_shape():
    """The fake's budget check only looked at a still-PLACEHOLDER shape,
    so a landing that promoted the shape made it blind to a re-import of
    the same file — and the request the plugin's resume guards exist
    never to send was silently accepted.

    What Vidispine answers to THAT request is not measured (2026-09-01
    measured a second component of the same TYPE after the first landed
    — the 400 — not the same FILE again), so the fake trips a tripwire
    that says so, rather than quoting a 400 it has no measurement for.
    A subclass of ``ComponentBudgetExceeded``, so "refused" still holds.
    ``removeItemShape`` is record-only in the fake: a test modelling a
    removed shape edits ``item_shapes``, or it trips this too.

    Mutation killed: dropping the attached-file scan from
    `refuse_if_budget_full`; raising the measured 400 text for it.
    """
    from tests.portal_stub import (
        DuplicateImportOfAnAttachedFile,
        IngestHelperFake,
        ItemAPIFake as _StubItemAPI,
    )

    VidispineFake.set_item("VX-900")
    _StubItemAPI().createPlaceholderShape("VX-900")
    IngestHelperFake().importFileToPlaceholder("VX-900", file_id="VX-41-DUP")
    # Landed and PROMOTED: no placeholder left to check a budget on.
    assert VidispineFake.placeholder_shape("VX-900") is None

    with pytest.raises(DuplicateImportOfAnAttachedFile, match="UNMEASURED") as excinfo:
        IngestHelperFake().importFileToPlaceholder("VX-900", file_id="VX-41-DUP")
    assert isinstance(excinfo.value, ComponentBudgetExceeded)
    assert "VIDEO_COMPONENT" not in str(excinfo.value)


def test_the_stub_refuses_an_import_with_no_file_id():
    """`providers/file.py` builds `{"file_id": None}` for a clip with no
    file, and a fake that accepted `fileId=None` certified a request
    Vidispine would 400 — or worse, import something else on.

    An EMPTY id is as file-less as ``None`` — a row whose ``file_id``
    was blanked rather than nulled — and Vidispine 400s it just the
    same, so the guard is ``not``, not ``is None`` (iteration 5).

    Mutation killed: dropping either `ImportWithoutAFileId` raise;
    `is None` in either guard.
    """
    from tests.portal_stub import (
        ImportWithoutAFileId,
        IngestHelperFake,
        ItemAPIFake as _StubItemAPI,
    )

    VidispineFake.set_item("VX-901")
    _StubItemAPI().createPlaceholderShape("VX-901")

    for empty in (None, ""):
        with pytest.raises(ImportWithoutAFileId):
            IngestHelperFake().importFileToPlaceholder("VX-901", file_id=empty)
        with pytest.raises(ImportWithoutAFileId):
            _StubItemAPI().doImportToPlaceholder("VX-901", query={"fileId": empty})
    # Nothing was registered for any of them.
    assert VidispineFake.component_jobs == {}


# --- the single path refuses a file-less anchor ------------------------------


def test_a_single_component_import_without_a_file_id_is_refused_before_sending(
    migrated_db,
):
    """`_import_multi_component` has had this refusal for its anchor;
    the single path sent `file_id=None` straight to Vidispine.

    Mutation killed: dropping the `if not main_file_id:` guard — the
    helper is then called with None.
    """
    clip = Clip(umid="SINGLE-NO-FILE-ID", item_id="VX-100")
    ingest_helper = _NoJobIngestHelper({"jobId": "VX-NEW"})

    imported = clip._import_single_component(
        None, [], None, ingest_helper, _JobHelperFake()
    )

    assert imported is False
    assert ingest_helper.calls == []
    assert "no Vidispine file id" in clip.error
    assert clip.job_id is None


# --- the in-flight candidates are the IMPORTABLE media files ----------------


def test_a_non_importable_extra_is_never_read_as_in_flight(
    migrated_db,
    es_fake,
    es_page,
    ingestable_provider,
    tmp_path,
    collection_seam,
    monkeypatch,
):
    """`import_file` handed the RAW `extra_files` to
    `_in_flight_component_files`, while the import loop and
    `_expected_file_ids` both go through `importable_extras`. An
    `xdcam`-style `metadatas` extra (a real `file_id`, never imported)
    whose basename matched a running job was then reported as an
    in-flight COMPONENT of this clip — a file no import will ever send
    and no wait can ever see land.

    Mutation killed: `self._in_flight_component_files(_ijh, extra_files +
    [main_file])` — the metadatas id then appears in the mapping.
    """
    rel = "2026/AH_20260101_sidecar"
    umid = f"{rel}/CLIPMETA"
    sources = _ingestable_page(tmp_path, rel, ["CLIPMETA"])
    VidispineFake.hold_jobs(settle_after_polls=None, containers=True)

    es_fake.push(es_page(sources, total=1))
    _queue_import_jobs("VX-JOB-META1")
    VidispineFake.fail_next("importFileToPlaceholder")
    first = _folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])
    assert (first["failed"], first["ingested"]) == (1, 0)
    stored = _fr36_cell(umid)

    # Run 2: the provider now lists a sidecar, and the running job's
    # source path is the sidecar's — the one candidate that must never
    # match.
    sidecar_path = f"{rel}/CLIPMETA.XML"
    VidispineFake.set_file_path(stored.file_id, sidecar_path)
    monkeypatch.setattr(
        ingestable_provider,
        "getClipAdditionalMediaFiles",
        lambda clip: [
            {"type": "metadatas", "file_id": "VX-41-META", "path": sidecar_path}
        ],
    )
    recorded = {}

    def record_multi(self, main_file, extra_files, shape_id, *args, **kwargs):
        recorded["in_flight"] = kwargs["in_flight_component_files"]
        return True

    monkeypatch.setattr(Clip, "_import_multi_component", record_multi)
    es_fake.push(es_page(sources, total=1))
    _rescanned_folder(tmp_path, rel).ingest(providers=[INGESTABLE_NAME])

    assert "in_flight" in recorded
    assert "VX-41-META" not in recorded["in_flight"]
    assert recorded["in_flight"] == {}


# --- the hoisted refusals, on the direct-call path ---------------------------


def test_the_anchor_refusal_names_the_shape_it_can_and_reports_the_budget(
    migrated_db,
):
    """On the direct-call path nothing guarantees a `shape_id`; the
    message must not say "shape None". And the budget verdict — the one
    grep-able line naming the declared set — is appended, because this
    refusal now precedes the line that used to log it.

    Mutations killed: `f"shape {shape_id}"` unguarded; dropping the
    `_component_budget_report` suffix.
    """
    clip = Clip(umid="ANCHOR-REFUSED", item_id="VX-100")
    item_helper = _ItemHelperFake({"jobId": "VX-MAIN"})

    imported = clip._import_multi_component(
        {"file_id": "VX-41-MAIN", "path": "main.mxf", "type": "video"},
        _video_extras(2),
        None,
        ["Admin"],
        None,
        None,
        item_helper,
        _JobHelperFake(),
        attached_file_ids=frozenset({"VX-41-MAIN"}),
    )

    assert imported is False
    assert "attached to its placeholder shape" in clip.error
    assert "None" not in clip.error
    assert "container=1, video=3, audio=0" in clip.error
    assert item_helper.itemapi.calls == []


def test_a_direct_call_with_a_foreign_file_on_the_shape_is_refused(migrated_db):
    """The classifier's foreign-file rung, re-stated where the import
    happens: a direct caller handing in a shape that holds another
    clip's file must be refused BEFORE anything is imported into it.

    Mutation killed: dropping the `foreign` rung from
    `_import_multi_component` — the extras are then imported into the
    other clip's shape.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-E"}),
        _video_extras(2),
        attached=frozenset({"VX-41-S001", "VX-99-OTHER"}),
    )

    assert imported is False
    assert "1 file(s) that are not this clip's (VX-99-OTHER)" in clip.error
    assert "container=1, video=3, audio=0" in clip.error
    assert api.calls == []


# --- two running imports of the anchor -------------------------------------


def test_two_running_anchor_jobs_are_both_waited_for_and_the_first_is_recorded(
    migrated_db, caplog
):
    """Two running imports of the anchor is already the duplicate this
    run refuses to add to. Every one of them is waited for; the row can
    record only one, and the log says which.

    Mutation killed: dropping the `len(anchor_jobs) > 1` warning;
    recording `anchor_jobs[-1]`.
    """
    job_helper = _JobHelperFake()
    with caplog.at_level(logging.WARNING):
        clip, imported, api = _multi_component_run(
            _ItemHelperFake(
                {"jobId": "VX-MAIN"},
                component_response={"jobId": "VX-E"},
                landed=["VX-41-S001", "VX-41-S002", "VX-41-MAIN"],
                promoted=True,
            ),
            _video_extras(2),
            job_helper=job_helper,
            in_flight_component_files={"VX-41-MAIN": ["VX-A1", "VX-A2"]},
        )

    assert imported is True
    # Nothing sent for the anchor: the two running jobs did it.
    assert [entry["component"] for entry in api.imports] == ["video", "video"]
    assert {"VX-A1", "VX-A2"} <= set(job_helper.polled)
    assert clip.job_id == "VX-A1"
    assert any(
        "2 import jobs are running for the anchor VX-41-MAIN (VX-A1, VX-A2)"
        in record.getMessage()
        and "VX-A1 is the one recorded on the clip" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


# --- landed is not promoted (D7, amended 2026-09-02, iteration 5) ----------


def test_an_anchor_job_that_landed_without_promoting_is_a_loud_dead_end(
    migrated_db,
):
    """The wait says the anchor LANDED. Only the anchor's job says whether
    the placeholder PROMOTED, and that job has already run: a shape that
    is still a placeholder after it is one nothing will evaluate again.
    Counting the clip ingested here — as this branch did until
    2026-09-02 — reported a success that never comes.

    The fake's default is a shape that stays a placeholder on both
    states, which is precisely this case. NOT recorded on the row: the
    job id is what keeps `is_incomplete_import` from revisiting the
    clip, and this one must be reported every run.

    Mutation killed: dropping the promotion check (imported is True,
    `job_id` recorded); recording the job before failing.
    """
    job_helper = _JobHelperFake()
    clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-S001", "VX-41-MAIN"],
        ),
        _video_extras(1),
        job_helper=job_helper,
        in_flight_component_files={"VX-41-MAIN": ["VX-ANCHOR-JOB"]},
    )

    assert imported is False
    assert _containers(api) == []
    assert "VX-ANCHOR-JOB" in job_helper.polled
    assert "still a placeholder" in clip.error
    assert "cannot be promoted by a re-run" in clip.error
    assert clip.job_id is None


def test_an_unreadable_promotion_state_fails_the_clip_for_the_next_run(
    migrated_db,
):
    """The shape query answers nothing on EITHER state after the wait.
    "Could not tell" is not "promoted": the clip is failed with a reason
    that says the next run re-examines it, and the job is not recorded —
    recording it would hide the item from that run.

    The wait itself is satisfied through the fake's ``landed`` read
    first; only the promotion read is blind, which is what the wrapper
    below arranges.

    Mutation killed: `return False if promoted else None` answering
    ``False`` on an empty un-filtered read; treating ``None`` as
    promoted.
    """

    class _BlindAfterTheWait(_ItemHelperFake):
        def getItemShapesFromNames(self, item_id, names, placeholder=False):
            if self.shape_reads >= 1:
                self.shape_reads += 1
                return []
            return super().getItemShapesFromNames(item_id, names, placeholder)

    clip, imported, api = _multi_component_run(
        _BlindAfterTheWait(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-S001", "VX-41-MAIN"],
        ),
        _video_extras(1),
        in_flight_component_files={"VX-41-MAIN": ["VX-ANCHOR-JOB"]},
    )

    assert imported is False
    assert _containers(api) == []
    assert "cannot be read" in clip.error
    assert "the next run re-examines this item" in clip.error
    assert clip.job_id is None


# --- an un-evidenced verdict is refused, not guessed (ruled 2026-09-02, D6) --


def test_an_unknown_anchor_verdict_refuses_the_multi_component_import(migrated_db):
    """The provider said ``None`` — "could not tell". A budget built on
    it is a guess in one of two directions, so the import declares
    nothing and imports nothing, and says why on ``Clip.error``.

    Mutation killed: dropping the `main_file_verdict_is_unknown` rung —
    the reader folds ``None`` into ``True`` and the run declares
    ``video=3``, the pre-2026-09-02 silent over-declaration; placing it
    after the extras loop — the extras land in a shape whose budget
    nobody can vouch for.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake({"jobId": "VX-MAIN"}, component_response={"jobId": "VX-E"}),
        _video_extras(2),
        main_file={
            "file_id": "VX-41-MAIN",
            "path": "main.R3D",
            "type": "video",
            MAIN_FILE_YIELDS_VIDEO: None,
        },
    )

    assert imported is False
    assert "could not tell whether the anchor main.R3D" in clip.error
    assert "see the provider's warning" in clip.error
    assert "nothing is imported" in clip.error
    # NOTHING sent: no declaration, no extra, no anchor.
    assert api.calls == []
    assert api.counts == []


def test_an_unknown_verdict_does_not_refuse_a_resume_that_declares_nothing(
    migrated_db,
):
    """A shape that already holds a file keeps the declaration the first
    run made; the verdict is not consulted on that resume. Refusing it
    stranded an import whose budget WAS evidenced — by the run that
    could read the metadata — on the strength of a re-extraction that
    no longer can (iteration 5).

    Mutation killed: `not attached_file_ids and` dropped from the rung.
    """
    clip, imported, api = _multi_component_run(
        _ItemHelperFake(
            {"jobId": "VX-MAIN"},
            component_response={"jobId": "VX-E"},
            landed=["VX-41-S001", "VX-41-S002"],
        ),
        _video_extras(2),
        main_file={
            "file_id": "VX-41-MAIN",
            "path": "main.R3D",
            "type": "video",
            MAIN_FILE_YIELDS_VIDEO: None,
        },
        attached=frozenset({"VX-41-S001"}),
    )

    assert imported is True, clip.error
    # Nothing declared (resume keeps the first run's budget), the missing
    # extra and the anchor sent.
    assert api.counts == []
    assert [call[2] for call in api.calls] == ["video", "container"]


def test_an_unknown_verdict_fails_the_clip_in_the_report_end_to_end(
    migrated_db, es_fake, es_page, spanned_provider, tmp_path, collection_seam
):
    """Through the REAL ``import_file`` and ``Folder.ingest``: the reason
    reaches ``response["errors"]``, the clip is counted failed, and the
    item is a CLEAN placeholder — nothing attached, nothing declared —
    so a corrected timecode on the next run is a fresh import, not a
    resume.

    Mutation killed: the refusal answering ``True`` instead of
    ``False`` — the clip is then counted ingested with no anchor.
    """
    spanned_provider(extras=2, deducible=None)
    rel = "2026/AH_20260101_unknownanchor"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPUNK"]), total=1))

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (response["ingested"], response["failed"]) == (0, 1)
    assert len(response["errors"]) == 1
    assert "could not tell whether the anchor" in response["errors"][0]
    assert _declared_counts() == []
    assert _imported_components() == []
    item_id = Clip.objects.get(pk=f"{rel}/CLIPUNK").item_id
    shape = VidispineFake.placeholder_shape(item_id)
    assert shape is not None
    assert not shape.get("landed"), shape


def test_an_unknown_verdict_does_not_touch_the_single_component_path(
    migrated_db, es_fake, es_page, spanned_provider, tmp_path, collection_seam
):
    """A one-file clip declares no budget, so the verdict is moot there:
    the import goes through exactly as before.

    Mutation killed: reading the verdict in ``import_file`` rather than
    in ``_import_multi_component`` — a REST-posted RED clip with no
    extras and no ``timecode`` is then refused for a budget it never
    declares.
    """
    spanned_provider(extras=0, deducible=None)
    rel = "2026/AH_20260101_soloknown"
    es_fake.push(es_page(_ingestable_page(tmp_path, rel, ["CLIPONE"]), total=1))
    _queue_import_jobs("VX-JOB-ONE")

    response = _folder(tmp_path, rel).ingest(providers=[SPANNED_NAME])

    assert (response["ingested"], response["failed"]) == (1, 0)
    assert response["errors"] == []
    assert _declared_counts() == []
    assert VidispineFake.call_names().count("importFileToPlaceholder") == 1
    assert "doImportToPlaceholder" not in VidispineFake.call_names()

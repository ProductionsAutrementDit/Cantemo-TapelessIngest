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

import os

import pytest
from django.db import DatabaseError, connection
from django.test.utils import CaptureQueriesContext

from VidiRest.objects.storage import VSFile

from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder, persist_scan_results
from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider
from portal.plugins.TapelessIngest.scan.persistence import build_persistence_plan

from tests.portal_stub import InjectedVidispineFault, VidispineFake

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
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.imports = []

    def updatePlaceholderComponentCount(self, item_id, shape_id, **kwargs):
        self.calls.append(("count", item_id, shape_id))

    def doImportToPlaceholder(self, item_id=None, **kwargs):
        self.calls.append(("import", item_id, kwargs.get("component", "container")))
        self.imports.append(
            {
                "component": kwargs.get("component", "container"),
                "query": dict(kwargs.get("query") or {}),
                "kwargs": kwargs,
            }
        )
        return self.response


class _ItemHelperFake:
    def __init__(self, response):
        self.itemapi = _ItemAPIFake(response)


class _JobFake:
    def __init__(self, job_id):
        self._job_id = job_id

    def getId(self):
        return self._job_id


class _JobHelperFake:
    def getJob(self, job_id):
        return _JobFake(job_id)


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
    item_helper = _ItemHelperFake({})

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

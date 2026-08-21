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

Two seams are replaced with local doubles: ``Folder.getCollection`` and
``Clip.import_file``. Both are PLUGIN code (not Portal — AD-11 is
untouched), and both are unreachable off-server because the Portal
helpers they construct take prod constructor kwargs the stubs
deliberately refuse. Replacing them is what makes the ingest-side
decisions observable; everything they guard is pinned separately from the
component methods below, whose helpers are injected as arguments.
"""

import os

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from VidiRest.objects.storage import VSFile

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder

STORAGE_ID = "VX-41"
LEGACY_STORAGE = "VX-LEGACY"
MISLABELLED_NAME = "fakemislabelled"


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


def _write_clips(tmp_path, rel, names, hashes=None):
    (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    sources = []
    for name in names:
        (tmp_path / rel / f"{name}.fake").write_bytes(b"clip data")
        sources.append(
            _source(
                f"{rel}/{name}.fake",
                f"VX-41-{name}",
                file_hash=(hashes or {}).get(name),
            )
        )
    return sources


def _folder_writes(captured):
    return [
        query["sql"]
        for query in captured
        if "TapelessIngest_folder" in query["sql"]
        and query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE"))
    ]


@pytest.fixture
def ingest_seams(monkeypatch):
    """Count the two plugin seams ``Folder.ingest`` drives (see module docstring).

    ``import_file`` writes the ingest state a real import would, so
    ``Clip.ingest``'s targeted UPDATE is exercised for real.
    """
    calls = {"collection": [], "import_file": []}
    result = {"skipped": False, "failed": False, "replaced": False, "ingested": True}

    def fake_get_collection(self, user, dryrun=False):
        calls["collection"].append(self.path)
        return "VX-COLLECTION"

    def fake_import_file(
        self, collection_id=None, user=None, replace=False, legacy_storages=None
    ):
        calls["import_file"].append(self.umid)
        if result["ingested"]:
            self.item_id = self.item_id or f"VX-ITEM-{len(calls['import_file'])}"
            self.job_id = f"VX-JOB-{len(calls['import_file'])}"
            self.status = Clip.STATUS_PLACHOLDER_CREATED
        return dict(result)

    monkeypatch.setattr(Folder, "getCollection", fake_get_collection)
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

    def updatePlaceholderComponentCount(self, item_id, shape_id, **kwargs):
        self.calls.append(("count", item_id, shape_id))

    def doImportToPlaceholder(self, item_id=None, **kwargs):
        self.calls.append(("import", item_id, kwargs.get("component", "container")))
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
    """The invariant guard: 2.4 persistence should have written it already."""
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

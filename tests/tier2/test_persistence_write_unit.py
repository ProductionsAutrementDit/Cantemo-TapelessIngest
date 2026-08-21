"""Tier 2 (story 2.4): the batched atomic write unit inside Folder.scan.

Sibling to test_scan_counters.py (whose pin #4 block this story updates)
and to test_scan_extraction_integration.py. Covered here, end to end
through a real ``scan()``/``ingest()``:

- new clips and their metadatas are materialized in one transaction,
  with a constant number of statements per folder;
- an EXISTING clip's location and ingest state are never rewritten by a
  scan, however far the file moved (the story's central safety property);
- the declared behavioral deltas of the FR-4 waiver: a re-scan of a
  never-ingested folder reports ``created=0``, and a dry run writes
  nothing at all;
- the Folder row is written at most once per invocation, never for a
  zero-hit folder, never for ``count_only``;
- re-running a scan produces no duplicate rows and no constraint errors;
- ``Clip.persist_metadatas()`` — the model-level batched writer the
  per-clip ingest endpoint calls — upserts and prunes like the deleted
  ``save()`` fan-out did;
- the unique constraints the upserts rely on really exist on the
  substrate (no new migration in this story: the prod audit found both
  already enforced), and both are usable as ``ON CONFLICT`` targets.

ORM needed throughout. Files live under a real tmp root because scan's
filesystem verification is real-FS and deliberate.
"""

import os

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider
from portal.plugins.TapelessIngest.scan.context import (
    RunOptions,
    ScanContext,
    StorageInfo,
)

STORAGE_ID = "VX-41"
FIXED_NAME = "fakefixedumid"
FIXED_UMID = "FIXED-UMID-0001"
BAD_SIDECAR_NAME = "fakebadsidecar"

XDCAM_SIDECAR = b"""<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta>
  <TargetMaterial umidRef="XDCAM-UMID-C0001"/>
  <Duration value="250"/>
  <CreationDate value="2026-01-01T10:00:00+01:00"/>
  <Device manufacturer="Sony" modelName="PXW-Z750" serialNo="12345"/>
</NonRealTimeMeta>
"""


def _source(path, file_id):
    return {
        "path": path,
        "hash": f"hash-{file_id}",
        "storage": STORAGE_ID,
        "id": file_id,
        "size": 1024,
    }


def _folder(tmp_path, rel_path):
    folder = Folder(storage_id=STORAGE_ID, path=rel_path)
    # Presetting _root_path bypasses StorageHelper/cache entirely.
    folder._root_path = str(tmp_path)
    return folder


class FixedUmidProvider:
    """Every file it claims is the SAME clip — the duplicate-umid case."""

    machine_name = FIXED_NAME

    def getExtensions(self):
        return [".fixed"]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = FIXED_UMID
        metadatas["clipname"] = media_file.getFileName()
        return metadatas


class BadSidecarProvider(BaseProvider):
    """Declares a ``clip_xml_file`` that cannot be parsed (or is absent)."""

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "Fake Bad Sidecar Provider"
        self.machine_name = BAD_SIDECAR_NAME

    def getExtensions(self):
        return [".bad"]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        # Relative, like xdcam's "./Clip/{clip}M01.XML" — resolved against
        # the clip's own directory by Clip._sidecar_absolute_path.
        metadatas["clip_xml_file"] = "./Clip/BROKEN.XML"
        return metadatas


@pytest.fixture
def fixed_umid_provider():
    provider = FixedUmidProvider()
    Clip._PROVIDER_CACHE[FIXED_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(FIXED_NAME, None)


@pytest.fixture
def bad_sidecar_provider():
    provider = BadSidecarProvider()
    Clip._PROVIDER_CACHE[BAD_SIDECAR_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(BAD_SIDECAR_NAME, None)


def _rescanned_folder(tmp_path, rel):
    """The production re-scan path: every caller fetches via get_or_new.

    A second scan must reuse the row the first one wrote — building a
    fresh unsaved Folder for a path that already exists has always been
    an IntegrityError, constraint-enforced since long before 2.4.
    """
    folder, is_new = Folder.get_or_new(storage_id=STORAGE_ID, path=rel)
    assert is_new is False
    folder._root_path = str(tmp_path)
    return folder


def _write_clips(tmp_path, rel, names, suffix=".fake"):
    (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    sources = []
    for name in names:
        (tmp_path / rel / f"{name}{suffix}").write_bytes(b"clip data")
        sources.append(_source(f"{rel}/{name}{suffix}", f"VX-41-{name}"))
    return sources


def _folder_writes(captured):
    return [
        query["sql"]
        for query in captured
        if "TapelessIngest_folder" in query["sql"]
        and query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE"))
    ]


# --------------------------------------------------------------------------
# The happy path: rows materialize, once, in one transaction
# --------------------------------------------------------------------------


def test_scan_persists_clips_and_metadatas(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    rel = "2026/AH_20260101_persist"
    sources = _write_clips(tmp_path, rel, ["CLIPA", "CLIPB"])
    es_fake.push(es_page(sources, total=2))

    response = _folder(tmp_path, rel).scan(providers=[fake_provider.machine_name])

    assert response["errors"] == []
    assert response["created"] == 2
    assert set(Clip.objects.values_list("pk", flat=True)) == {
        f"{rel}/CLIPA",
        f"{rel}/CLIPB",
    }
    # Both clips' metadata key-sets are the provider's full contribution.
    rows = set(ClipMetadata.objects.values_list("clip_id", "name", "value"))
    assert (f"{rel}/CLIPA", "provider", fake_provider.machine_name) in rows
    assert (f"{rel}/CLIPA", "umid", f"{rel}/CLIPA") in rows
    assert (f"{rel}/CLIPB", "umid", f"{rel}/CLIPB") in rows
    assert ClipMetadata.objects.count() == 4
    # The new rows carry their full insert-path column set.
    clip = Clip.objects.get(pk=f"{rel}/CLIPA")
    assert clip.path == rel
    assert clip.storage_id == STORAGE_ID
    assert clip.provider_name == fake_provider.machine_name


def test_constant_query_count_per_folder(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """O(1) statements per folder — not per file, not per metadata key."""
    providers = [fake_provider.machine_name]

    small_rel = "2026/AH_20260101_qsmall"
    es_fake.push(es_page(_write_clips(tmp_path, small_rel, ["A1", "A2"]), total=2))
    with CaptureQueriesContext(connection) as small:
        assert _folder(tmp_path, small_rel).scan(providers=providers)["errors"] == []

    big_rel = "2026/AH_20260101_qbig"
    big_names = ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8"]
    es_fake.push(es_page(_write_clips(tmp_path, big_rel, big_names), total=8))
    with CaptureQueriesContext(connection) as big:
        assert _folder(tmp_path, big_rel).scan(providers=providers)["errors"] == []

    assert Clip.objects.count() == 10
    assert ClipMetadata.objects.count() == 20
    assert len(big.captured_queries) == len(
        small.captured_queries
    ), "the write unit's statement count grew with the file count:\n" + "\n".join(
        query["sql"][:120] for query in big.captured_queries
    )


def test_rerun_writes_no_duplicate_rows(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """FR-10: a second scan of the same tree is a clean upsert."""
    rel = "2026/AH_20260101_rerun"
    sources = _write_clips(tmp_path, rel, ["CLIP1", "CLIP2"])
    providers = [fake_provider.machine_name]

    es_fake.push(es_page(sources, total=2))
    _folder(tmp_path, rel).scan(providers=providers)
    first = (Clip.objects.count(), ClipMetadata.objects.count(), Folder.objects.count())

    es_fake.push(es_page(sources, total=2))
    second_response = _rescanned_folder(tmp_path, rel).scan(providers=providers)

    assert second_response["errors"] == []
    assert (
        (
            Clip.objects.count(),
            ClipMetadata.objects.count(),
            Folder.objects.count(),
        )
        == first
        == (2, 4, 1)
    )


def test_duplicate_umid_in_folder_upserts_one_row(
    migrated_db, es_fake, es_page, fixed_umid_provider, tmp_path
):
    """Two files, one umid: one row, no ON CONFLICT double-hit crash."""
    rel = "2026/AH_20260101_dupumid"
    sources = _write_clips(tmp_path, rel, ["FIRST", "LAST"], suffix=".fixed")
    es_fake.push(es_page(sources, total=2))

    response = _folder(tmp_path, rel).scan(providers=[FIXED_NAME])

    assert response["errors"] == []
    # The response still reports both files, exactly as before 2.4...
    assert len(response["clips"]) == 2
    # ...while the write plan collapsed them: last occurrence wins.
    assert Clip.objects.count() == 1
    assert (
        ClipMetadata.objects.get(clip_id=FIXED_UMID, name="clipname").value
        == "LAST.fixed"
    )


# --------------------------------------------------------------------------
# Existing clips: metadata yes, location and ingest state never
# --------------------------------------------------------------------------


def test_moved_file_keeps_location_columns(
    migrated_db, es_fake, es_page, fixed_umid_provider, tmp_path
):
    """A moved / re-carded file must not rewrite the clip's location.

    Same umid, brand-new path and storage in the index hit. Pre-2.4 the
    scan never wrote those columns for an existing clip (it never called
    ``clip.save()``); the batched upsert must not start.
    """
    Clip.objects.create(
        umid=FIXED_UMID,
        path="2019/ORIGINAL_CARD",
        storage_id="VX-LEGACY",
        folder_path="/mnt/legacy/2019/ORIGINAL_CARD",
        reference_file="LEGACY-FILE-ID",
        provider_name="legacyprovider",
        item_id="VX-777",
        job_id="VX-888",
        status=Clip.STATUS_IMPORTED,
    )

    rel = "2026/AH_20260101_moved"
    sources = _write_clips(tmp_path, rel, ["MOVED"], suffix=".fixed")
    es_fake.push(es_page(sources, total=1))

    response = _folder(tmp_path, rel).scan(providers=[FIXED_NAME])

    assert response["errors"] == []
    # Known umid: not a creation.
    assert response["created"] == 0
    reloaded = Clip.objects.get(pk=FIXED_UMID)
    assert reloaded.path == "2019/ORIGINAL_CARD"
    assert reloaded.storage_id == "VX-LEGACY"
    assert reloaded.folder_path == "/mnt/legacy/2019/ORIGINAL_CARD"
    # Ingest state is just as untouchable...
    assert reloaded.item_id == "VX-777"
    assert reloaded.job_id == "VX-888"
    assert reloaded.status == Clip.STATUS_IMPORTED
    assert reloaded.reference_file == "LEGACY-FILE-ID"
    assert reloaded.provider_name == "legacyprovider"
    # ...but the metadata rows ARE refreshed, exactly as the pre-2.4
    # `metadatas` setter did for an already-saved clip.
    assert ClipMetadata.objects.get(clip_id=FIXED_UMID, name="clipname").value == (
        "MOVED.fixed"
    )
    assert Clip.objects.count() == 1


def test_existing_clip_stale_metadata_keys_are_deleted(
    migrated_db, es_fake, es_page, fixed_umid_provider, tmp_path
):
    clip = Clip.objects.create(
        umid=FIXED_UMID, path="2019/OLD", storage_id=STORAGE_ID, reference_file="OLD"
    )
    ClipMetadata.objects.create(clip=clip, name="obsolete", value="drop me")
    ClipMetadata.objects.create(clip=clip, name="clipname", value="stale value")

    rel = "2026/AH_20260101_stale"
    es_fake.push(
        es_page(_write_clips(tmp_path, rel, ["FRESH"], suffix=".fixed"), total=1)
    )

    _folder(tmp_path, rel).scan(providers=[FIXED_NAME])

    names = set(ClipMetadata.objects.values_list("name", flat=True))
    assert names == {"provider", "umid", "clipname"}
    assert ClipMetadata.objects.get(name="clipname").value == "FRESH.fixed"


def test_rescan_never_ingested_reports_created_zero(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """FR-4 waiver delta (b): re-scanning stops re-counting the same clips.

    Pre-2.4 a never-ingested clip was never written, so EVERY scan
    reported it as created. Now the first scan materializes the row and
    the second one reports the honest zero.
    """
    rel = "2026/AH_20260101_recount"
    sources = _write_clips(tmp_path, rel, ["CLIPR1", "CLIPR2"])
    providers = [fake_provider.machine_name]

    es_fake.push(es_page(sources, total=2))
    first = _folder(tmp_path, rel).scan(providers=providers)
    es_fake.push(es_page(sources, total=2))
    second = _rescanned_folder(tmp_path, rel).scan(providers=providers)

    assert first["created"] == 2
    assert second["created"] == 0
    # Everything else about the second scan is unchanged.
    assert second["processed"] == 2
    assert second["hits"] == 2
    assert [clip.umid for clip in second["clips"]] == [
        f"{rel}/CLIPR1",
        f"{rel}/CLIPR2",
    ]


# --------------------------------------------------------------------------
# The write unit's gates: dry run, count_only, zero hits, one page or many
# --------------------------------------------------------------------------


def test_dry_run_scan_writes_nothing(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """FR-4 waiver delta (c): a dry run stops writing the folder row.

    Pre-2.4, ``dry_run`` gated only ``Folder.ingest``'s ingest block: a
    dry run still saved the Folder row and still fanned out an existing
    clip's metadata. Both are gone. (Full dry-run purity is 2.7.)
    """
    existing = Clip.objects.create(
        umid="DRYRUN-EXISTING",
        path="2019/OLD",
        storage_id=STORAGE_ID,
        reference_file="OLD",
    )
    ClipMetadata.objects.create(clip=existing, name="obsolete", value="untouched")

    rel = "2026/AH_20260101_dryrun"
    sources = _write_clips(tmp_path, rel, ["CLIPD1", "CLIPD2"])
    es_fake.push(es_page(sources, total=2))

    with CaptureQueriesContext(connection) as captured:
        response = _folder(tmp_path, rel).ingest(
            dry_run=True, providers=[fake_provider.machine_name]
        )

    assert response["errors"] == []
    assert response["processed"] == 2
    assert Folder.objects.count() == 0
    assert set(Clip.objects.values_list("pk", flat=True)) == {"DRYRUN-EXISTING"}
    assert list(ClipMetadata.objects.values_list("name", "value")) == [
        ("obsolete", "untouched")
    ]
    # The batched lookup is a read; nothing in the dry run writes.
    assert not [
        query["sql"]
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]


def test_dry_run_via_a_passed_context_writes_nothing(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """Tree mode: the gate reads the run context's dry_run, not a kwarg."""
    rel = "2026/AH_20260101_dryctx"
    es_fake.push(es_page(_write_clips(tmp_path, rel, ["CLIPC"]), total=1))
    ctx = ScanContext(
        storages={STORAGE_ID: StorageInfo(id=STORAGE_ID, root_path=str(tmp_path))},
        options=RunOptions(
            dry_run=True, providers=[fake_provider.machine_name], legacy_storages=[]
        ),
    )

    response = Folder(storage_id=STORAGE_ID, path=rel).scan(context=ctx)

    assert response["created"] == 1
    assert (Clip.objects.count(), Folder.objects.count()) == (0, 0)


def test_count_only_writes_nothing(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    rel = "2026/AH_20260101_countonly"
    _write_clips(tmp_path, rel, ["CLIPC1", "CLIPC2"])
    es_fake.push(es_page([{}] * 2, total=2))

    with CaptureQueriesContext(connection) as captured:
        response = _folder(tmp_path, rel).scan(
            number=0, count_only=True, providers=[fake_provider.machine_name]
        )

    assert response["hits"] == 2
    assert response["processed"] == 0
    # Not one statement: no clips are processed, so there is no plan and
    # no transaction to open.
    assert captured.captured_queries == []
    assert (Clip.objects.count(), Folder.objects.count()) == (0, 0)


def test_zero_hit_folder_writes_no_folder_row(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """Errored files still count as zero provider hits — nothing is written."""
    rel = "2026/AH_20260101_zerohit"
    (tmp_path / rel).mkdir(parents=True)
    # Indexed but absent from disk: the desync guard errors every file.
    es_fake.push(es_page([_source(f"{rel}/GONE.fake", "VX-41-GONE")], total=1))

    response = _folder(tmp_path, rel).scan(providers=[fake_provider.machine_name])

    assert len(response["errors"]) == 1
    assert response["processed"] == 1
    assert Folder.objects.count() == 0
    assert Clip.objects.count() == 0


def test_multipage_folder_saved_once(
    migrated_db, es_fake, es_page, fake_provider, tmp_path
):
    """One write unit AFTER the page loop, not one save per page."""
    rel = "2026/AH_20260101_multipage"
    (tmp_path / rel).mkdir(parents=True)
    pages = []
    for page_index in range(2):
        names = [f"P{page_index}_{i:03d}" for i in range(100)]
        pages.append(_write_clips(tmp_path, rel, names))
    pages.append(_write_clips(tmp_path, rel, ["TAIL1", "TAIL2"]))
    for page, total in zip(pages, [202, 202, 202]):
        es_fake.push(es_page(page, total=total))

    with CaptureQueriesContext(connection) as captured:
        response = _folder(tmp_path, rel).scan(
            number=0, providers=[fake_provider.machine_name]
        )

    assert es_fake.calls == [(0, 100), (100, 100), (200, 100)]
    assert response["errors"] == []
    assert response["processed"] == 202
    assert Folder.objects.count() == 1
    # Exactly one folder write for three pages.
    assert len(_folder_writes(captured.captured_queries)) == 1
    assert Clip.objects.count() == 202


# --------------------------------------------------------------------------
# Clip.persist_metadatas(): the writer the ingest endpoint calls
# --------------------------------------------------------------------------


def test_persist_metadatas_upserts_and_prunes(migrated_db):
    clip = Clip.objects.create(
        umid="ENDPOINT-1", path="2026/X", storage_id=STORAGE_ID, reference_file="F"
    )
    ClipMetadata.objects.create(clip=clip, name="obsolete", value="drop me")
    ClipMetadata.objects.create(clip=clip, name="clipname", value="old")

    clip.metadatas = {"clipname": "new", "umid": "ENDPOINT-1"}
    clip.persist_metadatas()

    assert set(ClipMetadata.objects.values_list("name", "value")) == {
        ("clipname", "new"),
        ("umid", "ENDPOINT-1"),
    }


def test_persist_metadatas_is_idempotent(migrated_db):
    clip = Clip.objects.create(
        umid="ENDPOINT-2", path="2026/X", storage_id=STORAGE_ID, reference_file="F"
    )
    clip.metadatas = {"clipname": "same"}

    clip.persist_metadatas()
    clip.persist_metadatas()

    assert ClipMetadata.objects.count() == 1


def test_persist_metadatas_without_a_memo_writes_nothing(migrated_db):
    """No metadatas ever assigned: the deleted save() fan-out did nothing too."""
    clip = Clip.objects.create(
        umid="ENDPOINT-3", path="2026/X", storage_id=STORAGE_ID, reference_file="F"
    )
    ClipMetadata.objects.create(clip=clip, name="kept", value="by an earlier run")

    Clip.objects.get(pk="ENDPOINT-3").persist_metadatas()

    assert ClipMetadata.objects.count() == 1


def test_persist_metadatas_with_an_empty_mapping_clears_the_rows(migrated_db):
    clip = Clip.objects.create(
        umid="ENDPOINT-4", path="2026/X", storage_id=STORAGE_ID, reference_file="F"
    )
    ClipMetadata.objects.create(clip=clip, name="gone", value="soon")

    clip.metadatas = {}
    clip.persist_metadatas()

    assert ClipMetadata.objects.count() == 0


def test_saving_a_clip_no_longer_fans_out_metadata(migrated_db):
    """The save() override is gone: saving writes the clip row and nothing else."""
    clip = Clip(
        umid="NOFANOUT-1",
        path="2026/X",
        storage_id=STORAGE_ID,
        reference_file="F",
        metadatas={"clipname": "not persisted by save()"},
    )

    clip.save()

    assert Clip.objects.filter(pk="NOFANOUT-1").exists()
    assert ClipMetadata.objects.count() == 0


# --------------------------------------------------------------------------
# FR-12: clip_xml reaches the DB, once, and is never re-parsed
# --------------------------------------------------------------------------


def test_clip_xml_is_persisted_on_the_insert(migrated_db, es_fake, es_page, tmp_path):
    """The REAL xdcam provider against a real sidecar.

    Pre-2.4 ``save()`` assigned ``clip_xml`` AFTER ``super().save()``, so
    the serialized XML never reached the column on a first save, and the
    probe re-read the sidecar from disk on every save afterwards.
    """
    rel = "2026/AH_20260101_clipxml/XDROOT/Clip"
    clip_dir = tmp_path / rel
    clip_dir.mkdir(parents=True)
    (clip_dir / "C0001.mxf").write_bytes(b"media payload")
    (clip_dir / "C0001M01.XML").write_bytes(XDCAM_SIDECAR)
    es_fake.push(es_page([_source(f"{rel}/C0001.mxf", "VX-41-XML")], total=1))

    response = _folder(tmp_path, rel).scan(providers=["xdcam"])

    assert response["errors"] == []
    stored = Clip.objects.get(pk="XDCAM-UMID-C0001").clip_xml
    assert "XDCAM-UMID-C0001" in stored
    assert stored.startswith("<NonRealTimeMeta")

    # And the stored column is authoritative afterwards: re-hydrating the
    # clip parses the string, never the file.
    reloaded = Clip.objects.get(pk="XDCAM-UMID-C0001")
    (clip_dir / "C0001M01.XML").unlink()
    assert reloaded.xml is not None
    assert (
        reloaded.xml.getValueFromPath("TargetMaterial/@umidRef") == "XDCAM-UMID-C0001"
    )


def test_clip_xml_parse_failure_never_costs_the_clip(
    migrated_db, es_fake, es_page, bad_sidecar_provider, tmp_path, caplog
):
    """An unparseable sidecar is logged and skipped — the clip still lands.

    ``clip_xml`` is an optional column; a card that hands out broken XML
    must not cost the clip its row, its metadata or its ingest.
    """
    rel = "2026/AH_20260101_badxml"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPBAD.bad").write_bytes(b"media payload")
    (tmp_path / rel / "BROKEN.XML").write_bytes(b"<not-xml")
    es_fake.push(es_page([_source(f"{rel}/CLIPBAD.bad", "VX-41-BAD")], total=1))

    response = _folder(tmp_path, rel).scan(providers=[BAD_SIDECAR_NAME])

    assert response["errors"] == []
    assert Clip.objects.get(pk=f"{rel}/CLIPBAD").clip_xml == ""
    assert ClipMetadata.objects.filter(clip_id=f"{rel}/CLIPBAD").exists()
    assert any(
        record.levelname == "ERROR" and "Cannot parse the clip XML" in record.message
        for record in caplog.records
    ), caplog.text


def test_missing_sidecar_leaves_clip_xml_empty(
    migrated_db, es_fake, es_page, bad_sidecar_provider, tmp_path
):
    """No sidecar on disk at all: same swallow, no error, no row lost."""
    rel = "2026/AH_20260101_nosidecarxml"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "CLIPNOX.bad").write_bytes(b"media payload")
    es_fake.push(es_page([_source(f"{rel}/CLIPNOX.bad", "VX-41-NOX")], total=1))

    response = _folder(tmp_path, rel).scan(providers=[BAD_SIDECAR_NAME])

    assert response["errors"] == []
    assert Clip.objects.get(pk=f"{rel}/CLIPNOX").clip_xml == ""


# --------------------------------------------------------------------------
# FR-27: the duplicate-folder loophole is survivable
# --------------------------------------------------------------------------


def test_duplicate_folder_rows_resolve_first_by_pk(migrated_db, caplog):
    """Two ``(path, NULL storage_id)`` rows — the only way past the constraint.

    NULLs are distinct under a unique constraint, so the enforced
    ``(path, storage_id)`` uniqueness lets this one pair through. Prod has
    zero such rows (audited 2026-08-21) and a scan always passes a
    storage_id, so this is hardening: ``get_or_new`` must resolve it
    deterministically instead of raising ``MultipleObjectsReturned``.
    """
    path = "2026/AH_20260101_dupfolder"
    first = Folder.objects.create(path=path, storage_id=None, provider_names="a")
    second = Folder.objects.create(path=path, storage_id=None, provider_names="b")

    folder, is_new = Folder.get_or_new(path=path, storage_id=None)

    assert is_new is False
    assert folder.pk == min(first.pk, second.pk)
    assert any(
        record.levelname == "WARNING" and "Duplicate Folder rows" in record.message
        for record in caplog.records
    ), caplog.text


def test_get_or_new_still_returns_the_single_match(migrated_db):
    path = "2026/AH_20260101_singlefolder"
    saved = Folder.objects.create(path=path, storage_id=STORAGE_ID)

    folder, is_new = Folder.get_or_new(path=path, storage_id=STORAGE_ID)

    assert (folder.pk, is_new) == (saved.pk, False)


# --------------------------------------------------------------------------
# The constraints the upserts rely on (no new migration in this story)
# --------------------------------------------------------------------------


def test_migrated_substrate_has_both_unique_indexes(migrated_db):
    """AD-16 verification instead of a migration.

    The prod audit (2026-08-21) found both constraints already live and
    enforced on PG with zero duplicate rows, so 0001–0016 stays the whole
    chain. This proves a freshly migrated substrate gets them too — if a
    future migration ever drops one, the upserts below lose their
    conflict target and this fails first.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name IN "
            "('TapelessIngest_folder', 'TapelessIngest_clipmetadata')"
        )
        indexes = {name: sql for name, sql in cursor.fetchall()}

    folder_unique = [
        sql
        for name, sql in indexes.items()
        if sql and "UNIQUE" in sql.upper() and "TapelessIngest_folder" in sql
    ]
    metadata_unique = [
        sql
        for name, sql in indexes.items()
        if sql and "UNIQUE" in sql.upper() and "TapelessIngest_clipmetadata" in sql
    ]
    assert len(folder_unique) == 1, indexes
    assert '"path", "storage_id"' in folder_unique[0]
    assert len(metadata_unique) == 1, indexes
    assert '"clip_id", "name"' in metadata_unique[0]


def test_upsert_hits_the_folder_unique_target(migrated_db):
    path = "2026/AH_20260101_conflicttarget"
    Folder.objects.create(path=path, storage_id=STORAGE_ID, clips_total=1)

    Folder.objects.bulk_create(
        [Folder(path=path, storage_id=STORAGE_ID, clips_total=42)],
        update_conflicts=True,
        unique_fields=["path", "storage_id"],
        update_fields=["clips_total"],
    )

    assert Folder.objects.count() == 1
    assert Folder.objects.get().clips_total == 42


def test_upsert_hits_the_clipmetadata_unique_target(migrated_db):
    clip = Clip.objects.create(
        umid="TARGET-1", path="2026/X", storage_id=STORAGE_ID, reference_file="F"
    )
    ClipMetadata.objects.create(clip=clip, name="clipname", value="before")

    ClipMetadata.objects.bulk_create(
        [ClipMetadata(clip_id=clip.pk, name="clipname", value="after")],
        update_conflicts=True,
        unique_fields=["clip", "name"],
        update_fields=["value"],
    )

    assert ClipMetadata.objects.count() == 1
    assert ClipMetadata.objects.get().value == "after"


def test_clip_upsert_hits_the_primary_key_target(migrated_db):
    Clip.objects.create(
        umid="TARGET-2",
        path="2026/ORIGINAL",
        storage_id=STORAGE_ID,
        reference_file="F",
        clip_xml="",
    )

    Clip.objects.bulk_create(
        [
            Clip(
                umid="TARGET-2",
                path="2026/SOMEWHERE_ELSE",
                storage_id="VX-OTHER",
                reference_file="G",
                clip_xml="<x/>",
            )
        ],
        update_conflicts=True,
        unique_fields=["umid"],
        update_fields=["clip_xml"],
    )

    reloaded = Clip.objects.get(pk="TARGET-2")
    assert (Clip.objects.count(), reloaded.clip_xml) == (1, "<x/>")
    assert (reloaded.path, reloaded.storage_id) == ("2026/ORIGINAL", STORAGE_ID)

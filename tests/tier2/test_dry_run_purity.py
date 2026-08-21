"""Tier 2 (story 2.7): a dry run rehearses everything and changes nothing.

Two halves, and they are not the same claim:

- **purity (FR-31)** — every phase runs (discovery, verification,
  extraction, hash recovery, the persistence PLAN, the 2.5 ladder) and
  nothing is written anywhere. Proved with a zero-write WHITELIST over
  every statement the run executes (``connection.execute_wrapper`` sees
  ``bulk_create`` and ``queryset.update``, which ``CaptureQueriesContext``
  can miss), row-count DELTAS (the sqlite ``:memory:`` DB is
  session-scoped, so absolute counts prove nothing), the sharpest pin of
  all — the Folder instance was never saved — and zero invocations of
  ``Clip.ingest`` and ``Folder.getCollection`` (``getCollection`` can
  CREATE collections; AD-6/AD-15).

  Both dry-run entry paths are covered: the paged one
  (``ingest(dry_run=True)``, the Portal UI/API path) and the
  ctx-authoritative one (``ingest(context=…)``, the CRON path, where
  ``context.options.dry_run`` overrides the kwarg default). A gate that
  read the kwarg instead of the context would leave the cron path impure
  and pass the first test.

- **truthfulness (FR-11)** — the counters a dry run reports are the ones
  a real run would produce, for the counters the LADDER decides:
  ``hits``, ``processed``, ``created``, ``already_ingested``, ``skipped``
  (ladder share) and ``ingested`` (= ``len(to_ingest)``). That and only
  that is the equality invariant. ``failed``, ``replaced`` and the share
  of ``skipped`` that ``Clip.import_file`` decides on its own are
  SUBMISSION-OUTCOME counters: structurally 0 under dry-run because no
  submission occurs. So dry-run ``ingested`` is an UPPER bound on a real
  run's and dry-run ``skipped`` a LOWER bound; they coincide exactly when
  every submission succeeds. ``test_dry_run_counters_match_real_run``
  constructs that case; ``test_a_post_ladder_skip_shows_the_bound``
  constructs the other one and pins the bound instead.

Mechanism note (deviation from the spec's letter, deliberately
stronger). The spec specified the dry-vs-real comparison around a
monkeypatched ``Clip.ingest``, on the ground that the portal stub's
helper classes were attribute-less placeholders and a real submission
leg could not run off-server. That is no longer true: the story-2.4/2.5
patch round gave ``tests/portal_stub`` a modelled Vidispine surface, and
``tests/tier2/test_ingest_discipline.py`` already drives the REAL
``Clip.import_file`` through it. So the real-run side of the comparison
here runs the real submission leg — real ``import_file``, real
``Clip.ingest``, real ingest-state write — and only ``Folder.getCollection``
is doubled (PLUGIN code, not Portal: AD-11 is untouched; it needs a
Settings row and a live search backend, neither of which says anything
about the ladder, and it is the same double ``test_ingest_discipline.py``
uses). This proves strictly more: with a stub told to answer
``ingested: True``, a drift in ``import_file``'s verdict ladder or in
``Folder.ingest``'s result accumulation would still let the two sides
agree. Against the real leg it cannot.
"""

import os
import re
from contextlib import contextmanager

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection

from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider
from portal.plugins.TapelessIngest.scan.adapters import build_default_context

from tests.portal_stub import VidispineFake

STORAGE_ID = "VX-41"
PROVIDER_NAME = "fakedryrun"

# Every statement a dry run is ALLOWED to execute. A whitelist, not a
# three-verb blacklist: an unforeseen write verb (a raw DELETE, a
# CREATE TEMP TABLE, an sqlite REPLACE INTO) must fail this test rather
# than slip through the gaps of an enumeration of what we thought of.
READ_ONLY_SQL = re.compile(
    r"^(SELECT|SAVEPOINT|RELEASE|ROLLBACK|BEGIN|PRAGMA)\b", re.IGNORECASE
)


class DryRunProvider(BaseProvider):
    """A provider whose clips can go through the REAL ``import_file``.

    Subclasses the base (like ``test_ingest_discipline``'s ingestable
    provider) so the media-file accessors ``import_file`` reads are real
    enough for the submission leg to execute. That matters for the purity
    tests too: with a provider that could not submit, "nothing was
    written" would be true for the wrong reason.
    """

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "Fake Dry-Run Provider"
        self.machine_name = PROVIDER_NAME

    def getExtensions(self):
        return [".dry"]

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
def dry_run_provider():
    provider = DryRunProvider()
    Clip._PROVIDER_CACHE[PROVIDER_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(PROVIDER_NAME, None)


@pytest.fixture
def collection_seam(monkeypatch):
    """Count ``Folder.getCollection`` without resolving a real collection.

    PLUGIN code, not Portal — AD-11 untouched. Under dry-run it must
    never be called at all: it can CREATE collections in Vidispine
    (AD-6), which is the one Vidispine mutation a "read-only" rehearsal
    could commit without writing a single DB row.
    """
    calls = []

    def fake_get_collection(self, user, dryrun=False):
        calls.append(self.path)
        return "VX-COLLECTION"

    monkeypatch.setattr(Folder, "getCollection", fake_get_collection)
    return calls


@pytest.fixture
def ingest_recorder(monkeypatch):
    """Record every ``Clip.ingest`` call — and still let it happen.

    Recording-and-delegating rather than recording-and-returning keeps
    the two purity assertions INDEPENDENT: if the dry-run gate ever
    leaked, the recorder reports it AND the real writes show up in the
    SQL whitelist and the row-count deltas. A recorder that swallowed
    the call would make the whitelist vacuous.
    """
    calls = []
    real_ingest = Clip.ingest

    def recording_ingest(self, *args, **kwargs):
        calls.append(self.umid)
        return real_ingest(self, *args, **kwargs)

    monkeypatch.setattr(Clip, "ingest", recording_ingest)
    return calls


def _source(path, file_id, file_hash=None):
    return {
        "path": path,
        "hash": f"hash-{file_id}" if file_hash is None else file_hash,
        "storage": STORAGE_ID,
        "id": file_id,
        "size": 1024,
    }


def _folder(tmp_path, rel_path):
    """An UNSAVED folder — never having been saved is the sharpest pin."""
    folder = Folder(storage_id=STORAGE_ID, path=rel_path)
    # Presetting _root_path bypasses StorageHelper/cache entirely.
    folder._root_path = str(tmp_path)
    return folder


def _write_clips(tmp_path, rel, names, hashes=None):
    (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    sources = []
    for name in names:
        (tmp_path / rel / f"{name}.dry").write_bytes(b"clip data")
        sources.append(
            _source(
                f"{rel}/{name}.dry",
                f"VX-41-{name}",
                file_hash=(hashes or {}).get(name),
            )
        )
    return sources


@contextmanager
def _captured_sql():
    """Every statement executed on this connection, in order.

    ``connection.execute_wrapper`` rather than ``CaptureQueriesContext``:
    the wrapper sits under the cursor, so it sees ``bulk_create`` and
    ``queryset.update`` whatever the DEBUG setting and whatever the
    query-log truncation does.
    """
    statements = []

    def recorder(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(recorder):
        yield statements


def _assert_read_only(statements):
    offending = [sql for sql in statements if not READ_ONLY_SQL.match(sql.lstrip())]
    assert not offending, (
        f"a dry run executed {len(offending)} non-read-only statement(s) "
        f"(FR-31): {offending}"
    )


def _assert_folder_never_saved(folder):
    """The sharpest pin: this Folder instance has no row, at all.

    Spelled ``_state.adding`` rather than ``folder.pk is None``:
    ``Folder.id`` is a ``UUIDField(default=uuid.uuid4)``, so an unsaved
    instance ALREADY carries a pk and the null check would pin nothing.
    ``_state.adding`` is Django's own "was never loaded from, or written
    to, the DB" flag, and the existence query proves it from the other
    side.
    """
    assert folder._state.adding is True
    assert not Folder.objects.filter(pk=folder.pk).exists()


def _row_counts():
    """The four row populations a scan/ingest can grow.

    DELTAS, never absolutes: ``migrated_db`` is session-scoped over one
    sqlite ``:memory:`` connection, so an absolute count says as much
    about the previous test as about this one.
    """
    return (
        Clip.objects.count(),
        ClipMetadata.objects.count(),
        Folder.objects.count(),
        Clip.folders.through.objects.count(),
    )


# The FR-31 fixture, shared by both entry-path tests:
#   CLIPNEW    — no row, hashed        -> ladder SELECTS it (would ingest)
#   CLIPOLD    — row with item_id      -> ladder SKIPS it (already ingested)
#   CLIPNOHASH — no row, falsy hash    -> ladder SKIPS it (NFR-1 retry)
# `legacy_storages=[]` so hash recovery can assign no item_id and the
# `already_ingested` count below is the fixture's, not the fake's.
FIXTURE_COUNTERS = {
    "hits": 3,
    "processed": 3,
    "created": 2,  # CLIPNEW + CLIPNOHASH: neither has a pre-saved row
    "already_ingested": 1,  # CLIPOLD
    "ingested": 1,  # would-be: CLIPNEW
    "skipped": 2,  # ladder: CLIPOLD + CLIPNOHASH
    "failed": 0,  # structural: no submission occurred
    "replaced": 0,  # structural: no submission occurred
}


def _seed_purity_fixture(tmp_path, rel, es_fake, es_page):
    sources = _write_clips(
        tmp_path,
        rel,
        ["CLIPNEW", "CLIPOLD", "CLIPNOHASH"],
        hashes={"CLIPNOHASH": ""},
    )
    Clip(umid=f"{rel}/CLIPOLD", item_id="VX-100").save()
    es_fake.push(es_page(sources, total=3))


def _assert_dry_run_response(response):
    assert response["errors"] == [], response["errors"]
    for key, expected in FIXTURE_COUNTERS.items():
        assert response[key] == expected, f"{key}: {response[key]} != {expected}"


# --------------------------------------------------------------------------
# FR-31: zero writes, both entry paths
# --------------------------------------------------------------------------


def test_dry_run_writes_nothing(
    migrated_db,
    es_fake,
    es_page,
    dry_run_provider,
    storage_fake,
    tmp_path,
    collection_seam,
    ingest_recorder,
):
    """The paged / default-ctx path: ``ingest(dry_run=True, …)``."""
    rel = "2026/AH_20260101_pure"
    _seed_purity_fixture(tmp_path, rel, es_fake, es_page)
    folder = _folder(tmp_path, rel)
    before = _row_counts()

    with _captured_sql() as statements:
        response = folder.ingest(
            dry_run=True, providers=[PROVIDER_NAME], legacy_storages=[]
        )

    # Nothing was submitted and no collection was resolved (or created).
    assert ingest_recorder == []
    assert collection_seam == []
    assert VidispineFake.calls == []
    assert storage_fake.get_files_in_storage_calls == []
    # Nothing was written.
    _assert_read_only(statements)
    assert _row_counts() == before
    # The sharpest pin: the folder row itself was never inserted.
    _assert_folder_never_saved(folder)
    assert Folder.objects.count() == 0
    # ...and every phase still ran, and reported what it decided.
    _assert_dry_run_response(response)


def test_ctx_authoritative_dry_run_is_pure(
    migrated_db,
    es_fake,
    es_page,
    dry_run_provider,
    storage_fake,
    tmp_path,
    collection_seam,
    ingest_recorder,
):
    """The CRON path: ``dry_run`` comes from the context, not a kwarg.

    ``Folder.ingest`` resolves ``dry_run`` two ways, and a passed
    context's options WIN over the kwarg default. A gate that read the
    kwarg would be pure for the Portal UI and impure for every nightly
    ``--dryrun`` run — and would pass ``test_dry_run_writes_nothing``.
    """
    rel = "2026/AH_20260101_purectx"
    _seed_purity_fixture(tmp_path, rel, es_fake, es_page)
    folder = _folder(tmp_path, rel)
    context = build_default_context(
        folder,
        user=None,
        dry_run=True,
        providers=[PROVIDER_NAME],
        legacy_storages=[],
        replace=False,
    )
    before = _row_counts()

    with _captured_sql() as statements:
        # No dry_run= kwarg anywhere: the context alone says so.
        response = folder.ingest(context=context)

    assert ingest_recorder == []
    assert collection_seam == []
    assert VidispineFake.calls == []
    _assert_read_only(statements)
    assert _row_counts() == before
    _assert_folder_never_saved(folder)
    assert Folder.objects.count() == 0
    # Byte-identical counters to the paged path.
    _assert_dry_run_response(response)


# --------------------------------------------------------------------------
# FR-11: the counters a dry run reports are the ones a real run produces
# --------------------------------------------------------------------------

LADDER_DECIDABLE = (
    "hits",
    "processed",
    "created",
    "already_ingested",
    "skipped",
    "ingested",
)


def test_dry_run_counters_match_real_run(
    migrated_db,
    es_fake,
    es_page,
    dry_run_provider,
    storage_fake,
    tmp_path,
    collection_seam,
):
    """The equality invariant, over the ladder-decidable six only.

    The real side runs the REAL submission leg (``Clip.ingest`` ->
    ``import_file`` -> the portal_stub Vidispine doubles), so the
    agreement is between the dry run's PREDICTION and what the
    production code path actually did — not between two readings of the
    same stub. Only ``Folder.getCollection`` is doubled (see the module
    docstring).

    The dry run goes FIRST precisely because it writes nothing: the real
    run therefore starts from the identical DB state, which is what makes
    ``created`` and ``already_ingested`` comparable at all.
    """
    rel = "2026/AH_20260101_symmetry"
    sources = _write_clips(
        tmp_path,
        rel,
        ["CLIPNEW", "CLIPOLD", "CLIPNOHASH"],
        hashes={"CLIPNOHASH": ""},
    )
    Clip(umid=f"{rel}/CLIPOLD", item_id="VX-100").save()

    es_fake.push(es_page(sources, total=3))
    dry = _folder(tmp_path, rel).ingest(
        dry_run=True, providers=[PROVIDER_NAME], legacy_storages=[]
    )

    es_fake.push(es_page(sources, total=3))
    real = _folder(tmp_path, rel).ingest(
        dry_run=False, providers=[PROVIDER_NAME], legacy_storages=[]
    )

    assert (dry["errors"], real["errors"]) == ([], [])
    # Every submission succeeded, so the bounds are tight and the six
    # ladder-decidable counters coincide exactly.
    assert {key: dry[key] for key in LADDER_DECIDABLE} == {
        key: real[key] for key in LADDER_DECIDABLE
    }
    assert dry["ingested"] == 1
    assert dry["skipped"] == 2
    # Submission-outcome counters: asserted 0 on the DRY side only, and
    # structurally so — no submission occurred. The real side's values
    # are an outcome, not a contract, so nothing is claimed about them.
    assert (dry["failed"], dry["replaced"]) == (0, 0)
    # The real run really did submit — otherwise the equality above would
    # be an agreement between two rehearsals.
    assert "importFileToPlaceholder" in VidispineFake.call_names()
    assert collection_seam == [rel]
    assert Clip.objects.get(pk=f"{rel}/CLIPNEW").job_id == "VX-JOB-DEFAULT"


def test_a_would_be_replacement_surfaces_as_ingested_never_replaced(
    migrated_db,
    es_fake,
    es_page,
    dry_run_provider,
    storage_fake,
    tmp_path,
    collection_seam,
    ingest_recorder,
):
    """A `--replace --dryrun` run predicts an ingest, not a replacement.

    ``replaced`` is decided INSIDE ``import_file``, after it has looked
    at the item's original shape — a rehearsal cannot know it, and there
    is no replacement OUTCOME to report because nothing was replaced. So
    the clip the ladder selected surfaces as a would-be ``ingested`` and
    ``replaced`` stays 0, which is the structural reason, not a ruling.
    """
    rel = "2026/AH_20260101_wouldreplace"
    sources = _write_clips(tmp_path, rel, ["CLIPREP"])
    Clip(umid=f"{rel}/CLIPREP", item_id="VX-REP").save()
    es_fake.push(es_page(sources, total=1))
    before = _row_counts()

    with _captured_sql() as statements:
        response = _folder(tmp_path, rel).ingest(
            dry_run=True,
            replace=True,
            providers=[PROVIDER_NAME],
            legacy_storages=[],
        )

    assert response["errors"] == []
    assert response["already_ingested"] == 1
    assert (
        response["ingested"],
        response["skipped"],
        response["failed"],
        response["replaced"],
    ) == (1, 0, 0, 0)
    # ...and it is still a rehearsal.
    assert (ingest_recorder, collection_seam) == ([], [])
    _assert_read_only(statements)
    assert _row_counts() == before


def test_a_post_ladder_skip_shows_the_bound_not_the_equality(
    migrated_db,
    es_fake,
    es_page,
    dry_run_provider,
    storage_fake,
    tmp_path,
    collection_seam,
):
    """When a submission does NOT succeed, the bound is what holds.

    CLIPHELD is an incomplete import (item_id, no job id, placeholder
    status), so the 2.5 ladder lets it through and a dry run predicts it
    would be ingested. The real ``import_file`` then finds an original
    shape that already holds a file and refuses it — a POST-LADDER skip,
    decided inside the submission leg, which no rehearsal can predict.

    Real ``skipped`` is therefore dry-run ``skipped`` + 1 and real
    ``ingested`` is dry-run ``ingested`` - 1: dry-run ``ingested`` is an
    upper bound, dry-run ``skipped`` a lower bound. Claiming whole-dict
    equality would be claiming this case cannot happen.
    """
    rel = "2026/AH_20260101_bound"
    sources = _write_clips(tmp_path, rel, ["CLIPHELD"])
    umid = f"{rel}/CLIPHELD"
    Clip(
        umid=umid,
        path=rel,
        storage_id=STORAGE_ID,
        item_id="VX-HELD",
        status=Clip.STATUS_PLACHOLDER_CREATED,
    ).save()
    VidispineFake.set_item("VX-HELD")
    VidispineFake.set_original_shape(
        "VX-HELD",
        "VX-HELD-SHAPE",
        files=[{"id": "VX-OTHER-FILE", "storage": "VX-OTHERSTORAGE"}],
    )

    es_fake.push(es_page(sources, total=1))
    dry = _folder(tmp_path, rel).ingest(
        dry_run=True, providers=[PROVIDER_NAME], legacy_storages=[]
    )

    es_fake.push(es_page(sources, total=1))
    real = _folder(tmp_path, rel).ingest(
        dry_run=False, providers=[PROVIDER_NAME], legacy_storages=[]
    )

    # The rehearsal predicted an ingest...
    assert (dry["ingested"], dry["skipped"]) == (1, 0)
    # ...the submission leg decided otherwise.
    assert (real["ingested"], real["skipped"]) == (0, 1)
    assert real["skipped"] == dry["skipped"] + 1
    assert real["ingested"] == dry["ingested"] - 1
    # The counters the ladder alone decides still agree.
    assert {key: dry[key] for key in ("hits", "processed", "created")} == {
        key: real[key] for key in ("hits", "processed", "created")
    }
    assert (dry["failed"], dry["replaced"]) == (0, 0)


# --------------------------------------------------------------------------
# The end-of-run summary says which kind of run produced the counters
# --------------------------------------------------------------------------

COMMANDS = ["scan_tapeless_dir", "check_clips_in_folder"]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("dryrun", [True, False], ids=["dryrun", "real"])
def test_summary_line_is_labelled_under_dryrun(
    command, dryrun, migrated_db, monkeypatch, storage_fake
):
    """The label reaches the operator — and Slack, through CustomLogger.

    ``tests/tier1/test_commands_in_sync.py::test_handle_sources_identical``
    proves the sibling command's ``handle`` carries the same edit; this
    runs it for both anyway, because source equality of a block nobody
    executes is not evidence that the block works.
    """
    import importlib

    module = importlib.import_module(
        f"portal.plugins.TapelessIngest.management.commands.{command}"
    )
    # handle() reassigns these module globals; re-setting them to their
    # current values via monkeypatch restores them on teardown.
    monkeypatch.setattr(module, "SLACK_ACCESS_TOKEN", module.SLACK_ACCESS_TOKEN)
    monkeypatch.setattr(module, "logger", module.logger)
    monkeypatch.setattr(module, "scan_tapeless_dir", lambda parent_folder, **kw: 7)
    storage_fake.set_root("VX-41", "/dryrun-root")

    user = User.objects.create(pk=4244, username=f"story27-summary-{command}-{dryrun}")
    args = ["--storage", "VX-41", "--path", "2026", "--userId", "4244"]
    if dryrun:
        args.append("--dryrun")
    try:
        call_command(command, *args)
        messages = module.logger.messages
    finally:
        user.delete()  # keep the auth table empty for the unknown-user tests

    if dryrun:
        assert "DRY-RUN: 7 folders scanned — no changes were made" in messages
        assert "7 folders scanned" not in messages
    else:
        assert "7 folders scanned" in messages
        assert not [message for message in messages if "DRY-RUN" in message]

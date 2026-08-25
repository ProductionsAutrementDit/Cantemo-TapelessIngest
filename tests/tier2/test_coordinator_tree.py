"""Tier 2 (story 2.8): the whole pipeline, composed, over a real tree.

``tests/tier1/test_scan_coordinator.py`` proves the coordinator's contract
with a worker double. This proves the composition: the REAL
``process_folder`` — real discovery, verification, extraction, persistence
plan and 2.5 ladder — driven by the REAL walk over a real tmp tree, with
one folder that hits, one that finds nothing but errors, and one that dies.

The three folders are deliberately of three different KINDS, because the
counting rule they establish is the one Epic 3 inherits:

* a hit folder is scanned;
* a ZERO-hit folder with per-file errors is also scanned — per-file errors
  never fail a folder, which is exactly what today's ``count += 1``
  semantics were;
* only a folder whose worker CAUGHT at the folder boundary is failed.

``query_elastic`` is answered by a path-routed responder (2.6's harness):
it reads the ``parent`` regexps out of the search doc the scan actually
built, so what each folder finds is a property of the walk, not of a page
script's ordering.
"""

import dataclasses
import os
import re

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.scan.adapters import build_context
from portal.plugins.TapelessIngest.scan.coordinator import RunResult, TIMING_PHASES

from tests.portal_stub import RestTransportFake, VidispineFake
from tests.sql_capture import captured_sql

STORAGE_ID = "VX-41"
ROOT = "2026"

HIT_ONE = f"{ROOT}/AH_20260101_one"
HIT_TWO = f"{ROOT}/AH_20260102_two"
ZERO = f"{ROOT}/AH_20260103_zero"
BOOM = f"{ROOT}/AH_20260104_boom"


def _source(path):
    return {
        "path": path,
        "hash": f"hash-{path}",
        "storage": STORAGE_ID,
        "id": f"VX-41-{path}",
        "size": 1024,
    }


def _parent_regexps(search_doc):
    """Every ``{"regexp": {"parent": ...}}`` value in a search doc."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            regexp = node.get("regexp")
            if isinstance(regexp, dict) and "parent" in regexp:
                found.append(regexp["parent"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(search_doc)
    return found


def _install_router(es_fake, indexed, ghosts=(), raises=()):
    """Answer query_elastic from the search doc, like the real index.

    ``ghosts`` are index entries with no file behind them (the
    index/filesystem desync): they contribute a hit ROW but not to
    ``total``, which is how a "zero hits, real errors" folder is built.
    ``raises`` name folders whose query blows up — the index being briefly
    unavailable for one folder is precisely the folder-boundary failure
    the never-raising worker exists for.
    """
    all_paths = list(indexed) + list(ghosts)
    indexed_set = set(indexed)

    def respond(search_doc, first, number):
        regexps = _parent_regexps(search_doc)
        # parent_filters[0] is always the bare escaped folder path.
        folder_path = min(regexps, key=len)
        for raising in raises:
            if folder_path == re.escape(raising):
                raise TapelessIngestException("index unavailable for this folder")
        patterns = []
        for regexp in regexps:
            try:
                patterns.append(re.compile(regexp))
            except re.error:
                continue
        hits = [
            path
            for path in all_paths
            if any(pattern.fullmatch(os.path.dirname(path)) for pattern in patterns)
        ]
        return {
            "hits": {
                "total": {"value": len([p for p in hits if p in indexed_set])},
                "hits": [{"_source": _source(path)} for path in hits],
            }
        }

    es_fake.route(respond)


@pytest.fixture
def django_user(migrated_db):
    """A real User row: an INGESTING run now fails fast without one."""
    user = User.objects.create(pk=4246, username="story28-tree")
    yield user
    user.delete()  # keep the auth table empty for the unknown-user tests


@pytest.fixture
def tree(tmp_path, es_fake, storage_fake):
    """One shoot folder per outcome kind, plus the index that feeds them."""
    files = [
        f"{HIT_ONE}/CLIPA.fake",
        f"{HIT_TWO}/CLIPB.fake",
        f"{HIT_TWO}/CLIPC.fake",
        f"{BOOM}/CLIPD.fake",
    ]
    for relative in files:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    # A real directory whose only index entry has no file behind it.
    (tmp_path / ZERO).mkdir(parents=True, exist_ok=True)
    ghost = f"{ZERO}/GHOST.fake"

    _install_router(es_fake, files, ghosts=[ghost], raises=[BOOM])
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return tmp_path


def _context(fake_provider, **options):
    defaults = dict(
        user=None,
        dry_run=True,
        providers=[fake_provider.machine_name],
        legacy_storages=[],
        replace=False,
        startwith=["AH_"],
    )
    defaults.update(options)
    return build_context([STORAGE_ID], **defaults)


def _run(ctx, root=ROOT):
    emitted = []
    folder = Folder(storage_id=STORAGE_ID, path=root)
    folder._root_path = ctx.root_path_for(STORAGE_ID)
    return folder.scan_tree(ctx, emit=emitted.append), emitted


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------


def test_a_tree_run_merges_the_field_wise_sums(
    migrated_db, tree, fake_provider, es_fake
):
    ctx = _context(fake_provider)
    run_result, _emitted = _run(ctx)

    assert isinstance(run_result, RunResult)
    # hits is the SUM of the per-folder hits: 1 + 2 + 0 (zero-hit) and
    # nothing at all from the folder that died.
    assert run_result.counters.hits == 3
    # `processed` counts every hit ROW the scan looked at, the ghost
    # included: it was processed, and then it errored.
    assert run_result.counters.processed == 4
    assert run_result.counters.created == 3
    assert run_result.counters.already_ingested == 0
    # Dry run, so these are the ladder's WOULD-BE verdict (story 2.7).
    assert run_result.counters.ingested == 3
    assert (run_result.counters.failed, run_result.counters.replaced) == (0, 0)
    # Three folders scanned, one failed — the zero-hit folder counts as
    # SCANNED even though it produced an error.
    assert (run_result.folders_scanned, run_result.folders_failed) == (3, 1)


def test_the_merged_result_holds_no_clip_objects(
    migrated_db, tree, fake_provider, es_fake
):
    """The prod-scale invariant Epic 3 inherits (188k clips, 8k folders)."""
    run_result, _emitted = _run(_context(fake_provider))

    assert not hasattr(run_result, "clips")
    assert "clips" not in {f.name for f in dataclasses.fields(RunResult)}


# --------------------------------------------------------------------------
# Ordering and continuation
# --------------------------------------------------------------------------


def test_log_lines_are_emitted_in_merge_key_order(
    migrated_db, tree, fake_provider, es_fake
):
    """Tree order, not completion order — sorted(listing.dirs), depth-first."""
    _run_result, emitted = _run(_context(fake_provider))

    folder_lines = [
        line
        for line in emitted
        if not line.startswith("DRY-RUN:") and not line.startswith("User has to be")
    ]
    assert len(folder_lines) == 4
    # `sorted(listing.dirs)`, so the four shoot folders report in name
    # order: …01_one, …02_two, …03_zero, …04_boom. That the folder that
    # DIED is last is what makes this order tree-derived rather than
    # completion-derived — it finished first, having done nothing.
    assert folder_lines[0].startswith(f"found 1 clips in {HIT_ONE},")
    assert folder_lines[1].startswith(f"found 2 clips in {HIT_TWO},")
    assert folder_lines[2].startswith(f"found 0 clips in {ZERO},")
    assert folder_lines[3] == (
        f"Error ingesting {BOOM}: index unavailable for this folder"
    )


def test_a_dying_folder_does_not_stop_the_run(
    migrated_db, tree, fake_provider, es_fake
):
    """The `Executor.map` post-mortem, made concrete.

    The failing folder sorts LAST here on purpose in one direction and is
    proven not to matter in the other: every other folder still produced
    its counters, and the failure is reported in BOTH the errors and the
    operator's log lines.
    """
    run_result, emitted = _run(_context(fake_provider))

    template = f"Error ingesting {BOOM}: index unavailable for this folder"
    assert template in run_result.errors
    assert template in emitted
    assert run_result.counters.hits == 3


def test_a_zero_hit_folders_errors_are_surfaced_and_it_still_counts(
    migrated_db, tree, fake_provider, es_fake
):
    run_result, emitted = _run(_context(fake_provider))

    [line] = [line for line in emitted if line.startswith(f"found 0 clips in {ZERO},")]
    assert "1 errors encountered" in line
    assert f"Error scanning file {ZERO}/GHOST.fake" in line
    # Per-file errors never fail the FOLDER.
    assert run_result.folders_failed == 1  # the boom folder, and only it


def test_all_eight_counters_appear_on_every_folder_line(
    migrated_db, tree, fake_provider, es_fake
):
    """FR-37's other half: no line reads a key that might not be there."""
    _run_result, emitted = _run(_context(fake_provider))

    [line] = [
        line for line in emitted if line.startswith(f"found 2 clips in {HIT_TWO}")
    ]
    for fragment in (
        "found 2 clips",
        "0 already ingested",
        "2 created",
        "2 ingested",
        "0 skipped",
        "0 replaced",
        "0 errors encountered",
    ):
        assert fragment in line, line


# --------------------------------------------------------------------------
# The summary and the timing fold
# --------------------------------------------------------------------------


def test_the_summary_closes_the_run_with_folders_and_counters(
    migrated_db, tree, fake_provider, es_fake
):
    run_result, emitted = _run(_context(fake_provider))

    folders, counters = emitted[-2], emitted[-1]
    assert folders.startswith("DRY-RUN: 3 folders scanned, 1 failed in ")
    for phase in TIMING_PHASES:
        assert f"{phase} " in folders
    assert "other " in folders
    # The counters the whole story exists to own reach the operator, not
    # just the folder count and the phase timings.
    assert counters == (
        f"DRY-RUN: {run_result.counters.hits} clips found, "
        f"{run_result.counters.created} created, "
        f"{run_result.counters.already_ingested} already ingested, "
        f"{run_result.counters.processed} processed, "
        f"{run_result.counters.ingested} ingested, "
        f"{run_result.counters.skipped} skipped, "
        f"{run_result.counters.failed} failed, "
        f"{run_result.counters.replaced} replaced, "
        f"{len(run_result.errors)} errors"
    )


def test_a_real_run_summary_is_not_dry_run_labelled(
    migrated_db, tree, fake_provider, es_fake, monkeypatch, django_user
):
    # getCollection needs a Settings row and a live search backend and says
    # nothing about the summary; the same double test_dry_run_purity uses.
    monkeypatch.setattr(
        Folder, "getCollection", lambda self, user, dryrun=False: "VX-COLLECTION"
    )
    _run_result, emitted = _run(
        _context(fake_provider, dry_run=False, user=django_user)
    )

    assert not any("DRY-RUN" in line for line in emitted)
    assert emitted[-2].startswith("3 folders scanned, 1 failed in ")


def test_ctx_timings_equals_the_merged_fold(migrated_db, tree, fake_provider, es_fake):
    """`ScanContext.timings` has exactly one writer, and this is what it wrote."""
    ctx = _context(fake_provider)
    run_result, _emitted = _run(ctx)

    for phase in TIMING_PHASES:
        assert getattr(ctx.timings, phase) == pytest.approx(
            getattr(run_result.timings, phase)
        )
    # The phases a dry run really executes cost real time; persistence is
    # gated off, so it is the one that legitimately can be 0.
    assert run_result.timings.discovery > 0.0
    assert run_result.timings.extraction > 0.0


def test_the_window_line_uses_a_prefix_of_its_own(
    migrated_db, tree, fake_provider, es_fake
):
    """It must not collide with the commands' `format_window_log` line."""
    ctx = _context(fake_provider, date_window=["20260101", "20260102"])
    _run_result, emitted = _run(ctx)

    assert emitted[-1] == "Window: 20260101 to 20260102 (2 day folders)"
    assert not any(line.startswith("Scanning folders from ") for line in emitted)


# --------------------------------------------------------------------------
# Depth-1 filtering through the real walk
# --------------------------------------------------------------------------


def test_startwith_selects_depth_one_only(migrated_db, tree, fake_provider, es_fake):
    (tree / ROOT / "ZZ_ignored").mkdir(parents=True, exist_ok=True)
    # A depth-2 child that matches NO depth-1 filter is still scanned.
    (tree / HIT_ONE / "RUSHES").mkdir(parents=True, exist_ok=True)

    run_result, emitted = _run(_context(fake_provider))

    assert not any("ZZ_ignored" in line for line in emitted)
    # RUSHES was scanned (zero hits, no errors -> no line), so the four
    # shoot folders are still the only ones that reported.
    assert run_result.folders_scanned == 4


# --------------------------------------------------------------------------
# Dry-run purity survives composition
# --------------------------------------------------------------------------


def test_a_dry_tree_run_writes_no_rows(migrated_db, tree, fake_provider, es_fake):
    """2.7's guarantee, through the coordinator rather than one façade call."""
    before = (Clip.objects.count(), Folder.objects.count())

    run_result, _emitted = _run(_context(fake_provider))

    # It really did find and plan three clips...
    assert run_result.counters.created == 3
    # ...and wrote none of them.
    assert (Clip.objects.count(), Folder.objects.count()) == before


# --------------------------------------------------------------------------
# Story 3.1: the pool through the whole composition
# --------------------------------------------------------------------------

READ_ONLY_SQL = re.compile(
    r"^(SELECT|SAVEPOINT|RELEASE|ROLLBACK|BEGIN|PRAGMA)\b", re.IGNORECASE
)


def _stable_rows():
    """The run's DB rows, minus pks/timestamps that legitimately differ."""
    clips = sorted(
        Clip.objects.values_list(
            "umid",
            "folder_path",
            "path",
            "storage_id",
            "status",
            "provider_name",
            "reference_file",
        )
    )
    metadatas = sorted(ClipMetadata.objects.values_list("clip_id", "name", "value"))
    folders = sorted(
        Folder.objects.values_list(
            "path", "storage_id", "clips_total", "provider_names"
        )
    )
    return clips, metadatas, folders


def _comparable(run_result):
    """Everything but the timings, which legitimately differ run to run."""
    return dataclasses.replace(run_result, timings=None)


def _normalized_emission(emitted):
    """The emission with the wall-clock summary line made comparable."""
    # emitted[-2] is "N folders scanned, M failed in X.Xs — <phases>";
    # only its timings differ between runs.
    return emitted[:-2] + [emitted[-2].split(" in ")[0]] + [emitted[-1]]


def test_workers_4_is_equivalent_to_workers_1(
    migrated_db, tree, fake_provider, es_fake, django_user, monkeypatch
):
    """The story's headline AC, over the real composition and real rows.

    Same fixture tree, a WRITING run (`dry_run=False`): the sequential
    run and the 4-worker run must produce identical merged counters,
    identical emitted lines and identical DB rows. The DB and the fakes
    are reset between the halves, because the second run must start from
    the same world the first did.
    """
    # The same double every non-dry test here uses: getCollection needs a
    # Settings row and a live search backend and says nothing about the
    # equivalence.
    monkeypatch.setattr(
        Folder, "getCollection", lambda self, user, dryrun=False: "VX-COLLECTION"
    )

    sequential_result, sequential_emitted = _run(
        _context(fake_provider, dry_run=False, user=django_user, workers=1)
    )
    sequential_rows = _stable_rows()
    assert sequential_rows[0], "the writing run wrote no clips — vacuous"

    # Reset the world: rows, django cache, the Vidispine call logs.
    call_command("flush", interactive=False, verbosity=0)
    cache.clear()
    VidispineFake.reset()
    RestTransportFake.reset()

    pooled_result, pooled_emitted = _run(
        _context(fake_provider, dry_run=False, user=django_user, workers=4)
    )
    pooled_rows = _stable_rows()

    assert _comparable(pooled_result) == _comparable(sequential_result)
    assert _normalized_emission(pooled_emitted) == _normalized_emission(
        sequential_emitted
    )
    assert pooled_rows == sequential_rows


def test_a_pooled_dry_run_writes_nothing_from_any_thread(
    migrated_db, tree, fake_provider, es_fake
):
    """`--dryrun --workers 4`: purity, asserted by the SIGNAL recorder.

    The workers write (or here: must not write) on connections of their
    own; the old thread-local wrapper would have watched none of them.
    ``captured_sql`` installs itself through ``connection_created``, so a
    single worker-thread INSERT would land in ``statements`` and fail the
    whitelist.
    """
    ctx = _context(fake_provider, workers=4)
    before = _stable_rows()

    with captured_sql() as statements:
        run_result, _emitted = _run(ctx)

    # The run really worked (three clips planned) and really queried —
    # otherwise the recorder is watching nothing.
    assert run_result.counters.created == 3
    assert statements
    offending = [s for s in statements if not READ_ONLY_SQL.match(s.lstrip())]
    assert not offending, offending
    assert _stable_rows() == before


def test_workers_above_one_is_rejected_in_paged_mode(migrated_db, tree, fake_provider):
    """AD-14: a paged call scans ONE folder; a pool there is a mode confusion.

    Through the model boundary, so the ValueError arrives as the
    TapelessIngestException the Portal UI/API callers see.
    """
    # No startwith: the four folder filters are policed separately, and
    # this test is about the workers rejection specifically.
    ctx = _context(fake_provider, workers=4, startwith=())
    folder = Folder(storage_id=STORAGE_ID, path=HIT_ONE)

    with pytest.raises(TapelessIngestException, match="workers=4"):
        folder.scan(context=ctx)
    with pytest.raises(TapelessIngestException, match="workers=4"):
        folder.ingest(context=ctx)


def test_workers_1_takes_the_sequential_path_and_builds_no_executor(
    migrated_db, tree, fake_provider, es_fake, monkeypatch
):
    """`workers=1` IS the pre-3.1 code path, not a one-thread pool."""
    from portal.plugins.TapelessIngest.models import folder as folder_module

    def no_pool(*args, **kwargs):
        raise AssertionError("workers=1 constructed a PoolDispatcher")

    monkeypatch.setattr(folder_module, "PoolDispatcher", no_pool)

    run_result, _emitted = _run(_context(fake_provider, workers=1))

    assert run_result.counters.hits == 3

"""Tier 2 (story 4.2): the FR-4 gate, end to end, over a real fixture tree.

`tests/tier2/test_index_discovery_equivalence.py` proves the two paths
agree on ONE tree, by hand, inside a test. This file proves the HARNESS
that turns that into a re-derivable verdict over a corpus: it runs the
real `process_folder`, the real walk, the real providers and both real
discovery paths, and then asserts what the gate says about them.

Three things are pinned here that no tier-1 fake can reach:

* the whole relation over a real tree — the tuples really are read out of
  the post-extraction state, on both paths, and the verdict is
  `accepted`;
* the UNSTABLE-REFERENCE path, driven by the actual defect that motivates
  it: legacy pages an UNSORTED query with `from`/`size`, so a document
  order that shifts between pages makes the reference skip documents and
  disagree with itself. The router below shifts it (`deferred-work.md`
  keeps the real entry, grandfathered by AD-3);
* zero writes, asserted by the SQL recorder over EVERY connection — the
  harness is claimed to be repeatable on production, and that claim is
  worth exactly what the recorder says.

The router is written the same way the story-4.1 equivalence test's is,
and for the same reason: an INDEPENDENT little evaluator of the search
doc `build_search_doc` really produced, honouring `first`/`number` so
legacy's page loop actually runs, and returning matches in a
deliberately non-sorted order so the ordering divergence AD-7 sanctions
stays observable rather than accidental. It is re-stated here rather than
imported so that narrowing one file's fixture cannot silently narrow the
other's.
"""

import fnmatch
import io
import json
import os
import re

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder, process_folder
from portal.plugins.TapelessIngest.providers.red import CARD_SUBPATH_REGEXP
from portal.plugins.TapelessIngest.scan import equivalence
from portal.plugins.TapelessIngest.scan.adapters import build_context
from portal.plugins.TapelessIngest.scan.context import (
    DISCOVERY_INDEX,
    DISCOVERY_LEGACY,
)

from tests.portal_stub import query_elastic_fake
from tests.sql_capture import captured_sql

STORAGE_ID = "VX-41"
ROOT = "2026"

ONE = f"{ROOT}/AH_20260101_one"
TWO = f"{ROOT}/AH_20260102_two"
CARD = f"{ROOT}/AH_20260103_card"
DEEP = f"{ROOT}/AH_20260104_deep"
RED = f"{ROOT}/AH_20260105_red"
SUBPATH = "CONTENTS"

RDM = "A001_1234AB.RDM"
RDC = "A001_C001_1234AB.RDC"
RED_CLIP_DIR = f"{RED}/{RDM}/{RDC}"

FILES = [
    f"{ONE}/CLIPA.fake",
    f"{TWO}/CLIPB.fake",
    f"{TWO}/CLIPC.fake",
    f"{CARD}/{SUBPATH}/CLIPD.fake",
    f"{DEEP}/sub/CLIPE.fake",
    f"{RED_CLIP_DIR}/A001_001.fake",
    # The card's second segment: a HIT on both paths, dropped at assembly
    # as the anchor's extra file — so it is never its own AD-2 tuple.
    f"{RED_CLIP_DIR}/A001_002.fake",
    # Excluded by the extension clause alone.
    f"{ONE}/NOTES.txt",
]

# One tuple per assembled clip: every file but the text one and the
# card's extra segment.
EXPECTED_TUPLES = len(FILES) - 2

# Every statement the gate is allowed to execute. A whitelist, not a
# blacklist of the three verbs we happen to think of (the shape
# `test_dry_run_purity` established).
READ_ONLY_SQL = re.compile(
    r"^(SELECT|SAVEPOINT|RELEASE|ROLLBACK|BEGIN|PRAGMA)\b", re.IGNORECASE
)


def _source(path):
    parent, _, name = path.rpartition("/")
    return {
        "path": path,
        "parent": parent,
        "name": name,
        "hash": f"hash-{path}",
        "storage": STORAGE_ID,
        "id": f"VX-41-{path}",
        "item_type": "file",
        "size": 1024,
    }


def _matches(node, source):
    """A second, simpler evaluator of the clauses `build_search_doc` emits."""
    ((kind, body),) = node.items()
    if kind == "bool":
        must = [_matches(c, source) for c in body.get("must", [])]
        must += [_matches(c, source) for c in body.get("filter", [])]
        should = [_matches(c, source) for c in body.get("should", [])]
        must_not = [_matches(c, source) for c in body.get("must_not", [])]
        if not all(must):
            return False
        if any(must_not):
            return False
        if should and not must and not any(should):
            return False
        return True
    ((field, value),) = body.items()
    actual = source.get(field)
    if actual is None:
        return False
    if kind == "term":
        return actual == value
    if kind == "regexp":
        return re.fullmatch(value, actual) is not None
    if kind == "wildcard":
        return fnmatch.fnmatchcase(actual, value)
    if kind == "prefix":
        return actual.startswith(value)
    raise AssertionError(f"the router cannot evaluate a {kind!r} clause")


def _install_router(es_fake, paths, *, drift=False):
    """One responder for both discovery paths, as one index would be.

    ``drift=True`` makes the UNSORTED order shift by one document per
    legacy query — a faithful, minimal reproduction of what an unsorted
    `from`/`size` stream does when the index is written to underneath it.
    The `search_after` stream is untouched: it sorts, so it cannot drift,
    which is the whole reason AD-3 requires it.
    """
    stream = [
        {"_source": _source(path), "sort": [path, f"VX-41-{path}"]}
        for path in sorted(paths)
    ]
    unsorted_sources = [_source(path) for path in sorted(paths, reverse=True)]
    legacy_calls = [0]

    def respond(search_doc, first, number):
        if "sort" in search_doc:
            cursor = search_doc.get("search_after")
            start = 0
            if cursor is not None:
                start = 1 + next(
                    i for i, hit in enumerate(stream) if hit["sort"] == list(cursor)
                )
            page = stream[start : start + number]
            return {"hits": {"total": {"value": len(stream)}, "hits": page}}
        offset = legacy_calls[0] if drift else 0
        legacy_calls[0] += 1
        rotated = unsorted_sources[offset:] + unsorted_sources[:offset]
        query = search_doc["query"]
        matched = [source for source in rotated if _matches(query, source)]
        page = matched[first : first + number]
        return {
            "hits": {
                "total": {"value": len(matched)},
                "hits": [{"_source": source} for source in page],
            }
        }

    es_fake.route(respond)


def _red_filters(escaped_path):
    """`red.getFilters()`'s exact shape, over the fixture's extension."""
    return [
        {
            "bool": {
                "must": [
                    {
                        "regexp": {
                            "parent": os.path.join(escaped_path, CARD_SUBPATH_REGEXP)
                        }
                    },
                    {"wildcard": {"name": "*.fake"}},
                ]
            }
        },
    ]


@pytest.fixture
def card_provider(fake_provider, monkeypatch):
    """One sub-path AND one red-shaped raw filter, on the shared instance."""
    monkeypatch.setattr(fake_provider, "getSubPaths", lambda: [SUBPATH])
    monkeypatch.setattr(fake_provider, "getFilters", _red_filters)
    monkeypatch.setattr(fake_provider, "getSegmentedExtensions", lambda: [".fake"])
    return fake_provider


@pytest.fixture
def tree(tmp_path, es_fake, storage_fake):
    for relative in FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, FILES)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return tmp_path


@pytest.fixture
def drifting_tree(tmp_path, es_fake, storage_fake):
    """250 files in ONE folder — two and a half legacy pages, and drifting.

    The folder has to span pages for the drift to matter at all: a
    single-page folder returns the same set whatever order the documents
    arrive in. At `result_number = 100` this one is three pages, so a
    one-document shift between pages really does skip a document.
    """
    paths = [f"{ONE}/CLIP{i:03d}.fake" for i in range(250)]
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, paths, drift=True)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return paths


SMALL_ROOT = "2025"
SMALL = f"{SMALL_ROOT}/AH_20250101_small"


@pytest.fixture
def mixed_tree(tmp_path, es_fake, storage_fake):
    """Two scan roots under one drifting index: one paged, one single-page.

    Drift only bites a folder whose query spans PAGES — a single-page
    folder returns the same set whatever order the documents arrive in.
    So `2026` (250 files in one shoot folder, three legacy pages) goes
    unstable while `2025` (two files, one page) stays clean, which is
    exactly the situation the per-folder withholding exists for.
    """
    paths = [f"{ONE}/CLIP{i:03d}.fake" for i in range(250)]
    paths += [f"{SMALL}/CLIPX.fake", f"{SMALL}/CLIPY.fake"]
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, paths, drift=True)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return paths


@pytest.fixture
def two_roots(tmp_path, es_fake, storage_fake):
    """Two independent scan ROOTS, each with one shoot folder below it.

    Corpus entries are containers (`walk_tree` never scans its own root),
    so a multi-entry test needs two of them.
    """
    paths = [f"{ONE}/CLIPA.fake", f"{SMALL}/CLIPX.fake"]
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, paths)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return paths


def _base_context(provider, **options):
    defaults = dict(
        user=None,
        dry_run=True,
        providers=[provider.machine_name],
        # Empty on purpose: hash recovery is post-discovery, identical on
        # both paths, and outside the AD-2 tuple — buying a Vidispine
        # round trip per clip would only slow the gate down.
        legacy_storages=[],
        replace=False,
    )
    defaults.update(options)
    return build_context([STORAGE_ID], **defaults)


def _runner(provider, **options):
    base = _base_context(provider, **options)
    return equivalence.build_path_runner(
        context_for=equivalence.context_factory(base),
        process_folder=process_folder,
        query_elastic=query_elastic_fake,
    )


def _corpus(path=ROOT, note="the fixture tree"):
    return equivalence.load_corpus(
        f"{STORAGE_ID} | {path} | {note}\n", source="fixture-corpus.txt"
    )


# ---------------------------------------------------------------------------
# The gate, over a real tree, on both real paths
# ---------------------------------------------------------------------------


def test_the_harness_accepts_a_tree_the_two_paths_agree_on(
    migrated_db, tree, card_provider
):
    verdict = equivalence.run_equivalence(_corpus(), _runner(card_provider))

    assert verdict.status == equivalence.STATUS_ACCEPTED
    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_AGREED
    assert entry.divergences == ()
    # ...and it is not two empty runs agreeing.
    assert entry.reference.tuple_count == EXPECTED_TUPLES
    assert entry.index.tuple_count == EXPECTED_TUPLES


def test_the_tuples_are_the_ad2_tuples_and_name_the_owning_folder(
    migrated_db, tree, card_provider
):
    """The gate's subject, spelled out on the interesting shapes.

    CLIPD lives in a provider SUB-PATH and the RED segment three levels
    down inside a card; both are owned by the shoot folder that claimed
    them, not by the directory they sit in. Recording the directory would
    make a file claimed by two folders (the NFR-1 duplicate) look like a
    single tuple.
    """
    run_path = _runner(card_provider)
    reference = run_path(_corpus().entries[0], DISCOVERY_LEGACY)
    index = run_path(_corpus().entries[0], DISCOVERY_INDEX)
    assert reference.tuples == index.tuples
    by_file = {values[1]: values for values in reference.tuples}

    assert set(by_file) == {
        f"{ONE}/CLIPA.fake",
        f"{TWO}/CLIPB.fake",
        f"{TWO}/CLIPC.fake",
        f"{CARD}/{SUBPATH}/CLIPD.fake",
        f"{DEEP}/sub/CLIPE.fake",
        f"{RED_CLIP_DIR}/A001_001.fake",
    }
    assert by_file[f"{CARD}/{SUBPATH}/CLIPD.fake"] == (
        STORAGE_ID,
        f"{CARD}/{SUBPATH}/CLIPD.fake",
        f"{CARD}/{SUBPATH}/CLIPD",
        card_provider.machine_name,
        CARD,
    )
    assert by_file[f"{RED_CLIP_DIR}/A001_001.fake"][4] == RED
    assert by_file[f"{DEEP}/sub/CLIPE.fake"][4] == f"{DEEP}/sub"


def test_the_two_paths_are_invoked_over_one_registry_in_one_process(
    migrated_db, tree, card_provider
):
    """AD-2's "one process, same registry", observed rather than asserted.

    The shared provider instance records the `scan_context` of every
    extraction it performs. Three runs happen (legacy, legacy, index) and
    every one of them reached the SAME provider object — so
    `provider_name` cannot have diverged for registry reasons.
    """
    equivalence.run_equivalence(_corpus(), _runner(card_provider))

    seen = card_provider.seen_scan_contexts
    assert len(seen) == 3 * EXPECTED_TUPLES
    assert {ctx.provider_registry for ctx in seen} == {
        next(iter(seen)).provider_registry
    }
    assert card_provider in next(iter(seen)).provider_registry


def test_a_divergence_is_charged_and_the_verdict_is_rejected(
    migrated_db, tree, card_provider, monkeypatch
):
    """The gate must be able to FAIL over a real tree, or it proves nothing.

    A file the index path finds and legacy does not is injected by
    dropping one document from the legacy responder only — the shape a
    real `build_search_doc` regression would take.
    """
    router = query_elastic_fake.responder

    def dropping(search_doc, first, number):
        result = router(search_doc, first=first, number=number)
        if "sort" in search_doc:
            return result
        hits = [
            hit
            for hit in result["hits"]["hits"]
            if hit["_source"]["path"] != f"{TWO}/CLIPC.fake"
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}

    query_elastic_fake.route(dropping)

    verdict = equivalence.run_equivalence(_corpus(), _runner(card_provider))

    assert verdict.status == equivalence.STATUS_REJECTED
    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_DIVERGED
    (divergence,) = entry.divergences
    assert divergence.side == equivalence.SIDE_INDEX_ONLY
    assert divergence.classification == equivalence.CLASS_ABSENT_FROM_LEGACY
    assert divergence.verified_file_path == f"{TWO}/CLIPC.fake"
    assert divergence.owning_folder_path == TWO


def test_a_ratified_waiver_suppresses_that_divergence_and_is_marked_used(
    migrated_db, tree, card_provider
):
    router = query_elastic_fake.responder

    def dropping(search_doc, first, number):
        result = router(search_doc, first=first, number=number)
        if "sort" in search_doc:
            return result
        hits = [
            hit
            for hit in result["hits"]["hits"]
            if hit["_source"]["path"] != f"{TWO}/CLIPC.fake"
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}

    query_elastic_fake.route(dropping)
    waivers = equivalence.load_waivers(
        f"FR-1 | index_only | {TWO} | {TWO}/CLIPC.fake | * | pinned example\n"
    )

    verdict = equivalence.run_equivalence(
        _corpus(), _runner(card_provider), waivers=waivers
    )

    assert verdict.status == equivalence.STATUS_ACCEPTED
    assert verdict.unmatched_waivers == ()
    ((divergence, waiver),) = verdict.entries[0].suppressed
    assert divergence.verified_file_path == f"{TWO}/CLIPC.fake"
    assert waiver.fr == "FR-1"


# ---------------------------------------------------------------------------
# The instrument fault: legacy disagreeing with itself
# ---------------------------------------------------------------------------


def test_a_reference_that_disagrees_with_itself_withholds_the_verdict(
    migrated_db, drifting_tree, card_provider
):
    """Legacy's unsorted `from`/`size` paging, reproduced and caught.

    The two legacy runs page a shifting document order, so each of them
    SKIPS a document — different documents — and they disagree. That is
    an instrument fault, not an index-path defect: the entry must be
    `unstable_reference`, the index path must not be charged, and the
    verdict must be neither `accepted` nor `rejected`.
    """
    verdict = equivalence.run_equivalence(_corpus(), _runner(card_provider))

    assert verdict.status == equivalence.STATUS_UNSTABLE_REFERENCE
    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_UNSTABLE_REFERENCE
    # The reference really did disagree with ITSELF...
    assert entry.reference_self_divergences
    assert {d.side for d in entry.reference_self_divergences} <= {
        equivalence.SIDE_REFERENCE_FIRST_ONLY,
        equivalence.SIDE_REFERENCE_SECOND_ONLY,
    }
    # ...and both of its runs really did lose documents the index found.
    assert entry.index.tuple_count == 250
    assert entry.reference.tuple_count < 250
    assert entry.reference_second.tuple_count < 250
    # NOTHING is charged: the difference lands in the withheld field.
    assert entry.divergences == ()
    assert entry.withheld_divergences
    assert equivalence.SIDE_INDEX_ONLY in {d.side for d in entry.withheld_divergences}
    # The document keeps the two apart by name, which is what stops a
    # reader (or a downstream tool) folding one into the other.
    payload = entry.as_dict()
    assert payload["counts"]["divergences"] == 0
    assert payload["counts"]["withheld"] > 0
    assert payload["counts"]["reference_self_divergences"] > 0


def test_one_unstable_entry_does_not_invalidate_the_rest_of_the_corpus(
    migrated_db, mixed_tree, card_provider
):
    """The withholding is per-FOLDER, as the spec requires.

    Both scan roots are served by the same drifting index. Only the one
    whose folder spans legacy pages can lose a document, so the other
    root's evidence must survive intact — an instrument fault on one
    shoot must not cost the twenty that ran cleanly.
    """
    corpus = equivalence.load_corpus(
        f"{STORAGE_ID} | {ROOT} | paged, so drift bites\n"
        f"{STORAGE_ID} | {SMALL_ROOT} | single page, so it cannot\n",
        source="fixture-corpus.txt",
    )

    verdict = equivalence.run_equivalence(corpus, _runner(card_provider))

    # E1: the clean root's verdict SURVIVES the other's instability.
    assert verdict.status == equivalence.STATUS_ACCEPTED
    drifting, clean = verdict.entries
    assert drifting.status == equivalence.ENTRY_UNSTABLE_REFERENCE
    assert clean.status == equivalence.ENTRY_AGREED
    # The clean root really was scanned, and its verdict stands.
    assert clean.reference.tuple_count == 2
    assert clean.index.tuple_count == 2
    assert verdict.totals()[equivalence.ENTRY_AGREED] == 1
    assert verdict.totals()[equivalence.ENTRY_UNSTABLE_REFERENCE] == 1


def test_an_entry_whose_prefetch_dies_is_errored_and_the_run_continues(
    migrated_db, tree, card_provider, es_fake, tmp_path
):
    """A dead index prefetch is an ERRORED entry, and only that entry."""
    (tmp_path / "2025" / "AH_20250101_other").mkdir(parents=True)
    corpus = equivalence.load_corpus(
        f"{STORAGE_ID} | {ROOT} | fine\n{STORAGE_ID} | 2025 | prefetch dies\n",
        source="fixture-corpus.txt",
    )
    router = query_elastic_fake.responder

    def failing(search_doc, first, number):
        if "2025" in json.dumps(search_doc):
            raise ConnectionResetError("peer closed the connection")
        return router(search_doc, first=first, number=number)

    query_elastic_fake.route(failing)

    verdict = equivalence.run_equivalence(corpus, _runner(card_provider))

    fine, gone = verdict.entries
    assert fine.status == equivalence.ENTRY_AGREED
    assert fine.reference.tuple_count == EXPECTED_TUPLES
    assert gone.status == equivalence.ENTRY_ERRORED
    assert "ConnectionResetError" in gone.error
    # E2: a corpus/environment failure has a status of its own, distinct
    # from a flaky reference — CI reads the exit code and nothing else.
    assert verdict.status == equivalence.STATUS_ERRORED


def test_a_corpus_naming_a_vanished_folder_is_never_agreement(
    migrated_db, tree, card_provider, es_fake
):
    """A1, at the level a human actually meets it.

    Running the command over a corpus naming `2099/gone` used to print
    `verdict: accepted`, exit 0, `agreed 1`, and the line
    `[agreed] ... reference 0 tuple(s), index 0, 0 divergence(s)`. The
    folder had never existed.
    """
    corpus = equivalence.load_corpus(
        f"{STORAGE_ID} | 2099/gone | archived away\n", source="fixture-corpus.txt"
    )

    verdict = equivalence.run_equivalence(corpus, _runner(card_provider))

    assert verdict.status != equivalence.STATUS_ACCEPTED
    assert verdict.status == equivalence.STATUS_ERRORED
    assert verdict.entries[0].status == equivalence.ENTRY_ERRORED
    assert "does not resolve to a directory" in verdict.entries[0].error
    # It never reached the index either: no query was ever sent.
    assert es_fake.calls == []
    # ...and the console SAYS so, which is where the false pass was read.
    rendering = "\n".join(equivalence.render_verdict(verdict))
    assert "[errored]" in rendering
    assert "[agreed]" not in rendering


def test_an_empty_but_real_scan_root_proves_nothing(
    migrated_db, tree, card_provider, tmp_path
):
    """The same false pass through a different door.

    The directory exists, the walk runs, and it finds no folders at all —
    two empty tuple sets that compare equal.
    """
    (tmp_path / "2024").mkdir()
    corpus = equivalence.load_corpus(
        f"{STORAGE_ID} | 2024 | an empty year\n", source="fixture-corpus.txt"
    )

    verdict = equivalence.run_equivalence(corpus, _runner(card_provider))

    assert verdict.status == equivalence.STATUS_ERRORED
    assert "never reached the tuple collector" in verdict.entries[0].error


def test_a_folder_that_failed_during_the_walk_is_never_agreement(
    migrated_db, tree, card_provider, monkeypatch
):
    """A folder that died contributed no tuples on BOTH sides.

    So the two paths agree perfectly about a subtree neither of them saw.
    Driven through the real walk by making one folder's provider raise.
    """
    original_scan_pass = Folder._scan_pass

    def exploding(self, *args, **kwargs):
        if self.path == TWO:
            raise TapelessIngestException("simulated folder failure")
        return original_scan_pass(self, *args, **kwargs)

    monkeypatch.setattr(Folder, "_scan_pass", exploding)

    verdict = equivalence.run_equivalence(_corpus(), _runner(card_provider))

    assert verdict.status == equivalence.STATUS_ERRORED
    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_ERRORED
    assert TWO in entry.error
    assert "neither path saw" in entry.error


def test_a_context_whose_storage_did_not_resolve_is_refused(
    migrated_db, tree, card_provider, storage_fake, es_fake
):
    """`resolve_storages` degrades a bad id to `root_path=None` by design.

    For a production scan that is mercy; for a gate it is a guaranteed
    pair of empty tuple sets reported as agreement.
    """
    storage_fake.reset()

    with pytest.raises(equivalence.EquivalenceError, match="did not resolve"):
        _runner(card_provider)

    assert es_fake.calls == []


# ---------------------------------------------------------------------------
# Read-only, asserted rather than asserted-about
# ---------------------------------------------------------------------------


def test_re_running_the_gate_on_unchanged_data_yields_the_identical_verdict(
    migrated_db, tree, card_provider
):
    """Reproducibility over a REAL tree, not only over fakes.

    Two full gate runs -- six walks, two prefetches, the real providers
    -- must produce the same document once the wall-clock is taken out.
    Anything order-dependent in the tuple extraction, the classification
    or the rendering shows up here and nowhere else.
    """
    first = equivalence.run_equivalence(_corpus(), _runner(card_provider))
    second = equivalence.run_equivalence(_corpus(), _runner(card_provider))

    assert first.as_dict(with_timing=False) == second.as_dict(with_timing=False)
    assert first.status == equivalence.STATUS_ACCEPTED
    assert first.corpus.digest == second.corpus.digest


def test_the_gate_writes_nothing_at_all(migrated_db, tree, card_provider):
    """FR-31's recorder, over every connection, around the whole gate."""
    before = (
        Clip.objects.count(),
        ClipMetadata.objects.count(),
        Folder.objects.count(),
    )

    with captured_sql() as statements:
        verdict = equivalence.run_equivalence(_corpus(), _runner(card_provider))

    assert verdict.status == equivalence.STATUS_ACCEPTED
    # The run really executed against the DB — a gate that made no query
    # at all would pass this vacuously.
    assert statements
    offenders = [sql for sql in statements if not READ_ONLY_SQL.match(sql.strip())]
    assert not offenders, offenders
    assert (
        Clip.objects.count(),
        ClipMetadata.objects.count(),
        Folder.objects.count(),
    ) == before


def test_the_harness_refuses_a_context_that_would_write(
    migrated_db, tree, card_provider
):
    """Structural, not disciplinary: there is no writing gate run."""
    with pytest.raises(equivalence.EquivalenceError, match="dry_run"):
        _runner(card_provider, dry_run=False)


# ---------------------------------------------------------------------------
# The management command
# ---------------------------------------------------------------------------


def _files(tmp_path, corpus_text, waiver_text=""):
    corpus_file = tmp_path / "corpus.txt"
    waiver_file = tmp_path / "waivers.txt"
    corpus_file.write_text(corpus_text, encoding="utf-8")
    waiver_file.write_text(waiver_text, encoding="utf-8")
    return str(corpus_file), str(waiver_file)


def test_the_command_runs_the_gate_and_writes_a_machine_readable_verdict(
    migrated_db, tree, card_provider, tmp_path
):
    corpus_file, waiver_file = _files(
        tmp_path, f"{STORAGE_ID} | {ROOT} | the fixture tree\n"
    )
    out = tmp_path / "verdict.json"
    stdout = io.StringIO()

    call_command(
        "verify_discovery_equivalence",
        "--corpus",
        corpus_file,
        "--waivers",
        waiver_file,
        "--out",
        str(out),
        "--providers",
        card_provider.machine_name,
        stdout=stdout,
    )

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["verdict"] == equivalence.STATUS_ACCEPTED
    assert document["relation"]["tuple_fields"] == list(equivalence.AD2_FIELDS)
    assert document["corpus"]["entries"][0]["path"] == ROOT
    assert document["entries"][0]["counts"]["reference_tuples"] == EXPECTED_TUPLES
    # Both paths' versions travel with the verdict, so an old verdict
    # cannot be read as covering changed code.
    # C3: what this run was NARROWED to. A one-provider run must not
    # produce a document indistinguishable from a full-registry one.
    scope = document["scope"]
    assert scope["providers"] == [card_provider.machine_name]
    assert scope["discovery_page_size"] == 500
    assert scope["storage_roots"][STORAGE_ID]
    assert (scope["walk"], scope["shape"]) == ("sequential", "scan")
    versions = document["discovery_versions"]
    assert set(versions) == {"legacy", "index", "shared", "providers"}
    # C2: each digest travels with the sources it CLAIMS to cover, and
    # "legacy" covers the from/size page loop, not just build_search_doc.
    assert "portal.plugins.TapelessIngest.models.folder" in versions["legacy"]["covers"]
    assert versions["index"]["digest"].startswith("sha256:")
    assert (
        "portal.plugins.TapelessIngest.scan.extraction" in versions["shared"]["covers"]
    )
    # The fixture provider is a test double with no module of its own, so
    # E7's degradation is exercised here rather than hypothesised.
    assert versions["providers"]["digest"].startswith(equivalence.SOURCE_UNAVAILABLE)
    assert "verdict: accepted" in stdout.getvalue()


def test_the_command_refuses_an_unparseable_corpus_before_touching_anything(
    migrated_db, tree, card_provider, tmp_path, es_fake
):
    corpus_file, waiver_file = _files(tmp_path, "# nothing here\n")

    with pytest.raises(CommandError, match="holds no entries"):
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )

    assert es_fake.calls == []


def test_the_command_refuses_a_waiver_that_cites_no_fr(
    migrated_db, tree, card_provider, tmp_path, es_fake
):
    corpus_file, waiver_file = _files(
        tmp_path,
        f"{STORAGE_ID} | {ROOT} | n\n",
        waiver_text="| index_only | * | * | * | silently silencing a defect\n",
    )

    with pytest.raises(CommandError, match="is not an FR citation"):
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )

    assert es_fake.calls == []


def test_the_command_fails_the_build_when_the_gate_is_rejected(
    migrated_db, tree, card_provider, tmp_path
):
    """Cron and CI read the exit status and nothing else."""
    router = query_elastic_fake.responder

    def dropping(search_doc, first, number):
        result = router(search_doc, first=first, number=number)
        if "sort" in search_doc:
            return result
        hits = [
            hit
            for hit in result["hits"]["hits"]
            if hit["_source"]["path"] != f"{TWO}/CLIPC.fake"
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}

    query_elastic_fake.route(dropping)
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    out = tmp_path / "verdict.json"

    with pytest.raises(CommandError, match="REJECTED"):
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--out",
            str(out),
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )

    # The evidence is written BEFORE the failure: a rejected gate whose
    # verdict never landed is a build that fails with nothing to read.
    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == (
        equivalence.STATUS_REJECTED
    )


def test_two_command_runs_over_unchanged_data_write_identical_verdicts(
    migrated_db, tree, card_provider, tmp_path
):
    """C1, proven the way it was disproven: by diffing two --out files.

    The first cut had two reproducibility tests and both compared
    `as_dict(with_timing=False)` — a form no shipped path emitted. The
    document the command actually wrote carried `elapsed_seconds`, so
    every pair of runs differed.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    first_out = tmp_path / "first.json"
    second_out = tmp_path / "second.json"

    for out in (first_out, second_out):
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--out",
            str(out),
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )

    assert first_out.read_text(encoding="utf-8") == second_out.read_text(
        encoding="utf-8"
    )
    assert "elapsed_seconds" not in json.loads(first_out.read_text(encoding="utf-8"))


def test_with_timings_is_opt_in_and_makes_the_document_non_reproducible(
    migrated_db, tree, card_provider, tmp_path
):
    """The escape hatch exists, and it is honest about what it costs."""
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    out = tmp_path / "timed.json"

    call_command(
        "verify_discovery_equivalence",
        "--corpus",
        corpus_file,
        "--waivers",
        waiver_file,
        "--out",
        str(out),
        "--with-timings",
        "--providers",
        card_provider.machine_name,
        stdout=io.StringIO(),
    )

    document = json.loads(out.read_text(encoding="utf-8"))
    assert "elapsed_seconds" in document
    assert document["started_at"]


def test_the_command_reports_progress_per_entry_and_warns_about_ratification(
    migrated_db, two_roots, card_provider, tmp_path
):
    """E4 and F1, on the console a human actually watches."""
    corpus_file, waiver_file = _files(
        tmp_path,
        f"{STORAGE_ID} | {ROOT} | one\n{STORAGE_ID} | {SMALL_ROOT} | two\n",
    )
    stdout = io.StringIO()

    call_command(
        "verify_discovery_equivalence",
        "--corpus",
        corpus_file,
        "--waivers",
        waiver_file,
        "--providers",
        card_provider.machine_name,
        stdout=stdout,
    )

    output = stdout.getvalue()
    assert "[1/2] VX-41 2026: agreed" in output
    assert "[2/2] VX-41 2025: agreed" in output
    # F1: the shipped default is unratified and so is this ad-hoc file.
    assert "NOT RATIFIED" in output
    assert "started " in output and "elapsed " in output


def test_the_four_gate_outcomes_carry_four_distinct_exit_codes(
    migrated_db, tree, card_provider, tmp_path
):
    """E2: CI reads the exit code and nothing else.

    A corpus typo, a flaky reference and a real divergence must not look
    alike there.
    """
    from portal.plugins.TapelessIngest.management.commands import (
        verify_discovery_equivalence as command_module,
    )

    assert (
        len(
            {
                command_module.EXIT_ACCEPTED,
                command_module.EXIT_REJECTED,
                command_module.EXIT_WITHHELD,
                command_module.EXIT_ERRORED,
                command_module.EXIT_USAGE,
            }
        )
        == 5
    )

    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | 2099/gone | n\n")
    with pytest.raises(CommandError) as excinfo:
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )

    assert "could not RUN" in str(excinfo.value)
    assert getattr(excinfo.value, "returncode", None) == command_module.EXIT_ERRORED


def test_a_defective_page_size_fails_at_parse_time_before_any_storage_call(
    migrated_db, tree, card_provider, tmp_path, storage_fake
):
    """E6: AD-10's fail-fast, and it has to be at PARSE time.

    Deferring to `RunOptions.__post_init__` means the defect surfaces
    only after `build_context` has spent a live `getStorage` per id.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    before = storage_fake.get_storage_calls

    with pytest.raises(CommandError, match="discovery-page-size"):
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--discovery-page-size",
            "99999999",
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )

    assert storage_fake.get_storage_calls == before

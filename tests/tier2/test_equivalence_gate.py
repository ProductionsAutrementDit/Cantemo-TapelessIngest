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
from portal.plugins.TapelessIngest.management.commands import (
    verify_discovery_equivalence,
)
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
    # E2: a corpus/environment failure keeps a status of its own, distinct
    # from a flaky reference — CI reads the exit code and nothing else.
    # RULED 2026-09-04: it withdraws its own entry without sinking a
    # corpus whose other entry concluded.
    assert verdict.status == equivalence.STATUS_ACCEPTED
    assert verdict.totals()[equivalence.ENTRY_ERRORED] == 1


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


def _files(tmp_path, corpus_text, waiver_text="", ratified=True):
    """Corpus + waiver files for a `call_command` test.

    Ratified by DEFAULT. An accepted run over an unratified corpus exits
    `EXIT_WITHHELD` (RULED 2026-09-04: the exit code is the whole
    contract, so a rehearsal must not be able to green CI), and every
    test here except the ratification one is about the gate's mechanics
    rather than about that rule.
    """
    corpus_file = tmp_path / "corpus.txt"
    waiver_file = tmp_path / "waivers.txt"
    if ratified:
        corpus_text = "#! ratified: yes\n" + corpus_text
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
    assert set(versions) == {"legacy", "index", "shared", "providers", "instrument"}
    # C2: each digest travels with the sources it CLAIMS to cover, and
    # "legacy" covers the from/size page loop, not just build_search_doc.
    assert "portal.plugins.TapelessIngest.models.folder" in versions["legacy"]["covers"]
    assert versions["index"]["digest"].startswith("sha256:")
    assert (
        "portal.plugins.TapelessIngest.scan.extraction" in versions["shared"]["covers"]
    )
    # Which providers get instantiated and which storage root the
    # verification runs against both decide tuple fields, and both used
    # to be outside every digest.
    for label in (
        "portal.plugins.TapelessIngest.scan.adapters",
        "portal.plugins.TapelessIngest.scan.context",
    ):
        assert label in versions["shared"]["covers"]
    # The harness itself. A change to the comparison logic left no trace
    # in the verdict at all.
    assert versions["instrument"]["covers"] == [
        "portal.plugins.TapelessIngest.scan.equivalence"
    ]
    # The claim must be PINNED, not merely stated: a group that silently
    # failed to read a source it names carries a digest over less than it
    # claims, which the module calls worse than no digest.
    for name in ("legacy", "index", "shared", "instrument"):
        assert versions[name]["digest"].startswith("sha256:"), name
        assert "unavailable" not in versions[name], name
    # The providers group still digests the base class and the registry
    # order, both of which decide provider_name and umid — so it is a
    # real digest even though the FIXTURE provider is a double with no
    # module of its own. That degradation is named, not swallowed.
    assert versions["providers"]["digest"].startswith("sha256:")
    for label in (
        "portal.plugins.TapelessIngest.providers",
        "portal.plugins.TapelessIngest.providers.providers",
    ):
        assert label in versions["providers"]["covers"]
    assert any(
        card_provider.machine_name in entry
        for entry in versions["providers"]["unavailable"]
    )
    output = stdout.getvalue()
    assert "verdict: accepted" in output
    # And a partial read is visible to the human reading the console,
    # not only to a reader of the JSON.
    assert "covers LESS than it claims" in output


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


def test_an_unratified_run_reports_progress_and_is_withheld_not_accepted(
    migrated_db, two_roots, card_provider, tmp_path
):
    """E4 and F1, on the console a human actually watches."""
    corpus_file, waiver_file = _files(
        tmp_path,
        f"{STORAGE_ID} | {ROOT} | one\n{STORAGE_ID} | {SMALL_ROOT} | two\n",
        ratified=False,
    )
    stdout = io.StringIO()

    # RULED 2026-09-04: the entries agree, but the corpus is not
    # ratified, so the run is WITHHELD. A console warning nobody's CI
    # reads was the only thing standing between a rehearsal and a green
    # gate — and the exit code is the whole contract.
    with pytest.raises(CommandError) as excinfo:
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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_WITHHELD
    )
    assert "NOT RATIFIED" in str(excinfo.value)
    output = stdout.getvalue()
    # E4: progress lands per ENTRY and from inside the walk, so a
    # three-walk pass over a shoot tree is not silent.
    assert "[1/2] VX-41 2026: agreed" in output
    assert "[2/2] VX-41 2025: agreed" in output
    assert "VX-41 2026 [legacy]: starting" in output
    assert "VX-41 2026 [index]: starting" in output
    # F1: the warning is still said out loud, before the run and again in
    # the rendering.
    assert "NOT RATIFIED" in output
    assert "started " in output and "elapsed " in output


def test_the_five_exit_codes_are_distinct_and_an_errored_run_carries_its_own(
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


# ---------------------------------------------------------------------------
# The exit code IS the contract (code review 2026-09-04)
# ---------------------------------------------------------------------------


def test_a_wholly_unstable_run_reaches_ci_as_withheld_not_as_a_rejection(
    migrated_db, drifting_tree, card_provider, tmp_path
):
    """The command's WITHHELD branch had no test at all.

    Both tests that produced `unstable_reference` called
    `run_equivalence` directly, so nothing exercised the layer CI reads.
    Giving that branch `EXIT_REJECTED` — or deleting it, which drops the
    run to exit 0 — passed the whole suite.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_WITHHELD
    )
    assert "WITHHELD" in str(excinfo.value)
    assert "nothing was proven" in str(excinfo.value)


def test_a_run_whose_other_entry_concluded_is_accepted_and_says_so(
    migrated_db, mixed_tree, card_provider, tmp_path
):
    """RULED 2026-09-04: partial acceptance is STATED, not inferred."""
    corpus_file, waiver_file = _files(
        tmp_path,
        f"{STORAGE_ID} | {ROOT} | paged\n{STORAGE_ID} | {SMALL_ROOT} | single page\n",
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
    assert "verdict: accepted" in output
    assert "accepted over 1 of 2 entr(ies)" in output
    assert "withheld for an unstable legacy reference" in output


def test_a_rejected_gate_carries_the_rejected_code_not_just_the_word(
    migrated_db, tree, card_provider, tmp_path
):
    """The rejected and usage paths were matched by MESSAGE text only.

    Passing `EXIT_ERRORED` at the rejected site, or dropping `returncode`
    entirely, left every assertion green while CI read a real divergence
    as an environment fault.
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
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_REJECTED
    )


def test_a_corpus_defect_carries_the_usage_code(
    migrated_db, tree, card_provider, tmp_path
):
    """A corpus typo and a discovery divergence must not look alike."""
    corpus_file, waiver_file = _files(tmp_path, "# nothing here\n")

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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_USAGE
    )


@pytest.mark.parametrize("page_size", ["0", "-2", "many"])
def test_every_defective_page_size_fails_at_parse_time(
    migrated_db, tree, card_provider, tmp_path, storage_fake, page_size
):
    """This command's THIRD copy of the bound was tested at one end only.

    Only `99999999` was covered, so dropping the `< 1` branch from this
    copy left the twins' tests green while `--discovery-page-size 0`
    reached `build_context`, spent a live `getStorage` per storage id,
    and died in `RunOptions.__post_init__` as an uncaught `ValueError`.
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
            page_size,
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )

    assert storage_fake.get_storage_calls == before


def test_a_verdict_file_that_cannot_be_written_fails_the_run(
    migrated_db, tree, card_provider, tmp_path
):
    """RULED 2026-09-04: a requested artifact that was not produced fails.

    The evidence still reaches stdout first, so the run's findings are
    not lost — but CI, which reads the exit code and nothing else, must
    not record a green gate with no verdict file behind it.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    unwritable = tmp_path / "no-such-directory" / "verdict.json"
    stdout = io.StringIO()

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--out",
            str(unwritable),
            "--providers",
            card_provider.machine_name,
            stdout=stdout,
        )

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_USAGE
    )
    assert "cannot write the verdict" in str(excinfo.value)
    # The findings survived the failure.
    assert "verdict: accepted" in stdout.getvalue()


def test_the_verdict_file_is_replaced_atomically(
    migrated_db, tree, card_provider, tmp_path
):
    """A truncating write leaves a short document that parses as complete."""
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    out = tmp_path / "verdict.json"
    out.write_text("PREVIOUS", encoding="utf-8")

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

    assert json.loads(out.read_text(encoding="utf-8"))["verdict"] == "accepted"
    # No temp file left behind next to it.
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


def test_an_empty_out_path_is_refused_rather_than_skipped(
    migrated_db, tree, card_provider, tmp_path
):
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

    with pytest.raises(CommandError, match="--out cannot be empty"):
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--out",
            "",
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )


def test_a_repeated_provider_name_does_not_change_the_documents_identity(
    migrated_db, tree, card_provider, tmp_path
):
    """`build_provider_registry` de-duplicates; `scope` and the digest did not.

    Two runs that proved the same thing over the same effective registry
    wrote non-comparable documents.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    once = tmp_path / "once.json"
    twice = tmp_path / "twice.json"

    for out, providers in (
        (once, [card_provider.machine_name]),
        (
            twice,
            [
                card_provider.machine_name,
                card_provider.machine_name,
            ],
        ),
    ):
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--out",
            str(out),
            "--providers",
            *providers,
            stdout=io.StringIO(),
        )

    assert once.read_text(encoding="utf-8") == twice.read_text(encoding="utf-8")


def test_an_index_that_prefetched_nothing_is_an_instrument_fault(
    migrated_db, tree, card_provider, tmp_path
):
    """An index outage would otherwise REJECT the gate.

    Zero hits over a tree that demonstrably exists on disk turns every
    legacy tuple into an `absent_from_index` divergence — the gate
    failing for a reason that says nothing about discovery.
    """
    query_elastic_fake.route(
        lambda search_doc, first, number: {"hits": {"total": {"value": 0}, "hits": []}}
    )
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_ERRORED
    )
    assert "could not RUN" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Round 2: what the round-1 suite let through (2026-09-04)
#
# Each of these was written against a mutation that survived the full
# suite. A test that stays green when its subject is broken is not a pin.
# ---------------------------------------------------------------------------


def _dropping_router(missing):
    """A legacy responder that loses one document; the index keeps it.

    Copy-pasted four times before this; the sorted (`"sort" in
    search_doc`) branch is the INDEX query and must pass through
    untouched, which is what makes the lost document an `index_only`
    divergence rather than a mutual absence.
    """
    router = query_elastic_fake.responder

    def dropping(search_doc, first, number):
        result = router(search_doc, first=first, number=number)
        if "sort" in search_doc:
            return result
        hits = [
            hit for hit in result["hits"]["hits"] if hit["_source"]["path"] != missing
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}

    return dropping


def test_the_command_actually_applies_the_waiver_file_it_loaded(
    migrated_db, tree, card_provider, tmp_path
):
    """Dropping `waivers=waivers` from the command left 1283 green.

    Every `call_command` test passed an EMPTY waiver file, and the one
    test with a non-empty file died inside `load_waivers` before reaching
    the relation. So the file was read, validated, and written into the
    document — and suppressed nothing. A human ratifies a waiver for a
    known Feature-G difference, the gate keeps exiting 1, and the verdict
    additionally reports that waiver as having matched nothing.
    """
    query_elastic_fake.route(_dropping_router(f"{TWO}/CLIPC.fake"))
    corpus_file, waiver_file = _files(
        tmp_path,
        f"{STORAGE_ID} | {ROOT} | n\n",
        waiver_text=(
            f"FR-1 | index_only | {TWO} | {TWO}/CLIPC.fake | * | the known delta\n"
        ),
    )
    out = tmp_path / "verdict.json"
    stdout = io.StringIO()

    # No CommandError: the waiver turns a rejection into an acceptance.
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
    assert document["verdict"] == "accepted"
    assert document["totals"]["suppressed"] == 1
    assert document["unmatched_waivers"] == []
    ((suppressed,),) = [
        entry["suppressed"] for entry in document["entries"] if entry["suppressed"]
    ]
    assert suppressed["divergence"]["tuple"]["verified_file_path"] == (
        f"{TWO}/CLIPC.fake"
    )
    assert suppressed["waiver"]["fr"] == "FR-1"
    # And the console names what was silenced and by which line.
    assert "suppressed by waiver:" in stdout.getvalue()


def test_a_failed_replace_leaves_the_previous_verdict_intact_and_no_temp_file(
    migrated_db, tree, card_provider, tmp_path, monkeypatch
):
    """The round-1 atomicity test could not detect its own absence.

    Replacing `_write_atomically` with a plain truncating `open(path,
    "w")` left 1283 green: that test only overwrote a file and parsed the
    result, so the defect its docstring names — "a short document that
    parses as complete" — was unreachable. Failing the REPLACE is what
    separates the two implementations: a truncating write has already
    destroyed the previous file by then.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    out = tmp_path / "verdict.json"
    out.write_text("PREVIOUS EVIDENCE", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(verify_discovery_equivalence.os, "replace", boom, raising=True)

    with pytest.raises(CommandError) as excinfo:
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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_USAGE
    )
    # A truncating write would have destroyed this before failing.
    assert out.read_text(encoding="utf-8") == "PREVIOUS EVIDENCE"
    # And the cleanup branch ran, so no half-written temp file is left
    # sitting beside the verdict looking like one.
    assert list(tmp_path.glob("*.tmp")) == []


STABLE_SHOOT = f"{ROOT}/AH_20260106_stable"


@pytest.fixture
def drifting_and_stable_tree(tmp_path, es_fake, storage_fake):
    """ONE scan root holding a drifting folder and a stable one.

    `drifting_tree` puts all 250 files in a single folder, so its entry
    has no folder left to conclude with and the whole entry goes
    unstable. Per-folder withholding is only observable where a root
    holds both: the 250-file shoot spans three legacy pages and drifts;
    the two-file shoot is one page and returns the same set whatever
    order the documents arrive in.
    """
    paths = [f"{ONE}/CLIP{i:03d}.fake" for i in range(250)]
    paths += [f"{STABLE_SHOOT}/CLIPX.fake", f"{STABLE_SHOOT}/CLIPY.fake"]
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, paths, drift=True)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return paths


def test_a_folder_withheld_inside_a_concluding_entry_is_said_out_loud(
    migrated_db, drifting_and_stable_tree, card_provider, tmp_path
):
    """Partial acceptance was stated for one of its three triggers.

    Dropping `or totals["withheld_folders"]` from the condition left the
    suite green, so the steady state the module predicts — legacy pages
    at 100 hits, prod folders far above that — produced a clean-looking
    green gate silently covering less of the corpus than it claimed.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
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
    assert document["verdict"] == "accepted"
    # The drifting shoot is withheld; the stable one concluded.
    assert document["totals"]["withheld_folders"] >= 1
    assert document["entries"][0]["status"] == "agreed"
    output = stdout.getvalue()
    assert "folder(s) withheld inside entries that otherwise concluded" in output


def test_an_errored_entry_beside_an_accepted_one_is_stated_at_the_command(
    migrated_db, tree, card_provider, tmp_path
):
    """The other untested trigger of the partial-acceptance line."""
    corpus_file, waiver_file = _files(
        tmp_path,
        f"{STORAGE_ID} | {ROOT} | fine\n{STORAGE_ID} | 2099/gone | archived\n",
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
    assert "accepted over 1 of 2 entr(ies)" in output
    assert "1 errored" in output


def test_the_page_size_flag_reaches_the_index_prefetch(
    migrated_db, tree, card_provider, tmp_path, monkeypatch
):
    """`scope` reported the requested size without the run using it.

    The assertion read the number back out of the same `options` dict
    that produced it, so deleting `discovery_page_size=` from the
    `build_context` call left the index paging at the default while the
    verdict kept claiming the requested value — and paging is the exact
    mechanism `unstable_reference` exists for.
    """
    seen = []
    real = equivalence.prefetch_index

    def recording(query_elastic, storage_id, path, *, page_size):
        seen.append(page_size)
        return real(query_elastic, storage_id, path, page_size=page_size)

    monkeypatch.setattr(equivalence, "prefetch_index", recording)
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

    call_command(
        "verify_discovery_equivalence",
        "--corpus",
        corpus_file,
        "--waivers",
        waiver_file,
        "--discovery-page-size",
        "50",
        "--providers",
        card_provider.machine_name,
        stdout=io.StringIO(),
    )

    assert seen == [50]


def test_progress_is_emitted_from_inside_the_walk(
    migrated_db, tree, card_provider, tmp_path, monkeypatch
):
    """`PROGRESS_EVERY` had never fired: no fixture reaches 100 folders.

    So the in-walk hook could be deleted outright — and the shipped
    single-entry corpus would run three walks over 8,133 prod folders in
    total silence, which is the slow-versus-hung ambiguity it was added
    to remove. Lowering the cadence is the honest way to exercise it
    without a 100-folder fixture.
    """
    monkeypatch.setattr(equivalence, "PROGRESS_EVERY", 2)
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
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

    assert "folders walked" in stdout.getvalue()


def test_the_unratified_warning_lands_before_the_walk_not_only_after_it(
    migrated_db, tree, card_provider, tmp_path
):
    """Deleting the pre-run warning left the suite green.

    `render_verdict` emits the same phrase at the END, so `"NOT RATIFIED"
    in output` was satisfied either way — and the operator only learned
    the run was a rehearsal after waiting for it.
    """
    corpus_file, waiver_file = _files(
        tmp_path, f"{STORAGE_ID} | {ROOT} | n\n", ratified=False
    )
    stdout = io.StringIO()

    with pytest.raises(CommandError):
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
    assert output.index("NOT RATIFIED") < output.index("[legacy]: starting")


def test_an_unreadable_corpus_path_is_an_operator_error_not_a_rejection(
    migrated_db, tree, card_provider, tmp_path
):
    """`load_corpus_file`'s OSError wrapper had no test.

    A leaked OSError exits 1, which is `EXIT_REJECTED` — CI would read a
    typo'd path as a discovery divergence.
    """
    _, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            str(tmp_path / "no-such-corpus.txt"),
            "--waivers",
            waiver_file,
            "--providers",
            card_provider.machine_name,
            stdout=io.StringIO(),
        )

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_USAGE
    )
    assert "cannot read the corpus" in str(excinfo.value)


def test_an_unknown_provider_name_is_an_operator_error(
    migrated_db, tree, card_provider, tmp_path
):
    """`_build_registry_and_map` returns (None, None) for a bad name.

    This lands on `context_factory`'s no-registry refusal, NOT on the
    `build_context` guard — established by mutation: removing that guard
    leaves this test green. The guard has its own test below.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

    with pytest.raises(CommandError) as excinfo:
        call_command(
            "verify_discovery_equivalence",
            "--corpus",
            corpus_file,
            "--waivers",
            waiver_file,
            "--providers",
            "no_such_provider",
            stdout=io.StringIO(),
        )

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_USAGE
    )


def test_a_context_that_cannot_be_built_at_all_is_an_operator_error(
    migrated_db, tree, card_provider, tmp_path, monkeypatch
):
    """The round-1 `build_context` guard, pinned at last.

    Removing it left 1311 green: the unknown-provider test above reaches
    `context_factory` instead, and nothing in the fixture makes
    `build_context` itself raise. Without the guard the exception leaves
    a traceback and Django's default exit 1 — which is `EXIT_REJECTED`,
    i.e. CI reading an unreachable storage as a discovery divergence.
    """

    def exploding(*args, **kwargs):
        raise RuntimeError("getStorage timed out")

    monkeypatch.setattr(
        verify_discovery_equivalence.adapters, "build_context", exploding
    )
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_USAGE
    )
    assert "cannot build the run context" in str(excinfo.value)
    assert "getStorage timed out" in str(excinfo.value)


def test_a_waiver_defect_carries_the_usage_code_too(
    migrated_db, tree, card_provider, tmp_path
):
    """Parse-time refusals asserted the message and never the code.

    argparse's own exit status is 2, which equals `EXIT_WITHHELD`, so an
    operator error reaching CI as "the gate was withheld" is one
    unasserted branch away.
    """
    corpus_file, waiver_file = _files(
        tmp_path,
        f"{STORAGE_ID} | {ROOT} | n\n",
        waiver_text="nope | index_only | a | b | c | d\n",
    )

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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_USAGE
    )


def test_the_accepted_exit_code_is_actually_zero():
    """Only distinctness was asserted; the numeric contract was not.

    The Tasks section states `exit 0/1/2 = accepted/rejected/withheld`,
    and every other test compares against the symbol.
    """
    assert verify_discovery_equivalence.EXIT_ACCEPTED == 0
    assert verify_discovery_equivalence.EXIT_REJECTED == 1
    assert verify_discovery_equivalence.EXIT_WITHHELD == 2


def test_an_older_django_degrades_the_exit_code_loudly(monkeypatch):
    """The `TypeError` fallback exists for the Portal the server runs.

    It is the branch no test executes, on the deployment that is not the
    dev environment. If it ever fires, the four-code contract is off and
    that has to be visible rather than silent.
    """

    class OldCommandError(Exception):
        def __init__(self, message):
            super().__init__(message)

    monkeypatch.setattr(
        verify_discovery_equivalence, "CommandError", OldCommandError, raising=True
    )
    error = verify_discovery_equivalence._command_error(
        "the corpus holds no entries",
        verify_discovery_equivalence.EXIT_USAGE,
    )

    assert isinstance(error, OldCommandError)
    assert "[exit 4]" in str(error)


def test_the_gate_writes_nothing_through_the_command_either(
    migrated_db, tree, card_provider, tmp_path
):
    """The zero-writes recorder only ever wrapped `run_equivalence`.

    The Verification section's manual check runs the COMMAND, which also
    builds the context, resolves the provider registry, writes `--out`
    and renders — all outside the recorder's previous scope.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    out = tmp_path / "verdict.json"

    with captured_sql() as statements:
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

    # A command that made no query at all would pass this vacuously.
    assert statements
    offenders = [sql for sql in statements if not READ_ONLY_SQL.match(sql.strip())]
    assert not offenders, offenders
    assert (Clip.objects.count(), ClipMetadata.objects.count()) == (0, 0)


def test_an_index_that_lost_a_file_is_charged_to_the_index(
    migrated_db, tree, card_provider, tmp_path
):
    """The direction FR-4 exists for, proven on the real tree at last.

    Every real-tree injection filtered the LEGACY responder, so every
    end-to-end divergence was `index_only`/`absent_from_legacy`. The
    dangerous direction is the other one: index discovery LOSING media,
    which is what makes flipping the default unsafe.
    """
    router = query_elastic_fake.responder
    missing = f"{TWO}/CLIPC.fake"

    def losing(search_doc, first, number):
        result = router(search_doc, first=first, number=number)
        if "sort" not in search_doc:
            return result
        hits = [
            hit for hit in result["hits"]["hits"] if hit["_source"]["path"] != missing
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}

    query_elastic_fake.route(losing)

    verdict = equivalence.run_equivalence(_corpus(), _runner(card_provider))

    assert verdict.status == equivalence.STATUS_REJECTED
    (divergence,) = verdict.entries[0].divergences
    assert divergence.side == equivalence.SIDE_LEGACY_ONLY
    assert divergence.classification == equivalence.CLASS_ABSENT_FROM_INDEX
    assert divergence.verified_file_path == missing


def test_an_unrecognised_verdict_status_never_reaches_ci_as_a_pass(
    migrated_db, tree, card_provider, tmp_path, monkeypatch
):
    """The fifth-status guard exists so a later status cannot exit 0.

    Four statuses today. The ladder used to fall through to a silent
    return — a status added later would have read as `accepted` to the
    only consumer that matters.
    """
    real = equivalence.run_equivalence

    def with_new_status(*args, **kwargs):
        verdict = real(*args, **kwargs)
        return equivalence.dataclass_replace(verdict, status="inconclusive")

    monkeypatch.setattr(equivalence, "run_equivalence", with_new_status)
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")

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

    assert getattr(excinfo.value, "returncode", None) == (
        verify_discovery_equivalence.EXIT_USAGE
    )
    assert "unknown verdict status" in str(excinfo.value)


def test_a_descent_divergence_is_produced_by_the_real_walk(
    migrated_db, tree, card_provider
):
    """`consumed_subdirs` drift, end to end at last.

    Folder-set divergence is the NFR-1 duplicate-ingest direction and the
    waiver file calls it non-waivable — "a fix, not a signature" — yet it
    was only ever produced by hand-built `PathRun`s. Losing the file
    inside the provider's sub-path from the INDEX makes that path stop
    consuming `CONTENTS` and descend into it as an ordinary folder, while
    legacy still claims it. The two paths then walk different folder
    sets, which is exactly the shape a duplicate ingest would take.
    """
    router = query_elastic_fake.responder
    missing = f"{CARD}/{SUBPATH}/CLIPD.fake"

    def losing(search_doc, first, number):
        result = router(search_doc, first=first, number=number)
        if "sort" not in search_doc:
            return result
        hits = [
            hit for hit in result["hits"]["hits"] if hit["_source"]["path"] != missing
        ]
        return {"hits": {"total": {"value": len(hits)}, "hits": hits}}

    query_elastic_fake.route(losing)

    verdict = equivalence.run_equivalence(_corpus(), _runner(card_provider))

    entry = verdict.entries[0]
    assert verdict.status == equivalence.STATUS_REJECTED
    assert entry.folder_divergences, "the real walk produced no descent divergence"
    walked_once = {(d.side, d.folder_path) for d in entry.folder_divergences}
    assert (equivalence.SIDE_INDEX_ONLY, f"{CARD}/{SUBPATH}") in walked_once


class _RecordingStream:
    """A stdout double that records writes AND flushes, in order.

    Django wraps whatever `call_command(stdout=...)` is given in an
    `OutputWrapper`, whose `flush()` delegates here -- so this observes
    the real interleaving the operator's terminal or log file would see.
    """

    def __init__(self):
        self.events = []

    def write(self, text):
        self.events.append(("write", text))

    def flush(self):
        self.events.append(("flush",))

    def isatty(self):
        return False

    def getvalue(self):
        return "".join(
            t for kind, *rest in self.events for t in rest if kind == "write"
        )


def test_progress_is_flushed_as_it_is_produced_not_at_the_end(
    migrated_db, tree, card_provider, tmp_path
):
    """E4's hook is useless unless it reaches the reader while it runs.

    Python block-buffers a non-tty stdout, so a redirected run -- which
    is every unattended one, including cron -- shows nothing until 8KB
    has accumulated or the process exits. Measured on production
    2026-09-08: 130 bytes of log after 11 minutes of a 22-minute run,
    and all 130 were Django's own stderr. The progress hook exists
    precisely for the long unattended run and was inert in exactly that
    case.
    """
    corpus_file, waiver_file = _files(tmp_path, f"{STORAGE_ID} | {ROOT} | n\n")
    stream = _RecordingStream()

    call_command(
        "verify_discovery_equivalence",
        "--corpus",
        corpus_file,
        "--waivers",
        waiver_file,
        "--providers",
        card_provider.machine_name,
        stdout=stream,
    )

    kinds = [e[0] for e in stream.events]
    first_progress = next(
        i for i, e in enumerate(stream.events) if e[0] == "write" and "starting" in e[1]
    )
    assert "flush" in kinds[first_progress:], "no flush at all after a progress line"
    next_flush = kinds.index("flush", first_progress)
    later_writes = [
        i for i, k in enumerate(kinds) if k == "write" and i > first_progress
    ]
    assert not later_writes or next_flush < later_writes[-1], (
        "the stream was only flushed at the very end: a redirected run "
        "stays silent for its whole duration, which is what the hook exists "
        "to prevent"
    )

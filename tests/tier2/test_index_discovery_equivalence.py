"""Tier 2 (story 4.1): `--discovery=index` and `--discovery=legacy` agree.

The composition test the switch exists for (AD-2): the SAME real tree, the
SAME real walk, the SAME real `process_folder` — only the discovery path
differs. What must come out identical is what the operator and the
database see: the merged counters, the errors, the emitted lines, and the
clip rows a writing run leaves behind.

The router is the load-bearing part of this file, so it is written to be
able to EXPRESS divergence rather than to hide it:

* its legacy branch is an INDEPENDENT little evaluator of the search doc
  `build_search_doc` actually produced — parent regexps, the extension
  wildcards AND the providers' raw filters — not a parent-only
  approximation. Comparing `scan/discovery.py`'s evaluator against a
  second, differently written one is the only way the raw-filter half
  proves anything;
* it honours `first`/`number`, so legacy's page loop really runs. Without
  that, no equivalence test ever executed the multi-page path;
* and it returns matches in a deliberately NON-sorted order, the way an
  unsorted OpenSearch query does — which is what makes the AD-7 ordering
  divergence below observable instead of accidental.

The fixture is the interesting shape, not the minimal one: a folder whose
clips live in a provider SUB-PATH, a folder with an ordinary child folder
of its own, a RED-shaped `.RDM`/`.RDC` card whose files are reachable ONLY
through the provider's raw filter, and a subtree deep enough to cross a
page boundary at `--discovery-page-size 2`.
"""

import fnmatch
import os
import re

import pytest
from django.contrib.auth.models import User

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.models.folder import Folder, process_folder
from portal.plugins.TapelessIngest.providers.red import CARD_SUBPATH_REGEXP
from portal.plugins.TapelessIngest.scan.adapters import build_context
from portal.plugins.TapelessIngest.scan.context import DISCOVERY_INDEX
from portal.plugins.TapelessIngest.scan.discovery import DiscoveryIndex, prefetch_index

STORAGE_ID = "VX-41"
ROOT = "2026"

ONE = f"{ROOT}/AH_20260101_one"
TWO = f"{ROOT}/AH_20260102_two"
CARD = f"{ROOT}/AH_20260103_card"
DEEP = f"{ROOT}/AH_20260104_deep"
RED = f"{ROOT}/AH_20260105_red"

# The provider sub-path this fixture's provider claims. Its bucket belongs
# to CARD's hit set, and CARD's clips then consume it, so the walk never
# visits it as a folder of its own (AD-8).
SUBPATH = "CONTENTS"

# The real-world RED card layout, and the real shape of `red.getFilters()`:
# a `regexp` on `parent` anchored at the folder plus a `wildcard` on
# `name`. This is the ONLY way its files can be found — they sit in no
# `getSubPaths()` bucket — so it is what drives the raw-filter evaluator
# through the equivalence relation.
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
    # Same card, NOT the first segment. Since the card story it is a HIT
    # on both paths — the raw filter stopped selecting the anchor — and
    # is dropped at ASSEMBLY, as the anchor's extra file. A mode that
    # disagreed with the other about the raw filter would report a
    # different hit count here.
    f"{RED_CLIP_DIR}/A001_002.fake",
    # Indexed, in a matching parent, and NOT a media file: only the
    # extension clause excludes it. Without it in the fixture, a router
    # (or a mode) that ignored `getExtensions()` would look identical to
    # one that honoured it.
    f"{ONE}/NOTES.txt",
]

# Every file but the text one (excluded by the extension clause) is
# DISCOVERED — the second RED segment included.
EXPECTED_HITS = len(FILES) - 1
# ...and one fewer becomes a clip: the second segment is the first one's
# extra file, which is the assembly rules' job, not the query's.
EXPECTED_CLIPS = EXPECTED_HITS - 1


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


# --------------------------------------------------------------------------
# An INDEPENDENT evaluator of the legacy search doc
# --------------------------------------------------------------------------


def _matches(node, source):
    """Does `source` satisfy this query clause?

    Deliberately a second, simpler implementation than
    `scan/discovery.py`'s: no strictness, no MISSING sentinel, no
    projection contract — just the four clause kinds `build_search_doc`
    emits, read straight off the document. Two independently written
    evaluators agreeing on the same fixture is the evidence; one
    evaluator agreeing with itself is not.
    """
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


def _install_router(es_fake, paths):
    """One responder for both discovery paths, as one index would be."""
    stream = [
        {"_source": _source(path), "sort": [path, f"VX-41-{path}"]}
        for path in sorted(paths)
    ]
    # NOT sorted by path: an unsorted OpenSearch query returns documents
    # in index order, and legacy's per-page `sorted()` is the only thing
    # ordering them. Reverse order makes that visible.
    unsorted_sources = [_source(path) for path in sorted(paths, reverse=True)]

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
        query = search_doc["query"]
        matched = [source for source in unsorted_sources if _matches(query, source)]
        page = matched[first : first + number]
        return {
            "hits": {
                "total": {"value": len(matched)},
                "hits": [{"_source": source} for source in page],
            }
        }

    es_fake.route(respond)


@pytest.fixture
def tree(tmp_path, es_fake, storage_fake):
    for relative in FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, FILES)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return tmp_path


def _red_filters(escaped_path):
    """`red.getFilters()`'s exact shape, over the fixture's extension.

    The structure is red's verbatim — `bool.must` of a `regexp` on
    `parent` joined to the escaped folder plus a `wildcard` on `name` —
    and the card pattern is IMPORTED from the provider rather than
    copied, so narrowing it reddens the equivalence too. Only the suffix
    differs (`.fake` instead of `.R3D`), so the fixture's provider double
    can actually extract the clips it finds.

    Note the `name` wildcard no longer selects the `_001` anchor: since
    the card story that is an assembly concern, and the query returns
    every segment.
    """
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
    """The fixture provider: one sub-path AND one red-shaped raw filter.

    Monkeypatched on the INSTANCE rather than shipped on the double,
    because the same `getSubPaths()` feeds `build_search_doc`'s parent
    regexps, index discovery's bucket folding AND `consumed_subdirs`'
    layer (b) — one value drives all three, so the equivalence really is
    about discovery. `getFilters()` is the half no earlier test reached:
    `red` is the only shipped provider that has one, and its evaluator is
    the largest speculative piece of `scan/discovery.py`.

    `getSegmentedExtensions()` joins them for the same reason: since the
    card story it is what drops `A001_002.fake`, and it must drop it
    identically on both paths.
    """
    monkeypatch.setattr(fake_provider, "getSubPaths", lambda: [SUBPATH])
    monkeypatch.setattr(fake_provider, "getFilters", _red_filters)
    monkeypatch.setattr(fake_provider, "getSegmentedExtensions", lambda: [".fake"])
    return fake_provider


def _context(provider, **options):
    defaults = dict(
        user=None,
        dry_run=True,
        providers=[provider.machine_name],
        legacy_storages=[],
        replace=False,
        startwith=["AH_"],
    )
    defaults.update(options)
    return build_context([STORAGE_ID], **defaults)


def _run(ctx):
    emitted = []
    folder = Folder(storage_id=STORAGE_ID, path=ROOT)
    folder._root_path = ctx.root_path_for(STORAGE_ID)
    return folder.scan_tree(ctx, emit=emitted.append), emitted


def _with_index(ctx, es_fake, root=ROOT, index=None):
    """A context carrying a prefetched index, the way `scan_tree` builds one.

    Direct `process_folder` calls bypass `scan_tree`, so they attach it
    here — with `dataclasses.replace`, exactly as the real prefetch does.
    """
    import dataclasses

    if index is None:
        index = prefetch_index(es_fake, STORAGE_ID, root, page_size=500)
    return dataclasses.replace(ctx, discovery_index=index)


def _counters(run_result):
    counters = run_result.counters
    return {
        field: getattr(counters, field)
        for field in (
            "hits",
            "created",
            "already_ingested",
            "processed",
            "ingested",
            "skipped",
            "failed",
            "replaced",
        )
    }


def _clip_rows():
    return sorted(
        Clip.objects.values_list("umid", "path", "provider_name", "folder_path")
    )


def _timing_free(lines):
    """The emitted lines with the two elapsed-seconds figures blanked.

    The summary quotes wall-clock durations, which differ between any two
    runs; everything else in it is the run's own arithmetic.
    """
    return [re.sub(r"\d+\.\d+s", "<t>", line) for line in lines]


# --------------------------------------------------------------------------
# The equivalence relation (AD-2)
# --------------------------------------------------------------------------


def test_the_two_paths_agree_on_counters_errors_and_emitted_lines(
    migrated_db, tree, card_provider
):
    legacy_result, legacy_lines = _run(_context(card_provider))
    index_result, index_lines = _run(_context(card_provider, discovery=DISCOVERY_INDEX))

    assert _counters(index_result) == _counters(legacy_result)
    assert list(index_result.errors) == list(legacy_result.errors)
    assert (
        index_result.folders_scanned,
        index_result.folders_failed,
    ) == (legacy_result.folders_scanned, legacy_result.folders_failed)
    # The whole operator-visible report, summary lines included — index
    # discovery adds no banner of its own (its prefetch note goes to
    # portal.log).
    assert _timing_free(index_lines) == _timing_free(legacy_lines)
    # ...and the run really did find the tree, so this is not two empty
    # runs agreeing.
    assert legacy_result.counters.hits == EXPECTED_HITS
    assert legacy_result.counters.created == EXPECTED_CLIPS


def test_the_raw_filter_half_agrees_with_the_legacy_query(
    migrated_db, tree, card_provider
):
    """C3: the RED card, found through `getFilters()` on both paths.

    Its files sit in no `getSubPaths()` bucket, so ONLY the raw filter
    can reach them — and both segments must be DISCOVERED, which is what
    changed with the card story: the raw filter stopped selecting the
    anchor, so `A001_002.fake` is a hit on both paths and is dropped at
    assembly instead. A mode that ignored the filter finds 0 files here;
    one that still narrowed it to `*_001` finds 1.
    """
    legacy_result, legacy_lines = _run(_context(card_provider))
    index_result, index_lines = _run(_context(card_provider, discovery=DISCOVERY_INDEX))

    def red_line(lines):
        return [line for line in lines if f" files in {RED}," in line]

    assert red_line(legacy_lines) == red_line(index_lines)
    # "clips" in that line is the HIT count (a legacy misnomer the
    # summary keeps): both segments are found, and one clip is created.
    assert red_line(index_lines)[0].startswith(f"found 2 files in {RED},")
    assert ", 1 created," in red_line(index_lines)[0]
    assert _counters(index_result) == _counters(legacy_result)


def test_the_two_paths_write_the_same_clip_rows(
    migrated_db, tree, card_provider, monkeypatch
):
    """AD-2's equivalence is on the tuples, not on the counters alone."""
    monkeypatch.setattr(
        Folder, "getCollection", lambda self, user, dryrun=False: "VX-COLLECTION"
    )
    user = User.objects.create(pk=4441, username="story41-equivalence")
    try:
        _run(_context(card_provider, dry_run=False, user=user))
        legacy_rows = _clip_rows()
        ClipMetadata.objects.all().delete()
        Clip.objects.all().delete()
        Folder.objects.all().delete()

        _run(
            _context(
                card_provider,
                dry_run=False,
                user=user,
                discovery=DISCOVERY_INDEX,
            )
        )
        index_rows = _clip_rows()
    finally:
        user.delete()

    assert index_rows == legacy_rows
    assert len(legacy_rows) == EXPECTED_CLIPS


# --------------------------------------------------------------------------
# What the index path buys: one query stream per scan root
# --------------------------------------------------------------------------


def test_index_discovery_queries_once_per_scan_root_not_once_per_folder(
    migrated_db, tree, card_provider, es_fake
):
    _run(_context(card_provider, discovery=DISCOVERY_INDEX))
    index_calls = list(es_fake.calls)
    es_fake.calls.clear()

    _run(_context(card_provider))
    legacy_calls = list(es_fake.calls)

    # One stream: a single page (7 files, default page size 500) and no
    # follow-up, because the page came back short.
    assert index_calls == [(0, 500)]
    # Legacy asks once per folder the walk visits — and the gap is the
    # whole point: on prod that is ~4,000 queries for one scan root.
    assert len(legacy_calls) > len(index_calls)
    assert all(first == 0 for first, _number in index_calls)


def test_the_prefetched_index_is_safe_to_share_across_pool_workers(
    migrated_db, tree, card_provider, es_fake
):
    """AD-4 under the pool: the index is built BEFORE fan-out and only read.

    The prefetch is attached with `dataclasses.replace`, so no context is
    mutated once workers are running, and `hits_for` mutates nothing a
    caller can observe — a pooled run must therefore produce the
    sequential run's output byte for byte.
    """
    sequential_result, sequential_lines = _run(
        _context(card_provider, discovery=DISCOVERY_INDEX)
    )
    es_fake.calls.clear()

    pooled_result, pooled_lines = _run(
        _context(card_provider, discovery=DISCOVERY_INDEX, workers=4)
    )

    assert _counters(pooled_result) == _counters(sequential_result)
    assert _timing_free(pooled_lines) == _timing_free(sequential_lines)
    # Still ONE stream, however many workers read it.
    assert es_fake.calls == [(0, 500)]


def test_a_multi_page_prefetch_still_matches_legacy(
    migrated_db, tree, card_provider, es_fake
):
    """The page boundary crossed for real, inside a whole tree run."""
    legacy_result, legacy_lines = _run(_context(card_provider))
    es_fake.calls.clear()

    index_result, index_lines = _run(
        _context(card_provider, discovery=DISCOVERY_INDEX, discovery_page_size=2)
    )

    # 2 x 4 for the eight documents, then the empty fifth page that ends
    # the stream on the exactly-full boundary. `first` never moves.
    assert es_fake.calls == [(0, 2)] * 5
    assert _counters(index_result) == _counters(legacy_result)
    assert _timing_free(index_lines) == _timing_free(legacy_lines)


def test_the_index_stream_carries_a_cursor_and_never_an_offset(
    migrated_db, tree, card_provider, es_fake
):
    _run(_context(card_provider, discovery=DISCOVERY_INDEX, discovery_page_size=2))

    docs = [doc for doc, _doc_type in es_fake.call_docs]
    assert [doc.get("search_after") for doc in docs][0] is None
    assert all(doc.get("search_after") for doc in docs[1:])
    assert all(doc["sort"] == [{"path": "asc"}, {"id": "asc"}] for doc in docs)
    # AD-3's ban, at the wire: nothing in this run used from/size paging.
    assert all(first == 0 for first, _number in es_fake.calls)
    # C4: every argument is recorded, and only the cursor ever changes.
    assert {call["number"] for call in es_fake.call_kwargs} == {2}
    assert {call["first"] for call in es_fake.call_kwargs} == {0}


# --------------------------------------------------------------------------
# AD-8 under the index path
# --------------------------------------------------------------------------


def test_a_consumed_subdir_is_never_scanned_as_a_folder_of_its_own(
    migrated_db, tree, card_provider
):
    """The duplicate NFR-1 forbids, in the shape index discovery could take.

    CLIPD lives in a provider sub-path. Its bucket is folded into CARD's
    hit set, CARD's clip then consumes `CONTENTS`, and the walk refuses to
    descend — so the file is discovered exactly once, under CARD. If the
    bucket were ALSO scanned as its own folder, the counters would carry
    it twice.
    """
    _index_result, index_lines = _run(
        _context(card_provider, discovery=DISCOVERY_INDEX)
    )

    card_lines = [line for line in index_lines if f" files in {CARD}," in line]
    assert card_lines[0].startswith(f"found 1 files in {CARD},")
    # The sub-path folder never reports at all: it was never visited.
    assert not any(f"{CARD}/{SUBPATH}" in line for line in index_lines)
    # Same for the RED card's internals, consumed by its own clip.
    assert not any(f"{RED}/{RDM}" in line for line in index_lines)


def test_an_ordinary_child_folder_is_still_the_walks_to_visit(
    migrated_db, tree, card_provider
):
    """The other direction: folding too much would hide a real folder.

    `DEEP` has no files of its own and `sub` is not a provider sub-path,
    so `sub` must be scanned as its own folder — exactly as under legacy.
    """
    _index_result, index_lines = _run(
        _context(card_provider, discovery=DISCOVERY_INDEX)
    )

    assert any(line.startswith(f"found 1 files in {DEEP}/sub,") for line in index_lines)
    assert not any(line.startswith(f"found 1 files in {DEEP},") for line in index_lines)


# --------------------------------------------------------------------------
# The page-loop guard, and the ordering divergence it exposes
# --------------------------------------------------------------------------


@pytest.fixture
def wide_folder(tmp_path, es_fake, storage_fake):
    """One folder holding exactly `result_number` (100) matching files.

    The boundary the index-mode page-loop guard sits on: under legacy a
    full page means "ask for another", and index mode must NOT inherit
    that rule — its `hits_for` returns the folder's WHOLE bucket, so
    asking again returns the same 100 hits forever. Dropping the
    `discovery_index is None` term from the `has_next` condition hangs
    this test rather than failing it, which is the intended signal.
    """
    paths = [f"{ONE}/CLIP{i:03d}.fake" for i in range(100)]
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, paths)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return paths


def test_an_exactly_full_folder_does_not_loop_under_index_discovery(
    migrated_db, wide_folder, card_provider, es_fake, monkeypatch
):
    calls = []
    original = DiscoveryIndex.hits_for

    def counting(self, folder_path, provider_list):
        calls.append(folder_path)
        return original(self, folder_path, provider_list)

    monkeypatch.setattr(DiscoveryIndex, "hits_for", counting)

    ctx = _with_index(_context(card_provider, discovery=DISCOVERY_INDEX), es_fake)
    outcome = process_folder(STORAGE_ID, ONE, ctx, first=0, number=0, ingest=True)

    # ONE discovery pass for the folder, not an unbounded sequence of
    # identical ones.
    assert calls == [ONE]
    assert outcome.result.counters.hits == 100
    assert outcome.result.counters.processed == 100
    assert outcome.result.counters.created == 100
    assert not outcome.failed


@pytest.fixture
def deep_folder(tmp_path, es_fake, storage_fake):
    """250 files in one folder — two and a half legacy pages."""
    paths = [f"{ONE}/CLIP{i:03d}.fake" for i in range(250)]
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, paths)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    return paths


def test_a_multi_page_folder_finds_the_same_clips_in_a_different_order(
    migrated_db, deep_folder, card_provider, es_fake
):
    """A sanctioned divergence, pinned rather than left unexpressed.

    AD-7 requires file iteration within a folder to be deterministic and
    sorted by path in BOTH modes. Legacy sorts each 100-hit page on its
    own, so a folder spanning three pages hands its clips over in
    page-then-path order — index-order-dependent, and not globally
    sorted. Index mode has one page per folder and is globally sorted.

    So the SETS are equal (that is AD-2's equivalence relation, and it
    holds), and the ORDER differs — with index being the one that honours
    AD-7. Legacy's ordering is the defect; it stays, grandfathered with
    its query, until legacy retirement.
    """
    legacy_ctx = _context(card_provider)
    legacy = process_folder(STORAGE_ID, ONE, legacy_ctx, first=0, number=0, ingest=True)
    legacy_calls = list(es_fake.calls)
    es_fake.calls.clear()
    index_ctx = _with_index(_context(card_provider, discovery=DISCOVERY_INDEX), es_fake)
    index = process_folder(STORAGE_ID, ONE, index_ctx, first=0, number=0, ingest=True)

    # Legacy's page loop really ran — three from/size pages for 250 hits.
    # Before the router honoured `first`/`number` it never did, in ANY
    # equivalence test.
    assert legacy_calls == [(0, 100), (100, 100), (200, 100)]
    # `umid` is the per-FILE identity here (`clip.path` is the folder).
    legacy_umids = [clip.umid for clip in legacy.result.clips]
    index_umids = [clip.umid for clip in index.result.clips]

    assert legacy.result.counters.hits == index.result.counters.hits == 250
    # Same clips, by the relation AD-2 actually defines...
    assert set(legacy_umids) == set(index_umids)
    assert len(legacy_umids) == len(index_umids) == 250
    # ...and index is the mode that satisfies AD-7's sorted iteration.
    assert index_umids == sorted(index_umids)
    assert legacy_umids != sorted(legacy_umids)


# --------------------------------------------------------------------------
# Completeness: the descent guard must not be tautological
# --------------------------------------------------------------------------


def test_a_short_index_refuses_descent_instead_of_vouching_for_the_tree(
    migrated_db, tree, card_provider, es_fake
):
    """NFR-1's under-consumption direction, through the whole pipeline.

    `total = len(hits)` would make `seen_hits >= response["hits"]`
    vacuously true, so a folder whose index view is missing documents
    would authorize descent into subdirs its (incomplete) clips never
    claimed — the duplicate direction. The stream's own reported total
    travels onto the index for exactly this.
    """
    short = DiscoveryIndex(
        STORAGE_ID,
        ROOT,
        [{"_source": _source(f"{TWO}/CLIPB.fake")}],
        # The index says there are three files under this root; the view
        # holds one.
        reported_total=3,
    )
    ctx = _with_index(
        _context(card_provider, discovery=DISCOVERY_INDEX), es_fake, index=short
    )

    outcome = process_folder(STORAGE_ID, TWO, ctx, first=0, number=0, ingest=True)

    assert short.shortfall == 2
    # DOUBT: descent is not authorized for this folder.
    assert outcome.consumed_subdirs is None
    assert any(
        "the index reported 3 files but only 1 were returned" in error
        for error in outcome.result.errors
    )


def test_a_prefetch_failure_kills_the_run_with_the_scan_root_named(
    migrated_db, tree, card_provider, es_fake
):
    """A8: not just ValueError — a reset connection is at least as likely."""

    def respond(search_doc, first, number):
        raise ConnectionResetError("peer closed the connection")

    es_fake.route(respond)

    with pytest.raises(TapelessIngestException, match=f"prefetch {ROOT} on storage"):
        _run(_context(card_provider, discovery=DISCOVERY_INDEX))


# --------------------------------------------------------------------------
# Dry-run purity and the paged-mode refusals
# --------------------------------------------------------------------------


def test_a_dry_run_under_index_writes_nothing(migrated_db, tree, card_provider):
    run_result, _lines = _run(_context(card_provider, discovery=DISCOVERY_INDEX))

    assert run_result.counters.hits == EXPECTED_HITS
    assert Clip.objects.count() == 0
    assert ClipMetadata.objects.count() == 0
    assert Folder.objects.count() == 0


def test_index_discovery_is_refused_by_the_paged_facade(
    migrated_db, tree, card_provider, es_fake
):
    """AD-14, at the façade, before any query."""
    ctx = _context(card_provider, discovery=DISCOVERY_INDEX)
    folder = Folder(storage_id=STORAGE_ID, path=ONE)
    folder._root_path = ctx.root_path_for(STORAGE_ID)

    with pytest.raises(TapelessIngestException, match="paged mode"):
        folder.scan(number=25, context=ctx)
    with pytest.raises(TapelessIngestException, match="paged mode"):
        folder.ingest(number=25, context=ctx)
    # "before any query" is the load-bearing half.
    assert es_fake.calls == []


@pytest.mark.parametrize(
    "first,number",
    [
        # A page SIZE — the obvious half.
        (0, 25),
        # ...and a page OFFSET with the tree-mode `number=0` sentinel: a
        # caller resuming at row 25 gets the whole folder from row 0, so
        # every row before the offset is processed twice across the two
        # calls. Ignoring `first` is the duplicate direction.
        (25, 0),
        (25, 25),
    ],
)
def test_a_paging_request_under_index_is_refused_rather_than_ignored(
    migrated_db, tree, card_provider, es_fake, first, number
):
    """A12: `hits_for` returns a whole bucket, so `first`/`number` have
    nothing to slice — a caller asking for 25 rows from row 25 would
    silently get every file in the folder, from the beginning."""
    ctx = _with_index(_context(card_provider, discovery=DISCOVERY_INDEX), es_fake)

    outcome = process_folder(
        STORAGE_ID, TWO, ctx, first=first, number=number, ingest=True
    )

    assert outcome.failed
    assert any("cannot serve a paged request" in e for e in outcome.result.errors)


def test_a_context_without_a_prefetch_fails_loudly_rather_than_falling_back(
    migrated_db, tree, card_provider
):
    """A run that says `index` and quietly queries the legacy way is the
    one outcome the equivalence gate cannot detect."""
    ctx = _context(card_provider, discovery=DISCOVERY_INDEX)
    assert ctx.discovery_index is None

    result = process_folder(STORAGE_ID, ONE, ctx, number=0, ingest=True)

    assert result.failed
    assert any("no prefetched index" in error for error in result.result.errors)

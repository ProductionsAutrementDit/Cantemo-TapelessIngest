"""Tier 1 (story 4.1): the index discovery path, in isolation.

``scan/discovery.py`` is stdlib-only and takes an injected
``query_elastic``, so everything here is a pure unit test: the query
SHAPE (AD-3's envelope), ``search_after`` paging including the two
boundaries FR-3 exists for, the completeness arithmetic that keeps
NFR-1's descent guard meaningful, and the per-folder hit-set assembly
that has to reproduce what legacy's query for that folder would have
returned.

The legacy path is not exercised here at all — that is the point. Its
pins live in ``test_golden_search_doc.py`` and ``test_scan_pagination.py``
and this story must leave both untouched.

A recurring theme in the assertions below: this module must FAIL rather
than approximate. Every place where a provider's declaration or the
index's response cannot be reproduced faithfully raises, because the two
silent alternatives — matching nothing (the provider's clips vanish) and
matching everything (foreign files are handed to it) — are both
invisible in a green run and both produce wrong clips in production.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.scan.context import (
    DEFAULT_DISCOVERY,
    DEFAULT_DISCOVERY_PAGE_SIZE,
    DISCOVERY_INDEX,
    DISCOVERY_MODES,
    MAX_DISCOVERY_PAGE_SIZE,
    RunOptions,
    ScanContext,
    StorageInfo,
)
from portal.plugins.TapelessIngest.scan.coordinator import assert_mode_options
from portal.plugins.TapelessIngest.scan.discovery import (
    INDEX_SOURCE_FIELDS,
    MAX_INDEX_PAGES,
    MISSING,
    DiscoveryIndex,
    ShortIndexError,
    build_index_search_doc,
    fetch_index_hits,
    normalize_path,
    prefetch_index,
)

STORAGE_ID = "VX-41"
ROOT = "2026"
REPO_ROOT = Path(__file__).resolve().parents[2]

# cwd=repo root, no stub, no conftest: `python -c` puts the cwd on sys.path,
# so `scan.discovery` resolves to this repo's package in a bare interpreter.
PORTAL_FREEDOM_SCRIPT = (
    "import sys, scan.discovery; "
    "assert not [m for m in sys.modules if m == 'portal' or m.startswith('portal.')]; "
    "assert 'django' not in sys.modules"
)


# --------------------------------------------------------------------------
# AD-1: no Portal, no Django, no ORM
# --------------------------------------------------------------------------


def test_scan_discovery_imports_portal_free_in_subprocess():
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_FREEDOM_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan.discovery is not Portal-free in a bare interpreter (AD-1):\n"
        f"{result.stderr.decode(errors='replace')}"
    )


def test_the_golden_recorder_never_reaches_the_index_path():
    """AD-3's grandfathering, as a source guard.

    `build_search_doc` is compared byte for byte against a committed
    fixture. The one way that pin could silently stop meaning anything is
    if the legacy builder — or the recorder that drives it — grew a
    dependency on this story's module.

    The marker split is ASSERTED, not tolerated: `if len(...) > 1` would
    have made a renamed or deleted `build_search_doc` pass vacuously,
    which is the same defect this test exists to catch.
    """
    folder_source = (REPO_ROOT / "models" / "folder.py").read_text(encoding="utf-8")
    parts = folder_source.split("    def build_search_doc(self, provider_list):", 1)
    assert len(parts) == 2, "build_search_doc's definition line moved — re-anchor"
    body = parts[1].split("\n    def ", 1)[0]
    assert "discovery" not in body

    recorder = (REPO_ROOT / "tests" / "tier1" / "build_golden_doc.py").read_text(
        encoding="utf-8"
    )
    assert "build_search_doc" in recorder, "the golden recorder moved — re-anchor"
    assert "discovery" not in recorder


# --------------------------------------------------------------------------
# Provider doubles: duck-typed on the three methods build_search_doc reads
# --------------------------------------------------------------------------


class _Provider:
    def __init__(self, extensions=(), subpaths=(), filters=()):
        self._extensions = list(extensions)
        self._subpaths = list(subpaths)
        self._filters = list(filters)
        self.filter_paths = []

    def getExtensions(self):
        return list(self._extensions)

    def getSubPaths(self):
        return list(self._subpaths)

    def getFilters(self, escaped_path):
        self.filter_paths.append(escaped_path)
        return [_substitute(clause, escaped_path) for clause in self._filters]


def _substitute(node, escaped_path):
    """Render `{path}` in a filter template, the way red builds its own."""
    if isinstance(node, dict):
        return {k: _substitute(v, escaped_path) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, escaped_path) for v in node]
    if isinstance(node, str):
        return node.replace("{path}", escaped_path)
    return node


def _hit(path, hit_id=None, **extra):
    parent, _, name = path.rpartition("/")
    source = {
        "path": path,
        "parent": parent,
        "name": name,
        "storage": STORAGE_ID,
        "id": hit_id or f"VX-41-{path}",
        "item_type": "file",
    }
    source.update(extra)
    return {"_source": source, "sort": [source["path"], source["id"]]}


def _paths(hits):
    return [hit["_source"]["path"] for hit in hits]


def _index(paths, **kwargs):
    return DiscoveryIndex(STORAGE_ID, ROOT, [_hit(path) for path in paths], **kwargs)


def _hits_for(index, folder_path, providers):
    hits, _total = index.hits_for(folder_path, providers)
    return hits


# --------------------------------------------------------------------------
# The query shape (FR-2, AD-3)
# --------------------------------------------------------------------------


def test_the_query_carries_the_four_filters_and_nothing_else():
    doc = build_index_search_doc(STORAGE_ID, ROOT)

    assert doc["query"]["bool"]["filter"] == [
        {"term": {"storage": STORAGE_ID}},
        {
            "bool": {
                "should": [
                    {"term": {"parent": ROOT}},
                    {"prefix": {"parent": "2026/"}},
                ]
            }
        },
        {"term": {"item_type": "file"}},
        {
            "bool": {
                "must_not": [
                    {"term": {"state": "LOST"}},
                    {"term": {"state": "MISSING"}},
                ]
            }
        },
    ]
    # No `size`/`from` in the doc: paging is the caller's `number=` plus
    # the cursor, never a window offset.
    assert "from" not in doc and "size" not in doc


def test_the_query_uses_no_expensive_form():
    """FR-2: no `regexp`, no leading wildcard, nothing new and expensive.

    Legacy's query is exactly those two forms — a `regexp` per parent and
    a `*.ext`/`*.EXT` pair per extension — and reintroducing either here
    would put the cost this story removes straight back on the index.
    """
    text = repr(build_index_search_doc(STORAGE_ID, ROOT, search_after=["a", "b"]))

    assert "regexp" not in text
    assert "wildcard" not in text
    assert '"*' not in text and "'*" not in text


def test_the_sort_is_path_then_the_non_null_tiebreaker():
    doc = build_index_search_doc(STORAGE_ID, ROOT)

    assert doc["sort"] == [{"path": "asc"}, {"id": "asc"}]
    # The tiebreaker is only unique because directory documents — which
    # carry `id: null` on prod — are filtered out index-side.
    assert {"term": {"item_type": "file"}} in doc["query"]["bool"]["filter"]


def test_the_total_is_tracked_exactly_and_the_source_is_projected():
    """A capped total would make the completeness arithmetic lie.

    OpenSearch stops counting at 10,000 by default, so a 65,024-file scan
    root would report 10,000 — and `reported_total - hit_count` would go
    NEGATIVE, i.e. the index would vouch for a tree it had not counted.
    """
    doc = build_index_search_doc(STORAGE_ID, ROOT)

    assert doc["track_total_hits"] is True
    assert doc["_source"] == list(INDEX_SOURCE_FIELDS)
    # The projection must cover what VSFile reads, or every clip breaks
    # downstream on a KeyError instead of here on a clear message.
    assert {"path", "hash", "storage", "id", "size"} <= set(INDEX_SOURCE_FIELDS)


def test_search_after_is_carried_in_the_document():
    assert "search_after" not in build_index_search_doc(STORAGE_ID, ROOT)
    doc = build_index_search_doc(STORAGE_ID, ROOT, search_after=("2026/a.mp4", "VX-1"))
    assert doc["search_after"] == ["2026/a.mp4", "VX-1"]


def test_the_root_scope_is_the_subtree_and_not_a_bare_prefix():
    """`prefix parent = "2026"` would also match the sibling `2026bis`."""
    scope = build_index_search_doc(STORAGE_ID, ROOT)["query"]["bool"]["filter"][1]
    should = scope["bool"]["should"]

    assert {"prefix": {"parent": "2026/"}} in should
    # ...and the root folder's OWN files, which a `2026/` prefix misses
    # and legacy's exact regexp on `parent` returns.
    assert {"term": {"parent": "2026"}} in should


@pytest.mark.parametrize("root", ["2026/", "2026//"])
def test_a_trailing_slash_on_the_scan_root_is_normalised(root):
    """Silent-empty-tree class: BOTH scope clauses used to miss.

    `term parent = "2026/"` matches nothing (no indexed parent carries a
    trailing separator) and `prefix parent = "2026//"` matches nothing
    either — so the run reported an empty tree and exited CLEAN, which is
    the worst failure mode a nightly job has.
    """
    scope = build_index_search_doc(STORAGE_ID, root)["query"]["bool"]["filter"][1]

    assert scope["bool"]["should"] == [
        {"term": {"parent": "2026"}},
        {"prefix": {"parent": "2026/"}},
    ]


@pytest.mark.parametrize("root", ["", "/", None])
def test_a_rootless_scan_scopes_to_the_whole_storage(root):
    """And never produces the `//` prefix a bare `root + "/"` would."""
    filters = build_index_search_doc(STORAGE_ID, root)["query"]["bool"]["filter"]

    assert filters == [
        {"term": {"storage": STORAGE_ID}},
        {"term": {"item_type": "file"}},
        {
            "bool": {
                "must_not": [
                    {"term": {"state": "LOST"}},
                    {"term": {"state": "MISSING"}},
                ]
            }
        },
    ]


@pytest.mark.parametrize(
    "raw,expected",
    [("2026", "2026"), ("2026/", "2026"), ("/", ""), ("", ""), (None, "")],
)
def test_normalize_path(raw, expected):
    assert normalize_path(raw) == expected


# --------------------------------------------------------------------------
# search_after paging (FR-3)
# --------------------------------------------------------------------------


def test_every_page_after_the_first_uses_the_cursor_and_never_an_offset(es_fake):
    stream = [_hit(f"{ROOT}/f{i:03d}.mp4") for i in range(7)]
    es_fake.route_search_after(stream)

    hits, total = fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=3)

    assert _paths(hits) == _paths(stream)
    assert total == 7
    # 3 + 3 + 1: the short page ends the loop. `first` is 0 on EVERY call
    # — AD-3 forbids from/size beyond page one — and `number` is the page
    # size throughout.
    assert es_fake.calls == [(0, 3), (0, 3), (0, 3)]
    cursors = [doc.get("search_after") for doc, _doc_type in es_fake.call_docs]
    assert cursors == [
        None,
        [f"{ROOT}/f002.mp4", f"VX-41-{ROOT}/f002.mp4"],
        [f"{ROOT}/f005.mp4", f"VX-41-{ROOT}/f005.mp4"],
    ]
    # C4: the fake records every argument, and the doc_type must be the
    # same one legacy sends — the two paths read the same index.
    assert [call["doc_type"] for call in es_fake.call_kwargs] == [["file"]] * 3
    assert all(
        call["first"] == 0 and call["number"] == 3 for call in es_fake.call_kwargs
    )


def test_an_exactly_full_last_page_costs_one_extra_empty_fetch(es_fake):
    es_fake.route_search_after([_hit(f"{ROOT}/f{i}.mp4") for i in range(4)])

    hits, _total = fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=2)

    assert len(hits) == 4
    # The loop only stops on a NON-full page, so total % page_size == 0
    # buys one empty query — and, crucially, not one duplicated document.
    assert es_fake.calls == [(0, 2), (0, 2), (0, 2)]
    assert len(_paths(hits)) == len(set(_paths(hits)))


def test_more_than_ten_thousand_hits_are_all_fetched_exactly_once(es_fake):
    """The window legacy cannot exceed at all, crossed and then some."""
    stream = [_hit(f"{ROOT}/f{i:06d}.mp4") for i in range(10_500)]
    es_fake.route_search_after(stream)

    hits, total = fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=500)

    assert len(hits) == 10_500
    assert len(set(_paths(hits))) == 10_500
    assert _paths(hits) == _paths(stream)
    assert total == 10_500  # exact, because track_total_hits is on
    assert len(es_fake.calls) == 22  # 21 full pages + the short one
    assert all(first == 0 for first, _number in es_fake.calls)


def test_a_full_page_with_no_cursor_is_fatal(es_fake, es_page):
    """A truncated scan root must not look like a small tree."""
    es_fake.push(es_page([{"path": f"{ROOT}/a.mp4"}] * 2, total=9))

    with pytest.raises(ValueError, match="no sort values"):
        fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=2)


def test_a_cursor_carrying_a_null_is_refused(es_fake, es_page):
    """The exact shape `term item_type=file` exists to prevent.

    OpenSearch cannot compare `search_after` against a null, so a null
    tiebreaker silently skips or duplicates documents — which is the
    whole defect FR-3 closes.
    """
    es_fake.push(
        es_page(
            [{"path": f"{ROOT}/a.mp4", "id": None}, {"path": f"{ROOT}/b", "id": None}],
            total=9,
            sort=True,
        )
    )

    with pytest.raises(ValueError, match="contain a null"):
        fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=2)


def test_a_cursor_that_does_not_advance_is_refused_instead_of_looping(es_fake):
    """A responder ignoring `search_after` used to be an unbounded loop."""
    page = [_hit(f"{ROOT}/a.mp4"), _hit(f"{ROOT}/b.mp4")]

    def respond(search_doc, first, number):
        return {"hits": {"total": {"value": 99}, "hits": page}}

    es_fake.route(respond)

    with pytest.raises(ValueError, match="did not advance"):
        fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=2)
    # Two calls, not two hundred thousand: the second page is what proves
    # the cursor repeated.
    assert len(es_fake.calls) == 2


def test_the_page_ceiling_converts_an_endless_stream_into_an_error(
    es_fake, monkeypatch
):
    """Belt to the cursor check's braces: a stream that always advances
    but never ends still has to stop."""
    monkeypatch.setattr(
        "portal.plugins.TapelessIngest.scan.discovery.MAX_INDEX_PAGES", 5
    )
    counter = {"n": 0}

    def respond(search_doc, first, number):
        counter["n"] += 1
        start = counter["n"] * 2
        page = [_hit(f"{ROOT}/f{i:06d}.mp4") for i in (start, start + 1)]
        return {"hits": {"total": {"value": 10**9}, "hits": page}}

    es_fake.route(respond)

    with pytest.raises(ValueError, match="the stream is not ending"):
        fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=2)
    assert len(es_fake.calls) == 5
    assert MAX_INDEX_PAGES > 5  # the shipped ceiling is nowhere near a real run


def test_the_hit_ceiling_refuses_to_materialise_an_unbounded_root(es_fake, monkeypatch):
    """B1: the prefetch holds the whole scan root in memory by design.

    That is affordable for a year's tree (65,024 files) and is not
    affordable for an accidentally unscoped one — a `parent` clause that
    stopped narrowing would fetch a 29 TB storage's entire file index. A
    clear error beats an OOM kill three hours in.
    """
    monkeypatch.setattr(
        "portal.plugins.TapelessIngest.scan.discovery.MAX_INDEX_HITS", 5
    )
    counter = {"n": 0}

    def respond(search_doc, first, number):
        counter["n"] += 1
        start = counter["n"] * 2
        page = [_hit(f"{ROOT}/f{i:06d}.mp4") for i in (start, start + 1)]
        return {"hits": {"total": {"value": 10**9}, "hits": page}}

    es_fake.route(respond)

    with pytest.raises(ValueError, match="refusing to materialise"):
        fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=2)
    assert len(es_fake.calls) == 3  # 6 hits is the first count above 5


@pytest.mark.parametrize(
    "response,expected",
    [
        (None, "instead of a search result"),
        ({"error": "index_not_found"}, "no 'hits' body"),
        ({"hits": {"total": {"value": 1}}}, "not a list"),
        ({"hits": []}, "no 'hits' body"),
        # A completeness check whose input is missing must REFUSE, not
        # assume "no shortfall" — that is how a truncated stream sails
        # through the ShortIndexError gate and lets every folder
        # authorize descent on a partial view of itself.
        ({"hits": {"hits": []}}, "'hits.total' is None"),
        ({"hits": {"hits": [], "total": "many"}}, "'hits.total' is 'many'"),
        ({"hits": {"hits": [], "total": {"value": "1"}}}, "'hits.total' is"),
        # `gte` means OpenSearch stopped counting: the value is a floor,
        # not a count, so `hit_count` legitimately exceeds it and the
        # shortfall would clamp to zero and vouch for nothing.
        (
            {"hits": {"hits": [], "total": {"value": 10000, "relation": "gte"}}},
            "non-exact total",
        ),
    ],
)
def test_a_malformed_response_raises_a_clear_error(es_fake, response, expected):
    """`query_elastic` is injected, so a well-formed body cannot be assumed.

    Before this, an error envelope raised `KeyError`/`TypeError` straight
    past the caller's `ValueError`-only handling and out of the run.
    """

    def respond(search_doc, first, number):
        return response

    es_fake.route(respond)

    with pytest.raises(ValueError, match=expected):
        fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=2)


@pytest.mark.parametrize("page_size", [0, -1, True, "500", None])
def test_a_defective_page_size_is_refused_before_any_query(es_fake, page_size):
    with pytest.raises(ValueError, match="page size"):
        fetch_index_hits(es_fake, STORAGE_ID, ROOT, page_size=page_size)
    assert es_fake.calls == []


def test_prefetch_buckets_the_whole_stream_by_parent(es_fake):
    es_fake.route_search_after(
        [
            _hit(f"{ROOT}/A/one.mp4"),
            _hit(f"{ROOT}/A/two.mp4"),
            _hit(f"{ROOT}/B/three.mp4"),
        ]
    )

    index = prefetch_index(es_fake, STORAGE_ID, ROOT, page_size=500)

    assert index.hit_count == 3
    assert index.bucket_count == 2
    assert index.shortfall == 0
    assert _paths(index.bucket(f"{ROOT}/A")) == [
        f"{ROOT}/A/one.mp4",
        f"{ROOT}/A/two.mp4",
    ]
    assert index.bucket(f"{ROOT}/nothing-here") == ()
    # A3 again, at the read side.
    assert index.bucket(f"{ROOT}/A/") == index.bucket(f"{ROOT}/A")


# --------------------------------------------------------------------------
# Completeness: the arithmetic that keeps NFR-1's descent guard honest
# --------------------------------------------------------------------------


def test_a_short_stream_is_refused_at_prefetch(es_fake):
    """Every folder would otherwise read a truncated view and look fine."""

    def respond(search_doc, first, number):
        # Reports 9, returns 2 and ends — the shape a mid-stream index
        # change, a proxy cutting a response, or a silent error produces.
        return {
            "hits": {
                "total": {"value": 9},
                "hits": [_hit(f"{ROOT}/a.mp4"), _hit(f"{ROOT}/b.mp4")],
            }
        }

    es_fake.route(respond)

    with pytest.raises(ShortIndexError, match="but the index reported 9"):
        prefetch_index(es_fake, STORAGE_ID, ROOT, page_size=500)


def test_a_prefetch_whose_response_omits_the_total_refuses_to_build(es_fake):
    """Item 3, at the level that matters: the whole prefetch dies.

    `prefetch_index` is the gate that decides whether a run gets a
    trustworthy view of its scan root. A response with no `total` cannot
    answer that question, and answering it "fine" would be the one
    outcome no later check can catch.
    """

    def respond(search_doc, first, number):
        return {"hits": {"hits": [_hit(f"{ROOT}/a.mp4")]}}

    es_fake.route(respond)

    with pytest.raises(ValueError, match="'hits.total' is None"):
        prefetch_index(es_fake, STORAGE_ID, ROOT, page_size=500)


def test_a_total_below_the_bucketed_count_clamps_instead_of_going_negative():
    """A lying total must not make the completeness check EASIER.

    `shortfall` is added to every folder's reported total. A negative one
    would SUBTRACT from it, so a folder that consumed less than it found
    would still clear `seen_hits >= response["hits"]` — the
    under-consumption direction, i.e. descent authorized into subdirs the
    folder's (incomplete) clips never claimed.

    Reachable only via the bare-int spelling of `total`, which carries no
    `relation` for `_read_page` to reject; the clamp is what closes it.
    """
    index = _index(
        [f"{ROOT}/A/a.mp4", f"{ROOT}/A/b.mp4", f"{ROOT}/A/c.mp4"], reported_total=1
    )

    hits, total = index.hits_for(f"{ROOT}/A", [_Provider(extensions=[".mp4"])])

    assert index.shortfall == 0
    assert (len(hits), total) == (3, 3)


def test_a_short_index_makes_every_folders_total_unreachable():
    """The mutation this kills: `total = len(hits)` is TAUTOLOGICAL.

    `Folder._scan_pass` authorizes descent only when it consumed at least
    as many hits as the index reported. Answering that with the length of
    the very list it consumed makes the guard vacuously true — and the
    guard is what refuses descent when the index reported more files than
    it returned, which is NFR-1's under-consumption direction.
    """
    index = _index([f"{ROOT}/A/a.mp4", f"{ROOT}/A/b.mp4"], reported_total=5)
    provider = _Provider(extensions=[".mp4"])

    hits, total = index.hits_for(f"{ROOT}/A", [provider])

    assert index.shortfall == 3
    assert len(hits) == 2
    # ...so the folder can NEVER report having consumed everything.
    assert total == 5
    assert total > len(hits)


def test_a_complete_index_reports_each_folders_own_count():
    index = _index([f"{ROOT}/A/a.mp4", f"{ROOT}/B/b.mp4"])
    provider = _Provider(extensions=[".mp4"])

    hits, total = index.hits_for(f"{ROOT}/A", [provider])

    assert index.shortfall == 0
    assert (len(hits), total) == (1, 1)


def test_the_scan_root_is_normalised_on_the_index_itself():
    """Its reader is the descendants walk's stop condition.

    `_descendants` climbs from each bucket to its ancestors and stops at
    the scan root. With an un-normalised `2026/` that comparison never
    matches, so the walk runs past the root and registers ancestors above
    it — buckets a folder outside the scan would then be offered.
    """
    index = DiscoveryIndex(STORAGE_ID, "2026/", [_hit(f"{ROOT}/A/clip.mp4")])

    assert index.root_path == ROOT
    # The walk stopped AT the root: nothing above it was indexed.
    assert index._descendants.get("") is None
    assert index._descendants[ROOT] == (f"{ROOT}/A",)


def test_a_hit_without_an_explicit_name_falls_back_to_its_basename():
    """The silent under-match this module exists to prevent.

    Without the fallback, `_field(hit, "name")` is MISSING, the extension
    test fails, and the file is simply NOT FOUND — no error, no log line,
    one clip fewer than the legacy run. `_source` projections are exactly
    where a `name` goes missing.
    """
    nameless = {
        "_source": {"path": f"{ROOT}/A/clip.mp4", "parent": f"{ROOT}/A"},
        "sort": [f"{ROOT}/A/clip.mp4", "VX-1"],
    }
    index = DiscoveryIndex(STORAGE_ID, ROOT, [nameless])

    hits = _hits_for(index, f"{ROOT}/A", [_Provider(extensions=[".mp4"])])

    assert _paths(hits) == [f"{ROOT}/A/clip.mp4"]


def test_a_parentless_document_is_dropped_and_counted_as_doubt():
    """A9: the code now does what its comment always claimed.

    Re-deriving `parent` from `path` made such a document matchable HERE
    and nowhere else — legacy's parent filter could not have matched it.
    Dropping it is right; dropping it SILENTLY is not, so it counts
    against the reported total and no folder authorizes descent.
    """
    orphan = {"_source": {"path": f"{ROOT}/A/orphan.mp4", "name": "orphan.mp4"}}
    index = DiscoveryIndex(
        STORAGE_ID, ROOT, [orphan, _hit(f"{ROOT}/A/a.mp4")], reported_total=2
    )

    hits, total = index.hits_for(f"{ROOT}/A", [_Provider(extensions=[".mp4"])])

    assert _paths(hits) == [f"{ROOT}/A/a.mp4"]
    assert index.shortfall == 1
    assert total == 2 > len(hits)


# --------------------------------------------------------------------------
# Per-folder equivalence: the hit set legacy's query would have returned
# --------------------------------------------------------------------------


def test_a_folder_reads_its_own_bucket_filtered_by_extension():
    index = _index(
        [
            f"{ROOT}/A/clip.mp4",
            f"{ROOT}/A/clip.MP4",
            f"{ROOT}/A/notes.txt",
            f"{ROOT}/B/other.mp4",
        ]
    )
    provider = _Provider(extensions=[".mp4"])

    hits = _hits_for(index, f"{ROOT}/A", [provider])

    # Both cases, because `build_search_doc` emits `*.mp4` AND `*.MP4`...
    assert sorted(_paths(hits)) == [f"{ROOT}/A/clip.MP4", f"{ROOT}/A/clip.mp4"]


def test_a_mixed_case_extension_matches_neither_wildcard():
    """Legacy emits exactly two wildcards per extension, and so this does.

    `clip.Mp4` matches neither `*.mp4` nor `*.MP4`, so the index path must
    not match it either — being MORE permissive here would show up as a
    clip the legacy run never found.
    """
    index = _index([f"{ROOT}/A/clip.Mp4"])

    assert _hits_for(index, f"{ROOT}/A", [_Provider(extensions=[".mp4"])]) == []


def test_no_declared_extension_drops_the_clause_rather_than_matching_nothing():
    index = _index([f"{ROOT}/A/clip.anything"])

    assert len(_hits_for(index, f"{ROOT}/A", [_Provider()])) == 1


def test_a_folder_path_with_a_trailing_slash_still_finds_its_bucket():
    index = _index([f"{ROOT}/A/clip.mp4"])

    hits = _hits_for(index, f"{ROOT}/A/", [_Provider(extensions=[".mp4"])])

    assert _paths(hits) == [f"{ROOT}/A/clip.mp4"]


def test_a_provider_subpath_bucket_is_folded_into_its_owning_folder():
    index = _index(
        [
            f"{ROOT}/A/PRIVATE/M4ROOT/Clip/card.mp4",
            f"{ROOT}/A/own.mp4",
            f"{ROOT}/A/PRIVATE/M4ROOT/Sub/elsewhere.mp4",
        ]
    )
    provider = _Provider(
        extensions=[".mp4"],
        subpaths=["((PRIVATE/)?(M4ROOT/|XDROOT/))?(Clip|CLIP)"],
    )

    hits = _hits_for(index, f"{ROOT}/A", [provider])

    assert sorted(_paths(hits)) == [
        f"{ROOT}/A/PRIVATE/M4ROOT/Clip/card.mp4",
        f"{ROOT}/A/own.mp4",
    ]


def test_a_subpath_is_matched_in_full_the_way_an_es_regexp_is():
    """ES `regexp` full-matches the whole keyword; a prefix must not do."""
    index = _index([f"{ROOT}/A/ClipExtra/card.mp4"])
    provider = _Provider(extensions=[".mp4"], subpaths=["Clip"])

    assert _hits_for(index, f"{ROOT}/A", [provider]) == []


def test_a_sibling_folders_bucket_is_never_folded_in():
    index = _index([f"{ROOT}/AB/Clip/card.mp4"])
    provider = _Provider(extensions=[".mp4"], subpaths=["Clip"])

    assert _hits_for(index, f"{ROOT}/A", [provider]) == []


def test_a_raw_filter_is_evaluated_from_the_providers_own_document():
    """red's shape: a parent regexp joined to the folder + a name wildcard."""
    index = _index(
        [
            f"{ROOT}/A/A001_1234AB.RDM/A001_C001_1234AB.RDC/A001_001.R3D",
            f"{ROOT}/A/A001_1234AB.RDM/A001_C001_1234AB.RDC/A001_002.R3D",
            f"{ROOT}/A/elsewhere/A001_001.R3D",
        ]
    )
    provider = _Provider(
        extensions=[],
        filters=[
            {
                "bool": {
                    "must": [
                        {
                            "regexp": {
                                "parent": "{path}/[A-Z][0-9]{3}_[0-9A-Z]{6}.RDM/"
                                "[A-Z][0-9]{3}_[A-Z][0-9]{3}_[0-9A-Z]{6}.RDC"
                            }
                        },
                        {"wildcard": {"name": "*_001.R3D"}},
                    ]
                }
            }
        ],
    )

    hits = _hits_for(index, f"{ROOT}/A", [provider])

    assert _paths(hits) == [
        f"{ROOT}/A/A001_1234AB.RDM/A001_C001_1234AB.RDC/A001_001.R3D"
    ]
    # The provider was handed the ESCAPED folder path, exactly as
    # `build_search_doc` hands it.
    assert provider.filter_paths == [f"{ROOT}/A"]


def test_a_file_matching_both_branches_is_returned_once():
    index = _index([f"{ROOT}/A/clip.mp4"])
    provider = _Provider(
        extensions=[".mp4"],
        filters=[{"wildcard": {"name": "*.mp4"}}],
    )

    assert len(_hits_for(index, f"{ROOT}/A", [provider])) == 1


def test_several_providers_all_contribute_their_subpaths():
    """FR-14: one folder can need several providers; never break on first."""
    index = _index([f"{ROOT}/A/Clip/a.mp4", f"{ROOT}/A/DCIM/100GOPRO/b.mp4"])
    xdcam = _Provider(extensions=[".mp4"], subpaths=["Clip"])
    hdslr = _Provider(extensions=[".mp4"], subpaths=["DCIM/([0-9]{3})(GOPRO)"])

    hits = _hits_for(index, f"{ROOT}/A", [xdcam, hdslr])

    assert sorted(_paths(hits)) == [
        f"{ROOT}/A/Clip/a.mp4",
        f"{ROOT}/A/DCIM/100GOPRO/b.mp4",
    ]


def test_a_descendant_bucket_is_not_reached_without_a_subpath_or_filter():
    """AD-8 by construction: the walk owns descent, discovery does not.

    A plain child folder is its OWN folder for the walk to visit; folding
    it into its parent's hit set here would double every file in the tree.
    """
    index = _index([f"{ROOT}/A/child/clip.mp4"])

    assert _hits_for(index, f"{ROOT}/A", [_Provider(extensions=[".mp4"])]) == []


def test_hits_are_handed_over_in_the_response_shape():
    index = _index([f"{ROOT}/A/clip.mp4"])

    [hit] = _hits_for(index, f"{ROOT}/A", [_Provider(extensions=[".mp4"])])

    assert hit["_source"]["path"] == f"{ROOT}/A/clip.mp4"
    assert set(hit["_source"]) >= {"path", "storage", "id"}


def test_a_directory_document_that_slipped_through_is_bucketed_not_matched():
    """Belt and braces behind `term item_type=file`.

    The filter removes them index-side; if one ever arrived anyway it
    carries a null id, and the name/extension test is what keeps it out
    of a folder's hit set.
    """
    directory = {
        "_source": {
            "path": f"{ROOT}/A/CONTENTS",
            "parent": f"{ROOT}/A",
            "name": "CONTENTS",
            "id": None,
            "item_type": "directory",
        }
    }
    index = DiscoveryIndex(STORAGE_ID, ROOT, [directory, _hit(f"{ROOT}/A/clip.mp4")])

    hits = _hits_for(index, f"{ROOT}/A", [_Provider(extensions=[".mp4"])])

    assert _paths(hits) == [f"{ROOT}/A/clip.mp4"]


def test_the_registry_half_of_the_provider_read_is_computed_once(monkeypatch):
    """B2: `getExtensions()`/`getSubPaths()` do not depend on the folder.

    Recomputing (and re-`re.compile`-ing) them for each of ~4,000 folders
    is pure waste; only `getFilters(escaped_path)` is genuinely per-folder.
    """
    index = _index([f"{ROOT}/A/a.mp4", f"{ROOT}/B/b.mp4", f"{ROOT}/C/c.mp4"])
    provider = _Provider(extensions=[".mp4"], subpaths=["Clip"])
    calls = {"extensions": 0, "subpaths": 0}
    monkeypatch.setattr(
        provider,
        "getExtensions",
        lambda: calls.__setitem__("extensions", calls["extensions"] + 1) or [".mp4"],
    )
    monkeypatch.setattr(
        provider,
        "getSubPaths",
        lambda: calls.__setitem__("subpaths", calls["subpaths"] + 1) or ["Clip"],
    )

    for folder in ("A", "B", "C"):
        index.hits_for(f"{ROOT}/{folder}", [provider])

    assert calls == {"extensions": 1, "subpaths": 1}
    # ...while the folder-dependent half really is asked every time.
    assert provider.filter_paths == [f"{ROOT}/A", f"{ROOT}/B", f"{ROOT}/C"]

    # And the memo is keyed by the REGISTRY, not shared across registries:
    # a second provider list on the same index must get its OWN
    # declarations, or one paged caller's provider filter would silently
    # answer another's scan. (`.mp4` above; `.txt` here.)
    other = _Provider(extensions=[".txt"])
    index_two = _index([f"{ROOT}/A/a.mp4", f"{ROOT}/A/note.txt"])
    assert _paths(_hits_for(index_two, f"{ROOT}/A", [provider])) == [f"{ROOT}/A/a.mp4"]
    assert _paths(_hits_for(index_two, f"{ROOT}/A", [other])) == [f"{ROOT}/A/note.txt"]


# --------------------------------------------------------------------------
# The provider contract, broken loudly rather than quietly
# --------------------------------------------------------------------------


def test_an_uncompilable_subpath_fails_the_folder():
    index = _index([f"{ROOT}/A/clip.mp4"])

    with pytest.raises(ValueError, match="does not compile"):
        index.hits_for(f"{ROOT}/A", [_Provider(subpaths=["Clip(["])])


def test_an_unmodelled_raw_filter_clause_fails_the_folder():
    index = _index([f"{ROOT}/A/clip.mp4"])
    provider = _Provider(filters=[{"fuzzy": {"name": "clip"}}])

    with pytest.raises(ValueError, match="fuzzy"):
        index.hits_for(f"{ROOT}/A", [provider])


# Every Lucene-only operator, paired with the character the error must
# NAME. Pinning the character (not just the words "Lucene-only") is what
# keeps each row self-identifying: `"Clip<1-9>"` trips on its `<`, so a
# `>`-shaped row that only asserted the phrase would pass whether or not
# `>` were in the constant at all.
LUCENE_ONLY = [
    ("@", "@"),
    ("#", "#"),
    ("&", "&"),
    ("~", "~"),
    ("<1-9>", "<"),
    ("Clip>x", ">"),
]


@pytest.mark.parametrize("fragment,offender", LUCENE_ONLY)
def test_a_lucene_only_subpath_operator_is_refused_rather_than_mis_read(
    fragment, offender
):
    """Python `re` reads all of them as literals; Lucene does not."""
    index = _index([f"{ROOT}/A/clip.mp4"])

    with pytest.raises(ValueError, match=f"Lucene-only regexp operator '{offender}'"):
        index.hits_for(f"{ROOT}/A", [_Provider(subpaths=[f"Clip{fragment}"])])


@pytest.mark.parametrize("fragment,offender", LUCENE_ONLY)
def test_a_lucene_only_raw_filter_operator_is_refused_rather_than_mis_read(
    fragment, offender
):
    """The OTHER call site, and the one that actually bites in production.

    A provider's raw filter interpolates `re.escape(folder_path)`, so the
    pattern's content is FOLDER-NAME-derived — and `re.escape` leaves
    `@`, `<` and `>` bare. A shoot folder literally named `shoot@paris`
    therefore makes legacy's Lucene `regexp` match EVERY document in the
    index while a Python full-match matches only the literal. A
    sub-path's content, by contrast, is fixed provider source.
    """
    index = _index([f"{ROOT}/A/clip.mp4"])
    provider = _Provider(filters=[{"regexp": {"parent": f"{{path}}/Clip{fragment}"}}])

    with pytest.raises(ValueError, match=f"Lucene-only regexp operator '{offender}'"):
        index.hits_for(f"{ROOT}/A", [provider])


def test_an_at_sign_in_the_folder_name_itself_is_refused():
    """The whole reason the raw-filter call site is guarded.

    Nothing in the provider's source is unusual here — `red`'s filter
    template is fine. It is `re.escape("2026/shoot@paris")` leaving the
    `@` bare that turns a well-formed filter into a Lucene "match any
    string" the moment the folder is named that way.
    """
    index = _index([f"{ROOT}/shoot@paris/clip.mp4"])
    provider = _Provider(filters=[{"regexp": {"parent": "{path}/CLIP"}}])

    with pytest.raises(ValueError, match="Lucene-only regexp operator '@'"):
        index.hits_for(f"{ROOT}/shoot@paris", [provider])


def test_a_lucene_operator_inside_a_character_class_is_shared_dialect():
    """`[^/]*` and `[@#]` mean the same thing in both dialects."""
    index = _index([f"{ROOT}/A/C@rd/clip.mp4"])
    provider = _Provider(extensions=[".mp4"], subpaths=["C[@]rd"])

    assert len(_hits_for(index, f"{ROOT}/A", [provider])) == 1


def test_every_shipped_provider_subpath_survives_the_dialect_check():
    """The guard must not reject the registry it exists to serve."""
    from portal.plugins.TapelessIngest.scan.adapters import build_provider_registry

    index = _index([f"{ROOT}/A/clip.mp4"])

    # No raise: every shipped getSubPaths() is shared-dialect and compiles.
    index.hits_for(f"{ROOT}/A", build_provider_registry())


@pytest.mark.parametrize(
    "clause,expected",
    [
        ({"bool": {"must": [], "minimum_should_match": 1}}, "minimum_should_match"),
        ({"bool": {"should": [{"term": {"name": "a"}}], "boost": 2}}, "boost"),
        ({"bool": {}}, "empty bool"),
        ({"bool": []}, "expected a mapping"),
        ({"term": {"name": {"value": "a", "case_insensitive": True}}}, "case_insensi"),
        ({"regexp": {"name": {"value": "a.*", "flags": "ALL"}}}, "flags"),
        ({"wildcard": {"name": {"boost": 2}}}, "boost"),
        ({"term": {"metadata.card_id": "X"}}, "_source projection"),
        ({"regexp": {"name": 7}}, "expected a string pattern"),
        ({"term": {"name": ["a"]}}, "expected a scalar"),
        # Shape guards. Removing any of these degrades a NAMED error into
        # an IndexError or an unpacking failure — `process_folder` still
        # fails the folder safely either way, so this is about the
        # diagnostic an operator reads at 3am, not about the fail-safe.
        ({"term": {"name": {}}}, "no value key"),
        (
            {"term": {"name": "a"}, "prefix": {"name": "b"}},
            "expected a single-key query clause",
        ),
        ({"term": {"name": "a", "path": "b"}}, "expected a single field"),
        ({"bool": {"must": "not-a-list"}}, "expected a query clause or a list"),
    ],
)
def test_the_evaluator_refuses_what_it_cannot_reproduce(clause, expected):
    """A6: raise, never approximate.

    Dropping an option evaluates a DIFFERENT clause than legacy sends;
    an empty bool is an always-true predicate that would claim every file
    under the folder for the provider that shipped it.
    """
    index = _index([f"{ROOT}/A/clip.mp4"])

    with pytest.raises(ValueError, match=expected):
        index.hits_for(f"{ROOT}/A", [_Provider(filters=[clause])])


def test_the_object_spelling_of_a_wildcard_uses_its_own_key():
    """ES spells this one `{"wildcard": {"name": {"wildcard": "..."}}}`.

    Reading `value` there would have raised "no value key" on a perfectly
    legal clause.
    """
    index = _index([f"{ROOT}/A/A001_001.R3D", f"{ROOT}/A/A001_002.R3D"])
    # A declared extension nothing here carries, so ONLY the raw filter
    # can select — otherwise the parent+extension branch answers first.
    provider = _Provider(
        extensions=[".mp4"],
        filters=[{"wildcard": {"name": {"wildcard": "*_001.R3D"}}}],
    )

    hits = _hits_for(index, f"{ROOT}/A", [provider])

    assert _paths(hits) == [f"{ROOT}/A/A001_001.R3D"]


def test_a_term_filter_accepts_a_legal_non_string_value():
    index = DiscoveryIndex(
        STORAGE_ID,
        ROOT,
        [_hit(f"{ROOT}/A/empty.mp4", size=0), _hit(f"{ROOT}/A/full.mp4", size=1024)],
    )
    provider = _Provider(extensions=[".nope"], filters=[{"term": {"size": 0}}])

    assert _paths(_hits_for(index, f"{ROOT}/A", [provider])) == [f"{ROOT}/A/empty.mp4"]


@pytest.mark.parametrize(
    "clause",
    [
        {"regexp": {"state": ".*"}},
        {"wildcard": {"state": "*"}},
        {"prefix": {"state": ""}},
    ],
)
def test_an_absent_field_never_matches(clause):
    """A4: coercing a missing field to `""` matched documents the index
    would have excluded — these three patterns match every string, and
    `""` is a string."""
    index = _index([f"{ROOT}/A/clip.mp4"])  # no `state` key at all
    # `.nope` keeps the parent+extension branch out of it: only the raw
    # filter can select this file, and it must not.
    provider = _Provider(extensions=[".nope"], filters=[clause])

    assert _hits_for(index, f"{ROOT}/A", [provider]) == []


def test_a_present_field_still_matches_the_same_patterns():
    """...and the guard above must not have broken the ordinary case."""
    index = DiscoveryIndex(STORAGE_ID, ROOT, [_hit(f"{ROOT}/A/c.mp4", state="OK")])

    for clause in ({"regexp": {"state": ".*"}}, {"prefix": {"state": ""}}):
        provider = _Provider(extensions=[".nope"], filters=[clause])
        assert len(_hits_for(index, f"{ROOT}/A", [provider])) == 1


def test_field_reads_traverse_a_dotted_name():
    """A5: `metadata.card_id` used to silently read as None.

    (The projection refuses `metadata.*` at compile time today, so this
    pins the resolver itself — the piece that makes widening the
    projection a one-line change rather than a silent-loss bug.)
    """
    from portal.plugins.TapelessIngest.scan.discovery import _field

    hit = {"_source": {"path": "a", "nested": {"deep": {"key": "found"}}}}

    assert _field(hit, "nested.deep.key") == "found"
    assert _field(hit, "nested.deep.absent") is MISSING
    assert _field(hit, "path.deeper") is MISSING
    assert _field(hit, "absent") is MISSING


@pytest.mark.parametrize(
    "hit,expected",
    [
        ("not-a-hit", "where a hit document was expected"),
        ({"_source": "not-a-doc"}, "_source is str"),
        ({"_source": {"parent": 7, "path": "a"}}, "parent is 7"),
        ({"_source": {"parent": "2026/A", "path": 7}}, "path is 7"),
    ],
)
def test_a_malformed_hit_raises_a_clear_error(hit, expected):
    """A7: these used to reach `os.path.dirname` and raise `TypeError`
    straight past the caller's `ValueError` handling."""
    with pytest.raises(ValueError, match=expected):
        DiscoveryIndex(STORAGE_ID, ROOT, [hit])


# --------------------------------------------------------------------------
# The switch itself
# --------------------------------------------------------------------------


def test_the_default_is_legacy_and_the_modes_are_the_two():
    assert DISCOVERY_MODES == ("legacy", "index")
    assert DEFAULT_DISCOVERY == "legacy"
    assert RunOptions().discovery == "legacy"
    assert RunOptions().discovery_page_size == DEFAULT_DISCOVERY_PAGE_SIZE == 500


@pytest.mark.parametrize("value", ["INDEX", "elastic", "", None, 1])
def test_an_unknown_discovery_mode_is_unconstructable(value):
    with pytest.raises(ValueError, match="discovery must be one of"):
        RunOptions(discovery=value)


@pytest.mark.parametrize("value", [0, -1, True, "500", None])
def test_a_defective_page_size_is_unconstructable(value):
    with pytest.raises(ValueError, match="discovery_page_size"):
        RunOptions(discovery_page_size=value)


def test_a_page_size_above_the_result_window_is_unconstructable():
    RunOptions(discovery_page_size=MAX_DISCOVERY_PAGE_SIZE)
    with pytest.raises(ValueError, match="discovery_page_size must be <="):
        RunOptions(discovery_page_size=MAX_DISCOVERY_PAGE_SIZE + 1)


def test_index_discovery_is_rejected_in_paged_mode():
    """AD-14, and NOT via TREE_ONLY_OPTIONS: "legacy" is truthy."""
    options = RunOptions(discovery=DISCOVERY_INDEX)

    with pytest.raises(ValueError, match="cannot be used in paged mode"):
        assert_mode_options(options, "paged")
    # ...and tree mode is exactly where it IS allowed.
    assert_mode_options(options, "tree")
    assert_mode_options(RunOptions(), "paged")


def test_the_prefetched_index_rides_on_the_context():
    ctx = ScanContext(
        storages={STORAGE_ID: StorageInfo(id=STORAGE_ID, root_path="/root")},
        options=RunOptions(discovery=DISCOVERY_INDEX),
    )

    assert ctx.discovery_index is None
    # And the slot is still a FROZEN field: attaching one means replacing
    # the context, never mutating it under the workers (AD-4).
    with pytest.raises(Exception):
        ctx.discovery_index = _index([])

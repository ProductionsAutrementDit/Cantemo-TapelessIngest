"""Index discovery — the second path behind ``--discovery`` (story 4.1).

Stdlib-only by contract (AD-1), like ``scan.context``, ``scan.extraction``
and ``scan.coordinator``: this module must import in a bare interpreter
with no Portal stub installed. It never imports ``portal.*``, never opens
a ``Folder`` and never touches the ORM — the caller injects
``query_elastic`` and the already-built provider list.

**The legacy path is not here and must never be.** ``Folder`` keeps
querying through ``build_search_doc`` byte for byte (AD-3's
grandfathering, pinned by ``tests/tier1/test_golden_search_doc.py``);
nothing in this module is reachable unless ``--discovery=index`` is passed
in tree mode.

What this replaces
------------------
Legacy discovery issues ONE query PER FOLDER — ~4,000 queries for the 2026
tree's 3,935 directories — in the expensive form AD-3 bans (``regexp`` on
``parent``, paired leading-wildcard ``*.mp4``/``*.MP4`` on ``name``), and
pages it with ``from``/``size`` over an UNSORTED query, so a document can
be skipped or returned twice when index order shifts mid-scan and the
10,000-result window is a hard ceiling.

This path issues ONE query stream per SCAN ROOT:

* ``filter`` = ``term storage`` + the scan-root ``parent`` scope +
  ``term item_type=file`` + legacy's ``must_not`` on ``state``
  LOST/MISSING;
* ``sort`` = ``[{"path": "asc"}, {"id": "asc"}]`` — ``path`` gives the
  top-down order AD-8 needs, ``id`` is the unique tiebreaker;
* ``track_total_hits: true``, so the reported total is EXACT rather than
  capped at 10,000 — the completeness arithmetic below depends on it;
* ``_source`` projected down to ``INDEX_SOURCE_FIELDS``, so a 188k-clip
  scan root is not held in memory with every field it happens to carry;
* pagination is ``search_after`` carrying the previous page's ``sort``
  values. ``from``/``size`` beyond page one is forbidden.

Why ``term item_type=file`` is load-bearing
-------------------------------------------
``portal_file`` holds DIRECTORY documents too (measured on prod
2026-08-27: 211 of 1,708 in the sampled subtree), and those carry
``id: null``. A null tiebreaker breaks ``search_after``'s
no-skip/no-duplicate guarantee, which is the whole point of the change.
Filtering them out index-side is what makes ``id`` a real tiebreaker
(measured afterwards: 0 null ids, 500/500 unique). Legacy escapes this by
accident — a directory never matches its ``*.ext`` wildcard.

Completeness is arithmetic, not an assumption
---------------------------------------------
``Folder._scan_pass`` refuses to authorize descent unless it consumed at
least as many hits as the index REPORTED for that folder (NFR-1's
under-consumption direction). Answering that question with
``len(hits_for(...))`` would make it tautological, so the stream's own
``hits.total.value`` travels onto ``DiscoveryIndex``: any gap between it
and the number of hits actually bucketed becomes a ``shortfall`` that is
added to EVERY folder's reported total, and descent is refused
everywhere. ``prefetch_index`` refuses to build a short index at all;
the shortfall exists so that a ``DiscoveryIndex`` built any other way
still fails safe rather than silently vouching for a truncated tree.

Per-folder equivalence
----------------------
``DiscoveryIndex.hits_for(folder_path, provider_list)`` returns what
legacy's query for that folder would have returned, computed against the
prefetched buckets instead of the index:

    (parent matches  AND  name matches an extension)  OR  a raw filter

with ``parent matches`` being the folder's OWN bucket plus every bucket
whose path, relative to the folder, full-matches one of the providers'
``getSubPaths()`` patterns. The providers' own methods are the single
source of truth — ``getExtensions()``, ``getSubPaths()`` and
``getFilters()`` are read here exactly as ``build_search_doc`` reads them,
never re-derived into a list of our own.

Extension and filename filtering is therefore CLIENT-SIDE, which is AD-3's
sanctioned default (index-side ``case_insensitive`` wildcards stay
deferred: the bug was fixed only in OpenSearch 2.19 and prod runs 2.17).

The regexp dialect contract
---------------------------
A provider's ``getSubPaths()`` entries and the ``regexp`` values inside
its ``getFilters()`` are LUCENE regular expressions when legacy sends them
to OpenSearch, and PYTHON regular expressions when this module evaluates
them. The two dialects agree on everything the shipped providers use, but
they disagree on five Lucene-only operators — ``@`` (match any string),
``#`` (match empty), ``&`` (intersection), ``~`` (complement) and
``<n-m>`` (numeric range) — which Python reads as literals. Worse,
``re.escape`` leaves ``@``, ``<`` and ``>`` bare, so a folder literally
named ``shoot@paris`` makes legacy's ``regexp`` match EVERYTHING while a
Python full-match matches only the literal. Rather than silently
disagreeing, ``_reject_lucene_only`` raises on any pattern carrying one
of those operators outside a character class, and the folder fails
loudly. See ``deferred-work.md`` for the triage this leaves open.

AD-8 is not forked
------------------
Nothing here knows about consumed subdirs. The walk still drives descent,
so a consumed subdir's bucket is simply never requested — which IS AD-8's
"files inside a consumed subdir of an ancestor bucket are dropped", by
construction rather than by a second implementation of the predicate.
"""

import fnmatch
import os
import re

# The sort AD-3 requires: a top-down order plus a tiebreaker the
# ``item_type`` filter guarantees is non-null. Both fields are `keyword`
# in the Portal-owned mapping (evidence:
# _bmad-output/implementation-artifacts/prod-portal_file-mapping-2026-08-26.json),
# so neither needs a `.keyword` sub-field and no mapping change is implied.
INDEX_SORT = ({"path": "asc"}, {"id": "asc"})

# The doc types the stream asks for. Same value legacy passes, so the two
# paths read the same index.
INDEX_DOC_TYPE = ("file",)

# The ONLY `_source` keys anything downstream reads: `VSFile` takes
# path/hash/storage/id/size (mapping verified on prod 2026-08-20), and
# discovery itself reads parent/name/path/id plus the two filtered
# fields. Projecting is a cost measure (B1) AND a contract: a provider
# raw filter naming a field outside this list is refused at compile time
# rather than evaluated against a key the projection removed.
INDEX_SOURCE_FIELDS = (
    "path",
    "parent",
    "name",
    "id",
    "storage",
    "hash",
    "size",
    "state",
    "item_type",
)

# The query clauses that never depend on the scan root.
_ITEM_TYPE_FILE = {"term": {"item_type": "file"}}
_NOT_LOST_OR_MISSING = {
    "bool": {"must_not": [{"term": {"state": "LOST"}}, {"term": {"state": "MISSING"}}]}
}

# The ES clause kinds ``_compile_clause`` understands. A provider's
# ``getFilters()`` returns raw query documents that legacy hands straight
# to OpenSearch; evaluating them client-side means interpreting them, and
# an unsupported form must FAIL rather than quietly match nothing (which
# would drop that provider's clips) or quietly match everything (which
# would hand foreign files to it).
_LEAF_KINDS = ("term", "regexp", "wildcard", "prefix")

# The `bool` keys this evaluator models. `minimum_should_match`, `boost`
# and friends CHANGE what a clause matches (or, for boost, prove the
# author expected scoring semantics this filter context does not have),
# so an unmodelled key is refused rather than dropped.
_BOOL_KEYS = ("must", "filter", "should", "must_not")

# Per-kind spelling of a leaf's value in its object form. ES accepts both
# `{"wildcard": {"name": "*.R3D"}}` and
# `{"wildcard": {"name": {"wildcard": "*.R3D"}}}` — note the inner key is
# `wildcard`, not `value`, for that one kind.
_LEAF_VALUE_KEYS = {
    "term": ("value",),
    "prefix": ("value",),
    "regexp": ("value",),
    "wildcard": ("wildcard", "value"),
}

# Lucene regexp operators Python's `re` reads as literals. See "The
# regexp dialect contract" in the module docstring.
_LUCENE_ONLY_OPERATORS = "@#&~<>"

# Safety valve for the paging loop. A responder that answers the same
# full page forever — a broken cursor, a proxy replaying a cached
# response — must fail loudly rather than fill memory for hours. Both
# ceilings are far above any real scan root (the 2026 tree holds 65,024
# files) and exist only to convert a hang into an error.
MAX_INDEX_PAGES = 100_000
MAX_INDEX_HITS = 5_000_000


class ShortIndexError(ValueError):
    """The stream returned fewer documents than the index reported.

    A ``ValueError`` so the caller's existing handling covers it, and a
    type of its own so a caller that wants to distinguish "the index is
    inconsistent" from "this provider's filter is unreadable" can.
    """


def normalize_path(path):
    """A storage-root-relative path in the form ``parent`` is indexed in.

    Trailing separators are stripped, so a scan root passed as ``2026/``
    still matches the ``parent`` values ``2026`` and ``2026/...``. Before
    this, a trailing slash made BOTH root-scope clauses miss and the run
    reported an empty tree and exited clean — the worst possible failure
    mode for a nightly job. ``/`` and ``""`` both mean "no narrowing".
    """
    if not path:
        return ""
    return path.rstrip("/")


def build_index_search_doc(storage_id, root_path, *, search_after=None):
    """The index path's query document for one page of one scan root.

    ``search_after`` is the previous page's last hit's ``sort`` values.
    Both it and ``sort`` are placed IN the search doc: verified end to end
    on prod (2026-08-27) to survive
    ``query_elastic(query, first=, number=, doc_type=, **kwargs)``.

    Note what is NOT here: no ``regexp``, no leading wildcard, no ``from``.
    Paging is the caller's ``number=`` (the page SIZE) plus the cursor
    above; ``first`` stays 0 on every page (AD-3).
    """
    scope = _root_scope(root_path)
    filters = [{"term": {"storage": storage_id}}]
    if scope is not None:
        filters.append(scope)
    filters.append(_ITEM_TYPE_FILE)
    filters.append(_NOT_LOST_OR_MISSING)
    doc = {
        "query": {"bool": {"filter": filters}},
        "sort": [dict(entry) for entry in INDEX_SORT],
        # EXACT, not the default 10,000 cap: the completeness arithmetic
        # subtracts what was bucketed from what was reported, and a
        # capped total would make a 65,024-file scan root report a
        # negative shortfall — i.e. would vouch for a truncated tree.
        "track_total_hits": True,
        "_source": list(INDEX_SOURCE_FIELDS),
    }
    if search_after is not None:
        doc["search_after"] = list(search_after)
    return doc


def _root_scope(root_path):
    """``parent`` scoped to the scan root: the root itself, or below it.

    A bare ``prefix`` on the root path would be WIDER than the subtree —
    ``2026`` also prefixes ``2026bis`` — and a ``prefix`` on ``root + "/"``
    would miss the root folder's own files, which legacy's exact
    ``regexp`` on ``parent`` does return. The two-clause ``should`` is the
    prefix scoped correctly; a ``bool`` holding only ``should`` clauses
    defaults to ``minimum_should_match: 1``, so this is an OR.

    ``None`` for an empty root (or a bare ``/``) — the whole storage is in
    scope and there is nothing to narrow.
    """
    root_path = normalize_path(root_path)
    if not root_path:
        return None
    return {
        "bool": {
            "should": [
                {"term": {"parent": root_path}},
                {"prefix": {"parent": root_path + "/"}},
            ]
        }
    }


def fetch_index_hits(query_elastic, storage_id, root_path, *, page_size):
    """Every hit in one scan root, as ``search_after`` pages.

    Returns ``(hits, reported_total)``: the raw hit dicts in index sort
    order, and the total the FIRST response reported (``track_total_hits``
    makes it exact). The caller compares the two — see ``prefetch_index``.

    The loop ends on the first SHORT page, so a last page that is exactly
    full costs one extra (empty) fetch — the same shape as legacy's page
    loop, and the only form that cannot end a page early on a boundary.

    Everything that could turn this loop into a hang or a silent
    truncation raises instead: a response that is not a search result, a
    full page whose last hit carries no usable cursor, a cursor that does
    not strictly advance, and either ceiling in ``MAX_INDEX_PAGES`` /
    ``MAX_INDEX_HITS``.
    """
    if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1:
        raise ValueError(f"discovery page size must be an int >= 1 (got {page_size!r})")
    hits = []
    search_after = None
    reported_total = None
    pages = 0
    while True:
        doc = build_index_search_doc(storage_id, root_path, search_after=search_after)
        result = query_elastic(
            doc,
            doc_type=list(INDEX_DOC_TYPE),
            first=0,
            number=page_size,
        )
        page, total = _read_page(result, root_path)
        if reported_total is None:
            reported_total = total
        hits.extend(page)
        pages += 1
        if len(page) < page_size:
            return hits, reported_total
        if pages >= MAX_INDEX_PAGES:
            raise ValueError(
                f"index discovery gave up on {root_path!r} after "
                f"{pages} pages of {page_size}: the stream is not ending"
            )
        if len(hits) > MAX_INDEX_HITS:
            raise ValueError(
                f"index discovery gave up on {root_path!r} after "
                f"{len(hits)} hits (ceiling {MAX_INDEX_HITS}): refusing to "
                f"materialise a scan root this large in memory"
            )
        cursor = _read_cursor(page[-1], root_path)
        if cursor == search_after:
            # A cursor that repeats is an infinite loop with the memory
            # cost of a real scan. It means the responder ignored
            # `search_after`, or two documents share a full sort key —
            # which `term item_type=file` is supposed to make impossible.
            raise ValueError(
                f"index discovery cannot page past {cursor!r} in "
                f"{root_path!r}: the cursor did not advance, so the stream "
                f"would repeat forever"
            )
        search_after = cursor


def _read_page(result, root_path):
    """``(hits, total)`` out of one response, or a clear ``ValueError``.

    ``query_elastic`` is injected, so this cannot assume a well-formed
    search result: a ``None``, an error envelope or a transport-shaped
    dict used to raise ``KeyError``/``TypeError`` straight past the
    caller's ``ValueError`` handling and out of the run.

    ``total`` is REQUIRED and must be exact. A missing, non-integer or
    ``relation: "gte"`` total is refused rather than softened into "no
    shortfall" — see the two comments below.
    """
    if not isinstance(result, dict):
        raise ValueError(
            f"index discovery got {type(result).__name__} instead of a search "
            f"result while paging {root_path!r}"
        )
    body = result.get("hits")
    if not isinstance(body, dict):
        raise ValueError(
            f"index discovery got a response with no 'hits' body while "
            f"paging {root_path!r}: {sorted(result)!r}"
        )
    page = body.get("hits")
    if not isinstance(page, list):
        raise ValueError(
            f"index discovery got a response whose 'hits.hits' is "
            f"{type(page).__name__}, not a list, while paging {root_path!r}"
        )
    total = body.get("total")
    relation = None
    if isinstance(total, dict):
        # The modern spelling, and the only one that carries `relation`.
        # A bare int is ES 6.x's and is accepted — OpenSearch 2.x never
        # sends it, so it can only come from a proxy or a double, and it
        # is still an authoritative count as far as anything here can
        # tell. `shortfall`'s clamp is what stops a capped bare int from
        # going negative.
        relation = total.get("relation")
        total = total.get("value")
    if not isinstance(total, int) or isinstance(total, bool):
        # REFUSE, never "assume no shortfall". The completeness check
        # subtracts what was bucketed from what was reported; with no
        # reported number there is nothing to subtract, and defaulting to
        # `len(hits)` would make a truncated stream sail through the
        # ShortIndexError gate and let every folder authorize descent on
        # a partial view of its own contents.
        raise ValueError(
            f"index discovery got a response whose 'hits.total' is "
            f"{body.get('total')!r} while paging {root_path!r}: without an "
            f"exact count it cannot tell a complete stream from a truncated "
            f"one, and must not vouch for the tree"
        )
    if relation is not None and relation != "eq":
        # `gte` means OpenSearch STOPPED COUNTING — the `track_total_hits`
        # this query sets was ignored, dropped by a proxy, or overridden
        # cluster-side. The value is then a floor, not a count, and
        # `hit_count` legitimately exceeds it: `shortfall` would clamp to
        # zero and silently vouch for a tree nothing counted.
        raise ValueError(
            f"index discovery got a non-exact total "
            f"(relation={relation!r}, value={total}) while paging "
            f"{root_path!r}: track_total_hits was not honoured, so the "
            f"count is a floor and cannot answer the completeness check"
        )
    return page, total


def _read_cursor(hit, root_path):
    """The ``sort`` values to page past ``hit``, or a clear ``ValueError``.

    A ``None`` inside the cursor is refused rather than sent: OpenSearch
    cannot compare against a null sort value, so it is the exact shape
    that silently skips or duplicates documents — which is what
    ``term item_type=file`` exists to prevent and what this catches if it
    ever fails to.
    """
    if not isinstance(hit, dict):
        raise ValueError(
            f"index discovery cannot page {root_path!r}: the last hit of a "
            f"full page is {type(hit).__name__}, not a document"
        )
    cursor = hit.get("sort")
    path = (hit.get("_source") or {}).get("path") if isinstance(hit, dict) else None
    if not cursor or not isinstance(cursor, (list, tuple)):
        raise ValueError(
            f"index discovery cannot page past hit {path!r} in {root_path!r}: "
            f"the response carries no sort values, so search_after has no "
            f"cursor and from/size paging is forbidden (AD-3)"
        )
    cursor = list(cursor)
    if any(value is None for value in cursor):
        raise ValueError(
            f"index discovery cannot page past hit {path!r} in {root_path!r}: "
            f"the sort values {cursor!r} contain a null, which search_after "
            f"cannot compare against without skipping or duplicating documents"
        )
    return cursor


def prefetch_index(query_elastic, storage_id, root_path, *, page_size):
    """Fetch one scan root and bucket it — the whole prefetch, once per run.

    Raises ``ShortIndexError`` when the stream came back with fewer
    documents than the index reported. Every folder of the run would
    otherwise read a truncated scan root and report a small, plausible
    tree; the run must die instead, naming the gap.
    """
    hits, reported_total = fetch_index_hits(
        query_elastic, storage_id, root_path, page_size=page_size
    )
    index = DiscoveryIndex(storage_id, root_path, hits, reported_total=reported_total)
    if index.shortfall:
        raise ShortIndexError(
            f"index discovery fetched {index.hit_count} usable file(s) under "
            f"{normalize_path(root_path)!r} but the index reported "
            f"{index.reported_total}: refusing to scan a truncated view of "
            f"the tree"
        )
    return index


class DiscoveryIndex:
    """One scan root's hits, bucketed by ``parent``, read-only after build.

    Built ONCE per run in ``Folder.scan_tree`` before fan-out and then
    only read, which is what keeps ``ScanContext`` immutable under the
    pool (AD-4): ``hits_for`` mutates nothing a caller can observe, so
    any number of worker threads may call it at once. (The one write is
    the idempotent per-registry filter memo below, whose value is a pure
    function of its key.)

    ``_descendants`` maps every ancestor path inside the scan root to the
    buckets below it, so a folder never scans the whole bucket table to
    find its provider sub-paths. **That INDEX is bounded by
    ``buckets x path depth``; the COST OF USING IT is not.** A folder with
    any raw filter in play must consider every hit below it — the
    descendant buckets can be skipped only when neither a sub-path
    pattern nor a raw filter could reach into them — so total work
    degrades toward ``hits x depth`` whenever a provider ships a raw
    filter, which ``red`` always does. Recorded in ``deferred-work.md``.
    """

    __slots__ = (
        "storage_id",
        "root_path",
        "reported_total",
        "_buckets",
        "_descendants",
        "_hit_count",
        "_filter_memo",
    )

    def __init__(self, storage_id, root_path, hits, *, reported_total=None):
        self.storage_id = storage_id
        self.root_path = normalize_path(root_path)
        buckets = {}
        count = 0
        for hit in hits:
            parent = _hit_parent(hit)
            if parent is None:
                # A document with no `parent` belongs to no folder, and
                # legacy's parent filter could not have matched it either
                # — so it is DROPPED rather than re-derived from `path`,
                # which would make it matchable here and nowhere else.
                # It still counts against `reported_total` below, so the
                # gap becomes doubt instead of a silent loss.
                continue
            buckets.setdefault(parent, []).append(hit)
            count += 1
        descendants = {}
        for parent in buckets:
            node = os.path.dirname(parent)
            while node:
                descendants.setdefault(node, []).append(parent)
                if node == self.root_path:
                    break
                nxt = os.path.dirname(node)
                if nxt == node:
                    break
                node = nxt
        self._buckets = {parent: tuple(group) for parent, group in buckets.items()}
        self._descendants = {
            node: tuple(sorted(children)) for node, children in descendants.items()
        }
        self._hit_count = count
        self.reported_total = count if reported_total is None else reported_total
        self._filter_memo = {}

    def __repr__(self):
        return (
            f"DiscoveryIndex({self.storage_id!r}, {self.root_path!r}, "
            f"{self.hit_count} hits in {len(self._buckets)} buckets)"
        )

    @property
    def hit_count(self):
        """Bucketed hits — the whole scan root, every bucket."""
        return self._hit_count

    @property
    def bucket_count(self):
        return len(self._buckets)

    @property
    def shortfall(self):
        """Documents the index reported and this view does not hold.

        Zero for every index ``prefetch_index`` is willing to return.
        Non-zero means the stream was truncated, or documents were
        dropped for having no ``parent`` — either way this view cannot
        vouch for any folder's completeness, so it is added to EVERY
        folder's reported total and no folder authorizes descent.

        Clamped at zero: a reported total BELOW the bucketed count is a
        lying total (a cap that `_read_page`'s `relation` check did not
        see, because the response used the bare-int spelling), and a
        negative shortfall would SUBTRACT from every folder's total —
        making the completeness check easier to satisfy than it should
        be, in the under-consumption direction NFR-1 is about.
        """
        return max(0, self.reported_total - self._hit_count)

    def bucket(self, folder_path):
        """One folder's OWN hits, unfiltered. Empty tuple on a miss."""
        return self._buckets.get(normalize_path(folder_path), ())

    def hits_for(self, folder_path, provider_list):
        """``(hits, total)`` for the folder, as legacy's query would answer.

        ``hits`` have the shape of a real response's
        (``{"_source": {...}}`` dicts) and are handed back unsorted — the
        caller applies the same per-page ``sorted()`` by path it always
        has (AD-7).

        ``total`` is what the folder must have CONSUMED to authorize
        descent. It is ``len(hits)`` plus the whole index's ``shortfall``,
        so a truncated or lossy prefetch makes every folder fall short of
        its own total and refuse descent (NFR-1's safe direction) instead
        of comparing a list against its own length.

        Raises ``ValueError`` when a provider's ``getSubPaths()`` entry or
        ``getFilters()`` clause cannot be evaluated faithfully — it does
        not compile, it uses a Lucene-only operator, it names a field
        outside the ``_source`` projection, or it is a form this
        evaluator does not model. Each is the provider contract being
        broken in a way that would silently change which files this
        folder finds; the caller turns it into a failed folder rather
        than a quietly short one.
        """
        folder_path = normalize_path(folder_path)
        suffixes, patterns = self._registry_filters(provider_list)
        matchers = _compile_raw_filters(folder_path, provider_list)
        selected = []
        for parent in self._candidate_parents(folder_path, patterns, matchers):
            parent_ok = parent == folder_path or _subpath_match(
                patterns, folder_path, parent
            )
            if not parent_ok and not matchers:
                continue
            for hit in self._buckets[parent]:
                if parent_ok and _name_matches(hit, suffixes):
                    selected.append(hit)
                elif matchers and any(matcher(hit) for matcher in matchers):
                    selected.append(hit)
        return selected, len(selected) + self.shortfall

    def _candidate_parents(self, folder_path, patterns, matchers):
        """The buckets a folder's query could possibly draw from.

        Its own, always; plus every bucket below it — but only when there
        is something that could reach down there, i.e. a sub-path pattern
        or a raw filter. With neither (the common ``file``-only case) a
        folder's query is exactly its own bucket, and walking the subtree
        would be pure cost.
        """
        parents = []
        if folder_path in self._buckets:
            parents.append(folder_path)
        if patterns or matchers:
            parents.extend(self._descendants.get(folder_path, ()))
        return parents

    def _registry_filters(self, provider_list):
        """The folder-INDEPENDENT half of the providers' declarations.

        ``getExtensions()`` and ``getSubPaths()`` take no arguments, so
        aggregating them — and compiling the sub-paths — is the same work
        for all ~4,000 folders of a run. It is computed once per provider
        registry and memoized here; only ``getFilters(escaped_path)``,
        which really does depend on the folder, is rebuilt per call.

        The memo is safe under the pool without a lock: the key is the
        registry's identity, the value is a pure function of it, and a
        dict assignment is atomic — two workers racing compute the same
        pair and the loser's copy is simply discarded.
        """
        key = tuple(id(provider) for provider in provider_list or ())
        cached = self._filter_memo.get(key)
        if cached is not None:
            return cached
        extensions = []
        subpaths = []
        for provider in provider_list or ():
            extensions += list(provider.getExtensions() or ())
            subpaths += list(provider.getSubPaths() or ())
        computed = (_extension_suffixes(extensions), _compile_subpaths(subpaths))
        self._filter_memo[key] = computed
        return computed


# ---------------------------------------------------------------------------
# The client-side halves of legacy's query
# ---------------------------------------------------------------------------


def _hit_parent(hit):
    """The bucket key for one hit, or ``None`` when it has no folder.

    Also the hit-shape gate: a non-dict hit, a non-dict ``_source`` or a
    non-string ``parent``/``path`` used to reach ``os.path.dirname`` and
    raise ``TypeError`` straight past the caller's ``ValueError``
    handling.
    """
    if not isinstance(hit, dict):
        raise ValueError(
            f"index discovery got {type(hit).__name__} where a hit document "
            f"was expected"
        )
    source = hit.get("_source")
    if source is None:
        return None
    if not isinstance(source, dict):
        raise ValueError(
            f"index discovery got a hit whose _source is "
            f"{type(source).__name__}, not a document"
        )
    parent = source.get("parent")
    if parent is None:
        return None
    if not isinstance(parent, str):
        raise ValueError(
            f"index discovery got a hit whose parent is {parent!r}, not a path"
        )
    path = source.get("path")
    if path is not None and not isinstance(path, str):
        raise ValueError(
            f"index discovery got a hit whose path is {path!r}, not a path"
        )
    return normalize_path(parent)


def _extension_suffixes(extensions):
    """Legacy's ``*ext.lower()`` / ``*ext.upper()`` wildcard pair, as suffixes.

    Both cases and ONLY both cases: ``build_search_doc`` emits exactly
    those two wildcards per declared extension, so a file named
    ``clip.Mp4`` matches neither there and must match neither here.

    ``None`` — not an empty tuple — when no provider declares an
    extension: legacy then drops the extension clause entirely rather
    than matching nothing.
    """
    if not extensions:
        return None
    suffixes = []
    for extension in extensions:
        suffixes.append(extension.lower())
        suffixes.append(extension.upper())
    return tuple(dict.fromkeys(suffixes))


def _reject_lucene_only(pattern, what):
    """Refuse a pattern whose meaning differs between Lucene and Python.

    Scans outside character classes and past backslash escapes for the
    five Lucene-only operators (see the module docstring). ``[^/]*`` and
    ``(A|B){3}`` are shared dialect and pass; ``a@b`` or ``<1-9>`` do not.
    """
    escaped = False
    in_class = False
    for char in pattern:
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if in_class:
            if char == "]":
                in_class = False
            continue
        if char == "[":
            in_class = True
        elif char in _LUCENE_ONLY_OPERATORS:
            raise ValueError(
                f"{what} {pattern!r} uses the Lucene-only regexp operator "
                f"{char!r}, which Python reads as a literal — index "
                f"discovery would disagree with the legacy query instead of "
                f"reproducing it"
            )


def _compile_subpaths(subpaths):
    """``getSubPaths()`` compiled once per registry, deduplicated.

    ``list(set(...))`` in ``build_search_doc`` makes the legacy doc's
    order hash-seed-dependent; order is irrelevant to a full-match test,
    so this dedupes order-stably instead.
    """
    patterns = []
    for subpath in dict.fromkeys(subpaths):
        _reject_lucene_only(subpath, "provider sub-path")
        try:
            patterns.append(re.compile(subpath))
        except re.error as error:
            raise ValueError(
                f"provider sub-path {subpath!r} does not compile ({error}), so "
                f"index discovery cannot fold its bucket into the owning folder"
            ) from None
    return tuple(patterns)


def _subpath_match(patterns, folder_path, parent):
    """Is ``parent`` one of ``folder_path``'s provider sub-paths?

    Legacy asks the index for ``regexp parent = os.path.join(escaped_path,
    subpath)``, and an ES ``regexp`` is a FULL match on the whole keyword.
    The equivalent here is: strip the folder prefix and full-match what is
    left — which also sidesteps re-escaping the folder path, since the
    prefix is compared literally.
    """
    if not patterns:
        return False
    prefix = folder_path + "/" if folder_path else ""
    if prefix and not parent.startswith(prefix):
        return False
    relative = parent[len(prefix) :]
    return any(pattern.fullmatch(relative) for pattern in patterns)


def _name_matches(hit, suffixes):
    if suffixes is None:
        return True
    name = _field(hit, "name")
    if not isinstance(name, str):
        return False
    return any(name.endswith(suffix) for suffix in suffixes)


class _Missing:
    """Sentinel: the document does not carry this field at all.

    Distinct from ``None`` and from ``""`` deliberately. Coercing an
    absent field to ``""`` made ``regexp ".*"``, ``wildcard "*"`` and
    ``prefix ""`` match documents OpenSearch would not have returned —
    every predicate must read an absent field as NO MATCH.
    """

    def __repr__(self):
        return "<missing>"


MISSING = _Missing()


def _field(hit, field):
    """One indexed field of a hit, or ``MISSING``.

    Dotted names traverse nested documents (``metadata.card_id``), the
    way OpenSearch resolves them; anything that is not a mapping part-way
    down is ``MISSING`` rather than an exception.

    ``name`` falls back to the path's basename: the field is present on
    prod and in the ``_source`` projection, and the fallback keeps a hit
    whose ``_source`` was projected down further from silently matching
    nothing.
    """
    source = hit.get("_source")
    if not isinstance(source, dict):
        return MISSING
    if field == "name" and not source.get("name"):
        path = source.get("path")
        return os.path.basename(path) if isinstance(path, str) else MISSING
    node = source
    for part in field.split("."):
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return MISSING if node is None else node


# ---------------------------------------------------------------------------
# Provider raw filters, evaluated client-side
# ---------------------------------------------------------------------------


def _compile_raw_filters(folder_path, provider_list):
    """Every provider's ``getFilters()`` for this folder, as predicates.

    The escaped path is what a provider embeds in its raw filter, so it
    is passed here exactly as ``build_search_doc`` passes it — a provider
    that built its filter from an unescaped path would behave differently
    between the two modes.
    """
    escaped_path = re.escape(folder_path)
    matchers = []
    for provider in provider_list or ():
        for clause in provider.getFilters(escaped_path) or ():
            matchers.append(_compile_clause(clause))
    return tuple(matchers)


def _compile_clause(clause):
    """One ``getFilters()`` query document, as a predicate over a hit.

    Only the forms a provider actually ships are modelled — ``bool`` over
    ``must``/``filter``/``should``/``must_not``, and the four leaf kinds in
    ``_LEAF_KINDS``. Anything else raises: a raw filter is a provider
    telling discovery how to find its clips, and guessing at one we cannot
    read would either drop that provider's clips or hand it foreign files.

    ``should`` follows OpenSearch's own default: it is REQUIRED only when
    the ``bool`` carries no ``must``/``filter`` clause, and is otherwise
    optional (``minimum_should_match`` 0), which is what makes a
    ``must``+``should`` filter behave the same on both paths.
    """
    if not isinstance(clause, dict) or len(clause) != 1:
        raise ValueError(
            f"index discovery cannot evaluate the provider filter {clause!r}: "
            f"expected a single-key query clause"
        )
    ((kind, body),) = clause.items()
    if kind == "bool":
        return _compile_bool(body)
    if kind in _LEAF_KINDS:
        return _compile_leaf(kind, body)
    raise ValueError(
        f"index discovery cannot evaluate the provider filter clause "
        f"{kind!r}: supported kinds are bool, {', '.join(_LEAF_KINDS)}"
    )


def _compile_bool(body):
    if not isinstance(body, dict):
        raise ValueError(
            f"index discovery cannot evaluate the bool filter {body!r}: "
            f"expected a mapping of clause lists"
        )
    unmodelled = [key for key in body if key not in _BOOL_KEYS]
    if unmodelled:
        # `minimum_should_match` changes what the clause matches;
        # `boost`/`_name` prove the author expected scoring semantics a
        # filter context does not give them. Either way, evaluating the
        # clause without the key is evaluating a DIFFERENT clause.
        raise ValueError(
            f"index discovery cannot evaluate a bool filter carrying "
            f"{sorted(unmodelled)!r}: only {', '.join(_BOOL_KEYS)} are modelled"
        )
    must = _compile_all(body.get("must")) + _compile_all(body.get("filter"))
    should = _compile_all(body.get("should"))
    must_not = _compile_all(body.get("must_not"))
    if not (must or should or must_not):
        # An empty bool is an always-true predicate, which in the raw
        # filter's OR position would claim every file in the subtree for
        # this provider. Almost certainly a construction bug in the
        # provider; never something to run.
        raise ValueError(
            "index discovery refuses an empty bool filter: it models no "
            "condition, so it would claim every file under the folder"
        )
    should_required = bool(should) and not must

    def predicate(hit):
        if not all(inner(hit) for inner in must):
            return False
        if any(inner(hit) for inner in must_not):
            return False
        if should_required and not any(inner(hit) for inner in should):
            return False
        return True

    return predicate


def _compile_all(clauses):
    if clauses is None:
        return ()
    if isinstance(clauses, dict):
        clauses = [clauses]
    if not isinstance(clauses, (list, tuple)):
        raise ValueError(
            f"index discovery cannot evaluate the bool clause list {clauses!r}: "
            f"expected a query clause or a list of them"
        )
    return tuple(_compile_clause(clause) for clause in clauses)


def _leaf_value(kind, field, body):
    """The value of a leaf clause in either of its two legal spellings."""
    ((_field_name, value),) = body.items()
    if not isinstance(value, dict):
        return value
    value_keys = _LEAF_VALUE_KEYS[kind]
    present = [key for key in value if key in value_keys]
    unmodelled = [key for key in value if key not in value_keys]
    if unmodelled:
        # `case_insensitive`, `flags`, `rewrite`, `boost` — every one of
        # them changes what the clause matches (or how), and dropping it
        # silently evaluates a different clause than legacy sends.
        raise ValueError(
            f"index discovery cannot evaluate the {kind!r} filter on "
            f"{field!r} with option(s) {sorted(unmodelled)!r}: only "
            f"{', '.join(value_keys)} is modelled"
        )
    if not present:
        raise ValueError(
            f"index discovery cannot evaluate the {kind!r} filter on "
            f"{field!r}: no {' or '.join(value_keys)} key"
        )
    return value[present[0]]


def _compile_leaf(kind, body):
    if not isinstance(body, dict) or len(body) != 1:
        raise ValueError(
            f"index discovery cannot evaluate the provider filter "
            f"{{{kind!r}: {body!r}}}: expected a single field"
        )
    field = next(iter(body))
    if field.split(".")[0] not in INDEX_SOURCE_FIELDS:
        # The `_source` projection is a contract, not just a cost
        # measure: a filter on a field the stream does not fetch would
        # evaluate against MISSING for every document and silently drop
        # the whole provider's clips.
        raise ValueError(
            f"index discovery cannot evaluate the {kind!r} filter on "
            f"{field!r}: that field is not in the _source projection "
            f"({', '.join(INDEX_SOURCE_FIELDS)})"
        )
    value = _leaf_value(kind, field, body)
    if kind == "term":
        # `term` is an equality test and is legal on any scalar
        # (`{"term": {"size": 0}}`); the pattern kinds are not.
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(
                f"index discovery cannot evaluate the term filter on {field!r} "
                f"against {value!r}: expected a scalar"
            )
        return lambda hit: _field(hit, field) == value
    if not isinstance(value, str):
        raise ValueError(
            f"index discovery cannot evaluate the {kind!r} filter on {field!r} "
            f"against {value!r}: expected a string pattern"
        )
    if kind == "prefix":
        return lambda hit: _is_str(_field(hit, field)) and _field(
            hit, field
        ).startswith(value)
    if kind == "wildcard":
        # ES wildcards are `*` and `?` over the whole keyword — the same
        # two metacharacters `fnmatch` uses, and the same full-string
        # anchoring. `fnmatchcase`, never `fnmatch`: the platform's
        # case-folding would make `*_001.R3D` match `clip_001.r3d` on
        # macOS and not on Linux, and the index is case-sensitive.
        return lambda hit: _is_str(_field(hit, field)) and fnmatch.fnmatchcase(
            _field(hit, field), value
        )
    _reject_lucene_only(value, f"provider filter regexp on {field!r}")
    try:
        compiled = re.compile(value)
    except re.error as error:
        raise ValueError(
            f"index discovery cannot evaluate the provider filter "
            f"{{'regexp': {{{field!r}: {value!r}}}}}: it does not compile "
            f"({error})"
        ) from None
    return (
        lambda hit: _is_str(_field(hit, field))
        and compiled.fullmatch(_field(hit, field)) is not None
    )


def _is_str(value):
    """An absent field is never a match — see ``MISSING``."""
    return isinstance(value, str)

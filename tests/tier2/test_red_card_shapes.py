"""Tier 2: the RED card shapes, through a real scan.

The end-to-end half of ``tests/tier1/test_red_segment_grouping.py``: one
real tree on disk, the real walk, the real ``process_folder``, and the
story's I/O matrix checked row by row.

The provider double carries red's REAL card pattern
(``providers.red.CARD_SUBPATH_REGEXP``) and red's REAL segment selector
(``providers.red.Provider.segment_selector``), over the ``.fake`` suffix
— the same substitution ``tests/tier2/test_index_discovery_equivalence.py``
makes, and for the same reason: the shipped provider shells out to
``REDline``, which does not exist off-server. What is under test is the
composition (loose filter -> discovery -> the assembly rules -> AD-8
descent -> re-attachment at ingest), not REDline's CSV.

Every matrix row runs under BOTH discovery modes: both read the same
provider methods, and the ``--discovery=legacy`` vs ``index`` equivalence
proven on prod 2026-08-27 must still hold afterwards.
"""

import dataclasses
import fnmatch
import os
import re

import pytest

from portal.plugins.TapelessIngest.models.clip import (
    SEGMENT_EXTRA,
    Clip,
    segment_role,
    segment_stem,
)
from portal.plugins.TapelessIngest.models.folder import Folder, process_folder
from portal.plugins.TapelessIngest.providers.red import CARD_SUBPATH_REGEXP
from portal.plugins.TapelessIngest.providers.red import Provider as RedProvider
from portal.plugins.TapelessIngest.scan.adapters import build_context
from portal.plugins.TapelessIngest.scan.context import DISCOVERY_INDEX
from portal.plugins.TapelessIngest.scan.discovery import prefetch_index

STORAGE_ID = "VX-41"
ROOT = "2026"
SHOOT = f"{ROOT}/AA_20260804_A350_STARLUX_SHOTOVER"
PROVIDER_NAME = "redshaped"
SUFFIX = ".fake"

# The matrix, on disk — one folder per row, so a row that changes says
# which row it was.
CANONICAL = f"{SHOOT}/K001_0801E4.RDM/K001_K005_0804OG.RDC"
LOOSE_SUFFIXED = f"{SHOOT}/K002_K005_0804OG_002.RDC"
NON_NUMERIC_SUFFIX = f"{SHOOT}/K003_K048_0718WV_S000.RDC"
# "No card structure at all": media straight in the shoot folder.
NO_STRUCTURE = SHOOT
# An incompletely copied card: two increments, no `_001` anywhere.
ORPHAN_CARD = f"{SHOOT}/K005_BROKEN_002.RDC"

CANONICAL_FILES = [f"{CANONICAL}/K001_K005_0804OG_{i:03d}.fake" for i in (1, 2, 3)]
LOOSE_FILES = [f"{LOOSE_SUFFIXED}/K002_K005_0804OG_{i:03d}.fake" for i in range(1, 14)]
LOOSE_ANCHOR = LOOSE_FILES[0]

FILES = (
    # Canonical card, both levels. Thirty segments is the production
    # shape; three is the same rule.
    CANONICAL_FILES
    # The loose copy measured on prod 2026-08-27: no `.RDM` level, and a
    # copy suffix on the `.RDC` name. Thirteen segments, as measured.
    + LOOSE_FILES
    # A non-numeric copy suffix on the `.RDC`.
    + [f"{NON_NUMERIC_SUFFIX}/K003_K048_0718WV_{i:03d}.fake" for i in (1, 2)]
    # No card structure at all.
    + [f"{NO_STRUCTURE}/K004_LOOSE_{i:03d}.fake" for i in (1, 2)]
    # No increment in the name.
    + [f"{NO_STRUCTURE}/SOMECLIP.fake"]
    # A lone three-digit name with nothing else of its stem: rule 2 since
    # the 2026-08-28 amendment, and therefore INGESTED.
    + [f"{NO_STRUCTURE}/SHOT_042.fake"]
    # An incomplete set: two increments, no `_001`.
    + [f"{ORPHAN_CARD}/K005_BROKEN_{i:03d}.fake" for i in (4, 5)]
    # Uppercase extension while the provider declares `.fake` lowercase:
    # grouping must NOT suppress `_002`, because the provider whose guard
    # would claim the anchor is not the one that declared the suffix.
    + [f"{NO_STRUCTURE}/K006_MIXED_{i:03d}.FAKE" for i in (1, 2)]
)

# One clip per card folder, the two loose rows, the lone `SHOT_042`, and
# BOTH mixed-case files (ungrouped, so each is its own clip).
EXPECTED_CLIPS = 8
# Anchors and standalones only: the orphan set is reported, not processed.
EXPECTED_PROCESSED = EXPECTED_CLIPS


class RedShapedProvider:
    """red's discovery and re-attachment surface, over an extractable suffix."""

    name = "RED-shaped double"
    machine_name = PROVIDER_NAME

    def __init__(self, root):
        self.root = root
        # C1: every file that actually reached EXTRACTION. The property
        # this whole story exists for — that extras cost nothing — is
        # otherwise invisible to a counter-based assertion.
        self.extracted = []

    def getExtensions(self):
        return ["_001.fake", SUFFIX]

    def getSegmentedExtensions(self):
        return [SUFFIX]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        # red's document verbatim, over `.fake`. The loosened card
        # pattern is IMPORTED, so narrowing it reddens this file.
        return [
            {
                "bool": {
                    "must": [
                        {
                            "regexp": {
                                "parent": os.path.join(
                                    escaped_path, CARD_SUBPATH_REGEXP
                                )
                            }
                        },
                        {
                            "bool": {
                                "should": [
                                    {"wildcard": {"name": f"*{SUFFIX}"}},
                                    {"wildcard": {"name": f"*{SUFFIX.upper()}"}},
                                ]
                            }
                        },
                    ]
                }
            },
        ]

    def getMetadatasFromFile(self, media_file, metadatas, context):
        # The RED identity contract that matters here: the umid is the
        # CLIP's UUID, shared by every segment of a take, so it is
        # derived from the stem with the increment stripped — never from
        # the individual file. Grouping must change which file anchors a
        # clip, never the clip's identity.
        self.extracted.append(media_file.getPath())
        name = media_file.getFileName()
        stem = os.path.splitext(name)[0]
        # Segment semantics ONLY for the suffix this provider groups: a
        # take's files share one clip name (and so one umid), the way a
        # RED clip UUID is shared by every segment. Anything else is a
        # file of its own, which is exactly what the `file` provider does
        # with the lowercase cards red declines.
        clipname = re.sub(r"_[0-9]{3}\Z", "", stem) if name.endswith(SUFFIX) else stem
        metadatas["provider"] = self.machine_name
        metadatas["clipname"] = clipname
        metadatas["umid"] = _umid(os.path.dirname(media_file.getPath()), clipname)
        return metadatas

    def getClipAdditionalMediaFiles(self, clip):
        """red's re-attachment, over the real filesystem.

        Uses the SHIPPED selector, so this is the production rule about
        which siblings belong to an anchor — only the storage query is
        replaced by a directory listing, because the tier-2 stub does not
        implement `StorageHelper.getFilesInStorage`.
        """
        anchor = os.path.basename(clip.file.getPath())
        if not anchor.endswith(SUFFIX):
            # Mirrors red: it is only ever handed clips its own guard
            # claimed, so it never re-attaches around an anchor whose
            # extension case it declines. Scan and ingest agree on the
            # SAME set precisely because both are gated the same way.
            return []
        selector = RedProvider.segment_selector(anchor)
        if selector is None:
            return []
        _glob, pattern = selector
        directory = os.path.join(self.root, clip.path)
        return [
            {"type": "video", "path": f"{clip.path}/{name}"}
            for name in sorted(os.listdir(directory))
            if name != anchor and pattern.fullmatch(name)
        ]


def _umid(directory, clipname):
    return f"uuid-{directory}/{clipname}"


@pytest.fixture
def red_shaped(tmp_path):
    provider = RedShapedProvider(str(tmp_path))
    Clip._PROVIDER_CACHE[PROVIDER_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(PROVIDER_NAME, None)


# --------------------------------------------------------------------------
# One responder serving both discovery paths, as one index would
# --------------------------------------------------------------------------


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
    """A second, simpler evaluator of the legacy search doc."""
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
    stream = [
        {"_source": _source(path), "sort": [path, f"VX-41-{path}"]}
        for path in sorted(paths)
    ]
    # NOT path-sorted: an unsorted OpenSearch query answers in index
    # order, and legacy's per-page `sorted()` is what orders it.
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


def _context(**options):
    defaults = dict(
        user=None,
        dry_run=True,
        providers=[PROVIDER_NAME],
        legacy_storages=[],
        replace=False,
        startwith=["AA_"],
    )
    defaults.update(options)
    return build_context([STORAGE_ID], **defaults)


def _folder(es_fake, path=SHOOT, **options):
    """One folder, through the real tree-mode worker.

    Returns its ``FolderOutcome``, whose ``result.clips`` are the clip
    objects this folder assembled — readable under ``dry_run`` without
    asking the database what a rehearsal deliberately did not write.
    """
    ctx = _context(**options)
    if ctx.options.discovery == DISCOVERY_INDEX:
        ctx = dataclasses.replace(
            ctx,
            discovery_index=prefetch_index(es_fake, STORAGE_ID, ROOT, page_size=500),
        )
    return process_folder(STORAGE_ID, path, ctx, number=0)


def _shoot(es_fake, **options):
    return _folder(es_fake, SHOOT, **options)


def _run_tree(**options):
    emitted = []
    ctx = _context(**options)
    folder = Folder(storage_id=STORAGE_ID, path=ROOT)
    folder._root_path = ctx.root_path_for(STORAGE_ID)
    return folder.scan_tree(ctx, emit=emitted.append), emitted


def _anchors(outcome):
    """``{clip umid: the file name that anchors it}``.

    Keyed by umid, not by directory: several matrix rows share the shoot
    folder, and the whole point is that they are separate clips.
    """
    return {
        clip.umid: os.path.basename(clip.file.getPath())
        for clip in outcome.result.clips
    }


MODES = pytest.mark.parametrize(
    "options",
    [
        pytest.param({}, id="legacy"),
        pytest.param({"discovery": DISCOVERY_INDEX}, id="index"),
    ],
)


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------


@MODES
def test_every_shape_yields_exactly_one_clip_anchored_on_001(
    migrated_db, tree, red_shaped, es_fake, options
):
    """The acceptance criterion, all rows at once."""
    outcome = _shoot(es_fake, **options)

    assert _anchors(outcome) == {
        _umid(CANONICAL, "K001_K005_0804OG"): "K001_K005_0804OG_001.fake",
        _umid(LOOSE_SUFFIXED, "K002_K005_0804OG"): "K002_K005_0804OG_001.fake",
        _umid(NON_NUMERIC_SUFFIX, "K003_K048_0718WV"): "K003_K048_0718WV_001.fake",
        _umid(NO_STRUCTURE, "K004_LOOSE"): "K004_LOOSE_001.fake",
        _umid(NO_STRUCTURE, "SOMECLIP"): "SOMECLIP.fake",
        _umid(NO_STRUCTURE, "SHOT"): "SHOT_042.fake",
        _umid(NO_STRUCTURE, "K006_MIXED_001"): "K006_MIXED_001.FAKE",
        _umid(NO_STRUCTURE, "K006_MIXED_002"): "K006_MIXED_002.FAKE",
    }
    assert outcome.result.counters.created == EXPECTED_CLIPS


@MODES
def test_the_thirteen_segment_copy_reports_one_clip_not_thirteen(
    migrated_db, tree, red_shaped, es_fake, options
):
    """The measured prod defect: 13 clip candidates for one clip."""
    outcome = _shoot(es_fake, **options)

    clips = [clip for clip in outcome.result.clips if clip.path == LOOSE_SUFFIXED]
    assert len(clips) == 1
    assert os.path.basename(clips[0].file.getPath()) == "K002_K005_0804OG_001.fake"


@MODES
def test_no_clip_is_ever_anchored_on_a_middle_segment(
    migrated_db, tree, red_shaped, es_fake, options
):
    """The 24-clip production defect, as a rule."""
    outcome = _shoot(es_fake, **options)

    for clip in outcome.result.clips:
        name = os.path.basename(clip.file.getPath())
        parsed = segment_stem(name)
        if parsed is None or not name.endswith(SUFFIX):
            # Not a segment, or not a suffix this provider groups (the
            # mixed-case row): each such file is legitimately its own clip.
            continue
        stem, increment, extension = parsed
        anchor = f"{stem}_001{extension}"
        if anchor not in os.listdir(os.path.join(str(tree), clip.path)):
            # No `_001` on disk: a lone three-digit name (rule 2), not a
            # set whose head was skipped.
            continue
        assert increment == "001", (
            f"{clip.umid} is anchored on {name}, a middle segment of a set "
            f"whose {anchor} is right there"
        )


@MODES
def test_a_lone_three_digit_name_is_ingested_not_reported(
    migrated_db, tree, red_shaped, es_fake, options
):
    """Rule 2 as amended 2026-08-28: `SHOT_042.fake` is an ordinary name."""
    outcome = _shoot(es_fake, **options)

    assert _umid(NO_STRUCTURE, "SHOT") in {c.umid for c in outcome.result.clips}
    assert not any("SHOT_042" in error for error in outcome.result.errors)


@MODES
def test_a_mixed_case_extension_is_not_suppressed_on_the_wrong_providers_behalf(
    migrated_db, tree, red_shaped, es_fake, options
):
    """B2: grouping must never hide a file the declaring provider declines.

    The double declares `.fake`; these files are `.FAKE`. Case-sensitively
    they are not its segments, so BOTH become clips — where a lowercasing
    comparison would have dropped `_002` onto a clip nobody re-attaches it
    to, and the media would simply be gone.
    """
    outcome = _shoot(es_fake, **options)

    umids = {clip.umid for clip in outcome.result.clips}
    assert _umid(NO_STRUCTURE, "K006_MIXED_001") in umids
    assert _umid(NO_STRUCTURE, "K006_MIXED_002") in umids


@MODES
def test_files_with_no_card_structure_are_grouped_by_the_same_rules(
    migrated_db, tree, red_shaped, es_fake, options
):
    """Segments straight in the shoot folder: one clip, one extra."""
    outcome = _shoot(es_fake, **options)

    umids = {clip.umid for clip in outcome.result.clips}
    assert _umid(NO_STRUCTURE, "K004_LOOSE") in umids
    assert not any("K004_LOOSE_002" in umid for umid in umids)


# --------------------------------------------------------------------------
# Rule 3: an incomplete set is reported once, never a clip
# --------------------------------------------------------------------------


@MODES
def test_an_incomplete_set_is_one_error_and_never_a_clip(
    migrated_db, tree, red_shaped, es_fake, options
):
    """E1: one report per DIRECTORY, not one per file.

    Thirty tracebacks for one incompletely copied card read as thirty scan
    failures and inflate the error count `unclaimed_hits_doubt` weighs.
    """
    outcome = _shoot(es_fake, **options)

    orphan_errors = [e for e in outcome.result.errors if ORPHAN_CARD in e]
    assert len(orphan_errors) == 1
    # It names the directory, the count and the files, so an operator can
    # finish the copy.
    assert "2 segment(s) with no _001 anchor" in orphan_errors[0]
    assert "K005_BROKEN_004.fake" in orphan_errors[0]
    assert "K005_BROKEN_005.fake" in orphan_errors[0]
    assert not any("K005_BROKEN" in clip.umid for clip in outcome.result.clips)
    # ...and the folder continued.
    assert len(outcome.result.clips) == EXPECTED_CLIPS


def test_an_orphan_only_card_refuses_descent_instead_of_vouching_for_itself(
    migrated_db, tree, red_shaped, es_fake
):
    """The folder holds media, claims none of it, and says so."""
    outcome = _folder(es_fake, ORPHAN_CARD)

    assert outcome.result.clips == ()
    assert outcome.result.errors  # the one aggregated report
    assert outcome.consumed_subdirs is None
    assert any("none became a clip" in e for e in outcome.result.errors)


def test_the_orphan_card_is_reported_by_its_parent_and_again_on_descent(
    migrated_db, tree, red_shaped
):
    """C2: the double report, pinned as the behaviour it is.

    The shoot folder CLAIMS the orphan card's files through the raw filter
    and reports the incomplete set. It assembles no clip there, so the
    card is not a consumed subdir and the walk descends into it and
    reports the same condition again. Over-REPORTING, never
    over-ingesting: no clip is created either time, and the walk stops at
    the card. This is pinned so a later change to the descent gate cannot
    silently flip it to the under-consuming direction.
    """
    result, emitted = _run_tree()

    visited = set(re.findall(r"found \d+ files in (\S+),", "\n".join(emitted)))
    assert visited == {SHOOT, ORPHAN_CARD}
    reports = [
        error
        for error in result.errors
        if error.startswith(f"Incomplete segment set in {ORPHAN_CARD}")
    ]
    assert len(reports) == 2
    # The descent into it then refuses to go further, which is the third
    # mention of the path and the reason the walk stops there.
    assert any(
        error.startswith(f"Not descending into {ORPHAN_CARD}")
        for error in result.errors
    )
    # The two orphan files are counted as hits twice — once under each
    # folder that discovered them.
    assert result.counters.hits == len(FILES) + 2
    assert result.counters.created == EXPECTED_CLIPS
    # The claimed cards are still consumed and visited exactly once.
    assert CANONICAL not in visited
    assert LOOSE_SUFFIXED not in visited


# --------------------------------------------------------------------------
# The cost property, and the media it must not lose
# --------------------------------------------------------------------------


@MODES
def test_the_extras_never_reach_extraction(
    migrated_db, tree, red_shaped, es_fake, options
):
    """C1: the property the whole story exists for, pinned on the ACT.

    Counters cannot see this — moving the grouping block to AFTER
    extraction leaves every counter identical while restoring ~30 REDline
    invocations per canonical card. The provider records what it was
    actually asked to extract.
    """
    outcome = _shoot(es_fake, **options)

    anchors = {clip.file.getPath() for clip in outcome.result.clips}
    assert set(red_shaped.extracted) == anchors
    assert len(red_shaped.extracted) == EXPECTED_PROCESSED
    # Every hit is still DISCOVERED; only the work is avoided.
    assert outcome.result.counters.hits == len(FILES)
    assert outcome.result.counters.processed == EXPECTED_PROCESSED


@MODES
def test_what_the_scan_drops_is_exactly_what_the_ingest_reattaches(
    migrated_db, tree, red_shaped, es_fake, options
):
    """B3: the story's central safety property, end to end.

    A segment skipped at scan is only safe if the provider puts it back on
    the item at ingest. Both sides are computed here from the real tree,
    through the two independent implementations (`segment_role` in
    models/clip.py, `segment_selector` in providers/red.py).
    """
    outcome = _shoot(es_fake, **options)

    dropped = set()
    for path in FILES:
        directory, _, name = path.rpartition("/")
        siblings = lambda d=directory: set(  # noqa: E731
            os.listdir(os.path.join(str(tree), d))
        )
        if segment_role(name, (SUFFIX,), siblings) == SEGMENT_EXTRA:
            dropped.add(path)

    attached = set()
    for clip in outcome.result.clips:
        attached |= {
            entry["path"] for entry in red_shaped.getClipAdditionalMediaFiles(clip)
        }

    assert dropped == attached
    # ...and this is not two empty sets agreeing: 12 loose + 2 canonical
    # + 1 non-numeric + 1 no-structure.
    assert len(dropped) == 16


# --------------------------------------------------------------------------
# The desync that would lose a whole clip (B1)
# --------------------------------------------------------------------------


def test_extras_whose_anchor_never_arrived_are_reported_not_silently_dropped(
    migrated_db, tree, red_shaped, es_fake
):
    """B1: the index/filesystem desync grouping straddles.

    Grouping resolves the anchor on the FILESYSTEM but drops extras from
    the INDEX's hit set. When the anchor is on disk and absent from the
    hits, every segment is skipped as an extra, no clip is assembled — and
    without this report the folder would end with zero clips and zero
    errors, so `unclaimed_hits_doubt` would never fire and the walk would
    descend into a card whose media had silently vanished.
    """
    _install_router(es_fake, [p for p in FILES if p != LOOSE_ANCHOR])

    outcome = _folder(es_fake, LOOSE_SUFFIXED)

    assert outcome.result.clips == ()
    unclaimed = [e for e in outcome.result.errors if "never assembled" in e]
    assert len(unclaimed) == 1
    assert "12 segment(s)" in unclaimed[0]
    assert LOOSE_SUFFIXED in unclaimed[0]
    # Doubt is reachable again, which is the whole point.
    assert outcome.consumed_subdirs is None


def test_the_unclaimed_extras_report_stays_quiet_when_the_clip_is_there(
    migrated_db, tree, red_shaped, es_fake
):
    """The non-regression: an ordinary card must not accuse itself."""
    outcome = _shoot(es_fake)

    assert not any("never assembled" in e for e in outcome.result.errors)


# --------------------------------------------------------------------------
# Identity, and the mechanism this story deliberately does not use
# --------------------------------------------------------------------------


def test_a_previously_ingested_card_keeps_its_umid_and_stays_ingested(
    migrated_db, tree, red_shaped, es_fake
):
    """Grouping changes which file anchors a clip, never its identity.

    The row is seeded the way a PRE-change scan left it — anchored on a
    middle segment, which is the state of the 24 production clips — and
    the re-scan must recognise it by umid, not create a second clip.
    """
    umid = _umid(LOOSE_SUFFIXED, "K002_K005_0804OG")
    Clip.objects.create(
        umid=umid,
        path=LOOSE_SUFFIXED,
        storage_id=STORAGE_ID,
        spanned=False,
        item_id="VX-12345",
        provider_name=PROVIDER_NAME,
        reference_file="VX-41-old-middle-segment",
    )

    outcome = _shoot(es_fake)

    assert Clip.objects.filter(umid=umid).count() == 1
    assert Clip.objects.get(umid=umid).item_id == "VX-12345"
    # Counted ONCE, not once per segment.
    assert outcome.result.counters.already_ingested == 1
    assert outcome.result.counters.created == EXPECTED_CLIPS - 1


def test_grouping_never_touches_the_spanned_fields(
    migrated_db, tree, red_shaped, es_fake
):
    """A2 (ruled by Camille 2026-08-28), as an executable statement.

    `spanned`/`spanned_order`/`spanned_id`/`master_clip` and
    `getSpannedClips()` exist for a take split across SEVERAL PHYSICAL
    CARDS — N linked rows with a master. RED segments are one take's
    media split into files in one place: ONE row with N files. The two are
    complementary, and this story uses extras. Nothing it assembles may
    carry spanned state.
    """
    outcome = _shoot(es_fake)

    assert outcome.result.clips
    for clip in outcome.result.clips:
        assert (clip.spanned, clip.spanned_order, clip.spanned_id) == (
            False,
            0,
            None,
        )
        assert clip.master_clip is False


# --------------------------------------------------------------------------
# Paged mode: the REST browse endpoint paginates on `hits` (C3)
# --------------------------------------------------------------------------


def test_a_paged_scan_of_a_card_reports_files_per_page_not_clips(
    migrated_db, tree, red_shaped, es_fake
):
    """`views.py` computes `pages = hits / number` from this number.

    A 13-segment card is 13 hits and one clip, so a page-size-3 browse
    reports 5 pages of which exactly one carries a clip. That is a real,
    operator-visible consequence of the query no longer selecting the
    anchor, and it had no test at all: no paged case ever produced a
    segment group.
    """
    folder = Folder(storage_id=STORAGE_ID, path=LOOSE_SUFFIXED)
    folder._root_path = str(tree)

    pages = [
        folder.scan(first=first, number=3, providers=[PROVIDER_NAME])
        for first in (0, 3, 6, 9, 12)
    ]

    assert [page["hits"] for page in pages] == [13] * 5
    # One clip in the whole card, on whichever page carried its anchor.
    assert sum(len(page["clips"]) for page in pages) == 1
    # A paged pass can never authorize descent, and — because a card's
    # anchor legitimately sits on another page — never accuses a page of
    # holding unclaimed extras.
    for page in pages:
        assert page["consumed_subdirs"] is None
        assert not any("never assembled" in e for e in page["errors"])


# --------------------------------------------------------------------------
# The two discovery paths still agree (prod 2026-08-27)
# --------------------------------------------------------------------------


def _counters(run_result):
    return {
        field: getattr(run_result.counters, field)
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


def test_both_discovery_modes_agree_on_the_whole_matrix(migrated_db, tree, red_shaped):
    legacy_result, legacy_lines = _run_tree()
    index_result, index_lines = _run_tree(discovery=DISCOVERY_INDEX)

    assert _counters(index_result) == _counters(legacy_result)
    assert sorted(index_result.errors) == sorted(legacy_result.errors)
    assert [re.sub(r"\d+\.\d+s", "<t>", line) for line in index_lines] == [
        re.sub(r"\d+\.\d+s", "<t>", line) for line in legacy_lines
    ]
    # ...and this is not two empty runs agreeing.
    assert legacy_result.counters.created == EXPECTED_CLIPS


# --------------------------------------------------------------------------
# A declaration binds only where the declaring provider is applicable (D5)
# --------------------------------------------------------------------------

MISDECLARER_NAME = "misdeclarer"


class _Misdeclarer:
    """Groups `.fake`, but claims only `.other` — so it is never offered one.

    A registry-wide union of segmented suffixes would let this provider
    suppress another provider's segments, which nothing would then
    re-attach: `getClipAdditionalMediaFiles` is only ever called on the
    provider that CLAIMED the anchor.
    """

    name = "Mis-declarer"
    machine_name = MISDECLARER_NAME

    def getExtensions(self):
        return [".other"]

    def getSegmentedExtensions(self):
        return [SUFFIX]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        return metadatas


@pytest.fixture
def misdeclarer():
    provider = _Misdeclarer()
    Clip._PROVIDER_CACHE[MISDECLARER_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(MISDECLARER_NAME, None)


class _Ungrouping(RedShapedProvider):
    """Claims `.fake` and groups NOTHING — every file is its own clip."""

    machine_name = "ungrouping"

    def getSegmentedExtensions(self):
        return []


@pytest.fixture
def ungrouping(tmp_path):
    provider = _Ungrouping(str(tmp_path))
    Clip._PROVIDER_CACHE["ungrouping"] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop("ungrouping", None)


def test_a_provider_cannot_group_a_suffix_it_is_never_offered(
    migrated_db, tmp_path, es_fake, storage_fake, ungrouping, misdeclarer
):
    """The scoping rule, end to end.

    `ungrouping` claims the files and groups nothing; `misdeclarer` groups
    `.fake` and claims only `.other`, so the pre-filter never offers it
    one. Both files must therefore stay clips of their own — a
    registry-wide union would collapse them to one and lose the second.
    """
    rel = f"{SHOOT}/D5"
    paths = [f"{rel}/A_001.fake", f"{rel}/A_002.fake"]
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")
    _install_router(es_fake, paths)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))

    outcome = _folder(es_fake, rel, providers=["ungrouping", MISDECLARER_NAME])

    assert len(outcome.result.clips) == 2
    assert len(ungrouping.extracted) == 2

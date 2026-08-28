"""Tier 1: RED segment grouping and the optional card structure.

Four halves, all pure:

* the ASSEMBLY RULES (``models/clip.py::segment_role``) — anchor +
  extras, no-increment standalone, and rule 3's NARROWED orphan (an
  increment with no ``_001`` **and another increment of the same stem
  beside it**) — plus the suffix gate that decides which files are
  grouped at all;
* the CASE CONTRACT, which is a media-loss guard, not a nicety: a
  provider declares the case its own runtime guard accepts, and grouping
  must never suppress a file that provider will decline;
* the LOOSENED CARD FILTER (``providers/red.py::getFilters``) — no anchor
  selection, every card shape by extension alone, either case, and still
  inside the index evaluator's modelled subset;
* the ROUND TRIP: the segments the scan drops as extras are exactly the
  ones ``red.getClipAdditionalMediaFiles`` re-attaches at ingest.

Grouping deliberately does NOT live in the query: the query returns
every ``.R3D`` and these rules decide which one anchors a clip. The 24
production clips anchored on a middle segment (``…_004.R3D``,
``…_012.R3D``, ``…_026.R3D``, measured 2026-08-27) are what happens when
neither layer does it.
"""

import re

import pytest

from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.models.clip import (
    SEGMENT_ANCHOR,
    SEGMENT_EXTRA,
    SEGMENT_ORPHAN,
    SEGMENT_STANDALONE,
    SEGMENT_UNGROUPED,
    segment_role,
    segment_stem,
    segmented_extensions,
    segmented_extensions_by_provider,
)
from portal.plugins.TapelessIngest.providers.red import (
    CARD_SUBPATH_REGEXP,
    SEGMENT_FILE_LIMIT,
)
from portal.plugins.TapelessIngest.scan.adapters import build_provider_registry
from portal.plugins.TapelessIngest.scan.discovery import (
    DiscoveryIndex,
    _reject_lucene_only,
)
from portal.plugins.TapelessIngest.scan.extraction import (
    applicable_providers,
    build_extension_map,
)

R3D = (".R3D",)


def _never():
    raise AssertionError("the directory must not be listed here")


def _dir(*names):
    """A `siblings` callable over a fixed directory listing."""
    return lambda: set(names)


# --------------------------------------------------------------------------
# The suffix gate: which files are grouped at all
# --------------------------------------------------------------------------


def test_red_declares_its_r3d_files_as_segments():
    red = build_provider_registry(["red"])[0]

    # UPPERCASE, exactly matching the runtime guard `== ".R3D"`. See
    # test_grouping_never_suppresses_a_file_the_declaring_provider_declines.
    assert red.getSegmentedExtensions() == [".R3D"]
    assert segmented_extensions([red]) == (".R3D",)


def test_no_other_shipped_provider_groups_anything():
    """Grouping is red's alone — the Never boundary, made executable."""
    registry = build_provider_registry()
    grouping = {
        provider.machine_name
        for provider in registry
        if provider.getSegmentedExtensions()
    }

    assert grouping == {"red"}


def test_a_provider_without_the_hook_groups_nothing():
    """A duck-typed double predating the hook must not break the scan."""

    class Old:
        machine_name = "old"

    assert segmented_extensions([Old()]) == ()
    assert segmented_extensions(None) == ()
    assert segmented_extensions_by_provider([Old()]) == {}


def test_the_map_is_keyed_by_provider_identity():
    """Two instances of one class must be told apart, as ExtensionMap assumes."""

    class Grouper:
        machine_name = "grouper"

        def getSegmentedExtensions(self):
            return [".R3D"]

    first, second = Grouper(), Grouper()
    by_provider = segmented_extensions_by_provider([first, second])

    assert set(by_provider) == {id(first), id(second)}


def test_a_bare_string_declaration_is_refused_loudly():
    """Iterating it would group every filename ending in `d`, silently."""

    class Broken:
        machine_name = "broken"

        def getSegmentedExtensions(self):
            return ".R3D"

    with pytest.raises(TapelessIngestException, match="bare string"):
        segmented_extensions([Broken()])


@pytest.mark.parametrize("declared", ["R3D", "", ".", None, 3])
def test_a_suffix_without_a_leading_dot_is_refused_loudly(declared):
    class Broken:
        machine_name = "broken"

        def getSegmentedExtensions(self):
            return [declared]

    with pytest.raises(TapelessIngestException, match="unusable segmented extension"):
        segmented_extensions([Broken()])


def test_a_file_outside_the_declared_suffixes_is_never_grouped():
    # Same `_002` shape, a suffix nobody groups: no rule applies, and the
    # directory is not even listed.
    assert segment_role("CLIP_002.mxf", R3D, _never) == SEGMENT_UNGROUPED


# --------------------------------------------------------------------------
# The case contract — a media-loss guard (B2)
# --------------------------------------------------------------------------


def test_grouping_never_suppresses_a_file_the_declaring_provider_declines():
    """`x_002.r3d` must stay a clip of its own.

    red's guard is `file_extension == ".R3D"`, case-sensitive, so a
    lowercase card is NOT red's — the `file` provider claims each file as
    its own clip and has no `getClipAdditionalMediaFiles` at all. If
    grouping lowercased the comparison it would suppress every segment but
    the first on red's behalf, and the provider that actually claims the
    anchor would never re-attach them: one lonely `file` clip, the rest of
    the card gone.
    """
    red = build_provider_registry(["red"])[0]
    suffixes = segmented_extensions([red])

    assert segment_role("x_002.r3d", suffixes, _never) == SEGMENT_UNGROUPED
    assert segment_role("x_001.r3d", suffixes, _never) == SEGMENT_UNGROUPED
    # ...while the uppercase card this provider really claims IS grouped.
    assert segment_role("X_002.R3D", suffixes, _dir("X_001.R3D")) == SEGMENT_EXTRA


def test_the_lowercase_anchor_is_claimed_by_file_not_by_red():
    """The other half of the composition: red's guard really does decline.

    Both real providers, both real guards. `red` is APPLICABLE to
    `x_001.r3d` (its `.r3d` declaration is a superset) and still declines
    it, so `file` is what claims it — which is why grouping must not have
    suppressed its siblings.
    """
    registry = build_provider_registry()
    by_name = {provider.machine_name: provider for provider in registry}
    red = by_name["red"]
    extension_map = build_extension_map(registry)

    applicable = applicable_providers("x_001.r3d", extension_map)
    assert red in applicable
    assert by_name["file"] in applicable

    # red's guard, exercised directly: no umid, no provider name, and —
    # decisively — REDline is never resolved or run.
    metadatas = red.getMetadatasFromFile(_MediaFile("x_001.r3d"), {}, {})
    assert metadatas == {}


class _MediaFile:
    def __init__(self, name):
        self._name = name

    def getFileName(self):
        return self._name

    def getPath(self):
        return f"2026/AA_x/K001.RDC/{self._name}"

    def getId(self):
        return f"VX-41-{self._name}"


# --------------------------------------------------------------------------
# Rule 1: `X_001` anchors, `X_002…X_NNN` are its extras
# --------------------------------------------------------------------------


def test_the_001_file_anchors_its_clip():
    assert segment_role("K001_K005_0804OG_001.R3D", R3D, _never) == SEGMENT_ANCHOR


def test_a_higher_increment_beside_its_anchor_is_an_extra():
    siblings = _dir("A_001.R3D", "A_002.R3D", "A_012.R3D", "A_026.R3D")
    for name in ("A_002.R3D", "A_012.R3D", "A_026.R3D"):
        assert segment_role(name, R3D, siblings) == SEGMENT_EXTRA


def test_the_anchor_is_looked_for_by_name_in_the_same_directory():
    calls = []

    def siblings():
        calls.append(1)
        return {"K001_K005_0804OG_001.R3D", "K001_K005_0804OG_013.R3D"}

    assert segment_role("K001_K005_0804OG_013.R3D", R3D, siblings) == SEGMENT_EXTRA
    # Listed once, and only because this file is a non-anchor increment.
    assert calls == [1]


def test_the_directory_is_never_listed_for_an_anchor_or_a_standalone():
    """The common case must cost nothing beyond the DC-2 guard's listing."""
    assert segment_role("A_001.R3D", R3D, _never) == SEGMENT_ANCHOR
    assert segment_role("SOMECLIP.R3D", R3D, _never) == SEGMENT_STANDALONE


# --------------------------------------------------------------------------
# Rule 2: a name with no increment (or no set) is its own clip
# --------------------------------------------------------------------------


def test_a_name_without_an_increment_is_its_own_clip():
    for name in ("SOMECLIP.R3D", "A001.R3D", "K001_K005_0804OG.R3D"):
        assert segment_role(name, R3D, _never) == SEGMENT_STANDALONE


def test_a_lone_three_digit_name_is_ingested_not_reported_forever():
    """Rule 2, amended 2026-08-28 (Camille).

    `SHOT_042.R3D` on its own is an ordinary filename that happens to end
    in three digits — not a broken set. Under the frozen-as-written rule 3
    it was a permanent per-scan error and was NEVER ingested.
    """
    assert segment_role("SHOT_042.R3D", R3D, _dir("SHOT_042.R3D")) == (
        SEGMENT_STANDALONE
    )
    # A neighbour that is not an increment of the same stem changes nothing.
    assert (
        segment_role(
            "SHOT_042.R3D", R3D, _dir("SHOT_042.R3D", "OTHER_001.R3D", "NOTES.txt")
        )
        == SEGMENT_STANDALONE
    )


@pytest.mark.parametrize(
    "name",
    [
        "A_12.R3D",  # two digits: not the camera's format
        "A_0012.R3D",  # four digits after the only underscore
        "X_1000.R3D",  # ...the review's case: NOT segment 1000 of `X_1`
        "_001.R3D",  # no stem at all
        "002.R3D",  # nothing to anchor to
    ],
)
def test_only_a_three_digit_increment_after_a_real_stem_groups(name):
    assert segment_role(name, R3D, _never) == SEGMENT_STANDALONE
    assert segment_stem(name) is None


def test_a_double_increment_reads_its_last_group_and_does_not_false_orphan():
    """`A_001_002.R3D` alone is rule 2, not a false rule-3 error."""
    assert segment_stem("A_001_002.R3D") == ("A_001", "002", ".R3D")
    assert segment_role("A_001_002.R3D", R3D, _dir("A_001_002.R3D")) == (
        SEGMENT_STANDALONE
    )
    # ...and with its own anchor beside it, an ordinary extra.
    assert (
        segment_role("A_001_002.R3D", R3D, _dir("A_001_001.R3D", "A_001_002.R3D"))
        == SEGMENT_EXTRA
    )


def test_a_zero_increment_is_handled_without_inventing_an_anchor():
    assert segment_role("A_000.R3D", R3D, _dir("A_000.R3D")) == SEGMENT_STANDALONE
    assert segment_role("A_000.R3D", R3D, _dir("A_001.R3D")) == SEGMENT_EXTRA


def test_an_anchor_whose_case_differs_is_not_a_false_orphan():
    """`X_002.R3D` beside `x_001.r3d` is two different providers' business.

    Case-sensitive throughout: the lowercase file is not red's, so the
    uppercase one is a set of one and takes rule 2 — it is INGESTED, where
    a case-insensitive anchor test would have called this a complete card
    and a case-insensitive sibling test a broken one.
    """
    assert segment_role("X_002.R3D", R3D, _dir("x_001.r3d", "X_002.R3D")) == (
        SEGMENT_STANDALONE
    )


# --------------------------------------------------------------------------
# Rule 3 (narrowed): an incomplete SET is reported, never a clip
# --------------------------------------------------------------------------


def test_an_incomplete_set_is_reported_instead_of_becoming_a_clip():
    siblings = _dir("K001_004.R3D", "K001_005.R3D", "K001_006.R3D")

    for name in ("K001_004.R3D", "K001_005.R3D", "K001_006.R3D"):
        assert segment_role(name, R3D, siblings) == SEGMENT_ORPHAN


def test_the_orphan_rule_is_what_stops_a_middle_segment_anchoring_a_clip():
    """The defect this story corrects, stated as a rule.

    A middle segment of a demonstrably incomplete set must never be
    answered ANCHOR or STANDALONE: those are the two answers that let it
    become a clip of its own, which is how 24 production clips ended up
    referencing `…_004.R3D`, `…_012.R3D`, `…_026.R3D`.
    """
    assert (
        segment_role("A_004.R3D", R3D, _dir("A_004.R3D", "A_005.R3D")) == SEGMENT_ORPHAN
    )


def test_a_sibling_of_a_different_stem_or_extension_does_not_make_a_set():
    assert (
        segment_role("A_004.R3D", R3D, _dir("A_004.R3D", "B_005.R3D"))
        == SEGMENT_STANDALONE
    )
    assert (
        segment_role("A_004.R3D", R3D, _dir("A_004.R3D", "A_005.mov"))
        == SEGMENT_STANDALONE
    )


# --------------------------------------------------------------------------
# The round trip: what the scan drops, the ingest re-attaches (B3)
# --------------------------------------------------------------------------


class _StoredFile:
    def __init__(self, path):
        self._path = path

    def getPath(self):
        return self._path

    def getId(self):
        return f"VX-41-{self._path}"


class _StorageDouble:
    def __init__(self, paths):
        self.paths = paths
        self.queries = []

    def getFilesInStorage(self, number, first, path=None, sort=None):
        self.queries.append((number, first, path, sort))
        # The real backend answers a `*` glob; this models exactly that
        # much and no filtering of its own, so the provider's own
        # tightening is what the assertions see.
        import fnmatch
        import urllib.parse

        pattern = urllib.parse.unquote(path)
        return {
            "files": [
                _StoredFile(candidate)
                for candidate in self.paths
                if fnmatch.fnmatchcase(candidate, pattern)
            ]
        }


class _ClipDouble:
    umid = "uuid-round-trip"
    path = "2026/AA_x/K001_K005_0804OG_002.RDC"

    def __init__(self, anchor, storage, clipname):
        self.file = _StoredFile(f"{self.path}/{anchor}")
        self.metadatas = {"clipname": clipname}
        self._storage = storage

    def get_storage_helper(self):
        return self._storage


def _red():
    return build_provider_registry(["red"])[0]


def test_the_ingest_glob_comes_from_the_on_disk_stem_not_redlines_clip_name():
    """The renamed-rushes case — this story's whole population.

    `clipname` is REDline's CSV "Clip Name". For rushes that were renamed
    or re-wrapped it no longer matches the filename, and the old glob
    (`{clipname}_*.R3D`) then selected NOTHING: the clip was imported with
    only its first segment and nobody noticed.
    """
    storage = _StorageDouble(
        [
            f"{_ClipDouble.path}/RENAMED_001.R3D",
            f"{_ClipDouble.path}/RENAMED_002.R3D",
            f"{_ClipDouble.path}/RENAMED_003.R3D",
        ]
    )
    clip = _ClipDouble("RENAMED_001.R3D", storage, clipname="K001_K005_0804OG")

    files = _red().getClipAdditionalMediaFiles(clip)

    assert [f["path"] for f in files if f["type"] == "video"] == [
        f"{_ClipDouble.path}/RENAMED_002.R3D",
        f"{_ClipDouble.path}/RENAMED_003.R3D",
    ]
    # The glob really was built from the on-disk stem.
    assert storage.queries[0][2].endswith("RENAMED_*.R3D")


def test_a_neighbouring_clip_whose_stem_is_a_prefix_is_not_swallowed():
    """`A001` and `A001_B` in one flat folder — reachable since the card
    story made no-card-structure a supported shape."""
    storage = _StorageDouble(
        [
            f"{_ClipDouble.path}/A001_001.R3D",
            f"{_ClipDouble.path}/A001_002.R3D",
            f"{_ClipDouble.path}/A001_B_001.R3D",
            f"{_ClipDouble.path}/A001_B_002.R3D",
        ]
    )
    clip = _ClipDouble("A001_001.R3D", storage, clipname="A001")

    files = _red().getClipAdditionalMediaFiles(clip)

    assert [f["path"] for f in files if f["type"] == "video"] == [
        f"{_ClipDouble.path}/A001_002.R3D"
    ]


def test_the_ingest_selector_matches_the_scan_rule_exactly():
    """The story's central safety property, as one relation.

    Whatever the scan DROPS as an extra, the ingest must ATTACH. Both
    sides are derived here from the same directory listing, through the
    two independent implementations (`segment_role` in models/clip.py,
    `segment_selector` in providers/red.py).
    """
    names = [
        "A001_001.R3D",
        "A001_002.R3D",
        "A001_003.R3D",
        "A001_B_001.R3D",
        "A001_B_002.R3D",
        "SOMECLIP.R3D",
        "A001.wav",
    ]
    siblings = _dir(*names)
    dropped = {
        name for name in names if segment_role(name, R3D, siblings) == SEGMENT_EXTRA
    }

    storage = _StorageDouble([f"{_ClipDouble.path}/{name}" for name in names])
    attached = set()
    for anchor in ("A001_001.R3D", "A001_B_001.R3D"):
        clip = _ClipDouble(anchor, storage, clipname="ignored")
        attached |= {
            f["path"].rsplit("/", 1)[1]
            for f in _red().getClipAdditionalMediaFiles(clip)
            if f["type"] == "video"
        }

    assert dropped == attached == {"A001_002.R3D", "A001_003.R3D", "A001_B_002.R3D"}


def test_a_clip_with_no_increment_asks_for_no_segments():
    storage = _StorageDouble([f"{_ClipDouble.path}/SOMECLIP.R3D"])
    clip = _ClipDouble("SOMECLIP.R3D", storage, clipname="SOMECLIP")

    files = _red().getClipAdditionalMediaFiles(clip)

    assert [f for f in files if f["type"] == "video"] == []
    # Only the audio probe was issued — no segment glob at all.
    assert len(storage.queries) == 1
    assert storage.queries[0][2].endswith(".wav")


def test_hitting_the_storage_page_limit_is_reported_not_trimmed(caplog):
    """Truncation here loses media from an item that looks fully imported."""
    names = [f"{_ClipDouble.path}/A_{i:03d}.R3D" for i in range(1, 1002)]
    storage = _StorageDouble(names)
    clip = _ClipDouble("A_001.R3D", storage, clipname="A")

    with caplog.at_level("ERROR"):
        _red().getClipAdditionalMediaFiles(clip)

    assert storage.queries[0][0] == SEGMENT_FILE_LIMIT
    assert any("page limit" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# The loosened card filter
# --------------------------------------------------------------------------

STORAGE_ID = "VX-41"
FOLDER = "2026/AA_20260804_A350_STARLUX_SHOTOVER"


def _hit(path):
    parent, _, name = path.rpartition("/")
    return {
        "_source": {
            "path": path,
            "parent": parent,
            "name": name,
            "id": f"VX-41-{path}",
            "storage": STORAGE_ID,
            "item_type": "file",
        }
    }


def _selected(paths):
    """The paths red's own filters select, through the index evaluator.

    Uses the REAL provider document — the point is that what red returns
    is evaluable, not that a hand-written copy of it is.
    """
    index = DiscoveryIndex(STORAGE_ID, FOLDER, [_hit(path) for path in paths])
    hits, _total = index.hits_for(FOLDER, [_red()])
    return sorted(hit["_source"]["path"] for hit in hits)


def test_the_filter_no_longer_selects_the_anchor():
    """The query returns every segment; assembly picks the anchor."""
    card = f"{FOLDER}/K001_0801E4.RDM/K001_K005_0804OG.RDC"
    paths = [f"{card}/K001_K005_0804OG_{i:03d}.R3D" for i in (1, 2, 3)]

    assert _selected(paths) == sorted(paths)


@pytest.mark.parametrize(
    "card",
    [
        # Canonical card, both levels.
        "K001_0801E4.RDM/K001_K005_0804OG.RDC",
        # The loose copy measured on prod: no `.RDM` level, and a copy
        # suffix on the `.RDC` name.
        "K001_K005_0804OG_002.RDC",
        # A non-numeric copy suffix.
        "K001_K048_0718WV_S000.RDC",
        # Names carrying nothing the old pattern demanded.
        "whatever.RDC",
        "Extract.RDM/anything.RDC",
        # An `.RDM` level with the media directly inside it.
        "K001_0801E4.RDM",
        # EITHER level may be EITHER extension (the review's D2): the
        # comment and the waiver both say "either level optional", and an
        # `.RDM` nested in an `.RDM` is a real copy artefact.
        "Extract.RDM/inner.RDM",
        "K001.RDC/K001.RDM",
        # ...and either CASE, because the copies were made any which way.
        "k001_0801e4.rdm/k001_k005_0804og.rdc",
        "K001_K005_0804OG_002.Rdc",
    ],
)
def test_every_card_shape_is_reached_by_extension_alone(card):
    path = f"{FOLDER}/{card}/K001_K005_0804OG_001.R3D"

    assert _selected([path]) == [path]


def test_a_lowercase_r3d_under_a_card_is_still_discovered():
    """It is not red's to CLAIM, but it must not become invisible.

    Its card folder is consumed as a subdir the moment any clip is found
    there, so a filter that only matched `*.R3D` would leave a lowercase
    file discovered by nobody at all.
    """
    card = f"{FOLDER}/K001_K005_0804OG_002.RDC"

    assert _selected([f"{card}/k001_001.r3d"]) == [f"{card}/k001_001.r3d"]


def test_the_filter_stays_anchored_at_the_folder_and_two_levels_deep():
    """Loose is not unbounded: the reach AD-8's descent rule assumes."""
    too_deep = f"{FOLDER}/a/b.RDM/c.RDC/K001_001.R3D"
    sibling = "2026/AA_other/K001.RDC/K001_001.R3D"
    directly_inside = f"{FOLDER}/K001_001.R3D"

    assert _selected([too_deep, sibling]) == []
    # `directly_inside` is found through the ordinary parent+extension
    # branch instead, which is what makes "no card structure" a clip.
    index = DiscoveryIndex(STORAGE_ID, FOLDER, [_hit(directly_inside)])
    hits, _total = index.hits_for(FOLDER, [_red()])
    assert [hit["_source"]["path"] for hit in hits] == [directly_inside]


def test_the_filter_still_only_claims_r3d_files():
    """Loosening the parent must not hand red the sidecars beside the media."""
    card = f"{FOLDER}/K001_K005_0804OG_002.RDC"
    media = f"{card}/K001_K005_0804OG_001.R3D"

    assert _selected([media, f"{card}/K001_K005_0804OG.wav", f"{card}/x.RMD"]) == [
        media
    ]


def test_the_loosened_pattern_uses_no_lucene_only_operator():
    """Both dialects must read it the same, or the two paths diverge."""
    _reject_lucene_only(CARD_SUBPATH_REGEXP, "the loosened card pattern")
    # Character classes only — no construct whose meaning is dialect
    # dependent, and no case-insensitivity FLAG (neither engine would
    # honour one here).
    assert re.fullmatch(r"[\[\]^/A-Za-z+?.\\()]+", CARD_SUBPATH_REGEXP)


def test_the_filter_stays_inside_the_index_evaluators_modelled_subset():
    """`bool` over `regexp`/`wildcard` — nothing the evaluator refuses.

    `hits_for` raises ValueError on any clause form, option or field it
    cannot reproduce faithfully, so simply compiling red's real document
    is the assertion. It now carries a NESTED bool (the two name cases),
    which is the form this pin exists to keep inside the subset.
    """
    index = DiscoveryIndex(STORAGE_ID, FOLDER, [])

    assert index.hits_for(FOLDER, [_red()]) == ([], 0)

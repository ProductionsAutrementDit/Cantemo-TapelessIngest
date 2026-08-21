"""Tier 1 (Epic 2 final review): the consumption barrier's sharp edges.

Three defects the last review round found in the layer that decides what
a folder's clips CONSUMED — the single barrier standing between story
2.6's new descent and a duplicate ingest (NFR-1):

* layer (b) was fed from ``metadatas["provider"]``, which is
  last-writer-wins, so every co-matching provider's sub-path layout was
  missing from the consumed set;
* a folder whose clips could not say where they live returned an EMPTY
  consumed set — "descend into everything" — where the honest answer is
  DOUBT;
* ``ConsumedSubdirs`` is a ``frozenset`` subclass with ``__slots__`` and
  had no ``__reduce__``, so a round trip was relying on the default
  pickling protocol to carry ``_patterns``.

MEASURED, because the review round asserted this last point as a live
defect and it is not one HERE: on Python 3.11+ ``object.__getstate__``
handles ``__slots__``, so ``_patterns`` already survived pickle, ``copy``
and ``deepcopy`` on this interpreter (3.14) — verified by removing
``__reduce__`` and watching every test below still pass. The explicit
``__reduce__`` stays anyway, and is not merely cosmetic: it pins the
round trip against an interpreter or a protocol that does not do that for
us, and it ships the pattern SOURCES rather than compiled ``re.Pattern``
objects across Epic 3's process boundary. What the tests below pin is
therefore the PROPERTY — layer (b) survives a round trip — not a bug fix.
"""

import copy
import pickle
import re

import pytest

from portal.plugins.TapelessIngest.scan.extraction import (
    ConsumedSubdirs,
    consumed_subdirs,
    extract_metadatas,
    subpath_prefix_patterns,
)


class _File:
    def __init__(self, path):
        self._path = path

    def getPath(self):
        return self._path

    def getFileName(self):
        return self._path.rsplit("/", 1)[-1]


class _Provider:
    """A provider that claims a file by writing its own keys."""

    def __init__(self, machine_name, subpaths=(), keys=None, claims=True):
        self.machine_name = machine_name
        self._subpaths = list(subpaths)
        self._keys = keys or {}
        self._claims = claims

    def getSubPaths(self):
        return self._subpaths

    def getExtensions(self):
        return [".fake"]

    def getMetadatasFromFile(self, media_file, metadatas, context):
        if not self._claims:
            return metadatas
        metadatas["provider"] = self.machine_name
        metadatas.update(self._keys)
        return metadatas


class _Clip:
    def __init__(self, path=None, file=None):
        if path is not None:
            self.path = path
        if file is not None:
            self.file = file


# --------------------------------------------------------------------------
# A3: several providers legitimately match one file (AD-7 / FR-14)
# --------------------------------------------------------------------------


def test_every_contributing_provider_is_recorded_not_just_the_last(*_):
    """The standing multi-match note, made observable.

    ``metadatas["provider"]`` keeps only the last writer, so a consumed
    set derived from it silently drops the sub-paths of every other
    provider that claimed the file — leaving their directories open to
    descent, which is the duplicate direction.
    """
    card = _Provider("xdcamlike", subpaths=["Clip"])
    exif = _Provider("exiflike", keys={"width": 1920})
    matched = []

    metadatas = extract_metadatas(_File("2026/A/X.fake"), [card, exif], {}, {}, matched)

    # Last writer still wins the identity key, exactly as before...
    assert metadatas["provider"] == "exiflike"
    # ...but BOTH providers are known to have contributed.
    assert matched == [card, exif]


def test_a_provider_that_contributes_nothing_is_not_recorded():
    silent = _Provider("silent", claims=False)
    matched = []

    extract_metadatas(_File("2026/A/X.fake"), [silent], {"umid": "u"}, {}, matched)

    assert matched == []


def test_a_provider_returning_a_fresh_dict_counts_as_contributing():
    """The merge rule does not require in-place mutation, and nor does this."""

    class _Fresh:
        machine_name = "fresh"

        def getMetadatasFromFile(self, media_file, metadatas, context):
            return {"provider": "fresh", "umid": "u"}

    fresh = _Fresh()
    matched = []
    extract_metadatas(_File("2026/A/X.fake"), [fresh], {}, {}, matched)
    assert matched == [fresh]


def test_matched_is_optional_and_costs_nothing_when_omitted():
    """Every pre-existing caller passes four arguments and must keep working."""
    provider = _Provider("solo")
    assert extract_metadatas(_File("2026/A/X.fake"), [provider], {}, {})[
        "provider"
    ] == ("solo")


def test_the_co_matching_providers_subpaths_reach_the_consumed_set():
    """End to end for A3, through the real `consumed_subdirs`.

    The clip lives in the card provider's layout. Both providers matched
    it, so BOTH sub-path layouts are consumed — including the second one,
    which the last-writer-wins name could never have named.
    """
    card = _Provider("cardlike", subpaths=["CONTENTS/VIDEO"])
    also = _Provider("alsolike", subpaths=["SIDECAR"])

    consumed = consumed_subdirs(
        [_Clip(path="2026/A/CONTENTS/VIDEO")],
        [card, also],
        "2026/A",
    )

    assert "CONTENTS" in consumed
    # Layer (b) from the SECOND provider — the one that only appears when
    # the matched set is the union rather than the last writer.
    assert "SIDECAR" in consumed


# --------------------------------------------------------------------------
# A4: an unanswerable question is DOUBT, never an empty set
# --------------------------------------------------------------------------


def test_clips_that_cannot_say_where_they_live_are_doubt():
    reasons = []
    consumed = consumed_subdirs([_Clip(), _Clip()], [], "2026/A", reasons=reasons)

    assert consumed is None
    assert "none carries a path or a scanned file" in reasons[0]


def test_clips_sitting_in_the_folder_itself_are_still_an_empty_set():
    """The ordinary case, and it must NOT become doubt.

    A flat shoot folder whose clips live directly in it consumes no CHILD.
    That is a real, confident answer — "descend into everything" — and it
    is what almost every folder returns. Confusing it with A4's case would
    stop the recursion dead across the whole tree.
    """
    consumed = consumed_subdirs([_Clip(path="2026/A")], [], "2026/A")

    assert consumed is not None
    assert consumed == frozenset()


def test_no_clips_at_all_is_an_empty_set_not_doubt():
    """A zero-hit folder consumed nothing, and knows it."""
    assert consumed_subdirs([], [], "2026/A") == frozenset()


# --------------------------------------------------------------------------
# E25: layer (b) has to survive a round trip
# --------------------------------------------------------------------------


def _consumed_with_patterns():
    patterns = tuple(
        re.compile(candidate) for candidate in subpath_prefix_patterns("CONTENTS/VIDEO")
    )
    return ConsumedSubdirs({"CARD"}, patterns)


@pytest.mark.parametrize(
    "round_trip",
    [
        lambda value: pickle.loads(pickle.dumps(value)),
        copy.copy,
        copy.deepcopy,
    ],
    ids=["pickle", "copy", "deepcopy"],
)
def test_a_round_trip_keeps_the_subpath_patterns(round_trip):
    """Layer (b) has to survive the trip, by whichever mechanism.

    Losing `_patterns` would be silent — no error, no log line, just the
    sub-path directories left open to descent, which is the
    under-consuming direction. See the module docstring: the default
    protocol happens to preserve it on this interpreter, so `__reduce__`
    is what makes that a guarantee rather than an accident.
    """
    original = _consumed_with_patterns()
    assert "CONTENTS" in original  # layer (b), before

    restored = round_trip(original)

    assert isinstance(restored, ConsumedSubdirs)
    assert restored == original
    assert "CARD" in restored  # layer (a)
    assert "CONTENTS" in restored  # layer (b), after
    assert [p.pattern for p in restored.patterns] == [
        p.pattern for p in original.patterns
    ]


def test_a_pattern_less_consumed_set_round_trips_too():
    original = ConsumedSubdirs({"CARD"})
    restored = pickle.loads(pickle.dumps(original))
    assert restored == frozenset({"CARD"})
    assert restored.patterns == ()

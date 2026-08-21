"""Tier 1 (story 2.5): scan/ingestion.py — the pure ingest-decision ladder.

Decisions only: whether a file is worth a legacy-storage hash lookup,
whether a clip is worth an ingest at all, and why a rejected one was
rejected. The HTTP-call consequences of those decisions are pinned end to
end in tests/tier2/test_ingest_discipline.py.

Clips are plain local objects here — the ladder is duck-typed on
``provider_name``/``item_id``, which is exactly what makes it Tier-1
testable. Portal-freedom itself is proven in a bare subprocess with NO
stub installed.

``Clip._should_replace_original_files`` (FR-35) rides along: it needs
Django's model machinery but no database, and its helpers are injected as
arguments.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.scan.ingestion import (
    SKIP_ALREADY_INGESTED,
    SKIP_NO_HASH,
    needs_hash_recovery,
    select_clips_to_ingest,
    will_ingest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# cwd=repo root, no stub, no conftest: `python -c` puts the cwd on sys.path,
# so `scan.ingestion` resolves to this repo's package in a bare interpreter.
PORTAL_FREEDOM_SCRIPT = (
    "import sys, scan.ingestion; "
    "assert not [m for m in sys.modules if m == 'portal' or m.startswith('portal.')]; "
    "assert 'django' not in sys.modules"
)


class _Clip:
    """Opaque clip payload — the ladder reads two attributes, no more."""

    def __init__(self, umid, provider_name="fake", item_id=None):
        self.umid = umid
        self.provider_name = provider_name
        self.item_id = item_id

    def __repr__(self):
        return f"_Clip({self.umid!r}, item_id={self.item_id!r})"


class _File:
    """Minimal VS file stand-in for _should_replace_original_files."""

    def __init__(self, file_id, storage):
        self._file_id = file_id
        self._storage = storage

    def getId(self):
        return self._file_id

    def getStorage(self):
        return self._storage


# --------------------------------------------------------------------------
# AD-1: no Portal, no Django
# --------------------------------------------------------------------------


def test_scan_ingestion_imports_portal_free_in_subprocess():
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_FREEDOM_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


# --------------------------------------------------------------------------
# needs_hash_recovery: the gate that makes a re-scan free
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item_id, file_hash, legacy_storages, expected",
    [
        # The one case worth an HTTP call.
        (None, "abc", ["VX-1"], True),
        ("", "abc", ["VX-1"], True),
        # Already ingested: nothing left to recover, whatever else holds.
        ("VX-100", "abc", ["VX-1"], False),
        ("VX-100", None, ["VX-1"], False),
        ("VX-100", "abc", [], False),
        # No dedup key: never matched against a legacy storage (NFR-1).
        (None, None, ["VX-1"], False),
        (None, "", ["VX-1"], False),
        # Nowhere to look.
        (None, "abc", [], False),
        (None, "abc", None, False),
    ],
)
def test_needs_hash_recovery_truth_table(item_id, file_hash, legacy_storages, expected):
    assert needs_hash_recovery(item_id, file_hash, legacy_storages) is expected


# --------------------------------------------------------------------------
# will_ingest: item_id presence is the definition (FR-23), hash is a veto
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item_id, replace, has_hash, expected",
    [
        (None, False, True, True),
        ("", False, True, True),
        # FR-23: an item_id is what "already ingested" means.
        ("VX-100", False, True, False),
        # ...unless this run is explicitly replacing.
        ("VX-100", True, True, True),
        # NFR-1: no hash, no ingest — even for a replace run.
        (None, False, False, False),
        (None, True, False, False),
        ("VX-100", True, False, False),
    ],
)
def test_will_ingest_truth_table(item_id, replace, has_hash, expected):
    assert will_ingest(item_id, replace, has_hash) is expected


# --------------------------------------------------------------------------
# select_clips_to_ingest: the ladder over a folder's clips
# --------------------------------------------------------------------------


def test_ladder_splits_new_from_already_ingested():
    fresh = _Clip("NEW")
    seen = _Clip("SEEN", item_id="VX-100")

    to_ingest, skipped = select_clips_to_ingest(
        [(fresh, True), (seen, True)], None, False
    )

    assert to_ingest == [fresh]
    assert skipped == [(seen, SKIP_ALREADY_INGESTED)]


def test_ladder_reports_the_hash_less_clip_with_its_own_reason():
    """The reason token is what earns the retry-next-run log line."""
    hashless = _Clip("NOHASH")

    to_ingest, skipped = select_clips_to_ingest([(hashless, False)], None, False)

    assert to_ingest == []
    assert skipped == [(hashless, SKIP_NO_HASH)]


def test_ladder_prefers_the_hash_reason_over_already_ingested():
    """A hash-less clip that also has an item_id is still a hash problem."""
    both = _Clip("BOTH", item_id="VX-100")

    _, skipped = select_clips_to_ingest([(both, False)], None, False)

    assert skipped == [(both, SKIP_NO_HASH)]


def test_replace_run_re_ingests_item_id_bearing_clips():
    seen = _Clip("SEEN", item_id="VX-100")

    to_ingest, skipped = select_clips_to_ingest([(seen, True)], None, True)

    assert (to_ingest, skipped) == ([seen], [])


def test_provider_filtered_clips_are_neither_ingested_nor_skipped():
    """Today's ingest loop `continue`s over them — they count nowhere."""
    mine = _Clip("MINE", provider_name="xdcam")
    theirs = _Clip("THEIRS", provider_name="red")

    to_ingest, skipped = select_clips_to_ingest(
        [(mine, True), (theirs, True)], ["xdcam"], False
    )

    assert (to_ingest, skipped) == ([mine], [])


def test_no_provider_filter_means_every_provider():
    theirs = _Clip("THEIRS", provider_name="red")

    to_ingest, _ = select_clips_to_ingest([(theirs, True)], None, False)

    assert to_ingest == [theirs]


def test_ladder_preserves_input_order():
    clips = [
        _Clip(f"C{index}", item_id=None if index % 2 else "VX-1") for index in range(6)
    ]

    to_ingest, skipped = select_clips_to_ingest(
        ((clip, True) for clip in clips), None, False
    )

    assert [clip.umid for clip in to_ingest] == ["C1", "C3", "C5"]
    assert [clip.umid for clip, _ in skipped] == ["C0", "C2", "C4"]


def test_ladder_accepts_a_generator_and_an_empty_input():
    assert select_clips_to_ingest(iter(()), None, False) == ([], [])


# --------------------------------------------------------------------------
# FR-35: an item whose original shape holds no file is not "already imported"
# --------------------------------------------------------------------------


def _clip_with_file():
    clip = Clip(umid="FR35", item_id="VX-100", storage_id="VX-41")
    # Memo only: the `file` property must never reach getFileById here.
    clip._file = _File("VX-41-FILE", "VX-41")
    return clip


def test_empty_original_files_no_longer_auto_skips():
    """`len(original_files) >= 0` was always true — every call fell in it."""
    assert (
        _clip_with_file()._should_replace_original_files(
            [], replace=False, legacy_storages=None
        )
        is True
    )


def test_non_empty_original_files_still_skip_without_replace():
    others = [_File("VX-99-FILE", "VX-99")]

    assert (
        _clip_with_file()._should_replace_original_files(
            others, replace=False, legacy_storages=None
        )
        is False
    )


def test_same_file_id_still_skips_even_with_an_empty_shape_fallthrough():
    same = [_File("VX-41-FILE", "VX-99")]

    assert (
        _clip_with_file()._should_replace_original_files(
            same, replace=True, legacy_storages=None
        )
        is False
    )


def test_clip_without_a_file_never_replaces():
    clip = Clip(umid="FR35-NOFILE", item_id="VX-100")

    assert (
        clip._should_replace_original_files([], replace=True, legacy_storages=None)
        is False
    )

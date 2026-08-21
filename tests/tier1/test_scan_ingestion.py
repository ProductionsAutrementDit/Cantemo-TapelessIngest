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

from portal.plugins.TapelessIngest.models.clip import Clip, job_id_from_response
from portal.plugins.TapelessIngest.scan.ingestion import (
    SKIP_ALREADY_INGESTED,
    SKIP_NO_HASH,
    dedupe_clips_by_umid,
    is_incomplete_import,
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
    "item_id, replace, has_hash, incomplete, expected",
    [
        (None, False, True, False, True),
        ("", False, True, False, True),
        # FR-23: an item_id is what "already ingested" means.
        ("VX-100", False, True, False, False),
        # ...unless this run is explicitly replacing...
        ("VX-100", True, True, False, True),
        # ...or the id belongs to an import that never started a job.
        ("VX-100", False, True, True, True),
        # NFR-1: no hash, no ingest — for any combination above.
        (None, False, False, False, False),
        (None, True, False, False, False),
        ("VX-100", True, False, False, False),
        ("VX-100", False, False, True, False),
    ],
)
def test_will_ingest_truth_table(item_id, replace, has_hash, incomplete, expected):
    assert (
        will_ingest(
            item_id=item_id,
            replace=replace,
            has_hash=has_hash,
            import_incomplete=incomplete,
        )
        is expected
    )


def test_will_ingest_takes_no_positional_arguments():
    """Keyword-only: four booleans in a row are a bug waiting to happen."""
    with pytest.raises(TypeError):
        will_ingest("VX-100", False, True)


# --------------------------------------------------------------------------
# is_incomplete_import: the state a job-id-less import leaves behind
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item_id, job_id, at_placeholder, expected",
    [
        # Placeholder created, no job: the stuck clip.
        ("VX-100", None, True, True),
        ("VX-100", "", True, True),
        # A finished import has a job.
        ("VX-100", "VX-JOB", True, False),
        # A hash-RECOVERED id has no job either, and must NOT be retried:
        # it never went through import_file, so its status is untouched.
        ("VX-100", None, False, False),
        # No item at all: this rung has nothing to say.
        (None, None, True, False),
    ],
)
def test_is_incomplete_import_truth_table(item_id, job_id, at_placeholder, expected):
    assert is_incomplete_import(item_id, job_id, at_placeholder) is expected


# --------------------------------------------------------------------------
# select_clips_to_ingest: the ladder over a folder's clips
# --------------------------------------------------------------------------


def _state(clip, has_hash=True, incomplete=False):
    return (clip, has_hash, incomplete)


def test_ladder_splits_new_from_already_ingested():
    fresh = _Clip("NEW")
    seen = _Clip("SEEN", item_id="VX-100")

    to_ingest, skipped = select_clips_to_ingest(
        [_state(fresh), _state(seen)], providers=None, replace=False
    )

    assert to_ingest == [fresh]
    assert skipped == [(seen, SKIP_ALREADY_INGESTED)]


def test_ladder_reports_the_hash_less_clip_with_its_own_reason():
    """The reason token is what earns the retry-next-run log line."""
    hashless = _Clip("NOHASH")

    to_ingest, skipped = select_clips_to_ingest(
        [_state(hashless, has_hash=False)], providers=None, replace=False
    )

    assert to_ingest == []
    assert skipped == [(hashless, SKIP_NO_HASH)]


def test_ladder_prefers_the_hash_reason_over_already_ingested():
    """A hash-less clip that also has an item_id is still a hash problem."""
    both = _Clip("BOTH", item_id="VX-100")

    _, skipped = select_clips_to_ingest(
        [_state(both, has_hash=False)], providers=None, replace=False
    )

    assert skipped == [(both, SKIP_NO_HASH)]


def test_replace_run_re_ingests_item_id_bearing_clips():
    seen = _Clip("SEEN", item_id="VX-100")

    to_ingest, skipped = select_clips_to_ingest(
        [_state(seen)], providers=None, replace=True
    )

    assert (to_ingest, skipped) == ([seen], [])


def test_an_incomplete_import_is_selected_not_skipped():
    """Otherwise the placeholder its failed import left strands it forever."""
    stuck = _Clip("STUCK", item_id="VX-100")

    to_ingest, skipped = select_clips_to_ingest(
        [_state(stuck, incomplete=True)], providers=None, replace=False
    )

    assert (to_ingest, skipped) == ([stuck], [])


def test_provider_filtered_clips_are_neither_ingested_nor_skipped():
    """Today's ingest loop `continue`s over them — they count nowhere."""
    mine = _Clip("MINE", provider_name="xdcam")
    theirs = _Clip("THEIRS", provider_name="red")

    to_ingest, skipped = select_clips_to_ingest(
        [_state(mine), _state(theirs)], providers=["xdcam"], replace=False
    )

    assert (to_ingest, skipped) == ([mine], [])


def test_no_provider_filter_means_every_provider():
    theirs = _Clip("THEIRS", provider_name="red")

    to_ingest, _ = select_clips_to_ingest(
        [_state(theirs)], providers=None, replace=False
    )

    assert to_ingest == [theirs]


def test_ladder_preserves_input_order():
    clips = [
        _Clip(f"C{index}", item_id=None if index % 2 else "VX-1") for index in range(6)
    ]

    to_ingest, skipped = select_clips_to_ingest(
        (_state(clip) for clip in clips), providers=None, replace=False
    )

    assert [clip.umid for clip in to_ingest] == ["C1", "C3", "C5"]
    assert [clip.umid for clip, _ in skipped] == ["C0", "C2", "C4"]


def test_ladder_accepts_a_generator_and_an_empty_input():
    assert select_clips_to_ingest(iter(()), providers=None, replace=False) == ([], [])


# --------------------------------------------------------------------------
# umid dedup: one primary key is one clip is one ingest
# --------------------------------------------------------------------------


def test_two_files_one_umid_produce_one_ingest():
    """The umid IS the clip's primary key — two files mapping to it are
    one clip, and ingesting it twice is a duplicate ingest (NFR-1)."""
    first = _Clip("SAME")
    second = _Clip("SAME")

    to_ingest, skipped = select_clips_to_ingest(
        [_state(first), _state(second)], providers=None, replace=False
    )

    assert to_ingest == [second]
    assert skipped == []


def test_dedup_keeps_the_last_occurrence_in_the_first_slot():
    """The write side's rule (dedupe_candidates), so the ingested object
    is the one whose state the persisted row reflects."""
    first = _Clip("SAME")
    other = _Clip("OTHER")
    last = _Clip("SAME")

    deduped = dedupe_clips_by_umid(
        [_state(first), _state(other), _state(last)],
    )

    assert [clip for clip, _, _ in deduped] == [last, other]


def test_dedup_collapses_a_duplicate_that_is_already_ingested_once():
    """Two files, one umid, already-ingested: ONE skip, not two."""
    first = _Clip("SAME", item_id="VX-100")
    second = _Clip("SAME", item_id="VX-100")

    to_ingest, skipped = select_clips_to_ingest(
        [_state(first), _state(second)], providers=None, replace=False
    )

    assert to_ingest == []
    assert skipped == [(second, SKIP_ALREADY_INGESTED)]


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


# --------------------------------------------------------------------------
# FR-8: reading an item id out of whatever the hash lookup answered
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "results, expected",
    [
        ({"hits": 1, "file": [{"item": [{"id": "VX-100"}]}]}, "VX-100"),
        # A hit count with nothing behind it, and every other truncation:
        # each of these used to raise KeyError/IndexError out of the
        # scan's per-file wrapper, costing the clip its row entirely.
        ({"hits": 0, "file": []}, None),
        ({"hits": 1}, None),
        ({"hits": 1, "file": []}, None),
        ({"hits": 1, "file": [{}]}, None),
        ({"hits": 1, "file": [{"item": []}]}, None),
        ({"hits": 1, "file": [{"item": [{}]}]}, None),
        ({}, None),
        (None, None),
        ("nonsense", None),
    ],
)
def test_item_id_from_hash_hits(results, expected):
    assert Clip._item_id_from_hash_hits(results) == expected


# --------------------------------------------------------------------------
# FR-36: reading the job id out of whatever the helper answered
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"jobId": "VX-9"}, "VX-9"),
        ({"jobId": ""}, None),
        ({}, None),
        # The shapes a bare `"jobId" in res` raises TypeError on — out of
        # import_file, past the per-clip catch, into the folder's error
        # list instead of the failed counter.
        (None, None),
        ("", None),
        (b"", None),
        (["jobId"], None),
        (404, None),
    ],
)
def test_job_id_from_response(response, expected):
    assert job_id_from_response(response) == expected


def test_a_none_response_counts_failed_instead_of_raising():
    """The verdict must survive a helper that answers with nothing."""

    class _NoAnswerHelper:
        def importFileToPlaceholder(self, item_id, **kwargs):
            return None

    clip = Clip(umid="FR36-NONE", item_id="VX-100")

    assert (
        clip._import_single_component("F", [], None, _NoAnswerHelper(), None) is False
    )


def test_clip_without_a_file_never_replaces():
    clip = Clip(umid="FR35-NOFILE", item_id="VX-100")

    assert (
        clip._should_replace_original_files([], replace=True, legacy_storages=None)
        is False
    )

"""Tier 1 (story 2.4): scan/persistence.py — the pure write-plan layer.

Plan decisions only: what gets upserted, which columns may be rewritten,
which stale-key deletes collapse together, whether the Folder row is
written at all. The ORM half lives in models/folder.persist_scan_results
and is exercised in tests/tier2/test_persistence_write_unit.py.

Clips are plain local objects here — the plan layer treats them as opaque
payload, which is exactly what makes it Tier-1 testable. Portal-freedom
itself is proven in a bare subprocess with NO stub installed.
"""

import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.scan.persistence import (
    CLIP_UNIQUE_FIELDS,
    CLIP_UPDATE_FIELDS,
    FOLDER_SCAN_FIELDS,
    ClipCandidate,
    MetadataWrite,
    PersistencePlan,
    StaleDelete,
    build_persistence_plan,
    dedupe_candidates,
    group_stale_deletes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# cwd=repo root, no stub, no conftest: `python -c` puts the cwd on sys.path,
# so `scan.persistence` resolves to this repo's package in a bare interpreter.
PORTAL_FREEDOM_SCRIPT = (
    "import sys, scan.persistence; "
    "assert not [m for m in sys.modules if m == 'portal' or m.startswith('portal.')]; "
    "assert 'django' not in sys.modules"
)


class _Clip:
    """Opaque clip payload — the plan layer never looks inside."""

    def __init__(self, umid, label=None):
        self.umid = umid
        self.label = label


def _candidate(umid, label=None, metadatas=None, created=False):
    return ClipCandidate(
        umid=umid,
        clip=_Clip(umid, label),
        metadatas=metadatas,
        created=created,
    )


# --------------------------------------------------------------------------
# AD-1: no Portal, no Django
# --------------------------------------------------------------------------


def test_scan_persistence_imports_portal_free_in_subprocess():
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_FREEDOM_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan.persistence is not Portal-free in a bare interpreter (AD-1):\n"
        f"{result.stderr.decode(errors='replace')}"
    )


# --------------------------------------------------------------------------
# The update allow-list is the story's central safety property
# --------------------------------------------------------------------------


def test_location_and_ingest_state_columns_are_not_updatable():
    """A moved/re-carded file must never rewrite an existing clip's row.

    The Tier-2 sibling proves it end to end
    (test_moved_file_keeps_location_columns); this pins the allow-list
    itself, which is the thing a future story could widen by accident.
    """
    forbidden = {
        "path",
        "storage_id",
        "folder_path",
        "item_id",
        "job_id",
        "status",
        "imported_on",
        "collection_id",
        "output_file",
        "progress",
    }
    assert forbidden.isdisjoint(CLIP_UPDATE_FIELDS)
    assert CLIP_UNIQUE_FIELDS == ("umid",)
    assert FOLDER_SCAN_FIELDS == ("provider_names", "scanned_on", "clips_total")


def test_plan_carries_the_default_update_fields():
    plan = build_persistence_plan([_candidate("U1", metadatas={"a": "1"})])

    assert plan.update_fields == CLIP_UPDATE_FIELDS


def test_update_fields_are_caller_overridable_but_default_narrow():
    plan = build_persistence_plan(
        [_candidate("U1", metadatas={"a": "1"})], update_fields=("clip_xml", "status")
    )

    assert plan.update_fields == ("clip_xml", "status")


# --------------------------------------------------------------------------
# umid dedup (AD-8 backstop)
# --------------------------------------------------------------------------


def test_duplicate_umid_collapses_to_one_row_last_wins():
    candidates = [
        _candidate("U1", label="first", metadatas={"a": "1"}),
        _candidate("U2", label="other", metadatas={"a": "2"}),
        _candidate("U1", label="last", metadatas={"a": "3"}),
    ]

    plan = build_persistence_plan(candidates)

    # One row per umid; the duplicate keeps its FIRST appearance's slot
    # and the LAST occurrence's payload.
    assert [clip.umid for clip in plan.clip_rows] == ["U1", "U2"]
    assert [clip.label for clip in plan.clip_rows] == ["last", "other"]
    assert [(w.umid, w.metadatas) for w in plan.metadata_writes] == [
        ("U1", {"a": "3"}),
        ("U2", {"a": "2"}),
    ]


def test_dedupe_candidates_is_order_stable():
    deduped = dedupe_candidates(
        [_candidate("B"), _candidate("A"), _candidate("B"), _candidate("C")]
    )

    assert [candidate.umid for candidate in deduped] == ["B", "A", "C"]


# --------------------------------------------------------------------------
# Stale-key grouping: O(key-sets), not O(clips)
# --------------------------------------------------------------------------


def test_same_key_set_collapses_into_one_delete():
    writes = [
        MetadataWrite("U1", {"umid": "U1", "provider": "p"}),
        MetadataWrite("U2", {"provider": "p", "umid": "U2"}),
        MetadataWrite("U3", {"umid": "U3", "provider": "p"}),
    ]

    [stale] = group_stale_deletes(writes)

    # Key ORDER must not split the group — the names are sorted.
    assert stale.umids == ("U1", "U2", "U3")
    assert stale.keep_names == ("provider", "umid")


def test_different_key_sets_get_their_own_delete():
    writes = [
        MetadataWrite("U1", {"umid": "U1"}),
        MetadataWrite("U2", {"umid": "U2", "clipname": "c"}),
        MetadataWrite("U3", {"umid": "U3"}),
    ]

    grouped = group_stale_deletes(writes)

    assert grouped == (
        StaleDelete(umids=("U1", "U3"), keep_names=("umid",)),
        StaleDelete(umids=("U2",), keep_names=("clipname", "umid")),
    )


def test_empty_metadatas_keeps_its_delete_group():
    """An empty mapping wipes the clip's rows — the pre-2.4 fan-out did too."""
    [stale] = group_stale_deletes([MetadataWrite("U1", {})])

    assert stale == StaleDelete(umids=("U1",), keep_names=())


def test_candidate_without_metadatas_writes_nothing():
    plan = build_persistence_plan([_candidate("U1", metadatas=None)])

    # The clip row is still upserted; its metadata rows are left alone —
    # `None` means "not extracted", which is not "extracted as empty".
    assert [clip.umid for clip in plan.clip_rows] == ["U1"]
    assert plan.metadata_writes == ()
    assert plan.stale_deletes == ()


# --------------------------------------------------------------------------
# The folder-save gate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("provider_hits,expected", [(0, False), (1, True), (3, True)])
def test_folder_save_gate_follows_provider_hits(provider_hits, expected):
    plan = build_persistence_plan(
        [_candidate("U1", metadatas={"a": "1"})],
        folder_fields={"provider_names": "faketest"},
        provider_hits=provider_hits,
    )

    assert plan.save_folder is expected


def test_zero_hit_folder_plan_is_empty():
    """Errored-only folder: nothing to write, so no transaction at all."""
    plan = build_persistence_plan([], folder_fields={}, provider_hits=0)

    assert plan.is_empty is True
    assert plan.clip_rows == ()


def test_plan_with_clips_is_not_empty():
    plan = build_persistence_plan([_candidate("U1", metadatas={"a": "1"})])

    assert plan.is_empty is False


def test_plan_with_only_a_folder_save_is_not_empty():
    plan = build_persistence_plan([], folder_fields={"clips_total": 3}, provider_hits=1)

    assert plan.is_empty is False


# --------------------------------------------------------------------------
# Plan immutability
# --------------------------------------------------------------------------


def test_plan_is_frozen_and_folder_fields_are_read_only():
    plan = build_persistence_plan([], folder_fields={"clips_total": 3}, provider_hits=1)

    assert dataclasses.is_dataclass(plan)
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.save_folder = False
    with pytest.raises(TypeError):
        plan.folder_fields["clips_total"] = 9


def test_folder_fields_are_copied_from_the_caller_mapping():
    fields = {"clips_total": 3}
    plan = build_persistence_plan([], folder_fields=fields, provider_hits=1)

    fields["clips_total"] = 99

    assert plan.folder_fields["clips_total"] == 3


def test_default_plan_is_empty_and_harmless():
    assert PersistencePlan().is_empty is True

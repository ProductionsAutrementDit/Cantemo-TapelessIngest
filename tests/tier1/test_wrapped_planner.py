"""Tier 1: one verdict per item, and the exact plan for the writable ones."""

import posixpath

from portal.plugins.TapelessIngest.wrapped import verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import plan_item
from tests.wrapped_fakes import (
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    p2_originals,
    seed_item,
    wrapped_p2_document,
)

ITEM = "VX-35313"
OUTPUT = "/Volumes/ActiveMedia/CANTEMO_FILES/060A2B34.MXF"


def _world(document=None, archived=True, on_disk=()):
    gateway = InMemoryGateway()
    seed_item(gateway, ITEM, document or wrapped_p2_document())
    gateway.component_md[(ITEM, "VX-SW", "VX-SW-C")] = {
        "portal_archive_external_id": "Default-Archive#OLD"
    }
    originals = p2_originals()
    fake = FakeArchive()
    if archived:
        for n, original in enumerate(originals):
            fake.archive(to_absolute(original.relative), f"AirbusHelicopters#{n}")
    disk = FakeDisk({rel: b"x" for rel in on_disk})
    return gateway, fake, disk, originals


def _plan(gateway, fake, disk, originals, spanned=False, output_file=OUTPUT):
    return plan_item(
        item_id=ITEM,
        originals=originals,
        spanned=spanned,
        output_file=output_file,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
    )


def test_tape_only_originals_are_ready_with_their_handles():
    result = _plan(*_world())
    assert result.verdict == verdicts.READY
    assert result.plan["kind"] == "wrap"
    assert [o["entry"]["handle"] for o in result.plan["originals"]] == [
        f"AirbusHelicopters#{n}" for n in range(5)
    ]
    assert all(not o["on_disk"] for o in result.plan["originals"])
    assert result.plan["originals"][0]["tapes"] == [
        {"volume_id": "10509", "barcode": "BC10509", "label": "LABEL.10509"}
    ]
    assert result.plan["wrapped_file"] == {
        "file_id": "VX-W1",
        "storage_id": "VX-2",
        "state": "ARCHIVED",
        "path": "060A2B34.MXF",
    }


def test_the_plan_reads_p5_once_per_folder():
    gateway, fake, disk, originals = _world()
    _plan(gateway, fake, disk, originals)
    assert sorted(fake.folder_calls) == [
        "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2016/AH_TEST/CONTENTS/AUDIO",
        "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2016/AH_TEST/CONTENTS/VIDEO",
    ]


def test_rollback_keeps_the_wrapped_handle_lowres_and_duration():
    result = _plan(*_world())
    assert result.rollback["wrapped_shape_id"] == "VX-SW"
    assert result.rollback["component_metadata"]["VX-SW-C"] == {
        "portal_archive_external_id": "Default-Archive#OLD"
    }
    assert result.rollback["lowres_shape_ids"] == ["VX-LOW"]
    assert result.rollback["item_fields"]["durationSeconds"] == ["8.72"]


def test_originals_on_disk_but_not_archived_are_ready():
    gateway, fake, disk, originals = _world(archived=False)
    disk.contents = {o.relative: b"x" for o in originals}
    result = _plan(gateway, fake, disk, originals)
    assert result.verdict == verdicts.READY
    assert all(o["on_disk"] and o["entry"] is None for o in result.plan["originals"])


def test_one_missing_original_leaves_the_item_untouched():
    gateway, fake, disk, originals = _world()
    del fake.entries[to_absolute(originals[3].relative)]
    result = _plan(gateway, fake, disk, originals)
    assert result.verdict == verdicts.ORIGINALS_MISSING
    assert originals[3].relative in result.reason
    assert result.plan == {}


def test_an_existing_vx41_entity_is_reused():
    gateway, fake, disk, originals = _world()
    gateway.files[("VX-41", originals[0].relative)] = "VX-EXISTING"
    result = _plan(gateway, fake, disk, originals)
    assert result.plan["originals"][0]["file_id"] == "VX-EXISTING"
    assert result.plan["originals"][1]["file_id"] is None


def test_spanned_clips_are_deferred():
    assert _plan(*_world(), spanned=True).verdict == verdicts.SPANNED


def test_attached_file_other_than_output_file_is_unexpected():
    result = _plan(*_world(), output_file="/mnt/ActiveMedia/CANTEMO_FILES/OTHER.MXF")
    assert result.verdict == verdicts.UNEXPECTED
    assert "OTHER.MXF" in result.reason


def test_audio_layout_mismatch_is_unexpected():
    gateway, fake, disk, originals = _world(wrapped_p2_document(audio_count=1))
    result = _plan(gateway, fake, disk, originals)
    assert result.verdict == verdicts.UNEXPECTED
    assert "audio" in result.reason


def test_two_original_shapes_are_unexpected():
    gateway, fake, disk, originals = _world()
    gateway.shapes[ITEM].append(wrapped_p2_document(shape_id="VX-SW2"))
    assert _plan(gateway, fake, disk, originals).verdict == verdicts.UNEXPECTED


def test_a_shape_already_naming_the_originals_is_already_migrated():
    gateway, fake, disk, originals = _world()
    migrated = {
        "id": "VX-MIG",
        "tag": ["original"],
        "containerComponent": {
            "id": "C",
            "file": [{"id": "F0", "storage": "VX-41", "path": originals[0].relative}],
        },
        "videoComponent": [
            {
                "id": "V",
                "file": [
                    {"id": "F0", "storage": "VX-41", "path": originals[0].relative}
                ],
            }
        ],
        "audioComponent": [
            {
                "id": f"A{n}",
                "file": [{"id": f"F{n}", "storage": "VX-41", "path": o.relative}],
            }
            for n, o in enumerate(originals[1:], start=1)
        ],
    }
    gateway.shapes[ITEM] = [migrated]
    result = _plan(gateway, fake, disk, originals)
    assert result.verdict == verdicts.ALREADY_MIGRATED
    assert result.plan["kind"] == "complete"
    assert result.plan["new_shape_id"] == "VX-MIG"
    assert [o["file_id"] for o in result.plan["originals"]] == [
        "F0",
        "F1",
        "F2",
        "F3",
        "F4",
    ]

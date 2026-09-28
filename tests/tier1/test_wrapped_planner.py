"""Tier 1: one verdict per item, and the exact plan for the writable ones."""

import pytest

from portal.plugins.TapelessIngest.wrapped import verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import plan_item
from tests.wrapped_fakes import (
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    p2_clip_metadata,
    p2_originals,
    p2_template,
    proxy_copy_document,
    seed_item,
    wrapped_p2_document,
)

ITEM = "VX-35313"
OUTPUT = "/Volumes/ActiveMedia/CANTEMO_FILES/060A2B34.MXF"


def _world(document=None, archived=True, on_disk=(), duration="8.72", cpaa_marker=None):
    gateway = InMemoryGateway()
    seed_item(
        gateway,
        ITEM,
        document or wrapped_p2_document(),
        duration=duration,
        cpaa_marker=cpaa_marker,
    )
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


def _plan(gateway, fake, disk, originals, spanned=False, output_file=OUTPUT, **extra):
    return plan_item(
        item_id=ITEM,
        originals=originals,
        spanned=spanned,
        output_file=output_file,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        **extra,
    )


def _migrated_shape(originals):
    """Create a shape that references all originals on VX-41 with "original" tag."""
    return {
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
    assert result.plan["originals"][0]["entity_state"] == "ARCHIVED"


def test_a_stale_vx41_entity_for_a_tape_only_original_is_unexpected():
    gateway, fake, disk, originals = _world()
    gateway.files[("VX-41", originals[2].relative)] = "VX-STALE"
    gateway.file_states[("VX-41", "VX-STALE")] = "LOST"
    result = _plan(gateway, fake, disk, originals)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        f"stale VX-41 entity VX-STALE (LOST) for tape-only original "
        f"{originals[2].relative}"
    )
    assert result.plan == {}


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
    gateway.shapes[ITEM] = [_migrated_shape(originals)]
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


def test_already_migrated_rollback_keeps_what_apply_will_overwrite():
    gateway, fake, disk, originals = _world()
    # Preserve lowres shape when replacing with migrated shape
    lowres_shapes = [s for s in gateway.shapes[ITEM] if "lowres" in s.get("tag", [])]
    gateway.shapes[ITEM] = [_migrated_shape(originals)] + lowres_shapes
    gateway.component_md[(ITEM, "VX-MIG", "C")] = {
        "portal_archive_external_id": "Default-Archive#OLD"
    }
    result = _plan(gateway, fake, disk, originals)
    assert result.rollback["wrapped_shape_id"] == "VX-MIG"
    assert result.rollback["component_metadata"]["C"] == {
        "portal_archive_external_id": "Default-Archive#OLD"
    }
    assert result.rollback["lowres_shape_ids"] == ["VX-LOW"]
    assert result.rollback["item_fields"]["durationSeconds"] == ["8.72"]


# proxy-copied technical description (P2 templates)

KEY = "AVC-I_1080/50i|50i|AVC-I100"
TEMPLATES = {
    KEY: {
        "template": p2_template(),
        "reference_item": "VX-REF",
        "references": 499,
        "share": 0.998,
    }
}


# 497 frames at 1/25: the item's durationSeconds agrees with the P2 metadata
P2_SECONDS = "19.88"


def _proxy_world(duration=P2_SECONDS, cpaa_marker="true"):
    return _world(proxy_copy_document(), duration=duration, cpaa_marker=cpaa_marker)


def _proxy_plan(templates=TEMPLATES, world=None, **metadata):
    world = world or _proxy_world()
    return _plan(
        *world,
        clip_metadata=p2_clip_metadata(**metadata),
        templates=templates,
    )


def test_a_proxy_copy_with_a_template_is_ready_from_the_template():
    result = _proxy_plan()
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["technical_source"] == f"template:{KEY}"
    assert result.plan["template"] == p2_template()
    assert result.plan["timing"] == {
        "frames": 497,
        "num": 1,
        "den": 25,
        "start_tc_frames": 1657612,
    }
    assert result.plan["kind"] == "wrap"
    assert result.plan["wrapped_shape_id"] == "VX-SW"
    assert [o["entry"]["handle"] for o in result.plan["originals"]] == [
        f"AirbusHelicopters#{n}" for n in range(5)
    ]
    assert result.rollback["wrapped_shape_id"] == "VX-SW"


def test_the_plan_carries_a_copy_of_the_template():
    templates = {KEY: {"template": p2_template()}}
    result = _proxy_plan(templates=templates)
    templates[KEY]["template"]["mimeType"].append("changed")
    assert result.plan["template"] == p2_template()


def test_a_proxy_copy_without_a_template_is_unexpected_and_names_the_key():
    result = _proxy_plan(templates={})
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        f"proxy-copied technical description; no template for {KEY}"
    )
    assert result.plan == {}


def test_a_proxy_copy_with_incomplete_metadata_is_unexpected():
    result = _proxy_plan(video_codec=None)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "proxy-copied technical description; P2 metadata incomplete for a template"
    )


def test_a_proxy_copy_with_a_malformed_timecode_is_unexpected():
    result = _proxy_plan(timecode_start="18:25:04")
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith(
        "proxy-copied technical description; P2 metadata incomplete for a template"
    )
    assert "18:25:04" in result.reason


def test_a_proxy_copy_is_unexpected_without_any_metadata_or_template():
    result = _plan(*_proxy_world())
    assert result.verdict == verdicts.UNEXPECTED
    assert "P2 metadata incomplete" in result.reason


def test_a_proxy_copy_still_checks_the_attached_file_first():
    world = _proxy_world()
    result = _plan(
        *world,
        output_file="/mnt/ActiveMedia/CANTEMO_FILES/OTHER.MXF",
        clip_metadata=p2_clip_metadata(),
        templates=TEMPLATES,
    )
    assert result.verdict == verdicts.UNEXPECTED
    assert "OTHER.MXF" in result.reason


def test_a_proxy_copy_still_refuses_missing_originals():
    gateway, fake, disk, originals = _proxy_world()
    del fake.entries[to_absolute(originals[3].relative)]
    result = _plan(
        gateway,
        fake,
        disk,
        originals,
        clip_metadata=p2_clip_metadata(),
        templates=TEMPLATES,
    )
    assert result.verdict == verdicts.ORIGINALS_MISSING


def test_a_proxy_copy_still_refuses_a_stale_vx41_entity():
    gateway, fake, disk, originals = _proxy_world()
    gateway.files[("VX-41", originals[2].relative)] = "VX-STALE"
    gateway.file_states[("VX-41", "VX-STALE")] = "LOST"
    result = _plan(
        gateway,
        fake,
        disk,
        originals,
        clip_metadata=p2_clip_metadata(),
        templates=TEMPLATES,
    )
    assert result.verdict == verdicts.UNEXPECTED
    assert "stale VX-41 entity VX-STALE" in result.reason


def test_a_genuine_wrapped_shape_ignores_templates():
    result = _plan(*_world(), clip_metadata=p2_clip_metadata(), templates=TEMPLATES)
    assert result.verdict == verdicts.READY
    assert result.plan["technical_source"] == "wrapped"
    assert "template" not in result.plan and "timing" not in result.plan


def test_a_genuine_wrapped_shape_with_a_bad_layout_stays_unexpected():
    world = _world(wrapped_p2_document(audio_count=1))
    result = _plan(*world, clip_metadata=p2_clip_metadata(), templates=TEMPLATES)
    assert result.verdict == verdicts.UNEXPECTED
    assert "1 audio component(s) for 4 audio original(s)" in result.reason


def test_an_already_migrated_item_keeps_its_existing_description():
    gateway, fake, disk, originals = _world()
    gateway.shapes[ITEM] = [_migrated_shape(originals)]
    result = _plan(gateway, fake, disk, originals)
    assert result.plan["technical_source"] == "existing"


def test_a_p2_duration_disagreeing_with_durationseconds_is_unexpected():
    result = _proxy_plan(world=_proxy_world(duration="8.72"))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "proxy-copied technical description; P2 duration 19.880 s "
        "!= durationSeconds 8.72"
    )
    assert result.plan == {}


def test_the_duration_cross_check_tolerates_half_a_millisecond():
    assert _proxy_plan(world=_proxy_world("19.8804")).verdict == verdicts.READY
    assert _proxy_plan(world=_proxy_world("19.8806")).verdict == (verdicts.UNEXPECTED)


def test_a_proxy_copy_without_durationseconds_is_unexpected():
    world = _proxy_world()
    del world[0].items[ITEM]["durationSeconds"]
    result = _proxy_plan(world=world)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "proxy-copied technical description; no durationSeconds to cross-check"
    )


def test_a_genuine_wrapped_shape_is_not_duration_checked():
    # wrapped_p2_document says 218 frames, the item 8.72 s, P2 497 frames
    result = _plan(*_world(), clip_metadata=p2_clip_metadata(), templates=TEMPLATES)
    assert result.verdict == verdicts.READY


def test_a_durationseconds_that_is_not_a_number_is_unexpected():
    result = _proxy_plan(world=_proxy_world(duration="n/a"))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "proxy-copied technical description; durationSeconds 'n/a' is not a number"
    )


def test_a_template_that_is_itself_a_proxy_copy_is_refused():
    template = p2_template()
    template["mimeType"] = ["video/mp4"]
    result = _proxy_plan(templates={KEY: {"template": template}})
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        f"proxy-copied technical description; template {KEY} is itself a " f"proxy copy"
    )


def _without(name):
    template = p2_template()
    del template[name]
    return template


def _two_videos():
    template = p2_template()
    template["videoComponent"] *= 2
    return template


@pytest.mark.parametrize(
    "template, why",
    [
        (_without("containerComponent"), "no containerComponent"),
        (_without("videoComponent"), "0 videoComponent(s), expected 1"),
        (_two_videos(), "2 videoComponent(s), expected 1"),
        (_without("audioComponent"), "no audioComponent for 4 audio original(s)"),
    ],
)
def test_a_malformed_template_is_refused_at_plan_time(template, why):
    result = _proxy_plan(templates={KEY: {"template": template}})
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        f"proxy-copied technical description; template {KEY} is malformed: {why}"
    )


# the CPAA migration marker (portal_p5_migration_done)

NO_MARKER = (
    "proxy-copied technical description without the CPAA marker "
    "(portal_p5_migration_done)"
)


def test_a_proxy_copy_without_the_cpaa_marker_is_unexpected():
    result = _proxy_plan(world=_proxy_world(cpaa_marker=None))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == NO_MARKER
    assert result.plan == {}


@pytest.mark.parametrize("marker", ["false", "True", "TRUE", "", " true"])
def test_only_the_exact_true_marker_opens_the_template_route(marker):
    result = _proxy_plan(world=_proxy_world(cpaa_marker=marker))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == NO_MARKER


def test_the_marker_is_checked_before_the_template_and_the_duration():
    world = _proxy_world(duration="8.72", cpaa_marker=None)
    result = _proxy_plan(templates={}, world=world, video_codec=None)
    assert result.reason == NO_MARKER


def test_a_genuine_wrapped_shape_with_the_marker_plans_from_wrapped():
    result = _plan(*_world(cpaa_marker="true"))
    assert result.verdict == verdicts.READY
    assert result.plan["technical_source"] == "wrapped"


def test_a_genuine_wrapped_shape_ignores_a_false_marker():
    assert _plan(*_world(cpaa_marker="false")).verdict == verdicts.READY


def test_the_marker_is_recorded_in_rollback():
    result = _proxy_plan()
    assert result.verdict == verdicts.READY
    assert result.rollback["item_fields"]["portal_p5_migration_done"] == ["true"]
    assert result.rollback["item_fields"]["durationSeconds"] == ["19.88"]


def test_the_template_route_reads_the_item_fields_once():
    world = _proxy_world()
    calls = []
    real = world[0].item_fields

    def counting(item_id, names):
        calls.append(tuple(names))
        return real(item_id, names)

    world[0].item_fields = counting
    assert _proxy_plan(world=world).verdict == verdicts.READY
    assert len(calls) == 1


def test_a_template_audio_without_a_time_base_is_refused_at_plan_time():
    template = p2_template()
    del template["audioComponent"][0]["timeBase"]
    result = _proxy_plan(templates={KEY: {"template": template}})
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        f"proxy-copied technical description; template {KEY} is malformed: "
        f"audioComponent has no timeBase"
    )


def test_a_template_that_cannot_state_this_items_duration_is_refused():
    template = p2_template()
    template["audioComponent"][0]["timeBase"] = {"numerator": 1, "denominator": 44101}
    result = _proxy_plan(templates={KEY: {"template": template}})
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith(
        f"proxy-copied technical description; template {KEY} cannot state "
        f"this item: "
    )
    assert "audio samples" in result.reason

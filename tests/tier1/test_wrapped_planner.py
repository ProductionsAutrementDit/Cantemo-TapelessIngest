"""Tier 1: one verdict per item, and the exact plan for the writable ones."""

import pytest

from portal.plugins.TapelessIngest.wrapped import fields, verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import plan_item
from tests.wrapped_fakes import (
    LEGACY_PATH,
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    binary_only_document,
    doubly_attached_document,
    fileless_document,
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


def test_rollback_records_the_technical_fields_shape_create_rewrites():
    # Measured on prod (M10), 2026-09-29: shape/create?updateItemMetadata=true
    # rewrites these on the item besides durationSeconds.
    gateway, fake, disk, originals = _world()
    gateway.items[ITEM].update(
        {
            fields.ITEM_ORIGINAL_FILENAME_FIELD: ["060A2B34.MXF"],
            fields.ITEM_ORIGINAL_FORMAT_FIELD: ["mxf"],
            fields.ITEM_ORIGINAL_VIDEO_CODEC_FIELD: ["dvvideo"],
            fields.ITEM_ORIGINAL_AUDIO_CODEC_FIELD: ["pcm_s16le"],
            fields.ITEM_ORIGINAL_WIDTH_FIELD: ["1440"],
            fields.ITEM_ORIGINAL_HEIGHT_FIELD: ["1080"],
            fields.ITEM_MIME_TYPE_FIELD: ["application/mxf"],
            fields.ITEM_MEDIA_TYPE_FIELD: ["video"],
            fields.ITEM_DURATION_TIMECODE_FIELD: ["00:00:08:18"],
            fields.ITEM_START_TIMECODE_FIELD: ["00:00:00:00"],
            fields.ITEM_START_SECONDS_FIELD: ["0"],
        }
    )
    result = _plan(gateway, fake, disk, originals)
    assert result.rollback["item_fields"] == {
        fields.DURATION_FIELD: ["8.72"],
        fields.ITEM_ORIGINAL_FILENAME_FIELD: ["060A2B34.MXF"],
        fields.ITEM_ORIGINAL_FORMAT_FIELD: ["mxf"],
        fields.ITEM_ORIGINAL_VIDEO_CODEC_FIELD: ["dvvideo"],
        fields.ITEM_ORIGINAL_AUDIO_CODEC_FIELD: ["pcm_s16le"],
        fields.ITEM_ORIGINAL_WIDTH_FIELD: ["1440"],
        fields.ITEM_ORIGINAL_HEIGHT_FIELD: ["1080"],
        fields.ITEM_MIME_TYPE_FIELD: ["application/mxf"],
        fields.ITEM_MEDIA_TYPE_FIELD: ["video"],
        fields.ITEM_DURATION_TIMECODE_FIELD: ["00:00:08:18"],
        fields.ITEM_START_TIMECODE_FIELD: ["00:00:00:00"],
        fields.ITEM_START_SECONDS_FIELD: ["0"],
    }


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

KEY = "AVC-I_1080/50i|50i|AVC-I100|A24"
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


# inferring 16-bit audio when the P2 XML is missing

INFERRED_KEY = "AVC-I_1080/50i|50i|AVC-I100|A16"
INFERRED_TEMPLATES = {
    INFERRED_KEY: {
        "template": p2_template(audio_codec="pcm_s16le"),
        "reference_item": "VX-REF",
        "references": 499,
        "share": 0.998,
    }
}


def test_a_missing_audio_depth_is_inferred_for_avc_i100_1080_50i():
    result = _proxy_plan(templates=INFERRED_TEMPLATES, audio_bits_per_sample=None)
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["technical_source"] == f"template:{INFERRED_KEY}"
    assert result.plan["audio_bits_inferred"] is True


def test_an_explicit_audio_depth_is_not_recorded_as_inferred():
    result = _proxy_plan()
    assert result.verdict == verdicts.READY, result.reason
    assert "audio_bits_inferred" not in result.plan


def test_a_genuine_wrapped_shape_never_records_inference():
    result = _plan(*_world(), clip_metadata=p2_clip_metadata(), templates=TEMPLATES)
    assert result.verdict == verdicts.READY
    assert "audio_bits_inferred" not in result.plan


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


# G3: the wrapped MXF attached twice, once per online legacy storage

TWO_FILES = "2 distinct files on the original shape, expected 1"


def test_a_wrapped_mxf_attached_on_both_legacy_storages_is_ready():
    result = _plan(*_world(doubly_attached_document()))
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["technical_source"] == "wrapped"
    # ordered by storage_id, whatever order the components name them in
    assert result.plan["wrapped_files"] == [
        {
            "file_id": "VX-W11",
            "storage_id": "VX-11",
            "state": "CLOSED",
            "path": "060A2B34.MXF",
        },
        {
            "file_id": "VX-W26",
            "storage_id": "VX-26",
            "state": "CLOSED",
            "path": LEGACY_PATH,
        },
    ]
    assert "wrapped_file" not in result.plan
    assert {f["file_id"] for f in result.rollback["shape_files"]} == {
        "VX-W11",
        "VX-W26",
    }


@pytest.mark.parametrize(
    "copies",
    [
        # a copy with another basename
        [
            ("VX-W26", "VX-26", "2018/AH_X/OTHER.MXF"),
            ("VX-W11", "VX-11", "060A2B34.MXF"),
        ],
        # a copy on the tape-only storage
        [("VX-W2", "VX-2", "060A2B34.MXF"), ("VX-W11", "VX-11", "060A2B34.MXF")],
        # a copy on the rushes storage
        [("VX-W41", "VX-41", LEGACY_PATH), ("VX-W26", "VX-26", LEGACY_PATH)],
        # two files on the same storage
        [("VX-W26", "VX-26", LEGACY_PATH), ("VX-W27", "VX-26", "060A2B34.MXF")],
    ],
    ids=["other-basename", "vx-2", "vx-41", "same-storage"],
)
def test_a_refused_double_attachment_stays_unexpected(copies):
    result = _plan(*_world(doubly_attached_document(copies)))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == TWO_FILES
    assert result.plan == {}


@pytest.mark.parametrize("output_file", ["", None])
def test_a_double_attachment_without_an_output_file_is_unexpected(output_file):
    result = _plan(*_world(doubly_attached_document()), output_file=output_file)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == TWO_FILES


def test_three_copies_of_the_wrapped_mxf_are_unexpected():
    copies = [
        ("VX-W11", "VX-11", "060A2B34.MXF"),
        ("VX-W26", "VX-26", LEGACY_PATH),
        ("VX-W27", "VX-26", "2019/AH_Y/060A2B34.MXF"),
    ]
    result = _plan(*_world(doubly_attached_document(copies)))
    assert result.reason == "3 distinct files on the original shape, expected 1"


def test_a_double_attachment_reuses_a_not_imported_entity_of_on_disk_originals():
    gateway, fake, disk, originals = _world(doubly_attached_document())
    disk.contents = {o.relative: b"x" for o in originals}
    gateway.files[("VX-41", originals[0].relative)] = "VX-NI"
    gateway.file_states[("VX-41", "VX-NI")] = "NOT_IMPORTED"
    result = _plan(gateway, fake, disk, originals)
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["originals"][0]["file_id"] == "VX-NI"
    assert result.plan["originals"][0]["entity_state"] == "NOT_IMPORTED"
    assert result.plan["originals"][0]["on_disk"] is True


def _assert_originals_missing(result, missing):
    assert result.verdict == verdicts.ORIGINALS_MISSING
    assert result.reason == "neither on disk nor in P5: " + ", ".join(missing)
    assert "wrapped_files" not in result.plan
    assert "wrapped_file" not in result.plan


def test_a_double_attachment_without_any_original_is_originals_missing():
    # 36 such items on prod: their wrapped copies are the only essence.
    gateway, fake, disk, originals = _world(doubly_attached_document(), archived=False)
    result = _plan(gateway, fake, disk, originals)
    _assert_originals_missing(result, [o.relative for o in originals])


def test_a_double_attachment_with_only_the_video_on_disk_is_originals_missing():
    gateway, fake, disk, originals = _world(
        doubly_attached_document(), archived=False, on_disk=[p2_originals()[0].relative]
    )
    result = _plan(gateway, fake, disk, originals)
    _assert_originals_missing(result, [o.relative for o in originals[1:]])


def test_a_single_wrapped_file_keeps_the_single_file_plan():
    result = _plan(*_world())
    assert "wrapped_files" not in result.plan
    assert result.plan["wrapped_file"]["file_id"] == "VX-W1"


# G4: a never-analysed original shape (binaryComponent only)

BINARY_NO_MARKER = (
    "binary-only original shape without the CPAA marker (portal_p5_migration_done)"
)


def _binary_world(duration=P2_SECONDS, cpaa_marker="true"):
    return _world(binary_only_document(), duration=duration, cpaa_marker=cpaa_marker)


def test_a_binary_only_shape_with_a_template_is_ready_from_the_template():
    result = _proxy_plan(world=_binary_world())
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["technical_source"] == f"template:{KEY}"
    assert result.plan["template"] == p2_template()
    assert result.plan["timing"] == {
        "frames": 497,
        "num": 1,
        "den": 25,
        "start_tc_frames": 1657612,
    }
    assert result.plan["wrapped_file"] == {
        "file_id": "VX-W1",
        "storage_id": "VX-2",
        "state": "ARCHIVED",
        "path": "060A2B34.MXF",
    }
    assert result.rollback["component_metadata"] == {"VX-SW-B": {}}


def test_a_binary_only_shape_without_the_cpaa_marker_is_unexpected():
    result = _proxy_plan(world=_binary_world(cpaa_marker=None))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == BINARY_NO_MARKER
    assert result.plan == {}


def test_a_binary_only_shape_without_a_template_names_the_key():
    result = _proxy_plan(templates={}, world=_binary_world())
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == f"binary-only original shape; no template for {KEY}"


def test_a_binary_only_shape_with_incomplete_metadata_is_unexpected():
    result = _proxy_plan(world=_binary_world(), video_codec=None)
    assert result.reason == (
        "binary-only original shape; P2 metadata incomplete for a template"
    )


def test_a_binary_only_p2_duration_disagreeing_with_durationseconds_is_unexpected():
    result = _proxy_plan(world=_binary_world(duration="8.72"))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "binary-only original shape; P2 duration 19.880 s != durationSeconds 8.72"
    )
    assert result.plan == {}


def test_a_binary_only_shape_still_checks_the_attached_file():
    world = _binary_world()
    result = _plan(
        *world,
        output_file="/mnt/ActiveMedia/CANTEMO_FILES/OTHER.MXF",
        clip_metadata=p2_clip_metadata(),
        templates=TEMPLATES,
    )
    assert result.verdict == verdicts.UNEXPECTED
    assert "OTHER.MXF" in result.reason


# G5: a genuine original shape that names no file at all


def test_a_fileless_genuine_shape_with_a_matching_duration_is_ready():
    # wrapped_p2_document: 218 frames at 1/25 = 8.72 s, the item's duration
    result = _plan(*_world(fileless_document()))
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["technical_source"] == "wrapped"
    assert result.plan["kind"] == "wrap"
    assert "wrapped_file" not in result.plan
    assert "wrapped_files" not in result.plan
    assert result.rollback["shape_files"] == []


def test_a_fileless_duration_tolerates_half_a_millisecond():
    assert _plan(*_world(fileless_document(), duration="8.7204")).verdict == (
        verdicts.READY
    )
    assert _plan(*_world(fileless_document(), duration="8.7206")).verdict == (
        verdicts.UNEXPECTED
    )


def test_a_fileless_shape_disagreeing_with_durationseconds_is_unexpected():
    result = _plan(*_world(fileless_document(), duration="9.00"))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "fileless original shape; container duration 8.720 s != durationSeconds 9.00"
    )
    assert result.plan == {}


def test_a_fileless_shape_without_a_container_duration_is_unexpected():
    document = fileless_document()
    del document["containerComponent"]["duration"]
    result = _plan(*_world(document))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "fileless original shape; no container duration"


def test_a_fileless_shape_without_durationseconds_is_unexpected():
    world = _world(fileless_document())
    del world[0].items[ITEM]["durationSeconds"]
    result = _plan(*world)
    assert result.reason == (
        "fileless original shape; no durationSeconds to cross-check"
    )


def test_a_fileless_shape_with_a_non_numeric_durationseconds_is_unexpected():
    result = _plan(*_world(fileless_document(), duration="n/a"))
    assert result.reason == (
        "fileless original shape; durationSeconds 'n/a' is not a number"
    )


def test_a_fileless_proxy_copy_is_unexpected():
    world = _world(
        fileless_document(proxy_copy_document()),
        duration=P2_SECONDS,
        cpaa_marker="true",
    )
    result = _plan(*world, clip_metadata=p2_clip_metadata(), templates=TEMPLATES)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "fileless original shape: proxy-copied technical description"
    )


def test_a_fileless_binary_only_shape_is_unexpected():
    world = _world(
        fileless_document(binary_only_document()),
        duration=P2_SECONDS,
        cpaa_marker="true",
    )
    result = _plan(*world, clip_metadata=p2_clip_metadata(), templates=TEMPLATES)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "fileless original shape: binary-only"


def test_a_fileless_shape_with_a_bad_layout_stays_unexpected():
    result = _plan(*_world(fileless_document(wrapped_p2_document(audio_count=1))))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "1 audio component(s) for 4 audio original(s)"

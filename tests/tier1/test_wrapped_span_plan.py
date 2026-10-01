"""Tier 1: a spanned P2 take is planned as ONE multi-segment original shape
plus the pad-assembly manifest pad_forge reads."""

import json

import pytest

from portal.plugins.TapelessIngest.wrapped import fields, verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import (
    ROLLBACK_ITEM_FIELDS,
    plan_item,
)
from portal.plugins.TapelessIngest.wrapped.span import segment_files
from tests.wrapped_fakes import (
    SPAN_CONTENTS,
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    fileless_document,
    genuine_p2_document,
    p2_clip_metadata,
    p2_span,
    p2_template,
    proxy_copy_document,
    seed_item,
)

ITEM = "VX-35313"
OUTPUT = "/Volumes/ActiveMedia/CANTEMO_FILES/060A2B34.MXF"
KEY = "AVC-I_1080/50i|50i|AVC-I100|A24"
TEMPLATES = {KEY: {"template": p2_template()}}


def _files(span):
    return [f for s in span for f in (s.video, *s.audios)]


def _world(document=None, span=None, duration="10", cpaa_marker=None, archived=None):
    span = span if span is not None else p2_span()
    gateway = InMemoryGateway()
    seed_item(
        gateway,
        ITEM,
        document or genuine_p2_document(frames=250),
        duration=duration,
        cpaa_marker=cpaa_marker,
    )
    fake = FakeArchive()
    for n, original in enumerate(_files(span)):
        if archived is None or original.relative in archived:
            fake.archive(to_absolute(original.relative), f"AirbusHelicopters#{n}")
    return gateway, fake, FakeDisk(), span


def _plan(world, **extra):
    gateway, fake, disk, span = world
    return plan_item(
        item_id=ITEM,
        originals=[],
        spanned=True,
        output_file=OUTPUT,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        span=span,
        **extra,
    )


def _manifest(span):
    return json.dumps(
        {
            "schema": "pad-assembly/1",
            "clips": [
                {
                    "video": to_absolute(s.video.relative),
                    "audio": [to_absolute(a.relative) for a in s.audios],
                }
                for s in span
            ],
            "reel_audio": [],
        }
    )


def test_a_genuine_three_segment_take_is_ready_as_one_shape():
    world = _world()
    result = _plan(world)
    assert result.verdict == verdicts.READY, result.reason
    plan = result.plan
    assert plan["kind"] == "wrap"
    assert plan["technical_source"] == "wrapped"
    assert plan["wrapped_shape_id"] == "VX-SW"
    assert [(o["relative"], o["kind"], o["segment"]) for o in plan["originals"]] == [
        (f.relative, f.kind, n)
        for n, s in enumerate(world[3])
        for f in (s.video, *s.audios)
    ]
    assert plan["originals"][5]["relative"] == f"{SPAN_CONTENTS}/VIDEO/003876.MXF"
    assert [o["entry"]["handle"] for o in plan["originals"]] == [
        f"AirbusHelicopters#{n}" for n in range(15)
    ]
    assert plan["segments"] == [
        {"name": "0037OO", "frames": 100, "num": 1, "den": 25},
        {"name": "003876", "frames": 100, "num": 1, "den": 25},
        {"name": "0039EX", "frames": 50, "num": 1, "den": 25},
    ]
    assert plan["manifest"] == _manifest(world[3])
    assert plan["manifest"].startswith(
        '{"schema": "pad-assembly/1", "clips": [{"video": '
        '"/mnt/PAD_Storage/AA - RUSHES TAPELESS/2015/'
    )
    assert plan["manifest"].endswith('"]}], "reel_audio": []}')
    assert result.rollback["wrapped_shape_id"] == "VX-SW"


def test_segments_that_do_not_sum_to_the_wrapped_duration_are_unexpected():
    result = _plan(_world(genuine_p2_document(frames=260)))
    assert result.verdict == verdicts.UNEXPECTED
    assert (
        result.reason == "spanned take: segments sum to 10.000 s, wrapped is 10.400 s"
    )


def test_a_segment_with_another_audio_count_is_unexpected():
    span = p2_span()
    video, audios = segment_files(SPAN_CONTENTS, "0039EX", 2)
    span[2] = span[2].__class__("0039EX", video, audios, 50, (1, 25))
    result = _plan(_world(span=span))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "spanned take: segment 0039EX has 2 audio file(s), the master 4"
    )


def test_a_wrapped_shape_with_another_audio_count_is_unexpected():
    span = p2_span(audio_count=2)
    result = _plan(_world(span=span))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "4 audio component(s) for 2 audio original(s)"


def test_a_segment_inexact_in_a_component_time_base_is_unexpected():
    document = genuine_p2_document(frames=250)
    for body in document["audioComponent"]:
        body["timeBase"] = {"numerator": 1, "denominator": 7}
        body["duration"] = {
            "samples": 70,
            "timeBase": {"numerator": 1, "denominator": 7},
        }
    span = p2_span(takes=(("0037OO", 101), ("003876", 99), ("0039EX", 50)))
    result = _plan(_world(document, span=span))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "spanned take: segment 0037OO: duration 707/25 is not a whole number "
        "of audio samples"
    )


def test_a_proxy_copied_take_is_ready_from_its_template_for_the_whole_take():
    world = _world(proxy_copy_document(), cpaa_marker="true")
    result = _plan(
        world, clip_metadata=p2_clip_metadata(duration="100"), templates=TEMPLATES
    )
    assert result.verdict == verdicts.READY, result.reason
    plan = result.plan
    assert plan["technical_source"] == f"template:{KEY}"
    assert plan["template"] == p2_template()
    assert plan["timing"] == {
        "frames": 250,
        "num": 1,
        "den": 25,
        "start_tc_frames": 1657612,
    }
    assert [s["frames"] for s in plan["segments"]] == [100, 100, 50]
    assert plan["manifest"] == _manifest(world[3])


def test_a_proxy_copied_take_whose_segments_disagree_with_the_item_is_unexpected():
    world = _world(proxy_copy_document(), duration="12", cpaa_marker="true")
    result = _plan(
        world, clip_metadata=p2_clip_metadata(duration="100"), templates=TEMPLATES
    )
    assert result.verdict == verdicts.UNEXPECTED
    assert (
        result.reason == "spanned take: segments sum to 10.000 s, wrapped is 12.000 s"
    )


def test_a_proxy_copied_take_without_the_marker_keeps_the_template_problem():
    world = _world(proxy_copy_document())
    result = _plan(
        world, clip_metadata=p2_clip_metadata(duration="100"), templates=TEMPLATES
    )
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "proxy-copied technical description without the CPAA marker "
        "(portal_p5_migration_done)"
    )


def test_a_template_whose_edit_unit_is_not_the_takes_is_unexpected():
    world = _world(proxy_copy_document(), cpaa_marker="true")
    result = _plan(
        world,
        clip_metadata=p2_clip_metadata(duration="100", EditUnit="1001/30000"),
        templates=TEMPLATES,
    )
    assert result.verdict == verdicts.UNEXPECTED
    assert "EditUnit" in result.reason


def test_spanned_without_a_span_keeps_todays_reason_by_default():
    gateway, fake, disk, span = _world()
    result = plan_item(
        item_id=ITEM,
        originals=[],
        spanned=True,
        output_file=OUTPUT,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
    )
    assert result.verdict == verdicts.SPANNED
    assert result.reason == "spanned P2 clip: a later slice"


def test_spanned_without_a_span_says_why():
    gateway, fake, disk, span = _world()
    result = plan_item(
        item_id=ITEM,
        originals=[],
        spanned=True,
        output_file=OUTPUT,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        span_problem="spanned P2 clip: not the master of its take",
    )
    assert result.verdict == verdicts.SPANNED
    assert result.reason == "spanned P2 clip: not the master of its take"
    assert gateway.writes == []


def test_a_take_with_missing_segment_files_is_originals_missing():
    span = p2_span()
    first = {f.relative for f in _files(span[:2])}
    result = _plan(_world(span=span, archived=first))
    assert result.verdict == verdicts.ORIGINALS_MISSING
    assert result.reason == "neither on disk nor in P5: " + ", ".join(
        f.relative for f in _files(span[2:])
    )


def test_a_take_still_checks_the_attached_file_first():
    result = _plan(_world(), **{})
    assert result.verdict == verdicts.READY
    gateway, fake, disk, span = _world()
    result = plan_item(
        item_id=ITEM,
        originals=[],
        spanned=True,
        output_file="/x/OTHER.MXF",
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        span=span,
    )
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith("attached file 060A2B34.MXF is not the wrapped")


def test_rollback_records_the_pad_assembly_field():
    assert fields.PAD_ASSEMBLY_FIELD == "portal_pad_assembly"
    assert fields.PAD_ASSEMBLY_FIELD in ROLLBACK_ITEM_FIELDS


def test_a_fileless_genuine_take_is_cross_checked_then_ready():
    document = fileless_document(genuine_p2_document(frames=250))
    result = _plan(_world(document))
    assert result.verdict == verdicts.READY, result.reason
    assert "wrapped_file" not in result.plan and "wrapped_files" not in result.plan
    refused = _plan(_world(document, duration="12"))
    assert refused.verdict == verdicts.UNEXPECTED
    assert refused.reason.startswith("fileless original shape; container duration")


def test_a_genuine_take_whose_item_duration_differs_is_unexpected():
    result = _plan(_world(duration="10.4"))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "spanned take: segments sum to 10.000 s, durationSeconds is 10.400 s"
    )
    assert result.plan == {}


def test_a_genuine_take_without_a_usable_item_duration_is_unexpected():
    gateway, fake, disk, span = world = _world()
    del gateway.items[ITEM]["durationSeconds"]
    result = _plan(world)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "spanned take: no durationSeconds to cross-check"
    gateway.items[ITEM]["durationSeconds"] = ["ten"]
    result = _plan(world)
    assert result.reason == "spanned take: durationSeconds 'ten' is not a number"


def test_a_template_take_without_a_usable_item_duration_says_spanned_take():
    gateway, fake, disk, span = world = _world(
        proxy_copy_document(), cpaa_marker="true"
    )
    del gateway.items[ITEM]["durationSeconds"]
    result = _plan(
        world, clip_metadata=p2_clip_metadata(duration="100"), templates=TEMPLATES
    )
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "spanned take: no durationSeconds to cross-check"


def test_a_span_refuses_originals_passed_besides_it():
    gateway, fake, disk, span = _world()
    with pytest.raises(ValueError, match="span"):
        plan_item(
            item_id=ITEM,
            originals=_files(span),
            spanned=True,
            output_file=OUTPUT,
            gateway=gateway,
            archive=CachedArchive(fake),
            disk=disk,
            span=span,
        )

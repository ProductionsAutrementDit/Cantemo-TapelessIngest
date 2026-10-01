"""Tier 2: the command end to end against the plugin's own fakes."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from portal.plugins.TapelessIngest.management.commands import migrate_wrapped_items
from portal.plugins.TapelessIngest.models.clip import Clip, ClipFile, ClipMetadata
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.templates import strip_for_template
from tests.wrapped_fakes import (
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    genuine_p2_document,
    p2_clip_xml,
    p2_clip_metadata,
    p2_originals,
    p2_template,
    proxy_copy_document,
    seed_item,
    wrapped_p2_document,
)

LEGACY = "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/"


def _world(
    items=("VX-1",),
    storage="VX-2",
    documents=None,
    metadata=None,
    duration="8.72",
    cpaa_marker=None,
    folders=None,
):
    gateway, archive = InMemoryGateway(), FakeArchive()
    for n, item_id in enumerate(items):
        document = (documents or {}).get(item_id) or wrapped_p2_document(
            storage=storage
        )
        seed_item(
            gateway, item_id, document, duration=duration, cpaa_marker=cpaa_marker
        )
        clip = Clip.objects.create(
            umid=f"U{n}",
            path="2016/AH_TEST",
            storage_id="VX-41",
            reference_file="F",
            item_id=item_id,
            provider_name="panasonicP2",
            output_file="/mnt/ActiveMedia/CANTEMO_FILES/060A2B34.MXF",
            status=Clip.STATUS_IMPORTED,
        )
        _store_metadata(clip, (metadata or {}).get(item_id) or {})
        folder = (folders or {}).get(item_id, f"2016/AH_{n}")
        for original in p2_originals(clip_dir=f"{folder}/CONTENTS"):
            ClipFile.objects.create(
                clip=clip, path=LEGACY + original.relative, filetype=original.kind
            )
            archive.archive(to_absolute(original.relative), f"H#{n}{original.relative}")
    return gateway, archive, FakeDisk()


def _command(world, templates=None):
    gateway, archive, disk = world
    command = migrate_wrapped_items.Command()
    command.gateway_factory = lambda: gateway
    command.archive_factory = lambda: archive
    command.disk_factory = lambda: disk
    command.templates_factory = lambda: dict(templates or {})
    return command


def _store_metadata(clip, metadata):
    """As prod holds it: the audio depth only in the stored P2 clip XML."""
    metadata = dict(metadata)
    if "audio_bits_per_sample" in metadata:
        clip.clip_xml = p2_clip_xml(metadata.pop("audio_bits_per_sample"))
        clip.save()
    for name, value in metadata.items():
        ClipMetadata.objects.create(clip=clip, name=name, value=value)


def _run(world, *args, templates=None):
    out = StringIO()
    call_command(_command(world, templates), *args, stdout=out)
    return out.getvalue()


def test_plan_then_apply_then_verify(migrated_db):
    world = _world(("VX-1", "VX-2"))
    assert "ready: 2" in _run(world, "plan")
    _run(world, "apply", "--limit", "1")
    assert list(
        WrappedMigration.objects.values_list("item_id", "phase").order_by("item_id")
    ) == [("VX-1", "done"), ("VX-2", "")]
    assert "VX-1: ok" in _run(world, "verify")
    report = _run(world, "report")
    assert "ready/done: 1" in report and "ready/-: 1" in report


def test_plan_writes_nothing_to_vidispine(migrated_db):
    world = _world()
    _run(world, "plan")
    assert world[0].writes == []


def test_plan_never_overwrites_a_row_in_progress(migrated_db):
    world = _world()
    _run(world, "plan")
    WrappedMigration.objects.filter(item_id="VX-1").update(
        phase="shape_posted", plan={"frozen": True}
    )
    _run(world, "plan")
    assert WrappedMigration.objects.get(item_id="VX-1").plan == {"frozen": True}


def test_p5_failure_marks_item_error_and_run_continues(migrated_db):
    world = _world(("VX-1", "VX-2"))
    world[1].failing_folders.add(to_absolute("2016/AH_0/CONTENTS/VIDEO"))
    _run(world, "plan")
    verdicts = dict(WrappedMigration.objects.values_list("item_id", "verdict"))
    assert verdicts == {"VX-1": "error", "VX-2": "ready"}
    world[1].failing_folders.clear()
    _run(world, "plan")
    assert WrappedMigration.objects.get(item_id="VX-1").verdict == "ready"


def test_plan_isolates_a_save_failure_and_continues(migrated_db, monkeypatch):
    world = _world(("VX-1", "VX-2"))
    real_update_or_create = WrappedMigration.objects.update_or_create

    def flaky_update_or_create(*, item_id, defaults):
        if item_id == "VX-1":
            raise RuntimeError("boom")
        return real_update_or_create(item_id=item_id, defaults=defaults)

    monkeypatch.setattr(
        WrappedMigration.objects, "update_or_create", flaky_update_or_create
    )
    out = _run(world, "plan")
    assert "VX-1: FAILED to save plan: RuntimeError: boom" in out
    assert "save-failed: 1" in out
    assert not WrappedMigration.objects.filter(item_id="VX-1").exists()
    assert WrappedMigration.objects.get(item_id="VX-2").verdict == "ready"


def test_apply_isolates_a_failing_item(migrated_db):
    world = _world(("VX-1", "VX-2"))
    _run(world, "plan")
    world[0].shapes["VX-1"].append({"id": "VX-EXTRA-LOW", "tag": ["lowres"]})
    _run(world, "apply", "--all")
    rows = {r.item_id: r for r in WrappedMigration.objects.all()}
    assert rows["VX-1"].phase == "clip_updated" and "lowres" in rows["VX-1"].error
    assert rows["VX-1"].error.startswith("after clip_updated: StepError: ")
    assert rows["VX-2"].phase == "done" and rows["VX-2"].error == ""


def test_apply_dryrun_prints_writes_and_changes_nothing(migrated_db):
    world = _world()
    _run(world, "plan")
    out = _run(world, "apply", "--all", "--dryrun")
    assert "post_shape" in out and "untag_shape" in out
    assert world[0].writes == []
    assert WrappedMigration.objects.get(item_id="VX-1").phase == ""


def test_apply_dryrun_lists_the_clip_update(migrated_db):
    world = _world()
    _run(world, "plan")
    out = _run(world, "apply", "--all", "--dryrun")
    assert "clip_update" in out and "VX-1" in out
    clip = Clip.objects.get(item_id="VX-1")
    assert clip.output_file == "/mnt/ActiveMedia/CANTEMO_FILES/060A2B34.MXF"


def test_dryrun_is_refused_outside_apply(migrated_db):
    with pytest.raises(CommandError, match="--dryrun"):
        _run(_world(), "plan", "--dryrun")


def test_limit_must_be_positive(migrated_db):
    with pytest.raises(CommandError, match="--limit"):
        _run(_world(), "apply", "--limit", "0")


def test_delete_online_wrapped_is_refused_outside_apply(migrated_db):
    with pytest.raises(CommandError, match="--delete-online-wrapped"):
        _run(_world(), "plan", "--delete-online-wrapped")


def test_apply_keeps_an_online_wrapped_file_by_default(migrated_db):
    world = _world(storage="VX-26")
    _run(world, "plan")
    _run(world, "apply", "--item", "VX-1")
    assert "delete_file" not in world[0].write_names()
    assert WrappedMigration.objects.get(item_id="VX-1").plan["wrapped_kept"]


def test_apply_delete_online_wrapped_deletes_it(migrated_db):
    world = _world(storage="VX-26")
    _run(world, "plan")
    _run(world, "apply", "--item", "VX-1", "--delete-online-wrapped")
    assert world[0].writes[-1] == ("delete_file", "VX-26", "VX-W1")


def _row(item_id, verdict, reason="", **extra):
    return WrappedMigration.objects.create(
        item_id=item_id,
        clip_umid=f"U-{item_id}",
        verdict=verdict,
        reason=reason,
        **extra,
    )


def test_report_explains_every_verdict_that_is_not_acted_on(migrated_db):
    for n in range(3):
        _row(f"VX-U{n}", "unexpected", "2 original shapes")
    _row("VX-U9", "unexpected", "x" * 200)
    for n in range(12):
        _row(f"VX-M{n}", "originals-missing", f"neither on disk nor in P5: F{n}")
    _row("VX-M99", "originals-missing", "neither on disk nor in P5: F0")
    _row("VX-R1", "ready", "should not be listed")
    _row("VX-D1", "ready", phase="done", plan={"wrapped_kept": True})
    _row("VX-D2", "ready", phase="done", plan={"wrapped_kept": True})
    _row("VX-D3", "ready", phase="done", plan={})
    lines = _run(_world(()), "report").splitlines()

    at = lines.index("unexpected reasons:")
    assert lines[at + 1 : at + 3] == ["  3  2 original shapes", "  1  " + "x" * 120]
    at = lines.index("originals-missing reasons:")
    listed = lines[at + 1 : at + 11]
    assert listed[0] == "  2  neither on disk nor in P5: F0"
    assert len(listed) == 10 and all(line.startswith("  1  ") for line in listed[1:])
    assert not lines[at + 11].startswith("  ")
    assert "ready reasons:" not in lines
    assert "should not be listed" not in "\n".join(lines)
    assert "wrapped kept: 2" in lines


def test_report_verdict_lists_the_items_and_their_reasons(migrated_db):
    _row("VX-3", "unexpected", "third")
    _row("VX-1", "unexpected", "first")
    _row("VX-2", "spanned", "spanned P2 clip")
    out = _run(_world(()), "report", "--verdict", "unexpected")
    assert out.splitlines() == ["VX-1: first", "VX-3: third"]
    out = _run(_world(()), "report", "--verdict", "unexpected", "--limit", "1")
    assert out.splitlines() == ["VX-1: first"]


def test_report_verdict_must_name_a_verdict(migrated_db):
    with pytest.raises(CommandError, match="--verdict"):
        _run(_world(()), "report", "--verdict", "nope")


def test_verdict_is_refused_outside_report(migrated_db):
    with pytest.raises(CommandError, match="--verdict"):
        _run(_world(()), "verify", "--verdict", "unexpected")


@pytest.mark.parametrize("extra", [(), ("--dryrun",)])
def test_bare_apply_is_refused_without_all(migrated_db, extra):
    world = _world()
    _run(world, "plan")
    with pytest.raises(CommandError) as refused:
        _run(world, "apply", *extra)
    for flag in ("--all", "--item", "--collection", "--limit"):
        assert flag in str(refused.value)
    assert world[0].writes == []
    assert WrappedMigration.objects.get(item_id="VX-1").phase == ""


def test_all_is_refused_outside_apply(migrated_db):
    with pytest.raises(CommandError, match="--all"):
        _run(_world(), "verify", "--all")


def test_apply_stops_after_max_consecutive_failures(migrated_db):
    world = _world(("VX-1", "VX-2", "VX-3", "VX-4", "VX-5"))
    _run(world, "plan")
    for item_id in ("VX-1", "VX-3", "VX-4", "VX-5"):
        world[0].shapes[item_id].append({"id": f"{item_id}-LOW", "tag": ["lowres"]})
    out = _run(world, "apply", "--all", "--max-failures", "2")
    rows = {r.item_id: r for r in WrappedMigration.objects.all()}
    # VX-2's success resets the count; VX-3 and VX-4 make two in a row.
    assert [i for i, r in sorted(rows.items()) if r.error] == ["VX-1", "VX-3", "VX-4"]
    assert rows["VX-2"].phase == "done"
    assert (rows["VX-5"].phase, rows["VX-5"].error) == ("", "")
    assert "stopped after 2 consecutive failures (--max-failures 2)" in out
    assert out.rstrip().endswith("applied: 1, failed: 3")


def test_max_failures_defaults_to_twenty(migrated_db):
    items = tuple(f"VX-{n:02d}" for n in range(21))
    world = _world(items)
    _run(world, "plan")
    for item_id in items:
        world[0].shapes[item_id].append({"id": f"{item_id}-LOW", "tag": ["lowres"]})
    out = _run(world, "apply", "--all")
    assert "stopped after 20 consecutive failures (--max-failures 20)" in out
    assert WrappedMigration.objects.get(item_id="VX-20").error == ""


def test_max_failures_must_be_positive(migrated_db):
    with pytest.raises(CommandError, match="--max-failures"):
        _run(_world(), "apply", "--all", "--max-failures", "0")


# P2 templates for proxy-copied wrapped shapes

KEY = "AVC-I_1080/50i|50i|AVC-I100|A24"


def _proxy_world(items=("VX-1",)):
    return _world(
        items,
        documents={i: proxy_copy_document() for i in items},
        metadata={i: p2_clip_metadata() for i in items},
        duration="19.88",
        cpaa_marker="true",
    )


def test_plan_states_a_proxy_copy_from_its_template(migrated_db):
    world = _proxy_world()
    out = _run(world, "plan", templates={KEY: {"template": p2_template()}})
    assert "ready: 1" in out
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert row.plan["technical_source"] == f"template:{KEY}"
    assert row.plan["template"] == p2_template()
    assert row.plan["timing"]["start_tc_frames"] == 1657612
    assert world[0].writes == []


def test_plan_without_the_template_names_the_missing_key(migrated_db):
    _run(_proxy_world(), "plan")
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert row.verdict == "unexpected"
    assert row.reason == f"proxy-copied technical description; no template for {KEY}"


def test_plan_reads_each_clips_own_metadata(migrated_db):
    world = _world(
        ("VX-1", "VX-2"),
        documents={i: proxy_copy_document() for i in ("VX-1", "VX-2")},
        metadata={"VX-1": p2_clip_metadata(), "VX-2": {"video_codec": "X"}},
        duration="19.88",
        cpaa_marker="true",
    )
    _run(world, "plan", templates={KEY: {"template": p2_template()}})
    verdicts = dict(WrappedMigration.objects.values_list("item_id", "verdict"))
    assert verdicts == {"VX-1": "ready", "VX-2": "unexpected"}


def test_apply_posts_the_template_planned_by_plan(migrated_db):
    world = _proxy_world()
    _run(world, "plan", templates={KEY: {"template": p2_template()}})
    _run(world, "apply", "--item", "VX-1")
    assert WrappedMigration.objects.get(item_id="VX-1").phase == "done"
    (posted,) = [w[2] for w in world[0].writes if w[0] == "post_shape"]
    assert posted["containerComponent"]["format"] == "mxf_d10"


def _ready_clip(n, document, **metadata):
    item_id = f"VX-{n:02d}"
    Clip.objects.create(
        umid=f"T{n}",
        path="2016/AH_TEST",
        storage_id="VX-41",
        reference_file="F",
        item_id=item_id,
        provider_name="panasonicP2",
    )
    clip = Clip.objects.get(umid=f"T{n}")
    _store_metadata(clip, p2_clip_metadata(**metadata))
    return WrappedMigration.objects.create(
        item_id=item_id,
        clip_umid=f"T{n}",
        verdict="ready",
        plan={"kind": "wrap", "technical_source": "wrapped", "wrapped_shape": document},
    )


def _odd():
    document = genuine_p2_document()
    document["videoComponent"][0]["pixelFormat"] = "yuv420p10le"
    return document


def _templates(*args):
    out, err = StringIO(), StringIO()
    command = _command(_world(()))

    def no_gateway():
        raise AssertionError("templates must not touch Vidispine")

    command.gateway_factory = no_gateway
    command.archive_factory = no_gateway
    call_command(command, "templates", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def _snapshot():
    return sorted(
        WrappedMigration.objects.values_list(
            "item_id", "verdict", "phase", "plan", "updated_on"
        )
    )


def test_templates_keeps_the_majority_signature(migrated_db, tmp_path):
    for n in range(1, 4):
        _ready_clip(n, genuine_p2_document(shape_id=f"VX-S{n}"))
    _ready_clip(4, _odd())
    for n in range(5, 7):
        _ready_clip(n, genuine_p2_document(), video_codec="DV100_1080/50i")
    before = _snapshot()
    path = tmp_path / "p2_templates.json"
    out, _ = _templates("--out", str(path), "--min-refs", "2", "--min-share", "0.7")
    written = json.loads(path.read_text())
    assert written == {
        KEY: {
            "template": strip_for_template(genuine_p2_document()),
            "reference_item": "VX-01",
            "references": 3,
            "share": 0.75,
        },
        "DV100_1080/50i|50i|A24": {
            "template": strip_for_template(genuine_p2_document()),
            "reference_item": "VX-05",
            "references": 2,
            "share": 1.0,
        },
    }
    assert f"{KEY}: 4 ref(s), majority 3 (75.0%): kept" in out.splitlines()
    assert _snapshot() == before
    assert Clip.objects.count() == 6 and ClipMetadata.objects.count() == 36


def test_templates_rejects_a_key_below_the_thresholds(migrated_db, tmp_path):
    for n in range(1, 4):
        _ready_clip(n, genuine_p2_document())
    _ready_clip(4, _odd())
    _ready_clip(5, genuine_p2_document(), video_codec="DV100_1080/50i")
    path = tmp_path / "p2_templates.json"
    out, _ = _templates("--out", str(path), "--min-refs", "2")
    assert json.loads(path.read_text()) == {}
    lines = out.splitlines()
    assert f"{KEY}: 4 ref(s), majority 3 (75.0%): rejected, share below 95.0%" in lines
    assert (
        "DV100_1080/50i|50i|A24: 1 ref(s), majority 1 (100.0%): rejected, "
        "fewer than 2 references" in lines
    )


def test_templates_defaults_to_20_references(migrated_db):
    for n in range(1, 20):
        _ready_clip(n, genuine_p2_document())
    out, err = _templates()
    assert json.loads(out) == {}
    assert "rejected, fewer than 20 references" in err
    _ready_clip(20, genuine_p2_document())
    out, err = _templates()
    assert json.loads(out)[KEY]["references"] == 20
    assert f"{KEY}: 20 ref(s), majority 20 (100.0%): kept" in err


def test_templates_only_learns_from_genuine_ready_rows(migrated_db):
    for n in range(1, 3):
        _ready_clip(n, genuine_p2_document())
    templated = _ready_clip(3, _odd())
    templated.plan = {**templated.plan, "technical_source": f"template:{KEY}"}
    templated.save()
    legacy = _ready_clip(4, genuine_p2_document())
    del legacy.plan["technical_source"]
    legacy.save()
    unexpected = _ready_clip(5, _odd())
    unexpected.verdict = "unexpected"
    unexpected.save()
    _ready_clip(6, genuine_p2_document(), video_codec=None)
    out, err = _templates("--min-refs", "3", "--min-share", "1")
    assert json.loads(out)[KEY]["references"] == 3
    assert "no template key: 1" in err


@pytest.mark.parametrize(
    "args", [("--min-refs", "0"), ("--min-share", "0"), ("--min-share", "1.5")]
)
def test_templates_thresholds_must_be_sane(migrated_db, args):
    with pytest.raises(CommandError, match=args[0]):
        _templates(*args)


@pytest.mark.parametrize(
    "args", [("--out", "x.json"), ("--min-refs", "3"), ("--min-share", "0.9")]
)
def test_templates_options_are_refused_elsewhere(migrated_db, args):
    with pytest.raises(CommandError, match=args[0]):
        _run(_world(()), "report", *args)


def test_report_counts_ready_rows_per_technical_source(migrated_db):
    _row("VX-1", "ready", plan={"technical_source": "wrapped"})
    _row("VX-2", "ready", plan={"technical_source": "wrapped"})
    _row("VX-3", "ready", plan={"technical_source": f"template:{KEY}"})
    _row("VX-4", "ready", plan={})
    _row("VX-5", "already-migrated", plan={"technical_source": "existing"})
    lines = _run(_world(()), "report").splitlines()
    assert "technical source wrapped: 3" in lines
    assert f"technical source template:{KEY}: 1" in lines
    assert not any("existing" in line for line in lines)


def test_report_counts_ready_rows_with_inferred_audio_bits(migrated_db):
    _row("VX-1", "ready", plan={"audio_bits_inferred": True})
    _row("VX-2", "ready", plan={"technical_source": "wrapped"})
    _row("VX-3", "already-migrated", plan={"audio_bits_inferred": True})
    lines = _run(_world(()), "report").splitlines()
    assert "audio bits inferred: 1" in lines


def test_templates_never_learns_from_a_proxy_copy_or_a_frozen_row(migrated_db):
    for n in range(1, 3):
        _ready_clip(n, genuine_p2_document())
    # a ready 'wrapped' row whose shape is a proxy copy: never a reference
    _ready_clip(3, proxy_copy_document())
    frozen = _ready_clip(4, genuine_p2_document())
    frozen.phase = "shape_posted"
    frozen.save()
    out, err = _templates("--min-refs", "2", "--min-share", "1")
    assert json.loads(out)[KEY]["references"] == 2
    assert f"{KEY}: 2 ref(s), majority 2 (100.0%): kept" in err


def test_templates_drops_per_file_values_that_vary(migrated_db):
    for n, (packets, bitrate) in enumerate([(218, 114_000_000), (497, 113_500_000)]):
        document = genuine_p2_document()
        for body in [document["containerComponent"], *document["videoComponent"]]:
            body.update(numberOfPackets=packets, bitrate=bitrate)
        _ready_clip(n + 1, document)
    out, err = _templates("--min-refs", "2")
    template = json.loads(out)[KEY]["template"]
    for body in [template["containerComponent"], *template["videoComponent"]]:
        assert "numberOfPackets" not in body and "bitrate" not in body
    assert template == strip_for_template(genuine_p2_document())
    assert (
        f"{KEY}: dropped per-file values containerComponent.bitrate, "
        f"containerComponent.numberOfPackets, videoComponent.bitrate, "
        f"videoComponent.numberOfPackets" in err.splitlines()
    )


@pytest.mark.parametrize(
    "metadata, differs",
    [
        ({"timecode_start": "18:25:04:13"}, "containerComponent.startTimecode"),
        ({"duration": "498", "data_size": "283860000"}, "videoComponent.duration"),
    ],
)
def test_templates_rejects_a_format_whose_round_trip_disagrees(
    migrated_db, metadata, differs
):
    for n in range(1, 3):
        _ready_clip(n, genuine_p2_document())
    _ready_clip(3, genuine_p2_document(), **metadata)
    out, err = _templates("--min-refs", "2")
    assert json.loads(out) == {}
    lines = err.splitlines()
    assert (
        f"{KEY}: 3 ref(s), majority 3 (100.0%): rejected, round trip disagrees "
        f"for 1 reference(s)" in lines
    )
    (detail,) = [line for line in lines if "VX-03 disagrees on" in line]
    assert differs in detail


def test_templates_rejects_a_reference_without_timing(migrated_db):
    for n in range(1, 3):
        _ready_clip(n, genuine_p2_document())
    _ready_clip(3, genuine_p2_document(), timecode_start=None)
    out, err = _templates("--min-refs", "2")
    assert json.loads(out) == {}
    assert "VX-03 disagrees on timing: timecode_start" in err


def test_a_share_rejection_shows_the_top_two_signatures(migrated_db):
    for n in range(1, 4):
        _ready_clip(n, genuine_p2_document())
    _ready_clip(4, _odd())
    out, err = _templates("--min-refs", "2")
    lines = err.splitlines()
    at = lines.index(
        f"{KEY}: 4 ref(s), majority 3 (75.0%): rejected, share below 95.0%"
    )
    assert lines[at + 1 : at + 4] == [
        f"{KEY}:   top signatures: 3 vs 1, differing in video.pixelFormat",
        f"{KEY}:     video.pixelFormat: None (3) vs 'yuv420p10le' (1)",
        "no template key: 0",
    ]


def test_plan_reads_the_audio_depth_from_the_clip_xml(migrated_db):
    world = _world(
        ("VX-1", "VX-2"),
        documents={i: proxy_copy_document() for i in ("VX-1", "VX-2")},
        metadata={
            "VX-1": p2_clip_metadata(),
            # AVC-I_1080/25p has two genuine variants (see AUDIO_BITS_INFERRED),
            # so without XML depth it stays keyless, unlike AVC-I100 1080/50i.
            "VX-2": p2_clip_metadata(
                video_codec="AVC-I_1080/25p",
                framerate="25p",
                audio_bits_per_sample=None,
            ),
        },
        duration="19.88",
        cpaa_marker="true",
    )
    _run(world, "plan", templates={KEY: {"template": p2_template()}})
    rows = {r.item_id: r for r in WrappedMigration.objects.all()}
    assert rows["VX-1"].plan["technical_source"] == f"template:{KEY}"
    assert rows["VX-2"].verdict == "unexpected"
    assert rows["VX-2"].reason == (
        "proxy-copied technical description; P2 metadata incomplete for a template"
    )


def test_a_clipmetadata_row_never_stands_in_for_the_clip_xml(migrated_db):
    world = _proxy_world()
    clip = Clip.objects.get(item_id="VX-1")
    clip.clip_xml = ""
    clip.save()
    ClipMetadata.objects.create(clip=clip, name="audio_bits_per_sample", value="24")
    _run(world, "plan", templates={KEY: {"template": p2_template()}})
    assert WrappedMigration.objects.get(item_id="VX-1").verdict == "unexpected"


def _s16():
    document = genuine_p2_document()
    for body in document["audioComponent"]:
        body.update(codec="pcm_s16le", sampleFormat="AV_SAMPLE_FMT_S16")
    return document


def _s32():
    document = genuine_p2_document()
    for body in document["audioComponent"]:
        body.update(codec="pcm_s24le", sampleFormat="AV_SAMPLE_FMT_S32")
    return document


def test_templates_never_infers_the_audio_depth(migrated_db):
    # explicit 24-bit depth from the XML: keyed as usual
    _ready_clip(1, genuine_p2_document())
    # no XML depth at all: stays keyless, even though this exact format
    # (AVC-I100 1080/50i) is one the planner infers 16-bit audio for
    _ready_clip(2, genuine_p2_document(), audio_bits_per_sample=None)
    out, err = _templates("--min-refs", "1", "--min-share", "1")
    written = json.loads(out)
    assert written[KEY]["references"] == 1
    assert "no template key: 1" in err


def test_templates_separates_the_audio_depth_variants(migrated_db):
    for n in range(1, 4):
        _ready_clip(n, _s16(), audio_bits_per_sample="16")
    for n in range(4, 6):
        _ready_clip(n, _s32(), audio_bits_per_sample="24")
    out, err = _templates("--min-refs", "2", "--min-share", "1")
    written = json.loads(out)
    s16, s24 = "AVC-I_1080/50i|50i|AVC-I100|A16", KEY
    assert sorted(written) == [s16, s24]
    assert written[s16]["references"] == 3 and written[s24]["references"] == 2
    assert written[s16]["template"]["audioComponent"][0]["codec"] == "pcm_s16le"
    assert written[s24]["template"]["audioComponent"][0]["codec"] == "pcm_s24le"


# relocate: align migrated items on the folder names P5 knows

OLD, NEW = "2016/AH_0/", "2016/AH_140710_RENAMED/"
# A sibling shoot whose folder name starts with OLD's, as 2014/140710_...
# does for unrelated shoots on prod: it must never be touched.
SIBLING = "2016/AH_0 bis"


def _migrated():
    """VX-1 under OLD, VX-2 elsewhere and VX-3 in the sibling, all done."""
    world = _world(("VX-1", "VX-2", "VX-3"), folders={"VX-3": SIBLING})
    _run(world, "plan")
    _run(world, "apply", "--all")
    assert set(WrappedMigration.objects.values_list("phase", flat=True)) == {"done"}
    return world


def _relocate(world, tmp_path, *extra):
    return _run(
        world,
        "relocate",
        "--from",
        OLD,
        "--to",
        NEW,
        "--backup",
        str(tmp_path / "clipfiles.json"),
        *extra,
    )


def _originals(item_id):
    return WrappedMigration.objects.get(item_id=item_id).plan["originals"]


def _video(originals):
    (video,) = [o for o in originals if o["kind"] == "video"]
    return video


def test_relocate_moves_a_done_row_onto_the_new_folder(migrated_db, tmp_path):
    world = _migrated()
    gateway = world[0]
    before = _originals("VX-1")
    assert len(before) == 5
    gateway.items["VX-1"]["originalFilename"] = [_video(before)["relative"]]
    untouched = {i: _originals(i) for i in ("VX-2", "VX-3")}
    out = _relocate(world, tmp_path)
    after = _originals("VX-1")
    assert [o["relative"] for o in after] == [
        NEW + o["relative"][len(OLD) :] for o in before
    ]
    assert not {o["file_id"] for o in after} & {o["file_id"] for o in before}
    for original in after:
        found = gateway.find_file("VX-41", original["relative"])
        assert found.file_id == original["file_id"]
        assert gateway.file_state("VX-41", original["file_id"]) == "ARCHIVED"
    for original in before:
        assert gateway.find_file("VX-41", original["relative"]) is None
    assert Clip.objects.get(item_id="VX-1").file_id == _video(after)["file_id"]
    assert gateway.items["VX-1"]["originalFilename"] == [_video(after)["relative"]]
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert (row.phase, row.error) == ("done", "")
    assert "VX-1: relocated 5 files" in out
    assert "relocated: 1" in out and "failed: 0" in out
    assert "VX-1: ok" in _run(world, "verify")
    assert {i: _originals(i) for i in ("VX-2", "VX-3")} == untouched
    relocated_ids = {w[2] for w in gateway.writes if w[0] == "relocate_file"}
    assert relocated_ids == {o["file_id"] for o in before}


def test_relocate_leaves_an_originalfilename_that_is_not_the_old_path(
    migrated_db, tmp_path
):
    world = _migrated()
    world[0].items["VX-1"]["originalFilename"] = ["00924E.MXF"]
    applied = len(world[0].writes)
    out = _relocate(world, tmp_path)
    assert "VX-1: originalFilename left as ['00924E.MXF']" in out
    assert world[0].items["VX-1"]["originalFilename"] == ["00924E.MXF"]
    assert "set_item_metadata" not in world[0].write_names()[applied:]


def test_relocate_finishes_after_a_crash_between_relocate_and_state(
    migrated_db, tmp_path
):
    world = _migrated()
    gateway = world[0]
    video_before = _video(_originals("VX-1"))["relative"]
    gateway.items["VX-1"]["originalFilename"] = [video_before]
    real_relocate = gateway.relocate_file
    crashed = []

    def killed_after_first_relocate(*args):
        real_relocate(*args)
        if not crashed:
            crashed.append(args)
            raise RuntimeError("killed")

    gateway.relocate_file = killed_after_first_relocate
    out = _relocate(world, tmp_path)
    assert "VX-1: FAILED" in out and "killed" in out
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert "killed" in row.error
    # the first file moved (OPEN, new id) but the plan still names the old one
    first = _originals("VX-1")[0]
    assert first["relative"].startswith(OLD)
    assert gateway.find_file("VX-41", first["relative"]) is None
    out = _run(
        world,
        "relocate",
        "--from",
        OLD,
        "--to",
        NEW,
        "--backup",
        str(tmp_path / "second.json"),
    )
    assert "VX-1: relocated 5 files" in out
    after = _originals("VX-1")
    assert all(o["relative"].startswith(NEW) for o in after)
    assert all(gateway.file_state("VX-41", o["file_id"]) == "ARCHIVED" for o in after)
    assert gateway.write_names().count("relocate_file") == 5
    assert WrappedMigration.objects.get(item_id="VX-1").error == ""
    # the crashed file was the video: its resume still moved originalFilename
    assert crashed[0][2] == NEW + video_before[len(OLD) :]
    assert gateway.items["VX-1"]["originalFilename"] == [_video(after)["relative"]]
    assert Clip.objects.get(item_id="VX-1").file_id == _video(after)["file_id"]
    assert "VX-1: ok" in _run(world, "verify")


def test_relocate_is_a_no_op_once_done(migrated_db, tmp_path):
    world = _migrated()
    _relocate(world, tmp_path)
    writes = len(world[0].writes)
    out = _run(
        world,
        "relocate",
        "--from",
        OLD,
        "--to",
        NEW,
        "--backup",
        str(tmp_path / "again.json"),
    )
    assert len(world[0].writes) == writes
    assert "relocated: 0" in out


def test_relocate_only_reports_a_row_that_was_never_applied(migrated_db, tmp_path):
    world = _world(("VX-1", "VX-2"))
    _run(world, "plan")
    _run(world, "apply", "--item", "VX-2")
    plan = WrappedMigration.objects.get(item_id="VX-1").plan
    writes = len(world[0].writes)
    out = _relocate(world, tmp_path)
    assert "VX-1: to re-plan" in out and "to re-plan: 1" in out
    assert len(world[0].writes) == writes
    assert WrappedMigration.objects.get(item_id="VX-1").plan == plan


def test_relocate_skips_a_row_in_progress(migrated_db, tmp_path):
    world = _world()
    _run(world, "plan")
    _run(world, "apply", "--all")
    WrappedMigration.objects.filter(item_id="VX-1").update(phase="shape_posted")
    writes = len(world[0].writes)
    out = _relocate(world, tmp_path)
    assert "VX-1: skipped, in progress (shape_posted)" in out
    assert "in progress: 1" in out
    assert len(world[0].writes) == writes


def test_relocate_backs_up_then_rewrites_the_clipfiles(migrated_db, tmp_path):
    world = _migrated()
    moved = list(
        ClipFile.objects.filter(clip__item_id="VX-1")
        .order_by("pk")
        .values_list("pk", "path")
    )
    kept = list(
        ClipFile.objects.filter(clip__item_id__in=("VX-2", "VX-3"))
        .order_by("pk")
        .values_list("pk", "path")
    )
    out = _relocate(world, tmp_path)
    backup = json.loads((tmp_path / "clipfiles.json").read_text())
    assert backup == {"clipfile": [[pk, path] for pk, path in moved], "clip": []}
    assert list(
        ClipFile.objects.filter(clip__item_id="VX-1")
        .order_by("pk")
        .values_list("pk", "path")
    ) == [
        (pk, path.replace("RUSHES TAPELESS/" + OLD, "RUSHES TAPELESS/" + NEW))
        for pk, path in moved
    ]
    assert (
        list(
            ClipFile.objects.filter(clip__item_id__in=("VX-2", "VX-3"))
            .order_by("pk")
            .values_list("pk", "path")
        )
        == kept
    )
    assert sum(SIBLING + "/" in path for _, path in kept) == 5
    assert "clipfiles rewritten: 5" in out and "clips rewritten: 0" in out


def test_relocate_refuses_to_overwrite_a_backup(migrated_db, tmp_path):
    world = _migrated()
    (tmp_path / "clipfiles.json").write_text("[]")
    with pytest.raises(CommandError, match="--backup"):
        _relocate(world, tmp_path)
    assert all(o["relative"].startswith(OLD) for o in _originals("VX-1"))


def test_relocate_dryrun_prints_every_write_and_changes_nothing(migrated_db, tmp_path):
    world = _migrated()
    gateway = world[0]
    gateway.items["VX-1"]["originalFilename"] = [_video(_originals("VX-1"))["relative"]]
    writes = len(gateway.writes)
    plan = WrappedMigration.objects.get(item_id="VX-1").plan
    clip_file_id = Clip.objects.get(item_id="VX-1").file_id
    paths = list(ClipFile.objects.order_by("pk").values_list("path", flat=True))
    out = _run(world, "relocate", "--from", OLD, "--to", NEW, "--dryrun")
    assert out.count("VX-1: relocate_file") == 5
    assert out.count("VX-1: set_file_state") == 5
    assert "VX-1: set_item_metadata" in out and "VX-1: clip_update" in out
    assert out.count("clipfile ") == 5
    assert SIBLING not in out
    assert "VX-3" not in out
    assert len(gateway.writes) == writes
    assert WrappedMigration.objects.get(item_id="VX-1").plan == plan
    assert Clip.objects.get(item_id="VX-1").file_id == clip_file_id
    assert list(ClipFile.objects.order_by("pk").values_list("path", flat=True)) == (
        paths
    )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "args, message",
    [
        (["relocate", "--to", NEW, "--dryrun"], "--from"),
        (["relocate", "--from", OLD, "--dryrun"], "--to"),
        (["relocate", "--from", "2016/AH_0", "--to", NEW, "--dryrun"], "end with"),
        (["relocate", "--from", OLD, "--to", "2016/AH_X", "--dryrun"], "end with"),
        (["relocate", "--from", OLD, "--to", OLD, "--dryrun"], "differ"),
        (["relocate", "--from", OLD, "--to", OLD + "SUB/", "--dryrun"], "inside"),
        (["relocate", "--from", OLD, "--to", NEW], "--backup"),
        (["plan", "--from", OLD], "--from"),
        (["plan", "--to", NEW], "--to"),
        (["apply", "--all", "--backup", "b.json"], "--backup"),
    ],
)
def test_relocate_arguments_are_validated(migrated_db, args, message):
    with pytest.raises(CommandError, match=message):
        _run(_world(), *args)


def _again(world, tmp_path, name, *extra):
    return _run(
        world,
        "relocate",
        "--from",
        OLD,
        "--to",
        NEW,
        "--backup",
        str(tmp_path / name),
        *extra,
    )


def _new_path(relative):
    return NEW + relative[len(OLD) :]


def test_relocate_refuses_an_entity_already_at_the_new_path(migrated_db, tmp_path):
    # I-1: an AH_ entity from a storage scan must never be adopted. The
    # conflict is on the LAST original: the row is still left untouched.
    world = _migrated()
    gateway = world[0]
    last = _originals("VX-1")[-1]
    gateway.files[("VX-41", _new_path(last["relative"]))] = "VX-ORPHAN"
    plan = WrappedMigration.objects.get(item_id="VX-1").plan
    clip_file_id = Clip.objects.get(item_id="VX-1").file_id
    writes = len(gateway.writes)
    dry = _run(world, "relocate", "--from", OLD, "--to", NEW, "--dryrun")
    assert "VX-1: FAILED" in dry and "conflict: entity already at new path" in dry
    out = _relocate(world, tmp_path)
    assert "VX-1: FAILED" in out and "conflict: entity already at new path" in out
    assert "VX-ORPHAN" in out
    assert len(gateway.writes) == writes
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert row.plan == plan and "conflict" in row.error
    assert Clip.objects.get(item_id="VX-1").file_id == clip_file_id


def test_relocate_refuses_a_new_entity_the_shape_does_not_reference(
    migrated_db, tmp_path
):
    # I-1: a relocation that leaves the shape on another entity stops
    # before the state, Clip, originalFilename and plan writes.
    world = _migrated()
    gateway = world[0]
    video = _video(_originals("VX-1"))
    gateway.items["VX-1"]["originalFilename"] = [video["relative"]]

    def unreferenced(storage_id, file_id, new_relative):
        (key,) = [k for k, v in gateway.files.items() if v == file_id]
        del gateway.files[key]
        gateway.files[(storage_id, new_relative)] = "VX-STRAY"
        gateway.file_states[(storage_id, "VX-STRAY")] = "OPEN"
        gateway.writes.append(("relocate_file", storage_id, file_id, new_relative))

    gateway.relocate_file = unreferenced
    clip_file_id = Clip.objects.get(item_id="VX-1").file_id
    out = _relocate(world, tmp_path)
    assert "VX-1: FAILED" in out and "VX-STRAY" in out
    assert "set_file_state" not in gateway.write_names()
    assert gateway.items["VX-1"]["originalFilename"] == [video["relative"]]
    assert Clip.objects.get(item_id="VX-1").file_id == clip_file_id
    assert _video(_originals("VX-1")) == video


def test_relocate_refuses_an_old_entity_that_is_not_the_planned_one(
    migrated_db, tmp_path
):
    world = _migrated()
    gateway = world[0]
    first = _originals("VX-1")[0]
    gateway.files[("VX-41", first["relative"])] = "VX-OTHER"
    writes = len(gateway.writes)
    out = _relocate(world, tmp_path)
    assert "VX-1: FAILED" in out and "VX-OTHER" in out
    assert len(gateway.writes) == writes


def test_relocate_refuses_an_original_with_no_entity_at_either_path(
    migrated_db, tmp_path
):
    world = _migrated()
    gateway = world[0]
    last = _originals("VX-1")[-1]
    del gateway.files[("VX-41", last["relative"])]
    writes = len(gateway.writes)
    out = _relocate(world, tmp_path)
    assert "VX-1: FAILED" in out and "no VX-41 entity" in out
    assert len(gateway.writes) == writes


def test_relocate_skips_a_row_that_already_fails_verify(migrated_db, tmp_path):
    # I-2: a failure that predates the relocation is reported as such,
    # and nothing is written for that row.
    world = _migrated()
    gateway = world[0]
    gateway.shapes["VX-1"].append({"id": "VX-EXTRA-LOW", "tag": ["lowres"]})
    plan = WrappedMigration.objects.get(item_id="VX-1").plan
    writes = len(gateway.writes)
    out = _relocate(world, tmp_path)
    assert "VX-1: pre-existing verify failure: lowres shapes" in out
    assert "pre-existing verify failure: 1" in out
    assert len(gateway.writes) == writes
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert (row.plan, row.error) == (plan, "")


def test_relocate_rechecks_a_moved_row_until_it_verifies(migrated_db, tmp_path):
    # I-2: every file moved, then verify failed: the row stays selected
    # on the next runs, and its error is cleared once verify passes.
    world = _migrated()
    gateway = world[0]
    real_relocate = gateway.relocate_file

    def relocate_and_break_lowres(*args):
        real_relocate(*args)
        if not any(d["id"] == "VX-NEW-LOW" for d in gateway.shapes["VX-1"]):
            gateway.shapes["VX-1"].append({"id": "VX-NEW-LOW", "tag": ["lowres"]})

    gateway.relocate_file = relocate_and_break_lowres
    out = _relocate(world, tmp_path)
    assert "VX-1: FAILED" in out and "lowres" in out
    assert all(o["relative"].startswith(NEW) for o in _originals("VX-1"))
    out = _again(world, tmp_path, "second.json")
    assert "VX-1: FAILED" in out and "lowres" in out
    assert "failed: 1" in out
    gateway.shapes["VX-1"] = [
        d for d in gateway.shapes["VX-1"] if d["id"] != "VX-NEW-LOW"
    ]
    # a file left OPEN (say, reset by hand) is set back to ARCHIVED
    video = _video(_originals("VX-1"))
    gateway.file_states[("VX-41", video["file_id"])] = "OPEN"
    out = _again(world, tmp_path, "third.json")
    assert "VX-1: rechecked 5 relocated files" in out
    assert gateway.file_state("VX-41", video["file_id"]) == "ARCHIVED"
    assert "relocate_file" not in [
        w[0] for w in gateway.writes[-2:]
    ]  # the recheck never relocates
    assert WrappedMigration.objects.get(item_id="VX-1").error == ""


def test_relocate_never_rechecks_a_row_planned_under_the_new_folder(
    migrated_db, tmp_path
):
    # A row re-planned from the rewritten ClipFiles lives under NEW from the
    # start: its on-disk originals are CLOSED and must stay so.
    world = _world(("VX-1",), folders={"VX-1": NEW.rstrip("/")})
    world[1].entries.clear()
    world[2].contents.update(
        {o.relative: b"x" for o in p2_originals(clip_dir=NEW + "CONTENTS")}
    )
    _run(world, "plan")
    _run(world, "apply", "--all")
    assert WrappedMigration.objects.get(item_id="VX-1").phase == "done"
    writes = len(world[0].writes)
    out = _relocate(world, tmp_path)
    assert "VX-1" not in out
    assert len(world[0].writes) == writes


def test_relocate_moves_only_the_originals_under_the_old_folder(migrated_db, tmp_path):
    world = _migrated()
    row = WrappedMigration.objects.get(item_id="VX-1")
    elsewhere = "2016/ELSEWHERE/CONTENTS/AUDIO/00924E03.MXF"
    row.plan["originals"][-1]["relative"] = elsewhere
    row.save()
    out = _relocate(world, tmp_path)
    assert "VX-1: relocated 4 files" in out
    after = _originals("VX-1")
    assert after[-1]["relative"] == elsewhere
    assert all(o["relative"].startswith(NEW) for o in after[:-1])


def test_relocate_limit_counts_the_rows_it_moves(migrated_db, tmp_path):
    world = _world(("VX-1", "VX-2"), folders={"VX-2": OLD + "SUB"})
    _run(world, "plan")
    _run(world, "apply", "--all")
    out = _relocate(world, tmp_path, "--limit", "1")
    assert "relocated: 1" in out
    assert all(o["relative"].startswith(NEW) for o in _originals("VX-1"))
    assert all(o["relative"].startswith(OLD) for o in _originals("VX-2"))


def test_relocate_never_overwrites_a_clip_moved_off_the_old_video(
    migrated_db, tmp_path
):
    # M-3: a Clip re-pointed by hand since apply is a failure, not a target.
    world = _migrated()
    Clip.objects.filter(item_id="VX-1").update(file_id="VX-HAND")
    out = _relocate(world, tmp_path)
    assert "VX-1: FAILED" in out and "clip update matched 0 row(s)" in out
    assert Clip.objects.get(item_id="VX-1").file_id == "VX-HAND"
    assert _video(_originals("VX-1"))["relative"].startswith(OLD)


def test_relocate_fails_a_half_moved_row_whose_verify_breaks(migrated_db, tmp_path):
    # N-1: killed between relocate_file and set_file_state on file 3 of 5,
    # then verify breaks before the rerun. The row is this command's own
    # doing: FAILED with row.error, never "pre-existing", and the entity
    # left OPEN by the kill is set back to ARCHIVED.
    world = _migrated()
    gateway = world[0]
    real_relocate = gateway.relocate_file
    calls = []

    def killed_on_third(*args):
        real_relocate(*args)
        calls.append(args)
        if len(calls) == 3:
            raise RuntimeError("killed")

    gateway.relocate_file = killed_on_third
    out = _relocate(world, tmp_path)
    assert "VX-1: FAILED" in out and "killed" in out
    third = _originals("VX-1")[2]
    assert third["relative"].startswith(OLD) and "relocated_from" not in third
    stranded = gateway.find_file("VX-41", _new_path(third["relative"]))
    assert stranded.state == "OPEN"
    gateway.relocate_file = real_relocate
    gateway.shapes["VX-1"].append({"id": "VX-DRIFT-LOW", "tag": ["lowres"]})
    out = _again(world, tmp_path, "second.json")
    assert "pre-existing" not in out.replace("pre-existing verify failure: 0", "")
    assert "VX-1: FAILED" in out and "lowres" in out
    assert "failed: 1" in out
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert "lowres" in row.error
    assert gateway.file_state("VX-41", stranded.file_id) == "ARCHIVED"
    assert gateway.write_names().count("relocate_file") == 3


def test_relocate_dryrun_leaves_a_stranded_entity_open(migrated_db, tmp_path):
    world = _migrated()
    gateway = world[0]
    first = _originals("VX-1")[0]
    gateway.relocate_file("VX-41", first["file_id"], _new_path(first["relative"]))
    stranded = gateway.find_file("VX-41", _new_path(first["relative"]))
    gateway.shapes["VX-1"].append({"id": "VX-DRIFT-LOW", "tag": ["lowres"]})
    writes = len(gateway.writes)
    out = _run(world, "relocate", "--from", OLD, "--to", NEW, "--dryrun")
    assert "VX-1: FAILED" in out
    assert len(gateway.writes) == writes
    assert gateway.file_state("VX-41", stranded.file_id) == "OPEN"


def test_relocate_leaves_a_video_moved_by_hand_alone(migrated_db, tmp_path):
    # VX-10456: its video was relocated by hand before the command existed;
    # the plan names the new entity and path, without relocated_from, and
    # the 4 audios are still under OLD.
    world = _migrated()
    gateway = world[0]
    row = WrappedMigration.objects.get(item_id="VX-1")
    video = _video(row.plan["originals"])
    moved_rel = _new_path(video["relative"])
    gateway.relocate_file("VX-41", video["file_id"], moved_rel)
    by_hand = gateway.find_file("VX-41", moved_rel).file_id
    gateway.set_file_state("VX-41", by_hand, "ARCHIVED")
    video.update(file_id=by_hand, relative=moved_rel)
    row.save()
    Clip.objects.filter(item_id="VX-1").update(file_id=by_hand)
    writes = len(gateway.writes)
    out = _relocate(world, tmp_path)
    assert "VX-1: relocated 4 files" in out
    after = _originals("VX-1")
    assert _video(after) == {**video}
    assert all(
        o["relative"].startswith(NEW) and o["relocated_from"].startswith(OLD)
        for o in after
        if o["kind"] == "audio"
    )
    relocated = [w for w in gateway.writes[writes:] if w[0] == "relocate_file"]
    assert len(relocated) == 4 and by_hand not in {w[2] for w in relocated}
    assert Clip.objects.get(item_id="VX-1").file_id == by_hand
    assert WrappedMigration.objects.get(item_id="VX-1").error == ""
    assert "VX-1: ok" in _run(world, "verify")


# relocate: the Portal rows (ClipFile, Clip.path, Clip.folder_path) only

STORAGE_ROOT = "/Volumes/PAD_Storage/AA - RUSHES TAPELESS/"
OLD_DIR, NEW_DIR = OLD[:-1], NEW[:-1]


def _portal_clips():
    """Clips on OLD (path + folder_path), on NEW already, an empty
    folder_path, and two neighbours that merely share OLD's start."""
    rows = {
        "OLDCLIP": (OLD_DIR, STORAGE_ROOT + OLD_DIR),
        "OLDSUB": (OLD_DIR + "/sub", STORAGE_ROOT + OLD_DIR + "/sub"),
        "NOFOLDER": (OLD_DIR, ""),
        "ONNEW": (NEW_DIR, STORAGE_ROOT + NEW_DIR),
        "BIS": (OLD_DIR + "_BIS", STORAGE_ROOT + OLD_DIR + "_BIS"),
        "J2": (OLD_DIR + "2", STORAGE_ROOT + OLD_DIR + "2"),
    }
    for umid, (path, folder_path) in rows.items():
        Clip.objects.create(
            umid=umid, path=path, folder_path=folder_path, reference_file="F"
        )
    return rows


def _clip_rows():
    return {
        c.umid: (c.path, c.folder_path)
        for c in Clip.objects.filter(umid__in=list(_portal_clips_names()))
    }


def _portal_clips_names():
    return ("OLDCLIP", "OLDSUB", "NOFOLDER", "ONNEW", "BIS", "J2")


def _portal_only(world, tmp_path, *extra):
    return _run(
        world,
        "relocate",
        "--portal-only",
        "--from",
        OLD,
        "--to",
        NEW,
        "--backup",
        str(tmp_path / "portal.json"),
        *extra,
    )


def _forbid_gateway(world):
    def refuse():
        raise AssertionError("--portal-only must not build the gateway")

    command = _command(world)
    command.gateway_factory = refuse
    return command


def test_portal_only_aligns_clipfiles_and_clips_without_vidispine(
    migrated_db, tmp_path
):
    world = _migrated()
    rows = _portal_clips()
    moved = list(
        ClipFile.objects.filter(clip__item_id="VX-1")
        .order_by("pk")
        .values_list("pk", "path")
    )
    plans = list(WrappedMigration.objects.order_by("pk").values_list("plan", "phase"))
    writes = len(world[0].writes)
    out = StringIO()
    call_command(
        _forbid_gateway(world),
        "relocate",
        "--portal-only",
        "--from",
        OLD,
        "--to",
        NEW,
        "--backup",
        str(tmp_path / "portal.json"),
        stdout=out,
    )
    assert "clipfiles rewritten: 5" in out.getvalue()
    assert "clips rewritten: 3" in out.getvalue()
    assert "re-plan" in out.getvalue()
    assert _clip_rows() == {
        "OLDCLIP": (NEW_DIR, STORAGE_ROOT + NEW_DIR),
        "OLDSUB": (NEW_DIR + "/sub", STORAGE_ROOT + NEW_DIR + "/sub"),
        "NOFOLDER": (NEW_DIR, ""),
        "ONNEW": rows["ONNEW"],
        "BIS": rows["BIS"],
        "J2": rows["J2"],
    }
    assert [
        path
        for _, path in ClipFile.objects.filter(clip__item_id="VX-1")
        .order_by("pk")
        .values_list("pk", "path")
    ] == [
        path.replace("RUSHES TAPELESS/" + OLD, "RUSHES TAPELESS/" + NEW)
        for _, path in moved
    ]
    assert json.loads((tmp_path / "portal.json").read_text()) == {
        "clipfile": [[pk, path] for pk, path in moved],
        "clip": [
            ["NOFOLDER", OLD_DIR, ""],
            ["OLDCLIP", OLD_DIR, STORAGE_ROOT + OLD_DIR],
            ["OLDSUB", OLD_DIR + "/sub", STORAGE_ROOT + OLD_DIR + "/sub"],
        ],
    }
    assert len(world[0].writes) == writes
    assert plans == list(
        WrappedMigration.objects.order_by("pk").values_list("plan", "phase")
    )


def test_portal_only_dryrun_prints_the_rewrites_and_changes_nothing(
    migrated_db, tmp_path
):
    world = _migrated()
    _portal_clips()
    clip_rows = _clip_rows()
    paths = list(ClipFile.objects.order_by("pk").values_list("path", flat=True))
    out = StringIO()
    call_command(
        _forbid_gateway(world),
        "relocate",
        "--portal-only",
        "--from",
        OLD,
        "--to",
        NEW,
        "--dryrun",
        stdout=out,
    )
    text = out.getvalue()
    assert text.count("clipfile ") == 5
    assert f"clip OLDCLIP: path {OLD_DIR} -> {NEW_DIR}" in text
    assert (
        f"clip OLDCLIP: folder_path {STORAGE_ROOT + OLD_DIR} -> "
        f"{STORAGE_ROOT + NEW_DIR}"
    ) in text
    assert f"clip NOFOLDER: path {OLD_DIR} -> {NEW_DIR}" in text
    assert "NOFOLDER: folder_path" not in text
    assert "ONNEW" not in text and "BIS" not in text and "J2" not in text
    assert "clipfiles to rewrite: 5" in text and "clips to rewrite: 3" in text
    assert (
        "dry run: nothing written; after the real run, re-plan the items "
        "(no Vidispine read or write)"
    ) in text
    assert "portal rows aligned" not in text
    assert _clip_rows() == clip_rows
    assert list(ClipFile.objects.order_by("pk").values_list("path", flat=True)) == (
        paths
    )
    assert not list(tmp_path.iterdir())


def test_portal_only_only_applies_to_relocate(migrated_db):
    with pytest.raises(CommandError, match="--portal-only"):
        _run(_world(), "plan", "--portal-only")


def test_portal_only_keeps_the_backup_rules(migrated_db, tmp_path):
    world = _world()
    with pytest.raises(CommandError, match="--backup"):
        _run(world, "relocate", "--portal-only", "--from", OLD, "--to", NEW)
    (tmp_path / "portal.json").write_text("{}")
    with pytest.raises(CommandError, match="never overwritten"):
        _portal_only(world, tmp_path)


def test_relocate_leaves_a_clip_already_on_the_new_folder(migrated_db, tmp_path):
    world = _migrated()
    rows = _portal_clips()
    _relocate(world, tmp_path)
    assert _clip_rows()["ONNEW"] == rows["ONNEW"]
    backup = json.loads((tmp_path / "clipfiles.json").read_text())
    assert "ONNEW" not in [umid for umid, *_ in backup["clip"]]


def test_templates_never_learns_from_a_file_provider_row(migrated_db):
    for n in range(1, 3):
        _ready_clip(n, genuine_p2_document())
    WrappedMigration.objects.filter(item_id="VX-02").update(
        plan={
            "kind": "wrap",
            "provider": "file",
            "technical_source": "wrapped",
            "wrapped_shape": genuine_p2_document(),
        }
    )
    out, err = _templates("--min-refs", "1")
    assert json.loads(out)[KEY]["references"] == 1
    assert f"{KEY}: 1 ref(s), majority 1 (100.0%): kept" in err

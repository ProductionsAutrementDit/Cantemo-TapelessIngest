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
    p2_clip_metadata,
    p2_originals,
    p2_template,
    proxy_copy_document,
    seed_item,
    wrapped_p2_document,
)

LEGACY = "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/"


def _world(items=("VX-1",), storage="VX-2", documents=None, metadata=None):
    gateway, archive = InMemoryGateway(), FakeArchive()
    for n, item_id in enumerate(items):
        document = (documents or {}).get(item_id) or wrapped_p2_document(
            storage=storage
        )
        seed_item(gateway, item_id, document)
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
        for name, value in ((metadata or {}).get(item_id) or {}).items():
            ClipMetadata.objects.create(clip=clip, name=name, value=value)
        for original in p2_originals(clip_dir=f"2016/AH_{n}/CONTENTS"):
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
    assert "post_shape" in out and "retag_shape" in out
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

KEY = "AVC-I_1080/50i|50i|AVC-I100"


def _proxy_world(items=("VX-1",)):
    return _world(
        items,
        documents={i: proxy_copy_document() for i in items},
        metadata={i: p2_clip_metadata() for i in items},
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
    for name, value in p2_clip_metadata(**metadata).items():
        ClipMetadata.objects.create(clip=clip, name=name, value=value)
    return WrappedMigration.objects.create(
        item_id=item_id,
        clip_umid=f"T{n}",
        verdict="ready",
        plan={"kind": "wrap", "technical_source": "wrapped", "wrapped_shape": document},
    )


def _odd():
    document = wrapped_p2_document()
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
        _ready_clip(n, wrapped_p2_document(shape_id=f"VX-S{n}"))
    _ready_clip(4, _odd())
    for n in range(5, 7):
        _ready_clip(n, wrapped_p2_document(), video_codec="DV100_1080/50i")
    before = _snapshot()
    path = tmp_path / "p2_templates.json"
    out, _ = _templates("--out", str(path), "--min-refs", "2", "--min-share", "0.7")
    written = json.loads(path.read_text())
    assert written == {
        KEY: {
            "template": strip_for_template(wrapped_p2_document()),
            "reference_item": "VX-01",
            "references": 3,
            "share": 0.75,
        },
        "DV100_1080/50i|50i": {
            "template": strip_for_template(wrapped_p2_document()),
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
        _ready_clip(n, wrapped_p2_document())
    _ready_clip(4, _odd())
    _ready_clip(5, wrapped_p2_document(), video_codec="DV100_1080/50i")
    path = tmp_path / "p2_templates.json"
    out, _ = _templates("--out", str(path), "--min-refs", "2")
    assert json.loads(path.read_text()) == {}
    lines = out.splitlines()
    assert f"{KEY}: 4 ref(s), majority 3 (75.0%): rejected, share below 95.0%" in lines
    assert (
        "DV100_1080/50i|50i: 1 ref(s), majority 1 (100.0%): rejected, "
        "fewer than 2 references" in lines
    )


def test_templates_defaults_to_20_references(migrated_db):
    for n in range(1, 20):
        _ready_clip(n, wrapped_p2_document())
    out, err = _templates()
    assert json.loads(out) == {}
    assert "rejected, fewer than 20 references" in err
    _ready_clip(20, wrapped_p2_document())
    out, err = _templates()
    assert json.loads(out)[KEY]["references"] == 20
    assert f"{KEY}: 20 ref(s), majority 20 (100.0%): kept" in err


def test_templates_only_learns_from_genuine_ready_rows(migrated_db):
    for n in range(1, 3):
        _ready_clip(n, wrapped_p2_document())
    templated = _ready_clip(3, _odd())
    templated.plan = {**templated.plan, "technical_source": f"template:{KEY}"}
    templated.save()
    legacy = _ready_clip(4, wrapped_p2_document())
    del legacy.plan["technical_source"]
    legacy.save()
    unexpected = _ready_clip(5, _odd())
    unexpected.verdict = "unexpected"
    unexpected.save()
    _ready_clip(6, wrapped_p2_document(), video_codec=None)
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

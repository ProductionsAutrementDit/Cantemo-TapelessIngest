"""Tier 2: the command end to end against the plugin's own fakes."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from portal.plugins.TapelessIngest.management.commands import migrate_wrapped_items
from portal.plugins.TapelessIngest.models.clip import Clip, ClipFile
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from tests.wrapped_fakes import (
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    p2_originals,
    seed_item,
    wrapped_p2_document,
)

LEGACY = "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/"


def _world(items=("VX-1",), storage="VX-2"):
    gateway, archive = InMemoryGateway(), FakeArchive()
    for n, item_id in enumerate(items):
        seed_item(gateway, item_id, wrapped_p2_document(storage=storage))
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
        for original in p2_originals(clip_dir=f"2016/AH_{n}/CONTENTS"):
            ClipFile.objects.create(
                clip=clip, path=LEGACY + original.relative, filetype=original.kind
            )
            archive.archive(to_absolute(original.relative), f"H#{n}{original.relative}")
    return gateway, archive, FakeDisk()


def _run(world, *args):
    gateway, archive, disk = world
    command = migrate_wrapped_items.Command()
    command.gateway_factory = lambda: gateway
    command.archive_factory = lambda: archive
    command.disk_factory = lambda: disk
    out = StringIO()
    call_command(command, *args, stdout=out)
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


def test_apply_isolates_a_failing_item(migrated_db):
    world = _world(("VX-1", "VX-2"))
    _run(world, "plan")
    world[0].shapes["VX-1"].append({"id": "VX-EXTRA-LOW", "tag": ["lowres"]})
    _run(world, "apply")
    rows = {r.item_id: r for r in WrappedMigration.objects.all()}
    assert rows["VX-1"].phase == "clip_updated" and "lowres" in rows["VX-1"].error
    assert rows["VX-2"].phase == "done" and rows["VX-2"].error == ""


def test_apply_dryrun_prints_writes_and_changes_nothing(migrated_db):
    world = _world()
    _run(world, "plan")
    out = _run(world, "apply", "--dryrun")
    assert "post_shape" in out and "retag_shape" in out
    assert world[0].writes == []
    assert WrappedMigration.objects.get(item_id="VX-1").phase == ""


def test_apply_dryrun_lists_the_clip_update(migrated_db):
    world = _world()
    _run(world, "plan")
    out = _run(world, "apply", "--dryrun")
    assert "clip_update" in out and "VX-1" in out
    clip = Clip.objects.get(item_id="VX-1")
    assert clip.output_file == "/mnt/ActiveMedia/CANTEMO_FILES/060A2B34.MXF"


def test_dryrun_is_refused_outside_apply(migrated_db):
    with pytest.raises(CommandError, match="--dryrun"):
        _run(_world(), "plan", "--dryrun")


def test_limit_must_be_positive(migrated_db):
    with pytest.raises(CommandError, match="--limit"):
        _run(_world(), "apply", "--limit", "0")


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

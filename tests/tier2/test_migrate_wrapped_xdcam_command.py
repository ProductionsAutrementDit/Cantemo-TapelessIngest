"""Tier 2: ``plan --provider xdcam`` end to end against the plugin's fakes."""

from io import StringIO

import pytest
from django.core.management import call_command

from portal.plugins.TapelessIngest.management.commands import migrate_wrapped_items
from portal.plugins.TapelessIngest.models.clip import Clip, ClipFile
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from tests.tier1.test_wrapped_ffprobe import NON_REAL_TIME_META
from tests.wrapped_fakes import (
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    file_mov_document,
    seed_item,
    seed_lowres,
)

LEGACY = "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/"
SIZE = 1000


def _xdcam_clip(gateway, archive, n, item_id, spanned=False, clipfiles=True):
    seed_item(gateway, item_id, file_mov_document(file_id=f"VX-W{n}", name=f"W{n}.MXF"))
    seed_lowres(gateway, item_id)
    gateway.file_sizes[f"VX-W{n}"] = SIZE
    clip = Clip.objects.create(
        umid=f"X{n}",
        path="2019/AH_TEST",
        storage_id="VX-41",
        reference_file="F",
        item_id=item_id,
        provider_name="xdcam",
        output_file=f"/mnt/ActiveMedia/CANTEMO_FILES/W{n}.MXF",
        status=Clip.STATUS_IMPORTED,
        clip_xml=NON_REAL_TIME_META,
        spanned=spanned,
    )
    if clipfiles:
        relative = f"2019/AH_TEST/C{n:04d}.MXF"
        ClipFile.objects.create(clip=clip, path=LEGACY + relative, filetype="video")
        archive.archive(to_absolute(relative), f"H#{relative}", size=SIZE + 178)
    return clip


def _world():
    gateway, archive = InMemoryGateway(), FakeArchive()
    _xdcam_clip(gateway, archive, 1, "VX-21")
    _xdcam_clip(gateway, archive, 2, "VX-22", spanned=True, clipfiles=False)
    return gateway, archive, FakeDisk()


def _run(world, *args):
    gateway, archive, disk = world
    command = migrate_wrapped_items.Command()
    command.gateway_factory = lambda: gateway
    command.archive_factory = lambda: archive
    command.disk_factory = lambda: disk
    command.templates_factory = dict
    out = StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def planner_calls(monkeypatch):
    calls = []
    real = migrate_wrapped_items.plan_file_item

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(migrate_wrapped_items, "plan_file_item", spy)
    return calls


def test_plan_provider_xdcam_plans_plain_clips_and_defers_spanned_ones(
    migrated_db, planner_calls
):
    world = _world()
    _run(world, "plan", "--provider", "xdcam")
    rows = {r.item_id: r for r in WrappedMigration.objects.order_by("item_id")}
    assert rows["VX-21"].verdict == "ready", rows["VX-21"].reason
    assert rows["VX-21"].plan["provider"] == "xdcam"
    assert rows["VX-21"].plan["technical_source"] == "copy"
    assert (rows["VX-22"].verdict, rows["VX-22"].reason) == (
        "unexpected",
        "spanned xdcam clip: deferred",
    )
    # the spanned clip never reached the planner
    assert [c["item_id"] for c in planner_calls] == ["VX-21"]
    assert world[0].writes == []


def test_a_nonrealtimemeta_clip_xml_is_never_parsed_as_an_ffprobe(
    migrated_db, planner_calls
):
    _run(_world(), "plan", "--provider", "xdcam")
    (call,) = planner_calls
    assert call["provider"] == "xdcam"
    assert call.get("ffprobe") is None
    assert call.get("ffprobe_size") is None
    assert call.get("ffprobe_description") is None


def test_plan_provider_xdcam_streams_and_reports_progress(migrated_db, monkeypatch):
    monkeypatch.setattr(migrate_wrapped_items, "_PROGRESS_EVERY", 1)
    out = _run(_world(), "plan", "--provider", "xdcam")
    assert "planned 1 clips" in out and "planned 2 clips" in out


def test_an_xdcam_row_is_applied_and_verified_like_a_file_row(migrated_db):
    world = _world()
    _run(world, "plan", "--provider", "xdcam")
    _run(world, "apply", "--item", "VX-21")
    row = WrappedMigration.objects.get(item_id="VX-21")
    assert (row.phase, row.error) == ("done", "")
    assert "VX-21: ok" in _run(world, "verify", "--item", "VX-21")
    assert "technical source copy: 1" in _run(world, "report")

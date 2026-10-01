"""Tier 2: ``plan --provider file`` end to end against the plugin's fakes."""

import posixpath
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
    ffprobe_xml,
    file_mov_document,
    p2_originals,
    seed_item,
    seed_lowres,
    wrapped_p2_document,
)

LEGACY = "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/"
SIZE = 1000


def _file_clip(gateway, archive, n, item_id, document, clipfiles, xml):
    seed_item(gateway, item_id, document)
    seed_lowres(gateway, item_id)
    for body in [document["containerComponent"], *document["videoComponent"]]:
        for entity in body["file"]:
            gateway.file_sizes[entity["id"]] = SIZE
    clip = Clip.objects.create(
        umid=f"F{n}",
        path="2019/AH_TEST",
        storage_id="VX-41",
        reference_file="F",
        item_id=item_id,
        provider_name="file",
        output_file=f"/mnt/ActiveMedia/CANTEMO_FILES/W{n}.mov",
        status=Clip.STATUS_IMPORTED,
        clip_xml=xml,
    )
    for relative in clipfiles:
        ClipFile.objects.create(clip=clip, path=LEGACY + relative, filetype="video")
        archive.archive(to_absolute(relative), f"H#{relative}", size=SIZE + 178)
    return clip


def _world():
    gateway, archive = InMemoryGateway(), FakeArchive()
    xml = ffprobe_xml(size=SIZE)
    # ready: a genuine MOV copy on tape, ffprobe size == wrapped size
    _file_clip(
        gateway,
        archive,
        1,
        "VX-11",
        file_mov_document(file_id="VX-W1", name="W1.mov"),
        ["2019/AH_TEST/A001.MOV"],
        xml,
    )
    # unexpected: a proxy-copied description
    _file_clip(
        gateway,
        archive,
        2,
        "VX-12",
        file_mov_document(
            file_id="VX-W2",
            name="W2.mov",
            video_codec="h264",
            resolution=(480, 272),
            audio_codecs=("aac",),
        ),
        ["2019/AH_TEST/A002.MOV"],
        xml,
    )
    # already-migrated: no ClipFile, the shape names one VX-41 file
    _file_clip(
        gateway,
        archive,
        3,
        "VX-13",
        file_mov_document(
            file_id="VX-W3", storage="VX-41", state="CLOSED", name="2019/AH_TEST/A3.MOV"
        ),
        [],
        xml,
    )
    # unexpected: no ClipFile and the shape names the wrapped VX-2 copy
    _file_clip(
        gateway,
        archive,
        4,
        "VX-14",
        file_mov_document(file_id="VX-W4", name="W4.mov"),
        [],
        xml,
    )
    # unexpected: two ClipFile rows
    _file_clip(
        gateway,
        archive,
        5,
        "VX-15",
        file_mov_document(file_id="VX-W5", name="W5.mov"),
        ["2019/AH_TEST/A005.MOV", "2019/AH_TEST/A005B.MOV"],
        xml,
    )
    # a P2 clip, which --provider file never plans
    seed_item(gateway, "VX-1", wrapped_p2_document())
    p2 = Clip.objects.create(
        umid="P1",
        path="2016/AH_TEST",
        storage_id="VX-41",
        reference_file="F",
        item_id="VX-1",
        provider_name="panasonicP2",
        output_file="/mnt/ActiveMedia/CANTEMO_FILES/060A2B34.MXF",
        status=Clip.STATUS_IMPORTED,
    )
    for original in p2_originals():
        ClipFile.objects.create(
            clip=p2, path=LEGACY + original.relative, filetype=original.kind
        )
        archive.archive(to_absolute(original.relative), f"H#{original.relative}")
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


def _rows():
    return {
        r.item_id: (r.verdict, r.reason)
        for r in WrappedMigration.objects.order_by("item_id")
    }


def test_plan_provider_file_gives_each_item_its_verdict(migrated_db):
    world = _world()
    out = _run(world, "plan", "--provider", "file")
    rows = _rows()
    assert {item: verdict for item, (verdict, _) in rows.items()} == {
        "VX-11": "ready",
        "VX-12": "unexpected",
        "VX-13": "already-migrated",
        "VX-14": "unexpected",
        "VX-15": "unexpected",
    }
    assert rows["VX-12"][1] == (
        "proxy-copied technical description (file); ffprobe route pending"
    )
    assert rows["VX-14"][1] == (
        "no ClipFile and the original shape does not name one VX-41 file"
    )
    assert "ClipFile" in rows["VX-15"][1]
    ready = WrappedMigration.objects.get(item_id="VX-11")
    assert ready.plan["provider"] == "file"
    assert ready.plan["technical_source"] == "copy"
    assert ready.plan["originals"][0]["relative"] == "2019/AH_TEST/A001.MOV"
    assert ready.rollback["clip"]["output_file"] == (
        "/mnt/ActiveMedia/CANTEMO_FILES/W1.mov"
    )
    migrated = WrappedMigration.objects.get(item_id="VX-13")
    assert migrated.plan["provider"] == "file"
    assert "ready: 1" in out and "unexpected: 3" in out
    assert world[0].writes == []


def test_plan_without_provider_still_plans_p2_only(migrated_db):
    world = _world()
    _run(world, "plan")
    (row,) = WrappedMigration.objects.all()
    assert (row.item_id, row.verdict) == ("VX-1", "ready")
    assert "provider" not in row.plan


def test_a_file_row_is_applied_and_verified_like_any_row(migrated_db):
    world = _world()
    _run(world, "plan", "--provider", "file")
    _run(world, "apply", "--item", "VX-11")
    row = WrappedMigration.objects.get(item_id="VX-11")
    assert (row.phase, row.error) == ("done", "")
    assert "VX-11: ok" in _run(world, "verify", "--item", "VX-11")
    assert "technical source copy: 1" in _run(world, "report")
    clip = Clip.objects.get(umid="F1")
    assert clip.file_id == row.plan["originals"][0]["file_id"]
    assert posixpath.basename(row.plan["wrapped_file"]["path"]) == "W1.mov"


@pytest.mark.parametrize("action", ["apply", "verify", "report", "templates"])
def test_provider_file_is_refused_outside_plan(migrated_db, action):
    scope = ("--all",) if action == "apply" else ()
    with pytest.raises(CommandError, match="--provider only applies to 'plan'"):
        _run(_world(), action, "--provider", "file", *scope)


def test_an_unknown_provider_is_refused(migrated_db):
    with pytest.raises(CommandError, match="invalid choice: 'xdcam'"):
        _run(_world(), "plan", "--provider", "xdcam")


def test_provider_p2_stays_accepted_for_every_action(migrated_db):
    _run(_world(), "report", "--provider", "panasonicP2")


def test_plan_keeps_a_row_another_clip_of_the_item_owns(migrated_db):
    world = _world()
    _run(world, "plan")
    p2_row = WrappedMigration.objects.get(item_id="VX-1")
    gateway, archive, _ = world
    # a file clip planned onto the same item as the P2 clip
    _file_clip(
        gateway,
        archive,
        9,
        "VX-1",
        file_mov_document(file_id="VX-W9", name="W9.mov"),
        ["2019/AH_TEST/A009.MOV"],
        ffprobe_xml(size=SIZE),
    )
    out = _run(world, "plan", "--provider", "file", "--item", "VX-1")
    assert "VX-1: kept, row belongs to clip P1" in out
    assert "other clip's row: 1" in out
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert (row.clip_umid, row.plan, row.verdict) == (
        "P1",
        p2_row.plan,
        p2_row.verdict,
    )


def test_plan_provider_file_prints_its_progress(migrated_db, monkeypatch):
    monkeypatch.setattr(migrate_wrapped_items, "_PROGRESS_EVERY", 2)
    out = _run(_world(), "plan", "--provider", "file")
    assert out.splitlines()[:2] == ["planned 2 clips", "planned 4 clips"]

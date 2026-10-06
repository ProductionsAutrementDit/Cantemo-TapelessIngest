"""Tier 2: ``realign-clipfile`` end to end against the plugin's fakes."""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from portal.plugins.TapelessIngest.management.commands import migrate_wrapped_items
from portal.plugins.TapelessIngest.models.clip import Clip, ClipFile
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped import fields, verdicts
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from tests.wrapped_fakes import (
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    file_mov_document,
    seed_item,
)

LEGACY = "/Volumes/Infortrend/AA - RUSHES TAPELESS/"
REASON = "attached file x is not the wrapped output_file y"


def _item(gateway, archive, n, **row):
    item_id = f"VX-{n}"
    current = f"2019/AH_NEW_{n}/PRIVATE/CLIP/C{n:04d}.MP4"
    seed_item(
        gateway,
        item_id,
        file_mov_document(
            file_id=f"VX-F{n}", storage=fields.RUSHES_STORAGE, name=current
        ),
    )
    archive.archive(to_absolute(current), f"H#{n}")
    clip = Clip.objects.create(
        umid=f"U{n}",
        path=f"2019/AH_OLD_{n}",
        folder_path=f"{LEGACY}2019/AH_OLD_{n}",
        storage_id="VX-41",
        reference_file="F",
        item_id=item_id,
        provider_name="xdcam",
        output_file="/mnt/ActiveMedia/CANTEMO_FILES/W.MP4",
        status=Clip.STATUS_IMPORTED,
    )
    ClipFile.objects.create(
        clip=clip,
        path=f"{LEGACY}2019/AH_OLD_{n}/PRIVATE/./CLIP/C{n:04d}.MP4",
        filetype="video",
    )
    values = dict(verdict=verdicts.UNEXPECTED, phase="", reason=REASON)
    values.update(row)
    WrappedMigration.objects.create(item_id=item_id, clip_umid=f"U{n}", **values)


def _world(count=2):
    gateway, archive = InMemoryGateway(), FakeArchive()
    for n in range(1, count + 1):
        _item(gateway, archive, n)
    return gateway, archive


def _run(world, *args):
    gateway, archive = world
    command = migrate_wrapped_items.Command()
    command.gateway_factory = lambda: gateway
    command.archive_factory = lambda: archive
    command.disk_factory = FakeDisk
    command.templates_factory = dict
    out = StringIO()
    call_command(command, "realign-clipfile", *args, stdout=out)
    return out.getvalue()


def _path(n):
    return ClipFile.objects.get(clip_id=f"U{n}").path


def test_dryrun_prints_and_writes_nothing(migrated_db, tmp_path):
    world = _world()
    old = _path(1)
    out = _run(world, "--dryrun")
    new = to_absolute("2019/AH_NEW_1/PRIVATE/CLIP/C0001.MP4")
    pk = ClipFile.objects.get(clip_id="U1").pk
    assert f"VX-1: clipfile {pk}: {old} -> {new}" in out
    assert (
        "dry run: nothing written; realigned: 2, already aligned: 0, skipped: 0" in out
    )
    assert _path(1) == old
    assert "re-plan" not in out


def test_real_run_rewrites_only_the_clipfile_path(migrated_db, tmp_path):
    world = _world()
    old = _path(1)
    clip_before = Clip.objects.filter(umid="U1").values().get()
    row_before = WrappedMigration.objects.filter(item_id="VX-1").values().get()
    backup = tmp_path / "backup.json"
    out = _run(world, "--backup", str(backup))
    pk = ClipFile.objects.get(clip_id="U1").pk
    new = to_absolute("2019/AH_NEW_1/PRIVATE/CLIP/C0001.MP4")
    assert _path(1) == new
    assert Clip.objects.filter(umid="U1").values().get() == clip_before
    assert WrappedMigration.objects.filter(item_id="VX-1").values().get() == row_before
    assert json.loads(backup.read_text())[0] == {
        "item_id": "VX-1",
        "clipfile_pk": pk,
        "old": old,
        "new": new,
    }
    assert "realigned: 2, already aligned: 0, skipped: 0" in out
    assert "re-plan these items to complete them" in out
    assert "dry run" not in out
    assert world[0].writes == []


def test_a_skipped_row_is_listed_and_never_written(migrated_db, tmp_path):
    world = _world()
    ClipFile.objects.filter(clip_id="U2").update(path=LEGACY + "2019/X/OTHER.MP4")
    out = _run(world, "--backup", str(tmp_path / "b.json"))
    assert "VX-2: skipped: basename differs: OTHER.MP4 vs C0002.MP4" in out
    assert "realigned: 1, already aligned: 0, skipped: 1" in out
    assert _path(2) == LEGACY + "2019/X/OTHER.MP4"


def test_an_aligned_row_is_counted_apart(migrated_db, tmp_path):
    world = _world(1)
    ClipFile.objects.filter(clip_id="U1").update(
        path=LEGACY + "2019/AH_NEW_1/PRIVATE/CLIP/C0001.MP4"
    )
    out = _run(world, "--dryrun")
    assert "VX-1: already aligned" in out
    assert "realigned: 0, already aligned: 1, skipped: 0" in out


def test_needs_a_backup_unless_dryrun(migrated_db):
    with pytest.raises(CommandError, match="realign-clipfile needs --backup"):
        _run(_world())


def test_refuses_an_existing_backup(migrated_db, tmp_path):
    backup = tmp_path / "b.json"
    backup.write_text("keep")
    with pytest.raises(CommandError, match="already exists"):
        _run(_world(), "--backup", str(backup))
    assert backup.read_text() == "keep"


def test_only_unexpected_attached_file_rows_with_no_phase_are_selected(
    migrated_db, tmp_path
):
    world = _world(4)
    WrappedMigration.objects.filter(item_id="VX-2").update(phase="shape_posted")
    WrappedMigration.objects.filter(item_id="VX-3").update(reason="spanned: deferred")
    WrappedMigration.objects.filter(item_id="VX-4").update(verdict=verdicts.READY)
    out = _run(world, "--dryrun")
    assert "VX-1: clipfile" in out
    for item in ("VX-2", "VX-3", "VX-4"):
        assert item not in out
    assert "realigned: 1," in out


def test_item_and_limit_narrow_the_selection(migrated_db):
    world = _world(3)
    out = _run(world, "--dryrun", "--item", "VX-2")
    assert "VX-2: clipfile" in out and "VX-1" not in out and "VX-3" not in out
    out = _run(world, "--dryrun", "--limit", "2")
    assert "VX-3" not in out and "realigned: 2," in out

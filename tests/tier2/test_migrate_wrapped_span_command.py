"""Tier 2: ``plan`` resolves a spanned master's take and plans it whole."""

from io import StringIO

from django.core.management import call_command

from portal.plugins.TapelessIngest.management.commands import migrate_wrapped_items
from portal.plugins.TapelessIngest.models.clip import (
    Clip,
    ClipFile,
    ClipMetadata,
    SpannedClips,
)
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from tests.wrapped_fakes import (
    SPAN_CONTENTS,
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    genuine_p2_document,
    p2_originals,
    seed_item,
)

LEGACY = "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/"
TOP = "060A2B340101010501010D4313000000AAAA"


def _clip(umid, name, frames, archive, item_id=None, master=False, **metadata):
    clip = Clip.objects.create(
        umid=umid,
        path="2015/AH_150108_EC225_SAR_COROGNE",
        storage_id="VX-41",
        reference_file="F",
        item_id=item_id,
        provider_name="panasonicP2",
        output_file="/mnt/ActiveMedia/CANTEMO_FILES/060A2B34.MXF" if item_id else None,
        status=Clip.STATUS_IMPORTED,
        spanned=True,
        master_clip=master,
    )
    for original in p2_originals(SPAN_CONTENTS, name, 4):
        ClipFile.objects.create(
            clip=clip, path=LEGACY + original.relative, filetype=original.kind
        )
        archive.archive(to_absolute(original.relative), f"H#{original.relative}")
    values = {"clipname": name, "duration": str(frames), "EditUnit": "1/25"}
    values.update(metadata)
    for key, value in values.items():
        ClipMetadata.objects.create(clip=clip, name=key, value=value)
    return clip


def _world(master=True, rows=True):
    gateway, archive = InMemoryGateway(), FakeArchive()
    seed_item(gateway, "VX-1", genuine_p2_document(frames=250), duration="10")
    head = _clip(
        TOP,
        "0037OO",
        100,
        archive,
        item_id="VX-1",
        master=master,
        Relation_Top_GlobalClipID=TOP,
    )
    if rows:
        for order, (name, frames) in enumerate((("003876", 100), ("0039EX", 50))):
            segment = _clip(f"SEG{order}", name, frames, archive)
            SpannedClips.objects.create(master_clip=head, clip=segment, order=order)
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


def test_a_master_with_a_legacy_chain_is_planned_ready(migrated_db):
    assert "ready: 1" in _run(_world(), "plan")
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert row.verdict == "ready", row.reason
    assert [s["name"] for s in row.plan["segments"]] == ["0037OO", "003876", "0039EX"]
    assert len(row.plan["originals"]) == 15
    assert [o["segment"] for o in row.plan["originals"]] == (
        [0] * 5 + [1] * 5 + [2] * 5
    )
    assert row.rollback["clip"]["output_file"].endswith("060A2B34.MXF")


def test_a_spanned_clip_that_is_not_the_master_is_deferred(migrated_db):
    assert "spanned: 1" in _run(_world(master=False), "plan")
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert row.verdict == "spanned"
    assert row.reason == "spanned P2 clip: not the master of its take"


def test_an_unresolved_take_is_deferred_with_the_chain_reason(migrated_db):
    assert "spanned: 1" in _run(_world(rows=False), "plan")
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert row.verdict == "spanned"
    assert row.reason == "spanned P2 clip: master has no next segment"


def test_a_planned_take_applies_and_verifies(migrated_db):
    world = _world()
    _run(world, "plan")
    _run(world, "apply", "--all")
    row = WrappedMigration.objects.get(item_id="VX-1")
    assert row.phase == "done", row.error
    assert "VX-1: ok" in _run(world, "verify")

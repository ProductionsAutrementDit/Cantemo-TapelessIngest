"""Tier 2: a spanned take is applied as one multi-segment shape plus its
pad-assembly manifest, resumably, and verified against both."""

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.executor import Executor
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import plan_item
from portal.plugins.TapelessIngest.wrapped.verifier import verify_item
from tests.wrapped_fakes import (
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    genuine_p2_document,
    p2_clip_metadata,
    p2_originals,
    p2_span,
    p2_template,
    proxy_copy_document,
    seed_item,
)

ITEM = "VX-35313"
OUTPUT = "/Volumes/ActiveMedia/CANTEMO_FILES/060A2B34.MXF"
KEY = "AVC-I_1080/50i|50i|AVC-I100|A24"


class Crash(Exception):
    pass


class CrashingGateway(InMemoryGateway):
    """Performs the named write, then dies — the process-kill model."""

    def __init__(self, crash_after):
        super().__init__()
        self.crash_after = crash_after

    def __getattribute__(self, name):
        attribute = super().__getattribute__(name)
        if name == super().__getattribute__("crash_after"):

            def crashing(*args, **kwargs):
                attribute(*args, **kwargs)
                self.crash_after = None
                raise Crash(name)

            return crashing
        return attribute


def _clip():
    return Clip.objects.create(
        umid="U1",
        path="2015/AH_150108_EC225_SAR_COROGNE",
        storage_id="VX-41",
        reference_file="F",
        item_id=ITEM,
        provider_name="panasonicP2",
        output_file=OUTPUT,
        status=Clip.STATUS_IMPORTED,
        job_id="VX-J",
        spanned=True,
        master_clip=True,
    )


def _setup(gateway, document=None, span=True, duration="10", **extra):
    seed_item(
        gateway, ITEM, document or genuine_p2_document(frames=250), duration=duration
    )
    segments = p2_span() if span else None
    originals = (
        [f for s in segments for f in (s.video, *s.audios)] if span else p2_originals()
    )
    fake = FakeArchive()
    for n, original in enumerate(originals):
        fake.archive(to_absolute(original.relative), f"AirbusHelicopters#{n}")
    disk = FakeDisk()
    _clip()
    result = plan_item(
        item_id=ITEM,
        originals=[] if span else originals,
        spanned=span,
        output_file=OUTPUT,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        span=segments,
        **extra,
    )
    assert result.verdict == "ready", result.reason
    row = WrappedMigration.objects.create(
        item_id=ITEM,
        clip_umid="U1",
        verdict=result.verdict,
        plan=result.plan,
        rollback=result.rollback,
    )
    return row, disk


def _posted(gateway):
    (document,) = [w[2] for w in gateway.writes if w[0] == "post_shape"]
    return document


def test_a_spanned_take_is_migrated_end_to_end(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    Executor(gateway, disk).run(row)

    row.refresh_from_db()
    assert row.phase == "done" and row.error == ""
    assert gateway.write_names().count("register_file") == 15
    posted = _posted(gateway)
    assert len(posted["videoComponent"]) == 3
    assert len(posted["audioComponent"]) == 12
    ids = [o["file_id"] for o in row.plan["originals"]]
    videos = [o["file_id"] for o in row.plan["originals"] if o["kind"] == "video"]
    assert [v["file"] for v in posted["videoComponent"]] == [
        [{"id": v}] for v in videos
    ]
    assert posted["containerComponent"]["file"] == [{"id": ids[0]}]
    (new,) = gateway.original_shapes(ITEM)
    assert new.shape_id == row.plan["new_shape_id"]
    assert new.file_ids() == frozenset(ids)
    (wrapped,) = [d for d in gateway.shapes[ITEM] if d["id"] == "VX-SW"]
    assert "original" not in wrapped["tag"]
    assert gateway.items[ITEM][fields.PAD_ASSEMBLY_FIELD] == [row.plan["manifest"]]
    (summary,) = [w[2] for w in gateway.writes if w[0] == "set_item_metadata"]
    assert summary[fields.PAD_ASSEMBLY_FIELD] == row.plan["manifest"]
    clip = Clip.objects.get(umid="U1")
    assert clip.file_id == ids[0]
    assert clip.status == Clip.STATUS_SHAPE_POSTED and clip.output_file is None
    assert verify_item(row, gateway) == []


def test_a_crash_between_post_shape_and_metadata_resumes(migrated_db):
    gateway = CrashingGateway(crash_after="post_shape")
    row, disk = _setup(gateway)
    with pytest.raises(Crash):
        Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "files_registered"
    assert fields.PAD_ASSEMBLY_FIELD not in gateway.items[ITEM]

    Executor(gateway, disk).run(WrappedMigration.objects.get(item_id=ITEM))
    row.refresh_from_db()
    assert row.phase == "done"
    assert gateway.write_names().count("post_shape") == 1
    assert len(gateway.original_shapes(ITEM)) == 1
    assert gateway.items[ITEM][fields.PAD_ASSEMBLY_FIELD] == [row.plan["manifest"]]


def test_a_proxy_copied_take_is_posted_from_its_template(migrated_db):
    gateway = InMemoryGateway()
    seed_cpaa = {"portal_p5_migration_done": ["true"]}
    gateway.items[ITEM] = dict(seed_cpaa)
    row, disk = _setup(
        gateway,
        document=proxy_copy_document(),
        clip_metadata=p2_clip_metadata(duration="100"),
        templates={KEY: {"template": p2_template()}},
    )
    Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "done", row.error
    posted = _posted(gateway)
    assert posted["containerComponent"]["duration"]["samples"] == 10_000_000
    assert [v["duration"]["samples"] for v in posted["videoComponent"]] == [
        100,
        100,
        50,
    ]
    assert len(posted["audioComponent"]) == 12


def test_verify_reports_a_manifest_that_differs(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    Executor(gateway, disk).run(row)
    gateway.items[ITEM][fields.PAD_ASSEMBLY_FIELD] = ['{"schema": "other"}']
    assert verify_item(row, gateway) == ["pad-assembly manifest differs"]
    del gateway.items[ITEM][fields.PAD_ASSEMBLY_FIELD]
    assert verify_item(row, gateway) == ["pad-assembly manifest differs"]


def test_a_single_clip_row_writes_and_checks_no_manifest(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, document=None, span=False, duration="8.72")
    assert "manifest" not in row.plan and "segments" not in row.plan
    Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "done", row.error
    assert fields.PAD_ASSEMBLY_FIELD not in gateway.items[ITEM]
    (summary,) = [w[2] for w in gateway.writes if w[0] == "set_item_metadata"]
    assert fields.PAD_ASSEMBLY_FIELD not in summary
    gateway.items[ITEM][fields.PAD_ASSEMBLY_FIELD] = ["anything"]
    assert verify_item(row, gateway) == []

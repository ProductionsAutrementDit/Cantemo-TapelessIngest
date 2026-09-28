"""Tier 2: apply advances a row phase by phase, and survives a crash."""

import re

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.dryrun import RecordingGateway
from portal.plugins.TapelessIngest.wrapped.executor import Executor, StepError
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import plan_item
from portal.plugins.TapelessIngest.wrapped.shape import build_document_from_template
from portal.plugins.TapelessIngest.wrapped.templates import Timing
from portal.plugins.TapelessIngest.wrapped.verifier import verify_item
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

ITEM = "VX-35313"
OUTPUT = "/Volumes/ActiveMedia/CANTEMO_FILES/060A2B34.MXF"


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
                result = attribute(*args, **kwargs)
                self.crash_after = None
                raise Crash(name)

            return crashing
        return attribute


def _setup(
    gateway,
    storage="VX-2",
    state="ARCHIVED",
    on_disk=False,
    document=None,
    duration="8.72",
    cpaa_marker=None,
    **extra,
):
    seed_item(
        gateway,
        ITEM,
        document or wrapped_p2_document(storage=storage, state=state),
        duration=duration,
        cpaa_marker=cpaa_marker,
    )
    originals = p2_originals()
    fake = FakeArchive()
    for n, original in enumerate(originals):
        fake.archive(to_absolute(original.relative), f"AirbusHelicopters#{n}")
    disk = FakeDisk({o.relative: b"ess" for o in originals} if on_disk else {})
    Clip.objects.create(
        umid="U1",
        path="2016/AH_TEST",
        storage_id="VX-41",
        reference_file="F",
        item_id=ITEM,
        provider_name="panasonicP2",
        output_file=OUTPUT,
        status=Clip.STATUS_IMPORTED,
        job_id="VX-J",
    )
    result = plan_item(
        item_id=ITEM,
        originals=originals,
        spanned=False,
        output_file=OUTPUT,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        **extra,
    )
    row = WrappedMigration.objects.create(
        item_id=ITEM,
        clip_umid="U1",
        verdict=result.verdict,
        plan=result.plan,
        rollback=result.rollback,
    )
    return row, disk


def test_a_tape_only_item_is_migrated_end_to_end(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    Executor(gateway, disk).run(row)

    row.refresh_from_db()
    assert row.phase == "done" and row.error == ""
    assert [w[3] for w in gateway.writes if w[0] == "register_file"] == [True] * 5
    (new,) = gateway.original_shapes(ITEM)
    assert new.shape_id == row.plan["new_shape_id"]
    assert gateway.shape_ids(ITEM, "legacy-wrapped") == ["VX-SW"]
    assert gateway.shape_ids(ITEM, "lowres") == ["VX-LOW"]
    handles = sorted(
        gateway.component_metadata(ITEM, new.shape_id, c.component_id)[
            fields.EXTERNAL_ID_FIELD
        ]
        for c in new.components
    )
    # container and video both name the video original
    assert handles == sorted(
        ["AirbusHelicopters#0"] * 2 + [f"AirbusHelicopters#{n}" for n in range(1, 5)]
    )
    assert gateway.items[ITEM][fields.ARCHIVE_STATUS_FIELD] == ["Archived"]
    clip = Clip.objects.get(umid="U1")
    assert clip.status == Clip.STATUS_SHAPE_POSTED
    assert clip.output_file is None and clip.job_id == ""
    assert clip.file_id == row.plan["originals"][0]["file_id"]
    assert "delete_file" not in gateway.write_names()  # VX-2: tape only


def test_the_new_shape_is_posted_before_the_wrapped_one_is_detached(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    Executor(gateway, disk).run(row)
    names = gateway.write_names()
    assert names.index("post_shape") < names.index("retag_shape")


def test_crash_after_post_shape_does_not_post_twice(migrated_db):
    gateway = CrashingGateway(crash_after="post_shape")
    row, disk = _setup(gateway)
    with pytest.raises(Crash):
        Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "files_registered"

    Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "done"
    assert gateway.write_names().count("post_shape") == 1
    assert gateway.write_names().count("register_file") == 5


@pytest.mark.parametrize(
    "crash_after",
    ["register_file", "set_component_metadata", "set_item_metadata", "retag_shape"],
)
def test_every_crash_point_resumes_to_the_same_end_state(migrated_db, crash_after):
    gateway = CrashingGateway(crash_after=crash_after)
    row, disk = _setup(gateway)
    with pytest.raises(Crash):
        Executor(gateway, disk).run(row)
    Executor(gateway, disk).run(WrappedMigration.objects.get(item_id=ITEM))
    row.refresh_from_db()
    assert row.phase == "done"
    assert len(gateway.original_shapes(ITEM)) == 1
    assert gateway.write_names().count("post_shape") == 1
    assert gateway.write_names().count("register_file") == 5


def test_an_online_wrapped_file_is_kept_by_default(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, storage="VX-26", state="CLOSED")
    Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "done"
    assert "delete_file" not in gateway.write_names()
    assert row.plan["wrapped_kept"] is True


def test_an_online_wrapped_file_is_deleted_only_after_verification(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, storage="VX-26", state="CLOSED")
    Executor(gateway, disk, delete_online_wrapped=True).run(row)
    names = gateway.write_names()
    assert names[-1] == "delete_file"
    assert gateway.writes[-1] == ("delete_file", "VX-26", "VX-W1")
    row.refresh_from_db()
    assert "wrapped_kept" not in row.plan


@pytest.mark.parametrize("state", ["IMPORTED", "NOT_IMPORTED", "CLOSED"])
def test_every_online_state_is_deleted_when_asked(migrated_db, state):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, storage="VX-11", state="CLOSED")
    gateway.file_states[("VX-11", "VX-W1")] = state
    Executor(gateway, disk, delete_online_wrapped=True).run(row)
    assert gateway.writes[-1] == ("delete_file", "VX-11", "VX-W1")


@pytest.mark.parametrize("state", ["OPEN", "UNKNOWN", "ARCHIVED", "LOST", "MISSING"])
def test_a_state_outside_the_online_allowlist_is_not_deleted(migrated_db, state):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, storage="VX-26", state="CLOSED")
    gateway.file_states[("VX-26", "VX-W1")] = state
    Executor(gateway, disk, delete_online_wrapped=True).run(row)
    row.refresh_from_db()
    assert row.phase == "done"
    assert "delete_file" not in gateway.write_names()


def test_crash_after_delete_does_not_delete_twice(migrated_db):
    gateway = CrashingGateway(crash_after="delete_file")
    row, disk = _setup(gateway, storage="VX-26", state="CLOSED")
    with pytest.raises(Crash):
        Executor(gateway, disk, delete_online_wrapped=True).run(row)
    row.refresh_from_db()
    assert row.phase == "verified"

    Executor(gateway, disk, delete_online_wrapped=True).run(row)
    row.refresh_from_db()
    assert row.phase == "done"
    assert gateway.write_names().count("delete_file") == 1


def test_a_file_already_gone_is_not_deleted(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, storage="VX-26", state="CLOSED")
    gateway.file_states[("VX-26", "VX-W1")] = None
    Executor(gateway, disk, delete_online_wrapped=True).run(row)
    row.refresh_from_db()
    assert row.phase == "done"
    assert "delete_file" not in gateway.write_names()


def test_on_disk_originals_get_a_sha1(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, on_disk=True)
    Executor(gateway, disk).run(row)
    (new,) = gateway.original_shapes(ITEM)
    written = gateway.component_metadata(
        ITEM, new.shape_id, new.components[0].component_id
    )
    assert written[fields.SHA1_FIELD] == disk.sha1(row.plan["originals"][0]["relative"])
    assert [w[3] for w in gateway.writes if w[0] == "register_file"] == [False] * 5


def test_an_archived_original_gone_from_disk_since_plan_is_registered_archived(
    migrated_db,
):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, on_disk=True)
    gone = row.plan["originals"][2]["relative"]
    del disk.contents[gone]
    Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "done"
    registered = {w[2]: w[3] for w in gateway.writes if w[0] == "register_file"}
    assert registered[gone] is True
    assert [a for rel, a in registered.items() if rel != gone] == [False] * 4
    assert row.plan["originals"][2]["on_disk"] is False
    assert "sha1" not in row.plan["originals"][2]


def test_an_unarchived_original_gone_from_disk_since_plan_stops_the_row(
    migrated_db,
):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, on_disk=True)
    row.plan["originals"][2]["entry"] = None
    row.save()
    gone = row.plan["originals"][2]["relative"]
    del disk.contents[gone]
    with pytest.raises(
        StepError,
        match=f"on-disk original {gone} disappeared since plan and is not in P5",
    ):
        Executor(gateway, disk).run(row)
    assert gateway.writes == []
    row.refresh_from_db()
    assert row.phase == ""


def test_apply_refuses_a_disappeared_on_disk_original_bound_to_an_entity(
    migrated_db,
):
    # R1: at plan time an on-disk original's file_id usually names an
    # ONLINE VX-41 entity found by path. If the file is gone by apply
    # time, reusing that binding is exactly what the planner's F1 gate
    # would have refused had it known — so the row must stop, not reuse.
    gateway = InMemoryGateway()
    for original in p2_originals():
        gateway.register_file(fields.RUSHES_STORAGE, original.relative, archived=False)
    gateway.writes.clear()
    row, disk = _setup(gateway, on_disk=True)
    assert all(o["file_id"] for o in row.plan["originals"])
    gone = row.plan["originals"][2]["relative"]
    bound = row.plan["originals"][2]["file_id"]
    del disk.contents[gone]
    message = (
        f"on-disk original {gone} disappeared since plan and is bound to "
        f"VX-41 entity {bound}; re-plan"
    )
    with pytest.raises(StepError, match=re.escape(message)):
        Executor(gateway, disk).run(row)
    assert gateway.writes == []
    row.refresh_from_db()
    assert row.phase == ""


@pytest.mark.parametrize("index", [0, -1])
def test_apply_refuses_a_tape_only_original_whose_entity_appeared_since_plan(
    migrated_db, index
):
    # R1 belt and braces: a tape-only original with no file_id at plan
    # time, for which a non-ARCHIVED VX-41 entity shows up by apply time
    # (a stale index entry the planner never saw), must never be reused —
    # whatever position it sits at among the originals, since every
    # entity is looked up and checked in a read-only pass before the
    # first register_file call (fix round 2: index=-1 pins that a stale
    # entity on the LAST original still stops the row with zero writes,
    # rather than surfacing only after the earlier originals registered).
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, on_disk=False)
    stale = row.plan["originals"][index]
    assert stale["file_id"] is None
    gateway.files[(fields.RUSHES_STORAGE, stale["relative"])] = "VX-STALE"
    gateway.file_states[(fields.RUSHES_STORAGE, "VX-STALE")] = "LOST"
    message = (
        f"VX-41 entity VX-STALE (LOST) for tape-only original "
        f"{stale['relative']}; re-plan"
    )
    with pytest.raises(StepError, match=re.escape(message)):
        Executor(gateway, disk).run(row)
    assert gateway.writes == []
    row.refresh_from_db()
    assert row.phase == ""


def test_an_extra_original_shape_since_plan_stops_before_posting(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    gateway.shapes[ITEM].append(wrapped_p2_document(shape_id="VX-SX"))
    with pytest.raises(StepError, match=r"\['VX-SW', 'VX-SX'\]"):
        Executor(gateway, disk).run(row)
    assert "post_shape" not in gateway.write_names()
    row.refresh_from_db()
    assert row.phase == "files_registered"


def test_verification_compares_lowres_shapes_as_a_set(migrated_db):
    gateway = InMemoryGateway()
    gateway.shapes[ITEM] = [{"id": "VX-LOW2", "tag": ["lowres"]}]
    row, disk = _setup(gateway)
    assert row.rollback["lowres_shape_ids"] == ["VX-LOW2", "VX-LOW"]
    Executor(gateway, disk).run(row)
    gateway.shapes[ITEM].reverse()
    assert gateway.shape_ids(ITEM, "lowres") == ["VX-LOW", "VX-LOW2"]
    assert verify_item(row, gateway) == []


def test_a_failed_verification_stops_before_done(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    gateway.shapes[ITEM].append({"id": "VX-INTRUDER", "tag": ["lowres"]})
    with pytest.raises(Exception, match="lowres"):
        Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "clip_updated"


def test_already_migrated_items_only_get_metadata(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    Executor(gateway, disk).run(row)  # migrate once
    completed = WrappedMigration.objects.create(
        item_id="VX-2",
        clip_umid="U2",
        verdict="already-migrated",
        plan={
            "kind": "complete",
            "new_shape_id": row.plan["new_shape_id"],
            "originals": row.plan["originals"],
        },
        rollback={**row.rollback, "lowres_shape_ids": ["VX-LOW"]},
    )
    gateway.shapes["VX-2"] = gateway.shapes[ITEM]
    gateway.items["VX-2"] = gateway.items[ITEM]
    gateway.writes.clear()
    Executor(gateway, disk).run(completed)
    assert set(gateway.write_names()) == {"set_component_metadata", "set_item_metadata"}


def test_dry_run_writes_nothing_and_lists_every_write(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    recording = RecordingGateway(gateway)
    executor = Executor(recording, disk, persist=False)
    executor.run(row)
    assert gateway.writes == []
    names = [w[0] for w in recording.writes]
    assert names.count("register_file") == 5
    assert names.count("post_shape") == 1
    assert "retag_shape" in names
    assert WrappedMigration.objects.get(item_id=ITEM).phase == ""
    assert Clip.objects.get(umid="U1").output_file == OUTPUT
    # the caller's row object is untouched
    assert row.phase == ""
    assert row.plan["originals"][0]["file_id"] is None
    assert [u["umid"] for u in executor.planned_clip_updates] == ["U1"]


def test_dry_run_lists_the_delete_of_an_online_wrapped_file(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, storage="VX-26", state="CLOSED")
    recording = RecordingGateway(gateway)
    Executor(recording, disk, persist=False, delete_online_wrapped=True).run(row)
    assert gateway.writes == []
    assert recording.writes[-1] == ("delete_file", "VX-26", "VX-W1")


def test_dry_run_lists_no_delete_without_the_flag(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway, storage="VX-26", state="CLOSED")
    recording = RecordingGateway(gateway)
    Executor(recording, disk, persist=False).run(row)
    assert "delete_file" not in [w[0] for w in recording.writes]


def test_a_clip_update_matching_no_row_fails(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    row.clip_umid = "NOPE"
    row.save()
    with pytest.raises(Exception, match="NOPE"):
        Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "old_shape_removed"


def test_stop_before_must_name_a_phase(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    with pytest.raises(ValueError):
        Executor(gateway, disk).run(row, stop_before="verifed")


def test_a_proxy_copied_item_is_posted_from_its_template(migrated_db):
    gateway = InMemoryGateway()
    key = "AVC-I_1080/50i|50i|AVC-I100"
    row, disk = _setup(
        gateway,
        document=proxy_copy_document(),
        duration="19.88",
        cpaa_marker="true",
        clip_metadata=p2_clip_metadata(),
        templates={key: {"template": p2_template()}},
    )
    assert row.plan["technical_source"] == f"template:{key}"
    Executor(gateway, disk).run(row)

    row.refresh_from_db()
    assert row.phase == "done" and row.error == ""
    (posted,) = [w[2] for w in gateway.writes if w[0] == "post_shape"]
    ids = [o["file_id"] for o in row.plan["originals"]]
    assert posted == build_document_from_template(
        p2_template(),
        ids[0],
        ids[1:],
        Timing(frames=497, num=1, den=25, start_tc_frames=1657612),
    )
    assert posted["containerComponent"]["format"] == "mxf_d10"
    assert "video/mp4" not in posted["mimeType"]


def test_a_row_planned_before_technical_source_is_restated_from_wrapped(
    migrated_db,
):
    gateway = InMemoryGateway()
    row, disk = _setup(gateway)
    del row.plan["technical_source"]
    row.save()
    Executor(gateway, disk).run(row)
    (posted,) = [w[2] for w in gateway.writes if w[0] == "post_shape"]
    assert posted["containerComponent"]["format"] == "mxf"
    assert posted["videoComponent"][0]["codec"] == "dvvideo"

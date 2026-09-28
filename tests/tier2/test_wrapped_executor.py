"""Tier 2: apply advances a row phase by phase, and survives a crash."""

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.dryrun import RecordingGateway
from portal.plugins.TapelessIngest.wrapped.executor import Executor, StepError
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import plan_item
from tests.wrapped_fakes import (
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    p2_originals,
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


def _setup(gateway, storage="VX-2", state="ARCHIVED", on_disk=False):
    seed_item(gateway, ITEM, wrapped_p2_document(storage=storage, state=state))
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

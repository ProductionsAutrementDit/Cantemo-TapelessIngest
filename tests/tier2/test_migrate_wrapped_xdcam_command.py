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
    FS7_NRT,
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


# The NRT route: a proxy-copied xdcam shape stated from Clip.clip_xml's NRT,
# the original on tape only and the wrapped copy gone (no size but P5's).


def _nrt_clip(gateway, archive, clip_xml=None, clipfile=True):
    seed_item(
        gateway,
        "VX-31",
        file_mov_document(
            file_id="VX-W3",
            name="W3.MXF",
            video_codec="h264",
            resolution=(480, 272),
            audio_codecs=("aac",),
        ),
    )
    seed_lowres(gateway, "VX-31")
    clip = Clip.objects.create(
        umid="X3",
        path="2019/AH_TEST",
        storage_id="VX-41",
        reference_file="F",
        item_id="VX-31",
        provider_name="xdcam",
        output_file="/mnt/ActiveMedia/CANTEMO_FILES/W3.MXF",
        status=Clip.STATUS_IMPORTED,
        clip_xml=FS7_NRT if clip_xml is None else clip_xml,
    )
    if clipfile:
        relative = "2019/AH_TEST/C0003.mxf"
        ClipFile.objects.create(clip=clip, path=LEGACY + relative, filetype="video")
        archive.archive(to_absolute(relative), f"H#{relative}", size=SIZE + 178)
    return clip


def _nrt_world(**kwargs):
    gateway, archive = InMemoryGateway(), FakeArchive()
    _nrt_clip(gateway, archive, **kwargs)
    return gateway, archive, FakeDisk()


def test_plan_provider_xdcam_states_a_proxy_copy_from_its_nrt(migrated_db):
    from portal.plugins.TapelessIngest.wrapped.nrt import nrt_description, parse_nrt

    world = _nrt_world()
    _run(world, "plan", "--provider", "xdcam")
    row = WrappedMigration.objects.get(item_id="VX-31")
    assert row.verdict == "ready", row.reason
    assert row.plan["technical_source"] == "nrt"
    # the ClipFile's extension is upper-cased into the format key
    assert row.plan["nrt"] == nrt_description(parse_nrt(FS7_NRT), "MXF")[0]
    assert row.plan["size_proof"] == {
        "exact": {},
        "p5": SIZE + 178,
        "p5_alone": True,
    }
    assert world[0].writes == []


def test_the_planner_is_given_the_nrt_description(migrated_db, planner_calls):
    from portal.plugins.TapelessIngest.wrapped.nrt import nrt_description, parse_nrt

    _run(_nrt_world(), "plan", "--provider", "xdcam")
    (call,) = planner_calls
    assert call["nrt"] == nrt_description(parse_nrt(FS7_NRT), "MXF")
    assert call["original"].relative == "2019/AH_TEST/C0003.mxf"


def test_a_clip_xml_that_is_not_nrt_is_named(migrated_db, planner_calls):
    world = _nrt_world(clip_xml="<ffprobe><format size='1'/></ffprobe>")
    _run(world, "plan", "--provider", "xdcam")
    (call,) = planner_calls
    assert call["nrt"] == (None, "no NRT XML")
    row = WrappedMigration.objects.get(item_id="VX-31")
    assert (row.verdict, row.reason) == (
        "unexpected",
        "proxy-copied technical description (xdcam); no NRT XML",
    )


def test_no_clipfile_passes_no_nrt(migrated_db, planner_calls):
    _run(_nrt_world(clipfile=False), "plan", "--provider", "xdcam")
    (call,) = planner_calls
    assert call["original"] is None
    assert call["nrt"] is None


def test_an_nrt_row_is_applied_verified_and_reported(migrated_db):
    from portal.plugins.TapelessIngest.wrapped.shape import build_ffprobe_document

    world = _nrt_world()
    gateway = world[0]
    _run(world, "plan", "--provider", "xdcam")
    _run(world, "apply", "--item", "VX-31")
    row = WrappedMigration.objects.get(item_id="VX-31")
    assert (row.phase, row.error) == ("done", "")
    file_id = row.plan["originals"][0]["file_id"]
    (posted,) = [w[2] for w in gateway.writes if w[0] == "post_shape"]
    assert posted == build_ffprobe_document(row.plan["nrt"], file_id, 8_720_000)
    assert [a["essenceStreamId"] for a in posted["audioComponent"]] == list(
        range(2, 10)
    )
    (new,) = gateway.original_shapes("VX-31")
    assert new.file_ids() == frozenset([file_id])
    assert "VX-31: ok" in _run(world, "verify", "--item", "VX-31")
    assert "technical source nrt: 1" in _run(world, "report")


def test_an_nrt_row_tolerates_vidispines_microsecond_rerendering(migrated_db):
    from portal.plugins.TapelessIngest.wrapped.verifier import verify_item

    world = _nrt_world()
    _run(world, "plan", "--provider", "xdcam")
    _run(world, "apply", "--item", "VX-31")
    row = WrappedMigration.objects.get(item_id="VX-31")
    row.rollback["item_fields"]["durationSeconds"] = ["191.55803333333333"]
    world[0].items["VX-31"]["durationSeconds"] = ["191.558033"]
    assert verify_item(row, world[0]) == []

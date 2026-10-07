"""Tier 1: a wrapped ``xdcam`` item goes through the ``file`` planner (same
copy route, labelled ``xdcam``, no ffprobe)."""

import pytest

from portal.plugins.TapelessIngest.wrapped import verdicts
from tests.tier1.test_wrapped_file_planner import (
    FILE_ORIGINAL,
    ITEM,
    MOV,
    SIZE,
    _plan as _file_plan,
    _world,
    file_mov_document,
    plan_file_item,
)
from tests.wrapped_fakes import FS7_NRT


def _plan(world, **kwargs):
    kwargs.setdefault("ffprobe", None)
    kwargs.setdefault("ffprobe_size", None)
    gateway, fake, disk = world
    from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
    from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile
    from tests.wrapped_fakes import FILE_OUTPUT

    return plan_file_item(
        item_id=ITEM,
        original=kwargs.pop("original", OriginalFile(FILE_ORIGINAL, "video")),
        output_file=FILE_OUTPUT,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        provider="xdcam",
        **kwargs,
    )


def test_a_genuine_xdcam_copy_is_ready_from_its_own_description():
    result = _plan(_world(disk_bytes=b"x" * SIZE))
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["provider"] == "xdcam"
    assert result.plan["technical_source"] == "copy"
    assert result.plan["size_proof"]["exact"] == {"wrapped VX-W1": SIZE, "disk": SIZE}


def test_an_xdcam_item_already_naming_its_original_is_already_migrated():
    world = _world(
        file_mov_document(storage="VX-41", state="CLOSED", name=FILE_ORIGINAL)
    )
    result = _plan(world, original=None)
    assert result.verdict == verdicts.ALREADY_MIGRATED, result.reason
    assert result.plan["provider"] == "xdcam"


def test_a_proxy_copied_xdcam_shape_does_not_promise_an_ffprobe_route():
    world = _world(
        file_mov_document(
            video_codec="h264", resolution=(480, 272), audio_codecs=("aac",)
        )
    )
    result = _plan(world, ffprobe=MOV)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "proxy-copied technical description (xdcam); no ffprobe for this provider"
    )


def test_one_exact_size_and_no_p5_size_is_no_proof_for_xdcam():
    result = _plan(_world(archived=False))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith("no size proof (xdcam): only wrapped VX-W1")


def test_a_p5_size_outside_the_overhead_names_xdcam():
    result = _plan(_world(p5_size=SIZE + 600))
    assert result.reason.startswith(f"P5 size {SIZE + 600} is not the copy's (xdcam)")


def test_no_clipfile_and_a_vx2_file_names_nothing_but_keeps_its_wording():
    result = _plan(_world(), original=None)
    assert result.reason == (
        "no ClipFile and the original shape does not name one VX-41 file"
    )


def test_the_file_provider_wording_is_unchanged():
    result = _file_plan(_world(wrapped_size=None, archived=False))
    assert result.reason == (f"no size proof (file): only ffprobe {SIZE}, no P5 size")


@pytest.mark.parametrize("provider", ["file", "xdcam"])
def test_a_shared_entity_is_labelled_with_its_provider(provider):
    world = _world(
        file_mov_document(storage="VX-41", state="CLOSED", name=FILE_ORIGINAL)
    )
    gateway = world[0]
    gateway.file_items = lambda file_id: ["VX-OTHER"]
    gateway_world = world
    from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive

    result = plan_file_item(
        item_id=ITEM,
        original=None,
        output_file=None,
        gateway=gateway,
        archive=CachedArchive(gateway_world[1]),
        disk=gateway_world[2],
        provider=provider,
    )
    assert result.reason.endswith(f"({provider})")


# The NRT route: a proxy-copied or ambiguous xdcam shape is stated from the
# Sony NRT XML (wrapped.nrt); P5's size alone proves the tape-only original.


def _nrt(xml=None, extension="MXF"):
    from portal.plugins.TapelessIngest.wrapped.nrt import nrt_description, parse_nrt

    return nrt_description(parse_nrt(xml or FS7_NRT), extension)


def _nrt_world(duration="8.72", **kwargs):
    kwargs.setdefault("wrapped_size", None)
    world = _world(
        file_mov_document(
            video_codec="h264", resolution=(480, 272), audio_codecs=("aac",)
        ),
        **kwargs,
    )
    world[0].items[ITEM]["durationSeconds"] = [duration]
    return world


@pytest.mark.parametrize("ffprobe", [None, MOV])
def test_a_proxy_copied_xdcam_shape_with_its_nrt_and_p5_alone_is_ready(ffprobe):
    world = _nrt_world()
    nrt = _nrt()
    result = _plan(world, ffprobe=ffprobe, nrt=nrt)

    assert result.verdict == verdicts.READY, result.reason
    plan = result.plan
    assert plan["provider"] == "xdcam"
    assert plan["technical_source"] == "nrt"
    assert plan["nrt"] == nrt[0]
    assert plan["container_microseconds"] == 8_720_000
    assert "ffprobe" not in plan
    assert plan["size_proof"] == {"exact": {}, "p5": SIZE + 178, "p5_alone": True}
    assert plan["originals"][0]["on_disk"] is False
    assert result.rollback["wrapped_shape_id"] == "VX-SW"
    assert world[0].writes == []


def test_the_nrt_route_without_any_size_is_no_proof():
    result = _plan(_nrt_world(archived=False), nrt=_nrt())
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "no size proof (xdcam): no exact size known"


def test_a_known_exact_size_keeps_todays_size_rules_on_the_nrt_route():
    result = _plan(_nrt_world(wrapped_size=SIZE), nrt=_nrt())
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["size_proof"] == {
        "exact": {"wrapped VX-W1": SIZE},
        "p5": SIZE + 178,
    }


def test_a_known_exact_size_outside_the_p5_overhead_is_refused_on_the_nrt_route():
    result = _plan(_nrt_world(wrapped_size=SIZE, p5_size=SIZE + 600), nrt=_nrt())
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith(f"P5 size {SIZE + 600} is not the copy's (xdcam)")


def test_an_nrt_problem_is_the_unexpected_reason():
    result = _plan(_nrt_world(), nrt=(None, "NRT has no Duration"))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "proxy-copied technical description (xdcam); NRT has no Duration"
    )


def test_no_nrt_xml_is_named():
    result = _plan(_nrt_world(), nrt=(None, "no NRT XML"))
    assert result.reason == "proxy-copied technical description (xdcam); no NRT XML"


def test_an_nrt_duration_more_than_a_fifth_of_a_second_off_is_refused():
    result = _plan(_nrt_world(duration="9.02"), nrt=_nrt())
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "NRT route (xdcam): NRT duration 8.720 s != durationSeconds 9.02"
    )


def test_an_nrt_duration_within_a_fifth_of_a_second_states_the_items_own():
    result = _plan(_nrt_world(duration="8.9"), nrt=_nrt())
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["container_microseconds"] == 8_900_000


def test_the_nrt_route_with_no_duration_seconds_is_refused():
    world = _nrt_world()
    del world[0].items[ITEM]["durationSeconds"]
    result = _plan(world, nrt=_nrt())
    assert result.reason == "NRT route (xdcam): no durationSeconds to cross-check"


def test_a_genuine_xdcam_shape_stays_a_copy_whatever_its_nrt():
    result = _plan(_world(disk_bytes=b"x" * SIZE), nrt=_nrt())
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["technical_source"] == "copy"
    assert "nrt" not in result.plan
    assert "p5_alone" not in result.plan["size_proof"]


def test_a_genuine_xdcam_shape_ignores_an_nrt_problem():
    result = _plan(_world(disk_bytes=b"x" * SIZE), nrt=(None, "no NRT XML"))
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["technical_source"] == "copy"


def test_the_nrt_route_still_refuses_a_shared_entity():
    world = _nrt_world()
    gateway = world[0]
    gateway.files[("VX-41", FILE_ORIGINAL)] = "VX-F0"  # ARCHIVED by default
    gateway.file_items = lambda file_id: ["VX-OTHER"]
    result = _plan(world, nrt=_nrt())
    assert result.verdict == verdicts.UNEXPECTED
    assert "already belongs to item VX-OTHER" in result.reason


def test_a_file_item_never_takes_the_nrt_route():
    world = _nrt_world(wrapped_size=SIZE)
    gateway, fake, disk = world
    from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
    from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile
    from tests.wrapped_fakes import FILE_OUTPUT

    result = plan_file_item(
        item_id=ITEM,
        original=OriginalFile(FILE_ORIGINAL, "video"),
        output_file=FILE_OUTPUT,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        ffprobe=MOV,
        nrt=_nrt(),
    )
    assert result.reason == (
        "proxy-copied technical description (file); ffprobe route pending"
    )


def test_an_original_on_disk_of_unknown_size_never_takes_the_p5_alone_proof():
    # P5 alone stands only for a tape-only original: one on disk must give
    # its own size.
    world = _nrt_world(disk_bytes=b"x" * SIZE)
    world[2].size = lambda relative: None
    result = _plan(world, nrt=_nrt())
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "no size proof (xdcam): no exact size known"


def test_an_nrt_with_neither_description_nor_problem_is_a_bug():
    with pytest.raises(AssertionError):
        _plan(_nrt_world(), nrt=(None, None))

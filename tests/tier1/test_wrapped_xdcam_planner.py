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

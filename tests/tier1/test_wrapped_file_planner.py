"""Tier 1: a wrapped ``file`` item, one verdict and the exact plan (option B)."""

import posixpath

import pytest

from portal.plugins.TapelessIngest.wrapped import verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile, to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import plan_file_item
from tests.wrapped_fakes import (
    FILE_ORIGINAL,
    FILE_OUTPUT,
    WAV_ORIGINAL,
    WAV_OUTPUT,
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    binary_only_document,
    file_mov_document,
    file_wav_document,
    fileless_document,
    seed_item,
    seed_lowres,
)

ITEM = "VX-50001"
SIZE = 1000
MOV = ("prores", (1920, 1080), ("pcm_s24le", "pcm_s24le"))
LOWRES = ("h264", (480, 272), ("aac",))


def _world(
    document=None,
    wrapped_size=SIZE,
    archived=True,
    p5_size=SIZE + 178,
    disk_bytes=None,
    original=FILE_ORIGINAL,
):
    gateway = InMemoryGateway()
    seed_item(gateway, ITEM, document or file_mov_document())
    seed_lowres(gateway, ITEM)
    if wrapped_size is not None:
        gateway.file_sizes["VX-W1"] = wrapped_size
    fake = FakeArchive()
    if archived:
        fake.archive(to_absolute(original), "AirbusHelicopters#1", size=p5_size)
    disk = FakeDisk({} if disk_bytes is None else {original: disk_bytes})
    return gateway, fake, disk


def _plan(
    world,
    original=OriginalFile(FILE_ORIGINAL, "video"),
    output_file=FILE_OUTPUT,
    ffprobe=MOV,
    ffprobe_size=SIZE,
):
    gateway, fake, disk = world
    return plan_file_item(
        item_id=ITEM,
        original=original,
        output_file=output_file,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        ffprobe=ffprobe,
        ffprobe_size=ffprobe_size,
    )


def test_a_genuine_copy_on_tape_is_ready_from_its_own_description():
    world = _world()
    result = _plan(world)

    assert result.verdict == verdicts.READY, result.reason
    plan = result.plan
    assert plan["kind"] == "wrap"
    assert plan["provider"] == "file"
    assert plan["technical_source"] == "copy"
    assert plan["wrapped_shape_id"] == "VX-SW"
    assert plan["wrapped_shape"] == file_mov_document()
    assert plan["wrapped_file"] == {
        "file_id": "VX-W1",
        "storage_id": "VX-2",
        "state": "ARCHIVED",
        "path": posixpath.basename(FILE_OUTPUT),
    }
    (original,) = plan["originals"]
    assert original["relative"] == FILE_ORIGINAL
    assert original["kind"] == "video"
    assert original["on_disk"] is False and original["file_id"] is None
    assert original["entry"]["handle"] == "AirbusHelicopters#1"
    assert plan["size_proof"] == {
        "exact": {"ffprobe": SIZE, "wrapped VX-W1": SIZE},
        "p5": SIZE + 178,
    }
    assert result.rollback["wrapped_shape_id"] == "VX-SW"
    assert result.rollback["lowres_shape_ids"] == ["VX-LOW"]
    assert world[0].writes == []


def test_an_original_on_disk_of_the_same_size_completes_the_proof():
    result = _plan(_world(disk_bytes=b"x" * SIZE))
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["originals"][0]["on_disk"] is True
    assert result.plan["size_proof"]["exact"]["disk"] == SIZE


def test_an_original_on_disk_of_another_size_is_refused():
    result = _plan(_world(disk_bytes=b"x" * (SIZE + 1)))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith("copy and original differ in size: ")
    assert f"disk {SIZE + 1}" in result.reason


def test_ffprobe_disagreeing_with_the_wrapped_file_is_refused():
    result = _plan(_world(wrapped_size=SIZE + 2))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith("copy and original differ in size: ")


def test_one_exact_size_and_the_p5_overhead_is_a_proof():
    result = _plan(_world(wrapped_size=None, p5_size=SIZE + 178))
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["size_proof"] == {
        "exact": {"ffprobe": SIZE},
        "p5": SIZE + 178,
    }


@pytest.mark.parametrize("wrapped_size", [None, SIZE])
@pytest.mark.parametrize("delta", [600, 512, -1])
def test_a_p5_size_outside_the_overhead_is_refused(wrapped_size, delta):
    # Checked whenever P5 knows the original, even with two exact sources.
    result = _plan(_world(wrapped_size=wrapped_size, p5_size=SIZE + delta))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        f"P5 size {SIZE + delta} is not the copy's (file): {delta:+d} bytes "
        f"from {'ffprobe' if wrapped_size is None else 'ffprobe, wrapped VX-W1'} "
        f"{SIZE}, outside [0, 512)"
    )


@pytest.mark.parametrize("delta", [0, 178, 511])
def test_two_exact_sources_and_a_p5_size_within_the_overhead_are_ready(delta):
    result = _plan(_world(p5_size=SIZE + delta))
    assert result.verdict == verdicts.READY, result.reason
    assert result.plan["size_proof"]["p5"] == SIZE + delta


def test_no_exact_size_at_all_is_no_proof():
    result = _plan(_world(wrapped_size=None), ffprobe_size=None)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith("no size proof (file)")


def test_one_exact_size_and_no_p5_entry_is_no_proof():
    result = _plan(_world(wrapped_size=None, archived=False))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith("no size proof (file)")


def test_a_proven_original_neither_on_disk_nor_in_p5_is_missing():
    result = _plan(_world(archived=False))
    assert result.verdict == verdicts.ORIGINALS_MISSING


def test_a_shape_already_naming_the_original_is_already_migrated():
    world = _world()
    gateway = world[0]
    gateway.shapes[ITEM] = [
        {
            "id": "VX-MIG",
            "tag": ["original"],
            "videoComponent": [
                {
                    "id": "V",
                    "file": [
                        {
                            "id": "VX-F0",
                            "storage": "VX-41",
                            "path": FILE_ORIGINAL,
                            "state": "CLOSED",
                        }
                    ],
                }
            ],
        },
        {"id": "VX-LOW", "tag": ["lowres"]},
    ]
    result = _plan(world)
    assert result.verdict == verdicts.ALREADY_MIGRATED
    assert result.plan["kind"] == "complete"
    assert result.plan["provider"] == "file"
    assert result.plan["new_shape_id"] == "VX-MIG"
    (original,) = result.plan["originals"]
    assert original["file_id"] == "VX-F0" and original["kind"] == "video"


@pytest.mark.parametrize(
    "document, relative, kind",
    [
        (file_mov_document, FILE_ORIGINAL, "video"),
        (file_wav_document, WAV_ORIGINAL, "audio"),
    ],
)
def test_no_clipfile_and_one_vx41_file_is_that_original(document, relative, kind):
    world = _world(
        document(storage="VX-41", state="CLOSED", name=relative), original=relative
    )
    result = _plan(world, original=None)
    assert result.verdict == verdicts.ALREADY_MIGRATED, result.reason
    (original,) = result.plan["originals"]
    assert original["relative"] == relative
    assert original["kind"] == kind
    assert original["file_id"] == "VX-W1"
    assert result.plan["provider"] == "file"


def test_no_clipfile_and_a_vx2_file_is_refused():
    result = _plan(_world(), original=None)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "no ClipFile and the original shape does not name one VX-41 file"
    )


def test_a_proxy_copied_description_is_refused():
    world = _world(
        file_mov_document(
            video_codec="h264", resolution=(480, 272), audio_codecs=("aac",)
        )
    )
    result = _plan(world)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "proxy-copied technical description (file); ffprobe route pending"
    )


@pytest.mark.parametrize("ffprobe", [LOWRES, None])
def test_a_description_equal_to_the_lowres_and_unrefuted_is_ambiguous(ffprobe):
    world = _world(
        file_mov_document(
            video_codec="h264", resolution=(480, 272), audio_codecs=("aac",)
        )
    )
    result = _plan(world, ffprobe=ffprobe)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "original shape equals the lowres (file); ambiguous"


def test_a_binary_only_shape_is_refused():
    world = _world(binary_only_document(name=posixpath.basename(FILE_OUTPUT)))
    result = _plan(world)
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == "binary-only original shape (file)"


def test_a_fileless_shape_is_refused():
    result = _plan(_world(fileless_document(file_mov_document())))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason.startswith("fileless original shape (file)")


def test_two_original_shapes_are_refused():
    world = _world()
    world[0].shapes[ITEM].append(dict(file_mov_document(), id="VX-SW2"))
    result = _plan(world)
    assert (result.verdict, result.reason) == (
        verdicts.UNEXPECTED,
        "2 original shapes",
    )


def test_a_shape_naming_another_file_than_output_file_is_refused():
    result = _plan(_world(), output_file="/mnt/ActiveMedia/CANTEMO_FILES/other.mov")
    assert result.verdict == verdicts.UNEXPECTED
    assert "is not the wrapped output_file" in result.reason


def test_an_audio_only_wav_is_ready():
    world = _world(file_wav_document(), original=WAV_ORIGINAL)
    result = _plan(
        world,
        original=OriginalFile(WAV_ORIGINAL, "audio"),
        output_file=WAV_OUTPUT,
        ffprobe=(None, None, ("pcm_s24le",)),
    )
    assert result.verdict == verdicts.READY, result.reason
    (original,) = result.plan["originals"]
    assert original["kind"] == "audio"
    assert result.plan["wrapped_file"]["storage_id"] == "VX-26"


def test_a_resolution_disagreeing_with_ffprobe_is_refused():
    result = _plan(_world(), ffprobe=("prores", (3840, 2160), MOV[2]))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "original shape disagrees with ffprobe (file): "
        "video resolution 1920x1080, ffprobe 3840x2160"
    )


def test_an_audio_count_disagreeing_with_ffprobe_is_refused():
    result = _plan(_world(), ffprobe=("prores", (1920, 1080), ("pcm_s24le",) * 4))
    assert result.verdict == verdicts.UNEXPECTED
    assert result.reason == (
        "original shape disagrees with ffprobe (file): " "2 audio stream(s), ffprobe 4"
    )


def test_codec_names_differing_from_ffprobe_alone_stay_genuine():
    result = _plan(_world(), ffprobe=("unknown", (1920, 1080), ("aac", "aac")))
    assert result.verdict == verdicts.READY, result.reason


def test_without_ffprobe_a_shape_unlike_the_lowres_stays_genuine():
    result = _plan(_world(), ffprobe=None)
    assert result.verdict == verdicts.READY, result.reason


def test_the_p5_entry_is_looked_up_once():
    world = _world()
    _plan(world)
    assert world[1].lookup_calls == [to_absolute(FILE_ORIGINAL)]

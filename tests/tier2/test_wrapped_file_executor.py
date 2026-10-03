"""Tier 2: a genuine wrapped ``file`` item applied end to end (option B)."""

import posixpath

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration
from portal.plugins.TapelessIngest.wrapped import fields, verdicts
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.executor import Executor
from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile, to_absolute
from portal.plugins.TapelessIngest.wrapped.planner import plan_file_item
from portal.plugins.TapelessIngest.wrapped.relocate import Relocator
from portal.plugins.TapelessIngest.wrapped.shape import build_ffprobe_document
from portal.plugins.TapelessIngest.wrapped.verifier import verify_item
from tests.tier1.test_wrapped_ffprobe import PRORES_DESCRIPTION
from tests.wrapped_fakes import (
    FILE_ORIGINAL,
    FILE_OUTPUT,
    WAV_ORIGINAL,
    WAV_OUTPUT,
    FakeArchive,
    FakeDisk,
    InMemoryGateway,
    file_mov_document,
    file_wav_document,
    seed_item,
    seed_lowres,
)

ITEM = "VX-50001"
SIZE = 1000


def _setup(
    gateway, document, original, output_file, signature, on_disk=False, description=None
):
    seed_item(gateway, ITEM, document)
    seed_lowres(gateway, ITEM)
    gateway.file_sizes["VX-W1"] = SIZE
    fake = FakeArchive()
    fake.archive(to_absolute(original.relative), "AirbusHelicopters#7", size=SIZE)
    disk = FakeDisk({original.relative: b"e" * SIZE} if on_disk else {})
    Clip.objects.create(
        umid="U1",
        path="2019/AH_20190402_H175_TEST",
        storage_id="VX-41",
        reference_file="F",
        item_id=ITEM,
        provider_name="file",
        output_file=output_file,
        status=Clip.STATUS_IMPORTED,
        job_id="VX-J",
    )
    result = plan_file_item(
        item_id=ITEM,
        original=original,
        output_file=output_file,
        gateway=gateway,
        archive=CachedArchive(fake),
        disk=disk,
        ffprobe=signature,
        ffprobe_size=SIZE,
        ffprobe_description=description,
    )
    assert result.verdict == verdicts.READY, result.reason
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


@pytest.mark.parametrize("on_disk", [False, True])
def test_a_proxy_copied_mov_is_stated_from_its_ffprobe(migrated_db, on_disk):
    gateway = InMemoryGateway()
    row, disk = _setup(
        gateway,
        file_mov_document(
            video_codec="h264", resolution=(480, 272), audio_codecs=("aac",)
        ),
        OriginalFile(FILE_ORIGINAL, "video"),
        FILE_OUTPUT,
        ("prores", (1920, 1080), ("pcm_s24le", "pcm_s24le")),
        on_disk=on_disk,
        description=PRORES_DESCRIPTION,
    )
    assert row.plan["technical_source"] == "ffprobe"
    Executor(gateway, disk).run(row)

    row.refresh_from_db()
    assert row.phase == "done" and row.error == ""
    file_id = row.plan["originals"][0]["file_id"]
    assert _posted(gateway) == build_ffprobe_document(
        PRORES_DESCRIPTION, file_id, 8_720_000
    )
    (new,) = gateway.original_shapes(ITEM)
    assert new.shape_id == row.plan["new_shape_id"]
    assert new.file_ids() == frozenset([file_id])
    handles = {
        gateway.component_metadata(ITEM, new.shape_id, c.component_id)[
            fields.EXTERNAL_ID_FIELD
        ]
        for c in new.components
    }
    assert handles == {"AirbusHelicopters#7"}
    assert gateway.items[ITEM]["durationSeconds"] == ["8.72"]
    clip = Clip.objects.get(umid="U1")
    assert clip.file_id == file_id and clip.output_file is None
    assert clip.status == Clip.STATUS_SHAPE_POSTED
    assert posixpath.basename(row.plan["wrapped_file"]["path"]) == posixpath.basename(
        FILE_OUTPUT
    )
    assert verify_item(row, gateway) == []


@pytest.mark.parametrize("on_disk", [False, True])
def test_a_genuine_mov_is_restated_onto_its_original(migrated_db, on_disk):
    gateway = InMemoryGateway()
    row, disk = _setup(
        gateway,
        file_mov_document(),
        OriginalFile(FILE_ORIGINAL, "video"),
        FILE_OUTPUT,
        ("prores", (1920, 1080), ("pcm_s24le", "pcm_s24le")),
        on_disk=on_disk,
    )
    Executor(gateway, disk).run(row)

    row.refresh_from_db()
    assert row.phase == "done" and row.error == ""
    ((_, storage, relative, archived),) = [
        w for w in gateway.writes if w[0] == "register_file"
    ]
    assert (storage, relative, archived) == ("VX-41", FILE_ORIGINAL, not on_disk)
    file_id = row.plan["originals"][0]["file_id"]
    document = _posted(gateway)
    assert document["mimeType"] == ["video/quicktime"]
    assert document["containerComponent"]["format"] == "mov,mp4,m4a,3gp,3g2,mj2"
    assert document["containerComponent"]["file"] == [{"id": file_id}]
    assert [a["essenceStreamId"] for a in document["audioComponent"]] == [1, 2]
    assert document["videoComponent"][0]["codec"] == "prores"
    (new,) = gateway.original_shapes(ITEM)
    assert new.shape_id == row.plan["new_shape_id"]
    assert new.file_ids() == frozenset([file_id])
    handles = {
        gateway.component_metadata(ITEM, new.shape_id, c.component_id)[
            fields.EXTERNAL_ID_FIELD
        ]
        for c in new.components
    }
    assert handles == {"AirbusHelicopters#7"}
    clip = Clip.objects.get(umid="U1")
    assert clip.file_id == file_id and clip.output_file is None
    assert clip.status == Clip.STATUS_SHAPE_POSTED
    assert verify_item(row, gateway) == []


def test_an_audio_only_wav_points_its_clip_at_the_wav(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(
        gateway,
        file_wav_document(),
        OriginalFile(WAV_ORIGINAL, "audio"),
        WAV_OUTPUT,
        (None, None, ("pcm_s24le",)),
    )
    Executor(gateway, disk).run(row)

    row.refresh_from_db()
    assert row.phase == "done" and row.error == ""
    (original,) = row.plan["originals"]
    assert original["kind"] == "audio"
    document = _posted(gateway)
    assert "videoComponent" not in document
    assert document["audioComponent"][0]["file"] == [{"id": original["file_id"]}]
    assert Clip.objects.get(umid="U1").file_id == original["file_id"]
    # The online wrapped WAV (VX-26) is kept by default, and flagged.
    assert row.plan["wrapped_kept"] is True
    assert verify_item(row, gateway) == []


def test_relocating_an_audio_only_row_repoints_its_clip_at_the_wav(migrated_db):
    gateway = InMemoryGateway()
    row, disk = _setup(
        gateway,
        file_wav_document(),
        OriginalFile(WAV_ORIGINAL, "audio"),
        WAV_OUTPUT,
        (None, None, ("pcm_s24le",)),
    )
    Executor(gateway, disk).run(row)
    row.refresh_from_db()
    gateway.items[ITEM][fields.ITEM_ORIGINAL_FILENAME_FIELD] = [WAV_ORIGINAL]

    old, new = "2019/AH_20190402_H175_TEST/", "2019/AH_RENAMED/"
    assert Relocator(gateway, old, new).run(row) == 1

    (original,) = row.plan["originals"]
    assert original["relative"] == "2019/AH_RENAMED/SOUND/ZOOM0001.WAV"
    assert Clip.objects.get(umid="U1").file_id == original["file_id"]
    assert gateway.items[ITEM][fields.ITEM_ORIGINAL_FILENAME_FIELD] == [
        original["relative"]
    ]


def _applied(gateway, ffprobe_route):
    if ffprobe_route:
        row, disk = _setup(
            gateway,
            file_mov_document(
                video_codec="h264", resolution=(480, 272), audio_codecs=("aac",)
            ),
            OriginalFile(FILE_ORIGINAL, "video"),
            FILE_OUTPUT,
            ("prores", (1920, 1080), ("pcm_s24le", "pcm_s24le")),
            description=PRORES_DESCRIPTION,
        )
        assert row.plan["technical_source"] == "ffprobe"
    else:
        row, disk = _setup(
            gateway,
            file_mov_document(),
            OriginalFile(FILE_ORIGINAL, "video"),
            FILE_OUTPUT,
            ("prores", (1920, 1080), ("pcm_s24le", "pcm_s24le")),
        )
        assert row.plan.get("technical_source") != "ffprobe"
    Executor(gateway, disk).run(row)
    row.refresh_from_db()
    assert row.phase == "done" and row.error == ""
    return row


def _restated(gateway, row, before, after):
    row.rollback["item_fields"]["durationSeconds"] = before
    gateway.items[ITEM]["durationSeconds"] = after


def test_ffprobe_row_tolerates_vidispines_microsecond_rerendering(migrated_db):
    gateway = InMemoryGateway()
    row = _applied(gateway, True)
    _restated(gateway, row, ["191.55803333333333"], ["191.558033"])
    assert verify_item(row, gateway) == []


@pytest.mark.parametrize(
    "before, after",
    [
        (["191.558033"], ["191.558034"]),
        (["191.558033"], ["191.558032"]),
        (["191.558033"], ["abc"]),
        (["abc"], ["191.558033"]),
        (["191.558033"], None),
        (None, ["191.558033"]),
        (["191.558033"], []),
        (["191.558033"], ["191.558033", "191.558033"]),
    ],
)
def test_ffprobe_row_still_fails_on_a_real_duration_change(migrated_db, before, after):
    gateway = InMemoryGateway()
    row = _applied(gateway, True)
    _restated(gateway, row, before, after)
    if after is None:
        del gateway.items[ITEM]["durationSeconds"]
    problems = verify_item(row, gateway)
    assert len(problems) == 1 and problems[0].startswith("durationSeconds ")


def test_non_ffprobe_row_keeps_the_exact_duration_comparison(migrated_db):
    gateway = InMemoryGateway()
    row = _applied(gateway, False)
    _restated(gateway, row, ["191.55803333333333"], ["191.558033"])
    assert verify_item(row, gateway) == [
        "durationSeconds ['191.558033'] != ['191.55803333333333']"
    ]

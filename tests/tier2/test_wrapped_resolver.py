"""Tier 2: a wrapped P2 clip's originals, read from its ClipFile rows."""

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip, ClipFile
from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile
from portal.plugins.TapelessIngest.wrapped.resolver import (
    FILE_PROVIDER,
    ResolveError,
    resolve_file,
    resolve_p2,
    wrapped_clips,
    wrapped_p2_clips,
)

ROOT = "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/2016/AH_X/CONTENTS"


def _clip(
    umid="U1",
    item_id="VX-1",
    output_file="/mnt/ActiveMedia/CANTEMO_FILES/W.MXF",
    provider="panasonicP2",
    collection_id=None,
):
    return Clip.objects.create(
        umid=umid,
        path="2016/AH_X",
        storage_id="VX-41",
        reference_file="F",
        item_id=item_id,
        provider_name=provider,
        output_file=output_file,
        collection_id=collection_id,
        status=Clip.STATUS_IMPORTED,
    )


def test_video_first_then_audio_in_name_order(migrated_db):
    clip = _clip()
    for name in ("00924E02.MXF", "00924E00.MXF", "00924E01.MXF"):
        ClipFile.objects.create(
            clip=clip, path=f"{ROOT}/AUDIO/{name}", filetype="audio"
        )
    ClipFile.objects.create(
        clip=clip, path=f"{ROOT}/VIDEO/00924E.MXF", filetype="video"
    )
    assert resolve_p2(clip) == [
        OriginalFile("2016/AH_X/CONTENTS/VIDEO/00924E.MXF", "video"),
        OriginalFile("2016/AH_X/CONTENTS/AUDIO/00924E00.MXF", "audio"),
        OriginalFile("2016/AH_X/CONTENTS/AUDIO/00924E01.MXF", "audio"),
        OriginalFile("2016/AH_X/CONTENTS/AUDIO/00924E02.MXF", "audio"),
    ]


def test_no_video_is_refused(migrated_db):
    clip = _clip()
    ClipFile.objects.create(clip=clip, path=f"{ROOT}/AUDIO/a.MXF", filetype="audio")
    with pytest.raises(ResolveError, match="1 video"):
        resolve_p2(clip)


def test_an_unknown_filetype_is_refused(migrated_db):
    clip = _clip()
    ClipFile.objects.create(clip=clip, path=f"{ROOT}/VIDEO/v.MXF", filetype="video")
    ClipFile.objects.create(clip=clip, path=f"{ROOT}/ICON/i.BMP", filetype="thumbnail")
    with pytest.raises(ResolveError, match="thumbnail"):
        resolve_p2(clip)


def test_a_path_outside_the_rushes_root_is_refused(migrated_db):
    clip = _clip()
    ClipFile.objects.create(clip=clip, path="/Volumes/Other/v.MXF", filetype="video")
    with pytest.raises(ResolveError, match="Other"):
        resolve_p2(clip)


def test_population_is_wrapped_p2_clips_with_an_item(migrated_db):
    _clip("A", "VX-1")
    _clip("B", "VX-2", output_file="")  # already migrated: output_file cleared
    _clip("C", "VX-3", provider="xdcam")
    _clip("D", None)
    _clip("E", "VX-5", collection_id="VX-COL")
    assert [c.umid for c in wrapped_p2_clips()] == ["A", "E"]
    assert [c.umid for c in wrapped_p2_clips(item_id="VX-5")] == ["E"]
    assert [c.umid for c in wrapped_p2_clips(collection_id="VX-COL")] == ["E"]


FILE_ROOT = "/mnt/ActiveMedia/AA - RUSHES TAPELESS/2019/AH_Y"


@pytest.mark.parametrize("filetype", ["video", "audio"])
def test_a_file_clip_has_its_one_clipfile_as_original(migrated_db, filetype):
    clip = _clip(provider=FILE_PROVIDER)
    ClipFile.objects.create(clip=clip, path=f"{FILE_ROOT}/A.MOV", filetype=filetype)
    assert resolve_file(clip) == OriginalFile("2019/AH_Y/A.MOV", filetype)


def test_a_file_clip_without_clipfile_has_no_known_original(migrated_db):
    assert resolve_file(_clip(provider=FILE_PROVIDER)) is None


def test_a_file_clip_with_two_clipfiles_is_refused(migrated_db):
    clip = _clip(provider=FILE_PROVIDER)
    for name in ("A.MOV", "B.MOV"):
        ClipFile.objects.create(clip=clip, path=f"{FILE_ROOT}/{name}", filetype="video")
    with pytest.raises(ResolveError, match="2 ClipFile"):
        resolve_file(clip)


def test_a_file_clip_outside_the_rushes_root_is_refused(migrated_db):
    clip = _clip(provider=FILE_PROVIDER)
    ClipFile.objects.create(clip=clip, path="/Volumes/Other/A.MOV", filetype="video")
    with pytest.raises(ResolveError, match="Other"):
        resolve_file(clip)


def test_a_file_clip_of_an_unknown_filetype_is_refused(migrated_db):
    clip = _clip(provider=FILE_PROVIDER)
    ClipFile.objects.create(clip=clip, path=f"{FILE_ROOT}/A.JPG", filetype="image")
    with pytest.raises(ResolveError, match="image"):
        resolve_file(clip)


def test_file_population_mirrors_the_p2_one(migrated_db):
    _clip("A", "VX-1", provider=FILE_PROVIDER)
    _clip("B", "VX-2", output_file="", provider=FILE_PROVIDER)
    _clip("C", "VX-3")
    _clip("D", None, provider=FILE_PROVIDER)
    _clip("E", "VX-5", collection_id="VX-COL", provider=FILE_PROVIDER)
    assert [c.umid for c in wrapped_clips(FILE_PROVIDER)] == ["A", "E"]
    assert [c.umid for c in wrapped_clips(FILE_PROVIDER, item_id="VX-5")] == ["E"]
    assert [c.umid for c in wrapped_clips("panasonicP2")] == ["C"]

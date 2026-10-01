"""Tier 2: where a spanned master's segment chain is read from."""

import pytest

from portal.plugins.TapelessIngest.models.clip import (
    Clip,
    ClipFile,
    ClipMetadata,
    SpannedClips,
)
from portal.plugins.TapelessIngest.wrapped.resolver import resolve_span
from portal.plugins.TapelessIngest.wrapped.span import SpanUnresolved
from tests.wrapped_fakes import FakeDisk, p2_originals, p2_segment_xml

LEGACY = "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/"
CONTENTS = "2015/AH_150108_EC225_SAR_COROGNE/CONTENTS"
TOP = "060A2B340101010501010D4313000000AAAA"
MID = "060A2B340101010501010D4313000000BBBB"
LAST = "060A2B340101010501010D4313000000CCCC"


def _clip(umid, name, duration="7482", item_id=None, audio_count=2, **metadata):
    clip = Clip.objects.create(
        umid=umid,
        path="2015/AH_150108_EC225_SAR_COROGNE",
        storage_id="VX-41",
        reference_file="F",
        item_id=item_id,
        provider_name="panasonicP2",
        spanned=item_id is not None,
        status=Clip.STATUS_IMPORTED,
    )
    for original in p2_originals(CONTENTS, name, audio_count):
        ClipFile.objects.create(
            clip=clip, path=LEGACY + original.relative, filetype=original.kind
        )
    fields = {"clipname": name, "duration": duration, "EditUnit": "1/25"}
    fields.update(metadata)
    for key, value in fields.items():
        if value is not None:
            ClipMetadata.objects.create(clip=clip, name=key, value=value)
    return clip


def _master(**metadata):
    fields = {
        "Relation_Top_GlobalClipID": TOP,
        "Relation_Next_ClipName": "003876",
        "Relation_Next_GlobalClipID": MID,
    }
    fields.update(metadata)
    return _clip(TOP, "0037OO", item_id="VX-1", **fields)


def _xml_disk():
    return FakeDisk(
        {
            f"{CONTENTS}/CLIP/003876.XML": p2_segment_xml(
                "003876",
                MID,
                offset=7482,
                previous=TOP,
                next_name="0039EX",
                next_id=LAST,
            ).encode(),
            f"{CONTENTS}/CLIP/0039EX.XML": p2_segment_xml(
                "0039EX", LAST, frames=1200, offset=14964, previous=MID
            ).encode(),
        }
    )


def test_xml_source_walks_the_master_contents_folder(migrated_db):
    chain = resolve_span(_master(), _xml_disk())
    assert [s.name for s in chain] == ["0037OO", "003876", "0039EX"]
    assert [s.frames for s in chain] == [7482, 7482, 1200]
    assert chain[0].video.relative == f"{CONTENTS}/VIDEO/0037OO.MXF"
    assert [a.relative for a in chain[0].audios] == [
        f"{CONTENTS}/AUDIO/0037OO00.MXF",
        f"{CONTENTS}/AUDIO/0037OO01.MXF",
    ]
    assert chain[2].video.relative == f"{CONTENTS}/VIDEO/0039EX.MXF"


def test_legacy_table_wins_over_the_xml(migrated_db):
    master = _master()
    SpannedClips.objects.create(master_clip=master, clip=master, order=1)
    second = _clip("ROW2", "00AAAA", duration="100", audio_count=4)
    third = _clip("ROW3", "00BBBB", duration="50")
    SpannedClips.objects.create(master_clip=master, clip=third, order=3)
    SpannedClips.objects.create(master_clip=master, clip=second, order=2)
    chain = resolve_span(master, _xml_disk())
    assert [s.name for s in chain] == ["0037OO", "00AAAA", "00BBBB"]
    assert [s.frames for s in chain] == [7482, 100, 50]
    assert len(chain[1].audios) == 4


def test_a_self_row_alone_is_not_a_chain(migrated_db):
    master = _master()
    SpannedClips.objects.create(master_clip=master, clip=master, order=1)
    assert len(resolve_span(master, _xml_disk())) == 3


@pytest.mark.parametrize("duration", [None, "", "lots"])
def test_master_without_a_usable_duration_is_unresolved(migrated_db, duration):
    with pytest.raises(SpanUnresolved, match="duration"):
        resolve_span(_master(duration=duration), _xml_disk())


def test_master_without_next_is_unresolved(migrated_db):
    master = _master(Relation_Next_ClipName=None, Relation_Next_GlobalClipID="")
    with pytest.raises(SpanUnresolved, match="master has no next segment"):
        resolve_span(master, _xml_disk())


def test_resolve_error_becomes_span_unresolved(migrated_db):
    master = _master()
    ClipFile.objects.filter(clip=master, filetype="video").delete()
    with pytest.raises(SpanUnresolved, match="video ClipFile"):
        resolve_span(master, _xml_disk())


def test_a_legacy_segment_without_metadata_is_unresolved(migrated_db):
    master = _master()
    second = _clip("ROW2", "00AAAA", clipname=None, duration=None, EditUnit=None)
    SpannedClips.objects.create(master_clip=master, clip=second, order=2)
    with pytest.raises(SpanUnresolved, match="ROW2"):
        resolve_span(master, _xml_disk())

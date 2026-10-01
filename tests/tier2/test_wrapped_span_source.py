"""Tier 2: where a spanned master's segment chain is read from."""

import pytest
from django.db import IntegrityError, transaction

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


def _legacy(master=None, second=None, third=None, orders=(2, 3)):
    """A legacy-table take whose rows prove their order: master (7482
    frames) -> MID (100) -> LAST (50); each dict overrides that clip's
    ClipMetadata (None drops a row)."""
    master = _master(**(master or {}))
    SpannedClips.objects.create(master_clip=master, clip=master, order=1)
    fields = {
        "duration": "100",
        "audio_count": 4,
        "Relation_Top_GlobalClipID": TOP,
        "Relation_Previous_GlobalClipID": TOP,
        "Relation_Next_GlobalClipID": LAST,
        "Relation_OffsetInShot": "7482",
    }
    fields.update(second or {})
    middle = _clip(MID, "00AAAA", **fields)
    fields = {
        "duration": "50",
        "Relation_Top_GlobalClipID": TOP,
        "Relation_Previous_GlobalClipID": MID,
        "Relation_OffsetInShot": "7582",
    }
    fields.update(third or {})
    last = _clip(LAST, "00BBBB", **fields)
    # Created out of order: the table's order column decides.
    SpannedClips.objects.create(master_clip=master, clip=last, order=orders[1])
    SpannedClips.objects.create(master_clip=master, clip=middle, order=orders[0])
    return master


def test_legacy_table_wins_over_the_xml(migrated_db):
    chain = resolve_span(_legacy(), _xml_disk())
    assert [s.name for s in chain] == ["0037OO", "00AAAA", "00BBBB"]
    assert [s.frames for s in chain] == [7482, 100, 50]
    assert len(chain[1].audios) == 4


def test_legacy_chain_without_offsets_or_master_top_is_proven_by_ids(migrated_db):
    master = _legacy(
        master={"Relation_Top_GlobalClipID": None},
        second={"Relation_OffsetInShot": None},
        third={"Relation_OffsetInShot": None},
    )
    assert len(resolve_span(master, _xml_disk())) == 3


@pytest.mark.parametrize(
    "orders, message",
    [
        ((2, 4), r"legacy table orders \[2, 4\], expected \[2, 3\]"),
        ((3, 4), r"legacy table orders \[3, 4\], expected \[2, 3\]"),
        ((0, 2), r"legacy table orders \[0, 2\], expected \[2, 3\]"),
    ],
)
def test_legacy_orders_must_be_two_to_n(migrated_db, orders, message):
    with pytest.raises(SpanUnresolved, match=message):
        resolve_span(_legacy(orders=orders), _xml_disk())


@pytest.mark.parametrize(
    "overrides, message",
    [
        (
            {"master": {"Relation_Next_GlobalClipID": LAST}},
            f"segment 00AAAA \\(row 2\\): previous Next {LAST}, not {MID}",
        ),
        (
            {"second": {"Relation_Previous_GlobalClipID": "OTHER"}},
            f"segment 00AAAA \\(row 2\\): Previous OTHER, not {TOP}",
        ),
        (
            {"second": {"Relation_Previous_GlobalClipID": None}},
            f"segment 00AAAA \\(row 2\\): Previous None, not {TOP}",
        ),
        (
            {"third": {"Relation_Top_GlobalClipID": MID}},
            f"segment 00BBBB \\(row 3\\): Top {MID}, not {TOP}",
        ),
        (
            {"third": {"Relation_OffsetInShot": "7482"}},
            "segment 00BBBB \\(row 3\\): OffsetInShot 7482, not 7582",
        ),
        (
            {"third": {"Relation_OffsetInShot": "later"}},
            "segment 00BBBB \\(row 3\\): OffsetInShot 'later' is not a frame count",
        ),
        (
            {"second": {"Relation_Next_GlobalClipID": "OTHER"}},
            f"segment 00BBBB \\(row 3\\): previous Next OTHER, not {LAST}",
        ),
        (
            {"third": {"Relation_Next_GlobalClipID": "BEYOND"}},
            "segment 00BBBB \\(row 3\\): Next BEYOND, but it is the last segment",
        ),
    ],
)
def test_legacy_rows_must_prove_their_chain(migrated_db, overrides, message):
    with pytest.raises(SpanUnresolved, match=message):
        resolve_span(_legacy(**overrides), _xml_disk())


def test_duplicate_clip_metadata_cannot_exist(migrated_db):
    """Why the proof reads ClipMetadata through a dict without a duplicate
    check: (clip, name) is unique since migration 0001, so a second value
    for a key the proof reads can never be stored."""
    master = _master()
    with pytest.raises(IntegrityError), transaction.atomic():
        ClipMetadata.objects.create(
            clip=master, name="Relation_Top_GlobalClipID", value=TOP
        )


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


def test_xml_source_refuses_a_middle_segment(migrated_db):
    middle = _clip(
        MID,
        "003876",
        item_id="VX-2",
        Relation_Top_GlobalClipID=TOP,
        Relation_Previous_GlobalClipID=TOP,
        Relation_Next_ClipName="0039EX",
        Relation_Next_GlobalClipID=LAST,
    )
    with pytest.raises(SpanUnresolved, match="not the head of its take"):
        resolve_span(middle, _xml_disk())


def test_legacy_source_refuses_a_middle_segment(migrated_db):
    middle = _clip(MID, "003876", item_id="VX-2", Relation_Top_GlobalClipID=TOP)
    third = _clip("ROW3", "0039EX", duration="1200")
    SpannedClips.objects.create(master_clip=middle, clip=third, order=2)
    with pytest.raises(SpanUnresolved, match=f"not the head of its take.*{TOP}"):
        resolve_span(middle, _xml_disk())


def test_legacy_source_refuses_a_master_with_a_previous(migrated_db):
    master = _master(Relation_Previous_GlobalClipID="SOMEONE")
    second = _clip("ROW2", "003876")
    SpannedClips.objects.create(master_clip=master, clip=second, order=2)
    with pytest.raises(SpanUnresolved, match="Previous SOMEONE"):
        resolve_span(master, _xml_disk())

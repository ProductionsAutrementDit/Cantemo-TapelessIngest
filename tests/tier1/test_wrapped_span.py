"""Tier 1: the ordered segment chain of a spanned P2 take (pure logic)."""

from fractions import Fraction

import pytest

from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile
from portal.plugins.TapelessIngest.wrapped.span import (
    MasterIds,
    Segment,
    SpanUnresolved,
    chain_from_rows,
    chain_from_xml,
    parse_clip_xml,
)
from tests.wrapped_fakes import P2_V31 as V31, p2_segment_xml as clip_xml

V30 = "urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.0"
CONTENTS = "2015/AH_150108_EC225_SAR_COROGNE/CONTENTS"
TOP = "060A2B340101010501010D4313000000AAAA"  # also the default Top
MID = "060A2B340101010501010D4313000000BBBB"
LAST = "060A2B340101010501010D4313000000CCCC"


def files(name, audio_count=2, contents=CONTENTS):
    return OriginalFile(f"{contents}/VIDEO/{name}.MXF", "video"), tuple(
        OriginalFile(f"{contents}/AUDIO/{name}{n:02d}.MXF", "audio")
        for n in range(audio_count)
    )


def segment(name, frames=7482, edit_unit=(1, 25), audio_count=2):
    video, audios = files(name, audio_count)
    return Segment(name, video, audios, frames, edit_unit)


MASTER = segment("0037OO")
IDS = MasterIds(global_id=TOP, top_id=TOP, next_name="003876", next_id=MID)


def world(**overrides):
    """The three measured segments, keyed as ``read_xml`` is asked."""
    documents = {
        "003876": dict(
            name="003876",
            global_id=MID,
            offset=7482,
            previous=TOP,
            next_name="0039EX",
            next_id=LAST,
        ),
        "0039EX": dict(
            name="0039EX", global_id=LAST, frames=1200, offset=14964, previous=MID
        ),
    }
    for name, changes in overrides.items():
        if changes is None:
            documents.pop(name)
        else:
            documents[name].update(changes)
    return {
        f"{CONTENTS}/CLIP/{name}.XML": clip_xml(**fields)
        for name, fields in documents.items()
    }


def walk(documents, master=MASTER, ids=IDS):
    return chain_from_xml(master, ids, CONTENTS, documents.get)


def refusal(documents, master=MASTER, ids=IDS):
    with pytest.raises(SpanUnresolved) as raised:
        walk(documents, master, ids)
    return str(raised.value)


# parse_clip_xml ------------------------------------------------------------


def test_parse_reads_a_v31_document():
    parsed = parse_clip_xml(
        clip_xml(
            "003876",
            MID,
            offset=7482,
            previous=TOP,
            next_name="0039EX",
            next_id=LAST,
            audio_count=4,
        )
    )
    assert parsed.name == "003876"
    assert parsed.global_id == MID
    assert parsed.frames == 7482
    assert parsed.edit_unit == (1, 25)
    assert parsed.offset == 7482
    assert parsed.top_id == TOP
    assert parsed.previous_id == TOP
    assert parsed.next_name == "0039EX"
    assert parsed.next_id == LAST
    assert parsed.audio_count == 4


def test_parse_reads_another_namespace_version():
    parsed = parse_clip_xml(clip_xml("0039EX", LAST, namespace=V30, previous=MID))
    assert (parsed.name, parsed.global_id, parsed.previous_id) == (
        "0039EX",
        LAST,
        MID,
    )
    assert parsed.offset is None
    assert (parsed.next_name, parsed.next_id) == (None, None)


def test_parse_reads_a_document_without_namespace():
    text = clip_xml("0039EX", LAST).replace(f' xmlns="{V31}"', "")
    assert parse_clip_xml(text).global_id == LAST


@pytest.mark.parametrize("tag", ["ClipName", "GlobalClipID", "Duration", "EditUnit"])
def test_parse_refuses_a_missing_mandatory_element(tag):
    with pytest.raises(SpanUnresolved, match=tag):
        parse_clip_xml(clip_xml("0039EX", LAST, drop=(tag,)))


@pytest.mark.parametrize(
    "changes", [{"frames": "lots"}, {"edit_unit": "25"}, {"edit_unit": "1/0"}]
)
def test_parse_refuses_unreadable_values(changes):
    with pytest.raises(SpanUnresolved):
        parse_clip_xml(clip_xml("0039EX", LAST, **changes))


def test_parse_refuses_malformed_xml():
    with pytest.raises(SpanUnresolved, match="XML"):
        parse_clip_xml("<P2Main><ClipContent>")


# Segment -------------------------------------------------------------------


def test_segment_seconds_is_exact():
    assert segment("0039EX", frames=1200).seconds == Fraction(48)
    assert segment("X", frames=1001, edit_unit=(1001, 30000)).seconds == Fraction(
        1001 * 1001, 30000
    )


# chain_from_xml ------------------------------------------------------------


def test_xml_chain_walks_three_segments_in_order():
    chain = walk(world())
    assert [s.name for s in chain] == ["0037OO", "003876", "0039EX"]
    assert chain[0] is MASTER
    assert [s.frames for s in chain] == [7482, 7482, 1200]
    assert all(s.edit_unit == (1, 25) for s in chain)
    video, audios = files("003876")
    assert (chain[1].video, chain[1].audios) == (video, audios)
    assert chain[2].video == OriginalFile(f"{CONTENTS}/VIDEO/0039EX.MXF", "video")
    assert chain[2].audios[-1] == OriginalFile(
        f"{CONTENTS}/AUDIO/0039EX01.MXF", "audio"
    )


def test_xml_chain_takes_each_segments_own_audio_count():
    chain = walk(world(**{"0039EX": {"audio_count": 4}}))
    assert len(chain[2].audios) == 4
    assert chain[2].audios[3].relative == f"{CONTENTS}/AUDIO/0039EX03.MXF"


def test_xml_chain_accepts_a_missing_offset():
    chain = walk(world(**{"0039EX": {"offset": None}}))
    assert len(chain) == 3


def test_master_without_next_is_refused():
    ids = MasterIds(global_id=TOP, top_id=TOP, next_name=None, next_id=None)
    assert refusal(world(), ids=ids) == "master has no next segment"


def test_master_with_a_next_name_but_no_next_id_is_refused():
    ids = MasterIds(global_id=TOP, top_id=TOP, next_name="003876", next_id=None)
    assert "003876" in refusal(world(), ids=ids)


def test_missing_xml_is_refused_naming_segment_and_hop():
    message = refusal(world(**{"0039EX": None}))
    assert "0039EX" in message and "hop 2" in message and "missing" in message


def test_unexpected_global_id_is_refused():
    message = refusal(world(**{"0039EX": {"global_id": "SOMEONE_ELSE"}}))
    assert "0039EX" in message and "hop 2" in message and "GlobalClipID" in message


def test_wrong_previous_is_refused():
    message = refusal(world(**{"0039EX": {"previous": TOP}}))
    assert "0039EX" in message and "hop 2" in message and "Previous" in message


def test_wrong_top_is_refused():
    message = refusal(world(**{"003876": {"top": "OTHER_TOP"}}))
    assert "003876" in message and "hop 1" in message and "Top" in message


def test_wrong_offset_is_refused():
    message = refusal(world(**{"0039EX": {"offset": 7482}}))
    assert "0039EX" in message and "hop 2" in message and "OffsetInShot" in message


def test_different_edit_unit_is_refused():
    message = refusal(world(**{"003876": {"edit_unit": "1001/30000"}}))
    assert "003876" in message and "hop 1" in message and "EditUnit" in message


def test_a_repeated_segment_name_is_refused():
    loop = world(**{"0039EX": {"next_name": "003876", "next_id": MID}})
    message = refusal(loop)
    assert "003876" in message and "hop 3" in message and "repeat" in message


def test_a_chain_longer_than_64_segments_is_refused():
    documents, previous = {}, TOP
    for n in range(1, 65):
        name, gid = f"S{n:04d}", f"ID{n}"
        documents[f"{CONTENTS}/CLIP/{name}.XML"] = clip_xml(
            name,
            gid,
            frames=10,
            previous=previous,
            next_name=f"S{n + 1:04d}",
            next_id=f"ID{n + 1}",
        )
        previous = gid
    master = segment("0037OO", frames=10)
    ids = MasterIds(global_id=TOP, top_id=TOP, next_name="S0001", next_id="ID1")
    message = refusal(documents, master=master, ids=ids)
    assert message == "segment S0064 (hop 64): more than 64 segments"


def test_master_top_defaults_to_its_own_id():
    ids = MasterIds(global_id=TOP, top_id=None, next_name="003876", next_id=MID)
    assert len(walk(world(), ids=ids)) == 3


def test_a_middle_segment_is_not_the_head_of_its_take():
    ids = MasterIds(global_id=MID, top_id=TOP, next_name="0039EX", next_id=LAST)
    message = refusal(world(), ids=ids)
    assert "not the head of its take" in message
    assert TOP in message and MID in message


def test_a_master_with_a_previous_segment_is_not_the_head():
    ids = MasterIds(
        global_id=TOP, top_id=TOP, next_name="003876", next_id=MID, previous_id="P"
    )
    message = refusal(world(), ids=ids)
    assert "not the head of its take" in message and "Previous P" in message


def test_a_segment_xml_naming_another_clip_is_refused():
    message = refusal(world(**{"003876": {"name": "00XXXX"}}))
    assert message == "segment 003876 (hop 1): ClipName 00XXXX, not 003876"


def test_a_segment_xml_without_previous_is_refused():
    message = refusal(world(**{"0039EX": {"previous": None}}))
    assert message == f"segment 0039EX (hop 2): Previous None, not {MID}"


def test_a_segment_xml_without_top_is_refused():
    message = refusal(world(**{"0039EX": {"top": None}}))
    assert message == f"segment 0039EX (hop 2): Top None, not {TOP}"


def test_a_half_present_next_inside_a_segment_xml_is_refused():
    message = refusal(world(**{"0039EX": {"next_name": "0040AA"}}))
    assert message == ("segment 0040AA (hop 3): next ClipName or GlobalClipID missing")


@pytest.mark.parametrize("bad", ["../0039EX", "CLIP/0039EX", ".."])
def test_a_next_name_that_is_not_a_plain_name_is_refused(bad):
    asked = []

    def read_xml(relative):
        asked.append(relative)
        return world().get(relative)

    ids = MasterIds(global_id=TOP, top_id=TOP, next_name=bad, next_id=MID)
    with pytest.raises(SpanUnresolved, match="not a plain clip name"):
        chain_from_xml(MASTER, ids, CONTENTS, read_xml)
    assert asked == []


# chain_from_rows -----------------------------------------------------------


def test_rows_chain_puts_the_master_first():
    rows = [segment("003876"), segment("0039EX", frames=1200)]
    assert chain_from_rows(MASTER, rows) == [MASTER] + rows


def test_rows_chain_refuses_no_rows():
    with pytest.raises(SpanUnresolved, match="no segment"):
        chain_from_rows(MASTER, [])


def test_rows_chain_refuses_a_repeated_name():
    with pytest.raises(SpanUnresolved, match="003876.*repeat"):
        chain_from_rows(MASTER, [segment("003876"), segment("003876")])


def test_rows_chain_refuses_the_master_listed_again():
    with pytest.raises(SpanUnresolved, match="0037OO.*repeat"):
        chain_from_rows(MASTER, [segment("0037OO")])


def test_rows_chain_refuses_different_edit_units():
    with pytest.raises(SpanUnresolved, match="0039EX.*EditUnit"):
        chain_from_rows(
            MASTER, [segment("003876"), segment("0039EX", edit_unit=(1001, 30000))]
        )

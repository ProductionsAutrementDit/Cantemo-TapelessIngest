"""Tier 1: Vidispine shape JSON <-> the migration's value objects."""

import pytest

from portal.plugins.TapelessIngest.wrapped.dryrun import RecordingGateway
from portal.plugins.TapelessIngest.wrapped.gateway import Gateway, parse_shape
from tests.wrapped_fakes import InMemoryGateway, seed_item, wrapped_p2_document


def test_a_wrapped_p2_shape_parses_into_its_components():
    shape = parse_shape(wrapped_p2_document(audio_count=4))
    assert shape.shape_id == "VX-SW"
    assert shape.tags == ("original",)
    assert [len(shape.of_kind(k)) for k in ("container", "video", "audio")] == [
        1,
        1,
        4,
    ]
    assert shape.file_ids() == frozenset({"VX-W1"})
    (only,) = shape.files().values()
    assert (only.storage_id, only.state, only.path) == (
        "VX-2",
        "ARCHIVED",
        "060A2B34.MXF",
    )


def test_to_document_round_trips():
    document = wrapped_p2_document(audio_count=2)
    assert parse_shape(parse_shape(document).to_document()) == parse_shape(document)


def test_a_file_reference_without_details_parses():
    shape = parse_shape({"id": "S", "containerComponent": {"file": [{"id": "F"}]}})
    (file,) = shape.files().values()
    assert (file.file_id, file.storage_id, file.path, file.state) == ("F", "", "", "")


def test_the_fake_gateway_posts_and_untags_like_vidispine():
    gateway = InMemoryGateway()
    seed_item(gateway, "VX-1", wrapped_p2_document())
    gateway.files[("VX-41", "2016/X/V.MXF")] = "VX-F9"
    new_id = gateway.post_shape(
        "VX-1", {"containerComponent": {"file": [{"id": "VX-F9"}]}}
    )
    assert [s.shape_id for s in gateway.original_shapes("VX-1")] == ["VX-SW", new_id]
    gateway.untag_shape("VX-1", "VX-SW", "original")
    assert [s.shape_id for s in gateway.original_shapes("VX-1")] == [new_id]
    # the shape stays on the item, just without the "original" tag
    (untagged,) = [d for d in gateway.shapes["VX-1"] if d["id"] == "VX-SW"]
    assert "original" not in untagged["tag"]
    (posted,) = gateway.original_shapes("VX-1")
    (file,) = posted.files().values()
    assert (file.file_id, file.path) == ("VX-F9", "2016/X/V.MXF")


def test_the_recording_gateway_defines_every_gateway_method_itself():
    # No attribute fallback: a write added to the Protocol but not to the
    # dry run would otherwise reach the real Vidispine.
    wanted = {
        name
        for name, member in vars(Gateway).items()
        if callable(member) and not name.startswith("_")
    }
    assert wanted  # the Protocol really was inspected
    assert wanted - set(vars(RecordingGateway)) == set()
    assert "__getattr__" not in vars(RecordingGateway)


def _relocatable(gateway):
    """VX-1 with one original shape whose two components name VX-F1."""
    gateway.files[("VX-41", "2014/OLD/V.MXF")] = "VX-OLD1"
    gateway.file_states[("VX-41", "VX-OLD1")] = "ARCHIVED"
    file = {"id": "VX-OLD1", "storage": "VX-41", "path": "2014/OLD/V.MXF"}
    gateway.shapes["VX-1"] = [
        {
            "id": "VX-S1",
            "tag": ["original"],
            "containerComponent": {"id": "C", "file": [dict(file, state="CLOSED")]},
            "videoComponent": [{"id": "V", "file": [dict(file, state="CLOSED")]}],
        }
    ]
    gateway.component_md[("VX-1", "VX-S1", "V")] = {"handle": "H#1"}


def test_the_fake_gateway_relocates_like_vidispine():
    # Measured on VX-10456: a NEW entity at the new path, state OPEN, the
    # old one gone, every component re-pointed, component metadata kept.
    gateway = InMemoryGateway()
    _relocatable(gateway)
    gateway.relocate_file("VX-41", "VX-OLD1", "2014/AH_NEW/V.MXF")
    assert gateway.find_file("VX-41", "2014/OLD/V.MXF") is None
    moved = gateway.find_file("VX-41", "2014/AH_NEW/V.MXF")
    assert moved.file_id != "VX-OLD1" and moved.state == "OPEN"
    assert gateway.file_state("VX-41", "VX-OLD1") is None
    (shape,) = gateway.original_shapes("VX-1")
    assert shape.file_ids() == frozenset({moved.file_id})
    assert {f.path for c in shape.components for f in c.files} == {"2014/AH_NEW/V.MXF"}
    assert gateway.component_metadata("VX-1", "VX-S1", "V") == {"handle": "H#1"}
    gateway.set_file_state("VX-41", moved.file_id, "ARCHIVED")
    assert gateway.file_state("VX-41", moved.file_id) == "ARCHIVED"
    assert gateway.write_names() == ["relocate_file", "set_file_state"]


def test_the_recording_gateway_records_a_relocation_and_reads_coherently():
    inner = InMemoryGateway()
    _relocatable(inner)
    recording = RecordingGateway(inner)
    recording.relocate_file("VX-41", "VX-OLD1", "2014/AH_NEW/V.MXF")
    assert recording.find_file("VX-41", "2014/OLD/V.MXF") is None
    moved = recording.find_file("VX-41", "2014/AH_NEW/V.MXF")
    assert moved.file_id.startswith("DRYRUN-") and moved.state == "OPEN"
    assert recording.file_state("VX-41", "VX-OLD1") is None
    recording.set_file_state("VX-41", moved.file_id, "ARCHIVED")
    assert recording.file_state("VX-41", moved.file_id) == "ARCHIVED"
    assert recording.find_file("VX-41", "2014/AH_NEW/V.MXF").state == "ARCHIVED"
    assert [w[0] for w in recording.writes] == ["relocate_file", "set_file_state"]
    # nothing reached the real gateway
    assert inner.writes == []
    assert inner.find_file("VX-41", "2014/OLD/V.MXF").file_id == "VX-OLD1"


def test_the_fake_gateway_refuses_to_relocate_onto_a_taken_path():
    gateway = InMemoryGateway()
    _relocatable(gateway)
    gateway.files[("VX-41", "2014/AH_NEW/V.MXF")] = "VX-TAKEN"
    with pytest.raises(ValueError, match="VX-TAKEN"):
        gateway.relocate_file("VX-41", "VX-OLD1", "2014/AH_NEW/V.MXF")
    assert gateway.find_file("VX-41", "2014/OLD/V.MXF").file_id == "VX-OLD1"


def test_the_recording_gateway_repoints_the_shapes_it_relocated():
    inner = InMemoryGateway()
    _relocatable(inner)
    recording = RecordingGateway(inner)
    recording.relocate_file("VX-41", "VX-OLD1", "2014/AH_NEW/V.MXF")
    moved = recording.find_file("VX-41", "2014/AH_NEW/V.MXF")
    (shape,) = recording.original_shapes("VX-1")
    assert shape.file_ids() == frozenset({moved.file_id})
    (inner_shape,) = inner.original_shapes("VX-1")
    assert inner_shape.file_ids() == frozenset({"VX-OLD1"})


def test_the_recording_gateway_forgets_a_file_it_deleted():
    inner = InMemoryGateway()
    _relocatable(inner)
    recording = RecordingGateway(inner)
    recording.delete_file("VX-41", "VX-OLD1")
    assert recording.find_file("VX-41", "2014/OLD/V.MXF") is None
    assert recording.file_state("VX-41", "VX-OLD1") is None

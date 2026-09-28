"""Tier 1: Vidispine shape JSON <-> the migration's value objects."""

from portal.plugins.TapelessIngest.wrapped.gateway import parse_shape
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


def test_the_fake_gateway_posts_and_retags_like_vidispine():
    gateway = InMemoryGateway()
    seed_item(gateway, "VX-1", wrapped_p2_document())
    gateway.files[("VX-41", "2016/X/V.MXF")] = "VX-F9"
    new_id = gateway.post_shape(
        "VX-1", {"containerComponent": {"file": [{"id": "VX-F9"}]}}
    )
    assert [s.shape_id for s in gateway.original_shapes("VX-1")] == ["VX-SW", new_id]
    gateway.retag_shape("VX-1", "VX-SW", add="legacy-wrapped", remove="original")
    assert [s.shape_id for s in gateway.original_shapes("VX-1")] == [new_id]
    assert gateway.shape_ids("VX-1", "legacy-wrapped") == ["VX-SW"]
    (posted,) = gateway.original_shapes("VX-1")
    (file,) = posted.files().values()
    assert (file.file_id, file.path) == ("VX-F9", "2016/X/V.MXF")

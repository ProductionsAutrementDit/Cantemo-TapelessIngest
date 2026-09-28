"""Tier 1: VidispineGateway against the Portal stub — URLs and calls."""

from tests.portal_stub import RestTransportFake, StorageHelperFake, VidispineFake

from portal.plugins.TapelessIngest.wrapped.vidispine import VidispineGateway
from tests.wrapped_fakes import wrapped_p2_document


def _calls(name):
    return [details for call, details in VidispineFake.calls if call == name]


def test_original_shapes_lists_by_tag_then_reads_each_shape():
    RestTransportFake.route(
        "GET",
        r".*/item/VX-1/shape",
        lambda m, q: (
            {"uri": ["VX-SW"]} if q.get("tag") == ["original"] else {"uri": []}
        ),
    )
    RestTransportFake.route(
        "GET", r".*/item/VX-1/shape/VX-SW", lambda m, q: wrapped_p2_document()
    )
    (shape,) = VidispineGateway().original_shapes("VX-1")
    assert shape.shape_id == "VX-SW"
    assert len(shape.of_kind("audio")) == 4


def test_component_metadata_reads_the_key_value_document():
    RestTransportFake.route(
        "GET",
        r".*/item/VX-1/shape/S/component/C/metadata",
        lambda m, q: {"field": [{"key": "portal_sha1", "value": "abc"}]},
    )
    assert VidispineGateway().component_metadata("VX-1", "S", "C") == {
        "portal_sha1": "abc"
    }


def test_item_fields_walk_the_timespans():
    RestTransportFake.route(
        "GET",
        r".*/item/VX-1/metadata",
        lambda m, q: {
            "item": [
                {
                    "metadata": {
                        "timespan": [
                            {
                                "field": [
                                    {
                                        "name": "durationSeconds",
                                        "value": [{"value": "8.72"}],
                                    }
                                ]
                            }
                        ]
                    }
                }
            ]
        },
    )
    assert VidispineGateway().item_fields("VX-1", ["durationSeconds"]) == {
        "durationSeconds": ["8.72"]
    }


def test_find_file_answers_none_when_vidispine_does_not_know_the_path():
    StorageHelperFake.set_file("VX-41", "2016/X/V.MXF", "VX-9")
    StorageHelperFake.set_file_state("VX-9", "LOST")
    gateway = VidispineGateway()
    found = gateway.find_file("VX-41", "2016/X/V.MXF")
    assert (found.file_id, found.state) == ("VX-9", "LOST")
    assert gateway.find_file("VX-41", "2016/X/W.MXF") is None


def test_register_file_archived_creates_an_archived_entity():
    file_id = VidispineGateway().register_file("VX-41", "2016/X/V.MXF", archived=True)
    (created,) = _calls("createFileEntity")
    assert (created["state"], created["create_only"], created["file_id"]) == (
        "ARCHIVED",
        True,
        file_id,
    )


def test_register_file_on_disk_notifies_the_storage():
    VidispineGateway().register_file("VX-41", "2016/X/V.MXF", archived=False)
    (notified,) = _calls("notifyStorageOfFile")
    assert (notified["path"], notified["state"]) == ("2016/X/V.MXF", "CLOSED")
    assert notified["state_passed"] is True


def test_post_shape_uses_shape_create_with_updateItemMetadata():
    VidispineFake.set_item("VX-1")
    shape_id = VidispineGateway().post_shape(
        "VX-1", {"containerComponent": {"file": [{"id": "VX-9"}]}}
    )
    (posted,) = _calls("createShapeFromDocument")
    assert (posted["tag"], posted["update_item_metadata"]) == ("original", "true")
    assert shape_id == "VX-1-POSTED-1"


def test_metadata_writes_go_one_key_at_a_time():
    gateway = VidispineGateway()
    gateway.set_component_metadata("VX-1", "S", "C", {"a": "1", "b": "2"})
    gateway.set_item_metadata("VX-1", {"af_p5_barcodes": "AH012AL7"})
    assert [(c["key"], c["value"]) for c in _calls("setComponentMetadata")] == [
        ("a", "1"),
        ("b", "2"),
    ]
    (item,) = _calls("update_or_create_item_metadata")
    assert (item["field_name"], item["value"]) == ("af_p5_barcodes", "AH012AL7")


def test_retag_adds_the_legacy_tag_before_removing_original():
    RestTransportFake.route(
        "PUT", r".*/item/VX-1/shape/S/tag/legacy-wrapped", lambda m, q: None
    )
    RestTransportFake.route(
        "DELETE", r".*/item/VX-1/shape/S/tag/original", lambda m, q: None
    )
    VidispineGateway().retag_shape("VX-1", "S", add="legacy-wrapped", remove="original")
    assert [(c["method"], c["path"].rsplit("/", 1)[-1]) for c in _calls("rest")] == [
        ("PUT", "legacy-wrapped"),
        ("DELETE", "original"),
    ]


def test_delete_file_removes_it_from_its_storage():
    VidispineGateway().delete_file("VX-26", "VX-W1")
    (removed,) = _calls("removeFileFromStorage")
    assert (removed["storage_id"], removed["file_id"]) == ("VX-26", "VX-W1")


def test_file_state_reads_the_live_state():
    StorageHelperFake.set_file_state("VX-W1", "CLOSED")
    assert VidispineGateway().file_state("VX-26", "VX-W1") == "CLOSED"


def test_file_state_is_none_for_a_file_that_is_gone():
    assert VidispineGateway().file_state("VX-26", "VX-UNKNOWN") is None

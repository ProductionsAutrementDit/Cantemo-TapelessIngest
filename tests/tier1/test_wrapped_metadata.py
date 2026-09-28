"""Tier 1: the archive metadata each component and the item receive."""

from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.metadata import (
    archive_ts,
    component_fields,
    item_fields,
)


def _original(name, on_disk=False, handle="AirbusHelicopters#H", volumes=("10509",)):
    entry = (
        {"handle": handle, "volumes": list(volumes), "btime": 1506011266, "size": 1}
        if handle
        else None
    )
    return {
        "relative": f"2016/X/CONTENTS/VIDEO/{name}",
        "kind": "video",
        "file_id": "VX-F1",
        "on_disk": on_disk,
        "entry": entry,
        "tapes": (
            [{"volume_id": v, "barcode": f"BC{v}", "label": f"L.{v}"} for v in volumes]
            if handle
            else []
        ),
    }


def test_a_tape_only_component_carries_its_own_handle_and_no_sha1():
    assert component_fields(_original("00924E.MXF"), sha1=None) == {
        fields.ORIGINAL_FILENAME_FIELD: "00924E.MXF",
        fields.EXTERNAL_ID_FIELD: "AirbusHelicopters#H",
        fields.ARCHIVE_STATUS_FIELD: "ARCHIVED",
        fields.ARCHIVE_PLUGIN_FIELD: "c4c1d403-801b-4b1a-95a1-6a692f64c262",
        fields.ARCHIVE_POLICY_FIELD: "aw-10007",
        fields.ARCHIVE_TS_FIELD: "2017-09-21T16:27:46",
    }


def test_an_on_disk_unarchived_component_gets_its_name_and_sha1_only():
    original = _original("00924E.MXF", on_disk=True, handle=None)
    assert component_fields(original, sha1="abc") == {
        fields.ORIGINAL_FILENAME_FIELD: "00924E.MXF",
        fields.SHA1_FIELD: "abc",
    }


def test_item_fields_concatenate_distinct_values_in_order():
    originals = [
        _original("V.MXF", handle="H#V", volumes=("10509", "10516")),
        _original("A0.MXF", handle="H#A0", volumes=("10509", "10516")),
    ]
    assert item_fields(originals) == {
        fields.EXTERNAL_IDS_FIELD: "H#V, H#A0",
        fields.BARCODES_FIELD: "BC10509, BC10516",
        fields.TAPE_LABELS_FIELD: "L.10509, L.10516",
        fields.TAPE_NAMES_FIELD: "10509, 10516",
        fields.ARCHIVE_STATUS_FIELD: "Archived",
        fields.ARCHIVE_PLUGIN_FIELD: "c4c1d403-801b-4b1a-95a1-6a692f64c262",
        fields.ARCHIVE_POLICY_FIELD: "aw-10007",
        fields.ARCHIVE_TS_FIELD: "2017-09-21T16:27:46",
    }


def test_item_status_is_restored_when_every_original_is_on_disk_and_archived():
    originals = [_original("V.MXF", on_disk=True), _original("A.MXF", on_disk=True)]
    assert item_fields(originals)[fields.ARCHIVE_STATUS_FIELD] == "Archived/Restored"


def test_item_status_is_none_when_an_online_original_is_not_archived():
    originals = [
        _original("V.MXF", on_disk=True),
        _original("A.MXF", on_disk=True, handle=None),
    ]
    assert item_fields(originals)[fields.ARCHIVE_STATUS_FIELD] == ""


def test_nothing_archived_clears_the_item_archive_fields():
    # The wrapped file's Default-Archive values must not stay on the item.
    assert item_fields([_original("V.MXF", on_disk=True, handle=None)]) == {
        fields.EXTERNAL_IDS_FIELD: "",
        fields.BARCODES_FIELD: "",
        fields.TAPE_LABELS_FIELD: "",
        fields.TAPE_NAMES_FIELD: "",
        fields.ARCHIVE_STATUS_FIELD: fields.STATUS_NONE,
        fields.ARCHIVE_TS_FIELD: "",
        fields.ARCHIVE_PLUGIN_FIELD: "",
        fields.ARCHIVE_POLICY_FIELD: "",
    }


def test_archive_ts_is_utc():
    assert archive_ts(0) == "1970-01-01T00:00:00"

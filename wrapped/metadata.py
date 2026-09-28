"""Archive metadata for the new components and for the item.

Rules (spec, "Component metadata rules"): each component describes ITS
OWN file; technical analysis fields are never written here; a tape-only
original gets no sha1. The item-level values summarise the components.
"""

import posixpath
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from portal.plugins.TapelessIngest.wrapped import fields


def archive_ts(btime: int) -> str:
    moment = datetime.fromtimestamp(int(btime), tz=timezone.utc)
    return moment.replace(tzinfo=None).isoformat()


def _distinct(values: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def _policy_fields() -> Dict[str, str]:
    return {
        fields.ARCHIVE_PLUGIN_FIELD: fields.ARCHIWARE_PLUGIN_GUID,
        fields.ARCHIVE_POLICY_FIELD: fields.AIRBUS_POLICY_UUID,
    }


def component_fields(original: Mapping, sha1: Optional[str]) -> Dict[str, str]:
    result = {fields.ORIGINAL_FILENAME_FIELD: posixpath.basename(original["relative"])}
    entry = original.get("entry")
    if entry:
        result[fields.EXTERNAL_ID_FIELD] = entry["handle"]
        result[fields.ARCHIVE_STATUS_FIELD] = fields.COMPONENT_ARCHIVED
        result.update(_policy_fields())
        result[fields.ARCHIVE_TS_FIELD] = archive_ts(entry["btime"])
    if sha1:
        result[fields.SHA1_FIELD] = sha1
    return result


def _item_status(originals: Sequence[Mapping]) -> str:
    if not all(o["on_disk"] for o in originals):
        return fields.STATUS_ARCHIVED_OFFLINE
    if all(o.get("entry") for o in originals):
        return fields.STATUS_ARCHIVED_ONLINE
    return fields.STATUS_NONE


def item_fields(originals: Sequence[Mapping]) -> Dict[str, str]:
    archived = [o for o in originals if o.get("entry")]
    if not archived:
        # Spec rule 4: cleared, so the wrapped file's Default-Archive values
        # are not left on the item.
        return {
            fields.EXTERNAL_IDS_FIELD: "",
            fields.BARCODES_FIELD: "",
            fields.TAPE_LABELS_FIELD: "",
            fields.TAPE_NAMES_FIELD: "",
            fields.ARCHIVE_STATUS_FIELD: fields.STATUS_NONE,
            fields.ARCHIVE_TS_FIELD: "",
            fields.ARCHIVE_PLUGIN_FIELD: "",
            fields.ARCHIVE_POLICY_FIELD: "",
        }
    tapes = [tape for o in archived for tape in o.get("tapes", [])]
    result = {
        fields.EXTERNAL_IDS_FIELD: ", ".join(
            _distinct(o["entry"]["handle"] for o in archived)
        ),
        fields.BARCODES_FIELD: ", ".join(_distinct(t["barcode"] for t in tapes)),
        fields.TAPE_LABELS_FIELD: ", ".join(_distinct(t["label"] for t in tapes)),
        fields.TAPE_NAMES_FIELD: ", ".join(_distinct(t["volume_id"] for t in tapes)),
        fields.ARCHIVE_STATUS_FIELD: _item_status(originals),
    }
    result.update(_policy_fields())
    result[fields.ARCHIVE_TS_FIELD] = archive_ts(
        min(o["entry"]["btime"] for o in archived)
    )
    return result

"""What the migration writes, measured on prod rather than guessed.

Read on 2026-09-28 from ``portal.archive_framework.constants`` and
``ArchiwarePlugin.plugin_guid`` (Portal 6.2.1). Copied, not imported, so
the migration's logic stays importable off-server; Task 1 of the plan
re-reads them before the first write. Note the barcode field: it is
``af_p5_barcodes``, not the ``af_p5_tape_barcodes`` the 2023 script
documented.
"""

EXTERNAL_ID_FIELD = "portal_archive_external_id"
EXTERNAL_IDS_FIELD = "portal_archive_external_ids"
ARCHIVE_STATUS_FIELD = "portal_archive_status"
ARCHIVE_TS_FIELD = "portal_archive_ts"
ARCHIVE_PLUGIN_FIELD = "portal_archive_plugin_uuid"
ARCHIVE_POLICY_FIELD = "portal_archive_policy_uuid"
BARCODES_FIELD = "af_p5_barcodes"
TAPE_LABELS_FIELD = "af_p5_tape_labels"
TAPE_NAMES_FIELD = "af_p5_tape_names"
SHA1_FIELD = "portal_sha1"
ORIGINAL_FILENAME_FIELD = "componentOriginalFilename"
DURATION_FIELD = "durationSeconds"

ARCHIWARE_PLUGIN_GUID = "c4c1d403-801b-4b1a-95a1-6a692f64c262"
# P5 archive plan "Airbus Helicopters", whose index is AirbusHelicopters.
AIRBUS_POLICY_UUID = "aw-10007"

# Component level: the value Portal itself wrote on VX-35313's container.
COMPONENT_ARCHIVED = "ARCHIVED"
# Item level: portal.archive_framework.constants.STATUS_*.
STATUS_ARCHIVED_OFFLINE = "Archived"
STATUS_ARCHIVED_ONLINE = "Archived/Restored"
STATUS_NONE = ""

RUSHES_STORAGE = "VX-41"
# The wrapped files that are still on a disk, and the only ones ever deleted.
ONLINE_LEGACY_STORAGES = ("VX-26", "VX-11")
# The only file states a wrapped file may be deleted in (an allowlist: any
# other state, or none, keeps the file). Portal's getState vocabulary,
# measured on prod 2026-09-28: IMPORTED / NOT_IMPORTED / ARCHIVED / LOST;
# CLOSED is Vidispine's raw online state.
ONLINE_STATES = ("IMPORTED", "NOT_IMPORTED", "CLOSED")
ORIGINAL_TAG = "original"
LOWRES_TAG = "lowres"
# The wrapped shape is re-tagged, never deleted: reversible, touches no file.
LEGACY_WRAPPED_TAG = "legacy-wrapped"

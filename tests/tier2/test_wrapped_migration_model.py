"""Tier 2: migration 0019 applies and a row round-trips its JSON."""

import pytest
from django.db import IntegrityError, connection

from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration


def test_plan_and_rollback_are_ascii_json_text_columns(migrated_db):
    """jsonb itself is the SQL_ASCII hazard (Postgres decodes ``\\uXXXX``
    escapes back to real characters on input, which needs a server-encoding
    conversion that SQL_ASCII cannot do). The fields must be a TextField
    under the hood so the column is plain ``text``, never ``jsonb``.
    """
    from portal.plugins.TapelessIngest.models.wrapped_migration import (
        AsciiJSONField,
    )

    plan_field = WrappedMigration._meta.get_field("plan")
    rollback_field = WrappedMigration._meta.get_field("rollback")
    assert isinstance(plan_field, AsciiJSONField)
    assert isinstance(rollback_field, AsciiJSONField)
    assert plan_field.get_internal_type() == "TextField"
    assert rollback_field.get_internal_type() == "TextField"


def test_plan_round_trips_non_ascii_and_stores_pure_ascii(migrated_db):
    """Prod is SQL_ASCII: jsonb rejects any \\uXXXX escape above 0x7F, so
    the stored column must be pure ASCII even though the Python value
    round-trips the original non-ASCII text (BOM, accented name).
    """
    row = WrappedMigration.objects.create(
        item_id="VX-BOM",
        clip_umid="U-BOM",
        verdict="ready",
        plan={"xmp": "﻿Jean-François"},
    )
    row.refresh_from_db()
    assert row.plan == {"xmp": "﻿Jean-François"}
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT plan FROM {WrappedMigration._meta.db_table} " "WHERE item_id = %s",
            [row.item_id],
        )
        (raw,) = cursor.fetchone()
    assert raw.isascii()


def test_plan_and_rollback_default_to_empty_dict(migrated_db):
    row = WrappedMigration.objects.create(
        item_id="VX-DEFAULT", clip_umid="U-DEFAULT", verdict="ready"
    )
    assert row.plan == {}
    assert row.rollback == {}


def test_update_or_create_with_a_dict_still_works(migrated_db):
    WrappedMigration.objects.update_or_create(
        item_id="VX-UOC",
        defaults={"clip_umid": "U-UOC", "verdict": "ready", "plan": {"a": 1}},
    )
    row = WrappedMigration.objects.get(item_id="VX-UOC")
    assert row.plan == {"a": 1}


def test_row_round_trips(migrated_db):
    WrappedMigration.objects.create(
        item_id="VX-35313",
        clip_umid="U1",
        verdict="ready",
        plan={"originals": [{"relative": "2016/X/V.MXF", "entry": None}]},
        rollback={"wrapped_shape_id": "VX-72353"},
    )
    row = WrappedMigration.objects.get(item_id="VX-35313")
    assert row.phase == ""
    assert row.error == ""
    assert row.plan["originals"][0]["relative"] == "2016/X/V.MXF"
    assert row.rollback == {"wrapped_shape_id": "VX-72353"}


def test_one_row_per_item(migrated_db):
    WrappedMigration.objects.create(item_id="VX-1", clip_umid="U1", verdict="ready")
    with pytest.raises(IntegrityError):
        WrappedMigration.objects.create(item_id="VX-1", clip_umid="U2", verdict="ready")

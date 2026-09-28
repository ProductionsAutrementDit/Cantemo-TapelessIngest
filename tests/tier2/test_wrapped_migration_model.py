"""Tier 2: migration 0019 applies and a row round-trips its JSON."""

import pytest
from django.db import IntegrityError

from portal.plugins.TapelessIngest.models.wrapped_migration import WrappedMigration


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

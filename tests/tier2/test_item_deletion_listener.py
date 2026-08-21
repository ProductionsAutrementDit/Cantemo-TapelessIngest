"""The item-deletion listener, and the gap it left when it was unwired.

Deleting an item in Portal is supposed to reset the plugin's Clip row so the
next scan re-imports the file. The handler for that has always existed in
``plistner``, but nothing ever imported the module, so
``vidispine_post_delete.connect(...)`` never ran and the reset never
happened. Prod's ``__pycache__`` carried the evidence: ``plugin.pyc`` and
``__init__.pyc`` compiled under both Python 3.11 and 3.14, no
``plistner.pyc`` under either.

The damage was masked for years. ``Clip.item`` returns None on
``NotFoundError``, so ``create_item`` quietly made a new placeholder for a
clip whose item had been deleted, and the scan healed itself by accident.
Story 2.5's FR-23 rung then made the ingest decision from the stored
``item_id`` alone — no Vidispine call, which is the whole performance win —
and the accident stopped covering for the unwired listener. Measured on prod
2026-08-21: 25 RED clips carried an ``item_id`` whose item was gone, and the
scan reported every one of them "already ingested".

These tests pin both halves: the receiver is registered, and a reset clip is
one the ladder will ingest again.
"""

import portal.plugins.TapelessIngest.models as models_pkg
from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.scan.ingestion import will_ingest
from portal.vidispine.signals import vidispine_post_delete

# Deliberately reached through the models package rather than imported
# directly. `from ... import plistner` at the top of this file would run the
# module's connect() itself, and the registration test below would then be
# asserting that THIS FILE imported it — passing just as happily against the
# unwired plugin that shipped for years.
plistner = getattr(models_pkg, "plistner", None)


def test_the_deletion_receiver_is_actually_registered():
    """The bug was never the handler — it was that nothing imported it."""
    assert plistner is not None, "models/__init__.py no longer imports plistner"
    assert plistner.item_post_delete_handler in vidispine_post_delete.receivers


def test_importing_the_models_package_is_what_registers_it():
    """Pins WHERE the wiring lives, so a tidy-up cannot silently undo it."""
    assert getattr(models_pkg, "plistner", None) is not None


def test_deleting_an_item_clears_the_ids_that_would_skip_the_clip(migrated_db):
    clip = Clip.objects.create(
        umid="DELETED-ITEM",
        item_id="VX-209383",
        job_id="VX-777",
        path="2026/AA_20260804/K001.RDC/K001_001.R3D",
        provider_name="red",
    )

    plistner.item_post_delete_handler(instance="VX-209383", method="removeItem")

    clip.refresh_from_db()
    assert clip.item_id == ""
    assert clip.job_id == ""
    assert clip.file_id is None


def test_a_reset_clip_is_one_the_ladder_will_ingest_again(migrated_db):
    """The point of the reset, stated as the ladder sees it.

    ``has_hash`` is unaffected by the reset: it comes from the file the scan
    attaches each run (``Clip.cached_file_hash`` reads ``_file``), not from
    the ``file_id`` column the handler nulls.
    """
    assert (
        will_ingest(item_id="VX-209383", replace=False, has_hash=True) is False
    ), "before deletion the clip is correctly skipped as already-ingested"

    clip = Clip.objects.create(
        umid="RESET-THEN-INGEST",
        item_id="VX-209383",
        job_id="VX-777",
        path="2026/AA_20260804/K001.RDC/K001_001.R3D",
        provider_name="red",
    )
    plistner.item_post_delete_handler(instance="VX-209383", method="removeItem")
    clip.refresh_from_db()

    assert will_ingest(item_id=clip.item_id, replace=False, has_hash=True) is True


def test_other_vidispine_deletions_leave_clips_alone(migrated_db):
    """Only ``removeItem`` resets. A collection or shape deletion must not."""
    clip = Clip.objects.create(
        umid="UNTOUCHED",
        item_id="VX-209383",
        job_id="VX-777",
        path="2026/AA_20260804/K002.RDC/K002_001.R3D",
        provider_name="red",
    )

    plistner.item_post_delete_handler(instance="VX-209383", method="removeCollection")

    clip.refresh_from_db()
    assert clip.item_id == "VX-209383"
    assert clip.job_id == "VX-777"


def test_only_the_deleted_item_is_reset(migrated_db):
    """A shared-prefix or unrelated item_id must not be caught in the reset."""
    target = Clip.objects.create(
        umid="TARGET", item_id="VX-209383", job_id="VX-1", path="a", provider_name="red"
    )
    bystander = Clip.objects.create(
        umid="BYSTANDER",
        item_id="VX-2093830",
        job_id="VX-2",
        path="b",
        provider_name="red",
    )

    plistner.item_post_delete_handler(instance="VX-209383", method="removeItem")

    target.refresh_from_db()
    bystander.refresh_from_db()
    assert target.item_id == ""
    assert bystander.item_id == "VX-2093830"

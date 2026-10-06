"""Tier 2: a `braw_*` key a newer camera adds is mappable once stored.

The measured inventory (`KNOWN_METADATA_KEYS`) covers five bodies; the
mapping form must also offer any `braw_*` row a stored BRAW clip carries,
and nothing a non-BRAW clip happens to store under that prefix.
"""

from portal.plugins.TapelessIngest import forms
from portal.plugins.TapelessIngest.models.clip import Clip, ClipMetadata
from portal.plugins.TapelessIngest.providers import braw as braw_module


def test_a_stored_braw_key_becomes_selectable(migrated_db, monkeypatch):
    monkeypatch.setattr(braw_module, "_stored_names_cache", None)
    clip = Clip.objects.create(umid="BRAW-1", path="2026/AH_x", provider_name="braw")
    ClipMetadata.objects.create(clip=clip, name="braw_new_camera_key", value="1")
    ClipMetadata.objects.create(clip=clip, name="braw_camera_id", value="x")
    other = Clip.objects.create(umid="RED-1", path="2026/AH_y", provider_name="red")
    ClipMetadata.objects.create(clip=other, name="braw_not_ours", value="1")

    choices = dict(forms.get_provider_metadatas())

    assert choices["braw_new_camera_key"] == "BRAW: new_camera_key"
    assert "braw_not_ours" not in choices
    keys = [key for key, _label in forms.get_provider_metadatas()]
    assert keys.count("braw_camera_id") == 1


def test_the_stored_names_are_queried_once_per_ttl(migrated_db, monkeypatch):
    """One DISTINCT per TTL, not one per mapping row of the formset."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    monkeypatch.setattr(braw_module, "_stored_names_cache", None)
    clip = Clip.objects.create(umid="BRAW-2", path="2026/AH_x", provider_name="braw")
    ClipMetadata.objects.create(clip=clip, name="braw_first", value="1")
    with CaptureQueriesContext(connection) as captured:
        for _row in range(5):
            braw_module.Provider().getAvailableMetadatas()
    assert len(captured) == 1
    ClipMetadata.objects.create(clip=clip, name="braw_second", value="1")
    monkeypatch.setattr(braw_module, "_stored_names_cache", (0.0, ()))  # expired
    assert "braw_second" in dict(braw_module.Provider().getAvailableMetadatas())

"""Tier 1: the mapping form offers every provider's keys, base 13 first.

`forms.get_provider_metadatas` used to list `BaseProvider`'s 13 keys
only, so a `braw_*` value could be stored and never mapped. It now takes
the union over `PROVIDER_NAMES`: the base keys first and unchanged, then
each provider's own, duplicates dropped.
"""

import pytest

from portal.plugins.TapelessIngest import forms
from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
from portal.plugins.TapelessIngest.providers import braw as braw_module
from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider


def test_braw_is_registered_before_file():
    assert PROVIDER_NAMES.index("braw") < PROVIDER_NAMES.index("file")
    assert PROVIDER_NAMES[-1] == "file"


def test_the_base_13_come_first_and_unchanged():
    base = BaseProvider().getAvailableMetadatas()
    assert len(base) == 13
    assert forms.get_provider_metadatas()[:13] == tuple(base)


def test_every_braw_key_is_selectable_once():
    choices = forms.get_provider_metadatas()
    keys = [key for key, _label in choices]
    assert len(keys) == len(set(keys))
    for name in braw_module.KNOWN_METADATA_KEYS:
        assert name in keys
    for field, _label in braw_module.PROBE_FIELDS:
        assert "braw_probe_" + field in keys


def test_the_braw_provider_lists_base_keys_then_labelled_braw_keys():
    listed = braw_module.Provider().getAvailableMetadatas()
    assert listed[:13] == BaseProvider().getAvailableMetadatas()
    labels = dict(listed)
    assert labels["braw_camera_id"] == "BRAW: camera_id"
    assert labels["braw_frame0_analog_gain"] == "BRAW frame 0: analog_gain"
    assert labels["braw_probe_width"] == "BRAW probe: Width"


def test_a_provider_that_cannot_list_costs_only_its_own_keys(monkeypatch):
    real = Clip.get_provider_by_name

    class _Broken:
        def getAvailableMetadatas(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        Clip,
        "get_provider_by_name",
        classmethod(
            lambda cls, name, clip=None: _Broken() if name == "red" else real(name)
        ),
    )
    keys = [key for key, _label in forms.get_provider_metadatas()]
    assert "braw_camera_id" in keys
    assert keys[:13] == [key for key, _ in BaseProvider().getAvailableMetadatas()]


@pytest.mark.parametrize("name", PROVIDER_NAMES)
def test_every_registry_provider_still_answers(name):
    assert Clip.get_provider_by_name(name).getAvailableMetadatas()


def test_a_form_build_computes_the_union_once(monkeypatch):
    """One MetadataMappingForm per mapping row must not instantiate and
    list every provider again."""
    calls = []
    real = forms.get_provider_metadatas

    def _counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(forms, "get_provider_metadatas", _counting)
    monkeypatch.setattr(forms, "_provider_metadatas_cache", None)
    monkeypatch.setattr(forms, "get_system_fields", lambda: ())
    for _row in range(5):
        form = forms.MetadataMappingForm()
    assert len(calls) == 1
    keys = [key for key, _label in form.fields["metadata_provider"].choices]
    assert "braw_camera_id" in keys

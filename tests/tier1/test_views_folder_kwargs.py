"""``ClipsInPathsView`` builds its ``Folder`` from the model's CONCRETE
fields only — the three faces of that filter, DB-free.

The serializer's ``validated_data`` is keyed by SOURCE, not by declared
name: ``umid = CharField(source="id")`` lands as ``id``. So the filter
compares sources against model field names, and `SERIALIZER_ONLY_FOLDER_FIELDS`
names the sources that deliberately have no model field.
"""

import logging

import pytest
from rest_framework.test import APIRequestFactory

from portal.plugins.TapelessIngest import views as views_module
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.serializers import FolderSerializer
from portal.plugins.TapelessIngest.views import ClipsInPathsView


class _AcceptingFolderSerializer:
    """A `FolderSerializer` double answering a chosen `validated_data`.

    The real one's `is_valid()` runs model validators that may query,
    and tier 1 is DB-free; the filter under test reads only the dict.
    """

    validated_data = {}

    def __init__(self, data=None):
        self.data = data
        self.errors = {}

    def is_valid(self):
        return True


def _post(monkeypatch, validated_data):
    recorded = {}
    monkeypatch.setattr(
        _AcceptingFolderSerializer, "validated_data", dict(validated_data)
    )
    monkeypatch.setattr(views_module, "FolderSerializer", _AcceptingFolderSerializer)

    def fake_ingest(self, **kwargs):
        recorded["folder"] = self
        return {"clips": []}

    monkeypatch.setattr(Folder, "ingest", fake_ingest)
    request = APIRequestFactory().post(
        "/api/clips/", {"folder": {"path": "x"}, "clips": "__all__"}, format="json"
    )
    # No authentication: `ClipsInPathsView` declares no permission
    # classes of its own and the test settings set no default, so the
    # request reaches the kwargs seam as-is.
    response = ClipsInPathsView.as_view()(request)
    return response, recorded


def _model_kwargs():
    return {
        "id": "2026/AH_20260101_rest",
        "path": "2026/AH_20260101_rest",
        "storage_id": "VX-41",
        "provider_names": "file",
    }


def test_a_key_named_like_the_reverse_relation_is_dropped(monkeypatch):
    """`Folder._meta.get_fields()` lists `Clip.folders`' reverse relation
    under the name `clip`; `concrete_fields` does not. `Model.__init__`
    accepts any name `get_field()` resolves, so under the former a
    serializer key `clip` went through `Folder(**kwargs)` and landed as
    a stray attribute on the instance — silently.

    Mutation killed: `{f.name for f in Folder._meta.get_fields()}` —
    `clip` is then on the instance.
    """
    assert "clip" in {f.name for f in Folder._meta.get_fields()}

    response, recorded = _post(monkeypatch, {**_model_kwargs(), "clip": "nope"})

    assert response.status_code == 201, response.data
    assert recorded["folder"].id == "2026/AH_20260101_rest"
    assert "clip" not in vars(recorded["folder"])


def test_the_dropped_log_fires_for_a_typo_and_not_for_error(monkeypatch, caplog):
    """`error` is dropped on EVERY request, so logging it made the line
    fire unconditionally — the same as not having it. An unknown key is
    what the line exists to surface.

    Mutations killed: dropping `- SERIALIZER_ONLY_FOLDER_FIELDS` (fires
    for `error`); dropping the log (never fires).
    """
    logger = "portal.plugins.TapelessIngest.views"

    with caplog.at_level(logging.INFO, logger=logger):
        response, _ = _post(monkeypatch, {**_model_kwargs(), "error": "none"})
    assert response.status_code == 201, response.data
    assert not [
        record
        for record in caplog.records
        if "not on the Folder model" in record.getMessage()
    ]

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger):
        response, _ = _post(monkeypatch, {**_model_kwargs(), "pathh": "typo"})
    assert response.status_code == 201, response.data
    fired = [
        record.getMessage()
        for record in caplog.records
        if "not on the Folder model" in record.getMessage()
    ]
    assert len(fired) == 1
    assert "pathh" in fired[0]


def test_serializer_only_fields_are_exactly_the_writable_sources_off_the_model():
    """`SERIALIZER_ONLY_FOLDER_FIELDS` is the set of writable
    `FolderSerializer` SOURCES with no concrete `Folder` field — not a
    hand-maintained guess. A serializer field added without a model
    field behind it, or a model field removed, must move this set or
    the "dropped" line starts firing on every request again.

    Mutation killed: any other constant in `SERIALIZER_ONLY_FOLDER_FIELDS`.
    """
    writable_sources = {
        field.source
        for field in FolderSerializer().fields.values()
        if not field.read_only
    }
    concrete = {field.name for field in Folder._meta.concrete_fields}

    assert views_module.SERIALIZER_ONLY_FOLDER_FIELDS == writable_sources - concrete
    # ...and the sources really are sources: `umid` lands as `id`.
    assert "id" in writable_sources and "umid" not in writable_sources

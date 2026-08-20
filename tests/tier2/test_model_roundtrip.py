"""Tier 2: migrations 0001–0016 apply on sqlite and a model round-trips."""

import uuid

from portal.plugins.TapelessIngest.models.folder import Folder


def test_folder_roundtrip(migrated_db):
    folder = Folder(
        path="2026/TEST_SHOOT",
        storage_id="VX-41",
        provider_names="xdcam,file",
    )
    folder.save()

    assert isinstance(folder.pk, uuid.UUID)

    reloaded = Folder.objects.get(pk=folder.pk)
    assert reloaded.pk == folder.pk
    assert isinstance(reloaded.pk, uuid.UUID)
    assert reloaded.path == "2026/TEST_SHOOT"
    assert reloaded.storage_id == "VX-41"
    assert reloaded.provider_names == "xdcam,file"
    assert reloaded.clips_total == 0

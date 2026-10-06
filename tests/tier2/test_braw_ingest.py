"""Tier 2: a `.braw` clip, scan to posted shape, through the REAL provider.

brawprobe is stubbed at `sp.run` with a real captured output (the 6K
off-speed clip); everything else — extraction, persistence of the
`braw_*` rows, the shape route of `Clip.import_file`, the `lowres-forge`
transcode request — is the plugin's own code against the Vidispine fake.
"""

import json
import os

import pytest

from portal.plugins.TapelessIngest.models.clip import (
    TRANSCODE_SHAPE_TAG,
    Clip,
    ClipMetadata,
)
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.providers import braw as braw_module

from tests.portal_stub import VidispineFake

STORAGE_ID = "VX-41"
FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures", "braw"
)


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


@pytest.fixture
def collection_seam(monkeypatch):
    monkeypatch.setattr(
        Folder, "getCollection", lambda self, user, dryrun=False: "VX-COLLECTION"
    )


@pytest.fixture
def brawprobe(monkeypatch):
    calls = []

    def _install(fixture, returncode=0, stderr=""):
        with open(
            os.path.join(FIXTURES, fixture + ".json"), encoding="utf-8"
        ) as handle:
            stdout = handle.read()

        def _run(cmd, **kwargs):
            calls.append(cmd)
            return _Completed(
                stdout=stdout if returncode == 0 else "",
                stderr=stderr,
                returncode=returncode,
            )

        monkeypatch.setattr(
            braw_module, "resolve_brawprobe_path", lambda: "/usr/local/bin/brawprobe"
        )
        monkeypatch.setattr(braw_module.sp, "run", _run)
        return calls

    return _install


def _page(tmp_path, rel, name, es_page):
    (tmp_path / rel).mkdir(parents=True, exist_ok=True)
    (tmp_path / rel / name).write_bytes(b"braw data")
    source = {
        "path": f"{rel}/{name}",
        "hash": "hash-braw",
        "storage": STORAGE_ID,
        "id": "VX-41-braw",
        "size": 1024,
    }
    return es_page([source], total=1)


def _folder(tmp_path, rel):
    folder = Folder(storage_id=STORAGE_ID, path=rel)
    folder._root_path = str(tmp_path)
    return folder


def _calls(name):
    return [details for call, details in VidispineFake.calls if call == name]


@pytest.mark.parametrize(
    "name",
    ["0979-Gimbal-T1096_09091448_C004.braw", "0979-Gimbal-T1096_09091448_C004.BRAW"],
)
def test_a_braw_clip_is_scanned_stored_and_posted_as_a_whole_shape(
    migrated_db, es_fake, es_page, tmp_path, collection_seam, brawprobe, name
):
    calls = brawprobe("0979-Gimbal-T1096_09091448_C004")
    rel = "2024/AH_20240910_GIMBAL_6K"
    es_fake.push(_page(tmp_path, rel, name, es_page))

    response = _folder(tmp_path, rel).ingest(providers=["braw"])

    assert (response["ingested"], response["failed"]) == (1, 0), response
    assert response["errors"] == []
    # One probe, at scan time, on the absolute path.
    assert calls == [["/usr/local/bin/brawprobe", "--", str(tmp_path / rel / name)]]

    clip = Clip.objects.get(provider_name="braw")
    rows = dict(ClipMetadata.objects.filter(clip=clip).values_list("name", "value"))
    assert rows["braw_camera_id"] == "24e55415-41b1-4df5-977e-0102659a2290"
    assert rows["braw_crop_size"] == "6048x3200"
    assert rows["braw_probe_offspeed"] == "true"
    assert rows["framerate"] == "25/1"
    with open(
        os.path.join(FIXTURES, "0979-Gimbal-T1096_09091448_C004.json"), encoding="utf-8"
    ) as handle:
        expected = braw_module.metadatas_from_probe(json.load(handle))
    # Every stored value, persisted as the provider produced it.
    assert {k: rows.get(k) for k in expected} == expected
    assert all(len(value) <= 200 for value in rows.values())

    posted = _calls("createShapeFromDocument")
    assert len(posted) == 1
    document = posted[0]["document"]
    assert document["containerComponent"]["file"] == [{"id": clip.file_id}]
    assert document["videoComponent"][0]["resolution"] == {
        "width": 6048,
        "height": 3200,
    }
    assert document["audioComponent"][0]["channelCount"] == 2
    transcodes = _calls("requestItemTranscode")
    assert len(transcodes) == 1
    assert TRANSCODE_SHAPE_TAG in json.dumps(transcodes[0])


def test_an_unreadable_braw_is_reported_and_not_ingested(
    migrated_db, es_fake, es_page, tmp_path, collection_seam, brawprobe
):
    brawprobe(
        "0979-Gimbal-T1096_09091448_C004",
        returncode=4,
        stderr='brawprobe: event=error stage=clip_load message="the SDK could not open the clip"',
    )
    rel = "2024/AH_20240910_BROKEN"
    es_fake.push(_page(tmp_path, rel, "broken.braw", es_page))

    response = _folder(tmp_path, rel).ingest(providers=["braw"])

    assert response["ingested"] == 0
    assert "could not open the clip" in json.dumps(response["errors"])
    assert not Clip.objects.filter(provider_name="braw").exists()
    assert _calls("createShapeFromDocument") == []

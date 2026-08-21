"""Tier 2: a speculative sidecar probe is not a folder error (FR-22).

Production defect, found by the first real Epic 2 dry run over
``2026/AH_.../DCIM/DJI_001`` (a DJI drone card, claimed by ``hdslr``):

    76 already ingested, 0 created, ... 1 errors encountered:
    Error listing directory .../DCIM/CLIP: [Errno 2] No such file or directory

``panasonicP2`` (and ``ikegami``, and any future card provider guarded on
a sibling-directory sidecar) probes ``../CLIP/{name}.XML`` for EVERY file
it is offered, because sidecar presence IS its guard. Story 2.3 routed
those probes through ``FolderListings`` (FR-16), which records a failed
``os.scandir`` on the cache; story 2.6 surfaces every recorded failure
into ``response["errors"]`` (FR-22). Neither is wrong alone; composed,
every folder whose card type does not match a probing provider emits one
phantom error per probed directory — thousands a week across the cron's
tree, which is exactly what makes an error COUNT worthless.

The ruling pinned here, in both directions:

- a PROBE that finds NO DIRECTORY (``ENOENT``/``ENOTDIR``) got an
  ANSWER — "this is not a P2 card" — and is not a folder error;
- a PROBE that could not LOOK (``EACCES``, and every other ``OSError``)
  is still a failure and is still reported: the provider silently did
  not match a card it may well have had to match;
- a listing the SCAN needed — the folder's own directory — is reported
  whatever the errno, ``ENOENT`` included.

ORM needed: Clip.get_or_new and folder.save hit the DB.
"""

import os

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.providers.providers import Provider as BaseProvider

STORAGE_ID = "VX-41"
P2_LIKE_NAME = "fakep2like"


def _source(path, file_id):
    return {
        "path": path,
        "hash": f"hash-{file_id}",
        "storage": STORAGE_ID,
        "id": file_id,
        "size": 1024,
    }


def _folder(tmp_path, rel_path):
    folder = Folder(storage_id=STORAGE_ID, path=rel_path)
    folder._root_path = str(tmp_path)
    return folder


class P2LikeProvider(BaseProvider):
    """``panasonicP2``'s shape: an unconditional ``../CLIP/{name}.XML`` probe.

    Sidecar presence is the only guard, so the probe runs for every file
    the provider is offered — including files on a card that is not a P2
    card at all, whose media directory has no ``../CLIP`` sibling.
    """

    def __init__(self):
        BaseProvider.__init__(self)
        self.name = "Fake P2-like Provider"
        self.machine_name = P2_LIKE_NAME
        self.probes = []

    def getExtensions(self):
        return [".fake"]

    def is_extension_guarded(self):
        return False

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        filename, _ = os.path.splitext(media_file.getFileName())
        media_dirname = os.path.dirname(
            self.get_file_absolute_path(media_file, context)
        )
        sidecar = os.path.normpath(
            os.path.join(media_dirname, "../CLIP/" + filename + ".XML")
        )
        self.probes.append(self.probe_is_file(sidecar, context))
        # Not a P2 card: contribute nothing, exactly like the real one.
        metadatas["provider"] = "hdslr-stand-in"
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas


@pytest.fixture
def p2_like_provider():
    provider = P2LikeProvider()
    Clip._PROVIDER_CACHE[P2_LIKE_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(P2_LIKE_NAME, None)


def _dji_card(tmp_path):
    """The production layout: media in DCIM/DJI_001, no DCIM/CLIP sibling."""
    rel = "2026/AH_20260819_U050CAPAX_DRONE/DCIM/DJI_001"
    clip_dir = tmp_path / rel
    clip_dir.mkdir(parents=True)
    (clip_dir / "DJI_0001.fake").write_bytes(b"clip data")
    return rel, clip_dir


def test_a_probe_into_a_missing_directory_is_not_a_folder_error(
    migrated_db, es_fake, es_page, p2_like_provider, tmp_path
):
    rel, _ = _dji_card(tmp_path)
    es_fake.push(es_page([_source(f"{rel}/DJI_0001.fake", "VX-41-DJI1")], total=1))

    response = _folder(tmp_path, rel).scan(providers=[P2_LIKE_NAME])

    # The probe ran and answered "absent" — that IS the provider's answer.
    assert p2_like_provider.probes == [False]
    assert response["processed"] == 1
    # ...and the missing DCIM/CLIP sibling is not reported as a failure.
    assert response["errors"] == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores 0o000 directory permissions",
)
def test_a_probe_into_an_unreadable_directory_is_still_reported(
    migrated_db, es_fake, es_page, p2_like_provider, tmp_path
):
    """The half that must NOT regress: absent is an answer, unreadable is not.

    The sidecar directory exists and may well hold the sidecar; the scan
    could not look, so the provider silently did not match. Reporting it
    is the whole point of FR-22.
    """
    rel, clip_dir = _dji_card(tmp_path)
    locked = clip_dir.parent / "CLIP"
    locked.mkdir()
    (locked / "DJI_0001.XML").write_bytes(b"<xml/>")
    locked.chmod(0o000)
    es_fake.push(es_page([_source(f"{rel}/DJI_0001.fake", "VX-41-DJI1")], total=1))
    try:
        response = _folder(tmp_path, rel).scan(providers=[P2_LIKE_NAME])
    finally:
        locked.chmod(0o755)

    assert response["processed"] == 1
    assert response["errors"] == [
        f"Error listing directory {locked}: [Errno 13] Permission denied: '{locked}'"
    ]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores 0o000 directory permissions",
)
def test_the_folders_own_unreadable_directory_is_still_reported(
    migrated_db, es_fake, es_page, p2_like_provider, tmp_path
):
    """A listing the SCAN needed, failing: reported, as before this fix."""
    rel, clip_dir = _dji_card(tmp_path)
    clip_dir.chmod(0o000)
    es_fake.push(es_page([_source(f"{rel}/DJI_0001.fake", "VX-41-DJI1")], total=1))
    try:
        response = _folder(tmp_path, rel).scan(providers=[P2_LIKE_NAME])
    finally:
        clip_dir.chmod(0o755)

    assert (
        f"Error listing directory {clip_dir}: "
        f"[Errno 13] Permission denied: '{clip_dir}'" in response["errors"]
    )


def test_the_folders_own_missing_directory_is_still_reported(
    migrated_db, es_fake, es_page, p2_like_provider, tmp_path
):
    """ENOENT is silenced for a PROBE only — never for the scan's own path.

    The index still reports a hit for a folder whose directory is gone;
    that divergence is real and must not be filtered away with the
    speculative probes.
    """
    rel = "2026/AH_20260819_GONE/DCIM/DJI_001"
    gone = tmp_path / rel
    es_fake.push(es_page([_source(f"{rel}/DJI_0001.fake", "VX-41-GONE")], total=1))

    response = _folder(tmp_path, rel).scan(providers=[P2_LIKE_NAME])

    assert (
        f"Error listing directory {gone}: "
        f"[Errno 2] No such file or directory: '{gone}'" in response["errors"]
    )

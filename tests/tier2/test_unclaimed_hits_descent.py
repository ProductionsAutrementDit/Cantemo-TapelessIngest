"""Tier 2: an extractor outage must not invert the walk.

The end-to-end half of ``tests/tier1/test_unclaimed_hits_doubt.py``.

Measured on the REDline outage (commit e6d0090): ``REDline`` lives in
``/usr/local/bin``, which is not on the PATH ``/etc/crontab`` declares, so
under cron it exited 127 with empty stdout. ``red`` still MATCHED every
``.R3D`` — it just produced no umid — and ``Clip.extract_file_metadatas``
raised ``No UMID found in file …`` for each one. Every file became an
error, no file became a clip, and the descent gate read the resulting
empty consumed set as "descend into everything": 118 folders / 584 clips /
556 errors where a correct run reports 49 / 203 / 0.

The provider double here reproduces that exact shape — claims the file,
returns metadatas with no umid — rather than raising, so the test
exercises the real failure path instead of a synthetic one.
"""

import re

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.scan.adapters import build_context

STORAGE_ID = "VX-41"
ROOT = "2026"
BROKEN_NAME = "brokenextractor"


class _BrokenExtractor:
    """A provider whose external tool is gone: it claims, and yields nothing."""

    name = "Broken Extractor"
    machine_name = BROKEN_NAME

    def getExtensions(self):
        return [".fake"]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        # Claims the file, writes its name, and never reaches a umid —
        # REDline exiting 127 leaves the CSV reader with no row.
        metadatas["provider"] = self.machine_name
        return metadatas


@pytest.fixture
def broken_extractor():
    provider = _BrokenExtractor()
    Clip._PROVIDER_CACHE[BROKEN_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(BROKEN_NAME, None)


def _source(path):
    return {
        "path": path,
        "hash": f"hash-{path}",
        "storage": STORAGE_ID,
        "id": f"VX-41-{path}",
        "size": 1024,
    }


def _folder(tmp_path, rel_path):
    folder = Folder(storage_id=STORAGE_ID, path=rel_path)
    folder._root_path = str(tmp_path)
    return folder


def _write(tmp_path, *rel_paths):
    for rel in rel_paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")


def _queried_folder(search_doc):
    found = []

    def walk(node):
        if isinstance(node, dict):
            regexp = node.get("regexp")
            if isinstance(regexp, dict) and "parent" in regexp:
                found.append(regexp["parent"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(search_doc)
    return min(found, key=len) if found else None


def test_a_folder_whose_files_all_fail_extraction_forbids_descent(
    migrated_db, es_fake, es_page, tmp_path, broken_extractor
):
    """Hits found, nothing claimed, errors recorded — the walk must stop."""
    rel = f"{ROOT}/AH_20260101_redcard"
    _write(tmp_path, f"{rel}/A001.fake", f"{rel}/A002.fake")
    es_fake.push(
        es_page([_source(f"{rel}/A001.fake"), _source(f"{rel}/A002.fake")], total=2)
    )

    response = _folder(tmp_path, rel).scan(number=0, providers=[BROKEN_NAME])

    assert response["consumed_subdirs"] is None
    assert any("none became a clip" in error for error in response["errors"])
    # The folder is STOPPED, not silently emptied: it still reports what
    # it found, and the per-file failures are still in the report.
    assert (response["hits"], response["processed"]) == (2, 2)
    assert sum("No UMID found" in error for error in response["errors"]) == 2


def test_the_walk_does_not_descend_into_a_folder_whose_extraction_broke(
    migrated_db, es_fake, es_page, tmp_path, storage_fake, broken_extractor
):
    """The blast radius, contained — this is what the 118-vs-49 run was."""
    shoot = f"{ROOT}/AH_20260101_blast"
    _write(tmp_path, f"{shoot}/A001.fake")
    # The card internals the broken run walked into, and a sibling that
    # must still be walked so the test cannot pass by stopping everything.
    _write(tmp_path, f"{shoot}/A001.RDC/A001_001.fake")
    (tmp_path / f"{ROOT}/AH_20260101_other").mkdir(parents=True, exist_ok=True)

    queried = []

    def respond(search_doc, first, number):
        folder = _queried_folder(search_doc)
        queried.append(folder)
        if folder == re.escape(shoot):
            return es_page([_source(f"{shoot}/A001.fake")], total=1)
        return es_page([], total=0)

    es_fake.route(respond)
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    ctx = build_context(
        [STORAGE_ID],
        user=None,
        dry_run=True,
        providers=[BROKEN_NAME],
        legacy_storages=[],
        replace=False,
        startwith=["AH_"],
    )

    folder = Folder(storage_id=STORAGE_ID, path=ROOT)
    folder._root_path = ctx.root_path_for(STORAGE_ID)
    folder.scan_tree(ctx, emit=[].append)

    assert re.escape(f"{shoot}/A001.RDC") not in queried
    assert re.escape(f"{ROOT}/AH_20260101_other") in queried


def test_a_folder_whose_only_hits_are_ghosts_is_still_descended_into(
    migrated_db, es_fake, es_page, tmp_path, broken_extractor
):
    """The index/filesystem desync (DC-2) is expected, not alarming.

    A ghost is an index entry with no file behind it. It contributes a hit
    ROW, fails the real-filesystem guard, and lands in ``errors`` — which
    is FR-22 working exactly as designed. Nothing about the folder is
    unknown: the guard did its job, and there was never any media to
    claim. Stopping the walk there would punish the desync the guard
    exists to absorb, which is why the rule counts files that PASSED
    verification, not rows the index returned.
    """
    rel = f"{ROOT}/AH_20260101_ghosts"
    (tmp_path / rel / "CARD").mkdir(parents=True, exist_ok=True)
    # The row is returned but the file was never written to disk, and the
    # fixture's total is 0 — the shape `tests/tier2/test_selective_recursion.py`
    # builds for the same case.
    es_fake.push(es_page([_source(f"{rel}/GHOST.fake")], total=0))

    response = _folder(tmp_path, rel).scan(number=0, providers=[BROKEN_NAME])

    assert any("does not exist" in error for error in response["errors"])
    assert response["consumed_subdirs"] == frozenset()
    assert not any("Not descending into" in error for error in response["errors"])


def test_a_folder_that_found_nothing_at_all_is_still_descended_into(
    migrated_db, es_fake, es_page, tmp_path, broken_extractor
):
    """The intermediate shoot folder — and the reason the walk exists.

    Zero hits is not a failure to claim anything, and turning it into
    doubt would stop the recursion at the top of every tree.
    """
    rel = f"{ROOT}/AH_20260101_intermediate"
    (tmp_path / rel / "CARD").mkdir(parents=True, exist_ok=True)
    es_fake.push(es_page([], total=0))

    response = _folder(tmp_path, rel).scan(number=0, providers=[BROKEN_NAME])

    assert response["consumed_subdirs"] == frozenset()
    assert response["errors"] == []

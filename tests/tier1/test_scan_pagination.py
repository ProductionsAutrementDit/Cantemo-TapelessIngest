"""Tier 1 pins for Folder.scan: count_only pagination, error paths, quirks.

Unsaved Folder, count_only variants and error paths only — any non-count scan
hits the ORM (Clip.get_or_new / folder.save) and lives in Tier 2. Every scan
passes providers=[fake] so the FakeProvider registered in _PROVIDER_CACHE is
used (providers=None would fall back to the real PROVIDERS_LIST → ffprobe).
"""

import logging

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder

STORAGE_ID = "VX-41"
PATH = "2026/AH_20260101_scanpin"


def _folder(root="/golden-root"):
    folder = Folder(storage_id=STORAGE_ID, path=PATH)
    # Presetting _root_path bypasses StorageHelper/cache entirely.
    folder._root_path = root
    return folder


def test_count_multi_page_call_sequence(es_fake, es_page, fake_provider):
    # Pages deliberately carry DIFFERENT totals so the hits pin discriminates:
    # scan overwrites response["hits"] from each page, so the LAST page's
    # total wins (ledger entry "hits reflects only the last page's total",
    # tests/pinned-bugs.md).
    es_fake.push(es_page([{}] * 100, total=137))
    es_fake.push(es_page([{}] * 37, total=140))

    folder = _folder()
    response = folder.scan(
        number=0, count_only=True, providers=[fake_provider.machine_name]
    )

    # number=0 → page size 100, loops while a page is full, incrementing first.
    assert es_fake.calls == [(0, 100), (100, 100)]
    # `consumed_subdirs` is story 2.6's additive key (NFR-5-compatible,
    # sanctioned pin edit). A count_only pass assembles no clip, so it can
    # say nothing about consumption: None — DOUBT, "descent not authorized".
    assert response == {
        "clips": [],
        "hits": 140,
        "errors": [],
        "created": 0,
        "already_ingested": 0,
        "processed": 0,
        "consumed_subdirs": None,
    }
    # Golden-doc wiring: scan must send exactly build_search_doc's output for
    # the provider list it resolved (same interpreter → same set ordering, so
    # plain equality needs no seed machinery), with doc_type ["file"], on
    # every page.
    expected_doc = folder.build_search_doc(
        Clip._get_provider_list([fake_provider.machine_name])
    )
    assert es_fake.call_docs == [(expected_doc, ["file"])] * 2


def test_count_single_page(es_fake, es_page, fake_provider):
    es_fake.push(es_page([{}] * 37, total=37))

    response = _folder().scan(
        number=0, count_only=True, providers=[fake_provider.machine_name]
    )

    assert es_fake.calls == [(0, 100)]
    assert response["hits"] == 37
    assert response["processed"] == 0
    assert response["clips"] == []
    assert response["errors"] == []


def test_count_exactly_full_final_page(es_fake, es_page, fake_provider):
    # Boundary at total % page_size == 0: the loop only stops on a NON-full
    # page, so an exactly-full final page costs one extra (empty) query.
    es_fake.push(es_page([{}] * 100, total=200))
    es_fake.push(es_page([{}] * 100, total=200))
    es_fake.push(es_page([], total=200))

    response = _folder().scan(
        number=0, count_only=True, providers=[fake_provider.machine_name]
    )

    assert es_fake.calls == [(0, 100), (100, 100), (200, 100)]
    assert response["hits"] == 200
    assert response["processed"] == 0
    assert response["clips"] == []


def test_no_resolvable_root(es_fake, fake_provider):
    folder = _folder(root="")

    response = folder.scan(
        number=0, count_only=True, providers=[fake_provider.machine_name]
    )

    # Recorded in the response, no raise, and the index is never queried.
    assert es_fake.calls == []
    assert response == {
        "clips": [],
        "hits": 0,
        "errors": [f"Cannot get full path from storage {STORAGE_ID}, path {PATH}"],
        "created": 0,
        "already_ingested": 0,
        "processed": 0,
        # No absolute path -> nothing was scanned -> DOUBT, never an empty
        # frozenset (which would authorize descending everywhere).
        "consumed_subdirs": None,
    }


def test_storage_none_reports_cannot_get_full_path(es_fake, fake_provider):
    # Ledger row #5 FIXED by story 2.6 (FR-28; deleted from
    # tests/pinned-bugs.md, declared in tests/fr4-waivers.md). Pre-2.6 an
    # unresolvable storage left `_root_path` unassigned and the property
    # raised AttributeError out of the whole run; it now returns None, so
    # the folder reports the same "Cannot get full path" entry a falsy
    # root has always produced, and the run continues.
    folder = Folder(storage_id=None, path=PATH)

    response = folder.scan(
        number=0, count_only=True, providers=[fake_provider.machine_name]
    )

    assert response == {
        "clips": [],
        "hits": 0,
        "errors": [f"Cannot get full path from storage None, path {PATH}"],
        "created": 0,
        "already_ingested": 0,
        "processed": 0,
        "consumed_subdirs": None,
    }
    assert es_fake.calls == []


def test_cursor_is_ignored(es_fake, es_page, fake_provider):
    # Ledger quirk pinned as-is (tests/pinned-bugs.md): the cursor argument
    # is accepted but ignored.
    providers = [fake_provider.machine_name]

    es_fake.push(es_page([{}] * 3, total=3))
    without_cursor = _folder().scan(number=0, count_only=True, providers=providers)
    calls_without_cursor = list(es_fake.calls)
    es_fake.reset()

    es_fake.push(es_page([{}] * 3, total=3))
    with_cursor = _folder().scan(
        cursor="anything", number=0, count_only=True, providers=providers
    )

    assert with_cursor == without_cursor
    assert es_fake.calls == calls_without_cursor


# --------------------------------------------------------------------------
# Story 3.2 (AD-12): the four stderr tracebacks became attributed ERRORs
# --------------------------------------------------------------------------
#
# `models/folder.py` carried four `traceback.print_exc()` calls that wrote
# raw, unattributed tracebacks to stderr — from worker THREADS, under the
# pool. Each became `log.error(..., exc_info=True)` keeping its existing
# path-prefixed message; these pin all four: caplog sees exactly one
# ERROR record, carrying exc_info, where a stderr traceback used to go.

FOLDER_LOGGER = "portal.plugins.TapelessIngest.models.folder"


def _folder_error_records(caplog):
    return [
        record
        for record in caplog.records
        if record.name == FOLDER_LOGGER and record.levelno >= logging.ERROR
    ]


def _hit(path):
    return {
        "path": path,
        "hash": f"hash-{path}",
        "storage": STORAGE_ID,
        "id": f"{STORAGE_ID}-{path}",
        "size": 1024,
    }


def test_a_per_file_scan_error_is_an_error_record_with_traceback(
    es_fake, es_page, fake_provider, caplog
):
    """Pass-1 site: the file behind an index row fails verification."""
    ghost = f"{PATH}/GHOST.fake"
    es_fake.push(es_page([_hit(ghost)], total=1))

    with caplog.at_level(logging.ERROR, logger=FOLDER_LOGGER):
        response = _folder().scan(number=0, providers=[fake_provider.machine_name])

    errors = _folder_error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].getMessage().startswith(f"Error scanning file {ghost}: ")
    # The response channel is unchanged: the same message still travels.
    assert any(
        entry.startswith(f"Error scanning file {ghost}: ")
        for entry in response["errors"]
    )


def test_a_clip_assembly_error_is_an_error_record_with_traceback(
    tmp_path, es_fake, es_page, fake_provider, caplog, monkeypatch
):
    """Pass-2 site: a verified, extracted file whose clip assembly dies."""
    relative = f"{PATH}/CLIPA.fake"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"media")
    es_fake.push(es_page([_hit(relative)], total=1))
    # Tier 1 stays DB-free: the between-passes umid lookup answers empty.
    monkeypatch.setattr(Clip.objects, "in_bulk", lambda umids: {})

    def exploding_defaults(file, metadatas):
        raise RuntimeError("assembly exploded")

    monkeypatch.setattr(Clip, "new_clip_defaults", exploding_defaults)

    with caplog.at_level(logging.ERROR, logger=FOLDER_LOGGER):
        response = _folder(root=str(tmp_path)).scan(
            number=0, providers=[fake_provider.machine_name]
        )

    errors = _folder_error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].getMessage() == (
        f"Error scanning file {relative}: assembly exploded"
    )
    assert f"Error scanning file {relative}: assembly exploded" in response["errors"]


def test_a_persistence_failure_is_an_error_record_with_traceback(
    es_fake, es_page, fake_provider, caplog, monkeypatch
):
    """Write-unit site: the rolled-back transaction (DatabaseError)."""
    from django.db import DatabaseError

    from portal.plugins.TapelessIngest.models import folder as folder_module

    es_fake.push(es_page([], total=0))

    def exploding_persist(folder, plan, dry_run=False):
        raise DatabaseError("deadlock detected")

    monkeypatch.setattr(folder_module, "persist_scan_results", exploding_persist)

    with caplog.at_level(logging.ERROR, logger=FOLDER_LOGGER):
        response = _folder().scan(number=0, providers=[fake_provider.machine_name])

    errors = _folder_error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].getMessage() == (
        f"Error persisting scan results for {PATH}: deadlock detected"
    )
    assert (
        f"Error persisting scan results for {PATH}: deadlock detected"
        in response["errors"]
    )


def test_a_consumed_subdirs_failure_is_an_error_record_with_traceback(
    es_fake, es_page, fake_provider, caplog, monkeypatch
):
    """Descent site: an unexpected raise is DOUBT — and now also logged."""
    from portal.plugins.TapelessIngest.models import folder as folder_module

    es_fake.push(es_page([], total=0))

    def exploding_consumed(clips, matched_providers, folder_path, reasons=None):
        raise RuntimeError("layout probe exploded")

    monkeypatch.setattr(folder_module, "consumed_subdirs", exploding_consumed)

    with caplog.at_level(logging.ERROR, logger=FOLDER_LOGGER):
        response = _folder().scan(number=0, providers=[fake_provider.machine_name])

    errors = _folder_error_records(caplog)
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert errors[0].getMessage() == (
        f"Cannot compute consumed subdirs for {PATH}: layout probe exploded"
    )
    assert response["consumed_subdirs"] is None
    assert (
        f"Cannot compute consumed subdirs for {PATH}: layout probe exploded"
        in response["errors"]
    )

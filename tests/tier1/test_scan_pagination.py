"""Tier 1 pins for Folder.scan: count_only pagination, error paths, quirks.

Unsaved Folder, count_only variants and error paths only — any non-count scan
hits the ORM (Clip.get_or_new / folder.save) and lives in Tier 2. Every scan
passes providers=[fake] so the FakeProvider registered in _PROVIDER_CACHE is
used (providers=None would fall back to the real PROVIDERS_LIST → ffprobe).
"""

import pytest

from portal.plugins.TapelessIngest.models.folder import Folder

STORAGE_ID = "VX-41"
PATH = "2026/AH_20260101_scanpin"


def _folder(root="/golden-root"):
    folder = Folder(storage_id=STORAGE_ID, path=PATH)
    # Presetting _root_path bypasses StorageHelper/cache entirely.
    folder._root_path = root
    return folder


def test_count_multi_page_call_sequence(es_fake, es_page, fake_provider):
    es_fake.push(es_page([{}] * 100, total=137))
    es_fake.push(es_page([{}] * 37, total=137))

    response = _folder().scan(
        number=0, count_only=True, providers=[fake_provider.machine_name]
    )

    # number=0 → page size 100, loops while a page is full, incrementing first.
    assert es_fake.calls == [(0, 100), (100, 100)]
    assert response == {
        "clips": [],
        "hits": 137,
        "errors": [],
        "created": 0,
        "already_ingested": 0,
        "processed": 0,
    }


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
    }


def test_storage_none_raises_attributeerror(es_fake, fake_provider):
    # Ledger bug pinned as-is (do not fix): with storage=None and no preset
    # _root_path, root_path raises AttributeError instead of filling errors.
    folder = Folder(storage_id=None, path=PATH)

    with pytest.raises(AttributeError):
        folder.scan(number=0, count_only=True, providers=[fake_provider.machine_name])

    assert es_fake.calls == []


def test_cursor_is_ignored(es_fake, es_page, fake_provider):
    # Ledger quirk pinned as-is: the cursor argument is accepted but ignored.
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

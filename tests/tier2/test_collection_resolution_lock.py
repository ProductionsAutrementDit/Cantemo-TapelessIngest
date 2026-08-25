"""Tier 2 (story 3.1 review): the collection-resolution lock, EXECUTED.

Every scan-path test seams out ``Folder.getCollection`` (it needs a
Settings row and a live search backend), so the lock story 3.1 shipped in
``helpers.get_collection_from_path`` was never executed by any test —
removing it left the whole suite green. These tests drive the real
function, from real threads, against the portal_stub surfaces: the
routable ``client.put`` search fake and the recording
``CollectionHelperFake``.

The mutation each test kills:

* remove the lock → two barrier-released threads are mid-resolution at
  once and every path segment is created TWICE (the duplicate Vidispine
  collection the lock exists to prevent);
* remove the pre-lock cache probe → the warm-cache call blocks on the
  held lock and times out;
* remove the acquire timeout → the wedged-resolver test hangs instead of
  failing the folder honestly.
"""

import threading
import time

import pytest

from portal.plugins.TapelessIngest import helpers as helpers_module
from portal.plugins.TapelessIngest.helpers import (
    TapelessIngestException,
    TapelessIngestHelper,
)
from portal.plugins.TapelessIngest.models.settings import Settings

from tests.portal_stub import (
    ClientFake,
    ClientResponse,
    CollectionHelperFake,
    VidispineFake,
)


@pytest.fixture
def collection_settings(migrated_db):
    """The Settings row pk=1 the resolver reads its filter rules from."""
    settings = Settings.objects.create(pk=1, collections_ignore_folder_str="ZZNEVER")
    yield settings
    settings.delete()


@pytest.fixture
def empty_search():
    """Every v2 collection search answers `no hits` -> the create branch."""

    def respond(url, user=None, params=None, json=None):
        return ClientResponse(200, {"hits": 0, "results": []})

    ClientFake.route_put(respond)


def _created_names():
    return sorted(
        details["collection_name"]
        for name, details in VidispineFake.calls
        if name == "createCollection"
    )


def test_two_racing_workers_create_each_collection_once(
    collection_settings, empty_search, monkeypatch
):
    """Both miss the cache; the lock makes the second worker FIND, not create.

    A barrier releases both threads together and the stub's
    ``createCollection`` is slowed, so WITHOUT the lock both threads are
    guaranteed to be mid-resolution at once: the search responder answers
    "no hits" to both, both take the create branch, and every segment is
    created twice — which is exactly what this asserts cannot happen.
    With the lock, the second worker queues, then finds the first
    worker's freshly cached id under the lock.
    """
    real_create = CollectionHelperFake.createCollection

    def slow_create(self, collection_name=None, settingsprofile_id=None):
        time.sleep(0.05)
        return real_create(
            self,
            collection_name=collection_name,
            settingsprofile_id=settingsprofile_id,
        )

    monkeypatch.setattr(CollectionHelperFake, "createCollection", slow_create)

    barrier = threading.Barrier(2, timeout=5)
    results, errors = [], []

    def resolve():
        try:
            barrier.wait()
            results.append(
                TapelessIngestHelper.get_collection_from_path("LOCKY/SUB", None)
            )
        except Exception as e:  # pragma: no cover - failure reporting only
            errors.append(e)

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == []
    assert len(results) == 2
    # Both callers resolved the SAME collection...
    assert results[0] == results[1] == "VX-COLLECTION-SUB"
    # ...and each path segment was created exactly once, ever.
    assert _created_names() == ["LOCKY", "SUB"]


def test_a_warm_cache_answers_without_touching_the_lock(
    collection_settings, empty_search, monkeypatch
):
    """The double-checked fast path: a cache hit never queues on the lock.

    The lock is HELD for the whole second call and the timeout is tiny —
    if the fast path were gone, the call would block on the lock and
    raise the timeout exception instead of answering from the cache.
    """
    first = TapelessIngestHelper.get_collection_from_path("LOCKY/WARM", None)
    assert first == "VX-COLLECTION-WARM"

    monkeypatch.setattr(helpers_module, "COLLECTION_RESOLUTION_LOCK_TIMEOUT", 0.05)
    assert helpers_module._collection_resolution_lock.acquire(timeout=1)
    try:
        assert (
            TapelessIngestHelper.get_collection_from_path("LOCKY/WARM", None) == first
        )
    finally:
        helpers_module._collection_resolution_lock.release()


def test_a_wedged_resolver_times_out_instead_of_blocking_forever(
    collection_settings, empty_search, monkeypatch
):
    """Bounded acquire: a hung Vidispine call fails THIS folder, not the run.

    The exception surfaces at the folder boundary (``process_folder``
    catches it), so one wedged resolver costs one folder per timeout
    window instead of silently parking every worker in the pool.
    """
    monkeypatch.setattr(helpers_module, "COLLECTION_RESOLUTION_LOCK_TIMEOUT", 0.05)
    assert helpers_module._collection_resolution_lock.acquire(timeout=1)
    try:
        with pytest.raises(TapelessIngestException, match="timed out"):
            TapelessIngestHelper.get_collection_from_path("LOCKY/COLD", None)
    finally:
        helpers_module._collection_resolution_lock.release()
    # Nothing was created while the lock was wedged.
    assert _created_names() == []

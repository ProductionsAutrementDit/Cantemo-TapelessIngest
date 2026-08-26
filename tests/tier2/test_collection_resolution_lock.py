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
  failing the folder honestly;
* restore the non-200 ``break`` + unconditional full-path cache → the
  mid-path failure test returns the ancestor id instead of raising, with
  the poisoned entry cached under the full-path key;
* poison the SUBPATH key before the status check → the healthy retry
  silently reuses the stale ancestor and the created-names assert
  reddens;
* remove the lock-timeout cache re-probe, read it without the miss
  sentinel, or drop the bounded ``timeout=`` from the acquire → the
  timed-out-waiter pins go red instead of serving the holder's cached
  result.
"""

import logging
import threading
import time
import urllib.parse

import pytest
from django.core.cache import cache

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


def _full_path_key(filtered_path):
    """The full-path cache key exactly as the resolver computes it."""
    return urllib.parse.quote(f"tapelessingest_path_collection_{filtered_path}")


class _HolderFinishedDuringWait:
    """Lock double for the timed-out-waiter pins.

    Deterministically models the race the 300s window makes near-certain
    in prod: the bounded acquire comes back refused, but by then the
    holder has already cached its result for the same path. It also pins
    the bounded-acquire contract on the way through — a regression to an
    unbounded ``acquire()`` fails the timeout assert.
    """

    def __init__(self, key, value):
        self.key = key
        self.value = value

    def acquire(self, timeout=None):
        assert timeout == helpers_module.COLLECTION_RESOLUTION_LOCK_TIMEOUT
        # By the time the waiter gives up, the holder has cached it.
        cache.set(self.key, self.value, 60)
        return False

    def release(self):  # pragma: no cover - nothing acquired
        raise AssertionError("nothing was acquired, nothing to release")


def test_a_non_200_mid_path_raises_and_caches_nothing(collection_settings):
    """A failed segment search fails the FOLDER, never resolves an ancestor.

    The mutations this kills: restore the old ``break`` + unconditional
    full-path ``cache.set`` and the call returns the last resolved
    ancestor's id instead of raising, with the poisoned entry cached
    under the full-path key for 60s; poison the SUBPATH key before the
    status check and the healthy retry reuses the stale ancestor instead
    of creating BROKEN — the created-names assert reddens.
    """

    def respond(url, user=None, params=None, json=None):
        title = next(
            term["value"] for term in json["filter"]["terms"] if term["name"] == "title"
        )
        if title == "BROKEN":
            return ClientResponse(503, {"detail": "search backend down"})
        return ClientResponse(200, {"hits": 0, "results": []})

    ClientFake.route_put(respond)

    # The message carries segment, status AND the backend's reason — the
    # cron-side triage record must say more than "non-200".
    with pytest.raises(
        TapelessIngestException, match=r"BROKEN.*503.*search backend down"
    ):
        TapelessIngestHelper.get_collection_from_path("LOCKY/BROKEN/LEAF", None)

    # The full-path key holds NOTHING: no 60s window in which every clip
    # on this path ingests into the ancestor collection.
    assert cache.get(_full_path_key("LOCKY/BROKEN/LEAF")) is None
    # The walk stopped at the failure: only the segment before it exists.
    assert _created_names() == ["LOCKY"]

    # The next resolver starts fresh — with the search healthy again the
    # same path resolves completely instead of serving a poisoned entry.
    def healthy(url, user=None, params=None, json=None):
        return ClientResponse(200, {"hits": 0, "results": []})

    ClientFake.route_put(healthy)
    assert (
        TapelessIngestHelper.get_collection_from_path("LOCKY/BROKEN/LEAF", None)
        == "VX-COLLECTION-LEAF"
    )
    # And nothing poisoned the SUBPATH keys either: the retry had to
    # create BROKEN and LEAF for real (sorted; LOCKY is the first call's).
    assert _created_names() == ["BROKEN", "LEAF", "LOCKY"]


def test_a_timed_out_waiter_reprobes_the_cache_before_raising(
    collection_settings, monkeypatch, caplog
):
    """Lock-timeout with the holder finished: the waiter serves the cache.

    No search responder is installed, so any attempt to actually resolve
    would blow up on the AD-11 stub — the answer can only have come from
    the re-probe. The path spent the full bounded acquire blocked behind
    the holder, so the resolver must say so: the WARNING names the path
    and the timeout.

    The mutation this kills: remove the re-probe and the call raises the
    timeout exception — red.
    """
    monkeypatch.setattr(
        helpers_module,
        "_collection_resolution_lock",
        _HolderFinishedDuringWait(_full_path_key("LOCKY/HELD"), "VX-COLLECTION-HELD"),
    )

    with caplog.at_level(logging.WARNING, logger=helpers_module.log.name):
        assert (
            TapelessIngestHelper.get_collection_from_path("LOCKY/HELD", None)
            == "VX-COLLECTION-HELD"
        )

    [record] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = record.getMessage()
    assert "LOCKY/HELD" in message
    assert str(helpers_module.COLLECTION_RESOLUTION_LOCK_TIMEOUT) in message


def test_a_timed_out_waiter_serves_a_cached_none_resolution(
    collection_settings, monkeypatch
):
    """A cached ``None`` is a completed resolution, not a miss.

    A fully ignore-filtered path legitimately resolves to ``None`` and
    the holder caches exactly that under the full-path key. The re-probe
    must read it as a HIT — the sentinel default is what tells a cached
    None apart from an absent key.

    The mutation this kills: revert the sentinel to a bare
    ``cache.get(cache_key)`` and the cached None reads as a miss — the
    call raises the timeout exception despite a completed resolution —
    red.
    """
    monkeypatch.setattr(
        helpers_module,
        "_collection_resolution_lock",
        _HolderFinishedDuringWait(_full_path_key("LOCKY/NONE"), None),
    )

    assert TapelessIngestHelper.get_collection_from_path("LOCKY/NONE", None) is None

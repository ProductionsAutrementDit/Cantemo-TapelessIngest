"""Test bootstrap: stub injection, then a single session-wide django.setup().

Ordering is load-bearing (AD-11):
1. repo root onto sys.path (makes `tests.*` importable as a namespace package),
2. portal_stub.install() BEFORE any django/plugin import,
3. DJANGO_SETTINGS_MODULE set here — never required from the operator,
4. django.setup() once for the whole session (management commands import
   django.contrib.auth.models at module level, so even Tier 1 needs it;
   the tier split is DB usage, not Django presence).

There is deliberately no tests/__init__.py: with one present, pytest resolves
the package upward through the repo root's own __init__.py (which imports
portal.pluginbase.core) and crashes at collection before this file runs.
"""

import logging
import os
import sys
import threading
import warnings
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.portal_stub import install  # noqa: E402

install()

_prior_settings = os.environ.get("DJANGO_SETTINGS_MODULE")
if _prior_settings not in (None, "tests.tier2_settings"):
    warnings.warn(
        f"DJANGO_SETTINGS_MODULE was already set to {_prior_settings!r}; "
        f"overriding with 'tests.tier2_settings' for the off-server test run",
        RuntimeWarning,
        stacklevel=1,
    )
os.environ["DJANGO_SETTINGS_MODULE"] = "tests.tier2_settings"

import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.core.management import call_command  # noqa: E402

from tests.portal_stub import (  # noqa: E402
    ClientFake,
    RestTransportFake,
    StorageHelperFake,
    VidispineFake,
    invalidate_item_cache_fake,
    query_elastic_fake,
    vidispine_post_ingest,
    vidispine_pre_ingest,
)


@pytest.fixture(scope="session")
def migrated_db():
    """Apply all migrations (0001–0016 + django deps) to sqlite :memory:."""
    call_command("migrate", verbosity=0)
    yield


@pytest.fixture(autouse=True)
def _flush_db_after_db_test(request):
    """Tier 2 isolation: migrated_db is session-scoped over one sqlite :memory:
    connection, so rows written by one test would otherwise leak into the next.
    Flush on teardown, but only for tests that actually requested the DB —
    Tier 1 must stay DB-free.
    """
    yield
    if "migrated_db" in request.fixturenames:
        call_command("flush", interactive=False, verbosity=0)


FAKE_PROVIDER_NAME = "faketest"


class FakeProvider:
    """Deterministic provider double, injected via the Clip._PROVIDER_CACHE seam.

    Real providers are unusable off-server (`file` shells out to ffprobe, the
    others parse card structures). Like real providers under AD-7 (story
    2.3), getMetadatasFromFile mutates the shared `metadatas` dict in place
    and returns THE METADATAS ONLY — `context` stays an argument and stays
    mutable in place. The umid is derived deterministically from the full
    storage path (extension stripped) so same-named files in different
    directories never collide.

    Every getMetadatasFromFile call records `context.get("scan_context")`
    in `seen_scan_contexts` and `context.get("listings")` in
    `seen_listings`, so tests can assert the provider dict's
    "scan_context" (story 2.1: providers resolve absolute paths through
    it) and "listings" (story 2.2: the scan's FolderListings instance,
    the 2.3 sidecar-probe reuse surface) keys are load-bearing.

    Story 3.1: the `seen_*` recorders are appended from POOL WORKER
    threads whenever a test runs the tree with `workers > 1`, so the two
    appends are taken under a lock — the pair recorded by one call can
    never interleave with another thread's.
    """

    name = "Fake Test Provider"
    machine_name = FAKE_PROVIDER_NAME

    def __init__(self):
        self.seen_scan_contexts = []
        self.seen_listings = []
        self._seen_lock = threading.Lock()

    def getExtensions(self):
        return [".fake"]

    def getSegmentedExtensions(self):
        # No grouping by default — a `.fake` file is a clip of its own.
        # Tests that need red's multi-segment shape monkeypatch this on
        # the instance, the way they already do for getSubPaths/getFilters.
        return []

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        with self._seen_lock:
            self.seen_scan_contexts.append(context.get("scan_context"))
            self.seen_listings.append(context.get("listings"))
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas

    # Story 3.2 (retro F2): the minimum surface `Clip.import_file` reads,
    # so a tree run at `dry_run=False` can drive the REAL import against
    # the portal_stub Vidispine doubles instead of failing every clip at
    # `_createDictFromMetadataMapping`. Before this, the "writing" tree
    # runs were an equivalence of three per-clip AttributeErrors — no
    # placeholder was ever created, so nothing pooled could be proven
    # about NFR-1. Modelled on test_ingest_discipline's IngestableProvider.

    def _createDictFromMetadataMapping(self, clip):
        # The base provider's version maps clip metadatas through the
        # MetadataMapping table; the tier-2 DB holds no mapping rows, so
        # the real thing would also answer empty HERE. {} is faithful for
        # exactly that reason (and keeps the fake DB-free for Tier 1) —
        # it would stop being faithful if a test ever seeded mappings.
        return {}

    def getClipMainMediaFile(self, clip):
        # A file-less clip must not silently proceed through the fake:
        # the real import needs a Vidispine file id, and a None here
        # would only surface later as an unrelated-looking failure.
        assert clip.file_id, f"clip {clip.umid} reached import with no file_id"
        return {"file_id": clip.file_id, "path": clip.path, "type": "video"}

    def getClipAdditionalMediaFiles(self, clip):
        return []

    def getImportOptions(self):
        return {}


@pytest.fixture(autouse=True)
def _reset_query_elastic_fake():
    """Autouse: unconsumed queued responses must never leak into a later test.

    Over-pushed pages are a test bug, not noise — fail loudly instead of
    silently discarding them.
    """
    yield
    leftover = list(query_elastic_fake.queue)
    query_elastic_fake.reset()
    assert not leftover, (
        f"query_elastic fake queue not fully consumed at teardown: "
        f"{len(leftover)} unused page(s) — the test pushed more responses "
        f"than scan requested"
    )


@pytest.fixture(autouse=True)
def _reset_client_fake():
    """Autouse: the portal.api.client put-responder is class state.

    Without this, one test's routed search responder would keep answering
    for every later test, silently defeating the refuse-by-default AD-11
    placeholder behavior.
    """
    yield
    ClientFake.reset()


@pytest.fixture(autouse=True)
def _reset_storage_helper_fake():
    """Autouse: per-test roots and getStorage counters never leak across tests."""
    yield
    StorageHelperFake.reset()


@pytest.fixture(autouse=True)
def _clear_django_cache():
    """Autouse, and it belongs beside ``_reset_vidispine_fakes``.

    ``Clip.item`` memoizes the fetched item in Django's cache under
    ``item:{item_id}`` for 180 s, and the Tier 2 ``LocMemCache`` is
    process-global — while ``VidispineFake._placeholder_counter`` resets
    per test, so EVERY test's first placeholder is ``VX-PLACEHOLDER-1``.
    Without this, one test's ``FakeItem`` answers another test's
    ``Clip.item`` lookup and decides the outcome of a crash-recovery
    assertion.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _reset_vidispine_fakes():
    """Autouse: the ingest-side doubles are module-level singletons.

    Their queues and call logs are class state, so without this one test
    could observe another's calls (or consume its queued response) —
    exactly the cross-test leak the query_elastic fixture prevents.
    """
    yield
    # An armed fault whose call name never fired (misspelled window, or a
    # code path that stopped making the call) would otherwise turn a
    # crash test into a vacuous happy-path pass. Same for a queued import
    # response nothing consumed: the import the test scripted never ran.
    unfired = list(VidispineFake.faults)
    leftover_imports = list(VidispineFake.import_responses)
    VidispineFake.reset()
    RestTransportFake.reset()
    assert not unfired, f"armed Vidispine fault(s) never fired: {unfired}"
    assert not leftover_imports, (
        f"queued import response(s) never consumed: {leftover_imports} — "
        f"the test scripted an import that never happened"
    )
    vidispine_pre_ingest.calls.clear()
    vidispine_post_ingest.calls.clear()
    invalidate_item_cache_fake.calls.clear()


@pytest.fixture
def storage_fake():
    """The counting StorageHelper fake (class-level state; see portal_stub).

    Tests configure per-test roots via ``storage_fake.set_root(id, root)``
    and read ``storage_fake.get_storage_calls`` for the once-per-run AC;
    the autouse reset fixture clears the state after every test.
    """
    return StorageHelperFake


@pytest.fixture
def es_fake():
    """The stateful query_elastic fake bound into models/folder.py at import."""
    return query_elastic_fake


@pytest.fixture
def es_page():
    """Factory for raw query_elastic result pages.

    `sort=True` (story 4.1) attaches the per-hit `sort` values a real
    response carries when the search doc sorts — `[path, id]`, the index
    path's own tuple — so a `search_after` stream can be scripted. It
    defaults to False, which is the pre-4.1 shape byte for byte: the
    legacy path's query is unsorted and its pins compare whole hits.
    """

    def _page(sources, total, *, sort=False):
        hits = []
        for source in sources:
            hit = {"_source": source}
            if sort:
                hit["sort"] = [source.get("path"), source.get("id")]
            hits.append(hit)
        return {"hits": {"total": {"value": total}, "hits": hits}}

    return _page


@pytest.fixture
def fake_provider():
    """Register the FakeProvider under its test-only cache name, then unregister."""
    from portal.plugins.TapelessIngest.models.clip import Clip

    provider = FakeProvider()
    Clip._PROVIDER_CACHE[FAKE_PROVIDER_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(FAKE_PROVIDER_NAME, None)

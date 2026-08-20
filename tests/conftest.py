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
from django.core.management import call_command  # noqa: E402

from tests.portal_stub import StorageHelperFake, query_elastic_fake  # noqa: E402


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
    others parse card structures). Like real providers, getMetadatasFromFile
    mutates the shared `metadatas` dict in place and returns it with the
    context; the umid is derived deterministically from the full storage path
    (extension stripped) so same-named files in different directories never
    collide.

    Every getMetadatasFromFile call records `context.get("scan_context")`
    in `seen_scan_contexts` and `context.get("listings")` in
    `seen_listings`, so tests can assert the provider dict's
    "scan_context" (story 2.1: providers resolve absolute paths through
    it) and "listings" (story 2.2: the scan's FolderListings instance,
    the 2.3 sidecar-probe reuse surface) keys are load-bearing.
    """

    name = "Fake Test Provider"
    machine_name = FAKE_PROVIDER_NAME

    def __init__(self):
        self.seen_scan_contexts = []
        self.seen_listings = []

    def getExtensions(self):
        return [".fake"]

    def getSubPaths(self):
        return []

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        self.seen_scan_contexts.append(context.get("scan_context"))
        self.seen_listings.append(context.get("listings"))
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas, context


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
def _reset_storage_helper_fake():
    """Autouse: per-test roots and getStorage counters never leak across tests."""
    yield
    StorageHelperFake.reset()


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
    """Factory for raw query_elastic result pages."""

    def _page(sources, total):
        return {
            "hits": {
                "total": {"value": total},
                "hits": [{"_source": source} for source in sources],
            }
        }

    return _page


@pytest.fixture
def fake_provider():
    """Register the FakeProvider under its test-only cache name, then unregister."""
    from portal.plugins.TapelessIngest.models.clip import Clip

    provider = FakeProvider()
    Clip._PROVIDER_CACHE[FAKE_PROVIDER_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(FAKE_PROVIDER_NAME, None)

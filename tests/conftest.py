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
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.portal_stub import install  # noqa: E402

install()

os.environ["DJANGO_SETTINGS_MODULE"] = "tests.tier2_settings"

import django  # noqa: E402

django.setup()

import pytest  # noqa: E402
from django.core.management import call_command  # noqa: E402


@pytest.fixture(scope="session")
def migrated_db():
    """Apply all migrations (0001–0016 + django deps) to sqlite :memory:."""
    call_command("migrate", verbosity=0)
    yield

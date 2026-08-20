"""Minimal Django settings for Tier 2 (sqlite :memory:) tests.

Selected by tests/conftest.py via DJANGO_SETTINGS_MODULE — never required
from the operator. Dev-only, never deployed.
"""

SECRET_KEY = "tapeless-ingest-tests-only-not-a-real-secret"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "portal.plugins.TapelessIngest",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Matches the Django-1.11-era migrations (implicit AutoField pks).
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

USE_TZ = True

# Required: models/folder.py imports django.core.cache at module level and
# caches storage/collection lookups.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

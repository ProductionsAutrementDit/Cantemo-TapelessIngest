"""Minimal Django settings for Tier 2 (shared in-memory sqlite) tests.

Selected by tests/conftest.py via DJANGO_SETTINGS_MODULE — never required
from the operator. Dev-only, never deployed. The database is a
process-shared in-memory sqlite (memdb VFS — see the DATABASES comment),
so story 3.1's pool workers see the same DB from every thread.
"""

SECRET_KEY = "tapeless-ingest-tests-only-not-a-real-secret"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "portal.plugins.TapelessIngest",
]

# Shared in-memory sqlite (story 3.1): the worker pool gives every thread
# its own Django connection, and a plain ":memory:" DB is private to the
# connection that opened it — worker threads would see an empty,
# UN-migrated database. The URI form keeps ONE in-memory DB shared by
# every connection in the process (Django always connects with
# ``uri=True, check_same_thread=False``). The DB lives as long as at
# least one connection holds it open: the session-long main-thread
# connection (opened by the ``migrated_db`` migrate) keeps it alive
# across worker ``close_all()``.
#
# ``vfs=memdb`` rather than the spec's ``mode=memory&cache=shared``,
# VERIFIED 2026-08-25 (sqlite 3.53.1, threading probe): in shared-CACHE
# mode a concurrent writer fails with SQLITE_LOCKED ("database table is
# locked") IMMEDIATELY — table locks are exempt from the busy handler, so
# the ``timeout`` below cannot serialize writers there and the story's
# "no busy error" contract is unsatisfiable. The memdb VFS (SQLite's
# replacement for shared cache) shares the same in-memory DB with normal
# file-style locking, where ``timeout`` (busy_timeout) really does make a
# concurrent worker writer WAIT on the write lock. ``transaction_mode:
# IMMEDIATE`` makes ``transaction.atomic`` take the write lock at BEGIN,
# so a read-then-write upgrade inside a deferred transaction cannot hit
# the non-waiting SQLITE_BUSY deadlock verdict. ``transaction_mode`` is a
# Django >= 5.1 OPTION (5.2.10 in requirements-dev.txt); an older Django
# would reject it in get_connection_params.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "file:/tapeless-tier2?vfs=memdb",
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
    }
}

# Matches the Django-1.11-era migrations (implicit AutoField pks).
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

USE_TZ = True

# Prod shape VERIFIED 2026-08-20: a dict of URL-prefix rewrites passed through
# to VSFile; empty off-server (the stub VSFile only stores it).
VIDISPINE_REPLACE_URLS = {}

# Required: models/folder.py imports django.core.cache at module level and
# caches storage/collection lookups.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

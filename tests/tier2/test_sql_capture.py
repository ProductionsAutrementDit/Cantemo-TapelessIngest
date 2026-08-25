"""Tier 2 (story 3.1 review): the shared SQL capture under reconnects.

``connection_created`` fires on every RECONNECT, and a connection's
``execute_wrappers`` list survives ``close()`` — so a pool worker that
closes its connection per folder (the AD-5 hygiene wrapper) would hand
the capture a duplicate recorder per reconnect, and every later statement
would be counted N times. ``wrap_every_connection`` installs only when
its recorder is not already on that connection; this pins it.

The reconnect is SIMULATED by re-sending the signal for the already
wrapped main-thread connection rather than really closing it: the tier-2
DB is an in-memory memdb kept alive by exactly that session-long
connection, and closing it would drop every table for the rest of the
session.
"""

from django.db import connection
from django.db.backends.signals import connection_created

from portal.plugins.TapelessIngest.models.folder import Folder

from tests.sql_capture import captured_sql


def test_a_reconnect_does_not_duplicate_the_recorder(migrated_db):
    with captured_sql() as statements:
        # The reconnect event for a connection this capture already
        # wraps — what a pooled worker produces once per folder.
        connection_created.send(sender=type(connection), connection=connection)
        Folder.objects.count()

    counts = [s for s in statements if "COUNT" in s.upper()]
    assert len(counts) == 1, statements


def test_nested_captures_still_record_independently(migrated_db):
    """The guard is per-recorder: two captures both see the statement."""
    with captured_sql() as outer:
        with captured_sql() as inner:
            Folder.objects.count()

    assert [s for s in outer if "COUNT" in s.upper()]
    assert [s for s in inner if "COUNT" in s.upper()]

"""SQL capture that sees EVERY thread's connection (story 3.1, D24).

``connection.execute_wrapper`` registers on the calling thread's
connection only — ``django.db.connection`` is a per-thread proxy. That
was fine while the scan was sequential; under the story-3.1 worker pool
every worker's writes happen on a connection of its own, so a recorder
installed the old way silently watches NOTHING a worker does, and a
purity assertion over it proves nothing at all (demonstrated by the
final-round Epic 2 verification-gap reviewer with a threading probe).

The fix is the ``connection_created`` signal: the wrapper is installed on
every connection already open in the calling thread AND on every
connection any thread creates while the capture is active. The three
former thread-local copies (``test_dry_run_purity``,
``test_check_clips_in_folder``, ``test_epic2_review_pins``) all route
through here now.

This lives in ``tests/``, not ``tests/portal_stub`` — it fakes nothing;
it is shared test instrumentation over the real Django connection.
"""

import threading
from contextlib import contextmanager

from django.db import connections
from django.db.backends.signals import connection_created


@contextmanager
def wrap_every_connection(recorder):
    """Install ``recorder`` as an execute_wrapper on all connections.

    Covers the calling thread's already-open connections immediately, and
    every connection created ANYWHERE in the process while the context is
    active (worker threads reconnecting after ``close_all()`` included),
    via the ``connection_created`` signal.

    The wrappers are removed on exit for the connections that persist
    (the session-long main-thread one); a worker thread's connection dies
    with its thread either way.
    """
    lock = threading.Lock()
    entered = []

    def install(conn):
        if recorder in conn.execute_wrappers:
            # `connection_created` fires on every RECONNECT, and
            # `execute_wrappers` survives close(): a pooled worker that
            # closes its connection per folder (AD-5 hygiene) would
            # otherwise stack one duplicate recorder per reconnect and
            # record every later statement N times.
            return
        wrapper = conn.execute_wrapper(recorder)
        wrapper.__enter__()
        with lock:
            entered.append(wrapper)

    def on_connection_created(sender, connection, **kwargs):
        install(connection)

    for conn in connections.all(initialized_only=True):
        install(conn)
    # dispatch_uid: entering twice (nested captures) must register twice.
    connection_created.connect(on_connection_created, weak=False)
    try:
        yield
    finally:
        connection_created.disconnect(on_connection_created)
        with lock:
            wrappers, entered[:] = list(entered), []
        for wrapper in reversed(wrappers):
            wrapper.__exit__(None, None, None)


@contextmanager
def captured_sql():
    """Every statement executed on ANY connection while active, in order.

    An execute_wrapper rather than ``CaptureQueriesContext``: the wrapper
    sits under the cursor, so it sees ``bulk_create`` and
    ``queryset.update`` whatever the DEBUG setting and whatever the
    query-log truncation does. ``list.append`` is atomic under the GIL,
    so concurrent workers interleave statements but never lose one.
    """
    statements = []

    def recorder(execute, sql, params, many, context):
        statements.append(sql)
        return execute(sql, params, many, context)

    with wrap_every_connection(recorder):
        yield statements

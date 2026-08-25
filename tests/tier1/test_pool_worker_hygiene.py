"""Tier 1 (story 3.1 review): the pool-side wrapper and the pool's release.

The 3.1 review's mutation pass showed both shipped UNOBSERVED: deleting
the ``close_all()`` call, unhooking the wrapper from ``scan_tree``'s pool
branch, or dropping the shutdown left 633 tests green. These pin each
piece directly — DB-free (the Django surfaces are doubled at the plugin
module's own bindings, and ``walk_tree`` is doubled for the selection
pins, exactly like ``test_dry_run_purity`` does).
"""

import pytest

from portal.plugins.TapelessIngest.models import folder as folder_module
from portal.plugins.TapelessIngest.scan.context import (
    RunOptions,
    ScanContext,
    StorageInfo,
)
from portal.plugins.TapelessIngest.scan.coordinator import PoolDispatcher

# The walk's actual calling convention (number=0 is the loop-all-pages
# sentinel); the wrapper's parameters are mandatory on purpose.
WORKER_KWARGS = dict(first=0, number=0, cursor=None, count_only=False, ingest=True)


class _FakeConnections:
    def __init__(self, fail=False):
        self.closed = 0
        self._fail = fail

    def close_all(self):
        self.closed += 1
        if self._fail:
            raise RuntimeError("close_all exploded")


# --------------------------------------------------------------------------
# The wrapper itself
# --------------------------------------------------------------------------


def test_the_wrapper_closes_the_workers_connections_on_return(monkeypatch):
    fake = _FakeConnections()
    outcome = object()
    monkeypatch.setattr(folder_module, "connections", fake)
    monkeypatch.setattr(folder_module, "process_folder", lambda *a, **k: outcome)

    result = folder_module._process_folder_with_connection_hygiene(
        "VX-41", "2026/A", None, **WORKER_KWARGS
    )

    assert result is outcome
    assert fake.closed == 1


def test_the_wrapper_closes_the_workers_connections_on_raise(monkeypatch):
    """`process_folder` never raises by contract — but hygiene is a finally.

    If the contract is ever violated, the thread must still not carry a
    stale connection into its next folder.
    """
    fake = _FakeConnections()

    def violating_worker(*args, **kwargs):
        raise RuntimeError("contract violation")

    monkeypatch.setattr(folder_module, "connections", fake)
    monkeypatch.setattr(folder_module, "process_folder", violating_worker)

    with pytest.raises(RuntimeError, match="contract violation"):
        folder_module._process_folder_with_connection_hygiene(
            "VX-41", "2026/A", None, **WORKER_KWARGS
        )

    assert fake.closed == 1


def test_a_failing_close_does_not_clobber_the_folders_outcome(monkeypatch, caplog):
    """An exception out of the finally would REPLACE a good outcome.

    A close hiccup after a perfectly scanned folder must reach
    portal.log as a warning, never turn the folder into a walk failure.
    """
    fake = _FakeConnections(fail=True)
    outcome = object()
    monkeypatch.setattr(folder_module, "connections", fake)
    monkeypatch.setattr(folder_module, "process_folder", lambda *a, **k: outcome)

    with caplog.at_level("WARNING", logger=folder_module.log.name):
        result = folder_module._process_folder_with_connection_hygiene(
            "VX-41", "2026/A", None, **WORKER_KWARGS
        )

    assert result is outcome
    assert fake.closed == 1
    assert any("connection close failed" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# scan_tree's selection and release of the pool
# --------------------------------------------------------------------------


def _ctx(workers):
    # A user is set so the dry run's "User has to be provided" warning is
    # never emitted — the emit-raises test below needs the FIRST emitted
    # line to come from inside the walk, after the pool exists.
    return ScanContext(
        storages={"VX-41": StorageInfo(id="VX-41", root_path="/root")},
        options=RunOptions(dry_run=True, workers=workers, user="operator"),
    )


@pytest.fixture
def captured_walk(monkeypatch):
    """Double `walk_tree` at the name `scan_tree` calls; record its seam args."""
    captured = {}

    def fake_walk_tree(
        root_storage_id, root_path, *, ctx, process_folder, dispatch, gather, emit
    ):
        captured["process_folder"] = process_folder
        emit(f"walked {root_path}")
        return []

    monkeypatch.setattr(folder_module, "walk_tree", fake_walk_tree)
    return captured


@pytest.fixture
def shutdown_calls(monkeypatch):
    """Record every PoolDispatcher.shutdown, then really shut down."""
    calls = []
    original = PoolDispatcher.shutdown

    def recording(self, wait=True):
        calls.append(wait)
        original(self, wait=wait)

    monkeypatch.setattr(PoolDispatcher, "shutdown", recording)
    return calls


def test_the_pool_branch_selects_the_hygiene_wrapper(captured_walk, shutdown_calls):
    folder = folder_module.Folder(storage_id="VX-41", path="2026")

    folder.scan_tree(_ctx(4), emit=lambda line: None)
    assert (
        captured_walk["process_folder"]
        is folder_module._process_folder_with_connection_hygiene
    )

    folder.scan_tree(_ctx(1), emit=lambda line: None)
    assert captured_walk["process_folder"] is folder_module.process_folder


def test_the_pool_is_shut_down_on_normal_completion(captured_walk, shutdown_calls):
    folder = folder_module.Folder(storage_id="VX-41", path="2026")

    folder.scan_tree(_ctx(4), emit=lambda line: None)
    assert shutdown_calls == [True]

    # The sequential path never constructs a pool, so never shuts one down.
    folder.scan_tree(_ctx(1), emit=lambda line: None)
    assert shutdown_calls == [True]


def test_a_raising_emit_still_releases_the_pool_without_waiting(
    captured_walk, shutdown_calls
):
    """The dying run's path: shutdown(wait=False), exception preserved.

    A Ctrl-C or a raising emit must not stall behind in-flight folders —
    and the pool must still be released, or its threads outlive the run.
    """
    folder = folder_module.Folder(storage_id="VX-41", path="2026")

    def raising_emit(line):
        raise RuntimeError("emit died")

    with pytest.raises(RuntimeError, match="emit died"):
        folder.scan_tree(_ctx(4), emit=raising_emit)

    assert shutdown_calls == [False]

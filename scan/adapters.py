"""Portal adapters for the scan context (story 2.1).

Sole sanctioned ``portal.*`` import site for new scan code (AD-1). Tree
mode resolves storages here — one bare ``StorageHelper()`` and exactly one
``getStorage`` per unique storage id per run, no Django cache (AD-4). The
Redis ``storage:{id}`` cache (Nov-2025 prod hotfix) is deliberately left
to the paged/UI property chain.
"""

from typing import Dict, Iterable

from portal.vidispine.iexception import NotFoundError
from portal.vidispine.istorage import StorageHelper

from portal.plugins.TapelessIngest.scan.context import (
    RunOptions,
    ScanContext,
    StorageInfo,
    browse_root_path,
)


def resolve_storages(storage_ids: Iterable[str]) -> Dict[str, StorageInfo]:
    """Resolve each unique storage id once via a bare ``StorageHelper``.

    An unresolvable storage (``NotFoundError``) yields
    ``StorageInfo(root_path=None)``; consumers fall back to today's
    property chain, preserving pin #5's semantics.
    """
    storage_helper = StorageHelper()
    storages: Dict[str, StorageInfo] = {}
    for storage_id in storage_ids:
        if storage_id in storages:
            continue
        try:
            storage = storage_helper.getStorage(storage_id)
        except NotFoundError:
            storages[storage_id] = StorageInfo(id=storage_id, root_path=None)
            continue
        storages[storage_id] = StorageInfo(
            id=storage_id,
            root_path=browse_root_path(storage),
            storage=storage,
        )
    return storages


def build_context(
    storage_ids: Iterable[str],
    *,
    user,
    dry_run,
    providers,
    legacy_storages,
    replace,
) -> ScanContext:
    """Build the one per-run context for tree mode (commands' ``handle()``)."""
    return ScanContext(
        storages=resolve_storages(storage_ids),
        options=RunOptions(
            dry_run=dry_run,
            providers=providers,
            legacy_storages=legacy_storages,
            replace=replace,
            user=user,
        ),
    )


def build_default_context(
    folder,
    *,
    user=None,
    dry_run=False,
    providers=None,
    legacy_storages=None,
    replace=False,
) -> ScanContext:
    """Default context for paged callers — root_path-first and lazy.

    Exact read order: the memoized ``folder._root_path`` when present,
    WITHOUT dereferencing ``folder.storage``; only otherwise the
    ``folder.root_path`` property (Redis chain — pin #5's AttributeError
    propagates from here exactly as it does today).
    """
    if hasattr(folder, "_root_path"):
        root_path = folder._root_path
    else:
        root_path = folder.root_path
    return ScanContext(
        storages={
            folder.storage_id: StorageInfo(
                id=folder.storage_id,
                root_path=root_path,
                storage=getattr(folder, "_storage", None),
            )
        },
        options=RunOptions(
            dry_run=dry_run,
            providers=providers,
            legacy_storages=legacy_storages,
            replace=replace,
            user=user,
        ),
    )

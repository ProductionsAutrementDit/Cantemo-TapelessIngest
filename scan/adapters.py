"""Portal adapters for the scan context (story 2.1).

Sole sanctioned ``portal.*`` import site for new scan code (AD-1). Tree
mode resolves storages here — one bare ``StorageHelper()`` and exactly one
``getStorage`` per unique storage id per run, no Django cache (AD-4). The
Redis ``storage:{id}`` cache (Nov-2025 prod hotfix) is deliberately left
to the paged/UI property chain.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional

from portal.vidispine.istorage import StorageHelper

from portal.plugins.TapelessIngest.scan.context import (
    RunOptions,
    ScanContext,
    StorageInfo,
    browse_root_path,
)

log = logging.getLogger(__name__)


def resolve_storages(storage_ids: Iterable[str]) -> Dict[str, StorageInfo]:
    """Resolve each unique storage id once via a bare ``StorageHelper``.

    An unresolvable storage — ``NotFoundError`` or ANY other resolution
    failure — is logged and yields ``StorageInfo(root_path=None)``:
    consumers fall back to today's property chain, so a run degrades to
    the pre-2.1 per-folder "Cannot get full path" errors instead of
    crashing before the scan starts. Pin #5's semantics are preserved.
    """
    storage_helper = StorageHelper()
    storages: Dict[str, StorageInfo] = {}
    for storage_id in storage_ids:
        if storage_id in storages:
            continue
        try:
            storage = storage_helper.getStorage(storage_id)
        except Exception:
            log.error(
                f"resolve_storages: getStorage({storage_id!r}) failed; "
                f"falling back to per-folder resolution",
                exc_info=True,
            )
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
    user: Any,
    dry_run: bool,
    providers: Optional[List[str]],
    legacy_storages: Optional[List[str]],
    replace: bool,
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
    user: Any = None,
    dry_run: bool = False,
    providers: Optional[List[str]] = None,
    legacy_storages: Optional[List[str]] = None,
    replace: bool = False,
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

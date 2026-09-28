"""The read-only P5 lookup the migration needs, and nothing more.

The implementation belongs to the Archiware P5 plugin (a parallel
project); this module only states the contract, caches it for one run,
and loads it by entrypoint so the migration never imports P5 code at
module level.
"""

import importlib
import posixpath
from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Protocol, Tuple

ARCHIVE_ENTRYPOINT = "portal.plugins.ArchiwareP5.lookup:build_lookup"


class ArchiveLookupError(Exception):
    """P5 could not answer. Never means "not archived"."""


@dataclass(frozen=True)
class Entry:
    handle: str
    volumes: Tuple[str, ...]
    btime: int
    size: int


@dataclass(frozen=True)
class Volume:
    volume_id: str
    barcode: str
    label: str


class ArchiveLookup(Protocol):
    def lookup_folder(self, abs_path: str) -> FrozenSet[str]: ...

    def lookup(self, abs_path: str) -> Optional[Entry]: ...

    def volume(self, volume_id: str) -> Volume: ...


class CachedArchive:
    """One inventory call per folder and one call per volume, per run."""

    def __init__(self, lookup: ArchiveLookup):
        self._lookup = lookup
        self._folders: Dict[str, FrozenSet[str]] = {}
        self._volumes: Dict[str, Volume] = {}

    def resolve(self, abs_path: str) -> Optional[Entry]:
        folder, name = posixpath.split(abs_path)
        if folder not in self._folders:
            self._folders[folder] = frozenset(self._lookup.lookup_folder(folder))
        if name not in self._folders[folder]:
            return None
        return self._lookup.lookup(abs_path)

    def volume(self, volume_id: str) -> Volume:
        if volume_id not in self._volumes:
            self._volumes[volume_id] = self._lookup.volume(volume_id)
        return self._volumes[volume_id]


def load_archive_lookup(entrypoint: str = ARCHIVE_ENTRYPOINT) -> ArchiveLookup:
    module_name, _, attribute = entrypoint.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ArchiveLookupError(
            f"the P5 lookup {entrypoint} is not installed: {error}"
        ) from error
    return getattr(module, attribute)()

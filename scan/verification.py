"""Batched filesystem verification (story 2.2).

Stdlib-only by contract (AD-1/AD-9): this module must import with no
Portal stub installed, like ``scan.context``.

One ``os.scandir`` per unique normalized directory per scan invocation
feeds an immutable ``DirectoryListing``; a per-scan-invocation
``FolderListings`` cache — a local of the ``scan()`` call, never stored
in ``ScanContext``, the Django cache, or module globals (AD-4) — answers
``exists``/``is_file``/``is_dir`` queries against those listings.

Symlink semantics (the hybrid presence ruling):
- Listing sets are built with ``follow_symlinks=False``; FR-24's cycle
  guard applies ONLY to the recursion-facing ``dirs`` set (symlinked
  directories are never in ``dirs``).
- The ``exists``/``is_file``/``is_dir`` helpers see through symlinks
  exactly like today's ``os.path`` calls: entries recorded as symlinks
  fall back to one real ``os.path`` call of the matching kind (follows
  the link), so a valid symlink is seen-through and a broken one is
  absent.

Scandir failure: any ``OSError`` (missing dir, unreadable ``0o000``)
yields an empty listing with ``error=str(e)`` recorded — unsurfaced
until story 2.6 (FR-22) — matching ``os.path.exists``'s swallow-to-False
for these modes.

The API is deliberately wider than 2.2 needs: it is the reuse surface
for 2.3 sidecar probes (FR-16, incl. parent-directory probes such as
xdcam's ``../MEDIAPRO.XML``) and 2.6 subfolder discovery (FR-19).
"""

import os
from dataclasses import dataclass
from typing import FrozenSet, Optional


def _normalize(path: str) -> str:
    """Normalized absolute form of ``path`` — the cache/listing key."""
    return os.path.normpath(os.path.abspath(path))


@dataclass(frozen=True)
class DirectoryListing:
    """Immutable snapshot of one directory: raw dirent names, typed sets.

    ``names`` holds every raw dirent name; ``files``/``dirs`` are typed
    with ``follow_symlinks=False`` (a symlinked dir is in ``names`` and
    ``symlinks`` but never in ``dirs`` — FR-24); ``symlinks`` holds every
    symlink dirent, valid or broken. ``error`` records a failed scandir
    (``str`` of the ``OSError``; the listing is then empty).
    """

    path: str
    names: FrozenSet[str]
    files: FrozenSet[str]
    dirs: FrozenSet[str]
    symlinks: FrozenSet[str]
    error: Optional[str] = None


def list_directory(path: str) -> DirectoryListing:
    """List ``path`` with exactly one ``os.scandir`` call.

    Any ``OSError`` (missing directory, permission denied) yields an
    empty listing with the error recorded — never a raise.
    """
    normalized = _normalize(path)
    names, files, dirs, symlinks = set(), set(), set(), set()
    error = None
    try:
        with os.scandir(normalized) as entries:
            for entry in entries:
                names.add(entry.name)
                if entry.is_symlink():
                    symlinks.add(entry.name)
                if entry.is_file(follow_symlinks=False):
                    files.add(entry.name)
                elif entry.is_dir(follow_symlinks=False):
                    dirs.add(entry.name)
    except OSError as e:
        error = str(e)
        names, files, dirs, symlinks = set(), set(), set(), set()
    return DirectoryListing(
        path=normalized,
        names=frozenset(names),
        files=frozenset(files),
        dirs=frozenset(dirs),
        symlinks=frozenset(symlinks),
        error=error,
    )


class FolderListings:
    """Lazy per-scan-invocation cache of ``DirectoryListing`` objects.

    Owned by the scan worker (AD-4): built as a local of one ``scan()``
    call and dropped with it. Keys are normalized via ``os.path.normpath``
    so parent-directory probes (``dir/../MEDIAPRO.XML``) share the same
    listing as direct probes.
    """

    def __init__(self):
        self._listings = {}

    def get(self, directory: str) -> DirectoryListing:
        """The (cached) listing for ``directory`` — at most one scandir."""
        key = _normalize(directory)
        listing = self._listings.get(key)
        if listing is None:
            listing = list_directory(key)
            self._listings[key] = listing
        return listing

    def exists(self, abs_path: str) -> bool:
        """Batched ``os.path.exists``: dirent-name membership, symlinks
        resolved through one real ``os.path.exists`` (hybrid ruling)."""
        return self._query(abs_path, "names", os.path.exists)

    def is_file(self, abs_path: str) -> bool:
        """Batched ``os.path.isfile`` over the ``files`` set."""
        return self._query(abs_path, "files", os.path.isfile)

    def is_dir(self, abs_path: str) -> bool:
        """Batched ``os.path.isdir`` over the ``dirs`` set."""
        return self._query(abs_path, "dirs", os.path.isdir)

    def _query(self, abs_path, set_name, real_check):
        normalized = _normalize(abs_path)
        directory, name = os.path.split(normalized)
        listing = self.get(directory)
        if name in listing.symlinks:
            # Hybrid presence ruling: symlink dirents get one real os.path
            # call of the matching kind (follows the link) — identical to
            # today's per-file checks. Valid symlink: seen-through; broken
            # symlink: absent.
            return real_check(normalized)
        return name in getattr(listing, set_name)

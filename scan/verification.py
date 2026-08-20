"""Batched filesystem verification (story 2.2).

Stdlib-only by contract (AD-1/AD-9): this module must import with no
Portal stub installed, like ``scan.context``.

One ``os.scandir`` per unique normalized directory per scan invocation
feeds an immutable ``DirectoryListing``; a per-scan-invocation
``FolderListings`` cache — a local of the ``scan()`` call, never stored
in ``ScanContext``, the Django cache, or module globals (AD-4) — answers
``exists``/``is_file``/``is_dir`` queries against those listings. The
cache spans one folder invocation's directories, so its size is bounded
by that folder's subdirectory count, not the tree.

Query semantics — membership hit, miss-confirm fallback:
- A positive hit in the respective ``follow_symlinks=False`` set answers
  ``True`` with pure set membership (zero stat).
- A membership MISS is confirmed by one real ``os.path.exists``/
  ``isfile``/``isdir`` call before answering ``False``. Consequences:
  (a) case- or Unicode-normalization divergences between the index and
  the dirent bytes can never produce a false "does not exist" error;
  (b) a directory whose listing failed (``OSError``) degrades to exactly
  today's per-file real checks — its sets are empty, so every query
  miss-confirms; (c) a file created after the listing snapshot is still
  found via the miss-confirm; a file deleted after the snapshot passes
  verification and errors downstream — the one residual TOCTOU
  direction, deliberate under AD-4; (d) degenerate paths (filesystem
  root, empty split name) fall through to the real check.

Symlink semantics (the hybrid presence ruling):
- Listing sets are built with ``follow_symlinks=False``; FR-24's cycle
  guard applies ONLY to the recursion-facing ``dirs`` set (symlinked
  directories are never in ``dirs``).
- The helpers see through symlinks exactly like today's ``os.path``
  calls: entries recorded as symlinks fall back to one real ``os.path``
  call of the matching kind (follows the link), so a valid symlink is
  seen-through and a broken one is absent.

Scandir failure: an ``OSError`` from the ``os.scandir`` call or its
iteration (missing dir, unreadable ``0o000``) yields an empty listing
with ``error=str(e)`` recorded — unsurfaced until story 2.6 (FR-22),
exposed to it via ``FolderListings.errors()``. An ``OSError`` from a
single entry's type probes skips only that entry (it will miss-confirm
at query time).

The API is deliberately wider than 2.2 needs: it is the reuse surface
for 2.3 sidecar probes (FR-16, incl. parent-directory probes such as
xdcam's ``../MEDIAPRO.XML``) and 2.6 subfolder discovery (FR-19).
"""

import os
from dataclasses import dataclass


def _normalize(path: str) -> str:
    """Normalized absolute form of ``path`` — the cache/listing key.

    Relative paths are refused: every verification query joins an
    already-resolved storage root, so a relative input is a caller bug
    that ``os.path.abspath`` would silently mis-resolve against the cwd.
    """
    if not os.path.isabs(path):
        raise ValueError(
            f"verification paths must be absolute "
            f"(storage root already joined), got: {path!r}"
        )
    return os.path.normpath(path)


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
    names: frozenset[str]
    files: frozenset[str]
    dirs: frozenset[str]
    symlinks: frozenset[str]
    error: str | None = None


def list_directory(path: str) -> DirectoryListing:
    """List ``path`` with exactly one ``os.scandir`` call.

    An ``OSError`` from the scandir call or its iteration (missing
    directory, permission denied) yields an empty listing with the error
    recorded — never a raise. An ``OSError`` from one entry's type
    probes skips only that entry: absent from every set, it degrades to
    a real per-file check at query time (miss-confirm).
    """
    normalized = _normalize(path)
    names, files, dirs, symlinks = set(), set(), set(), set()
    error = None
    try:
        with os.scandir(normalized) as entries:
            for entry in entries:
                try:
                    entry_is_symlink = entry.is_symlink()
                    entry_is_file = entry.is_file(follow_symlinks=False)
                    entry_is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    # Unreadable entry: skip it — queries for this name
                    # miss-confirm against the real filesystem.
                    continue
                names.add(entry.name)
                if entry_is_symlink:
                    symlinks.add(entry.name)
                if entry_is_file:
                    files.add(entry.name)
                elif entry_is_dir:
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
        self._listings: dict[str, DirectoryListing] = {}

    def get(self, directory: str) -> DirectoryListing:
        """The (cached) listing for ``directory`` — at most one scandir."""
        key = _normalize(directory)
        listing = self._listings.get(key)
        if listing is None:
            listing = list_directory(key)
            self._listings[key] = listing
        return listing

    def errors(self) -> dict[str, str]:
        """``{path: error}`` for every cached listing whose scandir failed.

        Story 2.6 consumes this to surface scandir failures (FR-22);
        the format stays ``str(e)`` per its ruling.
        """
        return {
            listing.path: listing.error
            for listing in self._listings.values()
            if listing.error is not None
        }

    def exists(self, abs_path: str) -> bool:
        """Batched ``os.path.exists``: name-membership hit, symlinks and
        misses resolved through one real ``os.path.exists``."""
        normalized = _normalize(abs_path)
        directory, name = os.path.split(normalized)
        if not name:
            # Degenerate path (filesystem root): real check.
            return os.path.exists(normalized)
        listing = self.get(directory)
        if name in listing.symlinks:
            # Hybrid ruling: a symlink dirent gets one real (following)
            # check — valid symlink seen-through, broken symlink absent.
            return os.path.exists(normalized)
        if name in listing.names:
            return True
        # Miss-confirm: one real check before answering False.
        return os.path.exists(normalized)

    def is_file(self, abs_path: str) -> bool:
        """Batched ``os.path.isfile`` over the ``files`` set; symlinks
        and misses resolved through one real ``os.path.isfile``."""
        normalized = _normalize(abs_path)
        directory, name = os.path.split(normalized)
        if not name:
            return os.path.isfile(normalized)
        listing = self.get(directory)
        if name in listing.files:
            return True
        # Symlink dirent (hybrid ruling) or membership miss (confirm):
        # both resolve with one real, link-following check.
        return os.path.isfile(normalized)

    def is_dir(self, abs_path: str) -> bool:
        """Batched ``os.path.isdir`` over the ``dirs`` set; symlinks
        and misses resolved through one real ``os.path.isdir``."""
        normalized = _normalize(abs_path)
        directory, name = os.path.split(normalized)
        if not name:
            return os.path.isdir(normalized)
        listing = self.get(directory)
        if name in listing.dirs:
            return True
        return os.path.isdir(normalized)

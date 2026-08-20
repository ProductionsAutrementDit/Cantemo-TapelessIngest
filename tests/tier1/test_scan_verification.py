"""Tier 1 (story 2.2): scan/verification.py — batched, Portal-free listings.

Everything runs against tmp_path with no Portal object in sight;
Portal-freedom itself is proven in a bare subprocess with NO stub
installed (mirroring 2.1's check for scan.context).

Covers the spec's I/O matrix under the miss-confirm ruling: present
files answer via pure set membership; a membership miss is confirmed by
exactly one real os.path call before answering False, so a failed
listing degrades to today's per-file checks and case divergences never
produce false "does not exist" errors. Plus: multi-directory batching
(one scandir per unique normalized directory, monkeypatch-counted), the
FR-24 symlinked-dir fixture, valid/broken symlinks under the hybrid
ruling, through-link queries, FIFO dirents, ``..`` normalization,
per-entry OSError skip, empty directories, and scandir OSError handling
(missing dir + the tested ``0o000`` permission case).
"""

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.scan.verification import (
    DirectoryListing,
    FolderListings,
    list_directory,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# cwd=repo root, no stub, no conftest: `python -c` puts the cwd on sys.path,
# so `scan.verification` resolves to this repo's package in a bare interpreter.
PORTAL_FREEDOM_SCRIPT = (
    "import sys, scan.verification; "
    "assert not [m for m in sys.modules if m == 'portal' or m.startswith('portal.')]"
)


@pytest.fixture
def scandir_calls(monkeypatch):
    """Count (and forward) every os.scandir call, recording the path."""
    calls = []
    real_scandir = os.scandir

    def counting_scandir(path, *args, **kwargs):
        calls.append(os.fspath(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    return calls


@pytest.fixture
def exists_calls(monkeypatch):
    """Count (and forward) every os.path.exists call, recording the path."""
    calls = []
    real_exists = os.path.exists

    def counting_exists(path, *args, **kwargs):
        calls.append(os.fspath(path))
        return real_exists(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "exists", counting_exists)
    return calls


class _FakeEntry:
    """Duck-typed os.DirEntry double (the real type resists monkeypatching)."""

    def __init__(self, name, kind="file", raises=False):
        self.name = name
        self._kind = kind
        self._raises = raises

    def _probe(self, kinds):
        if self._raises:
            raise OSError(5, "Input/output error", self.name)
        return self._kind in kinds

    def is_symlink(self):
        return self._probe(("symlink",))

    def is_file(self, follow_symlinks=True):
        return self._probe(("file",))

    def is_dir(self, follow_symlinks=True):
        return self._probe(("dir",))


class _FakeScandir:
    """Context-manager/iterator double for a patched os.scandir result."""

    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc_info):
        return False


def test_verification_imports_portal_free_in_subprocess():
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_FREEDOM_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan.verification is not Portal-free in a bare interpreter (AD-9):\n"
        f"{result.stderr.decode(errors='replace')}"
    )


def test_directory_listing_is_frozen(tmp_path):
    listing = list_directory(str(tmp_path))
    with pytest.raises(dataclasses.FrozenInstanceError):
        listing.path = "/elsewhere"


def test_relative_path_raises_value_error():
    listings = FolderListings()
    with pytest.raises(ValueError, match="absolute"):
        listings.exists("relative/clip.fake")
    with pytest.raises(ValueError, match="absolute"):
        listings.get("relative")
    with pytest.raises(ValueError, match="absolute"):
        list_directory("relative")


def test_present_file_is_a_pure_membership_hit(tmp_path, exists_calls):
    (tmp_path / "CLIPNEW.fake").write_bytes(b"data")
    listings = FolderListings()
    assert listings.exists(str(tmp_path / "CLIPNEW.fake")) is True
    # Positive hit: pure set membership, zero real stat.
    assert exists_calls == []
    assert listings.is_file(str(tmp_path / "CLIPNEW.fake")) is True
    assert listings.is_dir(str(tmp_path / "CLIPNEW.fake")) is False


def test_index_only_file_miss_confirms_with_one_real_check(tmp_path, exists_calls):
    # CLIPGONE pattern: indexed but never written to disk.
    (tmp_path / "CLIPNEW.fake").write_bytes(b"data")
    gone = tmp_path / "CLIPGONE.fake"
    listings = FolderListings()
    assert listings.exists(str(gone)) is False
    # The miss was confirmed by exactly one real os.path.exists call.
    assert exists_calls == [str(gone)]
    assert listings.is_file(str(gone)) is False


def test_case_mismatch_never_false_absent(tmp_path):
    # Consequence (a) of miss-confirm: an index/dirent case divergence
    # answers exactly like os.path.exists (True on case-insensitive
    # filesystems via the fallback, False on case-sensitive ones) — never
    # a false "does not exist" the real filesystem would deny.
    (tmp_path / "CLIP.FAKE").write_bytes(b"data")
    query = str(tmp_path / "clip.fake")
    listings = FolderListings()
    assert listings.exists(query) == os.path.exists(query)
    assert listings.is_file(query) == os.path.isfile(query)


def test_file_created_after_snapshot_is_found(tmp_path):
    # Consequence (c): the snapshot is stale but the miss-confirm sees
    # the newly created file.
    listings = FolderListings()
    late = tmp_path / "LATE.fake"
    assert listings.exists(str(late)) is False
    late.write_bytes(b"created after the listing snapshot")
    assert listings.exists(str(late)) is True
    assert listings.is_file(str(late)) is True


def test_failed_listing_degrades_to_real_per_file_checks(tmp_path, monkeypatch):
    # Consequence (b): scandir fails on a directory whose files are
    # readable — every query miss-confirms, behaving exactly like
    # today's per-file os.path checks.
    (tmp_path / "CLIP.fake").write_bytes(b"data")

    def failing_scandir(path, *args, **kwargs):
        raise OSError(13, "Permission denied", os.fspath(path))

    monkeypatch.setattr(os, "scandir", failing_scandir)
    listings = FolderListings()
    assert listings.exists(str(tmp_path / "CLIP.fake")) is True
    assert listings.is_file(str(tmp_path / "CLIP.fake")) is True
    assert listings.exists(str(tmp_path / "MISSING.fake")) is False
    assert listings.errors() == {
        str(tmp_path): f"[Errno 13] Permission denied: '{tmp_path}'"
    }


def test_per_entry_oserror_skips_only_that_entry(tmp_path, monkeypatch):
    entries = [
        _FakeEntry("good.fake", kind="file"),
        _FakeEntry("bad.fake", kind="file", raises=True),
        _FakeEntry("subdir", kind="dir"),
    ]
    monkeypatch.setattr(os, "scandir", lambda path: _FakeScandir(entries))

    listing = list_directory(str(tmp_path))
    # The raising entry is skipped; the others survive; no error listing.
    assert listing.names == frozenset({"good.fake", "subdir"})
    assert listing.files == frozenset({"good.fake"})
    assert listing.dirs == frozenset({"subdir"})
    assert listing.error is None


def test_listing_sets_and_normalized_path(tmp_path):
    (tmp_path / "clip.fake").write_bytes(b"data")
    (tmp_path / "subdir").mkdir()
    listing = list_directory(str(tmp_path))
    assert listing.path == os.path.normpath(str(tmp_path))
    assert listing.names == frozenset({"clip.fake", "subdir"})
    assert listing.files == frozenset({"clip.fake"})
    assert listing.dirs == frozenset({"subdir"})
    assert listing.symlinks == frozenset()
    assert listing.error is None


def test_one_scandir_per_unique_directory(tmp_path, scandir_calls):
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "a" / "b"
    dir_b.mkdir(parents=True)
    (dir_a / "one.fake").write_bytes(b"1")
    (dir_b / "two.fake").write_bytes(b"2")

    listings = FolderListings()
    assert listings.exists(str(dir_a / "one.fake")) is True
    assert listings.exists(str(dir_b / "two.fake")) is True
    assert listings.exists(str(dir_a / "missing.fake")) is False
    # Two unique directories, exactly two scandir calls (the missing-file
    # miss-confirm is a stat, never another scandir).
    assert len(scandir_calls) == 2

    # Cache reused across ALL helpers — no further scandir.
    assert listings.is_file(str(dir_a / "one.fake")) is True
    assert listings.is_dir(str(dir_a / "b")) is True
    assert listings.get(str(dir_a)) is listings.get(str(dir_a))
    assert len(scandir_calls) == 2


def test_parent_dir_probe_normalizes_to_same_listing(tmp_path, scandir_calls):
    # xdcam's ../MEDIAPRO.XML pattern: probe from a subdir via "..".
    (tmp_path / "MEDIAPRO.XML").write_bytes(b"<xml/>")
    sub = tmp_path / "CLIP"
    sub.mkdir()

    listings = FolderListings()
    assert listings.exists(str(tmp_path / "MEDIAPRO.XML")) is True
    assert listings.is_file(str(sub / ".." / "MEDIAPRO.XML")) is True
    # The ".." probe normalized onto the already-cached parent listing.
    assert len(scandir_calls) == 1


def test_fr24_symlinked_dir_excluded_from_dirs(tmp_path):
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (tmp_path / "linked_dir").symlink_to(real_dir, target_is_directory=True)

    listing = list_directory(str(tmp_path))
    assert "linked_dir" in listing.names
    assert "linked_dir" in listing.symlinks
    # FR-24: the recursion-facing dirs set never contains symlinked dirs.
    assert "linked_dir" not in listing.dirs
    assert "linked_dir" not in listing.files
    assert listing.dirs == frozenset({"real_dir"})


def test_valid_symlinks_seen_through_by_helpers(tmp_path):
    # Hybrid ruling: symlink dirents resolve via one real os.path call,
    # so the helpers behave exactly like today's os.path functions.
    (tmp_path / "real.fake").write_bytes(b"data")
    (tmp_path / "link.fake").symlink_to(tmp_path / "real.fake")
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (tmp_path / "linked_dir").symlink_to(real_dir, target_is_directory=True)

    listings = FolderListings()
    assert listings.exists(str(tmp_path / "link.fake")) is True
    assert listings.is_file(str(tmp_path / "link.fake")) is True
    assert listings.is_dir(str(tmp_path / "link.fake")) is False
    assert listings.exists(str(tmp_path / "linked_dir")) is True
    assert listings.is_dir(str(tmp_path / "linked_dir")) is True
    assert listings.is_file(str(tmp_path / "linked_dir")) is False


def test_file_inside_symlinked_dir_found_through_link(tmp_path):
    # A query path that traverses a symlinked directory: the listing of
    # the link path follows the link (like os.scandir on that path), so
    # the file inside answers present — same as os.path.exists today.
    real_dir = tmp_path / "real_dir"
    real_dir.mkdir()
    (real_dir / "inside.fake").write_bytes(b"data")
    (tmp_path / "linked_dir").symlink_to(real_dir, target_is_directory=True)

    listings = FolderListings()
    through_link = str(tmp_path / "linked_dir" / "inside.fake")
    assert listings.exists(through_link) is True
    assert listings.is_file(through_link) is True
    assert listings.exists(str(tmp_path / "linked_dir" / "missing.fake")) is False


def test_broken_symlink_is_absent_to_helpers(tmp_path):
    (tmp_path / "broken.fake").symlink_to(tmp_path / "missing.fake")

    listing = list_directory(str(tmp_path))
    assert "broken.fake" in listing.names
    assert "broken.fake" in listing.symlinks

    listings = FolderListings()
    # os.path.exists on a broken symlink is False today — preserved.
    assert listings.exists(str(tmp_path / "broken.fake")) is False
    assert listings.is_file(str(tmp_path / "broken.fake")) is False
    assert listings.is_dir(str(tmp_path / "broken.fake")) is False


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="os.mkfifo not available on this platform"
)
def test_fifo_dirent_in_names_but_untyped(tmp_path):
    fifo = tmp_path / "pipe.fake"
    os.mkfifo(fifo)

    listing = list_directory(str(tmp_path))
    assert "pipe.fake" in listing.names
    assert "pipe.fake" not in listing.files
    assert "pipe.fake" not in listing.dirs
    assert "pipe.fake" not in listing.symlinks

    listings = FolderListings()
    # Same answers as os.path today: it exists, is neither file nor dir.
    assert listings.exists(str(fifo)) is True
    assert listings.is_file(str(fifo)) is False
    assert listings.is_dir(str(fifo)) is False


def test_empty_directory_answers_absent(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    listing = list_directory(str(empty))
    assert listing.names == frozenset()
    assert listing.error is None

    listings = FolderListings()
    assert listings.exists(str(empty / "anything.fake")) is False


def test_missing_directory_records_error_and_answers_absent(tmp_path):
    gone = tmp_path / "never_created"
    listing = list_directory(str(gone))
    assert listing.names == frozenset()
    assert listing.files == frozenset()
    assert listing.dirs == frozenset()
    assert listing.symlinks == frozenset()
    assert listing.error is not None

    listings = FolderListings()
    assert listings.exists(str(gone / "clip.fake")) is False
    # errors() exposes the recorded failure to story 2.6 (FR-22).
    assert listings.errors() == {str(gone): listing.error}


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores 0o000 directory permissions",
)
def test_unreadable_directory_answers_like_todays_real_checks(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "clip.fake").write_bytes(b"data")
    locked.chmod(0o000)
    try:
        listing = list_directory(str(locked))
        assert listing.names == frozenset()
        assert listing.error is not None

        listings = FolderListings()
        # Miss-confirm degrades to the real check, which — like today's
        # os.path.exists under an unreadable 0o000 dir — answers False.
        assert listings.exists(str(locked / "clip.fake")) is False
        assert listings.errors() == {str(locked): listings.get(str(locked)).error}
    finally:
        # Restore: pytest's tmp_path GC of prior runs fails on 0o000 dirs.
        locked.chmod(0o755)

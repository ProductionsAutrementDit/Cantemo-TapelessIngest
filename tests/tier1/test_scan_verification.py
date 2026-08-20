"""Tier 1 (story 2.2): scan/verification.py — batched, Portal-free listings.

Everything runs against tmp_path with no Portal object in sight;
Portal-freedom itself is proven in a bare subprocess with NO stub
installed (mirroring 2.1's check for scan.context).

Covers the spec's I/O matrix: present/absent files, multi-directory
batching (one scandir per unique normalized directory, monkeypatch-
counted), the FR-24 symlinked-dir fixture, valid/broken file symlinks
under the hybrid presence ruling, ``..`` normalization, empty
directories, and scandir OSError handling (missing dir + the tested
``0o000`` permission case).
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


def test_present_file_is_a_membership_hit(tmp_path):
    (tmp_path / "CLIPNEW.fake").write_bytes(b"data")
    listings = FolderListings()
    assert listings.exists(str(tmp_path / "CLIPNEW.fake")) is True
    assert listings.is_file(str(tmp_path / "CLIPNEW.fake")) is True
    assert listings.is_dir(str(tmp_path / "CLIPNEW.fake")) is False


def test_index_only_file_is_absent(tmp_path):
    # CLIPGONE pattern: indexed but never written to disk.
    (tmp_path / "CLIPNEW.fake").write_bytes(b"data")
    listings = FolderListings()
    assert listings.exists(str(tmp_path / "CLIPGONE.fake")) is False
    assert listings.is_file(str(tmp_path / "CLIPGONE.fake")) is False


def test_listing_sets_and_normalized_path(tmp_path):
    (tmp_path / "clip.fake").write_bytes(b"data")
    (tmp_path / "subdir").mkdir()
    listing = list_directory(str(tmp_path))
    assert listing.path == os.path.normpath(os.path.abspath(str(tmp_path)))
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
    # Two unique directories, exactly two scandir calls.
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


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores 0o000 directory permissions",
)
def test_unreadable_directory_records_error_and_answers_absent(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "clip.fake").write_bytes(b"data")
    locked.chmod(0o000)
    try:
        listing = list_directory(str(locked))
        assert listing.names == frozenset()
        assert listing.error is not None

        listings = FolderListings()
        # Matches os.path.exists's swallow-to-False under an unreadable dir.
        assert listings.exists(str(locked / "clip.fake")) is False
    finally:
        # Restore: pytest's tmp_path GC of prior runs fails on 0o000 dirs.
        locked.chmod(0o755)

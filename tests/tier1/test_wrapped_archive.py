"""Tier 1: the P5 lookup contract and its per-run cache."""

import pytest

from portal.plugins.TapelessIngest.wrapped.archive import (
    ArchiveLookupError,
    CachedArchive,
    Entry,
    load_archive_lookup,
)
from tests.wrapped_fakes import FakeArchive

FOLDER = "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2016/X/CONTENTS/AUDIO"


def test_one_folder_listing_serves_every_file_of_the_folder():
    fake = FakeArchive()
    for n in range(4):
        fake.archive(f"{FOLDER}/00924E0{n}.MXF", f"AirbusHelicopters#A{n}")
    cached = CachedArchive(fake)
    handles = [cached.resolve(f"{FOLDER}/00924E0{n}.MXF").handle for n in range(4)]
    assert handles == [f"AirbusHelicopters#A{n}" for n in range(4)]
    assert fake.folder_calls == [FOLDER]


def test_a_name_absent_from_the_folder_is_not_archived_without_a_file_lookup():
    fake = FakeArchive()
    fake.archive(f"{FOLDER}/00924E00.MXF", "AirbusHelicopters#A0")
    cached = CachedArchive(fake)
    assert cached.resolve(f"{FOLDER}/00924E09.MXF") is None
    assert fake.lookup_calls == []


def test_a_p5_failure_is_an_error_not_an_absence():
    fake = FakeArchive()
    fake.failing_folders.add(FOLDER)
    with pytest.raises(ArchiveLookupError):
        CachedArchive(fake).resolve(f"{FOLDER}/00924E00.MXF")


def test_volumes_are_fetched_once_per_run():
    fake = FakeArchive()
    fake.archive(f"{FOLDER}/a.MXF", "H", volumes=("10509",))
    calls = []
    original = fake.volume
    fake.volume = lambda volume_id: calls.append(volume_id) or original(volume_id)
    cached = CachedArchive(fake)
    assert cached.volume("10509").barcode == "BC10509"
    assert cached.volume("10509").label == "LABEL.10509"
    assert calls == ["10509"]


def test_entry_is_a_plain_value():
    assert Entry("H", ("1", "2"), 5, 7).volumes == ("1", "2")


def test_a_missing_p5_plugin_is_reported_by_name():
    with pytest.raises(ArchiveLookupError, match="no_such_p5_plugin"):
        load_archive_lookup("no_such_p5_plugin.lookup:build_lookup")

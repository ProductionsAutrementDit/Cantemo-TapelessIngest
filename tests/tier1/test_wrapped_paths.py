"""Tier 1: legacy ClipFile paths normalize to VX-41-relative paths."""

import hashlib

import pytest

from portal.plugins.TapelessIngest.wrapped import paths
from portal.plugins.TapelessIngest.wrapped.disk import Disk


@pytest.mark.parametrize(
    "legacy, relative",
    [
        (
            "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/2016/AH_160627_H225_BTP_TT"
            "/CONTENTS/VIDEO/00924E.MXF",
            "2016/AH_160627_H225_BTP_TT/CONTENTS/VIDEO/00924E.MXF",
        ),
        (
            "/mnt/ActiveMedia/AA - RUSHES TAPELESS/2013/EC_131204_ASIE_TESTIMONIES"
            "_KUALA1/CONTENTS/AUDIO/00733R00.MXF",
            "2013/EC_131204_ASIE_TESTIMONIES_KUALA1/CONTENTS/AUDIO/00733R00.MXF",
        ),
        (
            "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2016/AH_160328_AIRLIFT"
            "/AH_160401_AIRLIFT_FS7_#1/XDROOT/./Clip/982_0962.MXF",
            "2016/AH_160328_AIRLIFT/AH_160401_AIRLIFT_FS7_#1/XDROOT/Clip/982_0962.MXF",
        ),
        (
            "/Volumes/ActiveMedia/AA - RUSHES TAPELESS/2018/AH_20180208_S&S_Stephane"
            " KERVELLA/CONTENTS/VIDEO/0019WU.MXF",
            "2018/AH_20180208_S&S_Stephane KERVELLA/CONTENTS/VIDEO/0019WU.MXF",
        ),
    ],
)
def test_legacy_prefixes_normalize(legacy, relative):
    assert paths.to_relative(legacy) == relative


@pytest.mark.parametrize(
    "outside",
    [
        "/Volumes/ActiveMedia/CANTEMO_FILES/060A2B34.MXF",
        "/mnt/PAD_Storage/AA - RUSHES TAPELESS/",
        "/mnt/PAD_Storage/AA - RUSHES TAPELESS/../etc/passwd",
    ],
)
def test_paths_outside_the_rushes_root_are_refused(outside):
    with pytest.raises(paths.UnknownPrefix):
        paths.to_relative(outside)


def test_absolute_is_under_the_vx41_root():
    assert (
        paths.to_absolute("2016/X/a.MXF")
        == "/mnt/PAD_Storage/AA - RUSHES TAPELESS/2016/X/a.MXF"
    )


def test_disk_probe_reads_the_real_filesystem(tmp_path):
    (tmp_path / "2016").mkdir()
    (tmp_path / "2016" / "a.MXF").write_bytes(b"essence")
    disk = Disk(str(tmp_path))
    assert disk.exists("2016/a.MXF")
    assert not disk.exists("2016/b.MXF")
    assert not disk.exists("2016")  # a directory is not a file
    assert disk.sha1("2016/a.MXF") == hashlib.sha1(b"essence").hexdigest()


def test_disk_reads_text_of_a_real_file(tmp_path):
    (tmp_path / "2016" / "CLIP").mkdir(parents=True)
    (tmp_path / "2016" / "CLIP" / "a.XML").write_bytes(
        b"\xef\xbb\xbf<P2Main>\xc3\xa9</P2Main>"
    )
    disk = Disk(str(tmp_path))
    assert disk.read_text("2016/CLIP/a.XML") == "<P2Main>é</P2Main>"
    assert disk.read_text("2016/CLIP/b.XML") is None
    assert disk.read_text("2016/CLIP") is None  # a directory is not a file

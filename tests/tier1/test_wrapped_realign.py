"""Tier 1: the per-item checks behind ``migrate_wrapped_items realign-clipfile``."""

from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.archive import CachedArchive
from portal.plugins.TapelessIngest.wrapped.paths import to_absolute
from portal.plugins.TapelessIngest.wrapped.realign import (
    ALIGNED,
    REALIGN,
    SKIP,
    examine,
)
from tests.wrapped_fakes import (
    FakeArchive,
    InMemoryGateway,
    file_mov_document,
    seed_item,
)

LEGACY = "/Volumes/Infortrend/AA - RUSHES TAPELESS/"
SHAPE_FILE = "2019/AH_H125M_UZB/PRIVATE/M4ROOT/CLIP/C0020.MP4"
STALE = LEGACY + "2019/AH_H125_UZB/PRIVATE/M4ROOT/./CLIP/C0020.MP4"


def _world(path=SHAPE_FILE, storage=fields.RUSHES_STORAGE, known=True):
    gateway, archive = InMemoryGateway(), FakeArchive()
    seed_item(
        gateway, "VX-1", file_mov_document(file_id="VX-F1", storage=storage, name=path)
    )
    if known:
        archive.archive(to_absolute(path), "H#1")
    return gateway, CachedArchive(archive)


def _examine(world, clipfiles=((7, STALE),)):
    gateway, archive = world
    return examine("VX-1", list(clipfiles), gateway, archive)


def test_realigns_onto_the_shapes_file():
    outcome = _examine(_world())
    assert outcome.kind == REALIGN
    assert (outcome.clipfile_pk, outcome.old) == (7, STALE)
    assert outcome.new == to_absolute(SHAPE_FILE)


def test_already_aligned():
    outcome = _examine(_world(), [(7, LEGACY + SHAPE_FILE)])
    assert outcome.kind == ALIGNED


def _skip(world, why, **kwargs):
    outcome = _examine(world, **kwargs)
    assert outcome.kind == SKIP
    assert outcome.why == why


def test_skips_when_not_exactly_one_clipfile():
    _skip(_world(), "0 ClipFile rows, expected 1", clipfiles=[])
    _skip(_world(), "2 ClipFile rows, expected 1", clipfiles=[(1, STALE), (2, STALE)])


def test_skips_when_not_exactly_one_original_shape():
    gateway, archive = _world()
    gateway.shapes["VX-1"].append(file_mov_document(shape_id="VX-SW2"))
    _skip((gateway, archive), "2 original shapes")
    gateway.shapes["VX-1"] = []
    _skip((gateway, archive), "0 original shapes")


def test_skips_when_the_shape_has_not_exactly_one_file():
    gateway, archive = _world()
    second = file_mov_document(shape_id="VX-SW")
    component = gateway.shapes["VX-1"][0]["containerComponent"]
    component["file"].append(second["containerComponent"]["file"][0] | {"id": "VX-X"})
    _skip((gateway, archive), "2 files on the original shape, expected 1")


def test_skips_a_file_off_the_rushes_storage():
    _skip(
        _world(storage="VX-2"),
        f"original shape's file is on VX-2, not {fields.RUSHES_STORAGE}",
    )


def test_skips_when_the_basename_differs():
    _skip(
        _world(path="2019/AH_H125M_UZB/PRIVATE/M4ROOT/CLIP/C0021.MP4"),
        "basename differs: C0020.MP4 vs C0021.MP4",
    )
    _skip(_world(path="2019/X/c0020.mp4"), "basename differs: C0020.MP4 vs c0020.mp4")


def test_skips_a_clipfile_path_outside_the_rushes_root():
    outcome = _examine(_world(), [(7, "/elsewhere/C0020.MP4")])
    assert outcome.kind == SKIP
    assert outcome.why.startswith("ClipFile path: ")
    assert "AA - RUSHES TAPELESS" in outcome.why


def test_skips_a_file_shared_with_another_item():
    gateway, archive = _world()
    seed_item(
        gateway, "VX-2", file_mov_document(file_id="VX-F1", storage="VX-41", name="x")
    )
    _skip((gateway, archive), "VX-41 entity VX-F1 belongs to ['VX-1', 'VX-2']")


def test_skips_a_file_p5_does_not_know():
    _skip(_world(known=False), f"P5 does not know {SHAPE_FILE}")

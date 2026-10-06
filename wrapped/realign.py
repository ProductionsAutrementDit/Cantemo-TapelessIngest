"""Per-item check behind ``migrate_wrapped_items realign-clipfile``.

An item already migrated on the Vidispine side whose one ``ClipFile.path``
still names a shoot folder as it was before a rename. Everything is read
(the gateway, P5); the caller writes. The first failing check skips the
item, never the whole run.
"""

import posixpath
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.paths import (
    UnknownPrefix,
    to_absolute,
    to_relative,
)

REALIGN = "realign"
ALIGNED = "aligned"
SKIP = "skip"


@dataclass(frozen=True)
class Outcome:
    kind: str
    item_id: str
    why: str = ""
    clipfile_pk: Optional[int] = None
    old: str = ""
    new: str = ""


def _skip(item_id, why):
    return Outcome(SKIP, item_id, why=why)


def examine(item_id, clipfiles: Sequence[Tuple[int, str]], gateway, archive):
    """``clipfiles``: the clip's (pk, path) rows. ``archive`` is the
    planner's CachedArchive."""
    if len(clipfiles) != 1:
        return _skip(item_id, f"{len(clipfiles)} ClipFile rows, expected 1")
    pk, old = clipfiles[0]
    shapes = gateway.original_shapes(item_id)
    if len(shapes) != 1:
        return _skip(item_id, f"{len(shapes)} original shapes")
    files = list(shapes[0].files().values())
    if len(files) != 1:
        return _skip(item_id, f"{len(files)} files on the original shape, expected 1")
    file = files[0]
    if file.storage_id != fields.RUSHES_STORAGE:
        return _skip(
            item_id,
            f"original shape's file is on {file.storage_id}, "
            f"not {fields.RUSHES_STORAGE}",
        )
    try:
        current = to_relative(old)
    except UnknownPrefix as error:
        return _skip(item_id, f"ClipFile path: {error}")
    if posixpath.basename(file.path) != posixpath.basename(current):
        return _skip(
            item_id,
            f"basename differs: {posixpath.basename(current)} vs "
            f"{posixpath.basename(file.path)}",
        )
    items = gateway.file_items(file.file_id)
    if items != [item_id]:
        return _skip(item_id, f"VX-41 entity {file.file_id} belongs to {items}")
    if archive.resolve(to_absolute(file.path)) is None:
        return _skip(item_id, f"P5 does not know {file.path}")
    if current == file.path:
        return Outcome(ALIGNED, item_id, clipfile_pk=pk, old=old)
    return Outcome(
        REALIGN, item_id, clipfile_pk=pk, old=old, new=to_absolute(file.path)
    )

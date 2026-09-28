"""Legacy absolute paths -> paths relative to the VX-41 rushes root.

``ClipFile.path`` still carries the mount points of the wrapping era
(``/Volumes/ActiveMedia/...``, ``/mnt/ActiveMedia/...``). What survived
every re-mount is the ``AA - RUSHES TAPELESS`` directory itself, so the
relative path is whatever follows it.
"""

import posixpath
from dataclasses import dataclass

RUSHES_ROOT = "/mnt/PAD_Storage/AA - RUSHES TAPELESS"
_MARKER = "/AA - RUSHES TAPELESS/"


class UnknownPrefix(ValueError):
    """The path is not under any ``AA - RUSHES TAPELESS`` root."""


@dataclass(frozen=True)
class OriginalFile:
    """One original file of a clip, relative to the VX-41 root."""

    relative: str
    kind: str  # "video" or "audio"


def to_relative(path: str) -> str:
    index = path.find(_MARKER)
    if index < 0:
        raise UnknownPrefix(f"{path!r} is not under an 'AA - RUSHES TAPELESS' root")
    relative = posixpath.normpath(path[index + len(_MARKER) :])
    if relative in ("", ".") or relative.startswith(".."):
        raise UnknownPrefix(f"{path!r} does not name a file under the rushes root")
    return relative


def to_absolute(relative: str) -> str:
    return posixpath.join(RUSHES_ROOT, relative)

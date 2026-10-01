"""The one real-filesystem probe the migration makes.

A real ``isfile``, not the Vidispine index: the index is known to carry
entries for shoots deleted from disk.
"""

import hashlib
import os
from typing import Optional

from portal.plugins.TapelessIngest.wrapped.paths import RUSHES_ROOT

_CHUNK = 1 << 20


class Disk:
    def __init__(self, root: str = RUSHES_ROOT):
        self.root = root

    def exists(self, relative: str) -> bool:
        return os.path.isfile(os.path.join(self.root, relative))

    def sha1(self, relative: str) -> str:
        digest = hashlib.sha1()
        with open(os.path.join(self.root, relative), "rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def size(self, relative: str) -> Optional[int]:
        """The file's size in bytes, or None when it is not a file."""
        path = os.path.join(self.root, relative)
        return os.path.getsize(path) if os.path.isfile(path) else None

    def read_text(self, relative: str) -> Optional[str]:
        """A small text file (a P2 CLIP XML), or None when it is not a file."""
        path = os.path.join(self.root, relative)
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8-sig")

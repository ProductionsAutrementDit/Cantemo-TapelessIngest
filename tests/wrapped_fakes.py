"""In-memory fakes of the wrapped-migration's OWN protocols.

Not Portal mocks (AD-11 forbids those outside tests/portal_stub): these
stand in for ``wrapped.archive.ArchiveLookup``, ``wrapped.gateway.Gateway``
and ``wrapped.disk.Disk``, which the plugin itself defines.
"""

import posixpath

from portal.plugins.TapelessIngest.wrapped.archive import (
    ArchiveLookupError,
    Entry,
    Volume,
)


class FakeArchive:
    def __init__(self):
        self.entries = {}
        self.volumes = {}
        self.failing_folders = set()
        self.folder_calls = []
        self.lookup_calls = []

    def archive(self, abs_path, handle, volumes=("10509",), btime=1506011266):
        self.entries[abs_path] = Entry(handle, tuple(volumes), btime, 1)
        for volume_id in volumes:
            self.volumes.setdefault(
                volume_id, Volume(volume_id, f"BC{volume_id}", f"LABEL.{volume_id}")
            )

    def lookup_folder(self, abs_path):
        self.folder_calls.append(abs_path)
        if abs_path in self.failing_folders:
            raise ArchiveLookupError(f"P5 did not answer for {abs_path}")
        return frozenset(
            posixpath.basename(path)
            for path in self.entries
            if posixpath.dirname(path) == abs_path
        )

    def lookup(self, abs_path):
        self.lookup_calls.append(abs_path)
        return self.entries.get(abs_path)

    def volume(self, volume_id):
        return self.volumes[volume_id]

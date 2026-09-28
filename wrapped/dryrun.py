"""A gateway that reads for real and only RECORDS writes.

Reads stay coherent with the recorded writes (a posted shape is listed,
a re-tagged one disappears from the originals), so a dry run walks the
same phases a real run would and prints every write it would make.
"""

import copy

from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.gateway import FileEntity, parse_shape


class RecordingGateway:
    def __init__(self, inner):
        self._inner = inner
        self.writes = []
        self._files = {}
        self._posted = {}
        self._retagged = set()
        self._deleted_files = set()
        self._minted = 0

    def _mint(self, prefix):
        self._minted += 1
        return f"DRYRUN-{prefix}-{self._minted}"

    # reads
    def original_shapes(self, item_id):
        shapes = [
            s
            for s in self._inner.original_shapes(item_id)
            if (item_id, s.shape_id) not in self._retagged
        ]
        return shapes + [parse_shape(d) for d in self._posted.get(item_id, [])]

    def find_file(self, storage_id, relative):
        if (storage_id, relative) in self._files:
            return FileEntity(*self._files[(storage_id, relative)])
        return self._inner.find_file(storage_id, relative)

    def file_state(self, storage_id, file_id):
        if (storage_id, file_id) in self._deleted_files:
            return None
        return self._inner.file_state(storage_id, file_id)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    # writes
    def register_file(self, storage_id, relative, archived):
        file_id = self._mint("FILE")
        self._files[(storage_id, relative)] = (
            file_id,
            "ARCHIVED" if archived else "CLOSED",
        )
        self.writes.append(("register_file", storage_id, relative, archived))
        return file_id

    def post_shape(self, item_id, document):
        shape_id = self._mint("SHAPE")
        stored = copy.deepcopy(document)
        stored.update({"id": shape_id, "tag": [fields.ORIGINAL_TAG]})
        bodies = [stored["containerComponent"]] + stored.get("videoComponent", [])
        for n, body in enumerate(bodies + stored.get("audioComponent", [])):
            body["id"] = f"{shape_id}-C{n}"
        self._posted.setdefault(item_id, []).append(stored)
        self.writes.append(("post_shape", item_id, document))
        return shape_id

    def set_component_metadata(self, item_id, shape_id, component_id, fields_):
        self.writes.append(
            ("set_component_metadata", item_id, shape_id, component_id, dict(fields_))
        )

    def set_item_metadata(self, item_id, fields_):
        self.writes.append(("set_item_metadata", item_id, dict(fields_)))

    def retag_shape(self, item_id, shape_id, add, remove):
        self._retagged.add((item_id, shape_id))
        self.writes.append(("retag_shape", item_id, shape_id, add, remove))

    def delete_file(self, storage_id, file_id):
        self._deleted_files.add((storage_id, file_id))
        self.writes.append(("delete_file", storage_id, file_id))

"""A gateway that reads for real and only RECORDS writes.

Reads stay coherent with the recorded writes (a posted shape is listed,
a re-tagged one disappears from the originals), so a dry run walks the
same phases a real run would and prints every write it would make.
Every Gateway method is defined here explicitly: there is no attribute
fallback that could let an unlisted write through to the real gateway.
"""

import copy

from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.gateway import FileEntity, parse_shape

_COMPONENT_KEYS = (
    "containerComponent",
    "videoComponent",
    "audioComponent",
    "binaryComponent",
)


class RecordingGateway:
    def __init__(self, inner):
        self._inner = inner
        self.writes = []
        self._files = {}
        self._posted = {}
        self._retagged = set()
        self._deleted_files = set()
        self._states = {}  # (storage_id, file_id) -> state this run set
        self._relocated = {}  # old file_id -> (new file_id, new relative)
        self._minted = 0

    def _mint(self, prefix):
        self._minted += 1
        return f"DRYRUN-{prefix}-{self._minted}"

    # reads
    def original_shapes(self, item_id):
        documents = [
            s.to_document()
            for s in self._inner.original_shapes(item_id)
            if (item_id, s.shape_id) not in self._retagged
        ]
        documents += copy.deepcopy(self._posted.get(item_id, []))
        return [parse_shape(self._repoint(d)) for d in documents]

    def _repoint(self, document):
        """As Vidispine does on relocate: components name the new entity."""
        if not self._relocated:
            return document
        document = copy.deepcopy(document)
        for key in _COMPONENT_KEYS:
            raw = document.get(key)
            for body in raw if isinstance(raw, list) else [raw] if raw else []:
                for file in body.get("file", []):
                    if file.get("id") in self._relocated:
                        new_id, new_relative = self._relocated[file["id"]]
                        file.update(id=new_id, path=new_relative, state="OPEN")
        return document

    def find_file(self, storage_id, relative):
        if (storage_id, relative) in self._files:
            file_id, state = self._files[(storage_id, relative)]
            return FileEntity(file_id, self._states.get((storage_id, file_id), state))
        found = self._inner.find_file(storage_id, relative)
        if found is None or (storage_id, found.file_id) in self._deleted_files:
            return None
        state = self._states.get((storage_id, found.file_id), found.state)
        return FileEntity(found.file_id, state)

    def file_state(self, storage_id, file_id):
        if (storage_id, file_id) in self._deleted_files:
            return None
        if (storage_id, file_id) in self._states:
            return self._states[(storage_id, file_id)]
        return self._inner.file_state(storage_id, file_id)

    def shape_ids(self, item_id, tag):
        return self._inner.shape_ids(item_id, tag)

    def tagged_shapes(self, item_id, tag):
        # Only ever read for a lowres tag, which a dry run never writes.
        return self._inner.tagged_shapes(item_id, tag)

    def file_size(self, file_id):
        return self._inner.file_size(file_id)

    def component_metadata(self, item_id, shape_id, component_id):
        return self._inner.component_metadata(item_id, shape_id, component_id)

    def item_fields(self, item_id, names):
        return self._inner.item_fields(item_id, names)

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

    def untag_shape(self, item_id, shape_id, tag):
        self._retagged.add((item_id, shape_id))
        self.writes.append(("untag_shape", item_id, shape_id, tag))

    def relocate_file(self, storage_id, file_id, new_relative):
        # As Vidispine does it: the old entity goes, a new OPEN one appears.
        new_id = self._mint("FILE")
        self._deleted_files.add((storage_id, file_id))
        self._files[(storage_id, new_relative)] = (new_id, "OPEN")
        self._states[(storage_id, new_id)] = "OPEN"
        self._relocated[file_id] = (new_id, new_relative)
        self.writes.append(("relocate_file", storage_id, file_id, new_relative))

    def set_file_state(self, storage_id, file_id, state):
        self._states[(storage_id, file_id)] = state
        self.writes.append(("set_file_state", storage_id, file_id, state))

    def delete_file(self, storage_id, file_id):
        self._deleted_files.add((storage_id, file_id))
        self.writes.append(("delete_file", storage_id, file_id))

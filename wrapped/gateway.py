"""What the migration needs from Vidispine, as plain values.

Every Portal/Vidispine call goes through ``Gateway``. Its one real
implementation is ``wrapped.vidispine.VidispineGateway``; everything
else in ``wrapped`` is written against this protocol and is testable
without Portal.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

_COMPONENT_KEYS = (
    ("container", "containerComponent"),
    ("video", "videoComponent"),
    ("audio", "audioComponent"),
    ("binary", "binaryComponent"),
)


@dataclass(frozen=True)
class ShapeFile:
    file_id: str
    storage_id: str
    path: str
    state: str


@dataclass(frozen=True)
class Component:
    component_id: str
    kind: str
    files: Tuple[ShapeFile, ...]
    body: Dict[str, Any]


@dataclass(frozen=True)
class Shape:
    shape_id: str
    tags: Tuple[str, ...]
    components: Tuple[Component, ...]
    mime_types: Tuple[str, ...]

    def of_kind(self, kind: str) -> List[Component]:
        return [c for c in self.components if c.kind == kind]

    def files(self) -> Dict[str, ShapeFile]:
        found: Dict[str, ShapeFile] = {}
        for component in self.components:
            for file in component.files:
                found.setdefault(file.file_id, file)
        return found

    def file_ids(self) -> frozenset:
        return frozenset(self.files())

    def to_document(self) -> Dict[str, Any]:
        document: Dict[str, Any] = {"id": self.shape_id, "tag": list(self.tags)}
        if self.mime_types:
            document["mimeType"] = list(self.mime_types)
        for kind, key in _COMPONENT_KEYS:
            bodies = [dict(c.body) for c in self.of_kind(kind)]
            if not bodies:
                continue
            document[key] = bodies[0] if kind == "container" else bodies
        return document


@dataclass(frozen=True)
class FileEntity:
    file_id: str


def parse_shape(document: Mapping[str, Any]) -> Shape:
    components = []
    for kind, key in _COMPONENT_KEYS:
        raw = document.get(key)
        if raw is None:
            continue
        for body in raw if isinstance(raw, list) else [raw]:
            files = tuple(
                ShapeFile(
                    file_id=f.get("id", ""),
                    storage_id=f.get("storage", ""),
                    path=f.get("path", ""),
                    state=f.get("state", ""),
                )
                for f in body.get("file", [])
            )
            components.append(Component(body.get("id", ""), kind, files, dict(body)))
    return Shape(
        shape_id=document.get("id", ""),
        tags=tuple(document.get("tag", [])),
        components=tuple(components),
        mime_types=tuple(document.get("mimeType", [])),
    )


class Gateway(Protocol):
    def original_shapes(self, item_id: str) -> List[Shape]: ...

    def shape_ids(self, item_id: str, tag: str) -> List[str]: ...

    def component_metadata(
        self, item_id: str, shape_id: str, component_id: str
    ) -> Dict[str, str]: ...

    def item_fields(
        self, item_id: str, names: Sequence[str]
    ) -> Dict[str, List[str]]: ...

    def find_file(self, storage_id: str, relative: str) -> Optional[FileEntity]: ...

    def register_file(self, storage_id: str, relative: str, archived: bool) -> str: ...

    def post_shape(self, item_id: str, document: Mapping[str, Any]) -> str: ...

    def set_component_metadata(
        self,
        item_id: str,
        shape_id: str,
        component_id: str,
        fields: Mapping[str, str],
    ) -> None: ...

    def set_item_metadata(self, item_id: str, fields: Mapping[str, str]) -> None: ...

    def retag_shape(
        self, item_id: str, shape_id: str, add: str, remove: str
    ) -> None: ...

    def delete_file(self, storage_id: str, file_id: str) -> None: ...

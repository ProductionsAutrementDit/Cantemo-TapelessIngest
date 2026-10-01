"""The one module of ``wrapped`` that talks to Portal and Vidispine.

Reads go through raw REST (JSON in, values out); writes use the Portal
helpers the 2023 script proved on prod (``notifyStorageOfFile``, always
with an explicit ``state=``, ``setComponentMetadata``,
``update_or_create_item_metadata``, ``removeFileFromStorage``), the
``createFileEntity`` that ``providers/panasonicP2.py`` uses for ARCHIVED
entities (NOT something the 2023 script proved), and the plugin's own
``createShapeFromDocument``, which carries the ``updateItemMetadata=true``
that ``ItemAPI.createItemShape`` drops.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import quote

import simplejson as json
from RestAPIBase.resturl import RestURL
from RestAPIBase.utility import perform_request, prepare_request

from portal.metadata.utils import update_or_create_item_metadata
from portal.plugins.TapelessIngest.helpers import TapelessIngestHelper
from portal.plugins.TapelessIngest.wrapped import fields
from portal.plugins.TapelessIngest.wrapped.gateway import (
    FileEntity,
    Shape,
    parse_shape,
)
from portal.vidispine.iexception import NotFoundError
from portal.vidispine.iitem import ItemHelper
from portal.vidispine.istorage import StorageHelper


def _timespan_fields(document: Any) -> Dict[str, List[str]]:
    found: Dict[str, List[str]] = {}
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if "timespan" in node:
                for span in node["timespan"]:
                    for field in span.get("field", []):
                        found.setdefault(field["name"], []).extend(
                            v.get("value", "") for v in field.get("value", [])
                        )
            else:
                stack.extend(node.values())
    return found


class VidispineGateway:
    def __init__(self):
        self._ingest = TapelessIngestHelper()
        self._items = ItemHelper()
        self._storage = StorageHelper()

    def _request(self, method: str, path: str, query=None, body=None) -> Any:
        api = self._ingest.itemapi
        url = RestURL(f"{api.vsapi.super_url}API/{path}")
        if query:
            url.addQuery(query)
        options = {"method": method, "return_format": "json"}
        if body is not None:
            options.update(body=json.dumps(body), header_contenttype="json")
        result = perform_request(
            **prepare_request(api.vsapi.base64string, url.geturl(), **options)
        )
        return json.loads(result) if result else None

    # reads
    def shape_ids(self, item_id: str, tag: str) -> List[str]:
        listing = self._request("GET", f"item/{item_id}/shape", {"tag": tag}) or {}
        return list(listing.get("uri", []))

    def original_shapes(self, item_id: str) -> List[Shape]:
        return self.tagged_shapes(item_id, fields.ORIGINAL_TAG)

    def tagged_shapes(self, item_id: str, tag: str) -> List[Shape]:
        return [
            parse_shape(self._request("GET", f"item/{item_id}/shape/{shape_id}"))
            for shape_id in self.shape_ids(item_id, tag)
        ]

    def file_size(self, file_id: str) -> Optional[int]:
        document = self._request("GET", f"storage/file/{file_id}") or {}
        try:
            size = int(document.get("size"))
        except (TypeError, ValueError):
            return None
        return None if size < 0 else size

    def file_items(self, file_id: str) -> List[str]:
        document = (
            self._request("GET", f"storage/file/{file_id}", {"includeItem": "true"})
            or {}
        )
        return [item["id"] for item in document.get("item", []) if item.get("id")]

    def component_metadata(
        self, item_id: str, shape_id: str, component_id: str
    ) -> Dict[str, str]:
        document = (
            self._request(
                "GET",
                f"item/{item_id}/shape/{shape_id}/component/{component_id}/metadata",
            )
            or {}
        )
        return {f["key"]: f.get("value", "") for f in document.get("field", [])}

    def item_fields(self, item_id: str, names: Sequence[str]) -> Dict[str, List[str]]:
        document = self._request(
            "GET", f"item/{item_id}/metadata", {"field": ",".join(names)}
        )
        found = _timespan_fields(document or {})
        return {name: found[name] for name in names if name in found}

    def find_file(self, storage_id: str, relative: str) -> Optional[FileEntity]:
        try:
            found = self._storage.getFileByPath(storage_id, relative)
        except NotFoundError:
            return None
        return FileEntity(found.getId(), found.getState())

    def file_state(self, storage_id: str, file_id: str) -> Optional[str]:
        # getFileById is global in Vidispine: storage_id is unused here.
        try:
            return self._storage.getFileById(file_id).getState()
        except NotFoundError:
            return None

    # writes
    def register_file(self, storage_id: str, relative: str, archived: bool) -> str:
        if archived:
            created = self._storage.createFileEntity(
                storage_id,
                relative,
                createOnly=True,
                state="ARCHIVED",
                return_format="json",
            )
            return created["id"]
        return self._storage.notifyStorageOfFile(storage_id, relative, state="CLOSED")

    def post_shape(self, item_id: str, document: Mapping[str, Any]) -> str:
        created = self._ingest.itemapi.createShapeFromDocument(
            item_id, dict(document), tag=fields.ORIGINAL_TAG, update_item_metadata=True
        )
        return created["id"]

    def set_component_metadata(
        self, item_id: str, shape_id: str, component_id: str, values: Mapping[str, str]
    ) -> None:
        for key, value in values.items():
            self._items.setComponentMetadata(
                item_id, shape_id, component_id, key, value
            )

    def set_item_metadata(self, item_id: str, values: Mapping[str, str]) -> None:
        for key, value in values.items():
            update_or_create_item_metadata(item_id, key, value)

    def untag_shape(self, item_id: str, shape_id: str, tag: str) -> None:
        self._request("DELETE", f"item/{item_id}/shape/{shape_id}/tag/{tag}")

    def relocate_file(self, storage_id: str, file_id: str, new_relative: str) -> None:
        # Encoded here, not through addQuery: Portal's RestURL joins query
        # values raw, and shoot folders carry spaces.
        self._request(
            "POST",
            f"storage/{storage_id}/file/{file_id}/path"
            f"?path={quote(new_relative, safe='/')}",
        )

    def set_file_state(self, storage_id: str, file_id: str, state: str) -> None:
        self._request("PUT", f"storage/{storage_id}/file/{file_id}/state/{state}")

    def delete_file(self, storage_id: str, file_id: str) -> None:
        try:
            self._storage.removeFileFromStorage(storage_id, file_id)
        except NotFoundError:
            pass  # already gone: a resumed run re-entering the last phase

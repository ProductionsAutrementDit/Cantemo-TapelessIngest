# Pinned-bug ledger (story 1.3)

Defects and quirks in today's scan behavior, deliberately **pinned as-is** by
the story 1.3 test suite. Per the spec's boundaries, none of these may be
fixed without explicit human sign-off — Epic 2's strangler refactor and
Epic 4's discovery-equivalence gate compare against this exact behavior.
When one is eventually fixed (with sign-off), update the pinning test and
delete its entry here.

Anchors are SYMBOL names, not line numbers: `models/folder.py` has grown by
several hundred lines across Epic 2 and every numeric anchor rotted.

| # | Defect / quirk | Code anchor | Pinning test |
|---|----------------|-------------|--------------|
| 2 | `cursor` parameter accepted but completely ignored — results are identical with and without it | `Folder.scan` / `Folder.ingest` signatures (`cursor` is never read in either body) | `tests/tier1/test_scan_pagination.py::test_cursor_is_ignored` |
| 3 | `hits` reflects only the **last** page's `total.value`: each page overwrites `response["hits"]`, so divergent per-page totals silently lose all but the final one | `Folder.scan`, the `while has_next` page loop (`response["hits"] = search_result["hits"]["total"]["value"]`) | `tests/tier1/test_scan_pagination.py::test_count_multi_page_call_sequence` |

## Caveats

- **Error-string wording**: the exact text asserted after
  `"Error scanning file {path}: "` in the counters test partly reflects the
  stub `VSFile.__str__` (which renders the file path); prod's `VSFile` has no
  such `__str__`, so the live message embeds a default object repr instead.
  The pinned production behavior is the `Error scanning file {path}: {e}`
  template plus swallow-and-continue per file — not the repr of the file
  object inside the exception text.

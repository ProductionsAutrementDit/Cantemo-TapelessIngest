# Pinned-bug ledger (story 1.3)

Defects and quirks in today's scan behavior, deliberately **pinned as-is** by
the story 1.3 test suite. Per the spec's boundaries, none of these may be
fixed without explicit human sign-off — Epic 2's strangler refactor and
Epic 4's discovery-equivalence gate compare against this exact behavior.
When one is eventually fixed (with sign-off), update the pinning test and
delete its entry here.

| # | Defect / quirk | Code anchor | Pinning test |
|---|----------------|-------------|--------------|
| 1 | `already_ingested` inflation: it increments for **every** successfully processed clip, not only previously ingested ones, because `get_clip_from_file` unconditionally sets `clip.file` | `models/clip.py:332` (`clip.file = file`), counted at `models/folder.py:425-426` | `tests/tier2/test_scan_counters.py::test_single_page_scan_counters` |
| 2 | `cursor` parameter accepted but completely ignored — results are identical with and without it | `models/folder.py:356-365` (signature; `cursor` never read) | `tests/tier1/test_scan_pagination.py::test_cursor_is_ignored` |
| 3 | `hits` reflects only the **last** page's `total.value`: each page overwrites `response["hits"]`, so divergent per-page totals silently lose all but the final one | `models/folder.py:390` | `tests/tier1/test_scan_pagination.py::test_count_multi_page_call_sequence` |
| 4 | A "scan" is not read-only: it writes `provider_names`/`scanned_on` and saves the folder whenever at least one provider matched (and skips the save when zero providers matched, even if files errored) | `models/folder.py:438-441` | `tests/tier2/test_scan_counters.py::test_single_page_scan_counters` |
| 5 | `storage=None` raises `AttributeError` (`_root_path` never assigned) instead of recording a `Cannot get full path…` entry in `errors` | `models/folder.py:122-130` (`root_path` property) | `tests/tier1/test_scan_pagination.py::test_storage_none_raises_attributeerror` |

## Caveats

- **Error-string wording**: the exact text asserted after
  `"Error scanning file {path}: "` in the counters test partly reflects the
  stub `VSFile.__str__` (which renders the file path); prod's `VSFile` has no
  such `__str__`, so the live message embeds a default object repr instead.
  The pinned production behavior is the `Error scanning file {path}: {e}`
  template plus swallow-and-continue per file — not the repr of the file
  object inside the exception text.

# FR-4 equivalence waivers

FR-4 requires the refactored pipeline to behave equivalently to the legacy
one. Every DELIBERATE, signed-off divergence is declared here — one row per
behavioral delta, added by the story that introduces it, together with the
pinning-test update and the `tests/pinned-bugs.md` row it retires.

A delta absent from this table is a regression, not a decision.

| FR | story | date | behavioral delta |
|----|-------|------|------------------|
| FR-10 | 2.4 | 2026-08-21 | Scan now materializes `Clip`/`ClipMetadata` rows for never-ingested clips (a hash-recovered `item_id` therefore lands on the row at scan time instead of at ingest time). Pre-2.4 only an already-saved clip's metadata was written, per key, by the `metadatas` setter. |
| FR-10 | 2.4 | 2026-08-21 | Re-scanning a never-ingested folder reports `created=0` instead of re-counting the same N clips on every run — the first scan now persists them. More honest; the FR-4 counter tuples of a first scan are unaffected. Pinned by `tests/tier2/test_persistence_write_unit.py::test_rescan_never_ingested_reports_created_zero`. |
| FR-11 | 2.4 | 2026-08-21 | A dry-run scan no longer writes the `Folder` row nor an existing clip's metadata rows. Pre-2.4 `dry_run` gated only `Folder.ingest`'s ingest block, so a "dry" run still wrote both. Directionally FR-11; full dry-run purity is story 2.7. Pinned by `tests/tier2/test_persistence_write_unit.py::test_dry_run_scan_writes_nothing`. |

Retires: `tests/pinned-bugs.md` row #4 (scan writes the folder row). The
surviving half of that row — a scan is still not read-only, it still
persists `provider_names`/`scanned_on` — stays pinned in
`tests/tier2/test_scan_counters.py::test_single_page_scan_counters`, now
together with the write-once and zero-hit-no-row guarantees.

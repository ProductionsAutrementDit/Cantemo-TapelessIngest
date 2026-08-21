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
| FR-23 | 2.5 | 2026-08-21 | `already_ingested` now counts clips whose `item_id` is truthy (post hash-recovery) instead of every clip the page processed. Pre-2.5 it counted `clip.file is not None`, which the scan sets for every clip it touches, so the counter reported the page size. Reported values drop for folders of never-ingested clips; the ingest counters are unaffected. Pinned by `tests/tier2/test_scan_counters.py::test_single_page_scan_counters`. |
| FR-8 | 2.5 | 2026-08-21 | A file Cantemo has not hashed yet is SKIPPED with a logged retry-next-run reason instead of raising `TapelessIngestException("No hash found in file …")`. Pre-2.5 the exception cost the file its clip, its metadatas and its scan record, and appeared in `errors`; it is now scanned normally, counted `skipped` by the ingest ladder, never matched against a legacy storage and never ingested (NFR-1 ruling: the hash is the dedup key, ingesting without it risks a duplicate item). The next cron run retries it once the hash exists. Pinned by `tests/tier2/test_ingest_discipline.py::test_hash_less_file_is_skipped_with_a_retry_reason`. |
| FR-8 | 2.5 | 2026-08-21 | A recovered `item_id` is now assigned to PRE-EXISTING clip rows as well, not only to rows being created. Recovery fires only when the clip has no `item_id`, so it can never overwrite a known one; the scan still does not persist it (`CLIP_UPDATE_FIELDS` is unchanged) — `Clip.ingest`'s targeted update writes it. Pinned by `tests/tier2/test_ingest_discipline.py::test_recovery_assigns_the_item_id_to_a_pre_existing_row`. |
| FR-35 | 2.5 | 2026-08-21 | `_should_replace_original_files` no longer auto-skips every non-replace call: the guard was `len(original_files) >= 0`, always true. An item whose original shape holds NO file now falls through to the remaining checks (same file id / same storage / non-legacy storage) instead of being declared already-imported. Pinned by `tests/tier1/test_scan_ingestion.py::test_empty_original_files_no_longer_auto_skips`. |
| FR-36 | 2.5 | 2026-08-21 | An import response carrying no `jobId` is counted `failed`, never `ingested`. Pre-2.5 `import_file` fell through to an unconditional `result["ingested"] = True`, and `_import_multi_component` returned `True` whatever the response held — so a clip with a NULL `job_id` and no import job could be reported ingested. Pinned by `tests/tier2/test_ingest_discipline.py::test_multi_component_import_without_a_job_id_is_not_an_ingest` and `::test_a_failed_import_counts_failed_and_never_ingested`. |

Retires: `tests/pinned-bugs.md` row #4 (scan writes the folder row). The
surviving half of that row — a scan is still not read-only, it still
persists `provider_names`/`scanned_on` — stays pinned in
`tests/tier2/test_scan_counters.py::test_single_page_scan_counters`, now
together with the write-once and zero-hit-no-row guarantees.

Retires: `tests/pinned-bugs.md` row #1 (`already_ingested` inflation),
per the FR-23 row above.

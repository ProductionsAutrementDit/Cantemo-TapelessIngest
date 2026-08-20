Hello World!\n

## Running tests

The test suite runs entirely off-server — no Portal, no network, no
OpenSearch, no prod DB. Dev dependencies live in a local venv and are never
deployed:

```
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/
```

Tier 1 (`tests/tier1/`) proves the plugin modules import through the stub;
Tier 2 (`tests/tier2/`) migrates a sqlite `:memory:` DB and exercises the ORM.

**AD-11 stub rule:** `tests/portal_stub/` is the ONLY sanctioned mocking of
Portal and the Cantemo-only distributions (`VidiRest`, `RestAPIBase`,
`pyxb`). No `unittest.mock.patch("portal...")`, no `monkeypatch` of
`portal.*`, no per-test `sys.modules` writes anywhere else in the test tree.

## Management commands

The two production scan scripts are versioned in this repo as Django management
commands (`management/commands/`), ported verbatim from the unversioned copies
in `/opt/cantemo/scripts/` on the prod server:

- `scan_tapeless_dir` — recursive scan + ingest (`Folder.ingest`)
- `check_clips_in_folder` — same shape, but calls `Folder.scan` instead of
  `Folder.ingest`. **Known defect — this command has never been functional in
  production:** the prod script was missing the `WebClient` import and aborted
  at launch; beyond that, it calls `Folder.scan` with `dry_run`/`replace`
  kwargs the method does not accept (`TypeError` on the first folder
  processed) and its log line reads result keys (`ingested`, `skipped`,
  `replaced`) that only `Folder.ingest` produces. It is ported verbatim per
  this versioning story — do not run it expecting results. Its rebuild on the
  shared pipeline is planned (Epic 2, FR-37).

### Invocation

On a Portal host, run them through `manage.py` (which owns the Django setup —
the command modules themselves are import-side-effect-free):

```
/opt/cantemo/python/bin/python /opt/cantemo/portal/manage.py scan_tapeless_dir \
    --storage VX-41 --path 2026 --startWith AH AG AA ACH \
    --skip NE_PAS_ARCHIVER --userId 1 --since 1w

/opt/cantemo/python/bin/python /opt/cantemo/portal/manage.py check_clips_in_folder \
    --storage VX-41 --path 2026 --startWith AH --userId 1 --dryrun
# (check_clips_in_folder: same flags, but see the known defect note above —
#  it does not currently complete a run)
```

Flags, defaults, output, and failure modes are identical to the old scripts
(`--storage`, `--path`, `--userId` required; `--startWith`, `--providers`,
`--skip`, `--only` accept multiple values; plus `--from`, `--since`,
`--dryrun`, `--replace`). Check with
`/opt/cantemo/python/bin/python /opt/cantemo/portal/manage.py scan_tapeless_dir --help`.

**Dependency note:** the command modules import `slack_sdk` at module level,
and Django imports every management command module on *any* `manage.py`
invocation for command discovery — so `slack_sdk` must be present in Portal's
Python. It is today: the nightly cron scan uses it.

### Cron switch

Replace the two crontab entries that call
`/opt/cantemo/scripts/scan_tapeless_dir.py` with the `manage.py` equivalents,
keeping each entry's existing flags exactly as they are today. The lines below
reflect the live prod crontab values as of 2026-08-20 — if prod has drifted
from this README since, prod wins ("carry over each entry's existing flags
verbatim" stays the rule):

```
# daily 21:00, last-week window
0 21 * * * /opt/cantemo/python/bin/python /opt/cantemo/portal/manage.py scan_tapeless_dir --storage VX-41 --path $(date +\%Y) --startWith AH AG AA ACH --skip NE_PAS_ARCHIVER --userId 1 --since 1w
# weekly, Saturday 09:00
0 9 * * 6 /opt/cantemo/python/bin/python /opt/cantemo/portal/manage.py scan_tapeless_dir --storage VX-41 --path $(date +\%Y) --startWith AH AG AA ACH --skip NE_PAS_ARCHIVER --userId 1
```

**Deploy note:** switching cron is a separate action performed during a Portal
restart window — deploy the plugin, run
`manage.py scan_tapeless_dir --help` and one supervised dry run of
`scan_tapeless_dir` only (not `check_clips_in_folder` — see its known defect
note above), then edit the crontab. Do not delete the old
`/opt/cantemo/scripts/` files.

### Command import graph

The command modules import from the plugin **only**
`models.folder.Folder` and `helpers.TapelessIngestException` — none of the
plugin's dead code. Keep it that way: this narrow import graph is the
prerequisite (AD-10) for the later dead-code deletions, and can be re-checked
with:

```
grep -n "^from portal\.plugins\.TapelessIngest" management/commands/*.py
```

Expected output: exactly two import lines per command file — one from
`models.folder` (importing `Folder`) and one from `helpers` (importing
`TapelessIngestException`).

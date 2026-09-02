# TapelessIngest User Guide

## Introduction

The TapelessIngest plugin automates the import of professional camera media into Cantemo Portal. This guide covers common workflows and usage scenarios.

## Getting Started

### Prerequisites

1. **Cantemo Portal Access**: User account with ingest permissions
2. **Storage Configuration**: Storage containing camera media must be:
   - Registered in Vidispine
   - Browsable (allows file listing)
   - Indexed in Elasticsearch
3. **Camera Media**: Supported camera format on storage

### First Time Setup

1. **Access Admin Interface**
   - Navigate to **Admin > TapelessIngest**
   - Configure base settings

2. **Configure Metadata Mappings**
   - Map camera metadata fields to Portal fields
   - Example: `shooting_date` → `created`

3. **Set Collection Rules**
   - Define folder patterns to ignore
   - Set folder name transformations

## Basic Workflows

### Workflow 1: Import Camera Card

**Scenario**: Import all clips from a RED camera card

**Steps**:

1. **Mount Storage**
   - Ensure camera card is mounted and accessible
   - Verify storage is registered in Vidispine

2. **Create Folder**
   ```
   POST /tapelessingest/api/folder/
   {
     "storage_id": "VX-1",
     "path": "/CARD_A"
   }
   ```

3. **Scan Folder**
   ```
   POST /tapelessingest/api/folder/{folder_id}/scan/
   {
     "providers": ["red"]
   }
   ```

   Review scan results:
   - Number of clips found
   - Clip names and metadata
   - Any errors encountered

4. **Ingest Clips**
   ```
   POST /tapelessingest/api/folder/{folder_id}/ingest/
   {
     "number": 10  // Process 10 clips at a time
   }
   ```

5. **Monitor Progress**
   - Check clip status via UI or API
   - View Vidispine jobs in Portal
   - Wait for transcoding to complete

**Result**: All clips imported with metadata, organized in collections

### Workflow 2: Replace Legacy Storage Files

**Scenario**: Media was previously imported from old storage, now available on new faster storage

**Steps**:

1. **Identify Legacy Storage**
   - Note old storage ID (e.g., `VX-OLD`)
   - Verify new storage has same files

2. **Scan with Legacy Matching**
   ```
   POST /tapelessingest/api/folder/{folder_id}/scan/
   {
     "legacy_storages": ["VX-OLD"]
   }
   ```

   The plugin will:
   - Calculate file hashes
   - Match files on old storage
   - Find existing items

3. **Ingest with Replace**
   ```
   POST /tapelessingest/api/folder/{folder_id}/ingest/
   {
     "replace": true,
     "legacy_storages": ["VX-OLD"]
   }
   ```

4. **Verify Results**
   - Check items now reference new storage
   - Confirm original shapes updated
   - Verify metadata preserved

**Result**: Items now use new storage without recreating items

### Workflow 3: Selective Provider Import

**Scenario**: Folder contains mixed formats, only import specific format

**Steps**:

1. **Scan All Formats First**
   ```
   POST /tapelessingest/api/folder/{folder_id}/scan/
   ```

   Review detected providers in results

2. **Ingest Specific Format**
   ```
   POST /tapelessingest/api/folder/{folder_id}/ingest/
   {
     "providers": ["red"],
     "number": 0  // Process all at once
   }
   ```

**Result**: Only RED clips imported, other formats ignored

### Workflow 4: Batch Processing

**Scenario**: Large folder with 1000+ clips needs controlled import

**Steps**:

1. **Initial Scan**
   ```
   POST /tapelessingest/api/folder/{folder_id}/scan/
   {
     "number": 0  // Scan all without creating clip objects
   }
   ```

   Note total clip count

2. **Batch Import Loop**
   ```python
   total_clips = 1000
   batch_size = 20

   for offset in range(0, total_clips, batch_size):
       response = ingest_folder(
           folder_id,
           first=offset,
           number=batch_size
       )
       print(f"Processed {offset + batch_size}/{total_clips}")
       time.sleep(30)  # Wait between batches
   ```

3. **Monitor System Load**
   - Watch Vidispine job queue
   - Check transcoder utilization
   - Adjust batch size as needed

**Result**: Controlled import without overwhelming system

## Common Operations

### View Clip Information

**Via UI**:
1. Navigate to item in Portal
2. View **TapelessIngest** panel
3. See source clip info, provider, original metadata

**Via API**:
```
GET /tapelessingest/api/clip/{umid}/
```

### Check Ingest Status

**Clip Status Values**:
- **Not imported (0)**: Clip scanned but not ingested
- **Wrapped (1)**: Output file created locally
- **Registered (2)**: File registered in Vidispine
- **Placeholder created (3)**: Item exists but no media
- **Imported (4)**: Fully imported with transcodes

**Query by Status**:
```
GET /tapelessingest/api/clips/?status=4
```

### Manage Metadata Mappings

**Add Mapping**:
```
POST /tapelessingest/api/metadata-mappings/
{
  "metadata_provider": "timecode",
  "metadata_portal": "startTimecode"
}
```

**List Mappings**:
```
GET /tapelessingest/api/metadata-mappings/
```

### Configure Collection Hierarchy

**Settings**:
```python
collections_ignore_folder = [
    "PROXY",      # Ignore proxy folders
    "THUMB",      # Ignore thumbnail folders
    "\\..*"       # Ignore hidden folders (starts with .)
]

collections_rename_folder = {
    "MEDIA": "Media Files",
    "RUSHES": "Dailies"
}
```

**Example Path Transformation**:
```
Input:  /PROJECT_A/MEDIA/CARD_A/.hidden
Output: Project A > Media Files > CARD_A
        (.hidden ignored)
```

## Advanced Features

### Parallel Nightly Scan (`--workers`)

Since story 3.1 the command-line tree scan (`manage.py scan_tapeless_dir`,
and its read-only sibling `check_clips_in_folder`) processes folders on a
worker pool:

- `--workers N` (1..16) sets the pool width; **the default is 4**, so the
  first nightly cron run after deploying 3.1 is 4-way concurrent with no
  crontab change.
- The run's output is byte-identical to a sequential run — same counters,
  same log lines and Slack report, same database rows — whatever the pool
  width.
- **Rollback lever**: `--workers 1` runs strictly sequentially on the
  exact pre-3.1 code path. Add it to the cron line if a concurrent run
  ever needs to be ruled out.

This applies to tree (command-line) scans only; UI/API paged scans are
unaffected and reject `workers > 1`.

### Discovery Path (`--discovery`)

Since story 4.1 the tree scan can find its files two ways, and you choose
with a flag:

- `--discovery=legacy` — **the default, and what every run does today**:
  one index query per folder (~4,000 for a year's tree), paged with
  `from`/`size`.
- `--discovery=index` — one index query stream for the whole scan root,
  fetched up front and then read per folder. Far fewer queries, no
  10,000-result ceiling, and no risk of a file being missed or counted
  twice because the index shifted mid-scan.
- `--discovery-page-size N` (1..10000, default 500) — how many hits come
  back in ONE response of that stream. It does not limit how much is
  fetched overall, and it does nothing under `--discovery=legacy`.

Both paths must find the same clips; that is what the equivalence tests
check. **The default will not change until that has been verified against
the real production index**, so deploying this story changes nothing about
the nightly run on its own.

**Rollback lever**: `--discovery=legacy`. Drop the flag and the scan is
back on the path it has always used.

This applies to tree (command-line) scans only; UI/API paged scans always
use `legacy` and reject `--discovery=index`.

### Spanned Clips

**What are Spanned Clips?**
Clips split across multiple files due to camera file size limits.

**How It Works**:
1. Provider detects spanned relationships
2. Master clip created
3. All segments linked
4. Import creates multi-component shape

**Example (RED)**:
```
A001_C001_001.R3D  <- Master
A001_C001_002.R3D  <- Spanned segment
A001_C001_003.R3D  <- Spanned segment
```

**Result**: Single item with all segments as one continuous media

### Multi-Component Import

**Formats with Separate A/V**:
- Panasonic P2 (separate audio MXF files)
- XDCAM (separate audio tracks)
- Custom formats with external audio

**How It Works**:
1. Provider identifies the main media file and the additional ones
2. The ingest DECLARES up front how many components the shape must
   expect, and Vidispine promotes it only once every declared slot is
   filled:
   - 1 container component (the main file always fills this)
   - N components of each extra file's own type (audio or video)
   - plus 1 video component for the main file itself — **only when the
     provider says Vidispine can decode it**, see "Sources Vidispine
     cannot decode" below
3. The extra components are imported, then the main file LAST

A slot declared for essence that never arrives leaves the item holding
all its media on a shape that is never promoted, never tagged `original`
and never transcoded — with no error anywhere. That is why the count is
a branch and not a constant.

**Result**: Proper multi-track audio in Vidispine

**The import WAITS for the extra components.**
A multi-component import declares up front how many components the shape
must expect, imports every extra component, and imports the main file
LAST. The main file's job is the only thing that evaluates the
placeholder, creates the shape and starts the transcode — and nothing
re-evaluates a placeholder afterwards. So the ingest now blocks until
every extra component has actually attached its file before the main
file is imported. In the normal case that is a matter of seconds: the
files are already on the storage and already hashed.

**What a wedged Vidispine costs**, and it is not the same on both entry
points:

| Entry point | Per clip | All the waits of one call | Why |
|-------------|----------|---------------------------|-----|
| Scan (cron / command line) | 300 s | unbounded | Nothing is waiting on the run; it can afford to sit on a clip. |
| REST (`/tapelessingest/api/...`) | 30 s | 60 s | The call holds a request thread and its database connection for the whole wait. |

The REST column has two numbers because a REST call does not ingest one
clip: both ingest endpoints run many. All of one call's component waits
share a 60 s **component-wait budget**, and every per-clip bound is
clamped against what is left of it — so a 50-clip folder cannot cost
50 x 30 s on a held request thread. The first clips may spend the full
per-clip bound; the ones after them get whatever remains, and the ones
past the budget do not wait at all.

It bounds the **waiting**, not the request. A REST ingest also scans the
folder, extracts metadata and resolves collections, and none of that is
inside the 60 s — the call can take longer than a minute, it just cannot
spend longer than a minute waiting for components. The budget starts
when the ingest does, after the scan, so a large healthy folder cannot
spend it in discovery and leave every clip a 0 s wait.

The cron has no such budget: it holds nothing anyone is waiting on. Note
that this also means a cron run has no aggregate bound — 300 s per clip
against a wedged Vidispine is 300 s x the number of multi-component
clips, and the error cap keeps the report short without making the run
short. Watch the wall-clock time of a run whose report says components
never landed.

If the bound expires, the main file is **NOT** imported and the clip is
counted `failed` with the reason in the run's error list (and therefore
in the run summary's error count, not only in `portal.log`). Importing
the main file anyway would leave the item holding all its media on a
shape that is never promoted, never tagged `original` and never
transcoded — which is the failure this bound exists to prevent.

Only the first 10 such reasons per folder are listed, followed by a
count of the rest: a wedged Vidispine fails every clip of a card the
same way, and 200 identical lines would bury the rest of the report.
Every one of them is still in `portal.log`.

**A failed multi-component import leaves a RESUMABLE item.** Whatever
components did attach stay attached, and the next run imports only what
is missing and then the main file. It also accounts for the components
whose import job is **still running** from the failed run: those files
are not on the shape yet, but re-importing them would attach the same
media twice and Vidispine refuses the duplicate, so the next run waits
for the running job instead of starting a rival. A clip can therefore
need one more run than you expect — one to let the stuck job finish, one
to finish the import — and each of them reports why.

Two states are not resumed:

- **the placeholder already holds *every* file of the clip and is still
  a placeholder.** Vidispine is waiting on a component slot nothing will
  fill; no re-run can change that, and the shape has to be removed by
  hand in the Vidispine admin. Reported by name, with the files it
  holds. (`--replace` does *not* help here: its shape query only sees
  non-placeholder shapes, so a stuck item exposes no shape to remove.)
- **the placeholder holds a file that is not this clip's.** That is not
  a partly finished import of this clip at all, so nothing is imported
  into it. The report names the foreign file ids; check which item they
  belong to before re-running.

**Sources Vidispine cannot decode.** Some media yields no video essence
to Vidispine's shape deduction — every `.R3D` whose start timecode
carries the drop-frame flag, for instance. Those produce a *binary*
component, which satisfies the container slot and no video slot, so the
declared component count leaves the video slot out for them. The
provider decides this (`red` reads it from REDline's `Abs TC`); every
other provider keeps counting its main file as a video contributor.

### Dry Run Mode

Test import without actually ingesting:

```
POST /tapelessingest/api/folder/{folder_id}/ingest/
{
  "dry_run": true
}
```

**Returns**:
- Scan results
- Clips that would be ingested
- No actual import performed

**Use Cases**:
- Test provider detection
- Verify metadata extraction
- Check collection creation

## Troubleshooting

### Issue: No Clips Detected

**Symptoms**: Scan returns 0 clips

**Possible Causes**:
1. Files not indexed in Elasticsearch
2. Wrong provider selected
3. Folder path incorrect

**Solutions**:
1. Check Vidispine file indexing:
   ```
   GET /API/storage/{storage_id}/file/?path={path}
   ```
2. Try scan without provider filter
3. Verify folder path and storage ID

### Issue: Metadata Not Extracted

**Symptoms**: Clips found but no metadata

**Possible Causes**:
1. XML/metadata files missing
2. Provider not recognizing format
3. Metadata mapping not configured

**Solutions**:
1. Verify metadata files exist alongside media
2. Check provider implementation
3. Review metadata mappings configuration
4. Check Portal logs for parsing errors

### Issue: Import Fails

**Symptoms**: Status shows failed, no item created

**Possible Causes**:
1. File not accessible
2. Permissions issue
3. Vidispine error
4. Storage not configured properly

**Solutions**:
1. Check file exists and is readable
2. Verify user has ingest permissions
3. Review Vidispine job logs
4. Check storage methods (browse enabled)
5. Review Portal error logs

### Issue: A clip is `failed` and its media is already on the item

**Symptoms**: the run reports the clip `failed` with a reason mentioning
components, the item exists, and some or all of its files are attached
to a shape that is still a placeholder (no `original` tag, no proxy).

This is the multi-component import stopping deliberately rather than
finishing a shape nothing could promote. Read the reason in the run's
error list — it says which of the three states you are in:

1. **"extra component job(s) were still running after Ns"** — the wait
   expired. Nothing is wrong with the item: the components that landed
   stay attached and **the next run finishes it**. If it recurs, the
   Vidispine job queue is the thing to look at, not the plugin.
2. **"is still being imported by job VX-…"** — a previous run's import
   is still in flight. Again nothing to do: let that job finish and
   re-run. A clip can legitimately need one extra run for this.
3. **"STILL a placeholder"** / **"the shape is still a placeholder"** —
   the dead end, and the only one needing hands. Every slot Vidispine
   was told to expect can no longer be filled, or the anchor has already
   been imported — by this run's wait catching an earlier run's anchor
   job, or by an earlier run outright — and nothing re-evaluates a
   placeholder afterwards. The clip is reported `failed` on every run
   until someone acts, deliberately: it is never counted ingested on
   the strength of a landed anchor alone. No re-run can fix it:
   **remove the shape by hand in the Vidispine admin**, then re-scan.
   `--replace` does *not* help — its shape query only sees
   non-placeholder shapes, so a stuck item exposes nothing to remove.

A fourth message, **"holds N file(s) that are not this clip's"**, means
the item is not the one this clip belongs to. Find out which item those
file ids belong to before re-running anything.

A fifth message, **"the placeholder shape could not be read"**, means the
run could not tell whether the components attached at all: the shape
query itself did not answer. The main file is deliberately **not**
imported on an unknown state — closing a component set blind is how an
unpromotable placeholder gets made — so nothing has been damaged and the
next run resumes the clip. This one points at Vidispine's availability,
not at the item: if it recurs for every clip, check that Vidispine is
answering `GET /API/item/{id}/shape` at all before looking at the job
queue. Do **not** read it as "the components never attached" — that is a
different message, and this run never observed it.

A sixth message, **"the provider could not tell whether the anchor …
contributes a video component"**, means the provider declared that it
could not answer the question the component budget depends on — for a
RED clip, the `timecode` metadata (REDline's `Abs TC`) could not be
read as either `HH:MM:SS:FF` or `HH.MM.SS.FF`. The run refuses to
declare a component budget on a guess: over-declaring makes the dead
end above, under-declaring is Vidispine's refusal, and neither is
knowable without the evidence. Nothing was imported and nothing was
declared, so the item is a clean placeholder: fix the clip's metadata
(re-scan the folder if REDline can read the file) and the next run
imports it fresh. `portal.log` carries the provider's matching warning
— `red: clip <umid> … unreadable timecode` with the value it saw. This
refusal is made only when the run is about to **declare**: a clip
resumed into a shape that already holds a file keeps the budget the
first run declared, and is not refused on a verdict that run is not
going to use.

### Issue: Spanned Clips Not Working

**Symptoms**: Each segment imported as separate item

**Possible Causes**:
1. Provider not detecting spanned relationship
2. Missing metadata
3. Incorrect file naming

**Solutions**:
1. Verify file naming follows format convention
2. Check provider spanned clip logic
3. Review metadata files
4. Contact support if format not supported

### Issue: Wrong Collection Created

**Symptoms**: Items in unexpected collections

**Possible Causes**:
1. Collection rules misconfigured
2. Folder filtering not working
3. Cache issue

**Solutions**:
1. Review `collections_ignore_folder` patterns
2. Check `collections_rename_folder` mappings
3. Clear cache (restart Portal)
4. Test with `dry_run` first

## Best Practices

### Organization

**Folder Structure**:
```
Storage/
├── ProjectA/
│   ├── Day1/
│   │   ├── CARD_A/
│   │   └── CARD_B/
│   └── Day2/
└── ProjectB/
    └── Footage/
```

**Collection Result**:
```
- Project A
  - Day 1
    - Card A
    - Card B
  - Day 2
- Project B
  - Footage
```

### Batch Sizing

**Recommended Sizes**:
- **Small files (<100MB)**: 50-100 clips per batch
- **Medium files (100MB-1GB)**: 20-50 clips per batch
- **Large files (>1GB)**: 10-20 clips per batch
- **Very large files (>10GB)**: 5-10 clips per batch

**Considerations**:
- System resources (CPU, memory)
- Storage bandwidth
- Transcode queue size
- Network speed

### Metadata Mapping

**Essential Mappings**:
```
shooting_date     -> created
timecode          -> startTimecode
clipname          -> title
device_model      -> originalFormat
device_serial     -> originalId
```

**Optional Mappings**:
```
framerate         -> originalVideoField/originalFrameRate
video_codec       -> originalVideoField/originalVideoCodec
aspect_ratio      -> originalVideoField/aspectRatio
user_clip_name    -> description
```

### Performance Optimization

**Storage Performance**:
- Use fast network storage
- Enable caching if available
- Ensure adequate bandwidth

**Elasticsearch**:
- Keep index up to date
- Monitor query performance
- Consider dedicated ES instance for large deployments

**Vidispine**:
- Configure transcode profiles appropriately
- Balance transcode priority
- Monitor job queue depth

### Monitoring

**Key Metrics**:
- Clips scanned per hour
- Import success rate
- Average import time
- Failed imports
- Storage space used

**Log Locations**:
- Portal logs: `/var/log/cantemo/portal.log`
- Vidispine logs: Check Vidispine server
- Apache/Nginx logs: Web server logs

## Support

### Getting Help

1. **Check Logs**: Review Portal and Vidispine logs
2. **Test Isolation**: Use dry run and small batches
3. **Documentation**: Review API docs and provider docs
4. **Contact Support**: Provide logs and reproduction steps

### Reporting Issues

Include:
- Plugin version
- Portal version
- Provider name
- Sample folder structure
- Error messages from logs
- Steps to reproduce

### Feature Requests

Contact: Camille Darley - Productions Autrement Dit
Email: [Contact via studiopad.fr](http://www.studiopad.fr)

## Appendix

### Supported Metadata Fields

| Field | Description | Type |
|-------|-------------|------|
| umid | Unique Material Identifier | String |
| clipname | Clip name | String |
| duration | Duration in frames | Integer |
| timecode | Starting timecode | String (HH:MM:SS:FF) |
| framerate | Frame rate | Float |
| shooting_date | Recording date | Date |
| device_manufacturer | Camera manufacturer | String |
| device_model | Camera model | String |
| device_serial | Camera serial number | String |
| video_codec | Video codec | String |
| aspect_ratio | Aspect ratio | String |
| creation_date | File creation date | Date |
| last_update_date | Last modified date | Date |
| user_clip_name | User-assigned name | String |

### Provider Capabilities Matrix

| Provider | Spanned | Multi-Audio | XML | UMID |
|----------|---------|-------------|-----|------|
| RED | Yes | Yes | Yes | Yes |
| XDCAM | No | Yes | Yes | Yes |
| P2 | No | Yes | Yes | Yes |
| HDSLR | No | No | Partial | No* |
| AVCHD | No | Yes | Yes | Partial |
| Atomos | No | No | Partial | No* |
| Zoom | No | No | No | No* |
| File | No | No | No | No* |

\* UMID generated from file hash

### Keyboard Shortcuts (UI)

When using the admin interface:

- **Ctrl+S**: Scan folder
- **Ctrl+I**: Ingest folder
- **Ctrl+R**: Refresh view
- **Esc**: Close modal

### URL Patterns

- Folders: `/tapelessingest/folders/`
- Clips: `/tapelessingest/clips/`
- Settings: `/tapelessingest/settings/`
- Admin: `/admin/TapelessIngest/`

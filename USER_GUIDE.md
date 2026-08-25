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
1. Provider identifies main video file
2. Provider identifies additional audio files
3. Import creates shape with:
   - 1 video component
   - N audio components

**Result**: Proper multi-track audio in Vidispine

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

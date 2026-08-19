# TapelessIngest - Data Models

## Overview

TapelessIngest uses Django ORM models for data persistence. The models represent camera clips, scanned folders, plugin settings, and ingest job tracking.

## Entity Relationship Diagram

```
┌─────────────────┐       ┌─────────────────┐
│     Folder      │       │     Settings    │
├─────────────────┤       ├─────────────────┤
│ id (UUID) PK    │       │ id PK           │
│ path            │       │ storage_id      │
│ storage_id      │       │ tmp_storage     │
│ collection_id   │       │ base_folder     │
│ clips_total     │       │ ffmpeg_path     │
│ provider_names  │       │ ...             │
│ created_on      │       └─────────────────┘
│ scanned_on      │
└────────┬────────┘       ┌─────────────────┐
         │                │ MetadataMapping │
         │                ├─────────────────┤
         │                │ id PK           │
         ▼                │ metadata_provider│
┌─────────────────┐       │ metadata_portal │
│      Clip       │       └─────────────────┘
├─────────────────┤
│ umid PK         │       ┌─────────────────┐
│ path            │       │ AggregateIngestJob│
│ storage_id      │       ├─────────────────┤
│ folder_path     │       │ id PK           │
│ provider_name   │       │ user FK         │
│ status          │       │ task_id         │
│ item_id         │       └─────────────────┘
│ file_id         │
│ job_id          │       ┌─────────────────┐
│ collection_id   │       │  IngestClipJob  │
│ ...             │◄──────┤ clip PK/FK      │
└────────┬────────┘       │ status          │
         │                │ progress        │
         │                │ error           │
         ▼                └────────┬────────┘
┌─────────────────┐                │
│  ClipMetadata   │                ▼
├─────────────────┤       ┌─────────────────┐
│ id PK           │       │  IngestTaskJob  │
│ clip FK         │       ├─────────────────┤
│ name            │       │ id PK           │
│ value           │       │ job FK          │
└─────────────────┘       │ task_name       │
                          │ status          │
┌─────────────────┐       │ progress        │
│  SpannedClips   │       │ error           │
├─────────────────┤       └─────────────────┘
│ id PK           │
│ clip FK         │       ┌─────────────────┐
│ spanned_clip FK │       │      Reel       │
│ order           │       ├─────────────────┤
└─────────────────┘       │ umid PK         │
                          │ folder_path     │
                          │ media_xml       │
                          │ created_on      │
                          └─────────────────┘
```

## Model Definitions

### Clip

**Location**: `models/clip.py`

Represents a camera clip detected during folder scanning.

| Field | Type | Description |
|-------|------|-------------|
| `umid` | CharField(100) **PK** | Unique Material Identifier |
| `created_on` | DateTimeField | Creation timestamp |
| `imported_on` | DateTimeField | Import timestamp |
| `user` | ForeignKey(User) | Importing user |
| `path` | TextField | Relative file path |
| `storage_id` | CharField(255) | Vidispine storage ID |
| `folder_path` | TextField | Parent folder path |
| `output_file` | TextField | Wrapped output file path |
| `file_id` | CharField(100) | Vidispine file ID |
| `status` | IntegerField | Import status code |
| `progress` | IntegerField | Ingest progress (0-100) |
| `spanned` | BooleanField | Is part of spanned clip |
| `master_clip` | CharField(100) | Master clip UMID (if spanned) |
| `provider_name` | CharField(100) | Provider machine name |
| `collection_id` | TextField | Target collection ID |
| `item_id` | CharField(100) | Vidispine item ID |
| `job_id` | CharField(100) | Vidispine job ID |
| `reference_file` | CharField(100) | Reference file ID |

**Status Codes**:
| Code | Constant | Description |
|------|----------|-------------|
| 0 | `STATUS_NOT_IMPORTED` | Not yet imported |
| 1 | `STATUS_WRAPPED` | MXF wrapped |
| 2 | `STATUS_REGISTERED` | File registered |
| 3 | `STATUS_PLACEHOLDER_CREATED` | Placeholder exists |
| 4 | `STATUS_IMPORTED` | Fully imported |

### Folder

**Location**: `models/folder.py`

Represents a scanned folder containing camera media.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUIDField **PK** | Auto-generated UUID |
| `created_on` | DateTimeField | Creation timestamp |
| `scanned_on` | DateTimeField | Last scan timestamp |
| `path` | TextField | Folder path on storage |
| `clips_total` | IntegerField | Total clips found |
| `storage_id` | CharField(255) | Vidispine storage ID |
| `collection_id` | TextField | Associated collection ID |
| `provider_names` | CharField(100) | Comma-separated provider list |

**Unique Constraint**: `(path, storage_id)`

### Settings

**Location**: `models/settings.py`

Plugin configuration (singleton, pk=1).

| Field | Type | Description |
|-------|------|-------------|
| `storage_id` | CharField(255) | Default storage ID |
| `tmp_storage` | CharField(255) | Temporary storage ID |
| `bmxtranswrap` | CharField(255) | BMX transwrap binary path |
| `mxf2raw` | CharField(255) | MXF2RAW binary path |
| `ffmpeg_path` | CharField(255) | FFmpeg binary path |
| `base_folder` | CharField(255) | Base folder for scanning |
| `collections_ignore_folder_str` | TextField | Folders to ignore (CSV) |
| `collections_rename_folder_str` | TextField | Folder rename rules |

### MetadataMapping

**Location**: `models/settings.py`

Maps provider metadata fields to Portal fields.

| Field | Type | Description |
|-------|------|-------------|
| `id` | AutoField **PK** | Auto ID |
| `metadata_provider` | CharField(200) | Provider field name |
| `metadata_portal` | CharField(100) | Portal/Vidispine field |

### IngestClipJob

**Location**: `models/models.py`

Tracks ingest job for a clip.

| Field | Type | Description |
|-------|------|-------------|
| `clip` | OneToOneField(Clip) **PK** | Related clip |
| `status` | CharField(48) | Job status |
| `progress` | IntegerField | Overall progress |
| `error` | CharField(255) | Error message |
| `exception` | CharField(65535) | Exception details |
| `created_date` | DateTimeField | Job creation time |
| `modified_date` | DateTimeField | Last update time |

### IngestTaskJob

**Location**: `models/models.py`

Individual task within an ingest job.

| Field | Type | Description |
|-------|------|-------------|
| `id` | AutoField **PK** | Auto ID |
| `job` | ForeignKey(IngestClipJob) | Parent job |
| `task_name` | CharField(255) | Task description |
| `status` | CharField(48) | Task status |
| `progress` | IntegerField | Task progress |
| `error` | CharField(255) | Error message |
| `exception` | CharField(65535) | Exception details |
| `created_date` | DateTimeField | Task creation time |
| `modified_date` | DateTimeField | Last update time |

### ClipMetadata

**Location**: `models/clip.py`

Key-value metadata storage for clips.

| Field | Type | Description |
|-------|------|-------------|
| `id` | AutoField **PK** | Auto ID |
| `clip` | ForeignKey(Clip) | Parent clip |
| `name` | CharField | Metadata key |
| `value` | TextField | Metadata value |

### SpannedClips

**Location**: `models/clip.py`

Links clips that span multiple files.

| Field | Type | Description |
|-------|------|-------------|
| `id` | AutoField **PK** | Auto ID |
| `clip` | ForeignKey(Clip) | Master clip |
| `spanned_clip` | ForeignKey(Clip) | Spanned segment |
| `order` | IntegerField | Segment order |

### Reel

**Location**: `models/clip.py`

Camera reel tracking.

| Field | Type | Description |
|-------|------|-------------|
| `umid` | CharField(100) **PK** | Reel UMID |
| `created_on` | DateTimeField | Creation time |
| `folder_path` | TextField | Reel folder path |
| `media_xml` | TextField | XML metadata |

## Migrations

Located in `migrations/` directory:

| Migration | Description |
|-----------|-------------|
| `0001_initial` | Initial schema |
| `0002-0008` | Early field additions |
| `0009` | Collections ignore folder |
| `0010` | Collections rename folder |
| `0011-0012` | Auto migrations |
| `0013` | Folder clips total |
| `0014` | Clip folders |
| `0015` | Remove clip folder |
| `0016` | Auto migrations |

## Relationships

1. **Folder → Clips**: One folder contains many clips (`Clip.folder_path`)
2. **Clip → ClipMetadata**: One clip has many metadata entries
3. **Clip → SpannedClips**: One master clip links to many segments
4. **Clip → IngestClipJob**: One-to-one job tracking
5. **IngestClipJob → IngestTaskJob**: One job has many tasks

## Indexes

Default Django indexes on:
- Primary keys
- Foreign keys
- Unique constraints

Consider adding indexes on:
- `Clip.storage_id`
- `Clip.folder_path`
- `Clip.status`
- `Folder.storage_id`

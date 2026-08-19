# TapelessIngest - API Contracts

## Overview

TapelessIngest exposes a REST API using Django REST Framework. All endpoints require Cantemo Portal authentication.

## Base URL

```
/tapelessingest/
```

## Authentication

All endpoints require Portal authentication:

```http
Authorization: Basic <base64_credentials>
```

Or session-based authentication via Portal login.

## Endpoints

### Clips Browser

#### Scan/Ingest Clips

```http
PUT /tapelessingest/api/browser/clips
POST /tapelessingest/api/browser/clips
```

**PUT - Scan folder for clips**

Request:
```json
{
  "paths": [
    {
      "path": "/DCIM/100MEDIA",
      "storage": "VX-1"
    }
  ],
  "page": 1,
  "number": 25,
  "cursor": null
}
```

Response:
```json
{
  "folder": {
    "umid": "550e8400-e29b-41d4-a716-446655440000",
    "type": "folder",
    "path": "/DCIM/100MEDIA",
    "storage_id": "VX-1",
    "clips_total": 15,
    "collection_id": null,
    "provider_names": "",
    "providers": []
  },
  "clips": [
    {
      "umid": "abc123",
      "type": "clip",
      "path": "/DCIM/100MEDIA/A001.R3D",
      "storage_id": "VX-1",
      "provider_name": "red",
      "status": 0,
      "status_readable": "Not imported",
      "metadatas": {
        "clipname": "A001",
        "shooting_date": "2025-01-15T10:30:00Z"
      },
      "thumbnail_url": "/tapelessingest/clips/abc123/thumbnail",
      "item_id": null
    }
  ],
  "subfolders": [],
  "hits": 15,
  "page": 1,
  "number": 25,
  "next": 2,
  "pages": 1
}
```

**POST - Ingest clips**

Request:
```json
{
  "folder": {
    "path": "/DCIM/100MEDIA",
    "storage_id": "VX-1"
  },
  "clips": [
    {
      "umid": "abc123",
      "path": "/DCIM/100MEDIA/A001.R3D",
      "storage_id": "VX-1",
      "provider_name": "red",
      "metadatas": {}
    }
  ]
}
```

Or ingest all clips:
```json
{
  "folder": { ... },
  "clips": "__all__"
}
```

Response (201 Created):
```json
[
  {
    "umid": "abc123",
    "status": 3,
    "status_readable": "Placeholder created",
    "item_id": "VX-123",
    "job_id": "VX-456"
  }
]
```

### Job Progress

#### Get Job Status

```http
PUT /tapelessingest/api/browser/clips/jobs
```

Request:
```json
{
  "jobs_ids": ["VX-456", "VX-457"]
}
```

Response:
```json
{
  "VX-456": {
    "id": "VX-456",
    "progress": 75,
    "status": "STARTED",
    "type": "PLACEHOLDER_IMPORT"
  },
  "VX-457": {
    "id": "VX-457",
    "progress": 100,
    "status": "FINISHED",
    "type": "PLACEHOLDER_IMPORT"
  }
}
```

### File Notifications

#### Vidispine File Notification

```http
POST /tapelessingest/notification/file/created
```

Request (from Vidispine):
```json
{
  "field": [
    {"key": "fileId", "value": "VX-789"},
    {"key": "action", "value": "NEW"},
    {"key": "storageId", "value": "VX-1"}
  ]
}
```

Response:
```json
{"ok": true}
```

### Thumbnails & Proxies

#### Get Clip Thumbnail

```http
GET /tapelessingest/clips/{clip_id}/thumbnail
```

Response: `image/png` or `image/jpeg`

#### Get Clip Proxy

```http
GET /tapelessingest/clips/{clip_id}/proxy
```

Response: `video/mp4` or appropriate mime type

#### Get Clip Preview Page

```http
GET /tapelessingest/clips/{clip_id}/preview
```

Response: HTML page with video player

#### Get File Thumbnail

```http
GET /tapelessingest/file/{file_id}/thumbnail
```

Response: `image/jpeg` (generated via FFmpeg)

### Settings

#### Admin Settings Page

```http
GET /tapelessingest/admin/
POST /tapelessingest/admin/
```

**GET** - Render settings form

**POST** - Save settings

Form fields:
- `settings-storage_id`: Default storage
- `settings-tmp_storage`: Temporary storage
- `settings-base_folder`: Base folder path
- `settings-ffmpeg_path`: FFmpeg path
- `metadata-*`: Metadata mapping formset

## Data Types

### Clip Object

```typescript
interface Clip {
  umid: string;              // Primary key
  type: "clip";              // Always "clip"
  created_on: string;        // ISO datetime
  imported_on: string | null;
  user: number | null;       // User ID
  path: string;              // File path
  storage_id: string;        // Storage ID
  folder_path: string;
  output_file: string | null;
  file_id: string | null;
  status: number;            // 0-4
  progress: number | null;
  spanned: boolean;
  master_clip: string | null;
  provider_name: string;
  collection_id: string | null;
  item_id: string | null;
  job_id: string | null;
  reference_file: string | null;
  
  // Computed fields
  absolute_url: string;
  resource_uri: string;
  thumbnail_url: string;
  metadatas: Record<string, string>;
  media_files: MediaFile[];
  spanned_clips: SpannedClip[];
  state: string;
  error: string | null;
  job: Job | null;
  status_readable: string;
  duration_readable: string;
}
```

### Folder Object

```typescript
interface Folder {
  umid: string;              // UUID
  type: "folder";            // Always "folder"
  created_on: string;        // ISO datetime
  scanned_on: string | null;
  path: string;
  storage_id: string;
  clips_total: number;
  collection_id: string | null;
  provider_names: string;
  providers: string[];
  metadatas: {
    clipname: string;
    shooting_date: string;
  };
  error: string | null;
}
```

### Job Object

```typescript
interface Job {
  id: string;
  progress: number;
  status: "PENDING" | "STARTED" | "FINISHED" | "FAILED" | "NOT_FOUND";
  type: string;
}
```

## Error Responses

### 400 Bad Request
```json
{
  "error": "You have to provide at least one clip"
}
```

### 500 Internal Server Error
```json
{
  "errors": ["Error message"]
}
```

### 204 No Content
Returned when required parameters are missing.

## Pagination

List endpoints support pagination:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page` | int | 1 | Page number |
| `number` | int | 25 | Items per page |
| `cursor` | string | null | Cursor for continuation |

Response includes:
```json
{
  "hits": 100,
  "page": 1,
  "number": 25,
  "next": 2,
  "pages": 4
}
```

## Rate Limiting

No explicit rate limiting. Subject to Portal/Vidispine API limits.

## See Also

- [API.md](../API.md) - Detailed API documentation in project root
- [USER_GUIDE.md](../USER_GUIDE.md) - Workflow examples

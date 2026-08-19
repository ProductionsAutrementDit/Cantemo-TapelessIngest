# API Documentation

## Overview

The TapelessIngest plugin provides a REST API for scanning folders, managing clips, and performing ingestion operations. All endpoints require authentication and appropriate permissions.

## Base URL

```
/tapelessingest/
```

## Authentication

All API endpoints require authentication using Cantemo Portal credentials. Include authentication headers with all requests:

```http
Authorization: Basic <base64_credentials>
```

## Endpoints

### Folder Operations

#### Create/Get Folder
```http
POST /tapelessingest/api/folder/
GET  /tapelessingest/api/folder/{folder_id}/
```

**Create Folder Request Body:**
```json
{
  "storage_id": "VX-1",
  "path": "/path/to/media",
  "provider_names": "red,xdcam,hdslr"  // optional
}
```

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "storage_id": "VX-1",
  "path": "/path/to/media",
  "created_on": "2025-01-15T10:30:00Z",
  "scanned_on": null,
  "clips_total": 0,
  "provider_names": "",
  "collection_id": null
}
```

#### List Folders
```http
GET /tapelessingest/api/folders/
```

**Query Parameters:**
- `storage_id` (optional) - Filter by storage ID
- `page` (optional) - Page number for pagination
- `page_size` (optional) - Results per page

**Response:**
```json
{
  "count": 25,
  "next": "/tapelessingest/api/folders/?page=2",
  "previous": null,
  "results": [
    {
      "id": "...",
      "storage_id": "VX-1",
      "path": "/path/to/media",
      "clips_total": 15
    }
  ]
}
```

#### Scan Folder
```http
POST /tapelessingest/api/folder/{folder_id}/scan/
```

Scans folder for clips using configured providers. Does not perform ingestion.

**Request Body:**
```json
{
  "providers": ["red", "xdcam"],  // optional, defaults to all
  "first": 0,                     // optional, pagination offset
  "number": 100,                  // optional, clips to process (0 = all)
  "legacy_storages": ["VX-2"]     // optional, for hash matching
}
```

**Response:**
```json
{
  "hits": 15,
  "processed": 15,
  "created": 10,
  "already_ingested": 5,
  "errors": [],
  "clips": [
    {
      "umid": "060A2B340101010101010F001300000...",
      "reference_file": "A001_C001.XML",
      "path": "/CARD_A/Clip",
      "provider_name": "red",
      "status": 0,
      "metadatas": {
        "clipname": "A001_C001",
        "duration": "1440",
        "timecode": "01:00:00:00",
        "framerate": "24"
      }
    }
  ]
}
```

**Response Fields:**
- `hits` - Total clips found
- `processed` - Clips processed in this request
- `created` - New clips created
- `already_ingested` - Clips already imported
- `errors` - List of error messages
- `clips` - Array of clip objects (omitted if `number=0`)

#### Get Folder Clips
```http
GET /tapelessingest/api/folder/{folder_id}/clips/
```

Retrieves clips associated with folder.

**Query Parameters:**
- `first` (optional) - Offset for pagination
- `number` (optional) - Number of clips to return
- `status` (optional) - Filter by status (0-4)

**Response:**
```json
{
  "clips": [...],
  "total": 15
}
```

#### Ingest Folder
```http
POST /tapelessingest/api/folder/{folder_id}/ingest/
```

Scans folder and ingests clips to Vidispine.

**Request Body:**
```json
{
  "providers": ["red"],           // optional
  "first": 0,                     // optional
  "number": 10,                   // optional, clips per batch
  "replace": false,               // optional, replace existing items
  "legacy_storages": ["VX-2"],   // optional
  "dry_run": false                // optional, scan only without ingesting
}
```

**Response:**
```json
{
  "hits": 10,
  "processed": 10,
  "created": 8,
  "already_ingested": 2,
  "ingested": 7,
  "skipped": 2,
  "failed": 0,
  "replaced": 1,
  "errors": []
}
```

**Response Fields:**
- `ingested` - Clips successfully imported
- `skipped` - Clips skipped (already imported)
- `failed` - Clips that failed import
- `replaced` - Clips where source was replaced

**Status Codes:**
- `200` - Success
- `400` - Invalid request parameters
- `404` - Folder not found
- `500` - Server error

#### Get Subfolders
```http
GET /tapelessingest/api/folder/{folder_id}/subfolders/
```

Lists immediate subdirectories of folder.

**Response:**
```json
{
  "folders": [
    {
      "storage_id": "VX-1",
      "path": "/path/to/media/CARD_A",
      "is_new": true
    }
  ]
}
```

### Clip Operations

#### Get Clip
```http
GET /tapelessingest/api/clip/{umid}/
```

Retrieves detailed clip information.

**Response:**
```json
{
  "umid": "060A2B340101010101010F001300000...",
  "created_on": "2025-01-15T10:30:00Z",
  "imported_on": "2025-01-15T11:00:00Z",
  "folder_path": "/CARD_A/Clip",
  "path": "/CARD_A/Clip",
  "storage_id": "VX-1",
  "file_id": "VX-123456",
  "reference_file": "A001_C001.XML",
  "status": 4,
  "status_label": "Imported",
  "provider_name": "red",
  "collection_id": "VX-789",
  "item_id": "VX-456",
  "job_id": "VX-999",
  "spanned": false,
  "master_clip": false,
  "metadatas": {
    "clipname": "A001_C001",
    "duration": "1440",
    "timecode": "01:00:00:00",
    "framerate": "24",
    "device_model": "RED KOMODO",
    "device_serial": "123456"
  },
  "media_files": [
    {
      "file_id": "VX-123456",
      "path": "/CARD_A/R3D/A001_C001.R3D",
      "type": "video"
    }
  ]
}
```

**Clip Status Values:**
- `0` - Not imported
- `1` - Wrapped (output file created)
- `2` - Registered (file registered in Vidispine)
- `3` - Placeholder created
- `4` - Imported (fully imported with transcode)

#### List Clips
```http
GET /tapelessingest/api/clips/
```

Lists all clips with filtering and pagination.

**Query Parameters:**
- `storage_id` - Filter by storage
- `provider` - Filter by provider name
- `status` - Filter by status (0-4)
- `folder` - Filter by folder ID
- `item_id` - Filter by Vidispine item ID
- `search` - Search in clip name
- `page` - Page number
- `page_size` - Results per page

**Response:**
```json
{
  "count": 150,
  "next": "...",
  "previous": "...",
  "results": [...]
}
```

#### Get Clip Thumbnail
```http
GET /tapelessingest/api/clip/{umid}/thumbnail/
```

Returns thumbnail image for clip.

**Response:**
- Content-Type: `image/jpeg`
- Image data or placeholder if unavailable

#### Get Clip Jobs
```http
GET /tapelessingest/api/clip/{umid}/jobs/
```

Lists Vidispine jobs related to clip's item.

**Response:**
```json
{
  "jobs": [
    {
      "id": "VX-123",
      "type": "IMPORT",
      "state": "FINISHED",
      "rawstatus": "FINISHED",
      "user": "admin",
      "startTime": "2025-01-15 11:00:00",
      "joblink": "/job/VX-123/",
      "in_progress": false,
      "priority": "MEDIUM",
      "transcodeProgress": "100"
    }
  ]
}
```

#### Ingest Single Clip
```http
POST /tapelessingest/api/clip/{umid}/ingest/
```

Ingests a single clip to Vidispine.

**Request Body:**
```json
{
  "collection_id": "VX-789",      // optional
  "replace": false,                // optional
  "legacy_storages": ["VX-2"]     // optional
}
```

**Response:**
```json
{
  "skipped": false,
  "failed": false,
  "replaced": false,
  "ingested": true,
  "item_id": "VX-456",
  "job_id": "VX-999",
  "message": "Clip successfully ingested"
}
```

### Settings Operations

#### Get Settings
```http
GET /tapelessingest/api/settings/
```

Retrieves plugin settings.

**Response:**
```json
{
  "base_folder": "/mnt/ingest",
  "collections_ignore_folder": [
    "PROXY",
    "THUMB",
    "\\..*"
  ],
  "collections_rename_folder": {
    "MEDIA": "Media Files",
    "RUSHES": "Dailies"
  }
}
```

#### Update Settings
```http
PUT /tapelessingest/api/settings/
```

Updates plugin settings.

**Request Body:**
```json
{
  "base_folder": "/mnt/ingest",
  "collections_ignore_folder": ["PROXY"],
  "collections_rename_folder": {"MEDIA": "Media"}
}
```

### Metadata Mapping Operations

#### List Metadata Mappings
```http
GET /tapelessingest/api/metadata-mappings/
```

Lists all metadata field mappings.

**Response:**
```json
{
  "mappings": [
    {
      "id": 1,
      "metadata_provider": "shooting_date",
      "metadata_portal": "created"
    },
    {
      "id": 2,
      "metadata_provider": "device_model",
      "metadata_portal": "originalFormat"
    }
  ]
}
```

#### Create Metadata Mapping
```http
POST /tapelessingest/api/metadata-mappings/
```

Creates new metadata field mapping.

**Request Body:**
```json
{
  "metadata_provider": "shooting_date",
  "metadata_portal": "created"
}
```

#### Delete Metadata Mapping
```http
DELETE /tapelessingest/api/metadata-mappings/{id}/
```

Removes metadata field mapping.

## Common Response Codes

- **200 OK** - Request successful
- **201 Created** - Resource created successfully
- **400 Bad Request** - Invalid request parameters
- **401 Unauthorized** - Authentication required
- **403 Forbidden** - Insufficient permissions
- **404 Not Found** - Resource not found
- **500 Internal Server Error** - Server error

## Error Response Format

```json
{
  "error": "Error message description",
  "details": {
    "field": "Additional error details"
  }
}
```

## Usage Examples

### Example 1: Scan and Ingest Folder

```python
import requests
from requests.auth import HTTPBasicAuth

BASE_URL = "https://portal.example.com/tapelessingest/api"
auth = HTTPBasicAuth('username', 'password')

# Create folder
folder_data = {
    "storage_id": "VX-1",
    "path": "/CARD_A"
}
response = requests.post(
    f"{BASE_URL}/folder/",
    json=folder_data,
    auth=auth
)
folder = response.json()
folder_id = folder['id']

# Scan folder
scan_response = requests.post(
    f"{BASE_URL}/folder/{folder_id}/scan/",
    json={"providers": ["red"]},
    auth=auth
)
scan_result = scan_response.json()
print(f"Found {scan_result['hits']} clips")

# Ingest clips
ingest_response = requests.post(
    f"{BASE_URL}/folder/{folder_id}/ingest/",
    json={"number": 10},
    auth=auth
)
ingest_result = ingest_response.json()
print(f"Ingested: {ingest_result['ingested']}")
print(f"Skipped: {ingest_result['skipped']}")
print(f"Failed: {ingest_result['failed']}")
```

### Example 2: Replace Legacy Storage Files

```python
# Scan with legacy storage matching
scan_data = {
    "legacy_storages": ["VX-OLD-STORAGE"],
    "providers": ["red"]
}
response = requests.post(
    f"{BASE_URL}/folder/{folder_id}/scan/",
    json=scan_data,
    auth=auth
)

# Ingest with replace mode
ingest_data = {
    "replace": True,
    "legacy_storages": ["VX-OLD-STORAGE"]
}
response = requests.post(
    f"{BASE_URL}/folder/{folder_id}/ingest/",
    json=ingest_data,
    auth=auth
)
result = response.json()
print(f"Replaced: {result['replaced']} items")
```

### Example 3: Monitor Ingest Progress

```python
# Get folder with clip status
response = requests.get(
    f"{BASE_URL}/folder/{folder_id}/clips/",
    auth=auth
)
clips = response.json()['clips']

for clip in clips:
    print(f"{clip['umid']}: {clip['status_label']}")
    if clip['item_id']:
        # Get jobs for imported clips
        jobs_response = requests.get(
            f"{BASE_URL}/clip/{clip['umid']}/jobs/",
            auth=auth
        )
        jobs = jobs_response.json()['jobs']
        for job in jobs:
            print(f"  Job {job['id']}: {job['state']}")
```

### Example 4: Configure Metadata Mapping

```python
# Get existing mappings
response = requests.get(
    f"{BASE_URL}/metadata-mappings/",
    auth=auth
)
mappings = response.json()['mappings']

# Create new mapping
new_mapping = {
    "metadata_provider": "timecode",
    "metadata_portal": "timecode"
}
response = requests.post(
    f"{BASE_URL}/metadata-mappings/",
    json=new_mapping,
    auth=auth
)
print(f"Created mapping: {response.json()}")
```

## Pagination

Large result sets are paginated. Use `page` and `page_size` parameters:

```http
GET /tapelessingest/api/clips/?page=2&page_size=50
```

Response includes pagination metadata:

```json
{
  "count": 500,
  "next": "/api/clips/?page=3&page_size=50",
  "previous": "/api/clips/?page=1&page_size=50",
  "results": [...]
}
```

## Rate Limiting

API requests are subject to rate limiting:
- **100 requests per minute** per user
- **1000 requests per hour** per user

Exceeded limits return `429 Too Many Requests`.

## Webhooks / Callbacks

The plugin does not currently support webhooks. Use polling to monitor operation status.

## Python Client Example

```python
class TapelessIngestClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip('/') + '/tapelessingest/api'
        self.auth = HTTPBasicAuth(username, password)
        self.session = requests.Session()

    def create_folder(self, storage_id, path):
        response = self.session.post(
            f"{self.base_url}/folder/",
            json={"storage_id": storage_id, "path": path},
            auth=self.auth
        )
        response.raise_for_status()
        return response.json()

    def scan_folder(self, folder_id, providers=None):
        data = {}
        if providers:
            data['providers'] = providers
        response = self.session.post(
            f"{self.base_url}/folder/{folder_id}/scan/",
            json=data,
            auth=self.auth
        )
        response.raise_for_status()
        return response.json()

    def ingest_folder(self, folder_id, replace=False, legacy_storages=None):
        data = {"replace": replace}
        if legacy_storages:
            data['legacy_storages'] = legacy_storages
        response = self.session.post(
            f"{self.base_url}/folder/{folder_id}/ingest/",
            json=data,
            auth=self.auth
        )
        response.raise_for_status()
        return response.json()

    def get_clip(self, umid):
        response = self.session.get(
            f"{self.base_url}/clip/{umid}/",
            auth=self.auth
        )
        response.raise_for_status()
        return response.json()

# Usage
client = TapelessIngestClient(
    'https://portal.example.com',
    'username',
    'password'
)

folder = client.create_folder('VX-1', '/CARD_A')
scan_result = client.scan_folder(folder['id'], providers=['red'])
ingest_result = client.ingest_folder(folder['id'])
```

## Performance Considerations

### Batch Operations
- Use `number` parameter to process clips in batches
- Recommended batch size: 10-50 clips depending on format complexity

### Scanning
- Initial scan can be slow for folders with many files
- Elasticsearch indexing may add latency
- Use specific providers to reduce search scope

### Ingestion
- Import operations are asynchronous (Vidispine jobs)
- Monitor job status via `/clip/{umid}/jobs/` endpoint
- Large files may take significant time to transcode

### Caching
- Collection lookups are cached for 60 seconds
- Clear cache by restarting Portal services if needed

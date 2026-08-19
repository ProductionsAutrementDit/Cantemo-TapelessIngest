# TapelessIngest - Architecture Document

## Overview

TapelessIngest follows a **Plugin Architecture** pattern, extending Cantemo Portal's functionality through well-defined interfaces. The system uses a **Provider Pattern** for extensible camera format support and exposes a **REST API** for programmatic access.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      Cantemo Portal                              │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                   TapelessIngest Plugin                      ││
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────────┐ ││
│  │  │ REST API │  │  Views   │  │ Templates│  │   Static    │ ││
│  │  │(DRF)     │  │(Django)  │  │  (HTML)  │  │  (JS/CSS)   │ ││
│  │  └────┬─────┘  └────┬─────┘  └──────────┘  └─────────────┘ ││
│  │       │             │                                        ││
│  │  ┌────▼─────────────▼────┐                                  ││
│  │  │      Serializers      │                                  ││
│  │  └───────────┬───────────┘                                  ││
│  │              │                                               ││
│  │  ┌───────────▼───────────┐     ┌─────────────────────────┐ ││
│  │  │       Models          │     │      Providers          │ ││
│  │  │  ┌─────┐ ┌────────┐  │     │ ┌─────┐ ┌─────┐ ┌─────┐│ ││
│  │  │  │Clip │ │ Folder │  │◄────┤ │ RED │ │XDCAM│ │ P2  ││ ││
│  │  │  └─────┘ └────────┘  │     │ └─────┘ └─────┘ └─────┘│ ││
│  │  │  ┌─────────┐ ┌─────┐ │     │ ┌─────┐ ┌─────┐ ┌─────┐│ ││
│  │  │  │Settings │ │ Job │ │     │ │HDSLR│ │AVCHD│ │Zoom ││ ││
│  │  │  └─────────┘ └─────┘ │     │ └─────┘ └─────┘ └─────┘│ ││
│  │  └───────────────────────┘     └─────────────────────────┘ ││
│  └─────────────────────────────────────────────────────────────┘│
│                              │                                   │
│  ┌───────────────────────────▼───────────────────────────────┐  │
│  │                    Vidispine API                           │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │  │
│  │  │ Storage  │  │   Item   │  │Collection│  │   Job    │  │  │
│  │  │ Helper   │  │  Helper  │  │  Helper  │  │  Helper  │  │  │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Component Architecture

### 1. Plugin Layer (`plugin.py`)

Registers the plugin with Cantemo Portal using interface implementations:

| Interface | Class | Purpose |
|-----------|-------|---------|
| `IPluginURL` | `TapelessingestPluginURL` | URL routing registration |
| `IPluginBlock` | `TapelessingestAdminNavigationPlugin` | Admin navigation |
| `IPluginBlock` | `TapelessingestAdminMenuPlugin` | Admin menu panel |
| `IPluginBlock` | `TapelessIngestItemPanelPlugin` | Item view panel |
| `IAppRegister` | `TapelessingestRegister` | App registration |

### 2. API Layer (`urls.py`, `views.py`)

REST API endpoints using Django REST Framework:

| Endpoint | View | Method | Purpose |
|----------|------|--------|---------|
| `/api/browser/clips` | `ClipsInPathsView` | PUT/POST | Scan/ingest clips |
| `/api/browser/clips/jobs` | `ClipsJobsProgress` | PUT | Job status |
| `/notification/file/created` | `FileNotificationView` | POST | Vidispine notifications |
| `/admin/` | `SettingsView` | GET/POST | Plugin settings |
| `/clips/{id}/thumbnail` | `getClipThumbnail` | GET | Clip thumbnail |
| `/clips/{id}/proxy` | `getClipProxy` | GET | Clip proxy video |
| `/file/{id}/thumbnail` | `getFileThumbnail` | GET | File thumbnail |

### 3. Model Layer (`models/`)

Django ORM models for data persistence:

| Model | File | Purpose |
|-------|------|---------|
| `Clip` | `clip.py` | Camera clip representation |
| `Folder` | `folder.py` | Scanned folder |
| `Settings` | `settings.py` | Plugin configuration |
| `MetadataMapping` | `settings.py` | Metadata field mappings |
| `IngestClipJob` | `models.py` | Ingest job tracking |
| `IngestTaskJob` | `models.py` | Individual task tracking |

### 4. Provider Layer (`providers/`)

Extensible camera format handlers:

| Provider | File | Format |
|----------|------|--------|
| `Provider` (Base) | `providers.py` | Base class |
| `REDProvider` | `red.py` | RED camera |
| `XDCAMProvider` | `xdcam.py` | Sony XDCAM |
| `PanasonicP2Provider` | `panasonicP2.py` | Panasonic P2 |
| `HDSLRProvider` | `hdslr.py` | HDSLR cameras |
| `AVCHDProvider` | `avchd.py` | AVCHD format |
| `AtomosProvider` | `atomos.py` | Atomos recorders |
| `ZoomProvider` | `zoom.py` | Zoom audio |
| `IkegamiProvider` | `ikegami.py` | Ikegami cameras |
| `FileProvider` | `file.py` | Generic files |
| `ImageFileProvider` | `image_file.py` | Image files |
| `AudioFilesProvider` | `audio_files.py` | Audio files |
| `JVCProHDProvider` | `jvcprohd.py` | JVC ProHD |

### 5. Serialization Layer (`serializers.py`)

Django REST Framework serializers:

| Serializer | Model | Purpose |
|------------|-------|---------|
| `ClipSerializer` | `Clip` | Clip API representation |
| `FolderSerializer` | `Folder` | Folder API representation |
| `ClipMetadataSerializer` | `ClipMetadata` | Metadata key-value pairs |
| `MediaFileSerializer` | - | Media file info |
| `JobSerializer` | - | Job status info |

### 6. Frontend Layer

| Component | Location | Technology |
|-----------|----------|------------|
| Templates | `templates/TapelessIngest/` | Django Templates |
| JavaScript | `static/TapelessIngest/` | Backbone.js |
| Styles | `static/TapelessIngest/` | CSS |

## Data Flow

### Scan Flow

```
1. User initiates scan (API or UI)
   │
2. Folder.scan() queries Elasticsearch
   │
3. For each file, providers detect format
   │
4. Provider extracts metadata
   │
5. Clip objects created/updated
   │
6. Results serialized and returned
```

### Ingest Flow

```
1. User initiates ingest (API or UI)
   │
2. Clip.ingest() called
   │
3. Provider creates Vidispine placeholder
   │
4. Shape import task created
   │
5. Metadata applied to item
   │
6. Job tracked in IngestClipJob
   │
7. Status updates via polling
```

## Integration Points

### Vidispine API

Used via Portal helper classes:

- `StorageHelper` - File and storage operations
- `ItemHelper` - Item CRUD and metadata
- `CollectionHelper` - Collection management
- `JobHelper` - Job monitoring
- `IngestHelper` - Import operations

### Elasticsearch

Used for file discovery:

- Storage file indexing
- Path-based queries
- Extension filtering

## Configuration

### Settings Model

| Field | Type | Purpose |
|-------|------|---------|
| `storage_id` | CharField | Default storage |
| `tmp_storage` | CharField | Temporary storage |
| `bmxtranswrap` | CharField | BMX tool path |
| `mxf2raw` | CharField | MXF tool path |
| `ffmpeg_path` | CharField | FFmpeg path |
| `base_folder` | CharField | Base scan folder |
| `collections_ignore_folder_str` | TextField | Folders to ignore |
| `collections_rename_folder_str` | TextField | Folder renaming rules |

### Metadata Mappings

Maps provider metadata fields to Portal/Vidispine fields.

## Security Considerations

- All API endpoints require Portal authentication
- Admin views require admin permissions
- Vidispine operations run as authenticated user
- File access controlled by storage permissions

## Performance Considerations

- Elasticsearch used for efficient file queries
- Batch processing for multiple clips
- Job-based async processing for ingestion
- Pagination support in API responses
- Redis caching for repeated lookups (recent improvement)

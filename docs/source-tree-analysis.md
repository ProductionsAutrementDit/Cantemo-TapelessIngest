# TapelessIngest - Source Tree Analysis

## Directory Structure

```
TapelessIngest/
├── __init__.py                 # Package init
├── plugin.py                   # 🔌 Plugin entry point - Cantemo registration
├── urls.py                     # 🌐 URL routing - REST API endpoints
├── views.py                    # 📡 Request handlers - API views
├── serializers.py              # 📦 DRF serializers - API data formatting
├── forms.py                    # 📝 Django forms - Admin forms
├── helpers.py                  # 🔧 Utility functions
├── helpers_bk.py               # 📁 Backup of helpers (legacy)
├── utilities.py                # 🔧 Additional utilities
├── metadatas.py                # 📋 XML metadata parsing
├── errors.py                   # ❌ Custom exceptions
├── hedge.py                    # 🎬 Hedge integration
├── plistner.py                 # 👂 Event listener
├── filesystem_scanner.py       # 📂 File system operations
├── update_original_file_metadatas.py  # 🔄 Metadata update logic
│
├── models/                     # 📊 Django ORM Models
│   ├── __init__.py
│   ├── clip.py                 # Clip model (main entity) ~32KB
│   ├── folder.py               # Folder model ~15KB
│   ├── settings.py             # Settings & MetadataMapping ~3KB
│   └── models.py               # Job tracking models ~3KB
│
├── providers/                  # 🎥 Camera Format Providers
│   ├── __init__.py
│   ├── providers.py            # Base Provider class ~6KB
│   ├── red.py                  # RED camera support ~5KB
│   ├── xdcam.py                # Sony XDCAM ~11KB
│   ├── panasonicP2.py          # Panasonic P2 ~13KB
│   ├── hdslr.py                # HDSLR cameras ~500B
│   ├── avchd.py                # AVCHD format ~500B
│   ├── atomos.py               # Atomos recorders ~550B
│   ├── zoom.py                 # Zoom audio ~500B
│   ├── ikegami.py              # Ikegami cameras ~7KB
│   ├── jvcprohd.py             # JVC ProHD ~8KB
│   ├── file.py                 # Generic file provider ~6KB
│   ├── image_file.py           # Image files ~750B
│   └── audio_files.py          # Audio files ~6KB
│
├── classes/                    # 📦 Utility Classes
│   ├── __init__.py
│   ├── clip.py                 # Clip helper class ~1KB
│   └── folder.py               # Folder helper class ~500B
│
├── templates/                  # 🎨 Django Templates
│   └── TapelessIngest/
│       ├── base.html           # Base template
│       ├── navigation.html     # Nav bar plugin
│       ├── ti_viewpanel.html   # Item view panel
│       ├── ti_viewpanel_js.html # View panel JS
│       ├── proxy_player.html   # Video player
│       ├── gearbox_menu_big.html
│       ├── metadatas-update-item.html
│       └── admin/
│           ├── index.html
│           ├── settings.html       # Settings page
│           ├── admin_leftpanel_pane.html
│           └── metadatas_mapping.html
│
├── static/                     # 📁 Static Assets
│   └── TapelessIngest/
│       ├── tapelessingest.css  # Styles
│       ├── utilities.js        # JS utilities
│       ├── backbone.partial-fetch.js  # Backbone extension
│       ├── clips_model.js      # Clip Backbone model
│       ├── clips_collection.js # Clip collection
│       ├── clips_views.js      # Clip views
│       ├── folders_model.js    # Folder Backbone model
│       ├── folders_collection.js
│       └── folders_views.js    # Folder views
│
├── templatetags/               # 🏷️ Template Tags
│   ├── __init__.py
│   └── tapelessingest_extras.py
│
├── migrations/                 # 🔄 Database Migrations
│   ├── __init__.py
│   ├── 0001_initial.py
│   ├── 0002_auto_20190604_1534.py
│   ├── ... (16 migrations total)
│   └── 0016_auto_20211006_1749.py
│
├── vidispine/                  # 🔗 Vidispine Integration
│   ├── find_sidecar_file.xml   # Sidecar detection config
│   ├── import_sidecar_file.xml # Sidecar import config
│   ├── place_holder_import_tasks.xml  # Import tasks ~30KB
│   └── shape_import_tasks.xml  # Shape import ~4KB
│
├── docs/                       # 📚 Generated Documentation
│   ├── index.md                # Documentation index
│   ├── project-overview.md     # Project summary
│   ├── architecture.md         # System architecture
│   ├── data-models.md          # Database schema
│   ├── api-contracts.md        # REST API reference
│   ├── source-tree-analysis.md # This file
│   ├── development-guide.md    # Developer setup
│   └── project-scan-report.json # Scan state
│
└── [Root Documentation]        # 📄 Existing Docs
    ├── README.md               # Basic readme
    ├── API.md                  # Detailed API docs
    ├── PROVIDERS.md            # Provider system docs
    ├── USER_GUIDE.md           # User workflows
    ├── DOCUMENTATION.md        # Doc index
    ├── CODE_IMPROVEMENTS.md    # Code audit
    └── IMPROVEMENTS_SUMMARY.md # Recent fixes
```

## Critical Directories

### `/models` - Data Layer
Primary data models for the plugin. Core business logic resides here.

| File | LOC | Purpose |
|------|-----|---------|
| `clip.py` | ~900 | Main Clip model with ingest logic |
| `folder.py` | ~400 | Folder model with scan logic |
| `settings.py` | ~90 | Plugin settings singleton |
| `models.py` | ~80 | Job tracking models |

### `/providers` - Format Handlers
Extensible provider system for camera format support.

| Provider | Format | Complexity |
|----------|--------|------------|
| `xdcam.py` | Sony XDCAM | High - XML parsing, multi-file |
| `panasonicP2.py` | Panasonic P2 | High - Complex folder structure |
| `red.py` | RED camera | Medium - R3D files |
| `jvcprohd.py` | JVC ProHD | Medium |
| `ikegami.py` | Ikegami | Medium |
| `audio_files.py` | Audio | Medium - Multiple formats |
| `file.py` | Generic | Low - Fallback provider |
| Others | Various | Low - Simple wrappers |

### `/templates/TapelessIngest` - UI Layer
Django templates for admin interface and item views.

| Template | Purpose |
|----------|---------|
| `admin/settings.html` | Plugin configuration UI |
| `ti_viewpanel.html` | Clip info in item view |
| `proxy_player.html` | Video preview player |

### `/static/TapelessIngest` - Frontend
Backbone.js MVC application for browser interface.

| File | Purpose |
|------|---------|
| `clips_model.js` | Clip data model |
| `clips_views.js` | Clip UI rendering |
| `folders_model.js` | Folder data model |
| `folders_views.js` | Folder UI rendering |

### `/vidispine` - Integration
XML configuration for Vidispine import workflows.

| File | Purpose |
|------|---------|
| `place_holder_import_tasks.xml` | Main import workflow |
| `shape_import_tasks.xml` | Shape/transcode config |
| `find_sidecar_file.xml` | Sidecar detection |

## Entry Points

| Entry Point | File | Purpose |
|-------------|------|---------|
| **Plugin Registration** | `plugin.py` | Cantemo plugin interfaces |
| **URL Routing** | `urls.py` | API endpoint definitions |
| **Admin Interface** | `views.py:SettingsView` | Settings management |
| **API Scan** | `views.py:ClipsInPathsView` | Scan/ingest operations |

## Key Files by Function

### Configuration
- `models/settings.py` - Runtime settings
- `vidispine/*.xml` - Import workflows

### Business Logic
- `models/clip.py` - Clip operations
- `models/folder.py` - Scan operations
- `providers/providers.py` - Provider base class

### API
- `urls.py` - Route definitions
- `views.py` - Request handlers
- `serializers.py` - Data serialization

### Frontend
- `static/TapelessIngest/*.js` - Backbone app
- `templates/TapelessIngest/*.html` - Templates

## File Size Distribution

| Category | Files | Total Size |
|----------|-------|------------|
| Models | 4 | ~50KB |
| Providers | 13 | ~65KB |
| Views/API | 3 | ~25KB |
| Templates | 11 | ~15KB |
| Static JS | 9 | ~20KB |
| Vidispine XML | 4 | ~40KB |
| **Total** | ~45 | ~215KB |

## Dependencies

### Internal (Cantemo Portal)
- `portal.pluginbase.core`
- `portal.generic.baseviews`
- `portal.vidispine.*`
- `portal.api.v2`
- `VidiRest.*`

### External
- Django
- Django REST Framework
- elasticsearch_dsl
- pyxb (XML binding)
- simplejson

# TapelessIngest Documentation Index

> **Primary AI Entry Point** - Use this document as the starting point for understanding the TapelessIngest project.

## Project Overview

| Attribute | Value |
|-----------|-------|
| **Project** | TapelessIngest |
| **Type** | Backend - Cantemo Portal Plugin |
| **Language** | Python 3.x |
| **Framework** | Django + Django REST Framework |
| **Platform** | Cantemo Portal / Vidispine |
| **Architecture** | Plugin Architecture with REST API |

## Quick Reference

### Technology Stack
- **Backend**: Django, Django REST Framework
- **Frontend**: Backbone.js, Django Templates
- **Integration**: Vidispine API, Elasticsearch
- **Tools**: FFmpeg (thumbnails)

### Key Entry Points
- **Plugin Registration**: `plugin.py`
- **API Routing**: `urls.py`
- **Main Views**: `views.py`
- **Core Models**: `models/clip.py`, `models/folder.py`

## Generated Documentation

### Architecture & Design
- [Project Overview](./project-overview.md) - Executive summary and project information
- [Architecture](./architecture.md) - System architecture, component design, data flow

### Technical Reference
- [Data Models](./data-models.md) - Database schema, entity relationships, model definitions
- [API Contracts](./api-contracts.md) - REST API endpoints, request/response formats
- [Source Tree Analysis](./source-tree-analysis.md) - Directory structure, critical files

### Development
- [Development Guide](./development-guide.md) - Setup, workflow, coding standards, debugging

## Existing Documentation

Located in project root:

| Document | Description |
|----------|-------------|
| [API.md](../API.md) | Comprehensive REST API documentation with examples |
| [PROVIDERS.md](../PROVIDERS.md) | Provider system architecture, custom provider creation |
| [USER_GUIDE.md](../USER_GUIDE.md) | End-user workflows and common operations |
| [DOCUMENTATION.md](../DOCUMENTATION.md) | Original documentation index |
| [CODE_IMPROVEMENTS.md](../CODE_IMPROVEMENTS.md) | Code quality audit report |
| [IMPROVEMENTS_SUMMARY.md](../IMPROVEMENTS_SUMMARY.md) | Recent code improvements |

## Navigation by Task

### I want to...

**Understand the project**
→ Start with [Project Overview](./project-overview.md), then [Architecture](./architecture.md)

**Work with the API**
→ See [API Contracts](./api-contracts.md) or detailed [API.md](../API.md)

**Understand the data model**
→ See [Data Models](./data-models.md)

**Add a new camera format**
→ See [PROVIDERS.md](../PROVIDERS.md) and [Development Guide](./development-guide.md)

**Set up development environment**
→ See [Development Guide](./development-guide.md)

**Use the plugin as an end user**
→ See [USER_GUIDE.md](../USER_GUIDE.md)

**Review code quality**
→ See [CODE_IMPROVEMENTS.md](../CODE_IMPROVEMENTS.md)

## Project Structure Summary

```
TapelessIngest/
├── plugin.py           # Cantemo plugin registration
├── urls.py             # REST API endpoints
├── views.py            # Request handlers
├── serializers.py      # DRF serializers
├── models/             # Django ORM models
│   ├── clip.py         # Clip model (main entity)
│   ├── folder.py       # Folder model
│   └── settings.py     # Plugin settings
├── providers/          # Camera format handlers (12 providers)
├── templates/          # Django HTML templates
├── static/             # Backbone.js frontend
├── vidispine/          # Vidispine XML configs
└── docs/               # This documentation
```

## Key Concepts

### Provider System
Extensible architecture for camera format support. Each provider handles:
- Format detection (file extensions, folder structure)
- Metadata extraction (camera-specific XML/binary parsing)
- Clip identification (UMID generation)
- Import configuration

### Clip Lifecycle
1. **Scan** - Detect clips in folder via Elasticsearch
2. **Extract** - Provider extracts metadata
3. **Ingest** - Create Vidispine placeholder
4. **Import** - Shape import and transcoding
5. **Complete** - Metadata applied, item available

### Integration Points
- **Vidispine API** - Media asset management
- **Elasticsearch** - File indexing and search
- **Cantemo Portal** - Plugin host, authentication, UI

---

*Documentation generated: 2026-01-29*
*Scan mode: Deep Scan*
*Project type: Backend (Django Plugin)*

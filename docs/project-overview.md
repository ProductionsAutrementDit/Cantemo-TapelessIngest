# TapelessIngest - Project Overview

## Executive Summary

**TapelessIngest** is a Django plugin for Cantemo Portal that automates the import of professional camera media. It provides a provider-based architecture supporting multiple camera formats (RED, XDCAM, P2, HDSLR, AVCHD, etc.) with metadata extraction, batch ingestion, and integration with Vidispine media asset management.

## Project Information

| Attribute | Value |
|-----------|-------|
| **Project Name** | TapelessIngest |
| **Type** | Cantemo Portal Plugin |
| **Author** | Camille Darley - Productions Autrement Dit |
| **Version** | 0.0.1 |
| **License** | Proprietary |
| **Repository Type** | Monolith |

## Technology Stack

| Category | Technology | Purpose |
|----------|------------|---------|
| **Language** | Python 3.x | Core implementation |
| **Framework** | Django | Web framework via Cantemo Portal |
| **API** | Django REST Framework | REST API endpoints |
| **Platform** | Cantemo Portal | Plugin host system |
| **Media Integration** | Vidispine API | Media asset management |
| **ORM** | Django ORM | Database models |
| **Frontend** | Backbone.js | JavaScript MVC |
| **Templates** | Django Templates | HTML rendering |
| **Video Processing** | FFmpeg | Thumbnail generation |

## Architecture Pattern

**Plugin Architecture with REST API**

- Extends Cantemo Portal via plugin interfaces (IPluginURL, IPluginBlock, IAppRegister)
- Provider pattern for extensible camera format support
- REST API for programmatic access
- Django ORM for data persistence
- Vidispine integration for media operations

## Key Features

1. **Multi-Format Support** - 12 camera format providers
2. **Metadata Extraction** - Automatic extraction from camera files
3. **Batch Ingestion** - Process multiple clips efficiently
4. **Collection Organization** - Auto-create collections from folder structure
5. **Legacy Storage Matching** - Hash-based file matching for storage migration
6. **Spanned Clip Support** - Handle multi-file clips
7. **REST API** - Full programmatic access

## Core Components

| Component | Location | Description |
|-----------|----------|-------------|
| **Plugin Entry** | `plugin.py` | Cantemo plugin registration |
| **URL Routing** | `urls.py` | REST API endpoint definitions |
| **Views** | `views.py` | Request handlers and API views |
| **Models** | `models/` | Django ORM models (Clip, Folder, Settings) |
| **Providers** | `providers/` | Camera format handlers |
| **Serializers** | `serializers.py` | REST Framework serializers |
| **Templates** | `templates/` | Django HTML templates |
| **Static** | `static/` | JavaScript and CSS assets |

## Getting Started

### Prerequisites

- Cantemo Portal installation
- Vidispine configured with browsable storage
- Python 3.x environment
- FFmpeg for thumbnail generation

### Quick Start

1. Install plugin in Portal plugins directory
2. Configure storage in Admin > TapelessIngest
3. Set up metadata mappings
4. Use API or UI to scan and ingest camera media

## Documentation Index

- [Architecture](./architecture.md) - System architecture details
- [API Contracts](./api-contracts.md) - REST API reference
- [Data Models](./data-models.md) - Database schema
- [Development Guide](./development-guide.md) - Developer setup
- [Source Tree](./source-tree-analysis.md) - Code structure

## Related Documentation

Existing documentation in project root:

- [API.md](../API.md) - Detailed API documentation
- [PROVIDERS.md](../PROVIDERS.md) - Provider system documentation
- [USER_GUIDE.md](../USER_GUIDE.md) - End-user workflows
- [DOCUMENTATION.md](../DOCUMENTATION.md) - Documentation index

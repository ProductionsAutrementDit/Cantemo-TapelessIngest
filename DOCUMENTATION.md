# TapelessIngest Documentation Index

Complete documentation for the Cantemo Portal TapelessIngest plugin.

## 📚 Documentation Structure

### 1. [README.md](README.md) - Getting Started
**Primary documentation** covering:
- Overview and features
- Supported formats
- Architecture overview
- Installation and configuration
- Quick start usage
- File structure
- Troubleshooting basics
- Version history

**Audience**: Developers, system administrators, new users

---

### 2. [PROVIDERS.md](PROVIDERS.md) - Provider System
**Comprehensive provider architecture** covering:
- Provider system overview
- Base Provider class API reference
- Individual provider implementations
- Creating custom providers
- Provider detection logic
- Metadata extraction
- Spanned clip handling
- Best practices and troubleshooting

**Audience**: Developers extending the plugin, format specialists

**Key Topics**:
- RED, XDCAM, P2, HDSLR, AVCHD, Atomos, Zoom providers
- Provider method reference
- Step-by-step custom provider creation
- Format-specific folder structures
- Metadata field mappings

---

### 3. [API.md](API.md) - REST API Reference
**Complete REST API documentation** covering:
- All API endpoints
- Request/response formats
- Authentication
- Error handling
- Usage examples
- Python client library
- Rate limiting
- Performance considerations

**Audience**: Integration developers, automation engineers

**Key Endpoints**:
- Folder operations (scan, ingest, list)
- Clip operations (get, ingest, status)
- Settings and metadata mapping management
- Job monitoring

---

### 4. [USER_GUIDE.md](USER_GUIDE.md) - User Guide
**Practical usage documentation** covering:
- Common workflows
- Step-by-step procedures
- Best practices
- Troubleshooting guide
- Feature explanations
- Configuration examples

**Audience**: End users, operators, administrators

**Key Workflows**:
- Import camera card
- Replace legacy storage
- Selective provider import
- Batch processing
- Monitoring and status checking

---

## 🎯 Quick Navigation

### I want to...

**Get started quickly**
→ [README.md](README.md) - Installation and Overview

**Import my first camera card**
→ [USER_GUIDE.md](USER_GUIDE.md) - Workflow 1: Import Camera Card

**Understand the architecture**
→ [README.md](README.md#architecture) - Core Components

**Add support for a new camera format**
→ [PROVIDERS.md](PROVIDERS.md#creating-a-custom-provider) - Custom Provider Creation

**Integrate with my application**
→ [API.md](API.md) - REST API Reference

**Troubleshoot an issue**
→ [USER_GUIDE.md](USER_GUIDE.md#troubleshooting) - Common Issues

**Configure metadata mappings**
→ [API.md](API.md#metadata-mapping-operations) - Metadata Mapping API

**Understand spanned clips**
→ [USER_GUIDE.md](USER_GUIDE.md#spanned-clips) - Advanced Features

**Optimize performance**
→ [USER_GUIDE.md](USER_GUIDE.md#performance-optimization) - Best Practices

**Replace legacy storage files**
→ [USER_GUIDE.md](USER_GUIDE.md#workflow-2-replace-legacy-storage-files) - Migration Workflow

---

## 📖 Documentation by Role

### System Administrator
1. [README.md](README.md#installation) - Installation
2. [README.md](README.md#configuration) - Configuration
3. [USER_GUIDE.md](USER_GUIDE.md#getting-started) - Setup
4. [USER_GUIDE.md](USER_GUIDE.md#best-practices) - Best Practices

### Media Operator
1. [USER_GUIDE.md](USER_GUIDE.md) - Complete User Guide
2. [README.md](README.md#usage) - Quick Reference
3. [USER_GUIDE.md](USER_GUIDE.md#common-operations) - Daily Operations

### Python Developer
1. [API.md](API.md) - API Reference
2. [API.md](API.md#python-client-example) - Python Client
3. [API.md](API.md#usage-examples) - Code Examples

### Plugin Developer
1. [README.md](README.md#architecture) - Architecture Overview
2. [PROVIDERS.md](PROVIDERS.md) - Provider System
3. [README.md](README.md#development) - Development Guide
4. [PROVIDERS.md](PROVIDERS.md#creating-a-custom-provider) - Custom Providers

---

## 🔍 Key Concepts

### Provider System
Modular architecture for handling different camera formats.
- **Docs**: [PROVIDERS.md](PROVIDERS.md)
- **Overview**: [README.md](README.md#provider-system)

### Clip & Folder Models
Django models representing media and storage folders.
- **Folder**: [README.md](README.md#folder)
- **Clip**: [README.md](README.md#clip)

### Metadata Mapping
Configurable mapping between camera and Portal metadata fields.
- **Configuration**: [README.md](README.md#metadata-mapping)
- **API**: [API.md](API.md#metadata-mapping-operations)

### Spanned Clips
Multi-file clips spanning across multiple media files.
- **User Guide**: [USER_GUIDE.md](USER_GUIDE.md#spanned-clips)
- **Provider Impl**: [PROVIDERS.md](PROVIDERS.md#spanned-clip-methods)

### Multi-Component Import
One item whose media is several files (span files, or a separate audio track); the ingest waits for the components to attach before importing the main file, and a failure leaves a resumable item.
- **User Guide**: [USER_GUIDE.md](USER_GUIDE.md#multi-component-import)
- **Provider Impl**: [PROVIDERS.md](PROVIDERS.md#the-main-files-own-video-component)

### Legacy Storage Migration
Re-importing media from old storages using hash matching.
- **Feature**: [README.md](README.md#legacy-storage-migration)
- **Workflow**: [USER_GUIDE.md](USER_GUIDE.md#workflow-2-replace-legacy-storage-files)

---

## 🎓 Learning Path

### Beginner Path
1. Read [README.md](README.md) - Understand what the plugin does
2. Review [README.md](README.md#supported-formats) - Check if your format is supported
3. Follow [USER_GUIDE.md](USER_GUIDE.md#workflow-1-import-camera-card) - Import your first card
4. Explore [USER_GUIDE.md](USER_GUIDE.md#common-operations) - Learn daily operations

### Advanced User Path
1. Study [README.md](README.md#architecture) - Understand the architecture
2. Review [USER_GUIDE.md](USER_GUIDE.md#advanced-features) - Master advanced features
3. Learn [README.md](README.md#metadata-mapping) - Configure metadata
4. Optimize [USER_GUIDE.md](USER_GUIDE.md#best-practices) - Apply best practices

### Developer Path
1. Read [README.md](README.md#architecture) - Architecture overview
2. Study [PROVIDERS.md](PROVIDERS.md) - Provider system deep dive
3. Review [API.md](API.md) - API reference
4. Create [PROVIDERS.md](PROVIDERS.md#creating-a-custom-provider) - Build custom provider

---

## 📋 Checklists

### Pre-Installation Checklist
- [ ] Cantemo Portal installed and configured
- [ ] Vidispine backend accessible
- [ ] Storage registered in Vidispine
- [ ] Elasticsearch running and indexing files
- [ ] User has appropriate permissions

**Reference**: [README.md](README.md#installation)

### First Import Checklist
- [ ] Plugin installed and configured
- [ ] Metadata mappings configured
- [ ] Collection rules defined
- [ ] Test folder available
- [ ] Provider supports format
- [ ] Storage accessible and browsable

**Reference**: [USER_GUIDE.md](USER_GUIDE.md#getting-started)

### Custom Provider Checklist
- [ ] Provider file created
- [ ] Detection methods implemented
- [ ] Metadata extraction implemented
- [ ] File management methods implemented
- [ ] Provider registered in PROVIDERS_LIST
- [ ] Tested with sample media

**Reference**: [PROVIDERS.md](PROVIDERS.md#creating-a-custom-provider)

---

## 🛠️ Common Tasks

| Task | Documentation | Section |
|------|---------------|---------|
| Install plugin | [README.md](README.md#installation) | Installation |
| Configure settings | [README.md](README.md#configuration) | Configuration |
| Import camera card | [USER_GUIDE.md](USER_GUIDE.md#workflow-1-import-camera-card) | Basic Workflows |
| Add metadata mapping | [API.md](API.md#create-metadata-mapping) | API Reference |
| Create custom provider | [PROVIDERS.md](PROVIDERS.md#creating-a-custom-provider) | Provider Development |
| Troubleshoot scan issues | [USER_GUIDE.md](USER_GUIDE.md#issue-no-clips-detected) | Troubleshooting |
| Replace storage files | [USER_GUIDE.md](USER_GUIDE.md#workflow-2-replace-legacy-storage-files) | Advanced Workflows |
| Monitor ingest status | [USER_GUIDE.md](USER_GUIDE.md#check-ingest-status) | Common Operations |
| Integrate via API | [API.md](API.md#usage-examples) | API Examples |
| Optimize performance | [USER_GUIDE.md](USER_GUIDE.md#performance-optimization) | Best Practices |

---

## 📞 Support & Resources

### Getting Help
- **Installation Issues**: [README.md](README.md#installation)
- **Usage Questions**: [USER_GUIDE.md](USER_GUIDE.md)
- **API Integration**: [API.md](API.md)
- **Provider Development**: [PROVIDERS.md](PROVIDERS.md)

### Reporting Issues
Include:
- Plugin version
- Portal/Vidispine version
- Provider name
- Error messages
- Log excerpts

**Contact**: [README.md](README.md#author)

### Contributing
- Report bugs via issue tracker
- Submit feature requests
- Share custom providers
- Improve documentation

---

## 📝 Document Versions

| Document | Last Updated | Version |
|----------|--------------|---------|
| README.md | 2025-01-15 | 1.0 |
| PROVIDERS.md | 2025-01-15 | 1.0 |
| API.md | 2025-01-15 | 1.0 |
| USER_GUIDE.md | 2025-01-15 | 1.0 |
| DOCUMENTATION.md | 2025-01-15 | 1.0 |

---

## 🔄 Documentation Updates

This documentation is maintained alongside the plugin codebase. When reporting issues or requesting features, please reference the relevant documentation section.

**Plugin Version**: 0.0.1
**Author**: Camille Darley - Productions Autrement Dit
**Website**: [www.studiopad.fr](http://www.studiopad.fr)

---

## 📑 Quick Reference

### File Organization
```
TapelessIngest/
├── README.md           # Main documentation & overview
├── DOCUMENTATION.md    # This file - navigation guide
├── PROVIDERS.md        # Provider system reference
├── API.md             # REST API documentation
├── USER_GUIDE.md      # User workflows & troubleshooting
├── plugin.py          # Plugin registration
├── models/            # Data models
├── providers/         # Format providers
├── templates/         # UI templates
└── ...
```

### Essential Links
- **Main Docs**: [README.md](README.md)
- **Quick Start**: [USER_GUIDE.md](USER_GUIDE.md#getting-started)
- **API Reference**: [API.md](API.md)
- **Provider Guide**: [PROVIDERS.md](PROVIDERS.md)
- **Troubleshooting**: [USER_GUIDE.md](USER_GUIDE.md#troubleshooting)

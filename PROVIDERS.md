# Provider System Documentation

## Overview

The TapelessIngest plugin uses a **provider architecture** to handle different camera and recording device formats. Each provider is responsible for:

1. **Format Detection** - Identifying files belonging to its format
2. **Metadata Extraction** - Reading native camera metadata
3. **File Management** - Identifying media files and their relationships
4. **Import Configuration** - Providing format-specific import options

## Base Provider Class

All providers inherit from the base `Provider` class ([providers/providers.py](providers/providers.py)).

### Core Methods

#### Detection Methods

```python
def getExtensions(self):
    """
    Returns list of file extensions to search for.

    Returns:
        list: File extensions (e.g., ['.xml', '.mov'])
    """
    return []

def getSubPaths(self):
    """
    Returns list of subdirectories to search within folder.

    Returns:
        list: Relative paths (e.g., ['CLIP', 'VIDEO'])
    """
    return []

def getFilters(self, escaped_path):
    """
    Returns Elasticsearch filter queries for file detection.

    Args:
        escaped_path (str): Regex-escaped folder path

    Returns:
        list: Elasticsearch query filters
    """
    return []

def getSegmentedExtensions(self):
    """
    Returns suffixes whose files are SEGMENTS of one clip, not clips.

    For a camera that splits one take into `X_001.EXT`, `X_002.EXT` …
    `X_NNN.EXT`: the `_001` file anchors the clip, its siblings are
    skipped at scan and re-attached at ingest by
    getClipAdditionalMediaFiles, and an increment with no `_001` and
    other increments beside it is reported as an incomplete copy.

    Declare the EXACT case your runtime guard accepts and nothing wider:
    matching is case-sensitive, and a declaration reaching past your
    guard suppresses files you then decline — which loses their media,
    because whichever provider claims the anchor cannot re-attach them.

    Unlike the three methods above, this one does NOT feed
    build_search_doc, so declaring a suffix leaves the byte-frozen
    golden search doc untouched.

    Returns:
        list: Suffixes (e.g. ['.R3D']); empty means nothing is grouped
    """
    return []
```

#### Metadata Methods

```python
def getMetadatasFromFile(self, file, metadatas, context):
    """
    Extract metadata from file and update metadatas dict.

    Args:
        file (VSFile): Vidispine file object
        metadatas (dict): Existing metadata dictionary
        context (dict): Shared context across providers

    Returns:
        tuple: (updated_metadatas, updated_context)

    Required Keys to Set:
        - 'provider': Provider machine_name
        - 'umid': Unique material identifier
    """
    return metadatas, context

def getAvailableMetadatas(self):
    """
    Returns list of metadata fields this provider can extract.

    Returns:
        list: Tuples of (field_name, field_description)
    """
    return ()
```

#### File Management Methods

```python
def getClipMainMediaFile(self, clip):
    """
    Returns the primary media file for import.

    Args:
        clip (Clip): Clip model instance

    Returns:
        dict: File information with keys:
            - 'file_id': Vidispine file ID
            - 'path': File path
            - 'type': 'video', 'audio', or 'container'
    """
    return None

def getClipMediaFiles(self, clip):
    """
    Returns all media files for the clip.

    Args:
        clip (Clip): Clip model instance

    Returns:
        list: List of file info dicts (see getClipMainMediaFile)
    """
    media_file = self.getClipMainMediaFile(clip)
    if media_file:
        return [media_file]
    return None

def getClipAdditionalMediaFiles(self, clip):
    """
    Returns additional files to import (e.g., separate audio tracks).

    Args:
        clip (Clip): Clip model instance

    Returns:
        list: List of file info dicts
    """
    return []
```

#### Spanned Clip Methods

```python
def isSpannedClip(self, clip):
    """
    Check if clip spans multiple files.

    Args:
        clip (Clip): Clip model instance

    Returns:
        bool: True if clip is spanned
    """
    return False

def isMasterClip(self, clip):
    """
    Check if clip is the master of a spanned set.

    Args:
        clip (Clip): Clip model instance

    Returns:
        bool: True if master clip
    """
    return True

def getSpannedClips(self, clip):
    """
    Returns all clips in a spanned set.

    Args:
        clip (Clip): Master clip instance

    Returns:
        QuerySet: Related Clip objects
    """
    return False
```

#### Import Configuration

```python
def getImportOptions(self):
    """
    Returns Vidispine import options for this format.

    Returns:
        dict: Import options (e.g., {'no-transcode': True})
    """
    return {}
```

## Provider Implementations

### RED Provider
**File**: [providers/red.py](providers/red.py)
**Format**: RED Digital Cinema (.R3D files)

**Features**:
- Reads RED RMD (RED Metadata) XML files
- Extracts camera settings, timecode, and technical metadata
- Handles spanned R3D clips
- Supports multi-audio channel imports

**Detection**:
- Extensions: `.R3D`
- Searches root folder and subfolders

**Metadata Extracted**:
- Clip name, UMID, duration, timecode
- Camera model, serial number, firmware
- Resolution, frame rate, codec
- ISO, color temperature, tint
- Lens information

**Spanned Clips**:
- Detects when R3D files span multiple parts
- Groups by base filename (e.g., `A001_C001_*.R3D`)
- Creates master clip with related segments

### Sony XDCAM Provider
**File**: [providers/xdcam.py](providers/xdcam.py)
**Format**: Sony XDCAM (XDCAM folder structure)

**Features**:
- Reads Sony XML metadata files
- Supports XDCAM HD and XDCAM EX formats
- Handles proxy and hi-res files
- Multi-audio track support

**Detection**:
- Extensions: `.MXF`, `.XML`
- Subdirectories: `Clip`, `Sub`, `General`, `MEDIAPRO`
- Filters: XDCAM folder structure patterns

**Folder Structure**:
```
XDCAM_ROOT/
├── BPAV/
│   ├── CLPR/           # MXF media files
│   │   ├── C0001.MXF
│   │   └── ...
│   └── CUEUP.XML       # Cue sheet
├── General/
│   └── Sony.xml        # General metadata
├── Clip/
│   ├── C0001M01.XML   # Clip metadata
│   └── ...
└── Sub/                # Proxy media
    └── ...
```

**Metadata Extracted**:
- Clip name, UMID, creation date
- Duration, timecode, frame rate
- Video codec, audio channels
- Device information

### Panasonic P2 Provider
**File**: [providers/panasonicP2.py](providers/panasonicP2.py)
**Format**: Panasonic P2 (P2 card structure)

**Features**:
- Reads P2 XML metadata
- Supports AVC-Intra and DVCPRO HD
- Handles P2 card hierarchy
- Audio track mapping

**Detection**:
- Extensions: `.MXF`, `.XML`
- Subdirectories: `CLIP`, `VIDEO`, `AUDIO`, `VOICE`
- Filters: P2 folder structure patterns

**Folder Structure**:
```
P2_ROOT/
├── CONTENTS/
│   ├── CLIP/
│   │   ├── 0001AB.XML  # Clip metadata
│   │   └── ...
│   ├── VIDEO/
│   │   ├── 0001AB.MXF  # Video essence
│   │   └── ...
│   ├── AUDIO/
│   │   ├── 0001AB00.MXF # Audio track 1
│   │   ├── 0001AB01.MXF # Audio track 2
│   │   └── ...
│   └── VOICE/          # Voice memo files
└── LASTCLIP.TXT
```

**Metadata Extracted**:
- Clip name, UMID, duration
- Timecode, frame rate, drop frame
- Video codec, resolution
- Audio configuration
- Camera model, serial number

### HDSLR Provider
**File**: [providers/hdslr.py](providers/hdslr.py)
**Format**: DSLR and Mirrorless Cameras (.MOV, .MP4)

**Features**:
- Extracts EXIF and QuickTime metadata
- Handles various DSLR manufacturer formats
- Supports separate audio files
- Derives metadata from file properties

**Detection**:
- Extensions: `.MOV`, `.MP4`, `.M4V`
- Searches root folder

**Metadata Extracted**:
- Filename-based clip name
- File creation date
- Video codec, frame rate, resolution
- EXIF camera information (if available)
- Audio codec and sample rate

**UMID Generation**:
- Creates UMID from file hash and path when no native UMID exists

### AVCHD Provider
**File**: [providers/avchd.py](providers/avchd.py)
**Format**: AVCHD (BDMV folder structure)

**Features**:
- Reads AVCHD XML metadata
- Supports BDMV and PRIVATE folder structures
- Handles playlist information
- Multi-angle support

**Detection**:
- Extensions: `.MTS`, `.M2TS`, `.XML`
- Subdirectories: `BDMV/STREAM`, `BDMV/CLIPINF`, `AVCHD`

**Folder Structure**:
```
AVCHD_ROOT/
├── BDMV/
│   ├── STREAM/
│   │   ├── 00000.MTS   # Video stream
│   │   └── ...
│   ├── CLIPINF/
│   │   ├── 00000.CPI   # Clip information
│   │   └── ...
│   └── PLAYLIST/
│       └── 00000.MPL   # Playlist
└── PRIVATE/
    └── AVCHD/
        └── INDEX.BDM
```

**Metadata Extracted**:
- Clip name from filename
- Duration, timecode, frame rate
- Video codec (H.264), resolution
- Audio configuration
- Recording date/time

### Atomos Provider
**File**: [providers/atomos.py](providers/atomos.py)
**Format**: Atomos External Recorders (.MOV files)

**Features**:
- Reads Atomos QuickTime metadata
- Extracts timecode from file
- Supports ProRes and DNxHD codecs
- Handles Atomos XML sidecar files

**Detection**:
- Extensions: `.MOV`, `.XML`
- Searches root folder

**Metadata Extracted**:
- Clip name from filename
- Timecode track from QuickTime
- Duration, frame rate
- Video codec (ProRes, DNxHD)
- Creation date

### Zoom Provider
**File**: [providers/zoom.py](providers/zoom.py)
**Format**: Zoom Audio/Video Recorders

**Features**:
- Handles various Zoom recorder formats
- Supports both audio and video files
- Extracts basic file metadata
- Simple filename-based organization

**Detection**:
- Extensions: `.WAV`, `.MP3`, `.MOV`, `.MP4`
- Searches root folder

**Metadata Extracted**:
- Filename-based clip name
- File creation date
- Duration, sample rate (audio)
- Basic codec information

### File Provider
**File**: [providers/file.py](providers/file.py)
**Format**: Generic video/audio files

**Features**:
- Fallback provider for unrecognized formats
- Basic file metadata extraction
- Minimal processing
- Supports common video/audio containers

**Detection**:
- Extensions: `.MOV`, `.MP4`, `.MXF`, `.AVI`, `.MKV`, `.WAV`, `.MP3`
- Searches root folder

**Metadata Extracted**:
- Filename as clip name
- File hash as UMID
- File creation date
- Basic file properties

## Creating a Custom Provider

### Step 1: Create Provider File

Create a new file in `providers/` directory:

```python
# providers/myformat.py

from portal.plugins.TapelessIngest.providers.providers import Provider
import os
import logging

log = logging.getLogger(__name__)

class Provider(Provider):
    """Provider for MyFormat camera system."""

    def __init__(self, folder=None):
        super().__init__(folder)
        self.name = "MyFormat Camera"
        self.machine_name = "myformat"
        self.file_extensions = ('.mfc', '.mfx')  # MyFormat extensions
        self.folders_to_ignore = ['PROXY', 'THUMB']
```

### Step 2: Implement Detection

```python
    def getExtensions(self):
        """Return file extensions to search for."""
        return ['.MFC']  # MyFormat metadata files

    def getSubPaths(self):
        """Return subdirectories within folder to search."""
        return ['MEDIA', 'CLIPS']  # MyFormat folder structure
```

### Step 3: Implement Metadata Extraction

```python
    def getMetadatasFromFile(self, file, metadatas, context):
        """
        Extract metadata from MyFormat files.

        Args:
            file: Vidispine file object
            metadatas: Dictionary to populate
            context: Shared context

        Returns:
            (metadatas, context): Updated dictionaries
        """
        file_path = file.getPath()
        filename = os.path.basename(file_path)

        # Only process .MFC files
        if not filename.endswith('.MFC'):
            return metadatas, context

        # Set provider identifier
        metadatas['provider'] = self.machine_name

        # Parse metadata file
        xml = self.parseXML(os.path.join(self.folder.absolute_path, file_path))

        # Extract required fields
        metadatas['umid'] = xml.get('ClipID')  # REQUIRED
        metadatas['clipname'] = xml.get('ClipName')
        metadatas['duration'] = xml.get('Duration')
        metadatas['timecode'] = xml.get('StartTimecode')
        metadatas['framerate'] = xml.get('FrameRate')

        # Extract optional fields
        metadatas['shooting_date'] = xml.get('RecordingDate')
        metadatas['device_manufacturer'] = 'MyFormat'
        metadatas['device_model'] = xml.get('CameraModel')
        metadatas['device_serial'] = xml.get('SerialNumber')

        return metadatas, context
```

### Step 4: Implement File Management

```python
    def getClipMainMediaFile(self, clip):
        """
        Return the main media file to import.

        Args:
            clip: Clip model instance

        Returns:
            dict: File information
        """
        # Derive media filename from metadata filename
        # e.g., CLIP001.MFC -> CLIP001.MFV
        reference_file = clip.reference_file
        media_filename = reference_file.replace('.MFC', '.MFV')
        media_path = os.path.join(clip.path, 'MEDIA', media_filename)

        # Get file ID from Vidispine
        file_id = self.getFileIdFromFullPath(
            os.path.join(clip.root_path, media_path)
        )

        if not file_id:
            log.warning(f"Could not find media file: {media_path}")
            return None

        return {
            'file_id': file_id,
            'path': media_path,
            'type': 'video'
        }

    def getClipAdditionalMediaFiles(self, clip):
        """
        Return additional files (e.g., separate audio tracks).

        Returns:
            list: Additional file information dicts
        """
        additional_files = []

        # Check for separate audio file
        audio_filename = clip.reference_file.replace('.MFC', '.MFA')
        audio_path = os.path.join(clip.path, 'AUDIO', audio_filename)

        file_id = self.getFileIdFromFullPath(
            os.path.join(clip.root_path, audio_path)
        )

        if file_id:
            additional_files.append({
                'file_id': file_id,
                'path': audio_path,
                'type': 'audio'
            })

        return additional_files
```

### Step 5: Handle Spanned Clips (Optional)

```python
    def isSpannedClip(self, clip):
        """Check if clip spans multiple files."""
        # MyFormat indicates spanned clips with _Part suffix
        return '_Part' in clip.metadatas.get('clipname', '')

    def isMasterClip(self, clip):
        """Check if this is the master of a spanned set."""
        clipname = clip.metadatas.get('clipname', '')
        return clipname.endswith('_Part001')

    def getSpannedClips(self, clip):
        """
        Get all clips in spanned set.

        Returns:
            QuerySet: Related Clip objects ordered by part number
        """
        from portal.plugins.TapelessIngest.models.clip import Clip

        if not self.isMasterClip(clip):
            return Clip.objects.none()

        # Get base name without _PartXXX
        base_name = clip.metadatas['clipname'].replace('_Part001', '')

        # Find all clips with same base name
        clips = Clip.objects.filter(
            clipmetadata__name='clipname',
            clipmetadata__value__startswith=base_name
        ).order_by('clipmetadata__value')

        return clips
```

### Step 6: Register Provider

Add to `PROVIDERS_LIST` in [models/clip.py](models/clip.py:50-59):

```python
PROVIDERS_LIST = [
    "red",
    "panasonicP2",
    "xdcam",
    "hdslr",
    "zoom",
    "avchd",
    "atomos",
    "myformat",  # Add your provider
    "file",
]
```

### Step 7: Test Provider

```python
# Test manually in Django shell
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.models.clip import Clip

# Create test folder
folder = Folder(storage_id='VX-1', path='/path/to/myformat/files')
folder.save()

# Scan with your provider
response = folder.scan(providers=['myformat'])
print(f"Found {response['hits']} clips")
print(f"Created {response['created']} new clips")

# Check first clip
if response['clips']:
    clip = response['clips'][0]
    print(f"Clip: {clip.metadatas.get('clipname')}")
    print(f"UMID: {clip.umid}")
    print(f"Provider: {clip.provider_name}")
```

## Best Practices

### Error Handling
```python
def getMetadatasFromFile(self, file, metadatas, context):
    try:
        # Extraction logic
        return metadatas, context
    except Exception as e:
        log.error(f"Error extracting metadata from {file.getPath()}: {e}")
        return metadatas, context
```

### Performance
- Cache parsed XML in `context` to avoid re-parsing
- Use `getFileIdFromFullPath()` method for file lookups
- Minimize filesystem operations

### Metadata Requirements
- Always set `provider` and `umid` keys
- Use consistent naming for standard fields
- Document provider-specific fields

### File Path Handling
- Use `os.path.join()` for cross-platform compatibility
- Use `clip.root_path` for absolute paths
- Handle case-insensitive filesystems

## Troubleshooting

### Provider Not Detecting Files
1. Check `getExtensions()` returns correct extensions
2. Verify `getSubPaths()` matches folder structure
2b. If the format splits a take into numbered files, check
   `getSegmentedExtensions()` declares the suffix in the case your guard
   accepts, and that `getClipAdditionalMediaFiles()` selects exactly the
   siblings the scan drops
3. Test Elasticsearch filters with sample data

### Metadata Not Extracted
1. Verify XML parsing logic
2. Check file paths are correct
3. Add logging to trace execution

### Import Failures
1. Confirm `getClipMainMediaFile()` returns valid file ID
2. Check file exists in Vidispine
3. Verify import options are correct

### Spanned Clips Not Working
1. Verify `isSpannedClip()` detection logic
2. Check `getSpannedClips()` query
3. Ensure master clip is identified correctly

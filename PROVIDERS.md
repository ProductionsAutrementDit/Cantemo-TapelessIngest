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

The metadata-mapping form (`forms.get_provider_metadatas`) offers the
UNION over `PROVIDER_NAMES`: the base provider's 13 keys first, then each
provider's own in registry order, duplicates dropped. A provider that
stores keys of its own (`braw`'s `braw_*`) overrides this hook to list
them after the base keys.

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

#### The main file's own video component

`getClipMainMediaFile` may declare whether Vidispine's shape deduction
will extract a **video component** from the main file itself. The
multi-component import declares up front how many components the
placeholder shape must expect, and Vidispine promotes the shape only
once every declared slot is filled — so a slot declared for essence the
anchor never yields leaves the item holding all its media on a
placeholder for ever: no `original` tag, no transcode, and no error
anywhere.

```python
from portal.plugins.TapelessIngest.providers.providers import (
    MAIN_FILE_YIELDS_VIDEO,
)

def getClipMainMediaFile(self, clip):
    return {
        "type": "video",
        "track": 1,
        "order": 0,
        "file_id": clip.file.getId(),
        "path": clip.file.getPath(),
        # Optional. Absent means True. None means "I could not tell",
        # and a multi-component import is REFUSED on it.
        MAIN_FILE_YIELDS_VIDEO: True,
    }
```

- **Absent, or a non-mapping, reads as `True`.** That is backward
  compatibility — it is what every provider declared implicitly before
  the key existed — and not a claim that `True` is the safe answer.
- **An explicit `None` means "I could not tell", and is not a
  declaration.** `yields_video_component` still folds it into `True`
  for callers that only need a count, but the multi-component import
  reads the raw value (`main_file_verdict_is_unknown`) and **refuses to
  declare a budget on it**: the clip is reported failed, naming the
  main file, before anything is imported. A budget is a claim; a claim
  without evidence is one of the two errors below, and a guess cannot
  know which. The single-component path never reads the key. (Ruled
  2026-09-02.)
- **The two errors are not symmetric.** Over-declaring (counting a video
  slot the main file never fills) is **silent**: the shape stays a
  placeholder for ever. Under-declaring is **loud**: Vidispine refuses
  the main file with `400 {"invalidInput": {"explanation": "No more
  components of that type is accepted", "value": "VIDEO_COMPONENT"}}`
  and the clip is reported failed.
- **Only answer `False` when you can actually tell.** A source Vidispine
  cannot decode yields a *binary* component, which satisfies the
  container slot and no video slot. `red` derives the answer from
  REDline's `Abs TC` (`Provider.anchor_yields_video_component`): a
  whole-field dot-separated timecode means no video component, a
  colon-separated one means a video component, and anything it does not
  recognise answers `None` with a warning — so a RED clip with an
  unreadable `Abs TC` and span files is refused rather than guessed at.
- **`Abs TC` is the only field that decides, and the eight technical
  columns beside it are not a second opinion.** Since 2026-09-22 the
  same `--printMeta 3` row also yields `Frame Width`, `Frame Height`,
  `FPS`, `Record FPS`, `Total Frames`, `File Segments`, `REDCODE` and
  `Camera Audio Channels`, persisted verbatim as `ClipMetadata` rows
  (`REDLINE_TECHNICAL_COLUMNS`). The CAPTURE computes nothing: every
  value is copied exactly as REDline printed it — a string, never an
  int, never a float, never a quotient — and a populated `Frame Width`
  never upgrades a dot-separated anchor's verdict to `True`. The media
  was never the problem, only Vidispine's decoder is, and both clips
  measured on prod carried these columns identically populated. They
  are absent from `getAvailableMetadatas()`, so they are not selectable
  in a metadata mapping. Two of the values mean less than they look
  like: `Total Frames` on the anchor covers the **whole take**, not the
  anchor's segment (`File Segments=2` → `Total Frames=1012` while
  `Clip Out=1011`), and `Camera Audio Channels` can be `2` while `WAV
  Filename` is empty, the audio being inside the `.R3D`. A missing
  column raises `TapelessIngestException` naming it, exactly as the
  seven identifying columns do.
- **They ARE used, at one place and only there: `buildShapeDocument`.**
  When the verdict is `False`, Vidispine deduces no shape at all, so the
  plugin states one — and these are the numbers it states. `Total Frames
  / FPS` reproduces bit for bit the `durationSeconds` Vidispine writes
  when its own deduction succeeds (`1012 / 60` =
  `16.866666666666667`). Converting a captured string into a duration is
  the document's business; doing it at capture time would have put a
  computed number in a table whose other rows are transcriptions. See
  the next section.
- The key has **three** readers, and they answer different questions:
  `providers.providers.yields_video_component` (a count: `None` and an
  absent key both read `True`), `main_file_verdict_is_unknown` (the
  refusal: an explicit `None` is the only value it answers `True` for,
  and `_import_multi_component` refuses to declare a budget on it), and
  `main_file_declares_no_video` (the shape-posting route below: only a
  positive `False`/`0`/`""` selects it — an absent key is a provider
  that never had the question and `None` is the refusal). `models/clip.py`
  never learns what a codec or a timecode is.

#### Composing the shape yourself

```python
def buildShapeDocument(self, main_file, extra_files, metadatas):
    """A complete Vidispine ShapeDocument, or None."""
    return None
```

Consulted **only** when this provider has declared, positively, that the
main file yields no video component. Vidispine then deduces nothing from
it: there is no shape for an import to complete, and the item comes out
with a *binary* component, no duration, no resolution, no codec and no
proxy — `mediaType = 'data'` for a single-file clip, so no duration or
type search ever finds it. The plugin therefore posts a whole `original`
shape (`POST /API/item/{id}/shape/create?tag=original&updateItemMetadata=true`)
and asks for the proxy explicitly, instead of declaring a component
budget and importing.

- **`None` — the default — keeps today's behaviour.** A provider that
  can answer the verdict but cannot describe its own essence must not be
  handed a route that would have to guess a codec, a resolution and a
  frame rate on its behalf. That is what holds this route at `red` and
  `braw`, the two providers that compose one.
- **Be pure.** Dicts in, a dict out: no Vidispine, no filesystem, no
  database, no subprocess. The document a production item receives is
  then readable in a unit test.
- **Post the WHOLE shape, never a patch.** The files named by the
  *video* components must form a `_001`…`_N` set — same folder, same
  stem, no hole — because that is what the transcoder plugin
  reconstructs the take from. Completing the placeholder Vidispine left
  puts the anchor in a binary component and starts the video components
  at `_002`, which it refuses.
- **Refuse rather than invent.** Raise `TapelessIngestException` for
  anything you cannot describe honestly — a metadata value the extractor
  did not capture, a media file with no Vidispine file id, a gap in the
  segment numbering, an extra your document declares no component for.
  The clip is reported failed with that reason and the item keeps a
  clean placeholder, so a corrected clip is a fresh import.
- **State only what was measured.** `red`'s document carries the format
  constants read off a shape Vidispine built *itself* for a decodable
  twin, and the per-clip numbers REDline printed. Fields that were never
  measured for this population — `dropFrame` and the timecode fields on
  a dot-separated anchor — are **omitted**, not guessed.
- **Read a parameter rather than estimating it, when the file declares
  it.** The audio *inside* the `.R3D` gets no component: no captured
  column gives its sample rate, and the proxy comes out with audio
  anyway (pad_forge builds it from the file itself). A **separate
  `.wav`** is the opposite case — 322 RED clips on prod carry one — and
  it declares its own channels, width, rate and length in its header, so
  `red` opens it (`wave`, standard library) and states an
  `audioComponent` from what it read. The read happens in
  `buildShapeDocument`, not in `getClipAdditionalMediaFiles`: the
  ordinary import route never needs those numbers, so it opens nothing
  and a corrupt sidecar cannot break a clip that was importing fine. A
  `.wav` that cannot be read fails the clip by name; a sample width that
  has never been measured omits `sampleFormat` instead of extrapolating
  it.
- **`braw` has no reference shape at all.** Vidispine has never built a
  shape for a `.braw`, so `braw`'s document names the format
  (`braw` / `video/x-braw`) and states only what brawprobe measured and
  the scan stored: frames, snapped frame rate, resolution and — when the
  clip recorded sound — ONE `audioComponent` on the anchor, `itemTrack`
  `A1`, from the SDK's own channel count, rate, bit depth and sample
  count. Pixel format, bit depth, stream ids and timecode fields are
  omitted. The document is built from the stored `ClipMetadata`;
  brawprobe never runs at import.

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

**Metadata Extracted** — the fifteen `--printMeta 3` columns
(`REDLINE_REQUIRED_COLUMNS`), every one of them required and every one
of them copied verbatim:
- Identity and provenance: `Clip Name`, `UUID`, `Abs TC` (timecode),
  `Date` + `Timestamp` (shooting date), `Camera Model`, `Camera PIN`
- Technical, since 2026-09-22 and copied **verbatim**: `Frame Width`,
  `Frame Height`, `FPS`, `Record FPS`, `Total Frames`, `File Segments`,
  `REDCODE`, `Camera Audio Channels`. Nothing is derived at capture
  time; they are converted at one place only, `buildShapeDocument`,
  which states the shape Vidispine could not deduce (see *The main
  file's own video component*)

REDline is invoked **once per clip**; a run that prints no data row, or
a row missing any of the fifteen, raises `TapelessIngestException`
naming REDline, its exit status and what is missing. REDline exits `1`
on success, so the exit status never gates parsing.

**Spanned Clips**:
- Detects when R3D files span multiple parts
- Groups by base filename (e.g., `A001_C001_*.R3D`)
- Creates master clip with related segments

### Blackmagic RAW Provider
**File**: [providers/braw.py](providers/braw.py)
**Format**: Blackmagic RAW (`.braw`, any case)

**Detection**: extension `.braw`, guarded case-insensitively; no card
structure, no sidecar. Registered just before `file`, which does NOT
declare `.braw` and must not.

**Metadata Extracted** — everything, once, at scan time. The provider
runs `brawprobe -- <clip>` (`tools/brawprobe`, see its README) and stores:
- **Every clip-scope and frame-0 metadata key** the SDK yields, as
  `braw_<key>` (key whitespace-trimmed). A frame-0 key goes under
  `braw_frame0_` only when the clip carries the same key
  (`analog_gain`). Values are strings: scalars as text, arrays of 2–4
  numbers joined (`braw_crop_size` = `6048x3200`, `braw_sensor_rate` =
  `48/1`), anything else as compact JSON. Against the 200-character
  `ClipMetadata` columns: a NAME over 200 is skipped with a warning,
  never cut; a JSON value over 200 is skipped (a cut would not be JSON);
  a value read back at import (`braw_probe_clip`, the shape's numbers,
  `braw_anamorphic*`, `clipname`) over 200 refuses the clip; any other
  value is truncated with a warning. Nothing overwrites silently: the
  technical `braw_probe_*` keys win, and a raw key landing on a name
  already stored (`probe_width`, two keys equal once trimmed) is kept as
  `<name>_raw` (`_raw2`…) with a warning. Never stored:
  `post_3dlut_*_data` (the LUT itself; its name, title, size and mode
  are kept) and the `lens_distortion_correction_polynomial*` /
  `lens_shading_*` arrays.
- **The technical fields** as `braw_probe_<field>`: width, height,
  frame count, snapped frame rate and sensor rate (`num`/`den`, plus the
  SDK's reported float), `offspeed`, frame-0 timecode, gamma, gamut,
  audio channels / rate / bits / samples. brawprobe's snapped rates are
  the authority (it carries brawdump's `snapRate`); the Python port
  `snap_rate` only cross-checks the reported floats and logs a warning on
  a disagreement. `offspeed` is snapped sensor rate ≠ snapped frame
  rate.
- **The common keys**: `clipname` (`clip_number`), `timecode`,
  `framerate` (`num/den`), `duration` (seconds), `shooting_date`
  (`date_recorded` as `YYYY-MM-DD`), `device_manufacturer`,
  `device_model` (`camera_type`), `device_serial` (`camera_id`),
  `video_codec` (`Blackmagic RAW 12:1`), `aspect_ratio` (reduced `w:h`).

**Clip key**: `umid = uuid5(BRAW_NAMESPACE, "camera_id|date_recorded|clip_number")`,
each value stripped. `camera_id` identifies the camera, not the clip, and
BRAW has no per-clip id. A clip missing any of the three is refused with
a `TapelessIngestException` naming the key — never keyed on a hash.

**Failures**: brawprobe is resolved like REDline (`Settings.brawprobe_path`,
which must then be an absolute path to an executable, else PATH, else
`/usr/local/bin/brawprobe`) and run as `[binary, "--", path]`, without a
shell. A
non-zero exit (4: the SDK refused the clip) raises
`TapelessIngestException` with the exit status and a stderr excerpt; the
clip is not ingested.

**Import**: `getClipMainMediaFile` declares `MAIN_FILE_YIELDS_VIDEO:
False`, so the clip takes the shape route (`buildShapeDocument`, see
*Composing the shape yourself*) followed by the `lowres-forge` transcode.

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

Add to `PROVIDER_NAMES` in [providers/__init__.py](providers/__init__.py),
the single source of truth (see [docs/adding-a-provider.md](docs/adding-a-provider.md)):

```python
PROVIDER_NAMES = (
    "panasonicP2",
    "xdcam",
    "hdslr",
    "zoom",
    "red",
    "avchd",
    "atomos",
    "braw",
    "myformat",  # Add your provider
    "file",      # always last
)
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

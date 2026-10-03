"""In-memory fakes of the wrapped-migration's OWN protocols.

Not Portal mocks (AD-11 forbids those outside tests/portal_stub): these
stand in for ``wrapped.archive.ArchiveLookup``, ``wrapped.gateway.Gateway``
and ``wrapped.disk.Disk``, which the plugin itself defines.
"""

import copy
import hashlib
import posixpath

from portal.plugins.TapelessIngest.wrapped.archive import (
    ArchiveLookupError,
    Entry,
    Volume,
)
from portal.plugins.TapelessIngest.wrapped.gateway import FileEntity, parse_shape
from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile
from portal.plugins.TapelessIngest.wrapped.span import Segment, segment_files


class FakeArchive:
    def __init__(self):
        self.entries = {}
        self.volumes = {}
        self.failing_folders = set()
        self.folder_calls = []
        self.lookup_calls = []

    def archive(self, abs_path, handle, volumes=("10509",), btime=1506011266, size=1):
        self.entries[abs_path] = Entry(handle, tuple(volumes), btime, size)
        for volume_id in volumes:
            self.volumes.setdefault(
                volume_id, Volume(volume_id, f"BC{volume_id}", f"LABEL.{volume_id}")
            )

    def lookup_folder(self, abs_path):
        self.folder_calls.append(abs_path)
        if abs_path in self.failing_folders:
            raise ArchiveLookupError(f"P5 did not answer for {abs_path}")
        return frozenset(
            posixpath.basename(path)
            for path in self.entries
            if posixpath.dirname(path) == abs_path
        )

    def lookup(self, abs_path):
        self.lookup_calls.append(abs_path)
        return self.entries.get(abs_path)

    def volume(self, volume_id):
        return self.volumes[volume_id]


class FakeDisk:
    def __init__(self, contents=None):
        self.contents = dict(contents or {})

    def exists(self, relative):
        return relative in self.contents

    def sha1(self, relative):
        return hashlib.sha1(self.contents[relative]).hexdigest()

    def size(self, relative):
        data = self.contents.get(relative)
        return None if data is None else len(data)

    def read_text(self, relative):
        data = self.contents.get(relative)
        return None if data is None else data.decode("utf-8-sig")


class InMemoryGateway:
    """A Vidispine that only knows what a test told it."""

    def __init__(self):
        self.shapes = {}  # item_id -> [shape document]
        self.component_md = {}  # (item, shape, component) -> {key: value}
        self.items = {}  # item_id -> {field: [values]}
        self.files = {}  # (storage_id, relative) -> file_id
        # (storage_id, file_id) -> state, None means gone. find_file reads
        # it with default "ARCHIVED" (a VX-41 entity seeded straight into
        # ``files`` is a live archived one unless the test says otherwise);
        # file_state reads it with default "CLOSED" (the wrapped file is
        # online unless the test says otherwise). register_file records
        # "ARCHIVED" or "CLOSED" for what it creates.
        self.file_states = {}
        self.file_sizes = {}  # file_id -> size; absent means unknown (None)
        self.writes = []
        self._minted = 0

    def _mint(self, prefix):
        self._minted += 1
        return f"{prefix}{self._minted}"

    def write_names(self):
        return [write[0] for write in self.writes]

    # reads
    def original_shapes(self, item_id):
        return [
            parse_shape(d)
            for d in self.shapes.get(item_id, [])
            if "original" in d.get("tag", [])
        ]

    def shape_ids(self, item_id, tag):
        return [
            d["id"] for d in self.shapes.get(item_id, []) if tag in d.get("tag", [])
        ]

    def tagged_shapes(self, item_id, tag):
        return [
            parse_shape(d)
            for d in self.shapes.get(item_id, [])
            if tag in d.get("tag", [])
        ]

    def file_size(self, file_id):
        return self.file_sizes.get(file_id)

    def file_items(self, file_id):
        """As ``storage/file/{id}?includeItem=true``: every item one of
        whose shapes (any tag) names the file."""
        return sorted(
            item_id
            for item_id, documents in self.shapes.items()
            if any(
                f.get("id") == file_id
                for document in documents
                for body in _component_bodies(document)
                + document.get("binaryComponent", [])
                for f in body.get("file", [])
            )
        )

    def component_metadata(self, item_id, shape_id, component_id):
        return dict(self.component_md.get((item_id, shape_id, component_id), {}))

    def item_fields(self, item_id, names):
        fields = self.items.get(item_id, {})
        return {name: list(fields[name]) for name in names if name in fields}

    def find_file(self, storage_id, relative):
        file_id = self.files.get((storage_id, relative))
        if not file_id:
            return None
        return FileEntity(
            file_id, self.file_states.get((storage_id, file_id), "ARCHIVED")
        )

    def file_state(self, storage_id, file_id):
        return self.file_states.get((storage_id, file_id), "CLOSED")

    # writes
    def register_file(self, storage_id, relative, archived):
        file_id = self._mint("VX-F")
        self.files[(storage_id, relative)] = file_id
        self.file_states[(storage_id, file_id)] = "ARCHIVED" if archived else "CLOSED"
        self.writes.append(("register_file", storage_id, relative, archived))
        return file_id

    def post_shape(self, item_id, document):
        shape_id = self._mint("VX-S")
        stored = copy.deepcopy(document)
        stored.update({"id": shape_id, "tag": ["original"]})
        paths = {file_id: rel for (_, rel), file_id in self.files.items()}
        bodies = (
            [stored["containerComponent"]] if "containerComponent" in stored else []
        )
        bodies += stored.get("videoComponent", []) + stored.get("audioComponent", [])
        for n, body in enumerate(bodies):
            body["id"] = f"{shape_id}-C{n}"
            body["file"] = [
                {
                    "id": f["id"],
                    "storage": "VX-41",
                    "path": paths.get(f["id"], ""),
                    "state": "CLOSED",
                }
                for f in body.get("file", [])
            ]
        self.shapes.setdefault(item_id, []).append(stored)
        self.writes.append(("post_shape", item_id, copy.deepcopy(document)))
        return shape_id

    def set_component_metadata(self, item_id, shape_id, component_id, fields):
        self.component_md.setdefault((item_id, shape_id, component_id), {}).update(
            fields
        )
        self.writes.append(
            ("set_component_metadata", item_id, shape_id, component_id, dict(fields))
        )

    def set_item_metadata(self, item_id, fields):
        self.items.setdefault(item_id, {}).update({k: [v] for k, v in fields.items()})
        self.writes.append(("set_item_metadata", item_id, dict(fields)))

    def untag_shape(self, item_id, shape_id, tag):
        for document in self.shapes.get(item_id, []):
            if document["id"] == shape_id:
                document["tag"] = [t for t in document.get("tag", []) if t != tag]
        self.writes.append(("untag_shape", item_id, shape_id, tag))

    def delete_file(self, storage_id, file_id):
        self.file_states[(storage_id, file_id)] = None
        self.writes.append(("delete_file", storage_id, file_id))

    def relocate_file(self, storage_id, file_id, new_relative):
        # Measured on VX-10456: the old entity is deleted, a NEW one is
        # created at the new path in state OPEN and swapped into every
        # component that named the old one; component metadata stays.
        (old_key,) = [
            key
            for key, known in self.files.items()
            if key[0] == storage_id and known == file_id
        ]
        if (storage_id, new_relative) in self.files:
            # What Vidispine does then is unmeasured: refuse, loudly.
            raise ValueError(
                f"{new_relative} is already {self.files[(storage_id, new_relative)]}"
            )
        new_id = self._mint("VX-F")
        del self.files[old_key]
        self.files[(storage_id, new_relative)] = new_id
        self.file_states[(storage_id, file_id)] = None
        self.file_states[(storage_id, new_id)] = "OPEN"
        for documents in self.shapes.values():
            for document in documents:
                for body in _component_bodies(document):
                    body["file"] = [
                        (
                            dict(f, id=new_id, path=new_relative, state="OPEN")
                            if f.get("id") == file_id
                            else f
                        )
                        for f in body.get("file", [])
                    ]
        self.writes.append(("relocate_file", storage_id, file_id, new_relative))

    def set_file_state(self, storage_id, file_id, state):
        self.file_states[(storage_id, file_id)] = state
        self.writes.append(("set_file_state", storage_id, file_id, state))


def _component_bodies(document):
    bodies = []
    for key in ("containerComponent", "videoComponent", "audioComponent"):
        raw = document.get(key)
        if raw is not None:
            bodies.extend(raw if isinstance(raw, list) else [raw])
    return bodies


def wrapped_p2_document(
    shape_id="VX-SW",
    file_id="VX-W1",
    storage="VX-2",
    state="ARCHIVED",
    name="060A2B34.MXF",
    audio_count=4,
):
    """A wrapped P2 original shape as Vidispine returns it: every
    component names the ONE wrapped MXF."""
    wrapped = {"id": file_id, "storage": storage, "state": state, "path": name}
    duration = {"samples": 218, "timeBase": {"numerator": 1, "denominator": 25}}
    return {
        "id": shape_id,
        "tag": ["original"],
        "mimeType": ["application/mxf"],
        "containerComponent": {
            "id": f"{shape_id}-C",
            "file": [dict(wrapped)],
            "format": "mxf",
            "duration": duration,
            "metadata": {"field": [{"key": "umid", "value": "WRAPPED"}]},
        },
        "videoComponent": [
            {
                "id": f"{shape_id}-V",
                "file": [dict(wrapped)],
                "codec": "dvvideo",
                "resolution": {"width": 1440, "height": 1080},
                "duration": duration,
                "essenceStreamId": 0,
                "itemTrack": "V1",
            }
        ],
        "audioComponent": [
            {
                "id": f"{shape_id}-A{n}",
                "file": [dict(wrapped)],
                "codec": "pcm_s16le",
                "channelCount": 1,
                "duration": duration,
                "essenceStreamId": n + 1,
                "itemTrack": f"A{n + 1}",
            }
            for n in range(audio_count)
        ],
    }


LEGACY_PATH = "2018/AH_20180314_H160_USA_HPX600_1/060A2B34.MXF"


def doubly_attached_document(copies=None, name="060A2B34.MXF"):
    """A genuine wrapped shape whose every component names TWO entities of
    the same wrapped MXF (measured: 182 items), one per online legacy
    storage. ``copies`` is ``[(file_id, storage, path), ...]``."""
    if copies is None:
        copies = [("VX-W26", "VX-26", LEGACY_PATH), ("VX-W11", "VX-11", name)]
    document = wrapped_p2_document(name=name)
    entities = [
        {"id": file_id, "storage": storage, "state": "CLOSED", "path": path}
        for file_id, storage, path in copies
    ]
    for body in _component_bodies(document):
        body["file"] = [dict(entity) for entity in entities]
    return document


def binary_only_document(
    shape_id="VX-SW",
    file_id="VX-W1",
    storage="VX-2",
    state="ARCHIVED",
    name="060A2B34.MXF",
):
    """An original shape Vidispine never analysed: one ``binaryComponent``
    naming the wrapped MXF, no container/video/audio (measured: 12 items)."""
    wrapped = {"id": file_id, "storage": storage, "state": state, "path": name}
    return {
        "id": shape_id,
        "tag": ["original"],
        "binaryComponent": [{"id": f"{shape_id}-B", "file": [dict(wrapped)]}],
    }


def fileless_document(document=None):
    """``document`` (a genuine wrapped shape by default) with no component
    naming any file (measured: 10 items)."""
    document = copy.deepcopy(document or wrapped_p2_document())
    for body in _component_bodies(document) + document.get("binaryComponent", []):
        body.pop("file", None)
    return document


def p2_originals(clip_dir="2016/AH_TEST/CONTENTS", stem="00924E", audio_count=4):
    return [OriginalFile(f"{clip_dir}/VIDEO/{stem}.MXF", "video")] + [
        OriginalFile(f"{clip_dir}/AUDIO/{stem}{n:02d}.MXF", "audio")
        for n in range(audio_count)
    ]


def seed_item(
    gateway,
    item_id,
    document,
    lowres=("VX-LOW",),
    duration="8.72",
    cpaa_marker=None,
):
    gateway.shapes.setdefault(item_id, []).append(copy.deepcopy(document))
    for shape_id in lowres:
        gateway.shapes[item_id].append({"id": shape_id, "tag": ["lowres"]})
    gateway.items.setdefault(item_id, {})["durationSeconds"] = [duration]
    if cpaa_marker is not None:
        # portal_p5_migration_done, as Cantemo's migrate_cpaa writes it
        gateway.items[item_id]["portal_p5_migration_done"] = [cpaa_marker]


def proxy_copy_document(
    shape_id="VX-SW",
    file_id="VX-W1",
    storage="VX-2",
    state="ARCHIVED",
    name="060A2B34.MXF",
):
    """An original shape that names the wrapped MXF but whose technical
    description is a copy of the lowres proxy (measured: VX-10019)."""
    wrapped = {"id": file_id, "storage": storage, "state": state, "path": name}
    duration = {"samples": 39, "timeBase": {"numerator": 1, "denominator": 25}}
    return {
        "id": shape_id,
        "tag": ["original"],
        "mimeType": ["video/mp4"],
        "containerComponent": {
            "id": f"{shape_id}-C",
            "file": [dict(wrapped)],
            "format": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": duration,
        },
        "videoComponent": [
            {
                "id": f"{shape_id}-V",
                "file": [dict(wrapped)],
                "codec": "h264",
                "resolution": {"width": 480, "height": 272},
                "duration": duration,
                "essenceStreamId": 0,
            }
        ],
        "audioComponent": [
            {
                "id": f"{shape_id}-A0",
                "file": [dict(wrapped)],
                "codec": "aac",
                "channelCount": 2,
                "duration": duration,
                "essenceStreamId": 1,
            }
        ],
    }


def p2_clip_metadata(**overrides):
    """ClipMetadata of an AVC-Intra 100 1080/50i clip (measured: VX-35313)."""
    metadata = {
        "video_codec": "AVC-I_1080/50i",
        "framerate": "50i",
        "EditUnit": "1/25",
        "duration": "497",
        "timecode_start": "18:25:04:12",
        # ~114 Mb/s over 19.88 s
        "data_size": str(114_000_000 * 1988 // 800),
        # not ClipMetadata: the command parses it from Clip.clip_xml
        "audio_bits_per_sample": "24",
    }
    metadata.update(overrides)
    return {k: v for k, v in metadata.items() if v is not None}


def p2_template(audio_codec="pcm_s24le"):
    """A stripped template as ``p2_templates.json`` stores it."""
    return {
        "mimeType": ["application/mxf"],
        "containerComponent": {"format": "mxf_d10", "bitrate": 114000000},
        "videoComponent": [
            {
                "codec": "h264",
                "resolution": {"width": 1920, "height": 1080},
                "pixelFormat": "yuv422p10le",
                "fieldOrder": "tt",
            }
        ],
        "audioComponent": [
            {
                "codec": audio_codec,
                "channelCount": 1,
                "sampleFormat": "s32",
                "timeBase": {"numerator": 1, "denominator": 48000},
            }
        ],
    }


def genuine_p2_document(frames=497, start_tc_frames=1657612, **kwargs):
    """A genuine wrapped shape whose timing agrees with ``p2_clip_metadata``
    (VX-35313: 497 frames at 1/25, starting at 18:25:04:12), each duration
    in the time base Vidispine uses for that component (measured on prod):
    container in microseconds, video in frames, audio in audio samples."""
    document = wrapped_p2_document(**kwargs)
    document["containerComponent"]["duration"] = {
        "samples": frames * 1_000_000 // 25,
        "timeBase": {"numerator": 1, "denominator": 1_000_000},
    }
    document["videoComponent"][0]["duration"] = {
        "samples": frames,
        "timeBase": {"numerator": 1, "denominator": 25},
    }
    for body in document["audioComponent"]:
        body["timeBase"] = {"numerator": 1, "denominator": 48000}
        body["duration"] = {
            "samples": frames * 48000 // 25,
            "timeBase": {"numerator": 1, "denominator": 48000},
        }
    document["containerComponent"]["startTimecode"] = start_tc_frames
    return document


def p2_clip_xml(bits_per_sample="24", namespace=True):
    """The stored P2 clip XML, reduced to what the migration reads
    (measured: VX-39331 has 16 bits, VX-39084 24, both 48 kHz)."""
    xmlns = ' xmlns="urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.1"'
    depth = (
        f"<BitsPerSample>{bits_per_sample}</BitsPerSample>"
        if bits_per_sample is not None
        else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>'
        f"<P2Main{xmlns if namespace else ''}><ClipContent>"
        "<ClipName>00924E</ClipName><EssenceList>"
        "<Video><Codec>AVC-I_1080/50i</Codec></Video>"
        f"<Audio><SamplingRate>48000</SamplingRate>{depth}</Audio>"
        f"<Audio><SamplingRate>48000</SamplingRate>{depth}</Audio>"
        "</EssenceList></ClipContent></P2Main>"
    )


P2_V31 = "urn:schemas-Professional-Plug-in:P2:ClipMetadata:v3.1"


def p2_segment_xml(
    name,
    global_id,
    frames=7482,
    edit_unit="1/25",
    offset=None,
    top="060A2B340101010501010D4313000000AAAA",
    previous=None,
    next_name=None,
    next_id=None,
    audio_count=2,
    namespace=P2_V31,
    drop=(),
):
    """A P2 CLIP XML reduced to what the chain reads, laid out as the
    measured 2015/AH_150108_EC225_SAR_COROGNE documents are."""

    def element(tag, value):
        return "" if value is None or tag in drop else f"<{tag}>{value}</{tag}>"

    connection = (
        "<Connection>"
        f"<Top>{element('GlobalClipID', top)}</Top>"
        + (
            f"<Previous>{element('GlobalClipID', previous)}</Previous>"
            if previous
            else ""
        )
        + (
            f"<Next>{element('ClipName', next_name)}"
            f"{element('GlobalClipID', next_id)}</Next>"
            if next_name or next_id
            else ""
        )
        + "</Connection>"
    )
    audios = "".join(
        "<Audio><AudioFormat>MXF</AudioFormat></Audio>" for _ in range(audio_count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>'
        f'<P2Main xmlns="{namespace}"><ClipContent>'
        + element("ClipName", name)
        + element("GlobalClipID", global_id)
        + element("Duration", frames)
        + element("EditUnit", edit_unit)
        + "<EssenceList><Video><VideoFormat>MXF</VideoFormat></Video>"
        + audios
        + "</EssenceList><Relation>"
        + element("OffsetInShot", offset)
        + connection
        + "</Relation></ClipContent></P2Main>"
    )


SPAN_CONTENTS = "2015/AH_150108_EC225_SAR_COROGNE/CONTENTS"
# 100 + 100 + 50 frames at 1/25: a 10 s take.
SPAN_TAKES = (("0037OO", 100), ("003876", 100), ("0039EX", 50))


def p2_span(takes=SPAN_TAKES, contents=SPAN_CONTENTS, audio_count=4, edit_unit=(1, 25)):
    """A resolved spanned take, master first, as ``resolve_span`` returns it."""
    return [
        Segment(name, *segment_files(contents, name, audio_count), frames, edit_unit)
        for name, frames in takes
    ]


FILE_OUTPUT = "/mnt/ActiveMedia/CANTEMO_FILES/5f1c0de4-6b1e-4f43-9d0c-2a8f6f1e9b10.mov"
FILE_ORIGINAL = "2019/AH_20190402_H175_TEST/A001C003.MOV"
WAV_OUTPUT = "/mnt/ActiveMedia/CANTEMO_FILES/0b7e2c1a-3d4f-4e5a-8b6c-7d8e9f0a1b2c.wav"
WAV_ORIGINAL = "2019/AH_20190402_H175_TEST/SOUND/ZOOM0001.WAV"


def _wrapped_entity(file_id, storage, state, name):
    return {"id": file_id, "storage": storage, "state": state, "path": name}


def file_mov_document(
    shape_id="VX-SW",
    file_id="VX-W1",
    storage="VX-2",
    state="ARCHIVED",
    name=posixpath.basename(FILE_OUTPUT),
    video_codec="prores",
    resolution=(1920, 1080),
    audio_codecs=("pcm_s24le", "pcm_s24le"),
):
    """A genuine wrapped ``file`` shape as Vidispine returns it: container,
    video and audio streams of the ONE copied MOV, told apart by
    ``essenceStreamId`` (measured: 13,638 items)."""
    wrapped = _wrapped_entity(file_id, storage, state, name)
    width, height = resolution
    return {
        "id": shape_id,
        "tag": ["original"],
        "mimeType": ["video/quicktime"],
        "containerComponent": {
            "id": f"{shape_id}-C",
            "file": [dict(wrapped)],
            "format": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": {
                "samples": 19_880_000,
                "timeBase": {"numerator": 1, "denominator": 1_000_000},
            },
            "startTimecode": 1657612,
            "bitrate": 147_000_000,
            "metadata": {"field": [{"key": "major_brand", "value": "qt"}]},
            "mediaInfo": {"property": [{"key": "Format", "value": "MPEG-4"}]},
        },
        "videoComponent": [
            {
                "id": f"{shape_id}-V",
                "file": [dict(wrapped)],
                "codec": video_codec,
                "resolution": {"width": width, "height": height},
                "duration": {
                    "samples": 497,
                    "timeBase": {"numerator": 1, "denominator": 25},
                },
                "essenceStreamId": 0,
                "itemTrack": "V1",
                "mediaInfo": {"property": [{"key": "Format", "value": "ProRes"}]},
            }
        ],
        "audioComponent": [
            {
                "id": f"{shape_id}-A{n}",
                "file": [dict(wrapped)],
                "codec": codec,
                "channelCount": 1,
                "timeBase": {"numerator": 1, "denominator": 48000},
                "duration": {
                    "samples": 954_240,
                    "timeBase": {"numerator": 1, "denominator": 48000},
                },
                "essenceStreamId": n + 1,
                "itemTrack": f"A{n + 1}",
            }
            for n, codec in enumerate(audio_codecs)
        ],
    }


def file_wav_document(
    shape_id="VX-SW",
    file_id="VX-W1",
    storage="VX-26",
    state="CLOSED",
    name=posixpath.basename(WAV_OUTPUT),
):
    """A genuine wrapped audio-only ``file`` shape: one WAV, no video."""
    wrapped = _wrapped_entity(file_id, storage, state, name)
    return {
        "id": shape_id,
        "tag": ["original"],
        "mimeType": ["audio/x-wav"],
        "containerComponent": {
            "id": f"{shape_id}-C",
            "file": [dict(wrapped)],
            "format": "wav",
            "duration": {
                "samples": 12_000_000,
                "timeBase": {"numerator": 1, "denominator": 1_000_000},
            },
        },
        "audioComponent": [
            {
                "id": f"{shape_id}-A0",
                "file": [dict(wrapped)],
                "codec": "pcm_s24le",
                "channelCount": 2,
                "timeBase": {"numerator": 1, "denominator": 48000},
                "duration": {
                    "samples": 576_000,
                    "timeBase": {"numerator": 1, "denominator": 48000},
                },
                "essenceStreamId": 0,
                "itemTrack": "A1",
            }
        ],
    }


def seed_lowres(
    gateway,
    item_id,
    video_codec="h264",
    resolution=(480, 272),
    audio_codecs=("aac",),
    shape_id="VX-LOW",
):
    """Give the item's (bare, seeded) lowres shape a technical description."""
    document = {"id": shape_id, "tag": ["lowres"]}
    if video_codec is not None:
        width, height = resolution
        document["videoComponent"] = [
            {
                "id": f"{shape_id}-V",
                "codec": video_codec,
                "resolution": {"width": width, "height": height},
                "essenceStreamId": 0,
            }
        ]
    document["audioComponent"] = [
        {"id": f"{shape_id}-A{n}", "codec": codec, "essenceStreamId": n + 1}
        for n, codec in enumerate(audio_codecs)
    ]
    shapes = gateway.shapes.setdefault(item_id, [])
    shapes[:] = [d for d in shapes if d["id"] != shape_id] + [document]


def ffprobe_xml(
    video=("prores", 1920, 1080),
    audio=("pcm_s24le", "pcm_s24le"),
    size=123_456_789,
    format_name="mov,mp4,m4a,3gp,3g2,mj2",
    data_stream=True,
):
    """``ffprobe -print_format xml -show_format -show_streams`` of an
    original, as the ``file`` provider stored it in Clip.clip_xml."""
    streams = []
    if video is not None:
        codec, width, height = video
        streams.append(
            f'<stream index="{len(streams)}" codec_name="{codec}" '
            f'codec_type="video" width="{width}" height="{height}"/>'
        )
    for codec in audio:
        streams.append(
            f'<stream index="{len(streams)}" codec_name="{codec}" '
            f'codec_type="audio" sample_rate="48000" channels="1"/>'
        )
    if data_stream:
        streams.append(
            f'<stream index="{len(streams)}" codec_type="data" '
            f'codec_tag_string="tmcd"/>'
        )
    size_attribute = "" if size is None else f' size="{size}"'
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<ffprobe>\n'
        f"<streams>{''.join(streams)}</streams>\n"
        f'<format filename="/x" nb_streams="{len(streams)}" '
        f'format_name="{format_name}"{size_attribute}/>\n</ffprobe>\n'
    )


def ffprobe_route_xml(
    kind="mov",
    duration="8.720000",
    size=123_456_789,
    root="ffprobe",
    drop=(),
    sample_aspect_ratio="1:1",
):
    """ffprobe XML carrying everything the ffprobe route reads (frame rate,
    time base, channels, sample rate, duration). ``kind`` is "mov" (a
    ProRes MOV with a ``tmcd`` data stream at index 0, video 1, audio 2
    and 3), "avchd" (mpegts h264 + ac3) or "mxf" (h264 + pcm in MXF).
    ``drop`` names attributes left out of every stream."""
    aspect = (
        ""
        if sample_aspect_ratio is None
        else f' sample_aspect_ratio="{sample_aspect_ratio}"'
    )
    if kind == "mov":
        format_name = "mov,mp4,m4a,3gp,3g2,mj2"
        streams = [
            '<stream index="0" codec_type="data" codec_tag_string="tmcd"/>',
            '<stream index="1" codec_name="prores" codec_type="video" '
            f'width="1920" height="1080"{aspect} avg_frame_rate="25/1" '
            'time_base="1/25"/>',
            '<stream index="2" codec_name="pcm_s24le" codec_type="audio" '
            'sample_rate="48000" channels="1" time_base="1/48000"/>',
            '<stream index="3" codec_name="pcm_s24le" codec_type="audio" '
            'sample_rate="48000" channels="1" time_base="1/48000"/>',
        ]
    elif kind == "avchd":
        format_name = "mpegts"
        streams = [
            '<stream index="0" codec_name="h264" codec_type="video" '
            f'width="1920" height="1080"{aspect} avg_frame_rate="25/1" '
            'time_base="1/90000"/>',
            '<stream index="1" codec_name="ac3" codec_type="audio" '
            'sample_rate="48000" channels="2" time_base="1/90000"/>',
        ]
    else:
        format_name = "mxf"
        streams = [
            '<stream index="0" codec_name="h264" codec_type="video" '
            f'width="1920" height="1080"{aspect} avg_frame_rate="25/1" '
            'time_base="1/25"/>',
            '<stream index="1" codec_name="pcm_s24le" codec_type="audio" '
            'sample_rate="48000" channels="1" time_base="1/48000"/>',
        ]
    for name in drop:
        streams = [s.replace(f' {name}="', f' x-{name}="') for s in streams]
    top = "<ffprobe>" if root == "ffprobe" else '<Material umid="U1">'
    end = "</ffprobe>" if root == "ffprobe" else "</Material>"
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n{top}\n'
        f"<streams>{''.join(streams)}</streams>\n"
        f'<format filename="/x" nb_streams="{len(streams)}" '
        f'format_name="{format_name}" duration="{duration}" size="{size}"/>\n'
        f"{end}\n"
    )

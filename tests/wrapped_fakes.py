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


class FakeArchive:
    def __init__(self):
        self.entries = {}
        self.volumes = {}
        self.failing_folders = set()
        self.folder_calls = []
        self.lookup_calls = []

    def archive(self, abs_path, handle, volumes=("10509",), btime=1506011266):
        self.entries[abs_path] = Entry(handle, tuple(volumes), btime, 1)
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

    def retag_shape(self, item_id, shape_id, add, remove):
        for document in self.shapes.get(item_id, []):
            if document["id"] == shape_id:
                tags = [t for t in document.get("tag", []) if t != remove]
                document["tag"] = tags + ([add] if add not in tags else [])
        self.writes.append(("retag_shape", item_id, shape_id, add, remove))

    def delete_file(self, storage_id, file_id):
        self.file_states[(storage_id, file_id)] = None
        self.writes.append(("delete_file", storage_id, file_id))


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

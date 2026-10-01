"""The ordered segments of a spanned P2 take.

A spanned take is one shot recorded across several P2 clips. Legacy
wrapping put the whole take in ONE MXF on the master clip's item, so
migrating it means knowing every segment, in order, with its files and
length. Two sources say what they are, both injected here (no Django, no
filesystem): the legacy ``SpannedClips`` rows, already resolved by the
caller, or the P2 CLIP XML of each segment beside the master's VIDEO
folder, walked hop by hop from the master's ``Relation_Next_*`` metadata.

Every link of the XML walk is checked against what the previous hop
promised; anything that does not hold refuses the whole take with a
human reason (``SpanUnresolved``) rather than guessing.
"""

import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable, Iterable, List, Optional, Tuple

from portal.plugins.TapelessIngest.wrapped.paths import OriginalFile

MAX_SEGMENTS = 64


class SpanUnresolved(Exception):
    """The take's chain cannot be trusted; the message says why."""


@dataclass(frozen=True)
class Segment:
    name: str
    video: OriginalFile
    audios: Tuple[OriginalFile, ...]  # kind "audio", in channel order
    frames: int
    edit_unit: Tuple[int, int]  # (num, den)

    @property
    def seconds(self) -> Fraction:
        num, den = self.edit_unit
        return Fraction(self.frames * num, den)


@dataclass(frozen=True)
class MasterIds:
    """What the master's ClipMetadata says about the chain."""

    global_id: str  # Clip.umid
    top_id: Optional[str]
    next_name: Optional[str]
    next_id: Optional[str]
    previous_id: Optional[str] = None


@dataclass(frozen=True)
class ClipXml:
    name: str
    global_id: str
    frames: int
    edit_unit: Tuple[int, int]
    offset: Optional[int]
    top_id: Optional[str]
    previous_id: Optional[str]
    next_name: Optional[str]
    next_id: Optional[str]
    audio_count: int


@dataclass(frozen=True)
class RowIds:
    """What one legacy ``SpannedClips`` row and its clip's ClipMetadata
    say about that segment's place in the take."""

    order: int
    global_id: str  # Clip.umid
    name: str
    frames: int
    top_id: Optional[str]
    previous_id: Optional[str]
    next_id: Optional[str]
    offset: Optional[str]  # Relation_OffsetInShot as stored, if any


def parse_frames(value) -> int:
    try:
        frames = int(str(value).strip())
    except (TypeError, ValueError):
        raise SpanUnresolved(f"duration {value!r} is not a frame count") from None
    if frames <= 0:
        raise SpanUnresolved(f"duration {value!r} is not a frame count")
    return frames


def parse_edit_unit(value) -> Tuple[int, int]:
    try:
        num, den = (int(part) for part in str(value).strip().split("/"))
    except (TypeError, ValueError):
        raise SpanUnresolved(f"EditUnit {value!r} is not num/den") from None
    if num <= 0 or den <= 0:
        raise SpanUnresolved(f"EditUnit {value!r} is not num/den")
    return num, den


def segment_files(
    contents: str, name: str, audio_count: int
) -> Tuple[OriginalFile, Tuple[OriginalFile, ...]]:
    """A segment's files, named as ``providers/panasonicP2.py`` names them."""
    video = OriginalFile(f"{contents}/VIDEO/{name}.MXF", "video")
    audios = tuple(
        OriginalFile(f"{contents}/AUDIO/{name}{n:02d}.MXF", "audio")
        for n in range(audio_count)
    )
    return video, audios


def parse_clip_xml(text: str) -> ClipXml:
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise SpanUnresolved(f"unreadable CLIP XML: {error}") from None
    namespace = root.tag[: root.tag.index("}") + 1] if root.tag[:1] == "{" else ""

    def find(path: str):
        steps = "/".join(namespace + step for step in path.split("/"))
        return root.find(f"{namespace}ClipContent/{steps}")

    def text_of(path: str) -> Optional[str]:
        element = find(path)
        if element is None or not (element.text or "").strip():
            return None
        return element.text.strip()

    def mandatory(path: str) -> str:
        value = text_of(path)
        if value is None:
            raise SpanUnresolved(f"CLIP XML has no {path}")
        return value

    name = mandatory("ClipName")
    global_id = mandatory("GlobalClipID")
    frames = parse_frames(mandatory("Duration"))
    edit_unit = parse_edit_unit(mandatory("EditUnit"))
    offset = text_of("Relation/OffsetInShot")
    if offset is not None:
        try:
            offset = int(offset)
        except ValueError:
            raise SpanUnresolved(f"OffsetInShot {offset!r} is not a frame count")
    essences = find("EssenceList")
    audio_count = 0 if essences is None else len(essences.findall(f"{namespace}Audio"))
    return ClipXml(
        name=name,
        global_id=global_id,
        frames=frames,
        edit_unit=edit_unit,
        offset=offset,
        top_id=text_of("Relation/Connection/Top/GlobalClipID"),
        previous_id=text_of("Relation/Connection/Previous/GlobalClipID"),
        next_name=text_of("Relation/Connection/Next/ClipName"),
        next_id=text_of("Relation/Connection/Next/GlobalClipID"),
        audio_count=audio_count,
    )


def require_head(master_ids: MasterIds) -> None:
    """Refuse a clip that is not the first segment of its take: the P2
    provider's own rule is master <=> Top == GlobalClipID, and a head has
    no Previous."""
    top, umid = master_ids.top_id, master_ids.global_id
    if top and top != umid:
        raise SpanUnresolved(
            f"clip {umid} is not the head of its take: Top {top}, not {umid}"
        )
    if master_ids.previous_id:
        raise SpanUnresolved(
            f"clip {umid} is not the head of its take: "
            f"Previous {master_ids.previous_id}"
        )


def _plain_name(name: str) -> bool:
    return "/" not in name and "\\" not in name and ".." not in name


def chain_from_xml(
    master: Segment,
    master_ids: MasterIds,
    contents: str,
    read_xml: Callable[[str], Optional[str]],
) -> List[Segment]:
    """Master first, then each segment its predecessor's XML points to."""
    require_head(master_ids)
    if not master_ids.next_name and not master_ids.next_id:
        raise SpanUnresolved("master has no next segment")
    top_id = master_ids.top_id or master_ids.global_id
    chain, seen = [master], {master.name}
    previous_id, offset = master_ids.global_id, master.frames
    next_name, next_id = master_ids.next_name, master_ids.next_id
    hop = 1
    while next_name or next_id:
        where = f"segment {next_name or '?'} (hop {hop})"
        if not next_name or not next_id:
            raise SpanUnresolved(f"{where}: next ClipName or GlobalClipID missing")
        if not _plain_name(next_name):
            raise SpanUnresolved(f"{where}: not a plain clip name")
        if next_name in seen:
            raise SpanUnresolved(f"{where}: segment name repeats")
        if len(chain) >= MAX_SEGMENTS:
            raise SpanUnresolved(f"{where}: more than {MAX_SEGMENTS} segments")
        text = read_xml(f"{contents}/CLIP/{next_name}.XML")
        if text is None:
            raise SpanUnresolved(f"{where}: CLIP XML missing")
        try:
            clip = parse_clip_xml(text)
        except SpanUnresolved as error:
            raise SpanUnresolved(f"{where}: {error}") from None
        checks = (
            (clip.name == next_name, f"ClipName {clip.name}, not {next_name}"),
            (
                clip.global_id == next_id,
                f"GlobalClipID {clip.global_id}, not {next_id}",
            ),
            (
                clip.previous_id == previous_id,
                f"Previous {clip.previous_id}, not {previous_id}",
            ),
            (clip.top_id == top_id, f"Top {clip.top_id}, not {top_id}"),
            (
                clip.offset is None or clip.offset == offset,
                f"OffsetInShot {clip.offset}, not {offset}",
            ),
            (
                clip.edit_unit == master.edit_unit,
                f"EditUnit {clip.edit_unit}, not the master's {master.edit_unit}",
            ),
        )
        for holds, why in checks:
            if not holds:
                raise SpanUnresolved(f"{where}: {why}")
        video, audios = segment_files(contents, next_name, clip.audio_count)
        chain.append(Segment(next_name, video, audios, clip.frames, clip.edit_unit))
        seen.add(next_name)
        previous_id, offset = clip.global_id, offset + clip.frames
        next_name, next_id = clip.next_name, clip.next_id
        hop += 1
    return chain


def prove_rows(master_ids: MasterIds, master_frames: int, rows: List[RowIds]) -> None:
    """As strict as the XML walk: the legacy rows (sorted by ``order``,
    the master's self row excluded) must be numbered 2..N, and every link
    their ClipMetadata states must hold, from the master's Next to the last
    segment's empty Next."""
    orders = [row.order for row in rows]
    expected = list(range(2, len(rows) + 2))
    if orders != expected:
        raise SpanUnresolved(f"legacy table orders {orders}, expected {expected}")
    top_id = master_ids.top_id or master_ids.global_id
    previous_id, offset = master_ids.global_id, master_frames
    next_id = master_ids.next_id
    for row in rows:
        where = f"segment {row.name} (row {row.order})"
        if row.offset is None:
            stated = None
        else:
            try:
                stated = int(row.offset)
            except ValueError:
                raise SpanUnresolved(
                    f"{where}: OffsetInShot {row.offset!r} is not a frame count"
                ) from None
        checks = (
            (next_id == row.global_id, f"previous Next {next_id}, not {row.global_id}"),
            (
                row.previous_id == previous_id,
                f"Previous {row.previous_id}, not {previous_id}",
            ),
            (row.top_id == top_id, f"Top {row.top_id}, not {top_id}"),
            (
                stated is None or stated == offset,
                f"OffsetInShot {stated}, not {offset}",
            ),
        )
        for holds, why in checks:
            if not holds:
                raise SpanUnresolved(f"{where}: {why}")
        previous_id, offset = row.global_id, offset + row.frames
        next_id = row.next_id
    if rows and next_id:
        last = rows[-1]
        raise SpanUnresolved(
            f"segment {last.name} (row {last.order}): Next {next_id}, "
            f"but it is the last segment"
        )


def chain_from_rows(master: Segment, rows: Iterable[Segment]) -> List[Segment]:
    """Master first, then the legacy table's segments in the given order."""
    chain, seen = [master], {master.name}
    for row in rows:
        if row.name in seen:
            raise SpanUnresolved(f"segment {row.name}: segment name repeats")
        if row.edit_unit != master.edit_unit:
            raise SpanUnresolved(
                f"segment {row.name}: EditUnit {row.edit_unit}, "
                f"not the master's {master.edit_unit}"
            )
        chain.append(row)
        seen.add(row.name)
    if len(chain) == 1:
        raise SpanUnresolved("legacy table lists no segment besides the master")
    return chain

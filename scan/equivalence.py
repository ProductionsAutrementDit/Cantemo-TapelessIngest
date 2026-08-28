"""FR-4 / FR-34: the equivalence harness that gates the `--discovery` flip.

AD-2 defines equivalence as, per scan root, identical SETS of
``(storage_id, verified_file_path, umid, provider_name, owning_folder_path)``
tuples after verification and extraction, modulo an append-only waiver
list. This module is that relation, made runnable and re-derivable.

**This is an INSTRUMENT.** When ordinary code is wrong it fails; when this
is wrong it CERTIFIES a wrong answer, and a green gate is what unblocks
deleting the legacy path. Three properties therefore matter more here
than anywhere else in the plugin, and each is enforced structurally:

* **No false pass.** Every entry must produce POSITIVE evidence. Zero
  tuples on both sides is not agreement, a walk that recorded a failed
  folder is not agreement, a run whose collector was never invoked is not
  agreement, and a corpus none of whose entries agreed is not
  ``accepted``. A storage that would not resolve, a provider registry
  that would not build, or a corpus path that is not a directory are all
  refused BEFORE the walk rather than degraded into an empty run.
* **No false charge.** The legacy side runs TWICE and is compared to
  itself — tuples AND error strings — before the index path is consulted
  for a charge. Legacy pages an unsorted query with ``from``/``size`` and
  can skip or duplicate documents across pages (``deferred-work.md``,
  grandfathered by AD-3), so a reference that disagrees with itself is an
  instrument fault. It is reported as ``unstable_reference`` and withheld
  PER ENTRY; the corpus's other entries keep their verdicts.
* **Re-derivable evidence.** The canonical document carries no wall-clock
  and no timestamp, so two runs over unchanged data produce byte-identical
  output. Timings are opt-in.

Read-only by construction, three ways over: the context it is handed must
carry ``dry_run`` (checked, not assumed); it drives the walk in the SCAN
shape, so ``Folder.getCollection`` — the one Vidispine mutation a
"read-only" rehearsal could still commit (AD-6/AD-15) — is not reachable;
and no code path here writes a row, an item, or a waiver.

AD-1: stdlib plus ``scan.*`` only. ``process_folder`` (which opens the
ORM) and ``query_elastic`` (Portal's search entry point) are INJECTED, so
this module imports no ``portal.*`` and its unit tests run in the two-tier
substrate.
"""

import fnmatch
import glob as glob_module
import hashlib
import os
import re
import time
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from .context import DISCOVERY_INDEX, DISCOVERY_LEGACY
from .coordinator import SequentialDispatcher, walk_tree
from .discovery import prefetch_index

# ---------------------------------------------------------------------------
# The AD-2 vocabulary
# ---------------------------------------------------------------------------

# The tuple, field by field and in AD-2's own order. Every rendering in
# this module derives its column order from this one tuple.
AD2_FIELDS = (
    "storage_id",
    "verified_file_path",
    "umid",
    "provider_name",
    "owning_folder_path",
)

Ad2Tuple = Tuple[str, str, str, str, str]

# Which side of the comparison a divergence sits on. The two REFERENCE
# labels are deliberately different strings from the two GATE labels: a
# legacy run disagreeing with itself is an instrument fault, and a
# document that spelled it `legacy_only` would let a reader charge it to
# the index path — the exact fold the spec forbids.
SIDE_LEGACY_ONLY = "legacy_only"
SIDE_INDEX_ONLY = "index_only"
SIDE_ANY = "any"
GATE_SIDES = (SIDE_LEGACY_ONLY, SIDE_INDEX_ONLY)
WAIVER_SIDES = (SIDE_LEGACY_ONLY, SIDE_INDEX_ONLY, SIDE_ANY)
SIDE_REFERENCE_FIRST_ONLY = "reference_run_1_only"
SIDE_REFERENCE_SECOND_ONLY = "reference_run_2_only"

# The run-level statuses. FOUR, not three: a corpus typo and a flaky
# reference must not look alike at the CI layer, which is the only layer
# that reads the exit code.
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_UNSTABLE_REFERENCE = "unstable_reference"
STATUS_ERRORED = "errored"

ENTRY_AGREED = "agreed"
ENTRY_DIVERGED = "diverged"
ENTRY_UNSTABLE_REFERENCE = "unstable_reference"
ENTRY_ERRORED = "errored"

ENTRY_STATUSES = (
    ENTRY_AGREED,
    ENTRY_DIVERGED,
    ENTRY_UNSTABLE_REFERENCE,
    ENTRY_ERRORED,
)

# The PROPOSED classification of a divergence. Proposed is the operative
# word: it is a reading aid for the human who decides whether a waiver is
# owed, never an authorization.
CLASS_ABSENT_FROM_INDEX = "absent_from_index"
CLASS_ABSENT_FROM_LEGACY = "absent_from_legacy"
CLASS_PROVIDER_NAME_DRIFT = "provider_name_drift"
CLASS_UMID_DRIFT = "umid_drift"
CLASS_OWNING_FOLDER_DRIFT = "owning_folder_drift"
CLASS_MULTI_FIELD_DRIFT = "multi_field_drift"
CLASS_AMBIGUOUS_COUNTERPART = "ambiguous_counterpart"

_SINGLE_FIELD_CLASSES = {
    "provider_name": CLASS_PROVIDER_NAME_DRIFT,
    "umid": CLASS_UMID_DRIFT,
    "owning_folder_path": CLASS_OWNING_FOLDER_DRIFT,
}

# NFR-5 / E3: prod holds 188,082 clips in 8,133 folders, and a broken
# discovery path could make EVERY one of them a divergence. Both the
# document and the rendering are capped, with the omission stated —
# ``_summarize_names(limit=5)`` and ``_drop_clips`` exist in this repo
# because it has been burned by exactly this.
DOCUMENT_REPORT_LIMIT = 50
CONSOLE_REPORT_LIMIT = 10


class EquivalenceError(Exception):
    """The harness refuses to run, or cannot read the AD-2 tuple."""


class CorpusError(ValueError):
    """The corpus file is empty, unparseable or self-contradictory."""


class WaiverError(ValueError):
    """The waiver file is unparseable, or an entry cites no FR."""


# ---------------------------------------------------------------------------
# Path-segment globbing (D2)
# ---------------------------------------------------------------------------


def glob_match(pattern, value) -> bool:
    """``fnmatch`` per PATH SEGMENT: ``*`` does not cross ``/``.

    Plain ``fnmatch`` would let ``2026/AA_*`` silently cover every card
    subdirectory of every AA_ shoot — a waiver ratified for one file
    quietly suppressing a whole subtree, which is the one thing an
    append-only waiver list must not do. ``**`` crosses separators and
    has to be written; a bare ``*`` as the WHOLE pattern still means
    "anything", because that is what the waiver file tells operators to
    write for a blanket column.
    """
    if pattern == "*":
        return True
    return _segments_match(pattern.split("/"), value.split("/"))


def _segments_match(pattern, value) -> bool:
    if not pattern:
        return not value
    head = pattern[0]
    if head == "**":
        if _segments_match(pattern[1:], value):
            return True
        return bool(value) and _segments_match(pattern, value[1:])
    if not value:
        return False
    if not fnmatch.fnmatchcase(value[0], head):
        return False
    return _segments_match(pattern[1:], value[1:])


# ---------------------------------------------------------------------------
# The corpus and the waiver list: INPUTS, both of them
# ---------------------------------------------------------------------------

# Both files are pipe-delimited rather than JSON/YAML for one reason: the
# corpus is ratified by a human and the waiver list is APPENDED to by a
# human, one line per decision, and a line-oriented file is what makes
# "append-only" reviewable in a diff.
SEPARATOR = "|"

# A `#!` line is a DIRECTIVE, not a comment: `#! ratified: yes`.
DIRECTIVE_PREFIX = "#!"

DEFAULT_CORPUS_FILENAME = "equivalence_corpus.txt"
DEFAULT_WAIVERS_FILENAME = "equivalence_waivers.txt"

_STORAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_FR_RE = re.compile(r"^FR-\d+$")
_DIRECTIVE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def default_corpus_path() -> str:
    """The proposed corpus shipped beside this module."""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), DEFAULT_CORPUS_FILENAME
    )


def default_waivers_path() -> str:
    """The append-only waiver list kept beside this module (AD-2)."""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), DEFAULT_WAIVERS_FILENAME
    )


def _rows(text):
    """``(lineno, kind, payload)`` for every meaningful line.

    ``kind`` is ``"directive"`` for a ``#!`` line and ``"row"`` for a data
    line. A row is split on EVERY separator and the loaders require an
    exact column count — deliberately, and it is the one place this
    format costs its authors something. The first cut used
    ``maxsplit=columns - 1`` so a free-text note could contain ``|``; the
    consequence was that a PATH containing ``|`` silently re-parsed into
    a shorter path plus a note, and the gate then ran, cleanly and
    confidently, over the wrong subtree. An unambiguous parse is worth
    more to an instrument than a pipe in a note.
    """
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith(DIRECTIVE_PREFIX):
            yield lineno, "directive", line[len(DIRECTIVE_PREFIX) :].strip()
            continue
        if line.startswith("#"):
            continue
        yield lineno, "row", [cell.strip() for cell in line.split(SEPARATOR)]


@dataclass(frozen=True)
class CorpusEntry:
    """One scan root the gate is proven over, plus why it is in the corpus.

    ``path`` is a scan ROOT in exactly ``scan_tapeless_dir --path``'s
    sense: ``walk_tree`` treats it as a container and scans everything
    below it, never the root folder itself.
    """

    storage_id: str
    path: str
    note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"storage_id": self.storage_id, "path": self.path, "note": self.note}


@dataclass(frozen=True)
class Corpus:
    """A ratified list of scan roots, with an identity anyone can re-derive."""

    entries: Tuple[CorpusEntry, ...]
    source: str = "<string>"
    ratified: bool = False
    ratification_note: str = ""

    @property
    def digest(self) -> str:
        """SHA-256 over the SCOPE — the ``(storage_id, path)`` pairs.

        Deliberately not over the notes: two verdicts are comparable when
        they covered the same folders, and fixing a typo in a note must
        not read as a different corpus.
        """
        payload = "\n".join(f"{e.storage_id}\t{e.path}" for e in self.entries)
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def storage_ids(self) -> Tuple[str, ...]:
        """Every storage the corpus names, once each, in first-seen order."""
        return tuple(dict.fromkeys(entry.storage_id for entry in self.entries))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "digest": self.digest,
            # F1: a zero-argument run must not produce an
            # authoritative-looking verdict over an unratified scope.
            "ratified": self.ratified,
            "ratification_note": self.ratification_note,
            "entry_count": len(self.entries),
            "entries": [entry.as_dict() for entry in self.entries],
        }


def normalize_corpus_path(path) -> str:
    """Collapse ``//`` and a trailing separator, the way discovery does."""
    return "/".join(part for part in str(path).split("/") if part)


def load_corpus(text, *, source="<string>") -> Corpus:
    """Parse ``storage_id | path | note`` lines into a ``Corpus``.

    Refuses — loudly, naming the line — an entry with a missing column, a
    malformed storage id, an absolute or ``..``-bearing path, a path that
    normalises to the STORAGE ROOT (gating an entire storage by accident
    is not a corpus), a path containing the column separator, a duplicate
    ``(storage_id, path)``, or a path NESTED inside another entry (both
    would run, double-weighting the subtree in the verdict); and refuses a
    corpus with no entries at all. A gate that ran over an empty corpus
    would report ``accepted`` having proven nothing, which is the one
    failure mode a gate must not have.
    """
    entries = []
    seen = {}
    ratified = False
    ratification_note = ""
    for lineno, kind, payload in _rows(text):
        if kind == "directive":
            match = _DIRECTIVE_RE.match(payload)
            if not match:
                raise CorpusError(
                    f"{source}:{lineno}: '#!' introduces a directive "
                    f"('#! name: value'), not a comment"
                )
            name, value = match.group(1).lower(), match.group(2).strip()
            if name == "ratified":
                if value.lower() not in ("yes", "no"):
                    raise CorpusError(
                        f"{source}:{lineno}: ratified must be 'yes' or 'no' "
                        f"(got {value!r})"
                    )
                ratified = value.lower() == "yes"
            elif name == "ratification-note":
                ratification_note = value
            else:
                raise CorpusError(f"{source}:{lineno}: unknown directive {name!r}")
            continue
        cells = payload
        if len(cells) != 3:
            raise CorpusError(
                f"{source}:{lineno}: expected exactly 3 columns "
                f"('storage_id | path | note'), got {len(cells)}; this format "
                f"has no escaping, so neither a path nor a note may contain "
                f"{SEPARATOR!r}"
            )
        storage_id, path, note = cells
        if not _STORAGE_ID_RE.match(storage_id or ""):
            raise CorpusError(f"{source}:{lineno}: {storage_id!r} is not a storage id")
        if not path:
            raise CorpusError(f"{source}:{lineno}: the path column is empty")
        if path.startswith("/"):
            raise CorpusError(
                f"{source}:{lineno}: {path!r} is absolute; corpus paths are "
                f"storage-root-relative, the same coordinate system as "
                f"Folder.path"
            )
        if ".." in path.split("/"):
            raise CorpusError(f"{source}:{lineno}: {path!r} escapes the storage root")
        normalized = normalize_corpus_path(path)
        if not normalized:
            raise CorpusError(
                f"{source}:{lineno}: {path!r} normalises to the storage root; "
                f"gating a whole storage by accident is not a corpus"
            )
        key = (storage_id, normalized)
        if key in seen:
            raise CorpusError(
                f"{source}:{lineno}: {storage_id} {normalized} is already in "
                f"the corpus at line {seen[key]}; scanning it twice would "
                f"double its weight in the verdict"
            )
        for (other_storage, other_path), other_line in seen.items():
            if other_storage != storage_id:
                continue
            if _is_nested(normalized, other_path) or _is_nested(other_path, normalized):
                raise CorpusError(
                    f"{source}:{lineno}: {normalized} overlaps "
                    f"{other_path} from line {other_line}; the shared "
                    f"subtree would be scanned twice and counted twice"
                )
        seen[key] = lineno
        entries.append(CorpusEntry(storage_id=storage_id, path=normalized, note=note))
    if not entries:
        raise CorpusError(f"{source}: the corpus holds no entries")
    return Corpus(
        entries=tuple(entries),
        source=source,
        ratified=ratified,
        ratification_note=ratification_note,
    )


def _is_nested(inner, outer) -> bool:
    """Is ``inner`` at or below ``outer``? Segment-wise, never by prefix."""
    inner_parts = inner.split("/")
    outer_parts = outer.split("/")
    return inner_parts[: len(outer_parts)] == outer_parts


def load_corpus_file(path) -> Corpus:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as e:
        raise CorpusError(f"cannot read the corpus at {path}: {e}") from e
    return load_corpus(text, source=str(path))


@dataclass(frozen=True)
class Waiver:
    """One human-ratified, FR-citing suppression of a known divergence.

    The globs are PATH-SEGMENT patterns (see ``glob_match``) over the
    tuple's own fields, so a waiver reads as the thing it waives and
    cannot silently reach into a subtree. ``fr`` is mandatory: AD-2 admits
    one entry per Feature-G fix, EACH CITING ITS FR, and a waiver with no
    citation is indistinguishable from someone silencing a defect.
    """

    fr: str
    side: str
    folder: str
    file: str
    provider: str
    note: str = ""
    lineno: int = 0

    def matches(self, divergence: "Divergence") -> bool:
        if self.side != SIDE_ANY and self.side != divergence.side:
            return False
        return (
            glob_match(self.folder, divergence.owning_folder_path)
            and glob_match(self.file, divergence.verified_file_path)
            and glob_match(self.provider, divergence.provider_name)
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "fr": self.fr,
            "side": self.side,
            "folder": self.folder,
            "file": self.file,
            "provider": self.provider,
            "note": self.note,
            "lineno": self.lineno,
        }


def load_waivers(text, *, source="<string>") -> Tuple[Waiver, ...]:
    """Parse ``FR | side | folder | file | provider | note`` lines.

    An EMPTY list is valid and is where the list starts (the spec: "the
    waiver list starting empty and append-only"). What is not valid is a
    malformed entry or one that cites no FR — either would let a
    suppression exist that nobody can trace back to a decision.
    """
    waivers = []
    for lineno, kind, payload in _rows(text):
        if kind == "directive":
            raise WaiverError(f"{source}:{lineno}: the waiver list takes no directives")
        cells = payload
        if len(cells) != 6:
            raise WaiverError(
                f"{source}:{lineno}: expected exactly 6 columns ('FR | side | "
                f"folder | file | provider | note'), got {len(cells)}; this "
                f"format has no escaping, so no column may contain "
                f"{SEPARATOR!r}"
            )
        fr, side, folder, file_glob, provider, note = cells
        if not _FR_RE.match(fr or ""):
            raise WaiverError(
                f"{source}:{lineno}: {fr!r} is not an FR citation (expected "
                f"'FR-<number>'); AD-2 admits one waiver per Feature-G fix, "
                f"each citing its FR"
            )
        if side not in WAIVER_SIDES:
            raise WaiverError(
                f"{source}:{lineno}: side {side!r} must be one of "
                f"{', '.join(WAIVER_SIDES)}"
            )
        for name, value in (
            ("folder", folder),
            ("file", file_glob),
            ("provider", provider),
        ):
            if not value:
                raise WaiverError(
                    f"{source}:{lineno}: the {name} column is empty; use '*' "
                    f"to match anything, so that a blanket waiver is written "
                    f"down as one"
                )
        waivers.append(
            Waiver(
                fr=fr,
                side=side,
                folder=folder,
                file=file_glob,
                provider=provider,
                note=note,
                lineno=lineno,
            )
        )
    return tuple(waivers)


def load_waivers_file(path) -> Tuple[Waiver, ...]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as e:
        raise WaiverError(f"cannot read the waiver list at {path}: {e}") from e
    return load_waivers(text, source=str(path))


# ---------------------------------------------------------------------------
# Reading the AD-2 tuple out of a folder's post-extraction state
# ---------------------------------------------------------------------------


def _required_field(value, what, where):
    """A present, non-empty ``str`` — or a refusal.

    ``str(value)`` is what this replaced, and it was a defect of exactly
    the class this module exists to catch: it renders ``None`` as
    ``"None"``, so two clips with null umids compare EQUAL and the gate
    certifies agreement between two broken extractions.
    """
    if value is None:
        raise EquivalenceError(
            f"{where}: {what} is None, so its AD-2 tuple is unreadable"
        )
    if not isinstance(value, str):
        value = str(value)
    if not value:
        raise EquivalenceError(
            f"{where}: {what} is empty, so its AD-2 tuple is unreadable"
        )
    return value


def ad2_tuple(clip, owning_folder_path) -> Ad2Tuple:
    """The AD-2 tuple for one assembled clip.

    ``verified_file_path`` is the scanned file's own storage-root-relative
    path — ``clip.file`` is the ``VSFile`` that PASSED the DC-2
    filesystem guard, which is what makes the field "verified" rather
    than "indexed". ``owning_folder_path`` is the folder the WALK handed
    to the worker, not ``clip.path``: the latter is the file's directory,
    which for a provider sub-path or a RED card is a level (or three)
    below the folder that claimed it, and telling those apart is how a
    duplicate ingest (NFR-1) shows up in the set at all.

    Every field is guarded, not stringified. A tuple the harness cannot
    read honestly must abort the folder, never become a placeholder that
    happens to compare equal to another placeholder.
    """
    where = f"clip {getattr(clip, 'umid', None)!r} in {owning_folder_path}"
    file = getattr(clip, "file", None)
    if file is None:
        raise EquivalenceError(
            f"{where}: no scanned file, so its AD-2 tuple cannot be read"
        )
    return (
        _required_field(getattr(clip, "storage_id", None), "storage_id", where),
        _required_field(file.getPath(), "verified_file_path", where),
        _required_field(getattr(clip, "umid", None), "umid", where),
        _required_field(getattr(clip, "provider_name", None), "provider_name", where),
        _required_field(owning_folder_path, "owning_folder_path", where),
    )


class TupleCollector:
    """A ``process_folder`` decorator that reads the tuples before they die.

    ``walk_tree`` strips the live ``Clip`` objects from every outcome it
    keeps (prod holds 188,082 of them), and ``RunResult`` has no ``clips``
    field at all — so the post-extraction state the AD-2 tuple is defined
    on exists only for the instant between the worker returning and the
    walk recording. This wrapper is that instant.

    Three details are load-bearing:

    * it takes ``*args, **kwargs`` and forces ``ingest=False`` INSIDE
      rather than re-declaring ``walk_tree``'s signature. A re-declared
      signature that drifted would raise ``TypeError`` per folder — caught
      by ``walk_tree``'s dispatch guard, booked as a failed folder, and
      ending in two empty tuple sets. A default of ``number=25`` would be
      worse still: every folder silently truncated to 25 files, with the
      two paths agreeing on the truncation;
    * ``ingest=False`` pins the walk to the SCAN shape.
      ``Folder.getCollection`` — which can CREATE a Vidispine collection
      (AD-6/AD-15) — lives in the ingest pass and is therefore
      unreachable. The AD-2 tuple is defined post-EXTRACTION, so nothing
      inside the gate is lost;
    * a folder contributes ALL of its tuples or NONE. ``ad2_tuple`` can
      refuse mid-folder; adding as we go would leave a partial set that
      the comparison could still call agreement.

    Sequential by contract: the harness uses ``SequentialDispatcher``, so
    the accumulators need no lock and — more importantly — the verdict is
    reproducible.
    """

    def __init__(self, process_folder):
        self._process_folder = process_folder
        self.tuples = set()
        self.folder_paths = set()
        self.calls = 0

    def __call__(self, *args, **kwargs):
        kwargs["ingest"] = False
        outcome = self._process_folder(*args, **kwargs)
        self.calls += 1
        result = outcome.result
        self.folder_paths.add(result.folder_path)
        # All-or-nothing: built into a local list first, so a refusal
        # halfway through leaves the shared set untouched and the folder
        # is booked as failed rather than as partially agreeing.
        found = [ad2_tuple(clip, result.folder_path) for clip in result.clips]
        self.tuples.update(found)
        return outcome


# ---------------------------------------------------------------------------
# Invoking both paths in one process
# ---------------------------------------------------------------------------


def context_factory(base_context) -> Callable[[CorpusEntry, str], Any]:
    """``(entry, mode) -> ScanContext``, off ONE base context.

    The whole point is what it does NOT rebuild. Only
    ``options.discovery`` changes; ``provider_registry``,
    ``extension_map`` and the resolved ``storages`` are the SAME objects
    for both paths, so ``provider_name`` cannot diverge because the two
    sides instantiated different provider instances.

    Three refusals happen HERE, before any walk, because each of them
    would otherwise degrade into a plausible-looking empty or
    non-comparable run:

    * a context that would write. A gate run must be repeatable on
      production without changing anything;
    * a context with no provider registry. ``_build_registry_and_map``
      returns ``(None, None)`` for an unresolvable provider name, and
      ``_scan_pass`` then rebuilds a registry PER FOLDER — which destroys
      the one-registry guarantee AD-2's ``provider_name`` comparison
      rests on. The gate cannot run without it;
    * a storage whose root did not resolve. ``resolve_storages``
      deliberately degrades a bad storage id to ``root_path=None`` so a
      production scan reports per-folder "Cannot get full path" errors
      instead of crashing. For a scan that is mercy; for a GATE it is a
      guaranteed pair of empty tuple sets reported as agreement.
    """
    options = base_context.options
    if not options.dry_run:
        raise EquivalenceError(
            "the equivalence harness refuses a context whose dry_run is not "
            "set: a gate run must be repeatable on production without "
            "changing anything"
        )
    registry = base_context.provider_registry
    if not registry:
        raise EquivalenceError(
            "the equivalence harness refuses a context with no provider "
            "registry: the scan would rebuild one per folder, and AD-2's "
            "provider_name comparison assumes ONE registry for both paths "
            "(check the --providers names resolve)"
        )
    unresolved = sorted(
        storage_id
        for storage_id, info in base_context.storages.items()
        if not info.root_path
    )
    if unresolved:
        raise EquivalenceError(
            f"the equivalence harness refuses a context whose storage(s) "
            f"{', '.join(unresolved)} did not resolve to a browse root: "
            f"every folder would fail and both paths would agree on nothing"
        )

    def context_for(entry: CorpusEntry, mode: str):
        # `discovery_index` is cleared: the index path attaches its own
        # prefetch per entry, and a stale one would have it read another
        # scan root's buckets.
        return dataclass_replace(
            base_context,
            options=dataclass_replace(options, discovery=mode),
            discovery_index=None,
        )

    return context_for


@dataclass(frozen=True)
class PathRun:
    """One discovery path's pass over one corpus entry.

    ``tuples`` and ``folder_paths`` are dropped once the comparison is
    done (``stripped()``): an ``EntryVerdict`` retained for the whole run
    must not pin three full tuple sets per entry in memory.
    """

    mode: str
    tuples: FrozenSet[Ad2Tuple]
    folder_paths: FrozenSet[str]
    tuple_count: int
    folder_count: int
    failed_folders: Tuple[str, ...]
    errors: Tuple[str, ...]
    elapsed: float

    def stripped(self) -> "PathRun":
        return dataclass_replace(self, tuples=frozenset(), folder_paths=frozenset())

    def as_dict(self, *, with_timing=False) -> Dict[str, Any]:
        payload = {
            "mode": self.mode,
            "tuples": self.tuple_count,
            "folders_scanned": self.folder_count,
            "failed_folders": _capped(sorted(self.failed_folders)),
            "errors": _capped(sorted(self.errors)),
        }
        if with_timing:
            payload["elapsed_seconds"] = round(self.elapsed, 3)
        return payload


def _capped(values, limit=DOCUMENT_REPORT_LIMIT):
    """A bounded list that STATES what it dropped."""
    values = list(values)
    if len(values) <= limit:
        return values
    return values[:limit] + [f"... {len(values) - limit} more not shown"]


def build_path_runner(
    *,
    context_for,
    process_folder,
    query_elastic,
    clock=time.monotonic,
) -> Callable[[CorpusEntry, str], PathRun]:
    """``(entry, mode) -> PathRun``: one whole subtree, one discovery path.

    It drives ``walk_tree`` directly rather than ``Folder.scan_tree``
    because the tuples must be read at the ``process_folder`` seam, above
    which they no longer exist. The index path's prefetch is therefore
    done here, exactly as ``scan_tree`` does it — once per scan root,
    BEFORE fan-out, attached with ``dataclasses.replace`` so no context
    is ever mutated (AD-3/AD-4).

    Two guards bracket the walk, and both exist because their absence
    produces a CLEAN-LOOKING empty run rather than a failure:

    * the scan root must be a real directory. A corpus naming a path that
      has been archived away otherwise yields a root listing error, zero
      tuples on both sides, and ``agreed``;
    * the collector must actually have been invoked. If ``walk_tree``
      ever stopped routing through the injected ``process_folder``, every
      entry would come back with two empty sets that compare equal.
    """

    def run(entry: CorpusEntry, mode: str) -> PathRun:
        ctx = context_for(entry, mode)
        if ctx.options.discovery != mode:
            raise EquivalenceError(
                f"the context built for {mode!r} reports "
                f"discovery={ctx.options.discovery!r}"
            )
        if not ctx.options.dry_run:
            raise EquivalenceError(
                "the equivalence harness refuses a context whose dry_run is " "not set"
            )
        absolute = ctx.absolute_path_for(entry.storage_id, entry.path)
        if not absolute or not os.path.isdir(absolute):
            raise EquivalenceError(
                f"the scan root {entry.storage_id} {entry.path} does not "
                f"resolve to a directory ({absolute!r}); a gate cannot prove "
                f"anything about a tree that is not there"
            )
        if mode == DISCOVERY_INDEX:
            index = prefetch_index(
                query_elastic,
                entry.storage_id,
                entry.path,
                page_size=ctx.options.discovery_page_size,
            )
            ctx = dataclass_replace(ctx, discovery_index=index)
        collector = TupleCollector(process_folder)
        dispatcher = SequentialDispatcher()
        started = clock()
        outcomes = walk_tree(
            entry.storage_id,
            entry.path,
            ctx=ctx,
            process_folder=collector,
            dispatch=dispatcher.dispatch,
            gather=dispatcher.gather,
            # The operator report is outside the gate (AD-2 puts counters,
            # timings and error strings there); the harness reads tuples.
            emit=lambda line: None,
        )
        elapsed = clock() - started
        errors = []
        failed = []
        for outcome in outcomes:
            errors.extend(outcome.result.errors)
            if outcome.failed:
                failed.append(outcome.result.folder_path)
        if not collector.calls and not failed:
            raise EquivalenceError(
                f"the walk over {entry.storage_id} {entry.path} never reached "
                f"the tuple collector and reported no failure: the scan root "
                f"is empty, or the walk no longer routes through the injected "
                f"process_folder — either way this entry proves nothing"
            )
        return PathRun(
            mode=mode,
            tuples=frozenset(collector.tuples),
            folder_paths=frozenset(collector.folder_paths),
            tuple_count=len(collector.tuples),
            folder_count=len(collector.folder_paths),
            failed_folders=tuple(sorted(failed)),
            errors=tuple(sorted(set(errors))),
            elapsed=elapsed,
        )

    return run


# ---------------------------------------------------------------------------
# Comparing the sets (AD-2's relation) and classifying what differs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    """One tuple present on one side and not the other, with a reading.

    ``classification`` is PROPOSED. It exists so the human deciding
    whether a waiver is owed can see at a glance whether a file was
    missed entirely or merely attributed differently; it authorizes
    nothing.
    """

    side: str
    classification: str
    values: Ad2Tuple
    differing_fields: Tuple[str, ...] = ()
    counterpart: Optional[Ad2Tuple] = None

    @property
    def storage_id(self) -> str:
        return self.values[0]

    @property
    def verified_file_path(self) -> str:
        return self.values[1]

    @property
    def umid(self) -> str:
        return self.values[2]

    @property
    def provider_name(self) -> str:
        return self.values[3]

    @property
    def owning_folder_path(self) -> str:
        return self.values[4]

    @property
    def sort_key(self):
        """TOTAL, so set-iteration order never leaks into the document.

        A key that ties leaves the surviving order to ``sorted``'s
        stability over a ``set`` — i.e. to ``PYTHONHASHSEED`` — and two
        runs of the same data would then produce different bytes.
        """
        return (
            self.owning_folder_path,
            self.verified_file_path,
            self.umid,
            self.provider_name,
            self.storage_id,
            self.side,
            self.classification,
            self.differing_fields,
            self.counterpart or (),
        )

    def proposed_waiver(self) -> str:
        """The waiver LINE a human would have to ratify to suppress this.

        Emitted so that ratifying one is a copy, a chosen FR and a
        sentence of reasoning — and so that the harness's own output can
        never be mistaken for the waiver list itself: the FR column comes
        out as a placeholder that ``load_waivers`` REFUSES.

        The glob columns are ESCAPED. A real path holding ``*``, ``?`` or
        ``[`` would otherwise be copied verbatim into a pattern, and the
        ratified waiver would silence strictly more than what was
        observed.
        """
        return SEPARATOR.join(
            [
                "FR-?",
                self.side,
                glob_module.escape(self.owning_folder_path),
                glob_module.escape(self.verified_file_path),
                glob_module.escape(self.provider_name),
                f"{self.classification} — state the fix and its FR",
            ]
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "classification": self.classification,
            "tuple": dict(zip(AD2_FIELDS, self.values)),
            "differing_fields": list(self.differing_fields),
            "counterpart": (
                dict(zip(AD2_FIELDS, self.counterpart))
                if self.counterpart is not None
                else None
            ),
            "proposed_waiver": self.proposed_waiver(),
        }


@dataclass(frozen=True)
class FolderDivergence:
    """A folder one path walked into and the other did not.

    Not expressible as an AD-2 tuple, and deliberately NOT waivable. Two
    paths can produce identical tuple sets while descending into
    different folders — that is ``consumed_subdirs`` drift, and the
    direction that matters is the one where a folder gets scanned twice
    and its clips ingested twice (NFR-1). A difference here is a
    structural defect in the descent authorization, not an attribution
    detail somebody signs off with an FR.
    """

    side: str
    folder_path: str

    @property
    def sort_key(self):
        return (self.folder_path, self.side)

    def as_dict(self) -> Dict[str, Any]:
        return {"side": self.side, "folder_path": self.folder_path}


def _by_file(tuples):
    """``(storage_id, verified_file_path) -> [tuple, ...]``.

    The file is the natural identity to look a counterpart up by: it is
    the one field neither path can invent, so a tuple whose file exists on
    both sides but whose umid, provider or owning folder differs is an
    ATTRIBUTION difference, while one whose file exists on one side only
    is a file the other path did not find.
    """
    index = {}
    for values in tuples:
        index.setdefault((values[0], values[1]), []).append(values)
    return index


def _classify(values, counterparts, absent_class):
    others = counterparts.get((values[0], values[1]), ())
    if not others:
        return absent_class, (), None
    if len(others) > 1:
        return CLASS_AMBIGUOUS_COUNTERPART, (), None
    other = others[0]
    differing = tuple(
        name for index, name in enumerate(AD2_FIELDS) if values[index] != other[index]
    )
    if len(differing) == 1 and differing[0] in _SINGLE_FIELD_CLASSES:
        return _SINGLE_FIELD_CLASSES[differing[0]], differing, other
    return CLASS_MULTI_FIELD_DRIFT, differing, other


def compare_tuple_sets(
    left,
    right,
    *,
    left_side=SIDE_LEGACY_ONLY,
    right_side=SIDE_INDEX_ONLY,
    left_absent_class=CLASS_ABSENT_FROM_INDEX,
    right_absent_class=CLASS_ABSENT_FROM_LEGACY,
) -> Tuple[Divergence, ...]:
    """AD-2's relation: the symmetric difference of two tuple SETS.

    Sets, not sequences, and that is the whole design. Legacy sorts each
    ``from``/``size`` page on its own while the index path sorts the scan
    root globally, so the two hand their clips over in different ORDERS
    (the AD-7 divergence story 4.1 pinned). Comparing sequences would
    fail the gate for a reason the spine already sanctions.

    The side labels are parameters because this same relation compares
    the reference run against ITSELF, where ``legacy_only`` would be a
    lie that invites the reader to charge an instrument fault to the
    index path.
    """
    left = frozenset(left)
    right = frozenset(right)
    left_index = _by_file(left)
    right_index = _by_file(right)
    divergences = []
    for values in left - right:
        classification, differing, counterpart = _classify(
            values, right_index, left_absent_class
        )
        divergences.append(
            Divergence(
                side=left_side,
                classification=classification,
                values=values,
                differing_fields=differing,
                counterpart=counterpart,
            )
        )
    for values in right - left:
        classification, differing, counterpart = _classify(
            values, left_index, right_absent_class
        )
        divergences.append(
            Divergence(
                side=right_side,
                classification=classification,
                values=values,
                differing_fields=differing,
                counterpart=counterpart,
            )
        )
    return tuple(sorted(divergences, key=lambda d: d.sort_key))


def compare_folder_sets(
    left, right, *, left_side=SIDE_LEGACY_ONLY, right_side=SIDE_INDEX_ONLY
) -> Tuple[FolderDivergence, ...]:
    """Which folders each path actually walked into (NFR-1's direction)."""
    left = frozenset(left)
    right = frozenset(right)
    divergences = [FolderDivergence(left_side, path) for path in left - right]
    divergences += [FolderDivergence(right_side, path) for path in right - left]
    return tuple(sorted(divergences, key=lambda d: d.sort_key))


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryVerdict:
    """One corpus entry's outcome — and, when withheld or broken, why."""

    entry: CorpusEntry
    status: str
    reference: Optional[PathRun] = None
    reference_second: Optional[PathRun] = None
    index: Optional[PathRun] = None
    reference_self_divergences: Tuple[Divergence, ...] = ()
    reference_error_divergences: Tuple[str, ...] = ()
    divergences: Tuple[Divergence, ...] = ()
    folder_divergences: Tuple[FolderDivergence, ...] = ()
    suppressed: Tuple[Tuple[Divergence, Waiver], ...] = ()
    withheld_divergences: Tuple[Divergence, ...] = ()
    error: Optional[str] = None
    elapsed: float = 0.0

    @property
    def runs(self):
        return tuple(
            run
            for run in (self.reference, self.reference_second, self.index)
            if run is not None
        )

    def counts(self) -> Dict[str, Any]:
        return {
            "reference_tuples": (
                None if self.reference is None else self.reference.tuple_count
            ),
            "reference_second_run_tuples": (
                None
                if self.reference_second is None
                else self.reference_second.tuple_count
            ),
            "index_tuples": None if self.index is None else self.index.tuple_count,
            "folders_scanned": (
                None if self.reference is None else self.reference.folder_count
            ),
            "divergences": len(self.divergences),
            "folder_divergences": len(self.folder_divergences),
            "suppressed": len(self.suppressed),
            "withheld": len(self.withheld_divergences),
            "reference_self_divergences": len(self.reference_self_divergences),
            "failed_folders": len(
                set(f for run in self.runs for f in run.failed_folders)
            ),
            "errors": len(set(e for run in self.runs for e in run.errors)),
        }

    def as_dict(self, *, with_timing=False) -> Dict[str, Any]:
        payload = {
            "storage_id": self.entry.storage_id,
            "path": self.entry.path,
            "note": self.entry.note,
            "status": self.status,
            "counts": self.counts(),
            "reference_self_divergences": [
                d.as_dict()
                for d in self.reference_self_divergences[:DOCUMENT_REPORT_LIMIT]
            ],
            "reference_error_divergences": _capped(
                sorted(self.reference_error_divergences)
            ),
            "divergences": [
                d.as_dict() for d in self.divergences[:DOCUMENT_REPORT_LIMIT]
            ],
            "folder_divergences": [
                d.as_dict() for d in self.folder_divergences[:DOCUMENT_REPORT_LIMIT]
            ],
            "suppressed": [
                {"divergence": d.as_dict(), "waiver": w.as_dict()}
                for d, w in self.suppressed[:DOCUMENT_REPORT_LIMIT]
            ],
            "withheld_divergences": [
                d.as_dict() for d in self.withheld_divergences[:DOCUMENT_REPORT_LIMIT]
            ],
            "errors": _capped(sorted(set(e for run in self.runs for e in run.errors))),
            "failed_folders": _capped(
                sorted(set(f for run in self.runs for f in run.failed_folders))
            ),
            "error": self.error,
            "runs": [run.as_dict(with_timing=with_timing) for run in self.runs],
        }
        if with_timing:
            payload["elapsed_seconds"] = round(self.elapsed, 3)
        return payload


@dataclass(frozen=True)
class Verdict:
    """The document. Machine-readable, and re-derivable from unchanged data."""

    status: str
    corpus: Corpus
    entries: Tuple[EntryVerdict, ...]
    waivers: Tuple[Waiver, ...] = ()
    unmatched_waivers: Tuple[Waiver, ...] = ()
    unmatched_waivers_conclusive: bool = True
    discovery_versions: Optional[Mapping[str, Any]] = None
    scope: Optional[Mapping[str, Any]] = None
    started_at: Optional[str] = None
    elapsed: float = 0.0

    @property
    def accepted(self) -> bool:
        return self.status == STATUS_ACCEPTED

    def totals(self) -> Dict[str, int]:
        counts = {name: 0 for name in ENTRY_STATUSES}
        for entry in self.entries:
            counts[entry.status] += 1
        counts["entries"] = len(self.entries)
        counts["divergences"] = sum(len(e.divergences) for e in self.entries)
        counts["folder_divergences"] = sum(
            len(e.folder_divergences) for e in self.entries
        )
        counts["suppressed"] = sum(len(e.suppressed) for e in self.entries)
        counts["withheld"] = sum(len(e.withheld_divergences) for e in self.entries)
        counts["unmatched_waivers"] = len(self.unmatched_waivers)
        return counts

    def as_dict(self, *, with_timing=False) -> Dict[str, Any]:
        """The canonical document.

        ``with_timing`` defaults to FALSE, and that is the contract: the
        gate's evidence has to be diffable between two runs over
        unchanged data, and a wall-clock or a timestamp in it makes every
        pair of runs differ. Timings are opt-in, for a human reading one
        run's cost.
        """
        payload = {
            "verdict": self.status,
            "relation": {
                "definition": "AD-2",
                "tuple_fields": list(AD2_FIELDS),
                "comparison": "sets",
            },
            "corpus": self.corpus.as_dict(),
            "scope": dict(self.scope or {}),
            "discovery_versions": dict(self.discovery_versions or {}),
            "totals": self.totals(),
            "entries": [e.as_dict(with_timing=with_timing) for e in self.entries],
            "waivers": [w.as_dict() for w in self.waivers],
            "unmatched_waivers": [w.as_dict() for w in self.unmatched_waivers],
            # D4: an entry that never ran could not exercise its waivers,
            # so "this waiver matched nothing" is not yet evidence that it
            # is stale.
            "unmatched_waivers_conclusive": self.unmatched_waivers_conclusive,
        }
        if with_timing:
            payload["elapsed_seconds"] = round(self.elapsed, 3)
            payload["started_at"] = self.started_at
        return payload


def _apply_waivers(divergences, waivers, used):
    """Split into charged and suppressed, marking EVERY matching waiver.

    Suppression stops at the first match (one attribution per
    divergence), but usage does not: an overlapping narrower waiver that
    also covers the divergence is doing its job, and reporting it as
    "matched nothing" would send a human to delete it.
    """
    charged = []
    suppressed = []
    for divergence in divergences:
        matched = [
            position
            for position, waiver in enumerate(waivers)
            if waiver.matches(divergence)
        ]
        for position in matched:
            used[position] = True
        if matched:
            suppressed.append((divergence, waivers[matched[0]]))
        else:
            charged.append(divergence)
    return tuple(charged), tuple(suppressed)


def compare_entry(entry, run_path, *, waivers=(), used=None, clock=time.monotonic):
    """One corpus entry: reference twice, index once, verdict or refusal.

    Order is load-bearing. The reference runs FIRST and TWICE, and its two
    results are compared to each other — tuples AND error strings —
    before the index path is consulted for a charge. Legacy's unsorted
    ``from``/``size`` paging can skip or duplicate documents across pages,
    so a reference that disagrees with itself is an instrument fault: the
    entry becomes ``unstable_reference``, and whatever the index path
    found is reported as WITHHELD rather than as a divergence — a
    distinct field, so no reader and no downstream tool can fold it into
    the gate's failures.

    Before any of that, the entry must have produced EVIDENCE. Three
    conditions each end the entry as ``errored`` rather than as
    agreement, and every one of them was a live false-pass door:

    * any path run booked a failed folder. A folder that died contributed
      no tuples, so both sides can agree on a subtree neither of them
      saw;
    * every run came back with zero tuples. Two empty sets compare equal;
    * the run itself raised (an unresolvable root, a dead prefetch, a
      clip whose tuple could not be read).
    """
    used = [False] * len(waivers) if used is None else used
    started = clock()
    try:
        reference = run_path(entry, DISCOVERY_LEGACY)
        reference_second = run_path(entry, DISCOVERY_LEGACY)
        index = run_path(entry, DISCOVERY_INDEX)
    except Exception as e:
        return EntryVerdict(
            entry=entry,
            status=ENTRY_ERRORED,
            error=f"{type(e).__name__}: {e}",
            elapsed=clock() - started,
        )
    runs = (reference, reference_second, index)
    failed = sorted({folder for run in runs for folder in run.failed_folders})
    if failed:
        return EntryVerdict(
            entry=entry,
            status=ENTRY_ERRORED,
            reference=reference.stripped(),
            reference_second=reference_second.stripped(),
            index=index.stripped(),
            error=(
                f"{len(failed)} folder(s) failed during the gate run "
                f"({', '.join(failed[:5])}{'...' if len(failed) > 5 else ''}); "
                f"a folder that died contributed no tuples, so agreement over "
                f"this entry would be agreement about a subtree neither path "
                f"saw"
            ),
            elapsed=clock() - started,
        )
    if not any(run.tuple_count for run in runs):
        return EntryVerdict(
            entry=entry,
            status=ENTRY_ERRORED,
            reference=reference.stripped(),
            reference_second=reference_second.stripped(),
            index=index.stripped(),
            error=(
                "no clips were assembled on either path, so there is no "
                "positive evidence of equivalence — two empty sets compare "
                "equal"
            ),
            elapsed=clock() - started,
        )
    self_divergences = compare_tuple_sets(
        reference.tuples,
        reference_second.tuples,
        left_side=SIDE_REFERENCE_FIRST_ONLY,
        right_side=SIDE_REFERENCE_SECOND_ONLY,
        left_absent_class=CLASS_ABSENT_FROM_LEGACY,
        right_absent_class=CLASS_ABSENT_FROM_LEGACY,
    )
    # Two identical runs of the same path must also report the same
    # errors. A difference there means the instrument moved between the
    # two readings even where the tuples happened to survive it.
    reference_error_divergences = tuple(
        sorted(set(reference.errors) ^ set(reference_second.errors))
    )
    divergences = compare_tuple_sets(reference.tuples, index.tuples)
    folder_divergences = compare_folder_sets(reference.folder_paths, index.folder_paths)
    # Waivers are matched against every divergence, withheld ones
    # included: a waiver is "used" when the condition it describes was
    # OBSERVED. Reporting a waiver as stale because the folder it covers
    # happened to have an unstable reference this run would send a human
    # to delete a waiver that is doing its job.
    charged, suppressed = _apply_waivers(divergences, waivers, used)
    if self_divergences or reference_error_divergences:
        return EntryVerdict(
            entry=entry,
            status=ENTRY_UNSTABLE_REFERENCE,
            reference=reference.stripped(),
            reference_second=reference_second.stripped(),
            index=index.stripped(),
            reference_self_divergences=self_divergences,
            reference_error_divergences=reference_error_divergences,
            suppressed=suppressed,
            withheld_divergences=charged,
            folder_divergences=folder_divergences,
            elapsed=clock() - started,
        )
    return EntryVerdict(
        entry=entry,
        status=(ENTRY_DIVERGED if (charged or folder_divergences) else ENTRY_AGREED),
        reference=reference.stripped(),
        reference_second=reference_second.stripped(),
        index=index.stripped(),
        divergences=charged,
        folder_divergences=folder_divergences,
        suppressed=suppressed,
        elapsed=clock() - started,
    )


def verdict_status(entries) -> str:
    """The run-level status, in the only precedence that is honest.

    A real divergence is the strongest signal there is, so it wins:
    ``rejected``. An entry that could not be RUN is next, and it gets a
    status of its own — a corpus typo and a flaky reference are different
    faults, and merging them at the exit-code layer destroys the only
    distinction CI can act on.

    Then the spec's own rule, which is why an unstable entry does NOT by
    itself sink the run: instability "invalidates the run's verdict for
    the affected FOLDERS rather than the whole corpus". Legacy pages each
    folder at 100 hits and production has folders far above that, so
    instability is the expected steady state — a mechanism that escalated
    it to the whole run would permanently block the very flip it exists
    to make safe. A run therefore stays ``accepted`` when the entries
    that DID conclude all agreed, with the withheld count stated beside
    it; it becomes ``unstable_reference`` only when NOTHING concluded.
    """
    statuses = [entry.status for entry in entries]
    if ENTRY_DIVERGED in statuses:
        return STATUS_REJECTED
    if ENTRY_ERRORED in statuses:
        return STATUS_ERRORED
    if ENTRY_AGREED not in statuses:
        return STATUS_UNSTABLE_REFERENCE
    return STATUS_ACCEPTED


def run_equivalence(
    corpus,
    run_path,
    *,
    waivers=(),
    discovery_versions=None,
    scope=None,
    clock=time.monotonic,
    emit=None,
    now=None,
) -> Verdict:
    """Run the whole corpus and build the verdict.

    An entry that fails is ``errored`` and the run CONTINUES: a corpus is
    a set of independent scan roots, and one unreadable folder must not
    cost the evidence from the other twenty.

    ``emit`` is called once per entry as it completes. Three walks per
    entry over an 8,000-folder tree is hours of silence otherwise, which
    is the "cannot tell a slow run from a hung one" problem
    ``walk_tree``'s own incremental release exists to prevent.
    """
    if not corpus.entries:
        raise CorpusError(f"{corpus.source}: the corpus holds no entries")
    waivers = tuple(waivers)
    used = [False] * len(waivers)
    started = clock()
    entries = []
    for position, entry in enumerate(corpus.entries, start=1):
        verdict_entry = compare_entry(
            entry, run_path, waivers=waivers, used=used, clock=clock
        )
        entries.append(verdict_entry)
        if emit is not None:
            counts = verdict_entry.counts()
            emit(
                f"[{position}/{len(corpus.entries)}] "
                f"{entry.storage_id} {entry.path}: {verdict_entry.status} "
                f"(reference {counts['reference_tuples']} tuple(s), index "
                f"{counts['index_tuples']}, {counts['divergences']} "
                f"divergence(s))"
            )
    entries = tuple(entries)
    elapsed = clock() - started
    unmatched = tuple(
        waiver for position, waiver in enumerate(waivers) if not used[position]
    )
    return Verdict(
        status=verdict_status(entries),
        corpus=corpus,
        entries=entries,
        waivers=waivers,
        unmatched_waivers=unmatched,
        unmatched_waivers_conclusive=not any(
            entry.status == ENTRY_ERRORED for entry in entries
        ),
        discovery_versions=dict(discovery_versions or {}),
        scope=dict(scope or {}),
        started_at=now,
        elapsed=elapsed,
    )


# ---------------------------------------------------------------------------
# Naming what a verdict is valid FOR
# ---------------------------------------------------------------------------

SOURCE_UNAVAILABLE = "unavailable"


def source_version(text) -> str:
    """A re-derivable identity for a body of source.

    AD-2 requires the verdict to name both paths' versions, and there is
    no version NUMBER to name — the paths are code. A digest of their
    source is the honest answer: it is stable across runs of unchanged
    code and changes the moment any of it does, which is exactly when an
    old verdict stops applying.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def module_versions(groups, read_source) -> Dict[str, Any]:
    """``{name: {"digest": ..., "covers": [...]}}`` over grouped sources.

    Each digest is pinned to the sources it CLAIMS to cover, and the
    claim travels with it. The first cut digested ``build_search_doc``
    alone and called it "legacy" — which was blind to legacy's
    ``from``/``size`` page loop in ``_scan_pass``, the very code whose
    defect ``unstable_reference`` exists for, and blind to extraction,
    verification and the providers that decide three of the tuple's five
    fields. A narrow digest under a broad name is worse than no digest:
    it certifies that unchanged code produced the verdict when the code
    that matters may have changed underneath it.

    ``read_source`` is injected and may fail: a deploy stripped of
    sources must degrade to a NAMED "unavailable" digest, not turn a gate
    run into a traceback.
    """
    versions = {}
    for name, labels in groups.items():
        chunks = []
        unavailable = []
        for label in labels:
            try:
                chunks.append(f"### {label}\n{read_source(label)}")
            except Exception as e:
                unavailable.append(f"{label}: {type(e).__name__}")
        digest = (
            source_version("\n".join(chunks))
            if chunks
            else f"{SOURCE_UNAVAILABLE}:no-source"
        )
        entry = {"digest": digest, "covers": list(labels)}
        if unavailable:
            entry["unavailable"] = unavailable
        versions[name] = entry
    return versions


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_capped(lines, values, prefix, limit=CONSOLE_REPORT_LIMIT):
    values = list(values)
    for value in values[:limit]:
        lines.append(f"{prefix}{value}")
    if len(values) > limit:
        lines.append(f"{prefix}... {len(values) - limit} more not shown")


def render_verdict(verdict) -> Sequence[str]:
    """An operator-readable rendering; the JSON stays the contract.

    It renders the FAILURE evidence too. The first cut printed a clean
    ``[agreed]`` line for an entry whose walk had booked a root listing
    error, because ``errors`` and ``failed_folders`` reached the JSON and
    never the console — and the console is what a human actually reads.
    """
    totals = verdict.totals()
    lines = [f"verdict: {verdict.status}"]
    if not verdict.corpus.ratified:
        lines.append(
            "WARNING: this corpus is NOT RATIFIED — no human has confirmed "
            "that it covers the population FR-4 must be proven over, so this "
            "verdict is a rehearsal, not the gate"
        )
    lines.append(
        f"corpus: {verdict.corpus.source} ({verdict.corpus.digest}), "
        f"{totals['entries']} entr(ies), "
        f"ratified={'yes' if verdict.corpus.ratified else 'no'}"
    )
    for name, value in sorted((verdict.scope or {}).items()):
        lines.append(f"scope[{name}]: {value}")
    for name, value in sorted((verdict.discovery_versions or {}).items()):
        if isinstance(value, dict):
            covers = ", ".join(value.get("covers", ()))
            lines.append(f"discovery[{name}]: {value.get('digest')} covers {covers}")
        else:
            lines.append(f"discovery[{name}]: {value}")
    lines.append(
        f"agreed {totals[ENTRY_AGREED]}, diverged {totals[ENTRY_DIVERGED]}, "
        f"unstable_reference {totals[ENTRY_UNSTABLE_REFERENCE]}, "
        f"errored {totals[ENTRY_ERRORED]}"
    )
    lines.append(
        f"divergences {totals['divergences']} charged, "
        f"{totals['folder_divergences']} folder-set, "
        f"{totals['suppressed']} suppressed by waiver, "
        f"{totals['withheld']} withheld (unstable reference)"
    )
    for entry in verdict.entries:
        counts = entry.counts()
        lines.append(
            f"  [{entry.status}] {entry.entry.storage_id} {entry.entry.path}: "
            f"reference {counts['reference_tuples']} tuple(s), index "
            f"{counts['index_tuples']}, {counts['divergences']} divergence(s), "
            f"{counts['failed_folders']} failed folder(s), "
            f"{counts['errors']} error(s)"
        )
        if entry.error:
            lines.append(f"      error: {entry.error}")
        _render_capped(
            lines,
            sorted(set(f for run in entry.runs for f in run.failed_folders)),
            "      failed folder: ",
        )
        _render_capped(
            lines,
            sorted(set(e for run in entry.runs for e in run.errors)),
            "      error: ",
        )
        _render_capped(
            lines,
            (
                f"[{d.side}] {d.verified_file_path}"
                for d in entry.reference_self_divergences
            ),
            "      reference self-divergence ",
        )
        _render_capped(
            lines,
            entry.reference_error_divergences,
            "      reference error-divergence: ",
        )
        _render_capped(
            lines,
            (f"[{d.side}] {d.folder_path}" for d in entry.folder_divergences),
            "      folder walked on one path only ",
        )
        _render_capped(
            lines,
            (
                f"WITHHELD [{d.side}/{d.classification}] {d.verified_file_path}"
                for d in entry.withheld_divergences
            ),
            "      ",
        )
        shown = entry.divergences[:CONSOLE_REPORT_LIMIT]
        for divergence in shown:
            lines.append(
                f"      [{divergence.side}/{divergence.classification}] "
                f"{divergence.verified_file_path}"
            )
            lines.append(f"        proposed waiver: {divergence.proposed_waiver()}")
        if len(entry.divergences) > len(shown):
            lines.append(
                f"      ... {len(entry.divergences) - len(shown)} more "
                f"divergence(s) not shown; see the JSON verdict"
            )
    if verdict.unmatched_waivers:
        qualifier = (
            ""
            if verdict.unmatched_waivers_conclusive
            else " (INCONCLUSIVE: an "
            "entry errored, so its waivers never had the chance to match)"
        )
        lines.append(
            f"{len(verdict.unmatched_waivers)} waiver(s) matched nothing this "
            f"run — a stale waiver can hide a new defect{qualifier}:"
        )
        for waiver in verdict.unmatched_waivers:
            lines.append(
                f"  line {waiver.lineno}: {waiver.fr} {waiver.side} "
                f"{waiver.folder} {waiver.file} {waiver.provider}"
            )
    return lines

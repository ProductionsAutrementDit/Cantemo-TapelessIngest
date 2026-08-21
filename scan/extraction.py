"""Pure extraction-phase logic: the provider pre-filter and merge loop.

Stdlib-only by contract, like ``scan.context`` and ``scan.verification``:
this module must import with no Portal stub installed, so it holds no
provider imports, no registry instantiation (that lives adapter-side in
``scan.adapters.build_provider_registry``) and no Django. Everything here
takes the already-built registry as an argument.

See ``docs/adding-a-provider.md`` for the contract a provider must hold
up and the traps this pre-filter creates.

Applicability rule
------------------
A provider is applicable to a filename iff
``filename.lower().endswith(suffix.lower())`` for any suffix it declares,
OR it is always-applicable (see below). The result is the union across
every matched suffix bucket plus the always-applicable set, registry-
ordered and deduplicated.

Applicability MUST be a SUPERSET of each provider's runtime guard — the
guard decides, never the filter. Narrowing the filter below a guard
silently changes which provider claims a file, which changes its umid,
which changes the clip's primary key and re-ingests it as a duplicate.

A provider is ALWAYS-APPLICABLE when either holds:

* it declares itself non-extension-guarded
  (``Provider.is_extension_guarded()`` returns ``False``) — the card
  providers whose only runtime guard is sidecar presence, which is
  extension-agnostic: xdcam, panasonicP2, ikegami;
* it declares no usable suffixes at all — an empty declaration means
  unknown reach, and unknown reach must not be silently filtered out.

Applicability is extension-only: extension-only filtering is a strict
superset of extension AND sub-path, so no provider that could match
today is excluded. ``getSubPaths()`` narrowing can come with the
index-path work in Epic 4 if it is ever needed.

Merge rule
----------
Every applicable provider runs, in registry order, and its return value
is merged with ``metadatas.update(...)``. The loop never breaks on first
match, and it does not depend on providers mutating ``metadatas`` in
place: a provider returning a fresh dict contributes too. Later
providers' keys win, EXCEPT that overwriting ``umid`` or ``provider`` —
the two keys that decide the clip's identity — is logged as an error.
Provider exceptions propagate to ``models/folder.py``'s per-file wrapper.

Consumption rule (story 2.6, FR-19/NFR-1)
-----------------------------------------
``consumed_subdirs`` answers ONE question for the recursion: which of a
folder's immediate children were already covered by the clips this
folder's scan produced, and must therefore never be scanned again as
folders of their own — the duplicate-ingest path.

**The governing tie-break is NFR-1: when in doubt, a subdirectory is
CONSUMED.** Over-consuming skips a scan the next cron run redoes;
under-consuming risks an unrecoverable duplicate ingest. That is why
doubt is expressed as ``None`` ("descent is not authorized for this
folder") and NEVER as an empty frozenset, which means the opposite:
"nothing was consumed, descend into everything".
"""

import logging
import os
import re
from collections.abc import Mapping

log = logging.getLogger(__name__)

__all__ = [
    "IDENTITY_KEYS",
    "ConsumedSubdirs",
    "ExtensionMap",
    "applicable_providers",
    "build_extension_map",
    "consumed_subdirs",
    "extract_metadatas",
    "subpath_prefix_patterns",
]

# The keys that decide a clip's primary key and its metadata-mapping
# profile. Silent reassignment of either is a duplicate-ingest bug.
IDENTITY_KEYS = ("umid", "provider")


def _provider_label(provider):
    return getattr(provider, "machine_name", None) or repr(provider)


class ExtensionMap(Mapping):
    """Immutable ``suffix -> tuple(providers)`` map plus the always-applicable set.

    A read-only ``Mapping`` keyed by LOWERCASED suffixes whose values are
    registry-ordered provider tuples. It also carries the registry, so a
    union spanning several buckets can be re-ordered by it, and the
    always-applicable providers, which belong to every file's result.
    """

    __slots__ = ("_buckets", "_registry", "_always", "_ranks")

    def __init__(self, buckets, registry, always=()):
        self._buckets = {suffix: tuple(providers) for suffix, providers in buckets}
        self._registry = tuple(registry)
        self._always = tuple(always)
        # Identity-keyed: provider instances are not hashable-by-value and
        # two distinct instances of the same class must rank separately.
        self._ranks = {
            id(provider): index for index, provider in enumerate(self._registry)
        }

    def __getitem__(self, suffix):
        return self._buckets[suffix]

    def __iter__(self):
        return iter(self._buckets)

    def __len__(self):
        return len(self._buckets)

    def __bool__(self):
        # A map with no suffix buckets but some always-applicable provider
        # is still a usable pre-filter; only a map that can never select
        # anything is falsy, and callers then fall back to the full list.
        return bool(self._buckets or self._always)

    def __repr__(self):
        return f"ExtensionMap({dict(self._buckets)!r}, always={self._always!r})"

    @property
    def registry(self):
        """The registry this map was built from, in registry order."""
        return self._registry

    @property
    def always(self):
        """Providers applicable to every file regardless of its suffix."""
        return self._always

    def rank(self, provider):
        """Registry index of ``provider``; unknown providers sort last."""
        return self._ranks.get(id(provider), len(self._registry))


def _declared_suffixes(provider):
    """Usable lowercased suffixes declared by ``provider``.

    ``getExtensions()`` may return a list, a tuple or a ``dict_keys``
    view; all are materialized. Non-string and empty entries are dropped
    defensively — a provider returning a bare string would otherwise be
    iterated character by character and claim unrelated files.
    """
    declared = provider.getExtensions()
    if declared is None:
        return []
    if isinstance(declared, str):
        log.error(
            f"provider {_provider_label(provider)} returned a bare string from "
            f"getExtensions(); expected a sequence of suffixes — ignoring it"
        )
        return []
    suffixes = []
    for suffix in list(declared):
        if not isinstance(suffix, str) or not suffix:
            log.error(
                f"provider {_provider_label(provider)} declared an unusable "
                f"extension {suffix!r}; ignoring it"
            )
            continue
        suffixes.append(suffix.lower())
    return suffixes


def build_extension_map(registry):
    """Build the ``suffix -> providers`` map (plus always-applicable set).

    A provider declaring the same suffix twice is recorded once, and each
    bucket keeps registry order. A provider that is not extension-guarded,
    or that declares no usable suffix, lands in the always-applicable set
    instead of (not as well as) the suffix buckets.
    """
    buckets = {}
    always = []
    for provider in registry:
        suffixes = _declared_suffixes(provider)
        extension_guarded = True
        is_extension_guarded = getattr(provider, "is_extension_guarded", None)
        if callable(is_extension_guarded):
            extension_guarded = bool(is_extension_guarded())
        if not suffixes or not extension_guarded:
            if not any(known is provider for known in always):
                always.append(provider)
            continue
        for suffix in suffixes:
            bucket = buckets.setdefault(suffix, [])
            if not any(known is provider for known in bucket):
                bucket.append(provider)
    return ExtensionMap(buckets.items(), registry, always)


def applicable_providers(filename, extension_map):
    """Providers that may claim ``filename``, in registry order.

    Mirrors the ES wildcard: case-insensitive ``endswith`` over every
    declared suffix, unioned with the always-applicable set, deduplicated
    by identity.
    """
    if not extension_map:
        return ()
    lowered = filename.lower()
    matched = list(getattr(extension_map, "always", ()))
    for suffix, providers in extension_map.items():
        if not lowered.endswith(suffix):
            continue
        for provider in providers:
            if not any(known is provider for known in matched):
                matched.append(provider)
    rank = getattr(extension_map, "rank", None)
    if rank is not None:
        # Stable sort: a plain-dict extension_map (no rank) keeps the
        # first-appearance order it produced above.
        matched.sort(key=rank)
    return tuple(matched)


def extract_metadatas(media_file, providers, metadatas, context):
    """Run every applicable provider and merge its contribution.

    ``context`` stays an argument and stays provider-mutable in place
    (xdcam's ``mediapro_xml`` cache relies on it); providers return the
    metadatas only.
    """
    owners = {}
    for provider in providers:
        label = _provider_label(provider)
        before = {key: metadatas[key] for key in IDENTITY_KEYS if key in metadatas}
        result = provider.getMetadatasFromFile(media_file, metadatas, context)
        if result is not None and not isinstance(result, Mapping):
            raise TypeError(
                f"provider {label} returned {type(result).__name__} from "
                f"getMetadatasFromFile; expected a mapping of metadatas "
                f"(context is mutated in place, not returned)"
            )
        if result and result is not metadatas:
            metadatas.update(result)
        for key, previous in before.items():
            if metadatas.get(key) != previous:
                log.error(
                    f"provider {label} overwrote {key!r} for "
                    f"{media_file.getPath()}: {previous!r} (set by "
                    f"{owners.get(key, 'an earlier provider')}) -> "
                    f"{metadatas.get(key)!r}"
                )
                owners[key] = label
        for key in IDENTITY_KEYS:
            if key in metadatas and key not in owners:
                owners[key] = label
    return metadatas


class ConsumedSubdirs(frozenset):
    """The immediate child names a folder's scan consumed.

    A real ``frozenset`` of names (layer (a), the file-path layer — the
    actual duplicate barrier), widened at membership time by the provider
    sub-path patterns (layer (b), conservative belt-and-braces).

    Layer (b) cannot contribute NAMES: a ``getSubPaths()`` entry is an ES
    ``regexp``, and this module never lists a directory, so the only place
    a pattern can meet a real child name is the ``name in consumed`` test
    the recursion performs. Hence the widened ``__contains__``.

    Consequences, both deliberate under NFR-1's tie-break:

    * membership is BROADER than equality — ``"CONTENTS" in consumed`` can
      be true while ``consumed == frozenset()``. Only ever in the
      over-consuming direction, which is the safe one;
    * a pattern whose FIRST segment were ``.*`` would match every child
      name and silently kill descent for that folder. No shipped provider
      does this (see ``subpath_prefix_patterns``), and it is safe under
      the tie-break, but it is worth knowing about.
    """

    __slots__ = ("_patterns",)

    def __new__(cls, names=(), patterns=()):
        consumed = super().__new__(cls, names)
        consumed._patterns = tuple(patterns)
        return consumed

    @property
    def patterns(self):
        """The compiled sub-path patterns widening membership (layer (b))."""
        return self._patterns

    def __contains__(self, name):
        if super().__contains__(name):
            return True
        if not isinstance(name, str):
            return False
        return any(pattern.fullmatch(name) for pattern in self._patterns)

    def __repr__(self):
        return (
            f"ConsumedSubdirs({sorted(self)!r}, "
            f"patterns={[p.pattern for p in self._patterns]!r})"
        )


def subpath_prefix_patterns(pattern):
    """``pattern`` plus one truncation at each literal ``/`` outside a class.

    ``getSubPaths()`` entries are ES ``regexp`` patterns matched against
    the FULL folder-relative directory path, and several span more than
    one segment (``CONTENTS/VIDEO``, ``DCIM/([0-9]{3})(GOPRO|…)``). The
    recursion only ever decides about IMMEDIATE children, so an ancestor
    segment has to be able to match on its own: one truncated pattern per
    literal ``/``, with every group left open by the cut closed again.

    ``/`` inside a character class is not a separator (atomos ships
    ``[^/]*_S[0-9]{3}…``), and neither is an escaped ``\\/``. A cut that
    lands inside an alternation spanning a separator (xdcam's
    ``(M4ROOT/|XDROOT/)``) leaves the other branch carrying a ``/``; such
    a branch simply never matches a bare child name, which is harmless.

    Consuming a whole first-level segment (all of ``DCIM/``, not just the
    ``DCIM/001GOPRO`` that actually held clips) is deliberate
    over-consumption under NFR-1's tie-break.

    Returns the original first, then the truncations, deduplicated,
    order-stable. Nothing is compiled here — the caller owns the compile
    so that a compile failure can become DOUBT rather than an exception.
    """
    candidates = [pattern]
    depth = 0
    in_class = False
    escaped = False
    for index, char in enumerate(pattern):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if in_class:
            if char == "]":
                in_class = False
            continue
        if char == "[":
            in_class = True
        elif char == "(":
            depth += 1
        elif char == ")":
            # max(): an unbalanced ')' is the pattern's own problem, and
            # it will surface as a compile failure (i.e. as DOUBT).
            depth = max(depth - 1, 0)
        elif char == "/":
            candidates.append(pattern[:index] + ")" * depth)
    return tuple(dict.fromkeys(candidates))


def _instance_attribute(obj, name):
    """``obj``'s INSTANCE attribute ``name``, never a class descriptor.

    ``Clip.file`` is a lazy property that buys a Vidispine round trip on a
    miss; this module is pure by contract and must never trigger it. The
    scan-attached file lives in ``__dict__`` as ``_file`` (set by
    ``attach_file_metadatas``), and a duck-typed double may expose a plain
    ``file`` attribute instead — both are read here, neither can fire a
    property.
    """
    try:
        return vars(obj).get(name)
    except TypeError:
        return None


def _clip_directories(clip):
    """The directories one clip claims, storage-root-relative.

    Two, unioned: ``clip.path`` — the dir recorded on the row, which for a
    DB-resident clip is a CREATE-time default and may be stale — and the
    dir of the file THIS scan matched. When they disagree both are
    consumed (over-consumption, safe).
    """
    directories = []
    path = getattr(clip, "path", None)
    if path:
        directories.append(path)
    file = _instance_attribute(clip, "_file") or _instance_attribute(clip, "file")
    if file is not None:
        file_path = file.getPath()
        if file_path:
            directories.append(os.path.dirname(file_path))
    return directories


def _consumed_child(directory, folder_path):
    """The immediate child of ``folder_path`` that contains ``directory``.

    ``None`` when there is no child to skip: the file sits in the folder
    itself (``relpath`` is ``os.curdir``) or outside it entirely
    (``os.pardir``, or a ``../`` prefix). The predicate is exact rather
    than a ``startswith(os.pardir)``, so a dot-named child (``..hidden``,
    ``.cache``) is consumed like any other.
    """
    if not directory:
        return None
    relative = os.path.relpath(directory, folder_path or os.curdir)
    if relative == os.curdir or relative == os.pardir:
        return None
    if relative.startswith(os.pardir + os.sep):
        return None
    return relative.split(os.sep)[0]


def consumed_subdirs(found_clips, provider_matches, folder_path, *, reasons=None):
    """Immediate children of ``folder_path`` consumed by its own clips.

    Args:
        found_clips: the clips this folder's scan produced. Duck-typed:
            ``.path`` (storage-root-relative dir) and, when the scan
            attached one, a file exposing ``getPath()``.
        provider_matches: the providers that matched in this folder.
            Duck-typed on ``getSubPaths()``. This is the FOLDER-level
            matched set, which is last-writer-wins per clip and therefore
            does not strictly dominate the true per-clip multi-provider
            match set — layer (b) is a supplement to layer (a), never a
            substitute for it.
        folder_path: **``Folder.path`` — STORAGE-ROOT-RELATIVE**, the same
            coordinate system as ``Clip.path`` and ``VSFile.getPath()``.
            An absolute path would make every ``relpath`` start with
            ``..``, empty the consumed set and send the recursion into
            every child of every folder: duplicates at scale. Guarded.
        reasons: optional list; a doubt reason is appended to it, naming
            the offending pattern, for the caller's ``response["errors"]``.

    Returns:
        A ``ConsumedSubdirs`` (a ``frozenset`` of names, widened by the
        sub-path patterns), or ``None`` for DOUBT — "descent is not
        authorized for this folder". Doubt is NEVER an empty frozenset:
        that is the under-consuming direction, the one that duplicates.

    Raises:
        ValueError: ``folder_path`` is absolute (see above).
    """
    if os.path.isabs(folder_path or ""):
        raise ValueError(
            f"consumed_subdirs needs a storage-root-relative folder_path "
            f"(Folder.path), got the absolute path {folder_path!r}"
        )
    names = set()
    for clip in found_clips or ():
        for directory in _clip_directories(clip):
            child = _consumed_child(directory, folder_path)
            if child:
                names.add(child)
    patterns = []
    for provider in provider_matches or ():
        for subpath in provider.getSubPaths() or ():
            for candidate in subpath_prefix_patterns(subpath):
                try:
                    patterns.append(re.compile(candidate))
                except re.error as error:
                    # NFR-1: an unparsable sub-path on an otherwise
                    # complete pass is DOUBT, never a partial set — the
                    # provider's whole sub-tree would be left unconsumed
                    # and the recursion would re-ingest it.
                    if reasons is not None:
                        reasons.append(f"{subpath!r} does not compile ({error})")
                    return None
    return ConsumedSubdirs(names, patterns)

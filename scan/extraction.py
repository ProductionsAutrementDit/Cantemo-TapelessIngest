"""Pure extraction-phase logic (story 2.3).

Stdlib-only by contract (AD-1), like ``scan.context`` and
``scan.verification``: this module must import with no Portal stub
installed, so it holds NO provider imports, no registry instantiation
(that lives adapter-side in ``scan.adapters.build_provider_registry``)
and no Django. Everything here takes the already-built registry as an
argument.

Three pieces:

``build_extension_map``
    Registry -> immutable ``suffix -> providers`` map. Each provider's
    declared ``getExtensions()`` suffixes are lowercased; a provider that
    returns a ``dict_keys`` view (``providers/file.py``) is list()ed.
    Buckets preserve registry order, and the map remembers the registry
    so unions across buckets can be re-ordered by it.

``applicable_providers``
    The FR-13 pre-filter, mirroring the ES wildcard the search doc uses:
    a provider is applicable to a filename iff
    ``filename.lower().endswith(suffix.lower())`` for any suffix it
    declares. The result is the union across every matched suffix bucket,
    registry-ordered and deduplicated.

    FROZEN PRINCIPLE — pre-filter applicability is a SUPERSET of each
    provider's own runtime guard; the GUARD decides, never the filter.
    That is why ``providers/red.py`` declares BOTH ``"_001.r3d"`` and
    ``".r3d"``: red stays applicable to every ``.r3d``/``.R3D`` file
    exactly as before 2.3, and its internal ``== ".R3D"`` check
    reproduces today's selection byte-for-byte (regression-pinned with
    an uppercase ``X_002.R3D`` row). Narrowing the filter below a guard
    would silently drop clips.

    Applicability is EXTENSION-ONLY (ratified): extension-only filtering
    is a strict superset of extension AND sub-path, so no provider that
    could match today is ever excluded. ``getSubPaths()`` narrowing can
    come with the index-path work in Epic 4 if it is ever needed.

``extract_metadatas``
    The AD-7 merge loop: every applicable provider runs, in registry
    order, and its return value is merged with ``metadatas.update(...)``.
    It NEVER breaks on first match (FR-13/FR-32) and it no longer
    depends on providers mutating ``metadatas`` in place — a provider
    returning a FRESH dict now contributes, where before 2.3 its result
    was silently dropped unless an earlier provider had already set both
    ``provider`` and ``umid``. Later providers' keys win. Provider
    exceptions propagate to ``models/folder.py``'s existing per-file
    wrapper, whose error-string template is unchanged.
"""

from collections.abc import Mapping

__all__ = [
    "ExtensionMap",
    "applicable_providers",
    "build_extension_map",
    "extract_metadatas",
]


class ExtensionMap(Mapping):
    """Immutable ``suffix -> tuple(providers)`` map that knows registry order.

    A read-only ``Mapping`` (AD-4: nothing in the context is mutable),
    keyed by LOWERCASED suffixes, whose values are registry-ordered
    provider tuples. It also carries the registry itself so
    ``applicable_providers`` can order a union that spans several buckets
    (a plain ``dict`` works too — it just falls back to first-appearance
    order).
    """

    __slots__ = ("_buckets", "_registry", "_ranks")

    def __init__(self, buckets, registry):
        self._buckets = {suffix: tuple(providers) for suffix, providers in buckets}
        self._registry = tuple(registry)
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

    def __repr__(self):
        return f"ExtensionMap({dict(self._buckets)!r})"

    @property
    def registry(self):
        """The registry this map was built from, in registry order."""
        return self._registry

    def rank(self, provider):
        """Registry index of ``provider``; unknown providers sort last."""
        return self._ranks.get(id(provider), len(self._registry))


def build_extension_map(registry):
    """Build the lowercased ``suffix -> providers`` map for ``registry``.

    ``getExtensions()`` may return a list (most providers) or a
    ``dict_keys`` view (``providers/file.py``); both are materialized.
    A provider declaring the same suffix twice is recorded once, and
    each bucket keeps registry order.
    """
    buckets = {}
    for provider in registry:
        for suffix in list(provider.getExtensions()):
            bucket = buckets.setdefault(suffix.lower(), [])
            if not any(known is provider for known in bucket):
                bucket.append(provider)
    return ExtensionMap(buckets.items(), registry)


def applicable_providers(filename, extension_map):
    """Providers whose declared suffixes match ``filename`` (registry order).

    Mirrors the ES wildcard: case-insensitive ``endswith`` over every
    declared suffix. Union across matched buckets, deduplicated by
    identity, ordered by the registry.
    """
    if not extension_map:
        return ()
    lowered = filename.lower()
    matched = []
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
    """Run every applicable provider and merge its contribution (AD-7).

    ``context`` stays an argument and stays provider-mutable IN PLACE
    (xdcam's ``mediapro_xml`` cache relies on it); providers no longer
    return it. Never breaks on first match.
    """
    for provider in providers:
        result = provider.getMetadatasFromFile(media_file, metadatas, context)
        if result:
            metadatas.update(result)
    return metadatas

"""Tier 1 (story 3.1): registry providers are write-once — proven, executable.

Story 3.1's Precondition 1 is discharged by PROOF, not by per-worker
instantiation: Epic 2 already moved per-run provider state off ``self``
and into the per-invocation ``provider_context`` dict, so the shared
instances ``Clip._PROVIDER_CACHE`` hands out are safe to call from many
worker threads at once. Per-worker instantiation was rejected for a
load-bearing reason: ``scan.extraction.ExtensionMap`` ranks providers by
``id(provider)``, so fresh per-worker instances would silently require
per-worker extension maps too.

This guard makes that proof executable instead of narrative: every class
in the MRO of every ``PROVIDER_NAMES`` provider is AST-scanned for a
``self.<attr> = ...`` (or ``+=``/annotated) assignment outside
``__init__``, and ANY hit fails the suite. A provider change that
reintroduces per-run state on ``self`` breaks here, before it can race
under the pool.

Documented exclusions — both OUTSIDE the registry, so outside the proof:

* ``providers/jvcprohd.py`` — a REAL mutator (``self.base_path``,
  ``self.clips_path``, ``self.card_xml_file`` in ``checkPath``,
  ``self.clips_xml`` in ``getAllClips``). Not in ``PROVIDER_NAMES``; its
  card-era API has zero callers (see deferred-work.md, AD-10 pass). It
  doubles below as the positive control that the scanner catches what it
  claims to.
* ``providers/audio_files.py`` — does not parse at all (SyntaxError at
  line 98, pre-existing; see deferred-work.md). Not in ``PROVIDER_NAMES``,
  so it can never be handed to a worker — but any future "instantiate
  every provider module" approach must exclude it explicitly.
"""

import ast
import importlib
import inspect
import textwrap

from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES

PROVIDER_PACKAGE = "portal.plugins.TapelessIngest.providers"


def _self_writes_outside_init(cls):
    """Every ``self.<attr>`` assignment outside ``__init__`` in ``cls``.

    Scans the class's OWN source only; callers walk the MRO so inherited
    methods are scanned on the class that defines them.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    [class_def] = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    writes = []
    for method in class_def.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name == "__init__":
            continue
        for node in ast.walk(method):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    writes.append(
                        f"{cls.__module__}.{cls.__qualname__}."
                        f"{method.name}:{node.lineno} writes self.{target.attr}"
                    )
    return writes


def _registry_provider_classes():
    """The Provider class of every registry name, imported, NOT instantiated.

    Import only: instantiating real providers here would populate
    ``Clip._PROVIDER_CACHE`` with real instances behind the tests' backs.
    """
    for name in PROVIDER_NAMES:
        module = importlib.import_module(f"{PROVIDER_PACKAGE}.{name}")
        yield name, module.Provider


def test_registry_providers_never_write_self_outside_init():
    """The write-once proof: shared instances are safe under the pool.

    Walks the full MRO of every registry provider class, so a mutation
    hidden in a shared base class (``providers.providers.Provider``,
    ``providers.file.Provider``) fails exactly like one in the leaf.
    """
    violations = []
    for name, provider_class in _registry_provider_classes():
        for cls in inspect.getmro(provider_class):
            if cls is object or not cls.__module__.startswith(PROVIDER_PACKAGE):
                continue
            violations.extend(_self_writes_outside_init(cls))
    assert not violations, (
        "registry provider(s) write per-run state on self outside __init__ — "
        "shared provider instances are no longer safe under the story-3.1 "
        "worker pool (move the state into the per-invocation "
        "provider_context, as Epic 2 did):\n" + "\n".join(sorted(set(violations)))
    )


def test_the_scanner_catches_a_real_mutator():
    """Guards the guard: jvcprohd IS a mutator and the scanner must say so.

    If the AST sweep ever degrades into finding nothing anywhere, the
    write-once test above would pass vacuously; the known-mutating,
    non-registry provider proves the scanner still bites.
    """
    module = importlib.import_module(f"{PROVIDER_PACKAGE}.jvcprohd")
    writes = []
    for cls in inspect.getmro(module.Provider):
        if cls is object or not cls.__module__.startswith(PROVIDER_PACKAGE):
            continue
        writes.extend(_self_writes_outside_init(cls))
    assert any("checkPath" in write for write in writes), writes
    assert any("self.base_path" in write for write in writes), writes


def test_jvcprohd_and_audio_files_are_not_in_the_registry():
    """The exclusions stay exclusions: the proof covers the registry only.

    The day either name joins ``PROVIDER_NAMES``, the write-once guard
    must start covering it (and audio_files.py must first parse at all) —
    this pin turns that silent widening into a loud one.
    """
    assert "jvcprohd" not in PROVIDER_NAMES
    assert "audio_files" not in PROVIDER_NAMES


def test_a_seeded_cache_entry_wins_over_the_fresh_instance(monkeypatch):
    """The `setdefault` race fix, deterministically (story 3.1 review).

    The thread interleaving — B writes the cache between A's membership
    check and A's write — is reproduced without threads: a provider whose
    ``__init__`` seeds its own cache slot IS that interleaving, executed
    inside ``get_provider_by_name``. The old check-then-set overwrote the
    seeded instance and returned the fresh one, handing two callers two
    instances of one name; ``setdefault`` must return the seeded one.
    ``ExtensionMap`` ranks by ``id(provider)``, so instance identity is
    load-bearing, not cosmetic.

    A REAL registry module's ``Provider`` attribute is monkeypatched
    (plugin code, AD-11 untouched) rather than injecting a module, so
    ``get_provider_by_name``'s own ``__import__`` path is the one under
    test.
    """
    import importlib

    from portal.plugins.TapelessIngest.models.clip import Clip

    name = "zoom"
    zoom_module = importlib.import_module(f"{PROVIDER_PACKAGE}.{name}")
    sentinel = object()

    class SeedingProvider:
        def __init__(self):
            # The "other thread's" write, landing mid-construction.
            Clip._PROVIDER_CACHE[name] = sentinel

    monkeypatch.setattr(zoom_module, "Provider", SeedingProvider)
    prior = Clip._PROVIDER_CACHE.pop(name, None)
    try:
        resolved = Clip.get_provider_by_name(name)
        assert resolved is sentinel
        assert Clip._PROVIDER_CACHE[name] is sentinel
    finally:
        Clip._PROVIDER_CACHE.pop(name, None)
        if prior is not None:
            Clip._PROVIDER_CACHE[name] = prior

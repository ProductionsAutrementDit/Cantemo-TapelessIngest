"""Tier 1 (story 2.3): scan/extraction.py — the pure extraction phase.

Portal-freedom is proven the same way 2.1/2.2 prove it for scan.context
and scan.verification: a bare subprocess with NO stub installed.

The rest covers the spec's I/O matrix on pure logic:

- extension-map build: lowercasing, ``dict_keys`` materialization
  (providers/file.py), registry-ordered buckets, red's TWO declared
  suffixes;
- the pre-filter: applicable / zero-applicable, case-insensitive
  ``endswith``, union across matched buckets deduplicated and
  registry-ordered;
- the AD-7 merge loop: every applicable provider runs (never break on
  first match), fresh-dict returns are merged instead of dropped, later
  keys win, and a raising provider's exception propagates to the caller;
- the REGRESSION PIN for the frozen superset principle: an uppercase
  non-``_001`` ``X_002.R3D`` file still reaches the real ``red`` provider,
  whose real ``== ".R3D"`` guard wins provider/umid selection over the
  real ``file`` provider's ``"provider" not in metadatas`` guard —
  byte-identical selection to pre-2.3.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
from portal.plugins.TapelessIngest.scan.adapters import (
    build_context,
    build_provider_registry,
)
from portal.plugins.TapelessIngest.scan.extraction import (
    applicable_providers,
    build_extension_map,
    extract_metadatas,
)
from tests.portal_stub import VSFile

REPO_ROOT = Path(__file__).resolve().parents[2]

# cwd=repo root, no stub, no conftest: `python -c` puts the cwd on sys.path,
# so `scan.extraction` resolves to this repo's package in a bare interpreter.
PORTAL_FREEDOM_SCRIPT = (
    "import sys, scan.extraction; "
    "assert not [m for m in sys.modules if m == 'portal' or m.startswith('portal.')]"
)


class StubProvider:
    """Minimal provider double for the pure-logic rows (no Portal at all)."""

    def __init__(self, machine_name, extensions, contribution=None, raises=None):
        self.machine_name = machine_name
        self._extensions = extensions
        self._contribution = contribution
        self._raises = raises
        self.calls = []

    def getExtensions(self):
        return self._extensions

    def getMetadatasFromFile(self, media_file, metadatas, context):
        self.calls.append(media_file)
        if self._raises is not None:
            raise self._raises
        if self._contribution is None:
            return None
        # A FRESH dict, deliberately: pre-2.3 this contribution was dropped
        # unless an earlier provider had already set provider AND umid.
        return dict(self._contribution)


def _vsfile(path, file_id="F-1", storage="VX-41"):
    return VSFile(
        {"path": path, "hash": "h", "storage": storage, "id": file_id, "size": 1}
    )


def test_extraction_module_is_portal_free_in_bare_interpreter():
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_FREEDOM_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan.extraction is not Portal-free in a bare interpreter (AD-1):\n"
        f"{result.stderr.decode(errors='replace')}"
    )


# --------------------------------------------------------------------------
# build_extension_map
# --------------------------------------------------------------------------


def test_map_lowercases_and_preserves_registry_order():
    first = StubProvider("first", [".MXF", ".Mp4"])
    second = StubProvider("second", [".mxf"])
    registry = (first, second)

    extension_map = build_extension_map(registry)

    assert set(extension_map) == {".mxf", ".mp4"}
    assert extension_map[".mxf"] == (first, second)
    assert extension_map[".mp4"] == (first,)
    assert extension_map.registry == registry


def test_map_is_read_only():
    extension_map = build_extension_map([StubProvider("only", [".fake"])])

    with pytest.raises(TypeError):
        extension_map[".fake"] = ()


def test_map_materializes_dict_keys_views():
    # providers/file.py returns `self.file_types.keys()`, not a list.
    file_provider = build_provider_registry(["file"])[0]
    declared = file_provider.getExtensions()
    assert not isinstance(declared, (list, tuple))

    extension_map = build_extension_map([file_provider])

    assert {".mov", ".mxf", ".r3d", ".wav", ".mp3"} <= set(extension_map)
    assert extension_map[".r3d"] == (file_provider,)


def test_red_declares_both_narrow_and_broad_r3d_suffixes():
    red = build_provider_registry(["red"])[0]

    # The frozen superset principle: the narrow ES-wildcard form AND the
    # bare extension the runtime guard actually accepts.
    assert red.getExtensions() == ["_001.r3d", ".r3d"]


# --------------------------------------------------------------------------
# applicable_providers
# --------------------------------------------------------------------------


def test_applicable_is_case_insensitive_endswith():
    provider = StubProvider("p", [".MXF"])
    extension_map = build_extension_map([provider])

    assert applicable_providers("CLIP.mxf", extension_map) == (provider,)
    assert applicable_providers("CLIP.MXF", extension_map) == (provider,)
    assert applicable_providers("CLIP.MxF", extension_map) == (provider,)


def test_zero_applicable_providers_returns_empty_tuple():
    extension_map = build_extension_map([StubProvider("p", [".mxf"])])

    assert applicable_providers("CLIP.zzz", extension_map) == ()
    # An empty/None map short-circuits rather than raising.
    assert applicable_providers("CLIP.mxf", {}) == ()
    assert applicable_providers("CLIP.mxf", None) == ()


def test_union_across_matched_buckets_is_registry_ordered_and_deduplicated():
    # `narrow` matches only via "_001.r3d"; `broad` only via ".r3d";
    # `both` is in BOTH buckets and must appear exactly once, in registry
    # order (last), not twice and not first.
    narrow = StubProvider("narrow", ["_001.r3d"])
    both = StubProvider("both", ["_001.r3d", ".r3d"])
    broad = StubProvider("broad", [".r3d"])
    # Registry order deliberately differs from bucket-insertion order.
    registry = (broad, narrow, both)
    extension_map = build_extension_map(registry)

    assert applicable_providers("A_001.r3d", extension_map) == (broad, narrow, both)
    # Not matching the narrow bucket drops `narrow` only.
    assert applicable_providers("A_002.r3d", extension_map) == (broad, both)


def test_real_registry_prefilter_matches_the_matrix_rows():
    registry = build_provider_registry()
    extension_map = build_extension_map(registry)
    by_name = {provider.machine_name: provider for provider in registry}

    assert tuple(p.machine_name for p in registry) == PROVIDER_NAMES

    # Uppercase non-_001 R3D: red applicable through its ".r3d" bucket,
    # ahead of `file` (registry order), so red's guard decides.
    r3d = applicable_providers("X_002.R3D", extension_map)
    assert by_name["red"] in r3d
    assert r3d.index(by_name["red"]) < r3d.index(by_name["file"])
    # ...and the classic _001 form is unchanged.
    assert applicable_providers("X_001.R3D", extension_map) == r3d

    # A .wav never invokes the video providers.
    assert tuple(
        p.machine_name for p in applicable_providers("Z.wav", extension_map)
    ) == (
        "zoom",
        "file",
    )
    # A suffix no provider declares invokes nothing at all.
    assert applicable_providers("README.txt", extension_map) == ()


# --------------------------------------------------------------------------
# extract_metadatas (AD-7)
# --------------------------------------------------------------------------


def test_merge_runs_every_provider_in_order_and_later_keys_win():
    first = StubProvider("first", [".fake"], {"provider": "first", "umid": "U1"})
    second = StubProvider("second", [".fake"], {"umid": "U2", "extra": "second"})
    media_file = _vsfile("2026/AH_x/CLIP.fake")
    context = {}

    metadatas = extract_metadatas(media_file, (first, second), {}, context)

    # Never breaks on first match: BOTH ran (FR-13/FR-32)...
    assert first.calls == [media_file]
    assert second.calls == [media_file]
    # ...both FRESH-dict contributions are present, later keys winning.
    assert metadatas == {"provider": "first", "umid": "U2", "extra": "second"}


def test_merge_keeps_in_place_mutation_working():
    class InPlaceProvider:
        machine_name = "inplace"

        def getExtensions(self):
            return [".fake"]

        def getMetadatasFromFile(self, media_file, metadatas, context):
            metadatas["provider"] = self.machine_name
            metadatas["umid"] = "U-INPLACE"
            context["touched"] = True
            return metadatas

    context = {}
    metadatas = extract_metadatas(
        _vsfile("2026/AH_x/CLIP.fake"), (InPlaceProvider(),), {}, context
    )

    assert metadatas == {"provider": "inplace", "umid": "U-INPLACE"}
    # `context` stays an argument and stays mutable IN PLACE (AD-7).
    assert context == {"touched": True}


def test_merge_tolerates_a_provider_contributing_nothing():
    quiet = StubProvider("quiet", [".fake"], None)
    loud = StubProvider("loud", [".fake"], {"provider": "loud", "umid": "U"})

    metadatas = extract_metadatas(_vsfile("2026/AH_x/CLIP.fake"), (quiet, loud), {}, {})

    assert metadatas == {"provider": "loud", "umid": "U"}


def test_provider_exception_propagates_and_stops_the_file():
    boom = StubProvider("boom", [".fake"], raises=RuntimeError("provider exploded"))
    never = StubProvider("never", [".fake"], {"provider": "never"})

    with pytest.raises(RuntimeError, match="provider exploded"):
        extract_metadatas(_vsfile("2026/AH_x/CLIP.fake"), (boom, never), {}, {})

    # The caller (models/folder.py's per-file wrapper) records the error;
    # no later provider silently papers over it.
    assert never.calls == []


def test_zero_applicable_providers_yield_untouched_metadatas():
    metadatas = extract_metadatas(_vsfile("2026/AH_x/CLIP.zzz"), (), {}, {})

    # No umid -> Clip.get_clip_from_file raises "No UMID found in file ..."
    assert metadatas == {}


# --------------------------------------------------------------------------
# Regression pin: the uppercase non-_001 R3D matrix row, real providers
# --------------------------------------------------------------------------


def test_uppercase_non_001_r3d_selection_is_byte_identical(storage_fake, monkeypatch):
    """`X_002.R3D` must still be a RED clip, not a `file` clip.

    Both real guards run: red's ``file_extension == ".R3D"`` and
    providers/file.py's ``"provider" not in metadatas``. Only red's
    REDline shell-out is replaced (unavailable off-server) — the guard
    that decides selection, and therefore the clip's primary key, is the
    real one.
    """
    storage_fake.set_root("VX-R3D", "/mnt/r3d")
    registry = build_provider_registry(["red", "file"])
    red, file_provider = registry
    extension_map = build_extension_map(registry)

    def _fake_redline(media_absolute_path, metadatas):
        assert media_absolute_path == "/mnt/r3d/2026/AH_x/A001_C002.RDC/X_002.R3D"
        metadatas["umid"] = "RED-UUID-X002"
        return metadatas

    monkeypatch.setattr(red, "getAllClipMetadatas", _fake_redline)

    ctx = build_context(
        ["VX-R3D"],
        user=None,
        dry_run=True,
        providers=["red", "file"],
        legacy_storages=[],
        replace=False,
    )
    media_file = _vsfile("2026/AH_x/A001_C002.RDC/X_002.R3D", storage="VX-R3D")

    providers = applicable_providers(media_file.getFileName(), extension_map)
    assert providers == (red, file_provider)

    metadatas = extract_metadatas(
        media_file, providers, {}, {"scan_context": ctx, "clips": []}
    )

    # Red won: provider name and UMID are red's, so the clip's primary key
    # is unchanged — no PK flip, no duplicate-ingest risk.
    assert metadatas["provider"] == "red"
    assert metadatas["umid"] == "RED-UUID-X002"
    assert metadatas["extension"] == ".R3D"
    # `file` ran (never break on first match) but its own guard declined,
    # so it never shelled out to ffprobe and never overrode red.
    assert metadatas["clipname"] == "X_002"

"""Tier 1: scan/extraction.py — the pure pre-filter and merge logic.

Portal-freedom is proven the way scan.context and scan.verification
prove theirs: a bare subprocess with no stub installed.

Covered here:

- extension-map build: lowercasing, ``dict_keys`` materialization,
  registry-ordered buckets, unusable declarations skipped, and the
  always-applicable set (non-extension-guarded providers, and providers
  declaring nothing at all);
- the pre-filter: case-insensitive ``endswith``, union across matched
  buckets deduplicated and registry-ordered;
- the merge loop: every applicable provider runs, fresh-dict returns are
  merged, later keys win, reassigning an identity key is logged, a
  legacy two-tuple return fails by name, exceptions propagate;
- registry construction: the healthy path carries a pre-filter, an
  unresolvable name degrades exactly once and raises where it always
  did, and a broken declaration propagates instead of degrading;
- the regression pin for the superset rule: an uppercase non-``_001``
  ``X_002.R3D`` still reaches the real ``red`` provider, whose real
  ``== ".R3D"`` guard beats the real ``file`` provider's guard.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
from portal.plugins.TapelessIngest.scan.adapters import (
    build_context,
    build_provider_registry,
)
from portal.plugins.TapelessIngest.scan.extraction import (
    ConsumedSubdirs,
    applicable_providers,
    build_extension_map,
    consumed_subdirs,
    extract_metadatas,
    subpath_prefix_patterns,
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

    # The card providers guard on sidecar presence, not on the extension,
    # so they stay applicable to EVERY file — including a .wav, which
    # they do claim today when the sidecar is there.
    assert tuple(
        p.machine_name for p in applicable_providers("Z.wav", extension_map)
    ) == ("panasonicP2", "xdcam", "zoom", "file")
    # A suffix no provider declares still reaches the card providers only.
    assert tuple(
        p.machine_name for p in applicable_providers("README.txt", extension_map)
    ) == ("panasonicP2", "xdcam")
    # ...and every extension-guarded provider is genuinely filtered out.
    assert by_name["hdslr"] not in applicable_providers("Z.wav", extension_map)


def test_card_providers_declare_themselves_non_extension_guarded():
    # Their runtime guard is sidecar presence, which says nothing about
    # the extension: narrowing them by suffix would flip a card clip's
    # umid to a file hash. Pinned so a future edit cannot quietly widen
    # the pre-filter's authority over them.
    registry = build_provider_registry()
    by_name = {provider.machine_name: provider for provider in registry}

    for name in ("xdcam", "panasonicP2"):
        assert by_name[name].is_extension_guarded() is False
        assert by_name[name] in build_extension_map(registry).always
    for name in ("red", "file", "hdslr", "zoom", "avchd", "atomos"):
        assert by_name[name].is_extension_guarded() is True

    ikegami = build_provider_registry(["ikegami"])[0]
    assert ikegami.is_extension_guarded() is False


def test_provider_declaring_no_extensions_is_always_applicable():
    # The base Provider returns [] from getExtensions(). An empty
    # declaration means unknown reach, and unknown reach must never be
    # silently filtered out — pre-registry-v2 such a provider still ran.
    silent = StubProvider("silent", [])
    guarded = StubProvider("guarded", [".mxf"])
    extension_map = build_extension_map((silent, guarded))

    assert extension_map.always == (silent,)
    assert applicable_providers("ANY.zzz", extension_map) == (silent,)
    assert applicable_providers("A.mxf", extension_map) == (silent, guarded)


def test_always_applicable_only_map_is_truthy():
    # Item 2's guard is `if not extension_map`: a map whose only content
    # is the always-applicable set must NOT read as empty.
    always_only = build_extension_map((StubProvider("silent", []),))
    assert len(always_only) == 0
    assert bool(always_only) is True

    empty = build_extension_map(())
    assert bool(empty) is False
    assert applicable_providers("A.mxf", empty) == ()


def test_unusable_extension_declarations_are_skipped():
    # A bare string would otherwise be iterated character by character
    # and claim every file ending in one of its letters.
    class BareString(StubProvider):
        def getExtensions(self):
            return ".mxf"

    bare = BareString("bare", [])
    junk = StubProvider("junk", [".mxf", "", None, 3, ".MP4"])
    extension_map = build_extension_map((bare, junk))

    assert set(extension_map) == {".mxf", ".mp4"}
    assert extension_map[".mxf"] == (junk,)
    # No declaration survived for `bare`, so it is always-applicable
    # rather than matching "f", "m", "x"...
    assert extension_map.always == (bare,)
    assert applicable_providers("README.txt", extension_map) == (bare,)


# --------------------------------------------------------------------------
# extract_metadatas
# --------------------------------------------------------------------------


def test_merge_runs_every_provider_in_order_and_later_keys_win(caplog):
    first = StubProvider("first", [".fake"], {"provider": "first", "umid": "U1"})
    second = StubProvider("second", [".fake"], {"umid": "U2", "extra": "second"})
    media_file = _vsfile("2026/AH_x/CLIP.fake")
    context = {}

    metadatas = extract_metadatas(media_file, (first, second), {}, context)

    # Never breaks on first match: BOTH ran...
    assert first.calls == [media_file]
    assert second.calls == [media_file]
    # ...both FRESH-dict contributions are present, later keys winning.
    assert metadatas == {"provider": "first", "umid": "U2", "extra": "second"}
    # ...but reassigning an identity key is never silent.
    assert any(
        record.levelname == "ERROR"
        and "overwrote 'umid'" in record.message
        and "second" in record.message
        and "first" in record.message
        for record in caplog.records
    ), caplog.text


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
    # `context` stays an argument and stays mutable IN PLACE.
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


def test_non_mapping_return_names_the_offending_provider():
    # The pre-registry-v2 contract returned (metadatas, context). A
    # provider still on it must fail loudly and by name, not with an
    # opaque ValueError from dict.update() unpacking two keys.
    class LegacyTupleProvider:
        machine_name = "legacytuple"

        def getExtensions(self):
            return [".fake"]

        def getMetadatasFromFile(self, media_file, metadatas, context):
            return metadatas, context

    with pytest.raises(TypeError, match="legacytuple"):
        extract_metadatas(
            _vsfile("2026/AH_x/CLIP.fake"), (LegacyTupleProvider(),), {}, {}
        )


def test_in_place_overwrite_of_an_identity_key_is_also_logged(caplog):
    setter = StubProvider("setter", [".fake"], {"provider": "setter", "umid": "U1"})

    class InPlaceThief:
        machine_name = "thief"

        def getExtensions(self):
            return [".fake"]

        def getMetadatasFromFile(self, media_file, metadatas, context):
            metadatas["umid"] = "STOLEN"
            return metadatas

    metadatas = extract_metadatas(
        _vsfile("2026/AH_x/CLIP.fake"), (setter, InPlaceThief()), {}, {}
    )

    assert metadatas["umid"] == "STOLEN"
    assert any(
        record.levelname == "ERROR" and "thief" in record.message
        for record in caplog.records
    ), caplog.text


def test_reasserting_the_same_identity_value_is_not_logged(caplog):
    # Two providers agreeing on the umid is not a conflict.
    first = StubProvider("first", [".fake"], {"provider": "first", "umid": "SAME"})
    second = StubProvider("second", [".fake"], {"umid": "SAME", "extra": 1})

    extract_metadatas(_vsfile("2026/AH_x/CLIP.fake"), (first, second), {}, {})

    assert [r for r in caplog.records if r.levelname == "ERROR"] == []


# --------------------------------------------------------------------------
# Registry build: the healthy path, the one sanctioned degrade, the re-raise
# --------------------------------------------------------------------------


def test_healthy_context_carries_a_registry_and_a_prefilter(storage_fake):
    storage_fake.set_root("VX-REG-OK", "/mnt/regok")

    ctx = build_context(
        ["VX-REG-OK"],
        user=None,
        dry_run=True,
        providers=["red", "file"],
        legacy_storages=[],
        replace=False,
    )

    assert ctx.provider_registry is not None
    assert [p.machine_name for p in ctx.provider_registry] == ["red", "file"]
    assert ctx.extension_map is not None
    assert bool(ctx.extension_map) is True
    # The registry holds the shared cached instances, not fresh ones.
    assert ctx.provider_registry == build_provider_registry(["red", "file"])


def test_registry_dedupes_repeated_names():
    # `--providers red red` must not run red twice: the second pass would
    # merge red's own contribution over itself.
    assert [
        p.machine_name for p in build_provider_registry(["red", "red", "file"])
    ] == [
        "red",
        "file",
    ]


def test_unknown_provider_name_degrades_and_is_logged(storage_fake, caplog):
    storage_fake.set_root("VX-REG-BAD", "/mnt/regbad")

    ctx = build_context(
        ["VX-REG-BAD"],
        user=None,
        dry_run=True,
        providers=["definitely_not_a_provider"],
        legacy_storages=[],
        replace=False,
    )

    # Degraded, not raised: the scan re-raises at its legacy resolution.
    assert ctx.provider_registry is None
    assert ctx.extension_map is None
    assert any(
        record.levelname == "WARNING" and "definitely_not_a_provider" in record.message
        for record in caplog.records
    ), caplog.text


def test_unknown_provider_name_still_raises_importerror_from_scan(storage_fake):
    from portal.plugins.TapelessIngest.models.folder import Folder

    storage_fake.set_root("VX-REG-BAD2", "/mnt/regbad2")
    ctx = build_context(
        ["VX-REG-BAD2"],
        user=None,
        dry_run=True,
        providers=["definitely_not_a_provider"],
        legacy_storages=[],
        replace=False,
    )
    folder = Folder(storage_id="VX-REG-BAD2", path="2026/AH_x")

    with pytest.raises(ImportError):
        folder.scan(number=0, count_only=True, context=ctx)


def test_a_broken_getextensions_propagates_instead_of_degrading(storage_fake):
    """A map that cannot be built must NOT fall back to no pre-filter.

    The fallback resolves providers fine, so the run would silently
    proceed with the pre-filter disabled and select providers differently
    from a healthy run. That is the failure mode this re-raise prevents.
    """
    from portal.plugins.TapelessIngest.models.clip import Clip

    class BrokenProvider:
        machine_name = "brokenext"

        def getExtensions(self):
            raise RuntimeError("declaration is broken")

    storage_fake.set_root("VX-REG-BOOM", "/mnt/regboom")
    Clip._PROVIDER_CACHE["brokenext"] = BrokenProvider()
    try:
        with pytest.raises(RuntimeError, match="declaration is broken"):
            build_context(
                ["VX-REG-BOOM"],
                user=None,
                dry_run=True,
                providers=["brokenext"],
                legacy_storages=[],
                replace=False,
            )
    finally:
        Clip._PROVIDER_CACHE.pop("brokenext", None)


# ---------------------------------------------------------------------------
# Story 2.6 — consumption (FR-19 / NFR-1)
# ---------------------------------------------------------------------------

FOLDER = "2026/AH_20260101_shoot"


class StubClip:
    """Duck-typed clip: a recorded dir, optionally a scan-attached file."""

    def __init__(self, path, file_path=None):
        self.path = path
        if file_path is not None:
            self.file = StubFile(file_path)


class StubFile:
    def __init__(self, path):
        self._path = path

    def getPath(self):
        return self._path


class StubSubpathProvider:
    def __init__(self, machine_name, subpaths):
        self.machine_name = machine_name
        self._subpaths = list(subpaths)

    def getSubPaths(self):
        return self._subpaths


def test_consumed_by_file_path_takes_the_first_component():
    clips = [
        StubClip(f"{FOLDER}/PRIVATE/M4ROOT/Clip"),
        StubClip(f"{FOLDER}/PRIVATE/M4ROOT/Clip"),
    ]

    consumed = consumed_subdirs(clips, [], FOLDER)

    # The immediate child, not the deep dir: the recursion only ever
    # decides about children.
    assert consumed == frozenset({"PRIVATE"})


def test_consumed_unions_a_stale_recorded_dir_with_this_scans_file_dir():
    # A DB-resident clip's `path` is a CREATE-time default and may name a
    # different dir from the file this scan matched. Both are consumed —
    # over-consumption, safe under NFR-1's tie-break.
    clip = StubClip(f"{FOLDER}/OLDCARD/CLIP", file_path=f"{FOLDER}/NEWCARD/CLIP/A.MP4")

    assert consumed_subdirs([clip], [], FOLDER) == frozenset({"OLDCARD", "NEWCARD"})


def test_file_directly_in_the_folder_contributes_nothing():
    # relpath == os.curdir: there is no child to skip.
    clip = StubClip(FOLDER, file_path=f"{FOLDER}/A.MP4")

    assert consumed_subdirs([clip], [], FOLDER) == frozenset()


def test_a_clip_outside_the_folder_contributes_nothing():
    # os.pardir and a '../'-prefixed relpath both mean "not below us".
    outside = StubClip("2026", file_path="2026/OTHER/A.MP4")

    assert consumed_subdirs([outside], [], FOLDER) == frozenset()


def test_dot_named_children_are_consumed_like_any_other():
    # The predicate is an EXACT os.curdir/os.pardir test, never a
    # startswith('.') or startswith(os.pardir), so '.cache' and '..hidden'
    # are ordinary children.
    clips = [
        StubClip(f"{FOLDER}/.cache/CLIP"),
        StubClip(f"{FOLDER}/..hidden/CLIP"),
    ]

    assert consumed_subdirs(clips, [], FOLDER) == frozenset({".cache", "..hidden"})


def test_consumed_subdirs_refuses_an_absolute_folder_path():
    """The storage-root-relative contract, pinned.

    `folder_path` is `Folder.path` — the same coordinate system as
    `Clip.path` and `VSFile.getPath()`. Handing it `folder.absolute_path`
    instead would make every relpath start with '..', empty the consumed
    set and send the recursion into every child of every folder:
    duplicates at scale, silently. It is refused outright.
    """
    clip = StubClip(f"{FOLDER}/PRIVATE/M4ROOT/Clip")

    with pytest.raises(ValueError, match="storage-root-relative"):
        consumed_subdirs([clip], [], f"/mnt/PAD_Storage/{FOLDER}")


def test_no_clips_and_no_providers_is_an_empty_set_never_doubt():
    # frozenset() means "nothing consumed, descend into everything" — the
    # OPPOSITE of None. A folder that found nothing must be descended into.
    consumed = consumed_subdirs([], [], FOLDER)

    assert consumed is not None
    assert consumed == frozenset()


def test_provider_subpath_consumes_an_ancestor_segment_holding_no_clip():
    # panasonicP2's row: CONTENTS/ holds no clip file itself, but it is the
    # ancestor of CONTENTS/VIDEO and CONTENTS/AVCLIP, so scanning it as a
    # folder of its own would re-find (and re-ingest) the same files.
    provider = StubSubpathProvider("panasonicP2", ["CONTENTS/VIDEO", "CONTENTS/AVCLIP"])

    consumed = consumed_subdirs([], [provider], FOLDER)

    assert "CONTENTS" in consumed
    # Layer (b) contributes no NAMES: it cannot know which children exist.
    # Membership is deliberately broader than equality (see ConsumedSubdirs).
    assert consumed == frozenset()
    assert isinstance(consumed, ConsumedSubdirs)


def test_deeper_only_segments_are_not_consumed_on_their_own():
    # VIDEO / STREAM only ever appear BELOW a mandatory first segment, so
    # they are not immediate children of the scanned folder and must not be
    # matched by the derived prefixes. Their PARENT is what gets consumed.
    providers = [
        StubSubpathProvider("panasonicP2", ["CONTENTS/VIDEO"]),
        StubSubpathProvider("avchd", ["((PRIVATE/)?AVCHD/)?BDMV/STREAM"]),
    ]

    consumed = consumed_subdirs([], providers, FOLDER)

    assert "VIDEO" not in consumed
    assert "STREAM" not in consumed
    assert "CONTENTS" in consumed
    assert "BDMV" in consumed


SHIPPED_SUBPATH_PROVIDERS = list(PROVIDER_NAMES) + ["ikegami"]

# Every name a shipped sub-path can put DIRECTLY under a scanned folder.
CONSUMED_CHILD_NAMES = [
    "CONTENTS",
    "DCIM",
    "FOLDER01",
    "PRIVATE",
    "M4ROOT",
    "XDROOT",
    "BPAV",
    "CLPR",
    "128_0001L_01",
    "AVCHD",
    "BDMV",
    "Clip",
    "CLIP",
    "A001_S001_S001_T001",
    "BIN001",
]

# Names that only ever appear one level DOWN, reachable only through a
# parent that is itself consumed — so they are never immediate children.
DEEPER_ONLY_NAMES = ["VIDEO", "STREAM", "AVCLIP", "001GOPRO", "ZOOM0001"]


def _all_shipped_providers():
    # ikegami ships a getSubPaths() but is NOT in PROVIDER_NAMES, so it can
    # never reach consumed_subdirs through the canonical registry; it is
    # exercised here for derivation coverage only.
    return [Clip.get_provider_by_name(name) for name in SHIPPED_SUBPATH_PROVIDERS]


@pytest.mark.parametrize("name", CONSUMED_CHILD_NAMES)
def test_shipped_subpaths_consume_every_first_level_child_name(name):
    consumed = consumed_subdirs([], _all_shipped_providers(), FOLDER)

    assert name in consumed


@pytest.mark.parametrize("name", DEEPER_ONLY_NAMES)
def test_shipped_subpaths_do_not_consume_deeper_only_names(name):
    consumed = consumed_subdirs([], _all_shipped_providers(), FOLDER)

    assert name not in consumed


@pytest.mark.parametrize("name", DEEPER_ONLY_NAMES)
def test_deeper_only_names_are_still_reachable_through_their_parent(name):
    # Their segment IS load-bearing — it just lives inside a full sub-path,
    # not at the top of one. The full path matches an original pattern, and
    # the parent that leads to it is what consumed_subdirs skips.
    full_paths = {
        "VIDEO": "CONTENTS/VIDEO",
        "AVCLIP": "CONTENTS/AVCLIP",
        "STREAM": "PRIVATE/AVCHD/BDMV/STREAM",
        "001GOPRO": "DCIM/001GOPRO",
        "ZOOM0001": "FOLDER01/ZOOM0001",
    }
    patterns = []
    for provider in _all_shipped_providers():
        for subpath in provider.getSubPaths():
            patterns += list(subpath_prefix_patterns(subpath))

    assert any(re.fullmatch(pattern, full_paths[name]) for pattern in patterns)
    assert full_paths[name].split("/")[0] in consumed_subdirs(
        [], _all_shipped_providers(), FOLDER
    )


def test_prefix_derivation_keeps_the_original_and_ignores_class_slashes():
    # atomos is the only shipped '/'-inside-a-character-class case: it has
    # no separator at all, so it derives nothing beyond itself.
    assert subpath_prefix_patterns("[^/]*_S[0-9]{3}_S[0-9]{3}_T[0-9]{3}") == (
        "[^/]*_S[0-9]{3}_S[0-9]{3}_T[0-9]{3}",
    )
    # An escaped separator is not a separator either.
    assert subpath_prefix_patterns(r"a\/b") == (r"a\/b",)
    # Alternation spanning a separator: every cut closes its open groups.
    assert subpath_prefix_patterns("((PRIVATE/)?(M4ROOT/|XDROOT/))?(Clip|CLIP)") == (
        "((PRIVATE/)?(M4ROOT/|XDROOT/))?(Clip|CLIP)",
        "((PRIVATE))",
        "((PRIVATE/)?(M4ROOT))",
        "((PRIVATE/)?(M4ROOT/|XDROOT))",
    )


def test_every_shipped_derived_pattern_compiles():
    for provider in _all_shipped_providers():
        for subpath in provider.getSubPaths():
            for candidate in subpath_prefix_patterns(subpath):
                re.compile(candidate)


@pytest.mark.parametrize("broken", ["A)B/C", "[A/B"])
def test_an_uncompilable_original_subpath_is_doubt(broken):
    # The ORIGINAL pattern's compile is wrapped too, not only the derived
    # ones — otherwise these two never reach the doubt path at all.
    reasons = []
    provider = StubSubpathProvider("broken", [broken])
    clip = StubClip(f"{FOLDER}/PRIVATE/M4ROOT/Clip")

    consumed = consumed_subdirs([clip], [provider], FOLDER, reasons=reasons)

    # NFR-1: None, never frozenset() and never the partial layer-(a) set —
    # a partial set would leave the provider's subtree unconsumed and the
    # recursion would re-ingest it.
    assert consumed is None
    assert consumed is not frozenset()
    assert reasons and broken in reasons[0]


def test_an_uncompilable_derived_subpath_is_doubt():
    # Synthetic: a comment group swallowing an unbalanced '(' compiles as
    # written but not once truncated and re-closed. No shipped provider
    # does this (test_every_shipped_derived_pattern_compiles), but the
    # doubt path must not depend on that staying true.
    reasons = []
    provider = StubSubpathProvider("broken", ["x(?#a(b)/y"])

    assert re.compile("x(?#a(b)/y")

    consumed = consumed_subdirs([], [provider], FOLDER, reasons=reasons)

    assert consumed is None
    assert reasons


def test_doubt_needs_no_reasons_list():
    # `reasons` is optional: a caller that does not want the text still
    # gets the decision.
    provider = StubSubpathProvider("broken", ["A)B/C"])

    assert consumed_subdirs([], [provider], FOLDER) is None

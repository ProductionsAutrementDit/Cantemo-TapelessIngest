"""Tier 1 (story 4.2): the equivalence harness's own relation, on fakes.

Everything here runs with no DB, no index and no Portal call: the harness
takes ``process_folder`` and ``query_elastic`` as parameters precisely so
its relation — tuple extraction, set comparison, classification, waiver
matching, the verdict — can be driven directly.

This is an INSTRUMENT, so the tests are weighted accordingly. The largest
block is not the happy path but **the false-pass block**: every way a run
can come back looking like agreement while having proven nothing. Each of
those was a live defect (a corpus naming a vanished folder printed
``[agreed] … 0 tuple(s)`` and exited 0), and each is pinned from the
outside — through the verdict — rather than by asserting on the guard
that fixes it.

The rest:

* the corpus tests exist because a gate that ran over an empty, nested or
  half-parsed corpus would report ``accepted`` having proven nothing;
* the waiver tests exist because a waiver that cites no FR is
  indistinguishable from somebody silencing a defect (AD-2), because a
  STALE waiver hides the next one, and because a glob that crossed ``/``
  would silence a subtree somebody signed off one file of;
* the comparison tests pin SETS, and pin that ordering is outside the
  gate — legacy sorts per page, index sorts globally (AD-7), and a
  sequence comparison would fail for a reason the spine sanctions;
* the reproducibility tests are what make the verdict evidence rather
  than an anecdote.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from portal.plugins.TapelessIngest.scan import equivalence
from portal.plugins.TapelessIngest.scan.context import (
    DISCOVERY_INDEX,
    DISCOVERY_LEGACY,
    RunOptions,
    ScanContext,
    StorageInfo,
)
from portal.plugins.TapelessIngest.scan.coordinator import FolderOutcome, WorkerResult

STORAGE = "VX-41"
FOLDER = "2026/AH_20260101_one"
REPO_ROOT = Path(__file__).resolve().parents[2]

# cwd=repo root, no stub, no conftest: `python -c` puts the cwd on sys.path,
# so `scan.equivalence` resolves to this repo's package in a bare
# interpreter. The same script `test_index_discovery`, `test_scan_verification`
# and `test_scan_extraction` all ship for the same AD-1 claim.
PORTAL_FREEDOM_SCRIPT = (
    "import sys, scan.equivalence; "
    "assert not [m for m in sys.modules if m == 'portal' or m.startswith('portal.')]; "
    "assert 'django' not in sys.modules"
)


def test_scan_equivalence_imports_portal_free_in_subprocess():
    """AD-1, and it is load-bearing HERE beyond the usual reason.

    The harness takes `process_folder` and `query_elastic` as parameters
    so the relation can be unit-tested off-server. A `portal.*` import
    creeping in would not break those tests — it would quietly make the
    module unimportable in a bare interpreter and un-unit-testable in
    the tier the spec puts it in.
    """
    result = subprocess.run(
        [sys.executable, "-c", PORTAL_FREEDOM_SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"scan.equivalence is not Portal-free in a bare interpreter (AD-1):\n"
        f"{result.stderr.decode(errors='replace')}"
    )


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeFile:
    def __init__(self, path):
        self._path = path

    def getPath(self):
        return self._path


class FakeClip:
    """The three attributes the AD-2 tuple reads off a post-extraction clip."""

    def __init__(self, path, umid, provider_name, storage_id=STORAGE):
        self.file = FakeFile(path)
        self.umid = umid
        self.provider_name = provider_name
        self.storage_id = storage_id


def _tuple(path, umid="u", provider="red", folder=FOLDER, storage=STORAGE):
    return (storage, path, umid, provider, folder)


def _entry(path=FOLDER, storage=STORAGE, note=""):
    return equivalence.CorpusEntry(storage_id=storage, path=path, note=note)


def _path_run(mode, tuples, *, errors=(), failed=(), folders=None, elapsed=0.0):
    tuples = frozenset(tuples)
    if folders is None:
        folders = frozenset(values[4] for values in tuples) or frozenset({FOLDER})
    folders = frozenset(folders)
    return equivalence.PathRun(
        mode=mode,
        tuples=tuples,
        folder_paths=folders,
        tuple_count=len(tuples),
        folder_count=len(folders),
        failed_folders=tuple(failed),
        errors=tuple(errors),
        elapsed=elapsed,
    )


def _scripted_runner(script, **run_options):
    """``run_path`` over a ``{path: [run1, run2, run3]}`` script.

    The three lists are consumed IN CALL ORDER — legacy, legacy, index —
    so a test can make the reference disagree with itself simply by
    listing two different sets. A value may be a ``PathRun`` (to script
    errors or failed folders) or a bare iterable of tuples.
    """
    calls = {}

    def run_path(entry, mode):
        index = calls.get(entry.path, 0)
        calls[entry.path] = index + 1
        scripted = script[entry.path][index]
        if isinstance(scripted, equivalence.PathRun):
            return equivalence.dataclass_replace(scripted, mode=mode)
        return _path_run(mode, scripted, **run_options)

    return run_path


def _counting_clock():
    """A monotonic clock with no wall time in it, so a verdict is stable."""
    ticks = [0.0]

    def clock():
        ticks[0] += 1.0
        return ticks[0]

    return clock


def _agreeing(tuples):
    return [tuples, tuples, tuples]


# ---------------------------------------------------------------------------
# THE FALSE-PASS BLOCK: every door to "accepted" over no evidence
# ---------------------------------------------------------------------------


def test_two_empty_tuple_sets_are_not_agreement():
    """The defect this whole block exists for.

    A corpus naming a folder that has been archived away used to produce
    `verdict: accepted`, exit 0, `agreed 1` and a console line reading
    `reference 0 tuple(s), index 0, 0 divergence(s)`. Two empty sets
    compare equal; equality is not evidence.
    """
    corpus = equivalence.load_corpus("VX-41 | 2099/gone | archived away\n")

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2099/gone": _agreeing([])}),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_ERRORED
    assert verdict.entries[0].status == equivalence.ENTRY_ERRORED
    assert "no positive evidence" in verdict.entries[0].error


def test_a_failed_folder_anywhere_in_the_walk_is_not_agreement():
    """A folder that died contributed no tuples.

    So the two paths can agree perfectly about a subtree NEITHER of them
    saw. `failed_folders` reached the JSON and was consulted by nothing.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    good = _path_run(DISCOVERY_LEGACY, [_tuple("a")])
    broken = _path_run(DISCOVERY_LEGACY, [_tuple("a")], failed=("2026/AA_one/card",))

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [good, good, broken]}),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_ERRORED
    assert verdict.entries[0].status == equivalence.ENTRY_ERRORED
    assert "2026/AA_one/card" in verdict.entries[0].error


def test_a_corpus_with_one_broken_entry_is_never_accepted():
    """Even when every other entry agrees, the run is not clean."""
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_one | fine\nVX-41 | 2099/gone | archived\n"
    )
    tuples = [_tuple("a")]

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {"2026/AA_one": _agreeing(tuples), "2099/gone": _agreeing([])}
        ),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_ERRORED
    assert [e.status for e in verdict.entries] == [
        equivalence.ENTRY_AGREED,
        equivalence.ENTRY_ERRORED,
    ]


def test_a_context_with_no_provider_registry_is_refused_before_walking():
    """`_build_registry_and_map` returns (None, None) for a bad provider name.

    `_scan_pass` then rebuilds a registry PER FOLDER — which silently
    destroys the one-registry guarantee AD-2's `provider_name` comparison
    rests on. A scan may degrade that way; a gate may not.
    """
    base = _base_context(provider_registry=None)

    with pytest.raises(equivalence.EquivalenceError, match="no provider registry"):
        equivalence.context_factory(base)


def test_a_storage_that_did_not_resolve_is_refused_before_walking():
    """`resolve_storages` degrades a bad id to `root_path=None` on purpose.

    For a production scan that is mercy — it reports per-folder "Cannot
    get full path" instead of crashing. For a GATE it is a guaranteed
    pair of empty tuple sets reported as agreement.
    """
    base = _base_context(root_path=None)

    with pytest.raises(equivalence.EquivalenceError, match="did not resolve"):
        equivalence.context_factory(base)


def test_a_scan_root_that_is_not_a_directory_is_refused(tmp_path):
    run = equivalence.build_path_runner(
        context_for=equivalence.context_factory(_base_context(root_path=str(tmp_path))),
        process_folder=lambda *a, **k: None,
        query_elastic=None,
    )

    with pytest.raises(equivalence.EquivalenceError, match="does not resolve"):
        run(_entry(path="not/there"), DISCOVERY_LEGACY)


def test_a_walk_that_never_reaches_the_collector_raises(tmp_path, monkeypatch):
    """If `walk_tree` ever stopped routing through the injected worker,
    every entry would come back with two empty sets that compare equal."""
    (tmp_path / "2026").mkdir()
    from portal.plugins.TapelessIngest.scan import equivalence as module

    monkeypatch.setattr(module, "walk_tree", lambda *a, **k: [])
    run = module.build_path_runner(
        context_for=module.context_factory(_base_context(root_path=str(tmp_path))),
        process_folder=lambda *a, **k: None,
        query_elastic=None,
    )

    with pytest.raises(module.EquivalenceError, match="never reached the tuple"):
        run(_entry(path="2026"), DISCOVERY_LEGACY)


# ---------------------------------------------------------------------------
# The corpus is an INPUT, and a bad one stops the run
# ---------------------------------------------------------------------------


def test_a_corpus_parses_into_entries_with_their_notes():
    corpus = equivalence.load_corpus(
        "# a comment\n"
        "\n"
        "VX-41 | 2026/AA_one | the manual precedent\n"
        "VX-26 | 2025/AB_two | \n",
        source="corpus.txt",
    )

    assert [(e.storage_id, e.path) for e in corpus.entries] == [
        ("VX-41", "2026/AA_one"),
        ("VX-26", "2025/AB_two"),
    ]
    assert corpus.entries[0].note == "the manual precedent"
    assert corpus.entries[1].note == ""
    assert corpus.storage_ids == ("VX-41", "VX-26")


def test_ratification_is_a_directive_and_defaults_to_no():
    """Absence means UNRATIFIED — the safe default for an instrument."""
    unmarked = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    marked = equivalence.load_corpus(
        "#! ratified: yes\n"
        "#! ratification-note: signed off 2026-09-01\n"
        "VX-41 | 2026/AA_one | n\n"
    )

    assert unmarked.ratified is False
    assert marked.ratified is True
    assert marked.ratification_note == "signed off 2026-09-01"
    assert marked.as_dict()["ratified"] is True


def test_the_corpus_digest_covers_the_scope_and_not_the_notes():
    """Two verdicts are comparable when they covered the same folders."""
    scope = "VX-41 | 2026/AA_one | note one\n"
    renamed = "VX-41 | 2026/AA_one | a completely different note\n"
    widened = "VX-41 | 2026/AA_one | note one\nVX-41 | 2026/AA_two | \n"

    assert (
        equivalence.load_corpus(scope).digest == equivalence.load_corpus(renamed).digest
    )
    assert (
        equivalence.load_corpus(scope).digest != equivalence.load_corpus(widened).digest
    )


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("", "holds no entries"),
        ("# only comments\n\n", "holds no entries"),
        ("VX-41 | 2026/AA_one\n", "expected exactly 3 columns"),
        ("VX 41 | 2026/AA_one | n\n", "is not a storage id"),
        ("VX-41 |  | n\n", "the path column is empty"),
        ("VX-41 | /mnt/PAD_Storage/2026 | n\n", "is absolute"),
        ("VX-41 | 2026/../../etc | n\n", "escapes the storage root"),
        # F3: gating a whole storage by accident is not a corpus.
        ("VX-41 | / | n\n", "is absolute"),
        ("VX-41 | // | n\n", "is absolute"),
        (
            "VX-41 | 2026/AA_one | n\nVX-41 | 2026/AA_one | again\n",
            "already in the corpus at line 1",
        ),
        (
            "VX-41 | 2026/AA_one | n\nVX-41 | 2026/AA_one/card | nested\n",
            "overlaps 2026/AA_one from line 1",
        ),
        (
            "VX-41 | 2026/AA_one/card | n\nVX-41 | 2026/AA_one | parent\n",
            "overlaps 2026/AA_one/card from line 1",
        ),
        ("#! ratified: maybe\nVX-41 | 2026/a | n\n", "must be 'yes' or 'no'"),
        ("#! nonsense\nVX-41 | 2026/a | n\n", "introduces a directive"),
        ("#! unknown: 1\nVX-41 | 2026/a | n\n", "unknown directive"),
    ],
)
def test_a_bad_corpus_is_refused_by_name(text, fragment):
    with pytest.raises(equivalence.CorpusError) as excinfo:
        equivalence.load_corpus(text, source="corpus.txt")

    assert fragment in str(excinfo.value)


def test_a_path_holding_the_separator_is_refused_rather_than_mis_split():
    """D3: the format has no escaping, and it must SAY so rather than guess.

    With `maxsplit`, `2026/odd | name` re-parsed into path `2026/odd` plus
    a note — and the gate then ran, cleanly and confidently, over the
    wrong subtree. Requiring an exact column count costs authors a pipe
    in their notes and buys an unambiguous scope.
    """
    with pytest.raises(equivalence.CorpusError, match="exactly 3 columns"):
        equivalence.load_corpus("VX-41 | 2026/odd | name | note\n")
    with pytest.raises(equivalence.WaiverError, match="exactly 6 columns"):
        equivalence.load_waivers("FR-1 | index_only | * | * | * | note | with a pipe\n")


def test_a_sibling_prefix_is_not_treated_as_nesting():
    """`2026/AA_one` and `2026/AA_one_bis` are different subtrees.

    A string-prefix nesting test would refuse a perfectly good corpus.
    """
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_one | n\nVX-41 | 2026/AA_one_bis | n\n"
    )

    assert len(corpus.entries) == 2


def test_paths_are_normalised_so_two_spellings_are_one_entry():
    with pytest.raises(equivalence.CorpusError, match="already in the corpus"):
        equivalence.load_corpus("VX-41 | 2026/AA_one | n\nVX-41 | 2026/AA_one/ | n\n")


def test_run_equivalence_refuses_an_empty_corpus_even_if_one_is_synthesized():
    with pytest.raises(equivalence.CorpusError):
        equivalence.run_equivalence(
            equivalence.Corpus(entries=(), source="synthetic"),
            lambda entry, mode: _path_run(mode, ()),
        )


def test_the_shipped_corpus_and_waiver_files_parse():
    """They ship beside the harness; a typo in one must not wait for prod."""
    corpus = equivalence.load_corpus_file(equivalence.default_corpus_path())
    waivers = equivalence.load_waivers_file(equivalence.default_waivers_path())

    assert corpus.entries
    # F1: shipped UNRATIFIED, and it says so in machine-readable form.
    assert corpus.ratified is False
    assert corpus.ratification_note
    # The list starts EMPTY and is append-only.
    assert waivers == ()


# ---------------------------------------------------------------------------
# Waivers: read, never written
# ---------------------------------------------------------------------------


def test_a_waiver_line_parses_into_its_globs():
    (waiver,) = equivalence.load_waivers(
        "FR-24 | index_only | 2026/AA_* | **/*.R3D | red | the card fix\n",
        source="waivers.txt",
    )

    assert waiver.fr == "FR-24"
    assert waiver.side == equivalence.SIDE_INDEX_ONLY
    assert waiver.lineno == 1


def test_an_empty_waiver_list_is_valid():
    assert equivalence.load_waivers("# nothing ratified yet\n") == ()


@pytest.mark.parametrize(
    "text,fragment",
    [
        ("| index_only | * | * | * | no citation\n", "is not an FR citation"),
        ("chore-12 | index_only | * | * | * | not an FR\n", "is not an FR citation"),
        ("FR-24 | sideways | * | * | * | bad side\n", "must be one of"),
        ("FR-24 | index_only | * | * | * \n", "expected exactly 6 columns"),
        ("FR-24 | index_only |  | * | * | blank\n", "the folder column is empty"),
        ("#! ratified: yes\n", "takes no directives"),
    ],
)
def test_a_bad_waiver_is_refused_by_name(text, fragment):
    with pytest.raises(equivalence.WaiverError) as excinfo:
        equivalence.load_waivers(text, source="waivers.txt")

    assert fragment in str(excinfo.value)


@pytest.mark.parametrize(
    "pattern,value,expected",
    [
        # D2: `*` must NOT cross a separator, or a waiver ratified for one
        # shoot silently covers every card folder inside it.
        ("2026/AA_*", "2026/AA_one", True),
        ("2026/AA_*", "2026/AA_one/card", False),
        ("2026/AA_*/**", "2026/AA_one/card/deeper", True),
        ("**", "anything/at/all", True),
        ("**/*.R3D", "2026/a/b/c.R3D", True),
        ("**/*.R3D", "2026/a/b/c.fake", False),
        # A bare `*` still means "anything", because that is what the
        # waiver file tells operators to write for a blanket column.
        ("*", "2026/a/b/c", True),
        ("red", "red", True),
        ("red", "xdcam", False),
    ],
)
def test_waiver_globs_match_per_path_segment(pattern, value, expected):
    assert equivalence.glob_match(pattern, value) is expected


def test_the_proposed_waiver_a_divergence_emits_is_not_itself_ratifiable():
    """The harness PROPOSES; a human ratifies. The FR column proves it."""
    (divergence,) = equivalence.compare_tuple_sets(
        [], [_tuple("2026/AH_20260101_one/A.R3D")]
    )

    proposal = divergence.proposed_waiver()

    assert proposal.startswith("FR-?")
    with pytest.raises(equivalence.WaiverError, match="is not an FR citation"):
        equivalence.load_waivers(proposal + "\n")


def test_the_proposed_waiver_escapes_glob_metacharacters_in_real_paths():
    """D3: a path holding `*` would otherwise become a pattern.

    A human ratifying the proposal verbatim would then silence strictly
    more than what was observed.
    """
    (divergence,) = equivalence.compare_tuple_sets(
        [], [_tuple("2026/odd [take]/shot*.R3D", folder="2026/odd [take]")]
    )

    proposal = divergence.proposed_waiver()
    _fr, side, folder, file_glob, provider, _note = proposal.split("|")
    (waiver,) = equivalence.load_waivers(
        f"FR-1 |{side}|{folder}|{file_glob}|{provider}| ratified verbatim\n"
    )

    assert waiver.matches(divergence)
    # ...and it does NOT reach the neighbour the raw `*` would have caught.
    (neighbour,) = equivalence.compare_tuple_sets(
        [], [_tuple("2026/odd [take]/shotOTHER.R3D", folder="2026/odd [take]")]
    )
    assert not waiver.matches(neighbour)


# ---------------------------------------------------------------------------
# Reading the AD-2 tuple out of the post-extraction state
# ---------------------------------------------------------------------------


def test_the_tuple_is_the_five_ad2_fields_in_ad2_order():
    clip = FakeClip("2026/AH_20260101_one/CONTENTS/A.fake", "umid-a", "faketest")

    assert equivalence.ad2_tuple(clip, FOLDER) == (
        STORAGE,
        "2026/AH_20260101_one/CONTENTS/A.fake",
        "umid-a",
        "faketest",
        FOLDER,
    )
    assert equivalence.AD2_FIELDS == (
        "storage_id",
        "verified_file_path",
        "umid",
        "provider_name",
        "owning_folder_path",
    )


def test_the_owning_folder_is_the_walks_folder_not_the_files_directory():
    """The distinction NFR-1 duplicates would show up in."""
    clip = FakeClip("2026/AH_20260101_one/CONTENTS/A.fake", "umid-a", "faketest")

    claimed_here = equivalence.ad2_tuple(clip, FOLDER)
    claimed_below = equivalence.ad2_tuple(clip, f"{FOLDER}/CONTENTS")

    assert claimed_here != claimed_below
    assert len(equivalence.compare_tuple_sets([claimed_here], [claimed_below])) == 2


@pytest.mark.parametrize(
    "attribute,what",
    [
        ("umid", "umid"),
        ("provider_name", "provider_name"),
        ("storage_id", "storage_id"),
    ],
)
def test_a_null_field_raises_instead_of_being_stringified(attribute, what):
    """B1: `str(None)` is `"None"`, and two `"None"`s compare EQUAL.

    That is the instrument certifying agreement between two broken
    extractions — the exact failure class this module exists to catch.
    """
    clip = FakeClip("2026/a/A.fake", "umid-a", "faketest")
    setattr(clip, attribute, None)

    with pytest.raises(equivalence.EquivalenceError, match=f"{what} is None"):
        equivalence.ad2_tuple(clip, FOLDER)


def test_two_clips_with_null_umids_do_not_compare_equal():
    """The same defect, observed through the relation rather than the guard."""
    left = FakeClip("2026/a/A.fake", None, "faketest")
    right = FakeClip("2026/a/B.fake", None, "faketest")

    for clip in (left, right):
        with pytest.raises(equivalence.EquivalenceError):
            equivalence.ad2_tuple(clip, FOLDER)


def test_a_clip_with_no_scanned_file_raises_instead_of_producing_a_sentinel():
    clip = FakeClip("x", "umid-a", "faketest")
    del clip.file

    with pytest.raises(equivalence.EquivalenceError, match="no scanned file"):
        equivalence.ad2_tuple(clip, FOLDER)


def _recording_process_folder(folder_path, clips, seen):
    def process_folder(*args, **kwargs):
        seen.append(kwargs)
        return FolderOutcome(
            result=WorkerResult(folder_path=folder_path, clips=tuple(clips))
        )

    return process_folder


def test_the_collector_reads_the_tuples_and_refuses_the_ingest_shape():
    """AD-2's tuple is post-EXTRACTION; the ingest shape is outside it.

    `walk_tree` always asks for `ingest=True`, and the ingest pass is
    where `Folder.getCollection` — the one Vidispine mutation a read-only
    rehearsal could still commit — becomes reachable.
    """
    seen = []
    clips = [
        FakeClip(f"{FOLDER}/A.fake", "umid-a", "faketest"),
        FakeClip(f"{FOLDER}/B.fake", "umid-b", "faketest"),
    ]
    collector = equivalence.TupleCollector(
        _recording_process_folder(FOLDER, clips, seen)
    )

    collector(STORAGE, FOLDER, None, first=0, number=0, cursor=None, ingest=True)

    assert seen == [{"first": 0, "number": 0, "cursor": None, "ingest": False}]
    assert collector.tuples == {
        (STORAGE, f"{FOLDER}/A.fake", "umid-a", "faketest", FOLDER),
        (STORAGE, f"{FOLDER}/B.fake", "umid-b", "faketest", FOLDER),
    }
    assert collector.folder_paths == {FOLDER}
    assert collector.calls == 1


def test_the_collector_passes_unknown_keywords_through_untouched():
    """A3: it must NOT re-declare `walk_tree`'s signature.

    A renamed or added kwarg would otherwise raise `TypeError` per
    folder — caught by `walk_tree`'s dispatch guard, booked as a failed
    folder, and ending in two empty sets. And a `number=25` default
    would be worse: every folder silently truncated to 25 files, with
    both paths agreeing on the truncation.
    """
    seen = []
    collector = equivalence.TupleCollector(_recording_process_folder(FOLDER, [], seen))

    collector(STORAGE, FOLDER, None, some_future_kwarg="x", ingest=True)

    assert seen == [{"some_future_kwarg": "x", "ingest": False}]
    # No `number` was invented on the caller's behalf.
    assert "number" not in seen[0]


def test_a_folder_whose_tuple_cannot_be_read_contributes_nothing_at_all():
    """B2: all or nothing.

    `ad2_tuple` raises mid-folder; the tuples already extracted must not
    survive as a partial set the comparison could still call agreement.
    """
    clips = [
        FakeClip(f"{FOLDER}/A.fake", "umid-a", "faketest"),
        FakeClip(f"{FOLDER}/B.fake", None, "faketest"),
    ]
    collector = equivalence.TupleCollector(_recording_process_folder(FOLDER, clips, []))

    with pytest.raises(equivalence.EquivalenceError):
        collector(STORAGE, FOLDER, None, ingest=True)

    assert collector.tuples == set()


# ---------------------------------------------------------------------------
# One process, one registry (AD-2's "cannot diverge for registry reasons")
# ---------------------------------------------------------------------------


def _base_context(
    dry_run=True, provider_registry=("registry-sentinel",), root_path="/mnt/x"
):
    return ScanContext(
        storages={STORAGE: StorageInfo(id=STORAGE, root_path=root_path)},
        options=RunOptions(dry_run=dry_run, discovery=DISCOVERY_LEGACY),
        provider_registry=provider_registry,
        extension_map={"map": "sentinel"},
    )


def test_both_paths_get_the_same_registry_and_the_same_storages():
    base = _base_context()
    context_for = equivalence.context_factory(base)

    legacy = context_for(_entry(), DISCOVERY_LEGACY)
    index = context_for(_entry(), DISCOVERY_INDEX)

    assert legacy.provider_registry is index.provider_registry is base.provider_registry
    assert legacy.extension_map is index.extension_map is base.extension_map
    assert legacy.storages[STORAGE] is index.storages[STORAGE]
    assert (legacy.options.discovery, index.options.discovery) == (
        DISCOVERY_LEGACY,
        DISCOVERY_INDEX,
    )
    assert index.discovery_index is None


def test_a_context_that_would_write_is_refused_before_anything_runs():
    with pytest.raises(equivalence.EquivalenceError, match="dry_run"):
        equivalence.context_factory(_base_context(dry_run=False))


# ---------------------------------------------------------------------------
# The relation: SETS, and the classification of what differs
# ---------------------------------------------------------------------------


def test_identical_sets_in_different_orders_agree():
    """AD-7's sanctioned ordering divergence is outside the gate."""
    left = [_tuple("a"), _tuple("b"), _tuple("c")]

    assert equivalence.compare_tuple_sets(left, list(reversed(left))) == ()


def test_a_file_only_one_path_found_is_classified_as_absent():
    (legacy_only,) = equivalence.compare_tuple_sets([_tuple("a")], [])
    (index_only,) = equivalence.compare_tuple_sets([], [_tuple("a")])

    assert (legacy_only.side, legacy_only.classification) == (
        equivalence.SIDE_LEGACY_ONLY,
        equivalence.CLASS_ABSENT_FROM_INDEX,
    )
    assert (index_only.side, index_only.classification) == (
        equivalence.SIDE_INDEX_ONLY,
        equivalence.CLASS_ABSENT_FROM_LEGACY,
    )
    assert legacy_only.counterpart is None


@pytest.mark.parametrize(
    "field,other,expected",
    [
        (
            "provider_name",
            _tuple("a", provider="xdcam"),
            equivalence.CLASS_PROVIDER_NAME_DRIFT,
        ),
        ("umid", _tuple("a", umid="other"), equivalence.CLASS_UMID_DRIFT),
        (
            "owning_folder_path",
            _tuple("a", folder="2026/elsewhere"),
            equivalence.CLASS_OWNING_FOLDER_DRIFT,
        ),
    ],
)
def test_a_file_both_paths_found_but_attributed_differently(field, other, expected):
    divergences = equivalence.compare_tuple_sets([_tuple("a")], [other])

    assert len(divergences) == 2
    for divergence in divergences:
        assert divergence.classification == expected
        assert divergence.differing_fields == (field,)
        assert divergence.counterpart is not None


def test_two_differing_fields_are_not_reported_as_one():
    divergences = equivalence.compare_tuple_sets(
        [_tuple("a")], [_tuple("a", umid="other", provider="xdcam")]
    )

    assert {d.classification for d in divergences} == {
        equivalence.CLASS_MULTI_FIELD_DRIFT
    }
    assert divergences[0].differing_fields == ("umid", "provider_name")


def test_several_candidate_counterparts_are_reported_as_ambiguous():
    divergences = equivalence.compare_tuple_sets(
        [_tuple("a", umid="one")],
        [_tuple("a", umid="two"), _tuple("a", umid="three")],
    )

    assert divergences[0].classification == equivalence.CLASS_AMBIGUOUS_COUNTERPART


def test_the_divergence_sort_key_is_total():
    """C4: a key that ties leaves the order to `PYTHONHASHSEED`.

    Two divergences differing only in a field the key omitted would sort
    by set-iteration order, and two runs of the same data would write
    different bytes.
    """
    left = [_tuple("a", umid="one"), _tuple("a", umid="two")]
    right = [_tuple("a", umid="three"), _tuple("a", umid="four")]
    divergences = equivalence.compare_tuple_sets(left, right)

    keys = [d.sort_key for d in divergences]
    assert len(set(keys)) == len(keys)
    assert keys == sorted(keys)


def test_divergences_come_out_in_a_deterministic_order():
    left = [_tuple("z"), _tuple("a"), _tuple("m")]

    once = equivalence.compare_tuple_sets(left, [])
    again = equivalence.compare_tuple_sets(list(reversed(left)), [])

    assert [d.verified_file_path for d in once] == ["a", "m", "z"]
    assert once == again


def test_two_paths_that_walked_different_folders_are_not_equivalent():
    """B3: identical tuples, different descent — the NFR-1 direction.

    `consumed_subdirs` drift makes one path descend where the other did
    not. If the extra folder happens to hold no clips the tuple sets are
    identical, and a gate that only compared tuples would certify a
    duplicate-ingest risk as agreement.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    tuples = [_tuple("a")]
    reference = _path_run(DISCOVERY_LEGACY, tuples, folders={FOLDER})
    index = _path_run(DISCOVERY_INDEX, tuples, folders={FOLDER, f"{FOLDER}/card"})

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [reference, reference, index]}),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_REJECTED
    entry = verdict.entries[0]
    assert entry.divergences == ()
    (folder_divergence,) = entry.folder_divergences
    assert folder_divergence.side == equivalence.SIDE_INDEX_ONLY
    assert folder_divergence.folder_path == f"{FOLDER}/card"


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def test_an_agreeing_corpus_is_accepted_and_names_every_folder():
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | why it is here\n")
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": _agreeing([_tuple("a"), _tuple("b")])}),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_ACCEPTED
    assert verdict.accepted
    document = verdict.as_dict()
    assert [e["path"] for e in document["entries"]] == ["2026/AA_one"]
    assert document["entries"][0]["status"] == equivalence.ENTRY_AGREED
    assert document["entries"][0]["note"] == "why it is here"
    assert document["entries"][0]["counts"]["reference_tuples"] == 2
    assert document["entries"][0]["counts"]["index_tuples"] == 2
    assert document["relation"] == {
        "definition": "AD-2",
        "tuple_fields": list(equivalence.AD2_FIELDS),
        "comparison": "sets",
    }
    assert document["corpus"]["digest"] == corpus.digest


def test_a_divergence_rejects_the_run_and_is_reported_with_its_folder():
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    reference = [_tuple("a")]
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {"2026/AA_one": [reference, reference, reference + [_tuple("b")]]}
        ),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_REJECTED
    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_DIVERGED
    (divergence,) = entry.divergences
    assert divergence.side == equivalence.SIDE_INDEX_ONLY
    assert divergence.owning_folder_path == FOLDER
    assert divergence.verified_file_path == "b"


def test_an_unstable_reference_withholds_the_verdict_for_that_entry_only():
    """The instrument fault, kept distinct from a gate failure."""
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_one | unstable\nVX-41 | 2026/AA_two | clean\n"
    )
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                # legacy, legacy, index — the two legacy runs disagree.
                "2026/AA_one": [
                    [_tuple("a")],
                    [_tuple("d")],
                    [_tuple("a"), _tuple("c")],
                ],
                "2026/AA_two": _agreeing([_tuple("b")]),
            }
        ),
        clock=_counting_clock(),
    )

    unstable, clean = verdict.entries
    assert unstable.status == equivalence.ENTRY_UNSTABLE_REFERENCE
    assert clean.status == equivalence.ENTRY_AGREED
    # Reported distinctly, and NEVER charged.
    assert unstable.divergences == ()
    assert [d.verified_file_path for d in unstable.withheld_divergences] == ["c"]
    assert {d.side for d in unstable.reference_self_divergences} == {
        equivalence.SIDE_REFERENCE_FIRST_ONLY,
        equivalence.SIDE_REFERENCE_SECOND_ONLY,
    }
    # ...and the index run's evidence is still in the document.
    assert unstable.counts()["index_tuples"] == 2


def test_a_run_whose_remaining_entries_agree_is_still_accepted():
    """E1, and it is the difference between a usable gate and a dead one.

    The frozen block says instability invalidates the verdict for the
    affected FOLDERS, not the corpus. Legacy pages each folder at 100
    hits and production has folders far above that, so instability is the
    expected steady state — escalating it to the whole run would let the
    mechanism meant to stop instability blocking the flip block it
    permanently.
    """
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_one | unstable\nVX-41 | 2026/AA_two | clean\n"
    )
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                "2026/AA_one": [[_tuple("a")], [_tuple("d")], [_tuple("a")]],
                "2026/AA_two": _agreeing([_tuple("b")]),
            }
        ),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_ACCEPTED
    # ...with the withholding STATED, not hidden.
    totals = verdict.totals()
    assert totals[equivalence.ENTRY_AGREED] == 1
    assert totals[equivalence.ENTRY_UNSTABLE_REFERENCE] == 1


def test_a_corpus_where_nothing_concluded_is_withheld_not_accepted():
    """The other end of E1: no entry agreed, so nothing was proven."""
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | unstable\n")
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {"2026/AA_one": [[_tuple("a")], [_tuple("d")], [_tuple("a")]]}
        ),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_UNSTABLE_REFERENCE


def test_a_reference_whose_two_runs_report_different_errors_is_unstable():
    """The instrument moved between the two readings.

    Tuples can survive a shift that error strings do not, and a reference
    that is not reproducible is not a reference.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    tuples = [_tuple("a")]
    first = _path_run(DISCOVERY_LEGACY, tuples, errors=("Not descending into x",))
    second = _path_run(DISCOVERY_LEGACY, tuples)

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [first, second, second]}),
        clock=_counting_clock(),
    )

    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_UNSTABLE_REFERENCE
    assert entry.reference_error_divergences == ("Not descending into x",)


def test_a_failing_entry_is_errored_with_a_status_of_its_own():
    """E2: a corpus typo and a flaky reference must not look alike to CI."""
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_bad | unreadable\nVX-41 | 2026/AA_two | clean\n"
    )
    script = {"2026/AA_two": _agreeing([_tuple("b")])}

    def run_path(entry, mode):
        if entry.path == "2026/AA_bad":
            raise OSError("Permission denied: 2026/AA_bad")
        return _path_run(mode, script[entry.path].pop(0))

    verdict = equivalence.run_equivalence(corpus, run_path, clock=_counting_clock())

    assert verdict.status == equivalence.STATUS_ERRORED
    assert verdict.status != equivalence.STATUS_UNSTABLE_REFERENCE
    bad, clean = verdict.entries
    assert bad.status == equivalence.ENTRY_ERRORED
    assert "Permission denied" in bad.error
    assert clean.status == equivalence.ENTRY_AGREED


def test_a_matching_waiver_suppresses_the_divergence_and_is_marked_used():
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    waivers = equivalence.load_waivers(
        f"FR-24 | index_only | {FOLDER} | b | red | the card fix\n"
    )
    reference = [_tuple("a")]
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {"2026/AA_one": [reference, reference, reference + [_tuple("b")]]}
        ),
        waivers=waivers,
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_ACCEPTED
    entry = verdict.entries[0]
    assert entry.divergences == ()
    ((divergence, waiver),) = entry.suppressed
    assert divergence.verified_file_path == "b"
    assert waiver.fr == "FR-24"
    assert verdict.unmatched_waivers == ()


def test_every_waiver_that_covers_a_divergence_is_marked_used():
    """D1: suppression stops at the first match; USAGE must not.

    An overlapping narrower waiver is doing its job, and reporting it as
    "matched nothing" sends a human to delete it.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    waivers = equivalence.load_waivers(
        f"FR-24 | index_only | {FOLDER} | * | * | the broad one\n"
        f"FR-25 | index_only | {FOLDER} | b | red | the narrow one\n"
    )
    reference = [_tuple("a")]
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {"2026/AA_one": [reference, reference, reference + [_tuple("b")]]}
        ),
        waivers=waivers,
        clock=_counting_clock(),
    )

    assert verdict.unmatched_waivers == ()
    # Attributed once, to the first that matched.
    ((_divergence, waiver),) = verdict.entries[0].suppressed
    assert waiver.fr == "FR-24"


def test_a_waiver_on_the_wrong_side_does_not_suppress():
    waivers = equivalence.load_waivers(
        f"FR-24 | legacy_only | {FOLDER} | b | red | wrong side\n"
    )
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    reference = [_tuple("a")]
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {"2026/AA_one": [reference, reference, reference + [_tuple("b")]]}
        ),
        waivers=waivers,
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_REJECTED
    assert len(verdict.unmatched_waivers) == 1


def test_a_waiver_that_matched_nothing_is_reported_as_unmatched():
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    waivers = equivalence.load_waivers(
        "FR-24 | index_only | 2026/gone | * | * | fixed long ago\n"
    )
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": _agreeing([_tuple("a")])}),
        waivers=waivers,
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_ACCEPTED
    assert [w.fr for w in verdict.unmatched_waivers] == ["FR-24"]
    assert verdict.unmatched_waivers_conclusive is True
    assert verdict.as_dict()["totals"]["unmatched_waivers"] == 1
    assert any(
        "matched nothing this run" in line
        for line in equivalence.render_verdict(verdict)
    )


def test_unmatched_waivers_are_inconclusive_when_an_entry_errored():
    """D4: an entry that never ran could not exercise its waivers."""
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_one | ok\nVX-41 | 2026/AA_bad | broken\n"
    )
    waivers = equivalence.load_waivers(
        "FR-24 | index_only | 2026/AA_bad | * | * | covers the broken entry\n"
    )
    script = {"2026/AA_one": _agreeing([_tuple("a")])}

    def run_path(entry, mode):
        if entry.path == "2026/AA_bad":
            raise OSError("gone")
        return _path_run(mode, script[entry.path].pop(0))

    verdict = equivalence.run_equivalence(
        corpus, run_path, waivers=waivers, clock=_counting_clock()
    )

    assert verdict.unmatched_waivers_conclusive is False
    assert verdict.as_dict()["unmatched_waivers_conclusive"] is False
    assert any("INCONCLUSIVE" in line for line in equivalence.render_verdict(verdict))


def test_a_waiver_is_marked_used_even_when_the_entry_was_withheld():
    """Otherwise an unstable folder makes its own waivers look stale."""
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    waivers = equivalence.load_waivers(
        f"FR-24 | index_only | {FOLDER} | c | red | the card fix\n"
    )
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                "2026/AA_one": [
                    [_tuple("a")],
                    [_tuple("d")],
                    [_tuple("a"), _tuple("c")],
                ]
            }
        ),
        waivers=waivers,
        clock=_counting_clock(),
    )

    assert verdict.entries[0].status == equivalence.ENTRY_UNSTABLE_REFERENCE
    assert verdict.unmatched_waivers == ()
    assert verdict.entries[0].withheld_divergences == ()


# ---------------------------------------------------------------------------
# Evidence hygiene: reproducible, bounded, and it says what it covers
# ---------------------------------------------------------------------------


def test_the_canonical_document_carries_no_wall_clock_and_no_timestamp():
    """C1: two runs over unchanged data must produce identical bytes.

    Timings are opt-in precisely so the default form — the one `--out`
    writes — is diffable.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": _agreeing([_tuple("a")])}),
        now="2026-08-28T10:00:00",
    )

    assert "elapsed_seconds" not in verdict.as_dict()
    assert "started_at" not in verdict.as_dict()
    assert verdict.as_dict(with_timing=True)["started_at"] == "2026-08-28T10:00:00"
    assert "elapsed_seconds" in verdict.as_dict(with_timing=True)


def test_re_running_the_same_corpus_on_unchanged_data_yields_the_same_verdict():
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    reference = [_tuple("a"), _tuple("b")]
    script = {"2026/AA_one": [reference, reference, reference + [_tuple("c")]]}
    versions = {"legacy": {"digest": "sha256:aaaa", "covers": ["x"]}}

    first = equivalence.run_equivalence(
        corpus, _scripted_runner(script), discovery_versions=versions
    )
    second = equivalence.run_equivalence(
        corpus, _scripted_runner(script), discovery_versions=versions
    )

    # Under a REAL clock, and with no `with_timing=False` escape hatch:
    # this is the exact form the command writes to --out.
    assert first.as_dict() == second.as_dict()
    assert first.status == equivalence.STATUS_REJECTED


def test_the_scope_the_run_was_narrowed_to_travels_with_the_verdict():
    """C3: a one-provider run must not look like a full-registry run."""
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": _agreeing([_tuple("a")])}),
        scope={"providers": ["red"], "discovery_page_size": 500},
    )

    assert verdict.as_dict()["scope"] == {
        "providers": ["red"],
        "discovery_page_size": 500,
    }
    assert any(
        "scope[providers]" in line for line in equivalence.render_verdict(verdict)
    )


def test_the_document_and_the_rendering_are_both_capped():
    """E3: a broken discovery path makes EVERY clip a divergence.

    Prod holds 188,082 of them. This repo has `_summarize_names(limit=5)`
    and `_drop_clips` because it has been burned by exactly this.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    many = [_tuple(f"file{i:04d}") for i in range(400)]
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [[], [], many]}),
        clock=_counting_clock(),
    )

    entry = verdict.as_dict()["entries"][0]
    assert entry["counts"]["divergences"] == 400
    assert len(entry["divergences"]) == equivalence.DOCUMENT_REPORT_LIMIT
    rendering = equivalence.render_verdict(verdict)
    assert any("more divergence(s) not shown" in line for line in rendering)
    assert len(rendering) < 200


def test_the_tuple_sets_do_not_survive_the_comparison():
    """E3: an EntryVerdict retained for the whole run must not pin three
    full tuple sets per entry."""
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": _agreeing([_tuple("a"), _tuple("b")])}),
        clock=_counting_clock(),
    )

    entry = verdict.entries[0]
    assert entry.reference.tuples == frozenset()
    assert entry.reference.folder_paths == frozenset()
    # ...and the counts, which are what the document needs, survive.
    assert entry.reference.tuple_count == 2
    assert entry.counts()["reference_tuples"] == 2


def test_progress_is_emitted_per_entry():
    """E4: three walks per entry over 8,000 folders is hours of silence."""
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_one | n\nVX-41 | 2026/AA_two | n\n"
    )
    emitted = []

    equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                "2026/AA_one": _agreeing([_tuple("a")]),
                "2026/AA_two": _agreeing([_tuple("b")]),
            }
        ),
        emit=emitted.append,
        clock=_counting_clock(),
    )

    assert len(emitted) == 2
    assert emitted[0].startswith("[1/2] VX-41 2026/AA_one: agreed")
    assert emitted[1].startswith("[2/2] VX-41 2026/AA_two: agreed")


def test_the_rendering_shows_the_failure_evidence_it_holds():
    """E5: the console is what a human reads.

    The first cut printed a clean `[agreed]` line for an entry whose walk
    had booked a root listing error, because `errors` and
    `failed_folders` reached the JSON and never the console.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    broken = _path_run(
        DISCOVERY_LEGACY,
        [_tuple("a")],
        errors=("Error listing directory 2026/AA_one/x: gone",),
        failed=("2026/AA_one/x",),
    )
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [broken, broken, broken]}),
        clock=_counting_clock(),
    )

    rendering = "\n".join(equivalence.render_verdict(verdict))
    assert "failed folder: 2026/AA_one/x" in rendering
    assert "Error listing directory 2026/AA_one/x: gone" in rendering
    assert "[errored]" in rendering


def test_an_unratified_corpus_is_shouted_about_in_the_rendering():
    """F1: a rehearsal must not be quotable as the gate."""
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": _agreeing([_tuple("a")])}),
        clock=_counting_clock(),
    )

    rendering = equivalence.render_verdict(verdict)
    assert any("NOT RATIFIED" in line for line in rendering)
    assert verdict.as_dict()["corpus"]["ratified"] is False


# ---------------------------------------------------------------------------
# Naming what a verdict is valid FOR
# ---------------------------------------------------------------------------


def test_the_source_version_changes_with_the_source_and_not_otherwise():
    assert equivalence.source_version("a") == equivalence.source_version("a")
    assert equivalence.source_version("a") != equivalence.source_version("b")
    assert equivalence.source_version("a").startswith("sha256:")


def test_each_digest_is_pinned_to_the_sources_it_claims_to_cover():
    """C2: a narrow digest under a broad name is worse than no digest.

    The first cut digested `build_search_doc` alone and called it
    "legacy" — blind to legacy's `from`/`size` page loop, the very code
    whose defect `unstable_reference` exists for. Changing ANY covered
    source must move the digest.
    """
    sources = {"mod.a": "one", "mod.b": "two"}
    groups = {"legacy": ("mod.a", "mod.b")}

    versions = equivalence.module_versions(groups, sources.__getitem__)
    assert versions["legacy"]["covers"] == ["mod.a", "mod.b"]

    for label in sources:
        changed = dict(sources, **{label: sources[label] + " changed"})
        moved = equivalence.module_versions(groups, changed.__getitem__)
        assert moved["legacy"]["digest"] != versions["legacy"]["digest"], label


def test_an_uncovered_source_cannot_move_a_digest():
    """The other direction: the claim must be exactly what is digested."""
    groups = {"index": ("mod.a",)}
    first = equivalence.module_versions(groups, {"mod.a": "one"}.__getitem__)
    second = equivalence.module_versions(
        groups, {"mod.a": "one", "mod.b": "irrelevant"}.__getitem__
    )

    assert first == second


def test_a_source_less_deploy_degrades_to_a_named_unavailable_digest():
    """E7: `inspect.getsource` raises on a .pyc-only install.

    A gate run must report that it cannot name its versions, not die.
    """

    def read_source(label):
        if label == "mod.b":
            raise OSError("source not available")
        return "one"

    versions = equivalence.module_versions(
        {"legacy": ("mod.a", "mod.b"), "gone": ("mod.b",)}, read_source
    )

    assert versions["legacy"]["unavailable"] == ["mod.b: OSError"]
    assert versions["legacy"]["digest"].startswith("sha256:")
    assert versions["gone"]["digest"] == f"{equivalence.SOURCE_UNAVAILABLE}:no-source"

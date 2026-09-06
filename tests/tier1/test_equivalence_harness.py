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
    """A `PathRun` double.

    `folders` defaults to the folders the TUPLES name, which is what a
    real walk would report. The `or frozenset({FOLDER})` fallback only
    applies to an empty tuple set, and it is unrelated to the corpus
    entry under test — a run over `2099/gone` will claim to have walked
    `FOLDER`. That is harmless today because every empty-tuple case is
    booked `errored` before the folder set is read, but pass `folders`
    explicitly rather than relying on it.
    """
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


def test_one_broken_entry_withdraws_itself_without_sinking_the_corpus():
    """RULED 2026-09-04: `errored` escalates the way instability does.

    It used to sink the whole corpus, checked BEFORE "did anything
    conclude" — so nineteen agreements plus one archived-away path exited
    3, while nineteen agreements plus one unstable entry exited 0. Both
    are "part of the corpus proved nothing". The asymmetry also made a
    legitimately media-free subtree a permanently red gate with no way
    out short of editing the corpus.
    """
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

    assert verdict.status == equivalence.STATUS_ACCEPTED
    assert [e.status for e in verdict.entries] == [
        equivalence.ENTRY_AGREED,
        equivalence.ENTRY_ERRORED,
    ]
    # The partial acceptance is STATED, never inferred.
    assert verdict.totals()[equivalence.ENTRY_ERRORED] == 1


def test_a_corpus_where_nothing_concluded_is_errored_not_accepted():
    """The other half of the ruling: symmetric is not permissive.

    An entry withdrawing itself must not become a way for a corpus that
    proved NOTHING to exit 0.
    """
    corpus = equivalence.load_corpus(
        "VX-41 | 2098/gone | archived\nVX-41 | 2099/gone | archived\n"
    )

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2098/gone": _agreeing([]), "2099/gone": _agreeing([])}),
        clock=_counting_clock(),
    )

    assert verdict.status == equivalence.STATUS_ERRORED
    assert [e.status for e in verdict.entries] == [
        equivalence.ENTRY_ERRORED,
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
        ("VX-41 | / | n\n", "is absolute"),
        ("VX-41 | // | n\n", "is absolute"),
        # F3: gating a whole storage by accident is not a corpus. These
        # two reach the storage-root branch; the absolute cases above are
        # refused one guard earlier and never did.
        ("VX-41 | . | n\n", "normalises to the storage root"),
        ("VX-41 | ./. | n\n", "normalises to the storage root"),
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
    # RULED 2026-09-04: pin the INVARIANT, not the emptiness. `waivers ==
    # ()` turned the first legitimately ratified waiver — the very
    # workflow the frozen block describes — into a red suite whose
    # natural fix is deleting the assertion.
    for waiver in waivers:
        assert waiver.fr.startswith("FR-")
        assert waiver.note, f"waiver at line {waiver.lineno} states no reason"
        assert not (
            waiver.side == equivalence.SIDE_ANY
            and waiver.folder == waiver.file == waiver.provider == "*"
        )
    # RULED 2026-09-04: the corpus is the manual precedent's SHOOT, not
    # the whole year. Re-widening it to `VX-41 | 2026` left the suite
    # green, so the ruling and the deferred-work record that reconciles
    # it were held only by the file's current contents.
    (entry,) = corpus.entries
    assert entry.path != "2026", (
        "the corpus was re-widened to the whole year; that is a "
        "maintenance-window run and a separate ratification decision"
    )
    assert entry.path.startswith("2026/AA_")


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

    # Asserting `divergences[0]` relied on "one" < "three" < "two"
    # putting the ambiguous one first; renaming a umid moved the
    # assertion onto a different divergence. Assert the SET, and the
    # asymmetry with it: one left tuple has two candidates and is
    # ambiguous, while each right tuple has exactly one and is umid drift.
    assert {(d.side, d.classification) for d in divergences} == {
        (equivalence.SIDE_LEGACY_ONLY, equivalence.CLASS_AMBIGUOUS_COUNTERPART),
        (equivalence.SIDE_INDEX_ONLY, equivalence.CLASS_UMID_DRIFT),
    }
    assert len(divergences) == 3


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

    # The ENTRY keeps a status of its own — that is what CI acts on when
    # nothing concluded (see
    # `test_a_corpus_where_nothing_concluded_is_errored_not_accepted`).
    # Here a clean entry did conclude, so the run is accepted with the
    # errored count stated (RULED 2026-09-04).
    assert verdict.status == equivalence.STATUS_ACCEPTED
    bad, clean = verdict.entries
    assert bad.status == equivalence.ENTRY_ERRORED
    assert bad.status != equivalence.ENTRY_UNSTABLE_REFERENCE
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

    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_UNSTABLE_REFERENCE
    # The waiver's condition WAS observed, so it is not stale...
    assert verdict.unmatched_waivers == ()
    # ...but it suppresses nothing (RULED 2026-09-04). The run never
    # established this difference; a waiver that deleted it would have
    # the verdict claim a suppression for something it refused to prove,
    # while `as_dict(waivable=False)` refuses even to propose one.
    assert [d.verified_file_path for d in entry.withheld_divergences] == ["c"]
    assert entry.suppressed == ()
    assert verdict.totals()["suppressed"] == 0
    assert verdict.totals()["withheld"] == 1


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
    # DOCUMENT_REPORT_LIMIT divergences plus the marker that says so. The
    # bare slice this replaced truncated the CONTRACT in silence, while
    # `errors` and `failed_folders` beside it stated their omission —
    # a reader could only find out by cross-checking `counts`.
    shown = entry["divergences"]
    assert len(shown) == equivalence.DOCUMENT_REPORT_LIMIT + 1
    assert shown[-1]["omitted"] == 400 - equivalence.DOCUMENT_REPORT_LIMIT
    assert "not shown" in shown[-1]["note"]
    rendering = equivalence.render_verdict(verdict)
    assert any("more divergence(s) not shown" in line for line in rendering)
    # Was `< 200`, a hand-picked ceiling loose enough to miss a 5x
    # regression: raising CONSOLE_REPORT_LIMIT from 10 to 50 still fitted
    # under it. Two lines per shown divergence, plus the header block.
    shown_lines = [line for line in rendering if "/absent_from" in line]
    assert len(shown_lines) == equivalence.CONSOLE_REPORT_LIMIT


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


# ---------------------------------------------------------------------------
# The reference must agree with itself about FOLDERS, not only tuples
# (code review 2026-09-04)
# ---------------------------------------------------------------------------


def test_a_reference_that_walked_different_folders_is_not_charged_to_the_index():
    """The hole the folder self-check closes.

    Legacy's unsorted `from`/`size` paging perturbs `hits`; `hits` decides
    `consumed_subdirs`; `consumed_subdirs` decides the descent. So the
    reference can disagree with ITSELF about which folders it walked
    while its tuples happen to survive intact. That difference was then
    compared against the index path and charged to it as a
    `folder_divergence` — which is unwaivable and rejects the gate. An
    instrument fault, billed to the instrument under test.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    tuples = [_tuple("a")]
    first = _path_run(DISCOVERY_LEGACY, tuples, folders={FOLDER, FOLDER + "/card9"})
    second = _path_run(DISCOVERY_LEGACY, tuples, folders={FOLDER})
    index = _path_run(DISCOVERY_INDEX, tuples, folders={FOLDER})

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [first, second, index]}),
        clock=_counting_clock(),
    )

    entry = verdict.entries[0]
    assert verdict.status == equivalence.STATUS_ACCEPTED
    # The drift is reported as what it is, against the reference.
    assert [d.folder_path for d in entry.reference_folder_self_divergences] == [
        FOLDER + "/card9"
    ]
    # And the resulting index comparison for that folder is WITHHELD, in
    # a field of its own, never in `folder_divergences`.
    assert entry.folder_divergences == ()
    assert [d.folder_path for d in entry.withheld_folder_divergences] == [
        FOLDER + "/card9"
    ]
    assert entry.withheld_folders == (FOLDER + "/card9",)


def test_the_stable_folders_of_an_unstable_entry_still_conclude():
    """RULED 2026-09-04: withhold per FOLDER, as the frozen block says.

    "It invalidates the run's verdict for the affected FOLDERS rather
    than the whole corpus." Returning the whole entry as unstable threw
    away every conclusion about the rest of the subtree — and with a
    single-entry corpus that is the whole gate.
    """
    stable = "2026/AA_one/stable"
    flaky = "2026/AA_one/flaky"
    good = _tuple("a", folder=stable)
    drifting = _tuple("b", folder=flaky)
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    first = _path_run(DISCOVERY_LEGACY, [good, drifting])
    second = _path_run(DISCOVERY_LEGACY, [good], folders={stable, flaky})
    index = _path_run(DISCOVERY_INDEX, [good, drifting])

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [first, second, index]}),
        clock=_counting_clock(),
    )

    entry = verdict.entries[0]
    assert entry.withheld_folders == (flaky,)
    # The flaky folder proved nothing; the stable one did, so the entry
    # concludes rather than collapsing.
    assert entry.status == equivalence.ENTRY_AGREED
    assert verdict.status == equivalence.STATUS_ACCEPTED
    assert verdict.totals()["withheld_folders"] == 1


def test_an_entry_whose_every_folder_is_unstable_still_proves_nothing():
    """Per-folder withholding must not become a way to pass on nothing."""
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    first = _path_run(DISCOVERY_LEGACY, [_tuple("a"), _tuple("b")])
    second = _path_run(DISCOVERY_LEGACY, [_tuple("a")])
    index = _path_run(DISCOVERY_INDEX, [_tuple("a")])

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [first, second, index]}),
        clock=_counting_clock(),
    )

    assert verdict.entries[0].status == equivalence.ENTRY_UNSTABLE_REFERENCE
    assert verdict.status == equivalence.STATUS_UNSTABLE_REFERENCE


def test_an_error_set_difference_withholds_the_whole_entry():
    """An error string carries no folder, so nothing can be attributed."""
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    tuples = [_tuple("a")]
    first = _path_run(DISCOVERY_LEGACY, tuples, errors=("Not descending into x",))
    second = _path_run(DISCOVERY_LEGACY, tuples)
    index = _path_run(DISCOVERY_INDEX, tuples)

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [first, second, index]}),
        clock=_counting_clock(),
    )

    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_UNSTABLE_REFERENCE
    assert entry.reference_error_divergences == ("Not descending into x",)


def test_a_withheld_folder_divergence_is_not_counted_as_a_gate_failure():
    """The totals fed the command's REJECTED message.

    `Verdict.totals()` summed `folder_divergences` over ALL entries, so a
    number describing what the run REFUSED to conclude was quoted as what
    it found.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    tuples = [_tuple("a")]
    first = _path_run(DISCOVERY_LEGACY, tuples, folders={FOLDER, FOLDER + "/x"})
    second = _path_run(DISCOVERY_LEGACY, tuples, folders={FOLDER})
    index = _path_run(DISCOVERY_INDEX, tuples, folders={FOLDER})

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [first, second, index]}),
        clock=_counting_clock(),
    )

    totals = verdict.totals()
    assert totals["folder_divergences"] == 0
    assert totals["withheld_folder_divergences"] == 1
    rendering = "\n".join(equivalence.render_verdict(verdict))
    assert "0 folder-set charged" in rendering
    assert "WITHHELD" in rendering


# ---------------------------------------------------------------------------
# The smaller refusals (code review 2026-09-04)
# ---------------------------------------------------------------------------


def test_a_double_star_does_not_also_match_the_folder_it_hangs_off():
    """The waiver file says `2026/AA_*/**` covers everything BELOW."""
    assert equivalence.glob_match("2026/AA_x/**", "2026/AA_x/card1")
    assert equivalence.glob_match("2026/AA_x/**", "2026/AA_x/card1/deeper")
    assert not equivalence.glob_match("2026/AA_x/**", "2026/AA_x")


def test_a_dot_segment_cannot_smuggle_the_same_subtree_in_twice():
    with pytest.raises(equivalence.CorpusError, match="already in the corpus"):
        equivalence.load_corpus("VX-41 | 2026/AA_one | a\nVX-41 | 2026/./AA_one | b\n")


def test_a_directive_may_not_be_set_twice():
    """Last-wins would let a `ratified: no` be overridden further down."""
    with pytest.raises(equivalence.CorpusError, match="already set at line"):
        equivalence.load_corpus(
            "#! ratified: no\n#! ratified: yes\nVX-41 | 2026/AA_one | n\n"
        )


def test_a_waiver_that_suppresses_everything_is_refused():
    with pytest.raises(equivalence.WaiverError, match="waives EVERY divergence"):
        equivalence.load_waivers("FR-4 | any | * | * | * | blanket\n")


def test_a_numeric_ad2_field_is_refused_rather_than_stringified():
    """`str(0)` and the string `"0"` compare EQUAL."""
    clip = FakeClip("a", umid=0, provider_name="red")

    with pytest.raises(equivalence.EquivalenceError, match="AD-2 field is textual"):
        equivalence.ad2_tuple(clip, FOLDER)


def test_a_folder_whose_tuples_cannot_be_read_stays_out_of_the_folder_set():
    """All-or-nothing covered the tuples and not the folder set.

    A folder that aborted mid-read still appeared in `folder_paths` and
    could therefore take part in the descent comparison.
    """
    good = FakeClip("a", umid="u", provider_name="red")
    bad = FakeClip("b", umid=None, provider_name="red")

    def process_folder(*args, **kwargs):
        return FolderOutcome(result=WorkerResult(folder_path=FOLDER, clips=(good, bad)))

    collector = equivalence.TupleCollector(process_folder)
    with pytest.raises(equivalence.EquivalenceError):
        collector(FOLDER)

    assert collector.tuples == set()
    assert collector.folder_paths == set()


def test_the_capped_helpers_state_what_they_dropped():
    """`_capped`/`_render_capped`'s truncation branches had no test."""
    values = [f"folder{i:04d}" for i in range(equivalence.DOCUMENT_REPORT_LIMIT + 25)]

    capped = equivalence._capped(values)
    assert len(capped) == equivalence.DOCUMENT_REPORT_LIMIT + 1
    assert capped[-1] == "... 25 more not shown"

    lines = []
    equivalence._render_capped(lines, values, "  folder: ")
    assert len(lines) == equivalence.CONSOLE_REPORT_LIMIT + 1
    assert lines[-1].endswith(
        f"... {len(values) - equivalence.CONSOLE_REPORT_LIMIT} more not shown"
    )


def test_an_instrument_fault_carries_no_ratifiable_waiver_line():
    """A reference self-divergence was never established as a difference.

    Printing a ready-to-copy waiver beside one invites a human to
    permanently suppress a difference the run did not prove.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    first = _path_run(DISCOVERY_LEGACY, [_tuple("a"), _tuple("b")])
    second = _path_run(DISCOVERY_LEGACY, [_tuple("a")])
    index = _path_run(DISCOVERY_INDEX, [_tuple("a")])

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner({"2026/AA_one": [first, second, index]}),
        clock=_counting_clock(),
    )

    entry = verdict.as_dict()["entries"][0]
    assert entry["reference_self_divergences"]
    for divergence in entry["reference_self_divergences"]:
        assert divergence["proposed_waiver"] is None


# ---------------------------------------------------------------------------
# Round 2: what the round-1 suite let through (2026-09-04)
#
# Every test below was written against a MUTATION that survived the full
# suite. The bar for a test here is not that it passes — it is that it
# goes red when the thing it names is broken.
# ---------------------------------------------------------------------------


def test_one_diverging_entry_rejects_a_corpus_whose_other_entry_agreed():
    """`verdict_status`'s precedence had no multi-entry test at all.

    Every rejection case used a single-entry corpus, so reordering the
    ladder to test `ENTRY_AGREED -> accepted` BEFORE
    `ENTRY_DIVERGED -> rejected` left the whole suite green. A real
    divergence would then exit 0 the moment the corpus grew a second
    entry — and the shipped corpus header explicitly anticipates that
    growth ("a second shoot with different card shapes").
    """
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_one | agrees\nVX-41 | 2026/AA_two | diverges\n"
    )
    agreeing = _tuple("a", folder="2026/AA_one")
    both = _tuple("b", folder="2026/AA_two")
    extra = _tuple("c", folder="2026/AA_two")

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                "2026/AA_one": _agreeing([agreeing]),
                "2026/AA_two": [[both], [both], [both, extra]],
            }
        ),
        clock=_counting_clock(),
    )

    assert [e.status for e in verdict.entries] == [
        equivalence.ENTRY_AGREED,
        equivalence.ENTRY_DIVERGED,
    ]
    assert verdict.status == equivalence.STATUS_REJECTED


def test_a_divergence_in_a_stable_folder_still_rejects_an_unstable_entry():
    """Ruling #1's rejecting half, which the suite only tested permissively.

    Collapsing the per-folder split — one flaky folder withholding EVERY
    divergence in the entry — left 1283 green. That is the ruling
    inverted into a false PASS: instability is the steady state (legacy
    pages at 100 hits, prod folders run far above that), so a single
    drifting folder would have silenced every real divergence beside it.
    """
    stable = "2026/AA_one/stable"
    flaky = "2026/AA_one/flaky"
    kept = _tuple("a", folder=stable)
    drifting = _tuple("b", folder=flaky)
    real = _tuple("c", folder=stable)
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                "2026/AA_one": [
                    _path_run(DISCOVERY_LEGACY, [kept, drifting]),
                    _path_run(DISCOVERY_LEGACY, [kept], folders={stable, flaky}),
                    _path_run(DISCOVERY_INDEX, [kept, drifting, real]),
                ]
            }
        ),
        clock=_counting_clock(),
    )

    entry = verdict.entries[0]
    assert entry.withheld_folders == (flaky,)
    # The drifting folder's difference is withheld...
    assert [d.verified_file_path for d in entry.withheld_divergences] == []
    # ...and the stable folder's is CHARGED, so the gate fails.
    assert [d.verified_file_path for d in entry.divergences] == ["c"]
    assert entry.status == equivalence.ENTRY_DIVERGED
    assert verdict.status == equivalence.STATUS_REJECTED


def test_a_taint_reaches_the_folders_below_the_one_that_drifted():
    """`_is_under` reduced to exact membership left 1283 green.

    Prod is exactly the shape that punishes this: provider sub-paths and
    RED card directories sit BELOW the shoot folder whose paging drifts,
    so a difference owned by a card dir of a drifting shoot would be
    charged as a real divergence and reject the gate.
    """
    shoot = "2026/AA_one/shoot"
    card = f"{shoot}/CARD_001/CONTENTS"
    kept = _tuple("a", folder="2026/AA_one/stable")
    drifting = _tuple("b", folder=shoot)
    below = _tuple("c", folder=card)
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                "2026/AA_one": [
                    _path_run(DISCOVERY_LEGACY, [kept, drifting]),
                    _path_run(
                        DISCOVERY_LEGACY,
                        [kept],
                        folders={"2026/AA_one/stable", shoot},
                    ),
                    _path_run(DISCOVERY_INDEX, [kept, drifting, below]),
                ]
            }
        ),
        clock=_counting_clock(),
    )

    entry = verdict.entries[0]
    assert entry.withheld_folders == (shoot,)
    # `card` is not itself tainted — it is BELOW a tainted folder.
    assert card not in entry.withheld_folders
    assert [d.verified_file_path for d in entry.withheld_divergences] == ["c"]
    assert entry.divergences == ()
    assert verdict.status != equivalence.STATUS_REJECTED


def test_an_any_sided_waiver_suppresses_both_directions():
    """`SIDE_ANY` appeared only in the test that REFUSES a blanket row.

    Dropping the `self.side != SIDE_ANY` short-circuit left the suite
    green while silently disabling every `any` waiver — which is a side
    the loader accepts and the shipped waiver file tells operators to
    write.
    """
    waiver = equivalence.load_waivers(
        f"FR-9 | any | {FOLDER} | c | red | the card fix\n"
    )[0]
    index_only = equivalence.Divergence(
        side=equivalence.SIDE_INDEX_ONLY,
        classification=equivalence.CLASS_ABSENT_FROM_LEGACY,
        values=_tuple("c"),
    )
    legacy_only = equivalence.Divergence(
        side=equivalence.SIDE_LEGACY_ONLY,
        classification=equivalence.CLASS_ABSENT_FROM_INDEX,
        values=_tuple("c"),
    )

    assert waiver.matches(index_only)
    assert waiver.matches(legacy_only)
    # And it still discriminates on the fields that are not the side.
    other_file = equivalence.Divergence(
        side=equivalence.SIDE_INDEX_ONLY,
        classification=equivalence.CLASS_ABSENT_FROM_LEGACY,
        values=_tuple("d"),
    )
    assert not waiver.matches(other_file)


def test_a_withheld_divergence_carries_no_ratifiable_waiver_line_either():
    """The round-1 test for this rule was blind to its own subject.

    It iterated `reference_self_divergences`, whose sides are not in
    `GATE_SIDES` — so `proposed_waiver` is `None` one clause earlier and
    the assertion held with the `waivable` parameter deleted outright. A
    WITHHELD divergence is `index_only`/`legacy_only`, i.e. exactly the
    case `waivable=False` exists for.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                "2026/AA_one": [
                    [_tuple("a"), _tuple("b")],
                    [_tuple("a")],
                    [_tuple("a"), _tuple("c")],
                ]
            }
        ),
        clock=_counting_clock(),
    )

    entry = verdict.as_dict()["entries"][0]
    assert entry["withheld_divergences"]
    for divergence in entry["withheld_divergences"]:
        assert divergence["side"] in equivalence.GATE_SIDES
        assert divergence["proposed_waiver"] is None


def test_a_run_that_dies_late_keeps_the_evidence_from_the_runs_that_finished():
    """Reverting to a single tuple assignment left 1283 green.

    The errored entry a human has to diagnose then ships with nothing in
    it: no counts, no errors, no failed folders for the two legacy walks
    that completed before the third raised.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    calls = {"n": 0}

    def run_path(entry, mode):
        calls["n"] += 1
        if calls["n"] == 3:
            raise ConnectionResetError("peer closed the connection")
        return _path_run(mode, [_tuple("a")], errors=("Cannot get full path for x",))

    verdict = equivalence.run_equivalence(corpus, run_path, clock=_counting_clock())

    entry = verdict.entries[0]
    assert entry.status == equivalence.ENTRY_ERRORED
    assert "ConnectionResetError" in entry.error
    # The two walks that DID finish are still in the document.
    assert entry.reference is not None and entry.reference_second is not None
    assert entry.index is None
    assert entry.counts()["reference_tuples"] == 1
    assert entry.counts()["errors"] == 1
    payload = entry.as_dict()
    assert payload["errors"] == ["Cannot get full path for x"]
    assert len(payload["runs"]) == 2


def test_a_broken_entry_beside_an_unstable_one_reads_as_errored_not_withheld():
    """Ruling #4's precedence, which no test reached.

    Swapping the last two branches of `verdict_status` left the suite
    green while CI would read exit 2 (withheld: the instrument was
    flaky) where the ruling says exit 3 (errored: something is broken).
    The whole reason there are four codes is that those two must not look
    alike.
    """
    corpus = equivalence.load_corpus(
        "VX-41 | 2026/AA_flaky | unstable\nVX-41 | 2099/gone | archived\n"
    )

    verdict = equivalence.run_equivalence(
        corpus,
        _scripted_runner(
            {
                "2026/AA_flaky": [
                    [_tuple("a"), _tuple("b")],
                    [_tuple("a")],
                    [_tuple("a")],
                ],
                "2099/gone": _agreeing([]),
            }
        ),
        clock=_counting_clock(),
    )

    assert [e.status for e in verdict.entries] == [
        equivalence.ENTRY_UNSTABLE_REFERENCE,
        equivalence.ENTRY_ERRORED,
    ]
    assert equivalence.ENTRY_AGREED not in [e.status for e in verdict.entries]
    assert verdict.status == equivalence.STATUS_ERRORED


@pytest.mark.parametrize("attribute", ["umid", "provider_name", "storage_id"])
def test_an_empty_ad2_field_is_refused_like_a_null_one(attribute):
    """`""` is the second value that makes two broken extractions agree.

    The null parametrize covered `None`; the empty-string branch of
    `_required_field` was never reached, and an empty umid on both sides
    compares equal exactly the way `"None"` used to.
    """
    clip = FakeClip("a", umid="u", provider_name="red")
    setattr(clip, attribute, "")

    with pytest.raises(equivalence.EquivalenceError, match="is empty"):
        equivalence.ad2_tuple(clip, FOLDER)


@pytest.mark.parametrize("path", [None, ""])
def test_a_scanned_file_with_no_path_is_refused(path):
    """`verified_file_path` comes off `file.getPath()`, which can be empty.

    A pathless VSFile would otherwise become a sentinel that compares
    equal to another pathless one — the `str(None)` defect one field
    across.
    """
    clip = FakeClip("a", umid="u", provider_name="red")
    clip.file = FakeFile(path)

    with pytest.raises(equivalence.EquivalenceError, match="verified_file_path"):
        equivalence.ad2_tuple(clip, FOLDER)


def test_the_reference_runs_first_and_twice_and_the_index_runs_last():
    """ "Order is load-bearing" was asserted nowhere.

    `_scripted_runner` consumes its three scripted values positionally
    and rewrites `mode`, so it agrees with ANY order `compare_entry`
    picks. Inverting to index-first, or dropping the second reference
    run, would silently re-target every instability test in this file.
    """
    corpus = equivalence.load_corpus("VX-41 | 2026/AA_one | n\n")
    seen = []

    def run_path(entry, mode):
        seen.append(mode)
        return _path_run(mode, [_tuple("a")])

    equivalence.run_equivalence(corpus, run_path, clock=_counting_clock())

    assert seen == [DISCOVERY_LEGACY, DISCOVERY_LEGACY, DISCOVERY_INDEX]


def test_building_both_contexts_does_not_mutate_the_base_context():
    """ "No context is ever mutated (AD-3/AD-4)" had no test.

    The sharing test checks that the registry and storages are the SAME
    objects; nothing re-read the base's own `discovery` afterwards, which
    is the assertion that catches an in-place rebind.
    """
    base = _base_context()
    before = base.options.discovery
    context_for = equivalence.context_factory(base)

    legacy = context_for(_entry(), DISCOVERY_LEGACY)
    index = context_for(_entry(), DISCOVERY_INDEX)

    assert (legacy.options.discovery, index.options.discovery) == (
        DISCOVERY_LEGACY,
        DISCOVERY_INDEX,
    )
    assert base.options.discovery == before
    assert base.discovery_index is None

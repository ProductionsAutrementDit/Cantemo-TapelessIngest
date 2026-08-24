"""Tier 1: a folder that found media and produced no clip is DOUBT.

The gap this closes (Epic 2 retrospective, action item 3). ``walk_tree``
reads ``consumed_subdirs`` as three states, and ``scan.extraction`` is
careful everywhere that doubt is ``None`` and never an empty frozenset —
except in one case it never asked about: a folder that produced NO clips
at all falls through to ``frozenset()``, "descend into everything".

That is correct for an intermediate shoot folder. Discovery is scoped to
``parent == folder`` plus each provider's declared sub-paths
(``Folder.build_search_doc``), so a folder holding only card directories
has zero hits, zero clips, and genuinely consumed nothing — the walk MUST
descend to find the cards.

It is wrong for a card folder whose extractor broke. Measured (commit
e6d0090, REDline absent from cron's PATH): every ``.R3D`` was a hit, every
one died in the per-file wrapper as ``No UMID found in file …``, zero
clips were assembled — and the walk, reading ``frozenset()``, descended
into every ``.RDM``/``.RDC``. 118 folders / 584 clips / 556 errors where a
correct run reports 49 / 203 / 0.

Both shapes reach the gate as "zero clips". What separates them is
already in the response: the broken one had HITS it failed to claim, and
it had ERRORS. Hence the rule pinned here — and hence its NARROW form:
doubt requires a recorded error, so media that legitimately goes
unclaimed (a stray ``.MXF`` with no sidecar, sitting above real cards)
does not stop the walk. That narrowness is a deliberate ruling, not an
oversight, and ``test_unclaimed_hits_without_an_error_are_not_doubt``
is what keeps it from drifting.
"""

from portal.plugins.TapelessIngest.scan.extraction import unclaimed_hits_doubt


def test_hits_that_became_no_clip_with_an_error_are_doubt():
    """The REDline shape: media found, nothing claimed, something broke."""
    doubt = unclaimed_hits_doubt(verified_hits=68, clip_count=0, error_count=68)

    assert doubt is not None
    assert "68" in doubt


def test_unclaimed_hits_without_an_error_are_not_doubt():
    """The c1/c2 boundary — RULED narrow, and pinned so it stays narrow.

    A stray file that every provider's guard legitimately declines is a
    hit that becomes no clip, with nothing going wrong. Calling that
    doubt would stop the walk above real cards on the strength of one
    unclaimed file. The wider rule (doubt on any unclaimed hit) was
    considered and rejected; changing this test is how that decision gets
    revisited, deliberately.
    """
    assert unclaimed_hits_doubt(verified_hits=1, clip_count=0, error_count=0) is None


def test_a_folder_that_produced_clips_is_not_doubt_even_with_errors():
    """Per-file errors alongside real clips are the ordinary case.

    One unreadable file among ninety-nine good ones must not cost the
    folder its descent — the folder demonstrably worked.
    """
    assert unclaimed_hits_doubt(verified_hits=100, clip_count=99, error_count=1) is None


def test_a_folder_with_no_hits_is_not_doubt():
    """The intermediate shoot folder, and the reason the walk exists.

    Zero hits is not a failure to claim anything; it is nothing to claim.
    Making this doubt would stop the recursion dead at the top of every
    tree.
    """
    assert unclaimed_hits_doubt(verified_hits=0, clip_count=0, error_count=0) is None


def test_errors_from_files_that_never_verified_are_not_doubt():
    """The index/filesystem desync (DC-2), which is expected, not alarming.

    A ghost — an index entry with no file behind it — returns a hit row,
    fails the real-filesystem guard, and lands in ``errors``. That is
    FR-22 working as designed, and nothing about the folder is unknown:
    there was never any media to claim. This is why the count is of files
    that PASSED verification and reached extraction, not of rows the
    index returned; the first draft of this rule counted rows, and
    ``test_selective_recursion`` and ``test_coordinator_tree`` both
    caught it firing on their ghost folders.
    """
    assert unclaimed_hits_doubt(verified_hits=0, clip_count=0, error_count=3) is None

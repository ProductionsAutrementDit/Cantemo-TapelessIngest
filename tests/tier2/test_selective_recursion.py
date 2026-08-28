"""Tier 2 (story 2.6, FR-33): the REAL recursion over a real tmp tree.

This is the NFR-1 story's end-to-end guard. Before 2.6 both commands
recursed only when ``results["hits"] == 0``, so any subtree under a folder
that produced a clip was silently never scanned (FR-19), and that recursive
call dropped ``skip``/``only``/``startwith`` (FR-20). Fixing that means
descending into folders that were never descended into before — which is
exactly how a duplicate ingest gets created if consumption is wrong. So the
test that matters is not "was the independent clip found" alone, it is that
one together with **every umid extracted exactly once**.

Harness (the spec's PRIMARY option). ``query_elastic`` is answered by a
PATH-ROUTED responder rather than a scripted FIFO of pages: it reads the
``parent`` regexps out of the search doc scan actually built and returns
the fixture files whose parent directory matches one — the way the real
index behaves. A scripted queue would make the zero-duplicate assertion a
property of the page script; routed, it is a property of consumption.

Story 2.8 rebound the driver onto the seam that replaced the two
command-level recursions: ``Folder.scan_tree(ctx, emit=…)``. The filters
travel on the run context now, and the emission sink is a parameter, so
the module-global ``logger`` injection 2.6 needed is gone — a plain
recorder is passed in. Every assertion below is 2.6's, unchanged: the
claim is still that each umid is extracted exactly once while the
independent subtrees ARE reached.
"""

import importlib
import os
import re
from collections import Counter

import pytest

from portal.plugins.TapelessIngest.models.clip import Clip
from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.scan.adapters import build_context

STORAGE_ID = "VX-41"
CARD_PROVIDER_NAME = "fakecard"

MODULE = "portal.plugins.TapelessIngest.management.commands.scan_tapeless_dir"


class CardProvider:
    """A FakeProvider that declares a sub-path, like the real card providers.

    ``tests/conftest.py``'s FakeProvider declares ``getSubPaths() == []``,
    which cannot exercise consumption layer (b) at all. This one ships
    ``CARD/CLIP`` — the shape of panasonicP2's ``CONTENTS/VIDEO`` and
    xdcam's ``(PRIVATE/)?(M4ROOT/)?Clip``: a clip folder nested under an
    intermediate directory that holds no media file of its own.
    """

    name = "Fake Card Provider"
    machine_name = CARD_PROVIDER_NAME

    def __init__(self):
        self.seen_paths = []

    def getExtensions(self):
        return [".fake"]

    def getSubPaths(self):
        return ["CARD/CLIP"]

    def getFilters(self, escaped_path):
        return []

    def getMetadatasFromFile(self, media_file, metadatas, context):
        self.seen_paths.append(media_file.getPath())
        metadatas["provider"] = self.machine_name
        metadatas["umid"] = os.path.splitext(media_file.getPath())[0]
        return metadatas


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def log(self, message):
        self.messages.append(message)


@pytest.fixture
def card_provider():
    provider = CardProvider()
    Clip._PROVIDER_CACHE[CARD_PROVIDER_NAME] = provider
    yield provider
    Clip._PROVIDER_CACHE.pop(CARD_PROVIDER_NAME, None)


def _parent_regexps(search_doc):
    """Every ``{"regexp": {"parent": ...}}`` value in a search doc."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            regexp = node.get("regexp")
            if isinstance(regexp, dict) and "parent" in regexp:
                found.append(regexp["parent"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(search_doc)
    return found


def _source(path):
    return {
        "path": path,
        "hash": f"hash-{path}",
        "storage": STORAGE_ID,
        "id": f"VX-41-{path}",
        "size": 1024,
    }


def _install_router(es_fake, indexed, ghosts=()):
    """Answer query_elastic from the search doc, like the real index.

    ``indexed`` are storage-relative file paths the fixture created on
    disk; ``ghosts`` are index entries with no file behind them (the
    index/filesystem desync). A ghost contributes a hit ROW but not to
    ``total``, which is how this fixture builds the "zero hits, real
    errors" folder FR-22 is about.

    Returns the list of folder paths the scan queried, in call order.
    """
    queried = []
    all_paths = list(indexed) + list(ghosts)

    def respond(search_doc, first, number):
        regexps = _parent_regexps(search_doc)
        # parent_filters[0] is always the bare escaped folder path; every
        # other entry is that path plus a provider sub-path.
        queried.append(min(regexps, key=len))
        patterns = []
        for regexp in regexps:
            try:
                patterns.append(re.compile(regexp))
            except re.error:
                # A provider shipping an uncompilable sub-path is exactly
                # the doubt case below; the fixture index just ignores that
                # filter rather than modelling an ES-side failure.
                continue
        hits = [
            path
            for path in all_paths
            if any(pattern.fullmatch(os.path.dirname(path)) for pattern in patterns)
        ]
        total = len([path for path in hits if path in set(indexed)])
        return {
            "hits": {
                "total": {"value": total},
                "hits": [{"_source": _source(path)} for path in hits],
            }
        }

    es_fake.route(respond)
    return queried


def _run(module, monkeypatch, tmp_path, storage_fake, root_rel="2026", **kwargs):
    """Drive the real tree walk over the tmp tree, dry-run."""
    storage_fake.set_root(STORAGE_ID, str(tmp_path))
    context = build_context(
        [STORAGE_ID],
        user=None,
        dry_run=True,
        providers=[CARD_PROVIDER_NAME],
        legacy_storages=[],
        replace=False,
        **kwargs,
    )
    logger = RecordingLogger()

    parent = Folder(storage_id=STORAGE_ID, path=root_rel)
    # handle() seeds the top-level folder's root from the ctx; the walk's
    # worker seeds every child itself.
    parent._root_path = context.root_path_for(STORAGE_ID)

    run_result = parent.scan_tree(context, emit=logger.log)
    return run_result.folders_scanned, logger


def _write(tmp_path, *rel_paths):
    for rel in rel_paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"clip data")


def test_independent_subtrees_are_scanned_and_nothing_is_ingested_twice(
    migrated_db, es_fake, storage_fake, card_provider, tmp_path, monkeypatch
):
    module = importlib.import_module(MODULE)

    shoot = "2026/AH_20260101_shoot"
    card_a = f"{shoot}/CARD/CLIP/A.fake"
    card_b = f"{shoot}/CARD/CLIP/B.fake"
    # Independent, TWO levels below the folder that produced clips: today's
    # regression — the hits gate never looked below AH_20260101_shoot.
    deep = f"{shoot}/INTERVIEWS/DAY2/C.fake"
    # Excluded by --skip at depth 2 (the pre-2.6 recursive call dropped it).
    skipped = f"{shoot}/TMP/D.fake"
    # Excluded by --startWith at depth 1.
    outside = "2026/ZZ_20260101_ignored/E.fake"
    _write(tmp_path, card_a, card_b, deep, skipped, outside)
    # A folder with no media at all, whose index entry has no file behind
    # it: hits == 0 AND a real error. Pre-2.6 the whole log line — errors
    # included — was swallowed by the `hits > 0` gate (FR-22).
    (tmp_path / shoot / "NOFILES").mkdir(parents=True)
    ghost = f"{shoot}/NOFILES/GHOST.fake"

    queried = _install_router(
        es_fake, [card_a, card_b, deep, skipped, outside], ghosts=[ghost]
    )

    count, logger = _run(
        module,
        monkeypatch,
        tmp_path,
        storage_fake,
        startwith=["AH_"],
        skip=["TMP"],
    )

    # --- NFR-1: zero duplicates. -------------------------------------
    # Every umid the run extracted, exactly once, and exactly the set the
    # fixture makes reachable: the two card clips plus the deep
    # independent one. D (skipped) and E (startwith) are never touched.
    extracted = [os.path.splitext(path)[0] for path in card_provider.seen_paths]
    assert Counter(extracted) == Counter(
        [
            os.path.splitext(card_a)[0],
            os.path.splitext(card_b)[0],
            os.path.splitext(deep)[0],
        ]
    )
    assert len(extracted) == len(set(extracted))

    # --- FR-19: the consumed subtree is never even queried. ----------
    # CARD is consumed twice over — by the clips' own paths (layer a) and
    # by the CARD/CLIP sub-path's derived prefix (layer b).
    assert re.escape(f"{shoot}/CARD") not in queried
    assert re.escape(f"{shoot}/CARD/CLIP") not in queried
    # ...while the independent branch is walked all the way down.
    assert re.escape(f"{shoot}/INTERVIEWS") in queried
    assert re.escape(f"{shoot}/INTERVIEWS/DAY2") in queried

    # --- FR-20: the operator's filters apply at EVERY depth. ---------
    assert re.escape(f"{shoot}/TMP") not in queried
    assert re.escape("2026/ZZ_20260101_ignored") not in queried

    # --- FR-22: a zero-hit folder's errors are surfaced. -------------
    nofiles_lines = [
        message
        for message in logger.messages
        if message.startswith(f"found 0 files in {shoot}/NOFILES,")
    ]
    assert len(nofiles_lines) == 1
    assert "1 errors encountered" in nofiles_lines[0]
    assert f"Error scanning file {ghost}" in nofiles_lines[0]

    # Folders scanned: AH_shoot, its INTERVIEWS/DAY2/NOFILES, and the
    # ZZ_ sibling is never scanned at all (filtered before get_or_new).
    assert count == 4


def test_only_filter_applies_at_depth_without_a_date_window(
    migrated_db, es_fake, storage_fake, card_provider, tmp_path, monkeypatch
):
    """--only is an operator filter at every depth, on its own.

    Story 1.4 synthesized the --from/--since window into `only`
    (`only += date_window`), so the two were indistinguishable and `only`
    only ever selected depth-1 shoot folders. They are independent
    parameters since 2.6: this run passes NO window and still expects
    `only` to be evaluated at depth 2.
    """
    module = importlib.import_module(MODULE)

    shoot = "2026/AH_20260101_only"
    kept = f"{shoot}/only_keep/F.fake"
    dropped = f"{shoot}/drop/G.fake"
    _write(tmp_path, kept, dropped)

    queried = _install_router(es_fake, [kept, dropped])

    count, logger = _run(
        module,
        monkeypatch,
        tmp_path,
        storage_fake,
        startwith=["AH_"],
        only=["only"],
        date_window=None,
    )

    assert [os.path.splitext(path)[0] for path in card_provider.seen_paths] == [
        os.path.splitext(kept)[0]
    ]
    assert re.escape(f"{shoot}/only_keep") in queried
    assert re.escape(f"{shoot}/drop") not in queried
    assert count == 2


def test_doubt_in_the_response_forbids_descent(
    migrated_db, es_fake, storage_fake, card_provider, tmp_path, monkeypatch
):
    """`response["consumed_subdirs"] is None` = descent not authorized.

    A matched provider whose sub-path does not compile is the NFR-1 crux:
    the folder's own clips were found, but nothing can be said about what
    they consumed. The recursion must skip the folder entirely — not
    descend on the layer-(a) set it could have computed — and the reason
    must reach the operator through `response["errors"]`.
    """
    module = importlib.import_module(MODULE)

    shoot = "2026/AH_20260101_doubt"
    card = f"{shoot}/CARD/CLIP/H.fake"
    below = f"{shoot}/INTERVIEWS/I.fake"
    _write(tmp_path, card, below)

    queried = _install_router(es_fake, [card, below])
    # The provider still ships its working sub-path (so it matches a clip
    # and lands in the folder's matched set) PLUS one that does not
    # compile — the "otherwise complete pass" the tie-break is about.
    monkeypatch.setattr(card_provider, "getSubPaths", lambda: ["CARD/CLIP", "A)B/C"])

    count, logger = _run(module, monkeypatch, tmp_path, storage_fake, startwith=["AH_"])

    # The folder itself was scanned and its clip found...
    assert [os.path.splitext(path)[0] for path in card_provider.seen_paths] == [
        os.path.splitext(card)[0]
    ]
    # ...but NOTHING below it was queried, consumed or not.
    assert re.escape(f"{shoot}/CARD") not in queried
    assert re.escape(f"{shoot}/INTERVIEWS") not in queried
    assert count == 1

    [line] = [m for m in logger.messages if m.startswith(f"found 1 files in {shoot},")]
    assert f"Cannot compute consumed subdirs for {shoot}: 'A)B/C'" in line

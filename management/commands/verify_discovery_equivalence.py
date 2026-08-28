"""FR-4 / FR-34: run the AD-2 equivalence gate over a ratified corpus.

Read-only BY CONSTRUCTION, and the construction is the point:

* the run context is built with ``dry_run=True`` here and nowhere else —
  there is no flag that turns it off, the way ``check_clips_in_folder``
  has no flag that turns ``FORCE_DRY_RUN`` off;
* the harness drives the walk in the SCAN shape, so the ingest ladder and
  ``Folder.getCollection`` (which can CREATE a Vidispine collection) are
  not reachable;
* and no code path here writes a row, an item, or a waiver. The waiver
  list is READ. Its entries are added by a human, one per Feature-G fix,
  each citing its FR (AD-2).

The ``--out`` document is CANONICAL: no wall-clock, no timestamp, so two
runs over unchanged data write byte-identical files and the gate's
evidence is diffable. ``--with-timings`` adds them for a human reading one
run's cost; the timings always go to stdout regardless.

This command is NOT one of the twin scan commands: it shares no ``handle``
with them and must never be added to ``tests/tier1/test_commands_in_sync``.
It is deliberately separate because it must not grow a ``--dryrun`` flag,
a ``--workers`` flag (the verdict has to be reproducible, so the walk is
sequential) or a Slack leg.

Exit status is the gate, and the four gate outcomes get four codes: a
corpus typo and a flaky reference must not look alike to CI, which reads
the exit code and nothing else.
"""

import argparse
import datetime
import importlib
import inspect
import json
import logging

from django.core.management.base import BaseCommand, CommandError

from portal.search.elastic import query_elastic

from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
from portal.plugins.TapelessIngest.scan import adapters
from portal.plugins.TapelessIngest.scan import equivalence
from portal.plugins.TapelessIngest.scan.context import (
    DEFAULT_DISCOVERY_PAGE_SIZE,
    LEGACY_STORAGES,
    MAX_DISCOVERY_PAGE_SIZE,
)

log = logging.getLogger(__name__)

EXIT_ACCEPTED = 0
EXIT_REJECTED = 1
EXIT_WITHHELD = 2
EXIT_ERRORED = 3
# Distinct from every gate outcome: a corpus typo, an unparseable waiver
# list or a defective flag is the OPERATOR being wrong, not the gate
# saying anything about discovery.
EXIT_USAGE = 4

PLUGIN = "portal.plugins.TapelessIngest"

# What each digest CLAIMS to cover, and therefore what it must actually
# digest (AD-2: the verdict names both paths' versions). The groups are
# the modules that really determine an AD-2 tuple:
#
#   legacy   `build_search_doc` AND the `from`/`size` page loop in
#            `_scan_pass` — the loop is the half whose defect
#            `unstable_reference` exists for, and a digest of
#            `build_search_doc` alone was structurally blind to it.
#   index    the whole of `scan/discovery.py`.
#   shared   everything downstream of discovery that decides the tuple:
#            the walk and its descent authorization, extraction, the
#            filesystem verification the "verified" in
#            `verified_file_path` refers to, and the clip assembly that
#            produces `umid` and `provider_name`.
#   providers  the registry actually used by THIS run, resolved from
#            `--providers` rather than assumed, since three of the
#            tuple's five fields come out of them.
VERSION_GROUPS = {
    "legacy": (f"{PLUGIN}.models.folder",),
    "index": (f"{PLUGIN}.scan.discovery",),
    "shared": (
        f"{PLUGIN}.models.folder",
        f"{PLUGIN}.models.clip",
        f"{PLUGIN}.scan.coordinator",
        f"{PLUGIN}.scan.extraction",
        f"{PLUGIN}.scan.verification",
    ),
}


def _command_error(message, returncode):
    """``CommandError`` with an exit status where Django supports one.

    ``returncode`` is a Django >= 3.1 keyword. The dev environment is far
    past that, but this plugin ships into whatever Portal the server
    runs, and a ``TypeError`` out of the error path would turn a clean
    "rejected" into a traceback.
    """
    try:
        return CommandError(message, returncode=returncode)
    except TypeError:
        return CommandError(message)


def discovery_page_size(value):
    """argparse ``type=`` for --discovery-page-size: an int in [1, MAX].

    AD-10's fail-fast, and it has to be at PARSE time: deferring to
    ``RunOptions.__post_init__`` means the defect only surfaces after
    ``build_context`` has already spent a live ``getStorage`` per storage
    id. Not imported from the twin scan commands on purpose — importing
    one management command from another would drag its Slack client and
    config read into this one — but the BOUND is the shared constant, so
    there is still exactly one number.
    """
    try:
        page_size = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: '{value}'") from None
    if page_size < 1:
        raise argparse.ArgumentTypeError(
            f"--discovery-page-size must be >= 1 (got {page_size})"
        )
    if page_size > MAX_DISCOVERY_PAGE_SIZE:
        raise argparse.ArgumentTypeError(
            f"--discovery-page-size must be <= {MAX_DISCOVERY_PAGE_SIZE} "
            f"(got {page_size})"
        )
    return page_size


def read_module_source(label):
    """``inspect.getsource`` over a dotted module name, and it may raise.

    ``module_versions`` catches, so a source-less deploy (a .pyc-only
    install, a zipimport) degrades to a NAMED "unavailable" digest rather
    than turning a gate run into a traceback.
    """
    return inspect.getsource(importlib.import_module(label))


def discovery_versions(provider_names):
    """The digests, including the providers this run actually resolved."""
    groups = dict(VERSION_GROUPS)
    groups["providers"] = tuple(f"{PLUGIN}.providers.{name}" for name in provider_names)
    return equivalence.module_versions(groups, read_module_source)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--corpus",
            default=equivalence.default_corpus_path(),
            help="Corpus file: 'storage_id | path | note' lines (default: "
            "the proposed corpus shipped beside scan/equivalence.py)",
        )
        parser.add_argument(
            "--waivers",
            default=equivalence.default_waivers_path(),
            help="Append-only waiver list, READ ONLY (default: the list "
            "beside scan/equivalence.py)",
        )
        parser.add_argument(
            "--out",
            default=None,
            help="Write the canonical JSON verdict here; it is printed to "
            "stdout either way. Two runs over unchanged data write "
            "byte-identical files unless --with-timings is given",
        )
        parser.add_argument(
            "--with-timings",
            dest="with_timings",
            action="store_true",
            help="Add wall-clock and timestamp to the JSON verdict. This "
            "makes the document non-reproducible, so it is off by default",
        )
        parser.add_argument(
            "--providers",
            nargs="+",
            default=list(PROVIDER_NAMES),
            help="Provider registry for BOTH paths (one registry per run: "
            "provider_name must not diverge for registry reasons)",
        )
        parser.add_argument(
            "--discovery-page-size",
            dest="discovery_page_size",
            type=discovery_page_size,
            default=DEFAULT_DISCOVERY_PAGE_SIZE,
            help=f"Hits per response for the index path (default "
            f"{DEFAULT_DISCOVERY_PAGE_SIZE}, max {MAX_DISCOVERY_PAGE_SIZE})",
        )

    def handle(self, *cmd_args, **options):
        corpus_path = options["corpus"]
        waivers_path = options["waivers"]

        # Fail-fast (AD-10): a corpus or waiver defect aborts before any
        # storage resolution, index query or filesystem work. The page
        # size already failed at parse time.
        try:
            corpus = equivalence.load_corpus_file(corpus_path)
        except equivalence.CorpusError as e:
            raise _command_error(str(e), EXIT_USAGE) from None
        try:
            waivers = equivalence.load_waivers_file(waivers_path)
        except equivalence.WaiverError as e:
            raise _command_error(str(e), EXIT_USAGE) from None

        if not corpus.ratified:
            # F1: a zero-argument run uses the SHIPPED corpus, whose own
            # header says nothing in it is ratified. Said here, before
            # the run, and again in the verdict — an authoritative-looking
            # document over an unratified scope is how a rehearsal gets
            # quoted as the gate.
            self.stdout.write(
                f"WARNING: {corpus.source} is NOT RATIFIED. No human has "
                f"confirmed it covers the population FR-4 must be proven "
                f"over, so what follows is a rehearsal, not the gate."
            )

        # ONE context for the whole run, and therefore ONE provider
        # registry and one storage resolution shared by both paths
        # (AD-2/AD-4). `context_factory` rebinds only options.discovery,
        # and refuses an unresolved storage or a missing registry before
        # anything walks.
        base_context = adapters.build_context(
            corpus.storage_ids,
            user=None,
            # Not a flag. See the module docstring.
            dry_run=True,
            providers=options["providers"],
            legacy_storages=LEGACY_STORAGES,
            replace=False,
            discovery_page_size=options["discovery_page_size"],
        )
        try:
            context_for = equivalence.context_factory(base_context)
        except equivalence.EquivalenceError as e:
            raise _command_error(str(e), EXIT_USAGE) from None
        run_path = equivalence.build_path_runner(
            context_for=context_for,
            process_folder=self._process_folder(),
            query_elastic=query_elastic,
        )

        # C3: what this run was NARROWED to. A run over one provider
        # otherwise produces a document indistinguishable from a
        # full-registry one.
        scope = {
            "providers": sorted(options["providers"]),
            "discovery_page_size": options["discovery_page_size"],
            "legacy_storages": sorted(LEGACY_STORAGES),
            "storage_roots": {
                storage_id: base_context.storages[storage_id].root_path
                for storage_id in sorted(base_context.storages)
            },
            "walk": "sequential",
            "shape": "scan",
        }
        log.info(
            "Equivalence gate: %d corpus entr(ies) from %s (ratified=%s), "
            "%d waiver(s) from %s",
            len(corpus.entries),
            corpus.source,
            corpus.ratified,
            len(waivers),
            waivers_path,
        )
        started_at = datetime.datetime.now().isoformat(timespec="seconds")
        verdict = equivalence.run_equivalence(
            corpus,
            run_path,
            waivers=waivers,
            discovery_versions=discovery_versions(options["providers"]),
            scope=scope,
            # E4: three walks per entry over an 8,000-folder tree is
            # hours of silence otherwise.
            emit=self.stdout.write,
            now=started_at,
        )
        document = json.dumps(
            verdict.as_dict(with_timing=options["with_timings"]),
            indent=2,
            sort_keys=True,
        )
        if options["out"]:
            try:
                with open(options["out"], "w", encoding="utf-8") as handle:
                    handle.write(document + "\n")
            except OSError as e:
                # The verdict still goes to stdout below: a run that
                # PROVED something must not lose its evidence to a bad
                # --out path.
                self.stderr.write(f"cannot write the verdict to {options['out']}: {e}")
        for line in equivalence.render_verdict(verdict):
            self.stdout.write(line)
        # Never in the canonical document (it would make every pair of
        # runs differ), always on the console.
        self.stdout.write(f"started {started_at}, elapsed {verdict.elapsed:.1f}s")
        self.stdout.write(document)

        totals = verdict.totals()
        if verdict.status == equivalence.STATUS_REJECTED:
            raise _command_error(
                f"discovery equivalence REJECTED over {corpus.source} "
                f"({corpus.digest}): {totals['divergences']} unwaived "
                f"divergence(s), {totals['folder_divergences']} folder-set "
                f"divergence(s)",
                EXIT_REJECTED,
            )
        if verdict.status == equivalence.STATUS_ERRORED:
            raise _command_error(
                f"discovery equivalence could not RUN over {corpus.source} "
                f"({corpus.digest}): {totals[equivalence.ENTRY_ERRORED]} "
                f"entr(ies) produced no usable evidence; see the verdict",
                EXIT_ERRORED,
            )
        if verdict.status == equivalence.STATUS_UNSTABLE_REFERENCE:
            raise _command_error(
                f"discovery equivalence WITHHELD over {corpus.source} "
                f"({corpus.digest}): every entry's legacy reference "
                f"disagreed with itself, so nothing was proven",
                EXIT_WITHHELD,
            )
        if totals[equivalence.ENTRY_UNSTABLE_REFERENCE]:
            # ACCEPTED, but not over the whole corpus — and that has to
            # be said out loud rather than inferred from the totals.
            self.stdout.write(
                f"accepted over {totals[equivalence.ENTRY_AGREED]} of "
                f"{totals['entries']} entr(ies); "
                f"{totals[equivalence.ENTRY_UNSTABLE_REFERENCE]} withheld for "
                f"an unstable legacy reference"
            )

    def _process_folder(self):
        """Imported lazily so ``--help`` never pulls in the ORM."""
        from portal.plugins.TapelessIngest.models.folder import process_folder

        return process_folder

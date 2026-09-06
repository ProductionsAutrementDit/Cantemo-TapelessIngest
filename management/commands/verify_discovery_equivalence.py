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
import os
import tempfile

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
#   instrument the harness itself. A change to the comparison logic left
#            no trace in the verdict at all, which is the one module
#            whose movement invalidates every conclusion in the document.
VERSION_GROUPS = {
    "legacy": (f"{PLUGIN}.models.folder",),
    "index": (f"{PLUGIN}.scan.discovery",),
    "shared": (
        f"{PLUGIN}.models.folder",
        f"{PLUGIN}.models.clip",
        f"{PLUGIN}.scan.coordinator",
        f"{PLUGIN}.scan.extraction",
        f"{PLUGIN}.scan.verification",
        # Which providers get instantiated and which storage root the
        # filesystem verification runs against — both decide tuple
        # fields, and both were outside every digest.
        f"{PLUGIN}.scan.adapters",
        f"{PLUGIN}.scan.context",
    ),
    "instrument": (f"{PLUGIN}.scan.equivalence",),
}

# The base class every provider inherits `getExtensions`,
# `getSegmentedExtensions`, `getSubPaths` and `getMetadatasFromFile` from
# (so it decides `provider_name` and `umid` for every provider that does
# not override them), and the registry whose ORDER decides which provider
# claims a file. Both belong to the providers digest.
PROVIDER_SHARED = (f"{PLUGIN}.providers", f"{PLUGIN}.providers.providers")

# A provider's machine_name is USUALLY its module name, and where it is
# not the digest silently degraded to "unavailable" under a name that
# claimed to cover it.
PROVIDER_MODULES = {"audio_file": "audio_files"}


def provider_module(name):
    return f"{PLUGIN}.providers.{PROVIDER_MODULES.get(name, name)}"


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
        # The four-code contract is no longer in force, and a run whose
        # exit status silently became "1" would tell CI a corpus typo was
        # a discovery divergence. Say so where an operator can see it.
        log.error(
            "this Django does not support CommandError(returncode=): the "
            "gate's exit status collapses to 1, so exit code %d cannot be "
            "distinguished by CI",
            returncode,
        )
        return CommandError(f"[exit {returncode}] {message}")


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


def _write_atomically(path, text):
    """Write via a sibling temp file and ``os.replace``.

    A plain ``open(..., "w")`` truncates first, so a crash mid-write
    leaves a short JSON document that looks complete to everything except
    a parser — and the whole point of the file is to be diffable evidence
    someone trusts later.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def out_path(value):
    """argparse ``type=`` for --out: a non-empty path.

    ``--out ""`` is falsy, so it used to be skipped in silence and the
    operator believed evidence had been written to a file that never
    existed.
    """
    if not value.strip():
        raise argparse.ArgumentTypeError("--out cannot be empty")
    return value


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
    groups["providers"] = PROVIDER_SHARED + tuple(
        provider_module(name) for name in provider_names
    )
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
            type=out_path,
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
        # `build_provider_registry` de-duplicates, `scope` and the
        # providers digest did not: `--providers red red` produced the
        # same effective registry as `--providers red` under a different
        # digest, so two runs that proved the same thing wrote
        # non-comparable documents.
        providers = list(dict.fromkeys(options["providers"]))

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
        try:
            base_context = adapters.build_context(
                corpus.storage_ids,
                user=None,
                # Not a flag. See the module docstring.
                dry_run=True,
                providers=providers,
                legacy_storages=LEGACY_STORAGES,
                replace=False,
                discovery_page_size=options["discovery_page_size"],
            )
        except Exception as e:
            # An unknown provider name or an unreachable storage is the
            # OPERATOR being wrong. Outside a try it left a traceback and
            # Django's default exit 1 — indistinguishable, to the only
            # consumer that matters, from a real discovery divergence.
            raise _command_error(
                f"cannot build the run context: {type(e).__name__}: {e}",
                EXIT_USAGE,
            ) from None
        try:
            context_for = equivalence.context_factory(base_context)
        except equivalence.EquivalenceError as e:
            raise _command_error(str(e), EXIT_USAGE) from None
        run_path = equivalence.build_path_runner(
            context_for=context_for,
            process_folder=self._process_folder(),
            query_elastic=query_elastic,
            # E4 again: the per-ENTRY line lands only when an entry ends,
            # and the shipped corpus holds one entry. Progress has to
            # come from inside the walk.
            emit=self.stdout.write,
        )

        # C3: what this run was NARROWED to. A run over one provider
        # otherwise produces a document indistinguishable from a
        # full-registry one.
        scope = {
            "providers": sorted(providers),
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
        started_at = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        )
        verdict = equivalence.run_equivalence(
            corpus,
            run_path,
            waivers=waivers,
            discovery_versions=discovery_versions(providers),
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
        for line in equivalence.render_verdict(verdict):
            self.stdout.write(line)
        # Never in the canonical document (it would make every pair of
        # runs differ), always on the console.
        self.stdout.write(f"started {started_at}, elapsed {verdict.elapsed:.1f}s")
        self.stdout.write(document)
        # The evidence is on stdout before this point, so a --out failure
        # can be fatal without costing the run's findings. It IS fatal: a
        # verdict file that was asked for and not produced would otherwise
        # leave CI recording a green gate with no artifact behind it.
        if options["out"]:
            try:
                _write_atomically(options["out"], document + "\n")
            except OSError as e:
                raise _command_error(
                    f"cannot write the verdict to {options['out']}: {e} "
                    f"(the verdict itself is on stdout above)",
                    EXIT_USAGE,
                ) from None

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
                f"({corpus.digest}): no entry concluded — every one of them "
                f"was withheld for a legacy reference that disagreed with "
                f"itself, so nothing was proven",
                EXIT_WITHHELD,
            )
        if verdict.status != equivalence.STATUS_ACCEPTED:
            # Four statuses today. A fifth added later must not reach CI
            # as a green gate because this ladder ran out of branches.
            raise _command_error(
                f"unknown verdict status {verdict.status!r}; this command "
                f"does not know whether that is a pass",
                EXIT_USAGE,
            )
        if not corpus.ratified:
            # F1, and the exit code is the whole contract: an accepted
            # rehearsal that exits 0 IS a gate as far as CI can tell.
            raise _command_error(
                f"discovery equivalence WITHHELD over {corpus.source} "
                f"({corpus.digest}): the entries agreed, but the corpus is "
                f"NOT RATIFIED — no human has confirmed it covers the "
                f"population FR-4 must be proven over, so this is a "
                f"rehearsal and must not read as the gate",
                EXIT_WITHHELD,
            )
        if (
            totals[equivalence.ENTRY_UNSTABLE_REFERENCE]
            or totals[equivalence.ENTRY_ERRORED]
            or totals["withheld_folders"]
        ):
            # ACCEPTED, but not over the whole corpus — and that has to
            # be said out loud rather than inferred from the totals.
            self.stdout.write(
                f"accepted over {totals[equivalence.ENTRY_AGREED]} of "
                f"{totals['entries']} entr(ies); "
                f"{totals[equivalence.ENTRY_UNSTABLE_REFERENCE]} withheld for "
                f"an unstable legacy reference, "
                f"{totals[equivalence.ENTRY_ERRORED]} errored, "
                f"{totals['withheld_folders']} folder(s) withheld inside "
                f"entries that otherwise concluded"
            )
        log.info("discovery equivalence ACCEPTED (exit %d)", EXIT_ACCEPTED)

    def _process_folder(self):
        """Imported lazily so ``--help`` never pulls in the ORM."""
        from portal.plugins.TapelessIngest.models.folder import process_folder

        return process_folder

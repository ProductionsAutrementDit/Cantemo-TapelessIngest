# Ported verbatim from /opt/cantemo/scripts/check_clips_in_folder.py (prod
# copy, fetched 2026-08-20). Behavior-preserving, asymmetries included: calls
# folder.scan() (not folder.ingest()) and has no generic `except Exception`
# in the scan loop. The duplicated `skip` check went with story 2.6, which
# extracted the filter block into the shared `should_scan_entry` helper: the
# second check was byte-identical to the first and could never change the
# outcome. Approved deviations: the missing `WebClient` import is added (the
# prod script crashes at launch with NameError without it), django.setup()
# boilerplate dropped (manage.py owns setup), and module-level side effects
# (portal.conf read, Slack client, logger construction, main() call) moved
# into handle().
#
# KNOWN PRESERVED DEFECTS — the "preserved behavior" includes fatal runtime
# defects; this command has never completed a run in production:
# 1. folder.scan(...) is called with `dry_run` and `replace` kwargs that
#    Folder.scan (models/folder.py:284) does not accept -> TypeError on the
#    first folder processed.
# 2. The "found ... clips" log line reads result keys (`ingested`, `skipped`,
#    `replaced`) that only Folder.ingest produces, not Folder.scan ->
#    KeyError if execution ever got that far.
# Both are kept verbatim per this versioning story and are slated for the
# Epic 2 rebuild on the shared pipeline (FR-37). Do not "fix" them here.
description = """
Ingest found tapeless clips in all subdirectories.
"""
epilog = """
Example: Ingest all tapeless clips in folder 2022, in subfolders starting with AH_, with admin user:
./check_clips_in_folder.py --storage VX-41 --path 2022 --userId 1 --startWith AH_ --dryrun
"""
import os
import re
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from slack_sdk import WebClient

import argparse
import logging

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from configparser import ConfigParser

from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.helpers import TapelessIngestException
from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
from portal.plugins.TapelessIngest.scan import adapters
from portal.plugins.TapelessIngest.scan.verification import FolderListings

SLACK_ACCESS_TOKEN = None

# Slack chat.postMessage limits (docs.slack.dev, fetched 2026-08-20): the
# `text` field should be limited to 4,000 characters for readability, and
# Slack silently truncates messages containing more than 40,000 characters
# (there is no msg_too_long error). Chunk at the documented 4,000-character
# limit so no chunk ever nears the hard-truncation cliff.
SLACK_MAX_MESSAGE_LENGTH = 4000


class CustomLogger:
    def __init__(self):
        self.messages = []
        # Logging through standard Cantemo logging, i.e. to /var/log/cantemo/portal/portal.log
        self.logger = logging.getLogger("portal.plugins.TapelessIngest")
        self.slack_client = None
        if SLACK_ACCESS_TOKEN:
            try:
                self.slack_client = WebClient(token=SLACK_ACCESS_TOKEN)
            except Exception:
                self.slack_client = None
                self.logger.error(
                    "Failed to build Slack client; Slack notification "
                    "disabled for this run",
                    exc_info=True,
                )

    def log(self, message):
        print(message)
        self.logger.info(message)
        self.messages.append(message)

    def send_messages_to_slack(self):
        """Send the run report to Slack. Designed for a single end-of-run
        call: self.messages is not cleared, so a second call would resend
        the whole report."""
        if self.slack_client is None:
            self.logger.info(
                "Slack notification skipped: no Slack client (no [slack] "
                "ACCESS_TOKEN in portal.conf, or client construction failed)"
            )
            return
        if not self.messages:
            self.logger.info("Slack notification skipped: no messages to send")
            return
        # Greedy-pack whole messages (joined by newlines) into ordered
        # chunks of at most SLACK_MAX_MESSAGE_LENGTH characters; a single
        # message longer than the limit is hard-sliced into pieces.
        chunks = []
        current = ""
        for message in self.messages:
            pieces = [
                message[i : i + SLACK_MAX_MESSAGE_LENGTH]
                for i in range(0, len(message), SLACK_MAX_MESSAGE_LENGTH)
            ] or [message]
            for piece in pieces:
                candidate = f"{current}\n{piece}" if current else piece
                if len(candidate) <= SLACK_MAX_MESSAGE_LENGTH:
                    current = candidate
                else:
                    chunks.append(current)
                    current = piece
        if current:
            chunks.append(current)
        if not chunks:
            # Non-empty message list packed to nothing (all-empty strings):
            # every no-send path must leave a log line.
            self.logger.info("Slack notification skipped: nothing to send")
            return
        # One boundary around the whole loop, abort on first failure: after
        # a network/auth error the remaining sends would fail identically,
        # and a notification failure must never propagate into the scan run.
        try:
            for index, chunk in enumerate(chunks, start=1):
                self.slack_client.chat_postMessage(
                    channel="pad-notifications-cantemo",
                    text=chunk,
                )
        except Exception:
            self.logger.error(
                f"Slack notification failed on chunk {index}/{len(chunks)}; "
                f"remaining chunks abandoned",
                exc_info=True,
            )
            return
        self.logger.info(f"Slack notification sent ({len(chunks)} chunks)")


logger = None


# Module-level alias over the ONE canonical membership tuple (story
# 2.3). Kept, not removed: it is this command's --providers default AND
# the golden-doc recorder (tests/tier1/build_golden_doc.py) imports it.
PROVIDERS = list(PROVIDER_NAMES)

LEGACY_STORAGES = ["VX-2", "VX-26", "VX-11"]

SINCE_RE = re.compile(r"^(\d+)([dwmy])$")


def parse_since(value, now):
    """Parse a --since period (e.g. 10d, 2w, 3m, 1y) into a window start."""
    match = SINCE_RE.match(value)
    if not match:
        raise CommandError(
            f"Invalid --since '{value}': expected <number><unit>, "
            f"unit one of d/w/m/y"
        )
    number = int(match.group(1))
    unit = match.group(2)
    try:
        if unit == "d":
            return now - timedelta(days=number)
        if unit == "w":
            return now - timedelta(weeks=number)
        if unit == "m":
            return now - relativedelta(months=number)
        return now - relativedelta(years=number)
    except (OverflowError, ValueError) as e:
        raise CommandError(f"--since '{value}' out of range") from e


def parse_from(value):
    """Parse a --from date (YYYY-MM-DD) into a datetime."""
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise CommandError(f"Invalid --from '{value}': expected YYYY-MM-DD") from None


def compute_date_window(from_date, now):
    """Return one YYYYMMDD value per day from from_date to now, inclusive.

    The future-start error message is --from-specific by design: only
    --from can produce a future window start today (--since subtracts a
    non-negative period from now).
    """
    if from_date > now:
        raise CommandError("--from date is in the future")
    delta = now - from_date
    return [
        (from_date + timedelta(days=i)).strftime("%Y%m%d")
        for i in range(delta.days + 1)
    ]


def format_window_log(date_window):
    """Single range line summarizing the date window (FR-33)."""
    return (
        f"Scanning folders from {date_window[0]} to {date_window[-1]} "
        f"({len(date_window)} day folders)"
    )


def should_scan_entry(name, skip=None, only=None, startwith=None, date_window=None):
    """Do this subdirectory's filters allow it to be scanned?

    The legacy substring semantics verbatim (``str.find(...) != -1``, not
    ``in``) in the legacy order, extracted so the two commands cannot
    drift apart (FR-37; pinned by tests/tier1/test_commands_in_sync.py).

    Two deltas from the pre-2.6 inline block:

    * the byte-identical DUPLICATE ``skip`` check that followed
      ``startwith`` is gone — it could never change the outcome;
    * ``--skip``/``--only`` are the OPERATOR's filters and are evaluated
      at EVERY depth (FR-20 — the pre-2.6 recursive call forwarded
      neither), while ``startwith`` and ``date_window`` select shoot
      folders at depth 1 only and are simply not passed further down.

    ``date_window`` is its own parameter since story 2.6. Story 1.4
    synthesized it into ``only`` with an in-place ``+=`` that aliased
    ``args.only``, which made the two indistinguishable; they
    are independent now, so a run given both must satisfy both.
    """
    if skip:
        # search in skip if name contains one of the values
        for skip_entry in skip:
            if name.find(skip_entry) != -1:
                return False
    if only:
        # search in only if name contains one of the values
        if not any(name.find(only_entry) != -1 for only_entry in only):
            return False
    if startwith:
        # search in startwith if name begins with one of the values
        if not any(name.startswith(prefix) for prefix in startwith):
            return False
    if date_window:
        # one YYYYMMDD value per window day, same substring semantics
        if not any(name.find(day) != -1 for day in date_window):
            return False
    return True


def consumed_subdirs_from_results(results):
    """The descent authorization carried by a scan/ingest response.

    Three states, three representations (story 2.6, NFR-1):

    * a ``frozenset`` — descend into every child EXCEPT these names;
    * ``None`` — DOUBT: descent is not authorized for this folder at all,
      and the recursion skips it entirely;
    * no key — treated as ``None``, because a response that cannot say
      what it consumed must never be trusted to authorize a descent.

    ``None`` and ``frozenset()`` are NEVER interchangeable: the empty set
    is "nothing consumed, descend into everything", the opposite call.
    """
    return results.get("consumed_subdirs")


def scan_tapeless_dir(
    parent_folder,
    storage=None,
    ingest=False,
    user=None,
    startwith=None,
    count=0,
    skip=None,
    only=None,
    first=0,
    number=0,
    providers=None,
    replace=True,
    *,
    context=None,
    date_window=None,
    consumed=frozenset(),
):
    """Scan every subdirectory of ``parent_folder``, depth-first.

    ``consumed`` holds the immediate child names ``parent_folder``'s own
    clips already covered; they are skipped. The default is
    ``frozenset()`` — "nothing consumed" — so the root call from
    ``handle()`` descends into every child. Doubt is NEVER expressed here
    (that is what ``response["consumed_subdirs"] is None`` is for): an
    empty set means descend, and under-consuming is the one direction
    that produces an unrecoverable duplicate ingest (NFR-1).
    """
    if user is None:
        logger.log("User has to be provided")
    absolute_path = parent_folder.absolute_path
    if not absolute_path:
        # FR-28: an unresolvable storage is reported and the walk stops
        # here, instead of os.scandir(False) raising out of the run.
        logger.log(
            f"Cannot get full path from storage {parent_folder.storage_id}, "
            f"path {parent_folder.path}"
        )
        return count
    # Its own FolderListings (story 2.2's class-level reuse ruling): one
    # extra scandir per folder directory, consolidated by story 2.8.
    listings = FolderListings()
    listing = listings.get(absolute_path)
    # FR-22: a scandir failure is surfaced, never silently empty.
    for listing_path, listing_error in sorted(listings.errors().items()):
        logger.log(f"Error listing directory {listing_path}: {listing_error}")
    if listing.error is not None:
        return count
    # sorted(): deterministic cron logs, no hash-seed flakiness. `dirs`
    # excludes symlinked directories (FR-24's cycle guard, story 2.2).
    for name in sorted(listing.dirs):
        # NFR-1: a child this folder's own clips already covered is never
        # scanned again as a folder of its own — that IS the duplicate.
        if name in consumed:
            continue
        if not should_scan_entry(
            name, skip=skip, only=only, startwith=startwith, date_window=date_window
        ):
            continue
        folder_path = os.path.join(parent_folder.path, name)
        try:
            folder, is_new = Folder.get_or_new(storage_id=storage, path=folder_path)
            if context is not None:
                # Seed the memoized root from the run context BEFORE any
                # property access, so neither this folder's scan nor the
                # recursion's own listing re-resolves the storage (real
                # once-per-run, FR-7).
                child_root = context.root_path_for(storage)
                if child_root:
                    folder._root_path = child_root
            # print(f"Scanning {folder.path}...")
            ingest_message = ""
            results = folder.scan(
                first=first,
                number=number,
                cursor=None,
                user=user,
                providers=providers,
                legacy_storages=LEGACY_STORAGES,
                dry_run=not ingest,
                replace=replace,
                context=context,
            )
            count += 1
            error_message = ""
            if len(results["errors"]):
                error_string = "\n".join(results["errors"])
                error_message += f": {error_string}"
            # FR-22: log whenever there is anything to report. Before 2.6
            # the whole line was gated on hits > 0, so a zero-hit folder's
            # errors were swallowed together with it.
            if results["hits"] > 0 or len(results["errors"]):
                logger.log(
                    f"found {results['hits']} clips in {folder.path}, {results['already_ingested']} already ingested, {results['created']} created, providers are {folder.provider_names}, {results['ingested']} ingested, {results['skipped']} skipped, {results['replaced']} replaced, {len(results['errors'])} errors encountered{error_message}",
                )
            child_consumed = consumed_subdirs_from_results(results)
            if child_consumed is not None:
                # FR-19/FR-20: descend into every NON-consumed child, at
                # every depth, carrying the operator's filters down with
                # it. `startwith` and the date window select depth-1 shoot
                # folders and deliberately stop here.
                count = scan_tapeless_dir(
                    parent_folder=folder,
                    storage=storage,
                    ingest=ingest,
                    user=user,
                    count=count,
                    skip=skip,
                    only=only,
                    startwith=None,
                    providers=providers,
                    replace=replace,
                    context=context,
                    date_window=None,
                    consumed=child_consumed,
                )
        except FileNotFoundError:
            logger.log(f"Path doesn't exists anymore: {folder.path}")
        except TapelessIngestException as e:
            logger.log(f"Error ingesting {folder_path}: {e}")
    return count


class Command(BaseCommand):
    help = description + epilog

    def add_arguments(self, parser):
        parser.add_argument(
            "--storage", default="VX-41", help="Storage to list clip", required=True
        )
        parser.add_argument(
            "--path", default="2022", help="Root path to scan", required=True
        )
        parser.add_argument(
            "--userId",
            default="1",
            help="User id which perfom ingest",
            required=True,
        )
        parser.add_argument(
            "--startWith",
            nargs="+",
            default=["AH_"],
            help="Only scan folders which begin with one of these values "
            "(applies to top-level folders only)",
        )
        parser.add_argument(
            "--providers",
            nargs="+",
            default=PROVIDERS,
            help="Only scan folder which begin with this value",
        )
        parser.add_argument(
            "--skip",
            nargs="+",
            default=[],
            help="Skip folders containing these values",
        )
        parser.add_argument(
            "--only",
            nargs="+",
            default=[],
            help="Only scan folders containing these values",
        )
        # Only scan folders from this date
        parser.add_argument(
            "--from",
            dest="from_date",
            default=None,
            help="Only scan folders from this date, format YYYY-MM-DD; "
            "future dates are rejected; overrides --since when both are given",
        )
        # Only scan folders since this period
        parser.add_argument(
            "--since",
            dest="since",
            default=None,
            help="Only scan folders since this period: <number><unit>, "
            "units d/w/m/y (calendar-aware months/years), e.g. 1d, 2w, 3m, 4y",
        )
        parser.add_argument("--dryrun", action="store_true")
        parser.add_argument("--replace", action="store_true")

    def handle(self, *cmd_args, **options):
        global SLACK_ACCESS_TOKEN, logger
        args = argparse.Namespace(**options)

        # Fail-fast validation (AD-10): every argument defect aborts here
        # with a CommandError, before config read, Slack client, user
        # lookup and any index/filesystem/DB work.
        now = datetime.now()
        from_date = None
        if args.since:
            from_date = parse_since(args.since, now)
        if args.from_date:
            # --from wins over --since when both are given (preserved);
            # both are always validated.
            from_date = parse_from(args.from_date)
        date_window = None
        if from_date is not None:
            date_window = compute_date_window(from_date, now)

        try:
            user = User.objects.get(pk=args.userId)
        except (User.DoesNotExist, ValueError):
            raise CommandError(f"Unknown user id '{args.userId}'") from None

        # Side effects relocated from module level (approved deviation):
        # config read, Slack token, and logger construction happen at
        # command start instead of import time.
        cp = ConfigParser()
        cp.read("/etc/cantemo/portal/portal.conf")
        # raw=True: a literal % in the token must not trigger configparser
        # interpolation (InterpolationSyntaxError escapes the fallback).
        SLACK_ACCESS_TOKEN = cp.get("slack", "ACCESS_TOKEN", raw=True, fallback=None)
        logger = CustomLogger()

        storage = args.storage
        path = args.path

        only = args.only

        if date_window is not None:
            # Story 2.6: the window is its OWN depth-1 filter, passed as
            # date_window= below. It is no longer synthesized into `only`
            # (the in-place append aliased args.only itself), because
            # --only is an operator filter that applies at every depth
            # while the window selects shoot folders at depth 1 only.
            logger.log(format_window_log(date_window))

        # One context per run (story 2.1): the storage is resolved exactly
        # once here via the adapters, then threaded through the recursion as
        # an ADDITIONAL kwarg — every existing kwarg still passes unchanged.
        context = adapters.build_context(
            [args.storage],
            user=user,
            dry_run=args.dryrun,
            providers=args.providers,
            legacy_storages=LEGACY_STORAGES,
            replace=args.replace,
        )

        folder, is_new = Folder.get_or_new(storage_id=storage, path=path)
        # Seed the memoized root from the run context so the top-level
        # os.scandir(folder.absolute_path) never re-resolves the storage.
        root_path = context.root_path_for(storage)
        if root_path:
            folder._root_path = root_path
        # No KeyError possible: log the resolved VS object when the context
        # has one, else the raw storage id.
        storage_info = context.storages.get(storage)
        storage_label = storage_info.storage if storage_info is not None else storage
        logger.log(
            f"Scanning folder {folder.path} on storage {storage_label}, with user {user}, starting with {' or '.join(args.startWith)} using only {only}, skipping {args.skip}",
        )
        try:
            count = scan_tapeless_dir(
                folder,
                ingest=not args.dryrun,
                user=user,
                startwith=args.startWith,
                storage=storage,
                providers=args.providers,
                replace=args.replace,
                skip=args.skip,
                only=args.only,
                context=context,
                date_window=date_window,
            )
            logger.log(f"{count} folders scanned")
        except Exception as e:
            logger.log(f"Error scanning {folder.path}: {e}")
        logger.send_messages_to_slack()

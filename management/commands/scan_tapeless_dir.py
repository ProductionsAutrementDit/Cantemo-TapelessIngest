# Ported verbatim from /opt/cantemo/scripts/scan_tapeless_dir.py (prod copy,
# fetched 2026-08-20). Behavior-preserving: same flags, output, and failure
# modes. Only approved deviations: django.setup() boilerplate dropped
# (manage.py owns setup) and module-level side effects (portal.conf read,
# Slack client, logger construction, main() call) moved into handle().
description = """
Ingest found tapeless clips in all subdirectories.
"""
epilog = """
Example: Ingest all tapeless clips in folder 2022, in subfolders starting with AH_, with admin user:
./scan_tapeless_dir.py --storage VX-41 --path 2022 --userId 1 --startWith AH_ --dryrun
"""
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
from portal.plugins.TapelessIngest.providers import PROVIDER_NAMES
from portal.plugins.TapelessIngest.scan import adapters

SLACK_ACCESS_TOKEN = None

# FR-37: the ONE source difference between this command and its read-only
# sibling. `handle()` below is byte-identical in both files and reads this
# constant; nothing else may diverge.
FORCE_DRY_RUN = False

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

        if FORCE_DRY_RUN:
            # FR-37: this command is read-only BY CONSTRUCTION, so the two
            # write-side flags it keeps for sibling symmetry are inert.
            # Said once, at the start, rather than left for the operator to
            # infer from a summary that is labelled a rehearsal whatever
            # flags they passed.
            if args.dryrun:
                logger.log(
                    "--dryrun has no effect here: this command is read-only "
                    "by construction and always runs as a dry run"
                )
            if args.replace:
                # NOT "no effect": since story 2.7 the ladder runs in dry
                # mode too, so --replace still moves the would-be counters.
                logger.log(
                    "--replace performs no writes here: this command is "
                    "read-only by construction, but the flag still changes "
                    "the would-be counters this run reports"
                )

        if date_window is not None:
            # Story 2.6: the window is its OWN depth-1 filter, carried on
            # the run context below. It is no longer synthesized into
            # `only` (the in-place append aliased args.only itself),
            # because --only is an operator filter that applies at every
            # depth while the window selects shoot folders at depth 1 only.
            logger.log(format_window_log(date_window))

        # One context per run (story 2.1): the storage is resolved exactly
        # once here via the adapters. Since story 2.8 it also carries the
        # four folder filters, which used to be loose kwargs threaded
        # through a recursion that lived in this file.
        context = adapters.build_context(
            [args.storage],
            user=user,
            dry_run=args.dryrun or FORCE_DRY_RUN,
            providers=args.providers,
            legacy_storages=LEGACY_STORAGES,
            replace=args.replace,
            skip=args.skip,
            only=args.only,
            startwith=args.startWith,
            date_window=date_window,
        )

        folder, is_new = Folder.get_or_new(storage_id=storage, path=path)
        # Seed the memoized root from the run context so the top-level
        # directory listing never re-resolves the storage.
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
            # Story 2.8: the walk, the per-folder emission, the timing fold
            # and the end-of-run summary all live in the coordinator now.
            # This command owns exactly two things: building the context
            # and flushing Slack once.
            folder.scan_tree(context, emit=logger.log)
        except Exception as e:
            logger.log(f"Error scanning {folder.path}: {e}")
        logger.send_messages_to_slack()

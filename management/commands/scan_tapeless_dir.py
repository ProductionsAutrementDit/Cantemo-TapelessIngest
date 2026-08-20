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
import os
from datetime import datetime, timedelta
from slack_sdk import WebClient

import argparse
import logging

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from configparser import ConfigParser

from portal.plugins.TapelessIngest.models.folder import Folder
from portal.plugins.TapelessIngest.helpers import TapelessIngestException


SLACK_ACCESS_TOKEN = None


class CustomLogger:
    def __init__(self):
        self.messages = []
        # Logging through standard Cantemo logging, i.e. to /var/log/cantemo/portal/portal.log
        self.logger = logging.getLogger("portal.plugins.TapelessIngest")
        self.slack_client = WebClient(token=SLACK_ACCESS_TOKEN)

    def log(self, message):
        print(message)
        self.logger.info(message)
        self.messages.append(message)

    def send_messages_to_slack(self):
        all_messages = "\n".join(self.messages)
        self.slack_client.chat_postMessage(
            channel="pad-notifications-cantemo",
            text=all_messages,
        )


logger = None


PROVIDERS = [
    "panasonicP2",
    "xdcam",
    "hdslr",
    "zoom",
    "red",
    "avchd",
    "atomos",
    "file",
]

LEGACY_STORAGES = ["VX-2", "VX-26", "VX-11"]


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
):
    if user is None:
        logger.log("User has to be provided")
    with os.scandir(parent_folder.absolute_path) as it:
        for entry in it:
            if entry.is_dir():
                if skip:
                    # search in skip if entry.name contains one of the values
                    found = False
                    for skip_entry in skip:
                        if entry.name.find(skip_entry) != -1:
                            found = True
                            continue
                    if found:
                        continue
                if only:
                    # search in only if entry.name contains one of the values
                    found = False
                    for only_entry in only:
                        if entry.name.find(only_entry) != -1:
                            found = True
                            continue
                    if not found:
                        continue
                if startwith:
                    # search in startwith if entry.name contains one of the values
                    found = False
                    for startwith_entry in startwith:
                        if entry.name.startswith(startwith_entry):
                            found = True
                            continue
                    if not found:
                        continue
                if skip:
                    found = False
                    for skip_entry in skip:
                        if entry.name.find(skip_entry) != -1:
                            found = True
                            continue
                    if found:
                        continue
                folder_path = os.path.join(parent_folder.path, entry.name)
                try:
                    folder, is_new = Folder.get_or_new(
                        storage_id=storage, path=folder_path
                    )
                    # print(f"Scanning {folder.path}...")
                    ingest_message = ""
                    results = folder.ingest(
                        first=first,
                        number=number,
                        cursor=None,
                        user=user,
                        providers=providers,
                        legacy_storages=LEGACY_STORAGES,
                        dry_run=not ingest,
                        replace=replace,
                    )
                    if results["hits"] == 0:
                        count = scan_tapeless_dir(
                            parent_folder=folder,
                            storage=storage,
                            ingest=ingest,
                            user=user,
                            count=count,
                            providers=providers,
                            replace=replace,
                        )
                    count += 1
                    error_message = ""
                    if len(results["errors"]):
                        error_string = "\n".join(results["errors"])
                        error_message += f": {error_string}"
                    if results["hits"] > 0:
                        logger.log(
                            f"found {results['hits']} clips in {folder.path}, {results['already_ingested']} already ingested, {results['created']} created, providers are {folder.provider_names}, {results['ingested']} ingested, {results['skipped']} skipped, {results['replaced']} replaced, {len(results['errors'])} errors encountered{error_message}",
                        )
                except FileNotFoundError:
                    logger.log(f"Path doesn't exists anymore: {folder.path}")
                except TapelessIngestException as e:
                    logger.log(f"Error ingesting {folder_path}: {e}")
                except Exception as e:
                    logger.log(f"Error scanning {folder_path}: {e}")
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
            help="Only scan folder which begin with this value",
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
            help="Only scan folders from this date, format YYYY-MM-DD",
        )
        # Only scan folders since this period
        parser.add_argument(
            "--since",
            dest="since",
            default=None,
            help="Only scan folders since this period, format 1d, 2w, 3m, 4y",
        )
        parser.add_argument("--dryrun", action="store_true")
        parser.add_argument("--replace", action="store_true")

    def handle(self, *cmd_args, **options):
        # Side effects relocated from module level (approved deviation):
        # config read, Slack token, and logger construction happen at
        # command start instead of import time.
        global SLACK_ACCESS_TOKEN, logger
        cp = ConfigParser()
        cp.read("/etc/cantemo/portal/portal.conf")
        SLACK_ACCESS_TOKEN = cp.get("slack", "ACCESS_TOKEN")
        logger = CustomLogger()

        args = argparse.Namespace(**options)

        storage = args.storage
        path = args.path
        user = User.objects.get(pk=args.userId)

        only = args.only
        from_date = None

        # If since is provided, calculate from_date
        if args.since:
            since = args.since
            # Get number and unit
            number = int(since[:-1])
            unit = since[-1]
            # Calculate from_date
            if unit == "d":
                from_date = datetime.now() - timedelta(days=number)
            elif unit == "w":
                from_date = datetime.now() - timedelta(weeks=number)
            elif unit == "m":
                from_date = datetime.now() - timedelta(months=number)
            elif unit == "y":
                from_date = datetime.now() - timedelta(years=number)

        if args.from_date:
            # convert date to datetime
            from_date = datetime.strptime(args.from_date, "%Y-%m-%d")

        if from_date:
            logger.log(f"Scanning from {from_date}")
            # For each days since from_date, scan folder
            now = datetime.now()
            delta = now - from_date
            for i in range(delta.days + 1):
                date = from_date + timedelta(days=i)
                date_path = date.strftime("%Y%m%d")
                only += [date_path]
                logger.log(f"Scanning folders from {date_path}")

        folder, is_new = Folder.get_or_new(storage_id=storage, path=path)
        logger.log(
            f"Scanning folder {folder.path} on storage {folder.storage}, with user {user}, starting with {' or '.join(args.startWith)} using only {only}, skipping {args.skip}",
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
            )
            logger.log(f"{count} folders scanned")
        except Exception as e:
            logger.log(f"Error scanning {folder.path}: {e}")
        logger.send_messages_to_slack()

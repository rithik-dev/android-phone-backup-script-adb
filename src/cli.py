"""Command line entry point."""

import argparse
import os
import sys

from .config import DEFAULT_JOBS
from .errors import BackupError
from .runner import BackupRunner

DEFAULT_DEST = os.path.join(os.path.expanduser("~"), "Downloads",
                            "Android Backups")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="backup",
        description="Fast incremental Android media backup over ADB.")
    parser.add_argument(
        "--dest", default=os.environ.get("ANDROID_BACKUP_DEST") or DEFAULT_DEST,
        help="Backup root. Each run creates a timestamped snapshot inside it. "
             "Defaults to $ANDROID_BACKUP_DEST, else ~/Downloads/Android Backups.")
    parser.add_argument(
        "--serial",
        help="Target a specific device serial. Required when more than one "
             "device is attached.")
    parser.add_argument(
        "--jobs", type=int, default=DEFAULT_JOBS,
        help="Concurrent transfer streams (default %d). USB is usually the "
             "bottleneck, so higher values rarely help." % DEFAULT_JOBS)
    parser.add_argument(
        "--only",
        help="Comma separated folders to back up, for example DCIM,Pictures.")
    parser.add_argument(
        "--full", action="store_true",
        help="Re-transfer everything instead of reusing the previous snapshot.")
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Start a new snapshot instead of resuming an interrupted one.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and report what would transfer, then exit without writing.")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Echo every transferred file. Always recorded in the log file.")
    parser.add_argument(
        "--no-progress", action="store_true",
        help="Disable the live status line, for non-interactive logs.")
    return parser.parse_args(argv)


def main(argv=None):
    try:
        return BackupRunner(parse_args(argv)).run()
    except BackupError as error:
        print("\nError: %s" % error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130

"""Orchestrates a single backup run."""

import datetime
import signal
import sys
import threading
import time

from .adb import Adb, require_adb, select_device
from .config import DEFAULT_ROOTS
from .errors import BackupError
from .formatting import human_bytes, human_time
from .platform_hints import free_space_hint
from .progress import Progress
from .runlog import RunLog
from .scan import DeviceScanner
from .snapshot import BackupPlan, SnapshotStore, hardlink_files
from .transfer import TransferEngine

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_INTERRUPTED = 130

RULE = "=" * 70


class BackupRunner:
    def __init__(self, options):
        self.options = options
        self.store = SnapshotStore(options.dest)
        self.serial = None
        self.adb = None

    def run(self):
        require_adb()
        self.serial = select_device(self.options.serial)
        self.adb = Adb(self.serial)
        print("Device      : %s" % self.serial)

        scan = self._scan()
        if not scan.files:
            print("Nothing to back up.")
            return EXIT_OK

        self.store.create()
        previous = None if self.options.full else self.store.latest_complete()
        manifest = previous.load_manifest() if previous else None
        if previous and manifest is None:
            print("Previous    : %s (manifest unreadable, transferring in full)"
                  % previous.name)
            previous = None

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest, resumed = self._choose_destination(timestamp)
        plan = BackupPlan.build(scan.files, dest, previous, manifest,
                                full=self.options.full)

        self._report_plan(dest, previous, plan)
        if self.options.dry_run:
            # Nothing has been written yet, so there is nothing to undo.
            print("\nDry run, nothing transferred.")
            return EXIT_OK

        self._check_space(plan)
        dest.create()
        dest.mark_incomplete(timestamp)
        return self._execute(dest, resumed, scan, plan, timestamp)

    # -- phases ------------------------------------------------------------

    def _scan(self):
        roots = DEFAULT_ROOTS
        if self.options.only:
            roots = [r.strip().strip("/") for r in self.options.only.split(",")
                     if r.strip()]

        print("Scanning device...", end="", flush=True)
        started = time.time()
        scanner = DeviceScanner(self.adb)
        present, missing = scanner.existing_roots(roots)
        if not present:
            raise BackupError("None of these folders exist on the device: %s"
                              % ", ".join(roots))
        scan = scanner.scan(present, missing)

        print("\rScan        : %d files, %s in %s"
              % (len(scan.files), human_bytes(scan.total_bytes),
                 human_time(time.time() - started)))
        if scan.missing_roots:
            print("Not present : %s" % ", ".join(scan.missing_roots))
        if scan.unparsed:
            print("Warning     : %d entries were unreadable and skipped"
                  % scan.unparsed)
        return scan

    def _choose_destination(self, timestamp):
        resumable = (None if self.options.no_resume or self.options.full
                     else self.store.latest_incomplete())
        if resumable:
            print("Resuming    : %s" % resumable.name)
            return resumable, True
        return self.store.new_snapshot(timestamp), False

    def _report_plan(self, dest, previous, plan):
        if previous:
            print("Previous    : %s" % previous.name)
        print("Destination : %s" % dest.path)
        print("To transfer : %d files, %s"
              % (len(plan.to_fetch), human_bytes(plan.fetch_bytes)))
        if plan.to_link:
            print("Reusing     : %d files, %s (hardlinked from previous snapshot)"
                  % (len(plan.to_link), human_bytes(plan.link_bytes)))
        if plan.present:
            print("Already here: %d files, %s"
                  % (len(plan.present), human_bytes(plan.present_bytes)))

    def _check_space(self, plan):
        free = self.store.free_bytes()
        if plan.fetch_bytes > free:
            raise BackupError(
                "Not enough free space at %s\n"
                "  need : %s\n"
                "  free : %s\n"
                "Free up space, or pass --dest pointing at a larger volume.%s"
                % (self.store.root, human_bytes(plan.fetch_bytes),
                   human_bytes(free), free_space_hint()))
        print("Free space  : %s" % human_bytes(free))
        print("Streams     : %d\n" % max(1, self.options.jobs))

    def _execute(self, dest, resumed, scan, plan, timestamp):
        log = RunLog(dest.log_path, append=resumed)
        log.start(self.serial, dest.name, resumed=resumed)

        progress = Progress(
            total_files=plan.total_files,
            total_bytes=plan.total_bytes,
            folder_totals=scan.folder_totals(),
            log_handle=log,
            verbose=self.options.verbose,
            use_status_line=not self.options.no_progress and sys.stdout.isatty(),
            preexisting=plan.present,
        )

        cancel = threading.Event()
        installed = signal.signal(signal.SIGINT, lambda *_: cancel.set())

        started = time.time()
        progress.start()
        failed = []
        try:
            to_fetch = list(plan.to_fetch)
            to_fetch += hardlink_files(plan.to_link, dest, progress)
            engine = TransferEngine(self.adb, dest, progress, cancel,
                                    jobs=self.options.jobs)
            failed = engine.run(to_fetch)
        finally:
            progress.stop()
            signal.signal(signal.SIGINT, installed)
            log.flush()

        self._report_summary(dest, scan, progress, failed,
                             time.time() - started)

        if cancel.is_set():
            outcome = "interrupted, snapshot incomplete"
        elif failed:
            outcome = "%d files failed, snapshot incomplete" % len(failed)
        else:
            outcome = "complete"
        log.finish(outcome, progress.done_bytes - progress.linked_bytes,
                   progress.linked_files, progress.linked_bytes, len(failed))
        log.close()

        if cancel.is_set():
            print("\nInterrupted. Re-run the same command to resume, files "
                  "already transferred are skipped.")
            return EXIT_INTERRUPTED

        if failed:
            print("\nFinished with %d failures. The snapshot stays marked "
                  "incomplete so the next run retries them." % len(failed))
            return EXIT_FAILURES

        dest.write_manifest(scan.files, self.serial)
        dest.mark_complete()
        print("\nBackup complete.")
        return EXIT_OK

    def _report_summary(self, dest, scan, progress, failed, elapsed):
        totals = scan.folder_totals()
        moved = progress.done_bytes - progress.linked_bytes

        print("\n" + RULE)
        print("Summary")
        print(RULE)
        for root in sorted(totals):
            total = totals[root]
            done = progress.folder_done.get(root, [0, 0])
            print("  %-18s %5d/%-5d files  %10s / %s"
                  % (root, done[0], total[0], human_bytes(done[1]),
                     human_bytes(total[1])))
        print("-" * 70)
        print("  Transferred      : %s in %s (%s/s average)"
              % (human_bytes(moved), human_time(elapsed),
                 human_bytes(moved / elapsed if elapsed else 0)))
        if progress.linked_files:
            print("  Reused           : %d files, %s (no transfer needed)"
                  % (progress.linked_files, human_bytes(progress.linked_bytes)))
        if failed:
            print("  Failed           : %d files (listed in the log)"
                  % len(failed))
        print("  Snapshot         : %s" % dest.path)
        print("  Log              : %s" % dest.log_path)

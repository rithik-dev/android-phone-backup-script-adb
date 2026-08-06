"""Live progress reporting."""

import shutil
import sys
import threading
import time

from .formatting import elide, human_bytes, human_time

BAR_WIDTH = 24
REFRESH_SECONDS = 0.2


class Progress:
    """Byte based progress, per folder counters, and a per file log.

    Percentage tracks bytes rather than file count, because a library holding
    3 GB videos next to 30 KB thumbnails makes a file count badly misleading.
    """

    def __init__(self, total_files, total_bytes, folder_totals, log_handle=None,
                 verbose=False, use_status_line=True, preexisting=()):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.folder_totals = folder_totals
        self.log = log_handle
        self.verbose = verbose
        self.use_status_line = use_status_line

        self.lock = threading.Lock()
        self.done_files = 0
        self.done_bytes = 0
        self.wire_bytes = 0
        self.linked_files = 0
        self.linked_bytes = 0
        # Bytes read for a file still in flight, keyed by transfer stream.
        # Without this the bar sits still for a minute on a large video.
        self.inflight = {}
        self.folder_done = {name: [0, 0] for name in folder_totals}
        self.finished_folders = set()
        self.current = ""
        self.started = time.time()

        # Files a resumed run found on disk count towards the folder totals in
        # the summary, but not towards this run's transfer progress.
        for remote in preexisting:
            self._credit_folder(remote)

        self._stop = threading.Event()
        self._thread = None
        self._line_length = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self.use_status_line:
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        with self.lock:
            self._clear_line()

    # -- events ------------------------------------------------------------

    def wire_progress(self, count, stream=None):
        with self.lock:
            self.wire_bytes += count
            if stream is not None:
                self.inflight[stream] = self.inflight.get(stream, 0) + count

    def set_current(self, relpath):
        with self.lock:
            self.current = relpath

    def file_done(self, remote, stream=None):
        with self.lock:
            self.done_files += 1
            self.done_bytes += remote.size
            if stream is not None:
                self.inflight[stream] = 0
            self._credit_folder(remote)
            self.current = remote.relpath
        self._log("FETCH", remote)
        if self.verbose:
            self.emit("  %s  %s" % (remote.relpath, human_bytes(remote.size)))
        self._announce_if_folder_done(remote.root)

    def file_linked(self, remote):
        with self.lock:
            self.done_files += 1
            self.done_bytes += remote.size
            self.linked_files += 1
            self.linked_bytes += remote.size
            self._credit_folder(remote)
            self.current = remote.relpath
        self._log("LINK ", remote)
        self._announce_if_folder_done(remote.root)

    def file_failed(self, relpath, reason):
        self.emit("  ! %s  (%s)" % (relpath, reason))
        if self.log:
            self.log.write("FAIL  %s\t%s\n" % (relpath, reason))

    def emit(self, text):
        """Print a durable line without leaving status line residue behind."""
        with self.lock:
            self._clear_line()
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    # -- internals ---------------------------------------------------------

    def _credit_folder(self, remote):
        entry = self.folder_done.get(remote.root)
        if entry:
            entry[0] += 1
            entry[1] += remote.size

    def _log(self, tag, remote):
        if self.log:
            self.log.write("%s %s\t%d\n" % (tag, remote.relpath, remote.size))

    def _announce_if_folder_done(self, root):
        with self.lock:
            total = self.folder_totals.get(root)
            done = self.folder_done.get(root)
            if not total or not done or root in self.finished_folders:
                return
            if done[0] < total[0]:
                return
            self.finished_folders.add(root)
            line = ("  %-18s done  %d files  %s  (%s elapsed)"
                    % (root, done[0], human_bytes(done[1]),
                       human_time(time.time() - self.started)))
        self.emit(line)

    def _clear_line(self):
        if self._line_length:
            sys.stdout.write("\r" + " " * self._line_length + "\r")
            sys.stdout.flush()
            self._line_length = 0

    def _tick(self):
        while not self._stop.wait(REFRESH_SECONDS):
            self._render()

    def _render(self):
        with self.lock:
            total = self.total_bytes
            done = self.done_bytes + sum(self.inflight.values())
            done = min(done, total) if total else done
            elapsed = time.time() - self.started

            fraction = min(max(done / total if total else 1.0, 0.0), 1.0)
            rate = self.wire_bytes / elapsed if elapsed > 0.5 else 0.0
            eta = (total - done) / rate if rate > 1024 else None

            filled = int(fraction * BAR_WIDTH)
            line = ("  %s  %5.1f%%  %s / %s  %s/s  %s elapsed  ETA %s  [%d/%d]  %s"
                    % ("#" * filled + "-" * (BAR_WIDTH - filled),
                       fraction * 100, human_bytes(done), human_bytes(total),
                       human_bytes(rate), human_time(elapsed), human_time(eta),
                       self.done_files, self.total_files,
                       elide(self.current, 40)))
            line = line[:shutil.get_terminal_size((100, 24)).columns - 1]

            sys.stdout.write("\r" + line
                             + " " * max(0, self._line_length - len(line)))
            sys.stdout.flush()
            self._line_length = len(line)

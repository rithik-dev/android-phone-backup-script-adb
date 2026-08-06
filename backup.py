#!/usr/bin/env python3
"""Fast incremental media backup from an Android device over ADB.

Transfers are streamed as a tar archive straight off the device
(`adb exec-out tar -cf - ...`) and extracted on the fly. Nothing is ever
written to the phone, so a backup needs no free space on the device, and
the per-file ADB round trip that makes `adb pull` slow on large folders is
paid once per batch instead of once per file.

Repeat runs are incremental: files whose size and mtime are unchanged since
the previous snapshot are hardlinked from it rather than transferred, so
each snapshot is a complete browsable tree that costs almost no extra disk.

Requires Python 3.8+ and adb on PATH.
"""

import argparse
import datetime
import errno
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

# Storage roots to back up, relative to the device's shared storage.
# Anything missing on the device is skipped silently.
DEFAULT_ROOTS = [
    "DCIM",
    "Pictures",
    "Movies",
    "Music",
    "Documents",
    "Download",
    "Recordings",
    "Alarms",
    "Notifications",
    "Ringtones",
    "Podcasts",
    "Audiobooks",
    "Android/media",  # app media: WhatsApp, Signal, Telegram attachments
]

SDCARD = "/sdcard"

# Device-side paths that must never be walked. Android/data and Android/obb are
# unreadable by the shell user on modern Android and would only produce noise.
EXCLUDED_PREFIXES = ("Android/data", "Android/obb")

# Filenames are passed to the device shell as tar arguments. The device's
# ARG_MAX is 2 MB but a single argv entry is capped near 128 KB, and adb sends
# the whole command as one string, so batches stay well under that.
CHUNK_BYTES = 32 * 1024

# Abort a transfer batch if no bytes arrive for this long (wedged adb / unplug).
STALL_TIMEOUT = 120.0

# Short control commands should never hang the run.
ADB_TIMEOUT = 120.0


class BackupError(Exception):
    """Fatal, user-facing error."""


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def human_bytes(n):
    if abs(n) < 1024:
        return "%d B" % n
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if abs(n) < 1024.0 or unit == "TB":
            return "%.1f %s" % (n, unit)


def human_time(seconds):
    if seconds is None or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def elide(text, width):
    """Trim to width, keeping the tail of a path (the interesting part)."""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return "..." + text[-(width - 3):]


# --------------------------------------------------------------------------
# ADB plumbing
# --------------------------------------------------------------------------

class Adb:
    """Thin wrapper that pins every command to one device serial."""

    def __init__(self, serial=None):
        self.serial = serial

    def _argv(self, args):
        base = ["adb"]
        if self.serial:
            base += ["-s", self.serial]
        return base + list(args)

    def run(self, args, timeout=ADB_TIMEOUT, binary=False):
        kwargs = {"capture_output": True, "timeout": timeout}
        if not binary:
            kwargs["text"] = True
        try:
            return subprocess.run(self._argv(args), **kwargs)
        except subprocess.TimeoutExpired:
            raise BackupError(
                "adb %s timed out after %ss - device may be unplugged or asleep"
                % (" ".join(args[:2]), int(timeout)))

    def shell(self, command, timeout=ADB_TIMEOUT, binary=False):
        return self.run(["shell", command], timeout=timeout, binary=binary)

    def popen_exec_out(self, command):
        """Start a binary-safe streaming command. exec-out does not mangle
        line endings the way `adb shell` can, which matters for tar data."""
        return subprocess.Popen(
            self._argv(["exec-out", command]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def require_adb():
    if not shutil.which("adb"):
        raise BackupError(
            "'adb' not found on PATH.\n"
            "Install Android SDK Platform Tools, e.g. "
            "`brew install --cask android-platform-tools`.")


def list_devices():
    """Parse `adb devices`. Only lines with a tab separator are device rows,
    which keeps daemon-startup chatter out of the results."""
    try:
        result = subprocess.run(["adb", "devices"], capture_output=True,
                                text=True, timeout=ADB_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BackupError("`adb devices` timed out. Try `adb kill-server`.")
    devices = []
    for line in result.stdout.splitlines():
        line = line.rstrip()
        if "\t" not in line:
            continue
        serial, _, state = line.partition("\t")
        devices.append((serial.strip(), state.strip()))
    return devices


def select_device(requested_serial=None):
    devices = list_devices()

    if requested_serial:
        for serial, state in devices:
            if serial == requested_serial:
                if state != "device":
                    raise BackupError(
                        "Device [%s] is in state '%s', expected 'device'."
                        % (serial, state))
                return serial
        raise BackupError("Device [%s] not found. Connected: %s"
                          % (requested_serial,
                             ", ".join(s for s, _ in devices) or "none"))

    if not devices:
        raise BackupError(
            "No devices found.\n"
            "Plug in the phone, enable USB Debugging, and set the USB mode to "
            "file transfer.")

    usable = [(s, st) for s, st in devices if st == "device"]
    unauthorized = [s for s, st in devices if st == "unauthorized"]

    if unauthorized and not usable:
        raise BackupError(
            "Device [%s] is unauthorized.\n"
            "Unlock the phone and tap 'Allow' on the USB debugging prompt."
            % unauthorized[0])

    if not usable:
        raise BackupError("No usable device. States: %s"
                          % ", ".join("%s=%s" % (s, st) for s, st in devices))

    if len(usable) > 1:
        raise BackupError(
            "Multiple devices connected:\n%s\nRe-run with --serial <SERIAL>."
            % "\n".join("  - %s" % s for s, _ in usable))

    return usable[0][0]


# --------------------------------------------------------------------------
# Device scan
# --------------------------------------------------------------------------

class RemoteFile:
    __slots__ = ("relpath", "size", "mtime")

    def __init__(self, relpath, size, mtime):
        self.relpath = relpath
        self.size = size
        self.mtime = mtime

    @property
    def root(self):
        return self.relpath.split("/", 1)[0]


def discover_roots(adb, requested):
    """Keep only the roots that actually exist on the device."""
    probe = "; ".join(
        "[ -d %s ] && echo %s" % (shlex.quote(SDCARD + "/" + r), shlex.quote(r))
        for r in requested)
    result = adb.shell(probe)
    present = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    missing = [r for r in requested if r not in present]
    return present, missing


def scan_device(adb, roots):
    """One find across every root: size, mtime and path, NUL-delimited.

    A single invocation keeps the fixed ~1s adb round trip from being paid
    per folder, and gives exact totals up front so progress can be a real
    percentage rather than a spinner. NUL delimiting keeps filenames
    containing newlines intact.
    """
    quoted = " ".join(shlex.quote(SDCARD + "/" + r) for r in roots)
    prune = " ".join(
        "-path %s -prune -o" % shlex.quote(SDCARD + "/" + p)
        for p in EXCLUDED_PREFIXES)
    command = ("find %s %s -type f -printf '%%s|%%T@|%%p\\0' 2>/dev/null"
               % (quoted, prune))

    # Scanning a large library takes a while; allow generous time.
    result = adb.shell(command, timeout=900.0, binary=True)

    files = []
    unparsed = 0
    prefix = SDCARD + "/"
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            text = record.decode("utf-8", "surrogateescape")
            size_s, mtime_s, path = text.split("|", 2)
            size = int(size_s)
            mtime = float(mtime_s)
        except ValueError:
            unparsed += 1
            continue
        if not path.startswith(prefix):
            unparsed += 1
            continue
        rel = path[len(prefix):]
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        files.append(RemoteFile(rel, size, mtime))
    return files, unparsed


# --------------------------------------------------------------------------
# Snapshot manifests / incremental diff
# --------------------------------------------------------------------------

MANIFEST_NAME = ".backup-manifest.json"
INCOMPLETE_NAME = ".backup-incomplete"


def write_manifest(dest, files, device_serial):
    payload = {
        "version": 1,
        "device": device_serial,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "files": {f.relpath: [f.size, f.mtime] for f in files},
    }
    tmp = os.path.join(dest, MANIFEST_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, os.path.join(dest, MANIFEST_NAME))


def load_manifest(snapshot_dir):
    path = os.path.join(snapshot_dir, MANIFEST_NAME)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle).get("files", {})
    except (OSError, ValueError):
        return None


def _snapshot_dirs(backup_root):
    """Snapshot directories, newest first (names are timestamps)."""
    try:
        entries = sorted(os.listdir(backup_root), reverse=True)
    except OSError:
        return []
    return [os.path.join(backup_root, name) for name in entries
            if os.path.isdir(os.path.join(backup_root, name))]


def find_previous_snapshot(backup_root):
    """Most recent completed snapshot, if any."""
    for candidate in _snapshot_dirs(backup_root):
        if os.path.exists(os.path.join(candidate, INCOMPLETE_NAME)):
            continue
        if os.path.exists(os.path.join(candidate, MANIFEST_NAME)):
            return candidate
    return None


def find_incomplete_snapshot(backup_root):
    """Most recent snapshot that was interrupted or ended with failures.

    Resuming into it is what makes an interrupted 80 GB transfer cheap to
    restart: files already on disk are skipped by plan_transfer.
    """
    for candidate in _snapshot_dirs(backup_root):
        if os.path.exists(os.path.join(candidate, INCOMPLETE_NAME)):
            return candidate
    return None


def mtime_matches(a, b):
    """Compare device and recorded mtimes with tolerance.

    Tar records whole seconds while find reports nanoseconds, so a strict
    comparison would mark every file as changed on the second run.
    """
    return abs(a - b) < 2.0


def plan_transfer(files, previous_dir, previous_manifest, dest, full=False):
    """Split the scan into files to fetch, to hardlink, and already present."""
    to_fetch, to_link, present = [], [], []
    for remote in files:
        # A resumed run may already have the file in the destination.
        # --full re-transfers regardless of what is on disk.
        if not full:
            local = os.path.join(dest, remote.relpath)
            try:
                stat = os.stat(local)
                if stat.st_size == remote.size and mtime_matches(stat.st_mtime,
                                                                 remote.mtime):
                    present.append(remote)
                    continue
            except OSError:
                pass

        if previous_manifest is not None:
            record = previous_manifest.get(remote.relpath)
            if (record and record[0] == remote.size
                    and mtime_matches(record[1], remote.mtime)):
                source = os.path.join(previous_dir, remote.relpath)
                if os.path.exists(source):
                    to_link.append((remote, source))
                    continue
        to_fetch.append(remote)
    return to_fetch, to_link, present


def hardlink_unchanged(to_link, dest, progress):
    """Reuse the previous snapshot's data. Falls back to a copy across
    filesystems, and reports the rest so they get fetched instead."""
    failed = []
    for remote, source in to_link:
        target = os.path.join(dest, remote.relpath)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            progress.file_linked(remote)
            continue
        try:
            os.link(source, target)
        except OSError as exc:
            if exc.errno in (errno.EXDEV, errno.EMLINK, errno.EPERM):
                try:
                    shutil.copy2(source, target)
                except OSError:
                    failed.append(remote)
                    continue
            else:
                failed.append(remote)
                continue
        progress.file_linked(remote)
    return failed


# --------------------------------------------------------------------------
# Progress reporting
# --------------------------------------------------------------------------

class Progress:
    """Byte-accurate global progress plus per-folder counters.

    Percentage tracks bytes rather than file count: with 3 GB videos sitting
    next to 30 KB thumbnails, a file-count percentage is badly misleading.
    """

    def __init__(self, total_files, total_bytes, folder_totals, log_handle,
                 verbose=False, use_status_line=True, preexisting=()):
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.folder_totals = folder_totals
        self.log = log_handle
        self.verbose = verbose
        self.use_status_line = use_status_line

        self.lock = threading.Lock()
        self.done_files = 0
        self.done_bytes = 0     # bytes credited from completed files
        self.wire_bytes = 0     # raw bytes off the USB link, for the live rate
        # Bytes read for a file that has not finished yet, per transfer stream.
        # Without this the bar would sit still for a minute on a large video.
        self.inflight = {}
        self.linked_files = 0
        self.linked_bytes = 0
        self.folder_done = {name: [0, 0] for name in folder_totals}
        self.current = ""
        self.started = time.time()
        self.finished_folders = set()

        # Files a resumed run found already on disk count towards the folder
        # totals in the summary, but not towards this run's transfer progress.
        for remote in preexisting:
            entry = self.folder_done.get(remote.root)
            if entry:
                entry[0] += 1
                entry[1] += remote.size

        self._stop = threading.Event()
        self._last_line_len = 0
        self._thread = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self.use_status_line:
            self._thread = threading.Thread(target=self._tick, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._clear_line()

    # -- event hooks -------------------------------------------------------

    def wire_progress(self, count, stream=None):
        with self.lock:
            self.wire_bytes += count
            if stream is not None:
                self.inflight[stream] = self.inflight.get(stream, 0) + count

    def file_done(self, remote, stream=None):
        with self.lock:
            self.done_files += 1
            self.done_bytes += remote.size
            if stream is not None:
                self.inflight[stream] = 0
            entry = self.folder_done.get(remote.root)
            if entry:
                entry[0] += 1
                entry[1] += remote.size
            self.current = remote.relpath
        self._write_log("FETCH", remote)
        if self.verbose:
            self._emit("  %s  %s" % (remote.relpath, human_bytes(remote.size)))
        self._maybe_folder_complete(remote.root)

    def file_linked(self, remote):
        with self.lock:
            self.done_files += 1
            self.done_bytes += remote.size
            self.linked_files += 1
            self.linked_bytes += remote.size
            entry = self.folder_done.get(remote.root)
            if entry:
                entry[0] += 1
                entry[1] += remote.size
            self.current = remote.relpath
        self._write_log("LINK ", remote)
        self._maybe_folder_complete(remote.root)

    def set_current(self, relpath):
        with self.lock:
            self.current = relpath

    def file_failed(self, relpath, reason):
        self._emit("  ! %s  (%s)" % (relpath, reason))
        if self.log:
            self.log.write("FAIL  %s\t%s\n" % (relpath, reason))

    def _maybe_folder_complete(self, root):
        with self.lock:
            total = self.folder_totals.get(root)
            done = self.folder_done.get(root)
            if not total or not done or root in self.finished_folders:
                return
            if done[0] < total[0]:
                return
            self.finished_folders.add(root)
            elapsed = time.time() - self.started
            summary = ("  %-18s done  %d files  %s  (%s elapsed)"
                       % (root, done[0], human_bytes(done[1]),
                          human_time(elapsed)))
        self._emit(summary)

    # -- rendering ---------------------------------------------------------

    def _write_log(self, tag, remote):
        if self.log:
            self.log.write("%s %s\t%d\n" % (tag, remote.relpath, remote.size))

    def _emit(self, text):
        """Print a durable line without leaving status-line residue."""
        with self.lock:
            self._clear_line_locked()
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def _clear_line(self):
        with self.lock:
            self._clear_line_locked()

    def _clear_line_locked(self):
        if self._last_line_len:
            sys.stdout.write("\r" + " " * self._last_line_len + "\r")
            sys.stdout.flush()
            self._last_line_len = 0

    def _tick(self):
        while not self._stop.wait(0.2):
            self._render()

    def _render(self):
        with self.lock:
            total_bytes = self.total_bytes
            # Credit partial progress through the file currently streaming.
            done_bytes = self.done_bytes + sum(self.inflight.values())
            done_bytes = min(done_bytes, total_bytes) if total_bytes else done_bytes
            done_files = self.done_files
            current = self.current
            wire = self.wire_bytes
            elapsed = time.time() - self.started

            fraction = (done_bytes / total_bytes) if total_bytes else 1.0
            fraction = min(max(fraction, 0.0), 1.0)
            rate = wire / elapsed if elapsed > 0.5 else 0.0
            remaining = total_bytes - done_bytes
            eta = remaining / rate if rate > 1024 else None

            width = 24
            filled = int(fraction * width)
            bar = "#" * filled + "-" * (width - filled)

            line = ("  %s  %5.1f%%  %s / %s  %s/s  %s elapsed  ETA %s"
                    "  [%d/%d]  %s"
                    % (bar, fraction * 100,
                       human_bytes(done_bytes), human_bytes(total_bytes),
                       human_bytes(rate), human_time(elapsed), human_time(eta),
                       done_files, self.total_files, elide(current, 40)))
            columns = shutil.get_terminal_size((100, 24)).columns
            line = line[:columns - 1]

            padding = max(0, self._last_line_len - len(line))
            sys.stdout.write("\r" + line + " " * padding)
            sys.stdout.flush()
            self._last_line_len = len(line)


# --------------------------------------------------------------------------
# Streaming transfer
# --------------------------------------------------------------------------

class CountingReader:
    """Counts bytes as tarfile pulls them off the pipe.

    Without this the progress bar would only advance when a file finishes,
    which looks frozen for over a minute on a multi-gigabyte video.
    """

    def __init__(self, stream, progress):
        self.stream = stream
        self.progress = progress
        self.last_read = time.time()

    def read(self, size=-1):
        chunk = self.stream.read(size)
        if chunk:
            self.last_read = time.time()
            self.progress.wire_progress(len(chunk), stream=id(self))
        return chunk

    def close(self):
        try:
            self.stream.close()
        except OSError:
            pass


def build_batches(files):
    """Group files into tar invocations that fit in one shell command."""
    batches, current, length = [], [], 0
    for remote in files:
        quoted = shlex.quote(remote.relpath)
        if current and length + len(quoted) + 1 > CHUNK_BYTES:
            batches.append(current)
            current, length = [], 0
        current.append(remote)
        length += len(quoted) + 1
    if current:
        batches.append(current)
    return batches


def safe_member_path(dest, name):
    """Reject absolute paths and traversal before extracting.

    Python 3.9 has no tarfile extraction filter and the archive is built by
    the device, so every member is validated here instead.
    """
    if name.startswith("/") or os.path.isabs(name):
        return None
    target = os.path.realpath(os.path.join(dest, name))
    root = os.path.realpath(dest)
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def transfer_batch(adb, batch, dest, progress, cancel):
    """Stream one tar archive of the given files and extract it in flight."""
    by_name = {remote.relpath: remote for remote in batch}
    names = " ".join(shlex.quote(remote.relpath) for remote in batch)
    command = "tar -cf - -C %s %s" % (shlex.quote(SDCARD), names)

    proc = adb.popen_exec_out(command)
    stderr_chunks = []
    reader = threading.Thread(
        target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()

    counting = CountingReader(proc.stdout, progress)
    extracted = set()
    reported = set()
    stream_error = None

    # A wedged adb or a yanked cable leaves read() blocked forever, so kill the
    # transfer if no bytes arrive for a while and let the batch be retried.
    stalled = threading.Event()

    def watchdog():
        while proc.poll() is None:
            if cancel.wait(5.0):
                return
            if time.time() - counting.last_read > STALL_TIMEOUT:
                stalled.set()
                proc.kill()
                return
    threading.Thread(target=watchdog, daemon=True).start()

    try:
        with tarfile.open(fileobj=counting, mode="r|") as archive:
            for member in archive:
                if cancel.is_set():
                    break
                # Directories are pre-created; symlinks, sockets and devices
                # are not worth restoring from a media backup.
                if not member.isfile():
                    continue
                if safe_member_path(dest, member.name) is None:
                    progress.file_failed(member.name, "unsafe path in archive")
                    reported.add(member.name)
                    continue
                # Name the file before reading it, so the status line shows
                # what is streaming rather than what just finished.
                progress.set_current(member.name)
                try:
                    archive.extract(member, path=dest)
                except (OSError, tarfile.TarError) as exc:
                    progress.file_failed(member.name, str(exc))
                    reported.add(member.name)
                    continue
                remote = by_name.get(member.name)
                if remote is not None:
                    extracted.add(member.name)
                    progress.file_done(remote, stream=id(counting))
    except tarfile.TarError as exc:
        stream_error = str(exc)
    finally:
        counting.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        reader.join(timeout=5)

    stderr_text = b"".join(c for c in stderr_chunks if c).decode(
        "utf-8", "replace").strip()

    if cancel.is_set():
        return []

    missing = [remote for name, remote in by_name.items() if name not in extracted]
    if missing:
        if stalled.is_set():
            detail = "stalled - no data for %ds" % int(STALL_TIMEOUT)
        else:
            detail = (stderr_text.splitlines()[-1] if stderr_text
                      else (stream_error or "not present in archive"))
        for remote in missing:
            if remote.relpath not in reported:  # already reported above
                progress.file_failed(remote.relpath, detail)
    return missing


def precreate_dirs(files, dest):
    """Create every destination directory up front.

    tarfile creates parent directories with a check-then-create that races
    when batches extract concurrently, so the directory tree is built once,
    single-threaded, before any transfer starts.
    """
    wanted = set()
    for remote in files:
        parent = os.path.dirname(remote.relpath)
        while parent and parent not in wanted:
            wanted.add(parent)
            parent = os.path.dirname(parent)
    for parent in sorted(wanted):
        os.makedirs(os.path.join(dest, parent), exist_ok=True)


def run_transfer(adb, to_fetch, dest, progress, jobs, cancel):
    batches = build_batches(to_fetch)
    if not batches:
        return []
    precreate_dirs(to_fetch, dest)

    failed = []
    lock = threading.Lock()

    def work(batch):
        if cancel.is_set():
            return
        missing = transfer_batch(adb, batch, dest, progress, cancel)
        if missing:
            with lock:
                failed.extend(missing)

    if jobs <= 1:
        for batch in batches:
            work(batch)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(work, batches))
    return failed


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def preflight_space(dest, needed_bytes):
    usage = shutil.disk_usage(dest)
    if needed_bytes > usage.free:
        raise BackupError(
            "Not enough free space at %s\n"
            "  need : %s\n"
            "  free : %s\n"
            "Free up space, or pass --dest pointing at a larger volume."
            % (dest, human_bytes(needed_bytes), human_bytes(usage.free)))
    return usage.free


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Fast incremental Android media backup over ADB.")
    parser.add_argument(
        "--dest", default=os.path.join(os.path.expanduser("~"), "Downloads",
                                       "Android Backups"),
        help="Backup root; each run creates a timestamped snapshot inside it.")
    parser.add_argument("--serial", help="Target a specific device serial.")
    parser.add_argument(
        "--jobs", type=int, default=2,
        help="Concurrent transfer streams (default 2; USB is usually the "
             "bottleneck, so higher rarely helps).")
    parser.add_argument(
        "--only", help="Comma-separated roots to back up, e.g. DCIM,Pictures")
    parser.add_argument(
        "--full", action="store_true",
        help="Ignore the previous snapshot and re-transfer everything.")
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Start a new snapshot instead of resuming an interrupted one.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scan and report what would transfer, then exit.")
    parser.add_argument(
        "--verbose", action="store_true",
        help="Echo every transferred file (always written to the log file).")
    parser.add_argument(
        "--no-progress", action="store_true",
        help="Disable the live status line (for non-interactive logs).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    require_adb()
    serial = select_device(args.serial)

    roots = DEFAULT_ROOTS
    if args.only:
        roots = [r.strip().strip("/") for r in args.only.split(",") if r.strip()]

    adb = Adb(serial)
    print("Device      : %s" % serial)

    print("Scanning device...", end="", flush=True)
    scan_started = time.time()
    present, missing = discover_roots(adb, roots)
    if not present:
        raise BackupError("None of the requested folders exist on the device: %s"
                          % ", ".join(roots))
    files, unparsed = scan_device(adb, present)
    scan_seconds = time.time() - scan_started
    print("\rScan        : %d files, %s in %s"
          % (len(files), human_bytes(sum(f.size for f in files)),
             human_time(scan_seconds)))
    if missing:
        print("Not present : %s" % ", ".join(missing))
    if unparsed:
        print("Warning     : %d entries could not be parsed and were skipped"
              % unparsed)
    if not files:
        print("Nothing to back up.")
        return 0

    backup_root = os.path.abspath(os.path.expanduser(args.dest))
    os.makedirs(backup_root, exist_ok=True)

    previous_dir = None if args.full else find_previous_snapshot(backup_root)
    previous_manifest = load_manifest(previous_dir) if previous_dir else None
    if previous_dir and previous_manifest is None:
        print("Previous    : %s (manifest unreadable, doing a full transfer)"
              % os.path.basename(previous_dir))
        previous_dir = None

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    resumed = (None if args.no_resume or args.full
               else find_incomplete_snapshot(backup_root))
    if resumed:
        dest = resumed
        print("Resuming    : %s" % os.path.basename(dest))
    else:
        # Timestamps have one-second resolution, so two runs started in the
        # same second would otherwise share a snapshot directory.
        dest = os.path.join(backup_root, "Android_Backup_%s" % timestamp)
        serial_suffix = 1
        while os.path.exists(dest):
            serial_suffix += 1
            dest = os.path.join(backup_root, "Android_Backup_%s_%d"
                                % (timestamp, serial_suffix))

    to_fetch, to_link, present = plan_transfer(files, previous_dir,
                                               previous_manifest, dest,
                                               full=args.full)
    fetch_bytes = sum(f.size for f in to_fetch)
    link_bytes = sum(f.size for f, _ in to_link)

    if previous_dir:
        print("Previous    : %s" % os.path.basename(previous_dir))
    print("Destination : %s" % dest)
    print("To transfer : %d files, %s" % (len(to_fetch), human_bytes(fetch_bytes)))
    if to_link:
        print("Reusing     : %d files, %s (hardlinked from previous snapshot)"
              % (len(to_link), human_bytes(link_bytes)))
    if present:
        print("Already here: %d files, %s"
              % (len(present), human_bytes(sum(f.size for f in present))))

    if args.dry_run:
        # Nothing has been created on disk yet, so there is nothing to undo.
        print("\nDry run - nothing transferred.")
        return 0

    os.makedirs(dest, exist_ok=True)
    free = preflight_space(backup_root, fetch_bytes)
    print("Free space  : %s" % human_bytes(free))
    print("Streams     : %d" % max(1, args.jobs))
    print()

    marker = os.path.join(dest, INCOMPLETE_NAME)
    with open(marker, "w") as handle:
        handle.write(timestamp)

    folder_totals = {}
    for remote in files:
        entry = folder_totals.setdefault(remote.root, [0, 0])
        entry[0] += 1
        entry[1] += remote.size

    log_path = os.path.join(dest, "backup.log")
    # Appending keeps the record of earlier attempts when resuming.
    log_handle = open(log_path, "a" if resumed else "w", encoding="utf-8")
    log_handle.write("# backup of %s started %s\n" % (serial, timestamp))

    progress = Progress(
        total_files=len(to_fetch) + len(to_link),
        total_bytes=fetch_bytes + link_bytes,
        folder_totals=folder_totals,
        log_handle=log_handle,
        verbose=args.verbose,
        use_status_line=not args.no_progress and sys.stdout.isatty(),
        preexisting=present,
    )

    cancel = threading.Event()

    def on_sigint(signum, frame):
        cancel.set()
    previous_handler = signal.signal(signal.SIGINT, on_sigint)

    started = time.time()
    progress.start()
    failed = []
    try:
        link_failures = hardlink_unchanged(to_link, dest, progress)
        if link_failures:
            to_fetch.extend(link_failures)
        failed = run_transfer(adb, to_fetch, dest, progress, max(1, args.jobs),
                              cancel)
    finally:
        progress.stop()
        signal.signal(signal.SIGINT, previous_handler)
        log_handle.flush()

    elapsed = time.time() - started
    interrupted = cancel.is_set()
    transferred_bytes = progress.done_bytes - progress.linked_bytes

    print()
    print("=" * 70)
    print("Summary")
    print("=" * 70)
    for root in sorted(folder_totals):
        total = folder_totals[root]
        done = progress.folder_done.get(root, [0, 0])
        print("  %-18s %5d/%-5d files  %10s / %s"
              % (root, done[0], total[0], human_bytes(done[1]),
                 human_bytes(total[1])))
    print("-" * 70)
    print("  Transferred      : %s in %s (%s/s average)"
          % (human_bytes(transferred_bytes), human_time(elapsed),
             human_bytes(transferred_bytes / elapsed if elapsed else 0)))
    if progress.linked_files:
        print("  Reused           : %d files, %s (no transfer needed)"
              % (progress.linked_files, human_bytes(progress.linked_bytes)))
    if failed:
        print("  Failed           : %d files (see the log)" % len(failed))
    print("  Snapshot         : %s" % dest)
    print("  Log              : %s" % log_path)

    if interrupted:
        print("\nInterrupted. Re-run the same command to resume; files already "
              "transferred are skipped.")
        log_handle.close()
        return 130

    if failed:
        print("\nFinished with %d failures; snapshot left marked incomplete "
              "so the next run retries them." % len(failed))
    else:
        write_manifest(dest, files, serial)
        os.remove(marker)
        print("\nBackup complete.")

    log_handle.close()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BackupError as error:
        print("\nError: %s" % error, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        sys.exit(130)

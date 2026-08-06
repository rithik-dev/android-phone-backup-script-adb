"""Streaming tar transfer off the device."""

import os
import shlex
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import CHUNK_BYTES, SDCARD, STALL_TIMEOUT


class CountingReader:
    """Wraps the adb pipe so bytes are counted as tarfile consumes them."""

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


def build_batches(files, chunk_bytes=CHUNK_BYTES):
    """Group files into tar invocations that fit in one shell command."""
    batches, current, length = [], [], 0
    for remote in files:
        quoted = shlex.quote(remote.relpath)
        if current and length + len(quoted) + 1 > chunk_bytes:
            batches.append(current)
            current, length = [], 0
        current.append(remote)
        length += len(quoted) + 1
    if current:
        batches.append(current)
    return batches


def safe_member_path(dest_path, name):
    """Reject absolute paths and traversal. The archive is built by the device,
    and Python 3.9 has no tarfile extraction filter, so members are checked here."""
    if name.startswith("/") or os.path.isabs(name):
        return None
    target = os.path.realpath(os.path.join(dest_path, name))
    root = os.path.realpath(dest_path)
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def precreate_dirs(files, dest_path):
    """Build the directory tree once, single threaded.

    tarfile creates parent directories with a check then create that races when
    batches extract concurrently.
    """
    wanted = set()
    for remote in files:
        parent = os.path.dirname(remote.relpath)
        while parent and parent not in wanted:
            wanted.add(parent)
            parent = os.path.dirname(parent)
    for parent in sorted(wanted):
        os.makedirs(os.path.join(dest_path, parent), exist_ok=True)


class TransferEngine:
    """Streams batches of files off the device and extracts them in flight."""

    def __init__(self, adb, dest, progress, cancel, jobs=1):
        self.adb = adb
        self.dest_path = dest.path
        self.progress = progress
        self.cancel = cancel
        self.jobs = max(1, jobs)

    def run(self, to_fetch):
        """Transfer everything, returning the files that did not arrive."""
        batches = build_batches(to_fetch)
        if not batches:
            return []
        precreate_dirs(to_fetch, self.dest_path)

        failed = []
        lock = threading.Lock()

        def work(batch):
            if self.cancel.is_set():
                return
            missing = self.transfer(batch)
            if missing:
                with lock:
                    failed.extend(missing)

        if self.jobs == 1:
            for batch in batches:
                work(batch)
        else:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                list(pool.map(work, batches))
        return failed

    def transfer(self, batch):
        by_name = {remote.relpath: remote for remote in batch}
        names = " ".join(shlex.quote(remote.relpath) for remote in batch)
        proc = self.adb.stream("tar -cf - -C %s %s" % (shlex.quote(SDCARD), names))

        stderr_chunks = []
        stderr_reader = threading.Thread(
            target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
        stderr_reader.start()

        reader = CountingReader(proc.stdout, self.progress)
        stalled = self._start_watchdog(proc, reader)

        extracted, reported = set(), set()
        stream_error = None
        try:
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    if self.cancel.is_set():
                        break
                    # Directories are pre-created. Symlinks, sockets and device
                    # nodes are not worth restoring from a media backup.
                    if not member.isfile():
                        continue
                    if safe_member_path(self.dest_path, member.name) is None:
                        self.progress.file_failed(member.name,
                                                  "unsafe path in archive")
                        reported.add(member.name)
                        continue
                    # Name it before reading, so the status line shows what is
                    # streaming rather than what just finished.
                    self.progress.set_current(member.name)
                    try:
                        archive.extract(member, path=self.dest_path)
                    except (OSError, tarfile.TarError) as exc:
                        self.progress.file_failed(member.name, str(exc))
                        reported.add(member.name)
                        continue
                    remote = by_name.get(member.name)
                    if remote is not None:
                        extracted.add(member.name)
                        self.progress.file_done(remote, stream=id(reader))
        except tarfile.TarError as exc:
            stream_error = str(exc)
        finally:
            reader.close()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            stderr_reader.join(timeout=5)

        if self.cancel.is_set():
            return []

        missing = [remote for name, remote in by_name.items()
                   if name not in extracted]
        if missing:
            stderr_text = b"".join(c for c in stderr_chunks if c).decode(
                "utf-8", "replace").strip()
            if stalled.is_set():
                detail = "stalled, no data for %ds" % int(STALL_TIMEOUT)
            elif stderr_text:
                detail = stderr_text.splitlines()[-1]
            else:
                detail = stream_error or "not present in archive"
            for remote in missing:
                if remote.relpath not in reported:
                    self.progress.file_failed(remote.relpath, detail)
        return missing

    def _start_watchdog(self, proc, reader):
        """A wedged adb or an unplugged cable leaves read() blocked forever."""
        stalled = threading.Event()

        def watch():
            while proc.poll() is None:
                if self.cancel.wait(5.0):
                    return
                if time.time() - reader.last_read > STALL_TIMEOUT:
                    stalled.set()
                    proc.kill()
                    return

        threading.Thread(target=watch, daemon=True).start()
        return stalled

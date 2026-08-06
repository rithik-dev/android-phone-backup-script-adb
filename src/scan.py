"""Enumerating files on the device."""

import shlex

from .config import EXCLUDED_PREFIXES, SCAN_TIMEOUT, SDCARD


class RemoteFile:
    """One file on the device, as reported by find."""

    __slots__ = ("relpath", "size", "mtime")

    def __init__(self, relpath, size, mtime):
        self.relpath = relpath
        self.size = size
        self.mtime = mtime

    @property
    def root(self):
        return self.relpath.split("/", 1)[0]


class ScanResult:
    def __init__(self, files, unparsed, missing_roots):
        self.files = files
        self.unparsed = unparsed
        self.missing_roots = missing_roots

    @property
    def total_bytes(self):
        return sum(f.size for f in self.files)

    def folder_totals(self):
        """Map root folder to [file count, byte count]."""
        totals = {}
        for remote in self.files:
            entry = totals.setdefault(remote.root, [0, 0])
            entry[0] += 1
            entry[1] += remote.size
        return totals


class DeviceScanner:
    def __init__(self, adb):
        self.adb = adb

    def existing_roots(self, requested):
        probe = "; ".join(
            "[ -d %s ] && echo %s"
            % (shlex.quote(SDCARD + "/" + root), shlex.quote(root))
            for root in requested)
        result = self.adb.shell(probe)
        present = [line.strip() for line in result.stdout.splitlines()
                   if line.strip()]
        missing = [root for root in requested if root not in present]
        return present, missing

    def scan(self, roots, missing_roots=()):
        """One find across every root, returning size, mtime and path.

        A single invocation avoids paying the adb round trip per folder, and
        exact totals up front let progress be a real percentage. NUL delimiting
        keeps filenames containing newlines intact.
        """
        targets = " ".join(shlex.quote(SDCARD + "/" + r) for r in roots)
        prune = " ".join("-path %s -prune -o" % shlex.quote(SDCARD + "/" + p)
                         for p in EXCLUDED_PREFIXES)
        command = ("find %s %s -type f -printf '%%s|%%T@|%%p\\0' 2>/dev/null"
                   % (targets, prune))
        result = self.adb.shell(command, timeout=SCAN_TIMEOUT, binary=True)

        prefix = SDCARD + "/"
        files = []
        unparsed = 0
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            try:
                text = record.decode("utf-8", "surrogateescape")
                size, mtime, path = text.split("|", 2)
                remote = RemoteFile(path[len(prefix):], int(size), float(mtime))
            except ValueError:
                unparsed += 1
                continue
            if not path.startswith(prefix):
                unparsed += 1
                continue
            if remote.relpath.startswith(EXCLUDED_PREFIXES):
                continue
            files.append(remote)
        return ScanResult(files, unparsed, list(missing_roots))

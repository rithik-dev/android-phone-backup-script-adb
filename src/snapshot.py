"""Snapshot directories, manifests and the incremental plan."""

import datetime
import errno
import json
import os
import shutil

from .config import (INCOMPLETE_NAME, LOG_NAME, MANIFEST_NAME, MTIME_TOLERANCE,
                     SNAPSHOT_PREFIX)


def mtime_matches(a, b):
    return abs(a - b) < MTIME_TOLERANCE


class Snapshot:
    """One timestamped backup directory."""

    def __init__(self, path):
        self.path = path

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def manifest_path(self):
        return os.path.join(self.path, MANIFEST_NAME)

    @property
    def marker_path(self):
        return os.path.join(self.path, INCOMPLETE_NAME)

    @property
    def log_path(self):
        return os.path.join(self.path, LOG_NAME)

    def local_path(self, relpath):
        return os.path.join(self.path, relpath)

    def create(self):
        os.makedirs(self.path, exist_ok=True)

    def is_incomplete(self):
        return os.path.exists(self.marker_path)

    def has_manifest(self):
        return os.path.exists(self.manifest_path)

    def mark_incomplete(self, token):
        with open(self.marker_path, "w", encoding="utf-8") as handle:
            handle.write(token)

    def mark_complete(self):
        try:
            os.remove(self.marker_path)
        except OSError:
            pass

    def load_manifest(self):
        """Return {relpath: [size, mtime]}, or None if unusable."""
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as handle:
                return json.load(handle).get("files", {})
        except (OSError, ValueError):
            return None

    def write_manifest(self, files, serial):
        payload = {
            "version": 1,
            "device": serial,
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
            "files": {f.relpath: [f.size, f.mtime] for f in files},
        }
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp, self.manifest_path)


class SnapshotStore:
    """The backup root holding every snapshot."""

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))

    def create(self):
        os.makedirs(self.root, exist_ok=True)

    def free_bytes(self):
        return shutil.disk_usage(self.root).free

    def snapshots(self):
        """Newest first, since directory names are timestamps."""
        try:
            names = sorted(os.listdir(self.root), reverse=True)
        except OSError:
            return []
        return [Snapshot(os.path.join(self.root, name)) for name in names
                if os.path.isdir(os.path.join(self.root, name))]

    def latest_complete(self):
        for snapshot in self.snapshots():
            if not snapshot.is_incomplete() and snapshot.has_manifest():
                return snapshot
        return None

    def latest_incomplete(self):
        for snapshot in self.snapshots():
            if snapshot.is_incomplete():
                return snapshot
        return None

    def new_snapshot(self, timestamp):
        """Allocate an unused directory name. Timestamps have one second
        resolution, so two runs in the same second would otherwise collide."""
        path = os.path.join(self.root, SNAPSHOT_PREFIX + timestamp)
        suffix = 1
        while os.path.exists(path):
            suffix += 1
            path = os.path.join(self.root,
                                "%s%s_%d" % (SNAPSHOT_PREFIX, timestamp, suffix))
        return Snapshot(path)


class BackupPlan:
    """What this run must fetch, reuse, or leave alone."""

    def __init__(self, to_fetch, to_link, present):
        self.to_fetch = to_fetch
        self.to_link = to_link
        self.present = present

    @property
    def fetch_bytes(self):
        return sum(f.size for f in self.to_fetch)

    @property
    def link_bytes(self):
        return sum(f.size for f, _ in self.to_link)

    @property
    def present_bytes(self):
        return sum(f.size for f in self.present)

    @property
    def total_files(self):
        return len(self.to_fetch) + len(self.to_link)

    @property
    def total_bytes(self):
        return self.fetch_bytes + self.link_bytes

    @classmethod
    def build(cls, files, dest, previous=None, previous_manifest=None,
              full=False):
        to_fetch, to_link, present = [], [], []
        for remote in files:
            if not full and cls._already_here(dest, remote):
                present.append(remote)
                continue
            source = cls._reusable(previous, previous_manifest, remote)
            if source:
                to_link.append((remote, source))
                continue
            to_fetch.append(remote)
        return cls(to_fetch, to_link, present)

    @staticmethod
    def _already_here(dest, remote):
        """True when a resumed run already has this file on disk."""
        try:
            stat = os.stat(dest.local_path(remote.relpath))
        except OSError:
            return False
        return (stat.st_size == remote.size
                and mtime_matches(stat.st_mtime, remote.mtime))

    @staticmethod
    def _reusable(previous, manifest, remote):
        """Path in the previous snapshot holding identical content."""
        if previous is None or manifest is None:
            return None
        record = manifest.get(remote.relpath)
        if not record or record[0] != remote.size:
            return None
        if not mtime_matches(record[1], remote.mtime):
            return None
        source = previous.local_path(remote.relpath)
        return source if os.path.exists(source) else None


def hardlink_files(to_link, dest, progress):
    """Reuse the previous snapshot's data. Returns files that must be fetched
    instead, for example when the snapshots sit on different filesystems."""
    failed = []
    for remote, source in to_link:
        target = dest.local_path(remote.relpath)
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

"""The per file log written into each snapshot."""

import datetime
import threading

from .config import PROJECT_NAME, PROJECT_URL, VERSION
from .formatting import human_bytes, human_time

# IST has no daylight saving, so a fixed offset is exact and needs no tz data.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")

RULE = "# " + "=" * 68


def now():
    return datetime.datetime.now(IST)


def full_stamp(moment):
    return moment.strftime("%a %d %b %Y, %H:%M:%S IST")


def clock(moment):
    return moment.strftime("%H:%M:%S")


class RunLog:
    """Timestamped record of every file, bracketed by a header and footer.

    Resumed runs append, so one file can hold several attempts in order.
    """

    def __init__(self, path, append=False):
        self.path = path
        self.started = None
        self._lock = threading.Lock()
        self._handle = open(path, "a" if append else "w", encoding="utf-8")

    def start(self, serial, snapshot_name, resumed=False):
        self.started = now()
        self._write("\n".join([
            RULE,
            "# Android backup %s" % ("resumed" if resumed else "started"),
            "# Device    : %s" % serial,
            "# Snapshot  : %s" % snapshot_name,
            "# Started   : %s" % full_stamp(self.started),
            "# Tool      : %s v%s" % (PROJECT_NAME, VERSION),
            "# Source    : %s" % PROJECT_URL,
            RULE,
            "",
        ]) + "\n")

    def record(self, tag, relpath, size):
        self._write("%s  %-5s  %s\t%d\n" % (clock(now()), tag, relpath, size))

    def failure(self, relpath, reason):
        self._write("%s  %-5s  %s\t%s\n" % (clock(now()), "FAIL", relpath, reason))
        self.flush()

    def finish(self, outcome, moved_bytes, linked, copied, failed_files):
        """linked and copied are (file count, byte count) pairs."""
        ended = now()
        elapsed = (ended - self.started).total_seconds() if self.started else 0.0
        rate = moved_bytes / elapsed if elapsed else 0
        self._write("\n" + "\n".join([
            RULE,
            "# Finished   : %s" % full_stamp(ended),
            "# Duration   : %s" % human_time(elapsed),
            "# Result     : %s" % outcome,
            "# Transferred: %s (%s/s average)"
            % (human_bytes(moved_bytes), human_bytes(rate)),
            "# Hardlinked : %d files, %s (no disk space used)"
            % (linked[0], human_bytes(linked[1])),
            "# Copied     : %d files, %s (volume cannot hardlink)"
            % (copied[0], human_bytes(copied[1])),
            "# Failed     : %d files" % failed_files,
            RULE,
            "",
        ]) + "\n")
        self.flush()

    def flush(self):
        with self._lock:
            self._handle.flush()

    def close(self):
        with self._lock:
            self._handle.close()

    def _write(self, text):
        with self._lock:
            self._handle.write(text)

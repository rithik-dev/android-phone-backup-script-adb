"""Tunable constants shared across the package."""

SDCARD = "/sdcard"

# Storage roots to back up, relative to shared storage. Missing ones are skipped.
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
    "Android/media",
]

# Unreadable by the shell user on modern Android, so walking them only adds noise.
EXCLUDED_PREFIXES = ("Android/data", "Android/obb")

# Filenames ride along as tar arguments. Device ARG_MAX is 2 MB, but a single
# argv entry caps near 128 KB, so batches stay well under that.
CHUNK_BYTES = 32 * 1024

# Kill a transfer that produces no bytes for this long.
STALL_TIMEOUT = 120.0

ADB_TIMEOUT = 120.0
SCAN_TIMEOUT = 900.0

# Tar stores whole seconds while find reports nanoseconds, so exact comparison
# would flag every file as changed on the next run.
MTIME_TOLERANCE = 2.0

SNAPSHOT_PREFIX = "Android_Backup_"
MANIFEST_NAME = ".backup-manifest.json"
INCOMPLETE_NAME = ".backup-incomplete"
LOG_NAME = "backup.log"

DEFAULT_JOBS = 2

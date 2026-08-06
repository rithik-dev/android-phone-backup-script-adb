"""Platform specific advice appended to error messages."""

import subprocess
import sys

# Ask tmutil to reclaim up to 200 GB, at the most aggressive urgency.
THIN_COMMAND = "tmutil thinlocalsnapshots / 214748364800 4"


def local_snapshot_count():
    """Time Machine local snapshots on the boot volume. macOS only."""
    if sys.platform != "darwin":
        return 0
    try:
        result = subprocess.run(["tmutil", "listlocalsnapshots", "/"],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return 0
    return sum(1 for line in result.stdout.splitlines()
               if "com.apple.TimeMachine" in line)


def free_space_hint():
    """Explain why Finder can report far more space than is really free.

    Finder counts purgeable space as available, and Time Machine local
    snapshots are usually what holds it. statvfs, which this tool and df both
    use, counts only blocks that are free right now.
    """
    count = local_snapshot_count()
    if not count:
        return ""
    return (
        "\n\nFinder may show far more space than this. Its 'available' figure "
        "counts\npurgeable space, and %d Time Machine local snapshot%s currently "
        "holding\ndata on this disk. Reclaim it with:\n    %s"
        % (count, " is" if count == 1 else "s are", THIN_COMMAND))

"""Human readable rendering of sizes, durations and paths."""


def human_bytes(count):
    if abs(count) < 1024:
        return "%d B" % count
    for unit in ("KB", "MB", "GB", "TB"):
        count /= 1024.0
        if abs(count) < 1024.0 or unit == "TB":
            return "%.1f %s" % (count, unit)


def human_time(seconds):
    if seconds is None or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def elide(text, width):
    """Trim to width, keeping the tail of a path since that is the useful end."""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return "..." + text[-(width - 3):]

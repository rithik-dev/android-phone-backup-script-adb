#!/bin/bash
#
# Double click this file in Finder to run a backup.
#
# To always back up somewhere other than ~/Downloads/Android Backups, put the
# path between the quotes below, for example:
#     DEST="/Volumes/MyDrive/Android Backups"
#
DEST=""

cd "$(dirname "$0")" || exit 1

finish() {
    echo
    read -n 1 -s -r -p "Press any key to close this window..."
    echo
    exit "$1"
}

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 was not found."
    echo "Install it from https://www.python.org/downloads/ or run: xcode-select --install"
    finish 1
fi

if ! command -v adb >/dev/null 2>&1; then
    echo "adb was not found on PATH."
    echo "Install the Android platform tools:"
    echo "    brew install --cask android-platform-tools"
    finish 1
fi

if [ -n "$DEST" ]; then
    python3 index.py --dest "$DEST" "$@"
else
    python3 index.py "$@"
fi
status=$?

echo
case $status in
    0)   echo "Done." ;;
    130) echo "Stopped early. Run this again to pick up where it left off." ;;
    *)   echo "Exited with status $status." ;;
esac
finish $status

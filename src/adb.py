"""ADB process handling and device selection."""

import shutil
import subprocess

from .config import ADB_TIMEOUT
from .errors import BackupError


class Adb:
    """Runs adb commands, pinned to a single device serial."""

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
                "adb %s timed out after %ss. The device may be unplugged, "
                "locked or asleep." % (" ".join(args[:2]), int(timeout)))

    def shell(self, command, timeout=ADB_TIMEOUT, binary=False):
        return self.run(["shell", command], timeout=timeout, binary=binary)

    def stream(self, command):
        """Start a command whose stdout is binary. exec-out is used rather than
        shell because shell can translate line endings and corrupt tar data."""
        return subprocess.Popen(
            self._argv(["exec-out", command]),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def require_adb():
    if not shutil.which("adb"):
        raise BackupError(
            "'adb' was not found on PATH.\n"
            "Install the Android platform tools, for example:\n"
            "  brew install --cask android-platform-tools")


def list_devices():
    """Return (serial, state) pairs. Only tab separated rows are devices, which
    keeps adb daemon startup messages out of the result."""
    try:
        result = subprocess.run(["adb", "devices"], capture_output=True,
                                text=True, timeout=ADB_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise BackupError("'adb devices' timed out. Try: adb kill-server")

    devices = []
    for line in result.stdout.splitlines():
        if "\t" not in line:
            continue
        serial, _, state = line.rstrip().partition("\t")
        devices.append((serial.strip(), state.strip()))
    return devices


def select_device(requested_serial=None):
    """Resolve exactly one usable device, or explain why that is not possible."""
    devices = list_devices()

    if requested_serial:
        for serial, state in devices:
            if serial == requested_serial:
                if state != "device":
                    raise BackupError("Device [%s] is in state '%s', expected "
                                      "'device'." % (serial, state))
                return serial
        raise BackupError(
            "Device [%s] is not connected. Currently attached: %s"
            % (requested_serial, ", ".join(s for s, _ in devices) or "none"))

    if not devices:
        raise BackupError(
            "No devices found.\n"
            "Connect the phone, enable USB debugging, and set the USB mode to "
            "file transfer.")

    usable = [serial for serial, state in devices if state == "device"]
    unauthorized = [serial for serial, state in devices if state == "unauthorized"]

    if not usable and unauthorized:
        raise BackupError(
            "Device [%s] is unauthorized.\n"
            "Unlock the phone and tap 'Allow' on the USB debugging prompt."
            % unauthorized[0])

    if not usable:
        raise BackupError(
            "No usable device. States: %s"
            % ", ".join("%s=%s" % (s, st) for s, st in devices))

    if len(usable) > 1:
        raise BackupError(
            "More than one device is connected:\n%s\nRe-run with --serial SERIAL."
            % "\n".join("  - %s" % s for s in usable))

    return usable[0]

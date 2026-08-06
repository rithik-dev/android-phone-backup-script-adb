# Android Phone Backup

Fast incremental backup of an Android phone's media folders to a Mac or Linux
machine over ADB.

Files are streamed off the device as a single tar archive per batch and
extracted on the fly. Nothing is ever written to the phone, so a backup needs no
free space on the device, and the per file ADB round trip that makes `adb pull`
slow on large folders is paid once per batch instead of once per file.

Repeat runs only transfer what changed. Everything else is hardlinked from the
previous snapshot, so each backup is a complete browsable tree that costs almost
no extra disk space.

## Requirements

- Python 3.8 or newer (macOS ships Python 3.9, which is enough)
- Android platform tools on PATH: `brew install --cask android-platform-tools`
- USB debugging enabled on the phone, with the USB mode set to file transfer

## Quick start

Double click **`Backup Phone.command`** in Finder. It opens a Terminal window,
runs the backup, and waits for a keypress before closing so you can read the
summary.

To send backups somewhere other than the default, open that file in any text
editor and set the destination near the top:

```bash
DEST="/Volumes/MyDrive/Android Backups"
```

From a terminal:

```bash
python3 index.py
```

From VS Code, pick one of the configurations in the Run and Debug panel:

| Configuration | Runs |
| --- | --- |
| Backup: all folders (incremental) | the normal backup, no flags |
| Backup: force re-transfer of everything | `--full` |
| Backup: dry run | `--dry-run` |
| Backup: camera only, verbose | `--only DCIM --verbose` |
| Backup: single stream (slow link) | `--jobs 1` |
| Backup: prompt for destination | asks, then `--dest` |
| Backup: prompt for folders | asks, then `--only` |

Incremental reuse is the default everywhere, including the first entry, which
passes no flags at all. Only the `--full` configuration turns it off.

They all use the integrated terminal, which the live progress bar needs, and
require the Python extension. If your VS Code predates the debugpy adapter,
change `"type": "debugpy"` to `"type": "python"` in `.vscode/launch.json`.

Stopping a run with the red stop button kills the process outright rather than
interrupting it, so no footer is written to the log. That is still safe: the
snapshot stays marked incomplete and the next run resumes into it. Press Ctrl+C
in the terminal instead for a clean stop with a summary.

Before a first big run, see what would happen without transferring anything:

```bash
python3 index.py --dry-run
```

## Where backups are stored

By default:

```
~/Documents/Android Backups/
    Android_Backup_20260806_094547/
        DCIM/Camera/20250726_172955.jpg
        Android/media/com.whatsapp/...
        backup.log
        .backup-manifest.json
    Android_Backup_20260807_083012/
        ...
```

Each run creates its own timestamped snapshot. The tree inside mirrors the
phone's `/sdcard` exactly, so `/sdcard/DCIM/Camera/x.jpg` lands at
`DCIM/Camera/x.jpg` and `/sdcard/Android/media/...` keeps its full path.

Change the destination with `--dest`, or set `ANDROID_BACKUP_DEST` in your
environment to change the default permanently:

```bash
python3 index.py --dest "/Volumes/MyDrive/Android Backups"
```

### How the default is found on each system

There is no single path that means "documents" everywhere, so it is resolved at
run time:

| System | How it is found |
| --- | --- |
| macOS | `~/Documents` |
| Windows | The `Personal` entry in the registry under `Explorer\User Shell Folders`, because Windows lets this folder be relocated and OneDrive frequently does move it. Falls back to `%USERPROFILE%\Documents`. |
| Linux and BSD | `$XDG_DOCUMENTS_DIR`, then `XDG_DOCUMENTS_DIR` in `~/.config/user-dirs.dirs`. This is what carries localized names, so a German desktop correctly resolves to `~/Dokumente`. Falls back to `~/Documents`. |

If the resolved folder does not exist, which is common on minimal Linux installs
and on systems with no desktop environment, the backup root becomes
`~/Android Backups` instead. A documents folder is never created just to hold
backups. Run `python3 index.py --help` to see the path your system resolves to.

### A warning about synced folders

Documents is also the folder most likely to be synced to the cloud: iCloud Drive
on macOS when Desktop and Documents syncing is on, and OneDrive on Windows,
which redirects it by default on many machines. Backing up a phone into a synced
folder means every photo gets uploaded, which is slow, and on a large library
will exhaust the cloud quota.

The run checks for this and says so before transferring anything:

```
Destination : /Users/you/Library/Mobile Documents/.../Android Backups/Android_Backup_...
Warning     : this destination is inside iCloud Drive, which will try to upload
              every backed up file. Pass --dest to use a local folder or an
              external drive instead.
```

iCloud Drive, OneDrive, Dropbox and Google Drive are all recognised. If you see
that warning, point `--dest` at a local folder or an external drive.

Two files are written into every snapshot:

- `backup.log`, a timestamped record of every file
- `.backup-manifest.json`, each file's size and modification time, which is what
  the next run compares against

The log is bracketed by a header and footer in IST, and every line carries the
time it happened:

```
# ====================================================================
# Android backup started
# Device    : R5CT21KF3TD
# Snapshot  : Android_Backup_20260806_104249
# Started   : Thu 06 Aug 2026, 10:42:49 IST
# Tool      : android-phone-backup-script-adb v1.0.0
# Source    : https://github.com/rithik-dev/android-phone-backup-script-adb
# ====================================================================

10:42:49  FETCH  Documents/Aadhar.pdf	1130377
10:42:50  LINK   Ringtones/Bella Ciao Ringtone.mp3	979244

# ====================================================================
# Finished   : Thu 06 Aug 2026, 10:42:50 IST
# Duration   : 00:01
# Result     : complete
# Transferred: 15.6 MB (12.4 MB/s average)
# Hardlinked : 0 files, 0 B (no disk space used)
# Copied     : 0 files, 0 B (volume cannot hardlink)
# Failed     : 0 files
# ====================================================================
```

The tag on each line tells you what actually happened:

| Tag | Meaning |
| --- | --- |
| `FETCH` | Transferred from the device |
| `LINK` | Hardlinked from the previous snapshot: no transfer, no disk space |
| `COPY` | Copied from the previous snapshot: no transfer, but disk space used, because the destination volume cannot hardlink |
| `FAIL` | Did not arrive, with the reason |

A resumed run appends rather than overwriting, so a single log can hold several
attempts in order, each with its own header and footer. Interrupted attempts get
a footer too, recorded as `interrupted, snapshot incomplete`.

## Options

| Option | Description |
| --- | --- |
| `--dest PATH` | Backup root. Each run creates a timestamped snapshot inside it. Defaults to `$ANDROID_BACKUP_DEST`, otherwise the `Android Backups` folder in your documents directory. |
| `--serial SERIAL` | Target a specific device. Required when more than one device is attached. Find serials with `adb devices`. |
| `--jobs N` | Concurrent transfer streams, default 2. USB is usually the bottleneck, so higher values rarely help. |
| `--only A,B` | Comma separated folders to back up, for example `--only DCIM,Pictures`. Accepts nested paths such as `Android/media`. |
| `--full` | Re-transfer everything instead of reusing the previous snapshot. Also starts a new snapshot rather than resuming. |
| `--no-resume` | Start a new snapshot instead of resuming an interrupted one. |
| `--dry-run` | Scan and report what would transfer, then exit without writing anything. |
| `--verbose` | Echo every transferred file to the terminal. Files are always recorded in `backup.log` regardless. |
| `--no-progress` | Disable the live status line, for piping output to a file. |

## What gets backed up

These folders, when they exist on the device:

`DCIM`, `Pictures`, `Movies`, `Music`, `Documents`, `Download`, `Recordings`,
`Alarms`, `Notifications`, `Ringtones`, `Podcasts`, `Audiobooks`,
`Android/media`

`Android/media` is where messaging apps keep attachments, so WhatsApp, Signal
and Telegram media are included. Folders that do not exist are listed as
"Not present" and skipped without failing the run.

`Android/data` and `Android/obb` are deliberately excluded. The ADB shell user
cannot read them on modern Android, so walking them would only produce errors.

This is a media backup. It does not include SMS, call logs, contacts or app
APKs. Those need separate tooling, since `adb backup` is deprecated and disabled
on Android 12 and newer.

To change the folder list permanently, edit `DEFAULT_ROOTS` in
`src/config.py`.

## How incremental backups work

The first run transfers everything. Every run after that:

1. Scans the device once with a single `find`, collecting each file's path, size
   and modification time.
2. Loads `.backup-manifest.json` from the most recent completed snapshot.
3. For each file, compares size and modification time against that manifest.
   - Unchanged: hardlinked from the previous snapshot, no transfer.
   - New or changed: added to the transfer list.
4. Transfers only the files on that list.
5. Writes a fresh manifest describing the complete snapshot.

**Every snapshot contains every file.** This is deliberate, and it is what you
should expect to see. Two backups taken minutes apart will look identical in
Finder, because each one is a full, independent tree. You can browse or copy any
one of them without needing the others, and deleting an old snapshot never
damages a newer one, since the data survives as long as any snapshot references
it.

What changes between runs is how much was *transferred*, not how much is
present. Because unchanged files are hardlinked rather than copied, a snapshot
containing 80 GB of photos can cost only a few megabytes of new disk space if
little has changed.

To confirm a run reused rather than re-downloaded, check any of these:

```bash
cd ~/Documents/Android\ Backups

# the run summary said so
grep -E "Transferred|Hardlinked|Copied" */backup.log

# every line tagged LINK is hardlinked, COPY is copied, FETCH came off the phone
grep -c LINK  Android_Backup_*/backup.log

# apparent size versus real size on disk
du -Ash .    # counts every snapshot separately
du -sh  .    # what the snapshots actually occupy together
```

If the second number is close to the first divided by the number of snapshots,
hardlinking is working.

## How hardlinks work

This is the part that surprises people, so it is worth being precise.

A file name and a file's contents are two different things. The contents live
somewhere on disk, and a name is just a pointer to them. A **hardlink** is a
second name pointing at the same contents. Not a copy, not a shortcut, not an
alias. There is exactly one copy of the bytes, reachable through two names, and
neither name is more "real" than the other.

So when the second backup reuses a photo:

```
Android_Backup_...094547/DCIM/Camera/x.jpg  ─┐
                                             ├─►  the actual 3 MB of photo
Android_Backup_...104650/DCIM/Camera/x.jpg  ─┘
```

One 3 MB allocation, two names. This is why:

- **Every snapshot looks complete in Finder**, because it is complete. Each
  timestamped folder contains every file that was on the phone at that moment.
- **Ten snapshots of an 80 GB phone do not use 800 GB.** They use 80 GB plus
  whatever actually changed. The photos you never touched exist once.
- **Deleting an old snapshot is safe and frees almost nothing.** Removing a name
  only frees the contents when the last name pointing at them is gone. Delete
  the oldest backup and the newer ones are untouched, because they hold their
  own names to the same data.
- **You can restore from any single snapshot** without needing the others.

The one thing to know: because the names share contents, **editing a file inside
one snapshot edits it in all of them.** Deleting is fine, renaming is fine,
editing in place is not. Treat snapshots as read only, which is what you want
from a backup anyway.

To see it yourself, the link count is how many names point at the contents:

```bash
stat -f "links=%l  %N" ~/Documents/Android\ Backups/*/DCIM/Camera/*.jpg | head
```

`links=1` means one name, `links=3` means three snapshots share that file.

### When hardlinks are not possible

Hardlinks are a filesystem feature. APFS and HFS+ (your Mac's internal disk)
support them. **exFAT does not**, and exFAT is the common format for portable
drives, so this matters when backing up to an external disk.

When linking fails, the file is copied from the previous snapshot instead. That
still avoids re-downloading it over USB, which is the slow part, but the space
is genuinely used again, so ten snapshots really would cost ten times the space.
The run tells you which happened:

```
  Hardlinked       : 10661 files, 78.9 GB (no transfer, no disk used)
  Copied           : 0 files, 0 B
```

and the log tags each line `LINK` or `COPY`. If you see large `Copied` numbers,
your destination drive is probably exFAT. Reformatting it as APFS or Mac OS
Extended restores hardlinking, at the cost of the drive no longer being readable
on Windows.

### Copying snapshots elsewhere

A plain `cp -R` of the backup root expands every hardlink into a real copy, so
80 GB of snapshots can balloon into hundreds of gigabytes. To move them while
keeping the sharing intact:

```bash
rsync -aH ~/Documents/Android\ Backups/ /Volumes/Other/Android\ Backups/
```

The `-H` is what preserves hardlinks. Without it you get the expansion.

Modification times are compared with a two second tolerance. Tar stores whole
seconds while `find` reports nanoseconds, so an exact comparison would flag every
file as changed on the second run.

If the previous manifest is missing or corrupt, the run falls back to a full
transfer and says so.

## If the device disconnects or the run stops

The snapshot is marked incomplete the moment it is created, and only marked
complete after every file has arrived. That marker drives recovery.

**Press Ctrl+C.** The current batch stops, the summary still prints, and the
snapshot stays marked incomplete. Exit status is 130.

**Unplug the cable, or the phone sleeps or locks.** Reads stop producing data.
A watchdog kills the stalled transfer after 120 seconds so the run cannot hang
forever. The affected files are reported as failures and the snapshot stays
incomplete.

**Either way, run the same command again.** It finds the incomplete snapshot,
resumes into it rather than starting a new one, skips every file already on
disk, and transfers only what is missing. Nothing is re-downloaded.

Short control commands have their own timeout, so a wedged ADB server produces a
clear error instead of a hang. If that happens, `adb kill-server` clears it.

Use `--no-resume` if you would rather abandon an incomplete snapshot and start
fresh.

## Edge cases handled

- **Free space is checked on both ends before starting.** The transfer is
  streamed, so the phone needs no free space at all. The destination volume is
  checked against the exact byte count from the scan, and the run refuses to
  start rather than filling the disk.
- **Filenames with spaces, quotes, ampersands or newlines.** Every remote path
  is shell quoted, and the scan is NUL delimited rather than newline delimited.
- **Two runs started in the same second.** Snapshot names carry a numeric
  suffix when needed, so runs cannot silently share a directory.
- **More than one device attached.** The run stops and lists the serials instead
  of guessing. Every ADB command is pinned to the chosen serial, so a device
  connected midway through cannot affect a run in progress.
- **Unauthorized device.** Detected before any transfer, with instructions to
  accept the prompt on the phone.
- **Archive safety.** The archive is built by the device, so every member is
  checked for absolute paths and directory traversal before extraction. Python
  3.9 has no built in tarfile extraction filter, so this is done explicitly.
- **Symlinks, sockets and device nodes** in the archive are skipped.
- **Concurrent extraction.** Destination directories are created once up front,
  because tarfile creates parent directories with a check then create sequence
  that races when several batches extract at the same time.
- **Snapshots on a different filesystem from the previous one.** Hardlinking
  falls back to a copy, and then to a normal transfer if that also fails.
- **Partial failures.** Files that fail are listed individually, the snapshot
  stays incomplete, and the next run retries exactly those files.
- **Unreadable scan entries** are counted and reported rather than silently
  dropped.
- **Output redirected to a file.** The live status line is disabled
  automatically when stdout is not a terminal.

## Progress output

```
Device      : R5CT21KF3TD
Scan        : 10875 files, 82.1 GB in 00:05
Not present : Alarms, Notifications, Podcasts, Audiobooks
Previous    : Android_Backup_20260805_211540
Destination : /Volumes/MyDrive/Android Backups/Android_Backup_20260806_094547
To transfer : 214 files, 3.2 GB
Reusing     : 10661 files, 78.9 GB (hardlinked from previous snapshot)
Free space  : 412.8 GB
Streams     : 2

  Documents          done  6 files  9.6 MB  (00:02 elapsed)
  ####################----   84.0%  2.7 GB / 3.2 GB  38.6 MB/s  01:11 elapsed  ETA 00:13  [198/214]  ...Camera/20250726_172955.jpg
```

The percentage tracks bytes rather than file count, since a library with 3 GB
videos next to 30 KB thumbnails makes a file count meaningless. It also advances
while a single large file is still streaming, so the bar does not appear frozen
during a big video. Each folder prints a line as it completes, and a per folder
table is printed at the end.

## Performance notes

Measured on a Galaxy S22 Ultra over USB 2.0, transferring 1259 small files
totalling 39.4 MB:

| Method | Time |
| --- | --- |
| `adb pull -a` | 6.34s |
| tar on the device, then pull the archive | 3.13s |
| streamed tar, used here | 2.97s |

Writing a tar on the phone first is not only slower, it also requires free space
on the device equal to the folder being backed up, which fails outright on a
nearly full phone.

**The cable usually matters more than any of this.** Sustained throughput on
that phone measured 39.1 MB/s, which is USB 2.0 speed. Many phones ship with a
USB 2.0 cable in the box. On a USB 3 cable the same hardware runs several times
faster. If a large backup feels slow, check the cable before anything else. The
average throughput printed in the summary tells you what you are getting.

`--jobs` has a modest effect on folders full of small files and almost none on
large videos, where the USB link is already saturated. The default of 2 is a
reasonable compromise.

## Project layout

```
index.py               entry point, run this
Backup Phone.command   double clickable launcher for Finder
src/
    cli.py             argument parsing
    runner.py          orchestrates one backup run
    adb.py             ADB process handling and device selection
    scan.py            enumerating files on the device
    snapshot.py        snapshot directories, manifests, incremental plan
    transfer.py        streaming tar transfer and extraction
    progress.py        live progress bar and logging
    formatting.py      size and duration rendering
    paths.py           where the default backup folder lives, per platform
    platform_hints.py  cloud sync and free space advice
    config.py          folder list, timeouts, tunables
    errors.py          error type
```

## Exit statuses

| Status | Meaning |
| --- | --- |
| 0 | Backup completed, snapshot marked complete |
| 1 | Some files failed, or a fatal error such as insufficient space |
| 130 | Interrupted with Ctrl+C, re-run to resume |

## Troubleshooting

**"adb was not found on PATH."** Install the platform tools with
`brew install --cask android-platform-tools`.

**"Device is unauthorized."** Unlock the phone and tap Allow on the USB
debugging prompt. Tick "always allow" to avoid repeating it.

**"No devices found."** Check the cable, enable USB debugging in developer
options, and set the USB mode to file transfer rather than charging only.

**"More than one device is connected."** Run `adb devices` and pass the serial
you want with `--serial`.

**"Not enough free space."** The message shows exactly how much is needed
against how much is free. Point `--dest` at a larger volume.

On macOS the reported figure is often far smaller than what Finder shows, and
the error explains why when that applies. Finder counts purgeable space as
available, mostly Time Machine local snapshots holding data on the internal
disk. This tool uses `statvfs`, the same measure as `df`, which counts only
blocks that are free right now. macOS does purge snapshots under pressure, but
it does so on demand and with a lag, so a long sustained write can outrun it and
fail partway rather than being refused up front. To reclaim that space
deliberately, which deletes local snapshots but leaves backups on an external
Time Machine drive untouched:

```bash
tmutil thinlocalsnapshots / 214748364800 4
df -h /System/Volumes/Data
```

**The transfer stalls repeatedly.** Keep the phone unlocked and awake during a
large backup, and try a different USB port or cable.

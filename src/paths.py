"""Where backups go by default, resolved per platform.

Every desktop OS has a documents folder, but none of them agree on where it is.
Windows lets it be moved, often onto OneDrive. Linux localizes the name, so it
may be Dokumente or Documentos. Only macOS is reliably ~/Documents.
"""

import os
import sys

BACKUP_FOLDER_NAME = "Android Backups"


def _windows_documents():
    """Ask the registry, since Windows lets this folder be relocated."""
    try:
        import winreg
    except ImportError:
        return None

    keys = [
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
    ]
    for key in keys:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Personal")
        except OSError:
            continue
        # User Shell Folders stores paths unexpanded, as %USERPROFILE%\Documents.
        expanded = os.path.expandvars(value).strip()
        if expanded and "%" not in expanded:
            return expanded
    return None


def _xdg_documents():
    """Follow the XDG spec, which is what carries localized folder names."""
    from_env = os.environ.get("XDG_DOCUMENTS_DIR")
    if from_env:
        return os.path.expandvars(from_env)

    config_home = (os.environ.get("XDG_CONFIG_HOME")
                   or os.path.join(os.path.expanduser("~"), ".config"))
    try:
        with open(os.path.join(config_home, "user-dirs.dirs"), "r",
                  encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("#") or not line.startswith("XDG_DOCUMENTS_DIR"):
                    continue
                _, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if value:
                    return os.path.expandvars(value)
    except OSError:
        pass
    return None


def documents_dir():
    """The user's documents folder, or their home directory if there is none."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        candidate = _windows_documents()
    elif sys.platform == "darwin":
        candidate = os.path.join(home, "Documents")
    else:
        candidate = _xdg_documents()

    candidate = candidate or os.path.join(home, "Documents")
    if os.path.isdir(candidate):
        return candidate
    # Rather than create a folder this system does not use, fall back to home.
    return home


def default_backup_root():
    return os.path.join(documents_dir(), BACKUP_FOLDER_NAME)

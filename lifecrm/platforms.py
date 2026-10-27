"""Where things live on each supported machine.

Two combinations are supported end to end: a Windows laptop with an Android
(Samsung) phone, and a MacBook with an iPhone. Code that needs a default
location — a cloud-drive folder, the Messages database, an iPhone backup —
asks here instead of hard-coding one OS's layout. Every function only
*proposes* paths; callers check existence and config.toml always wins.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SYSTEM = ("mac" if sys.platform == "darwin"
          else "windows" if sys.platform.startswith("win")
          else "linux")


def is_mac() -> bool:
    return SYSTEM == "mac"


def is_windows() -> bool:
    return SYSTEM == "windows"


def home() -> Path:
    return Path.home()


def venv_python(root: Path) -> Path:
    """The project venv's interpreter: Scripts\\python.exe on Windows,
    bin/python everywhere else."""
    if is_windows():
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def local_appdata() -> Path:
    """Windows' %LOCALAPPDATA%, when it lies under this home — a test's fake
    home is honored, and so is the real variable on a real Windows — else
    <home>/AppData/Local, where Windows keeps it by default."""
    h = home()
    v = os.environ.get("LOCALAPPDATA")
    if v:
        p = Path(v)
        if os.path.normcase(str(p)).startswith(os.path.normcase(str(h))):
            return p
    return h / "AppData" / "Local"


# ---------------------------------------------------------------- Phone Link (Windows)

PHONE_LINK_PACKAGE = "Microsoft.YourPhone_8wekyb3d8bbwe"


def phone_link_root() -> Path:
    """Where Phone Link keeps one cache folder per linked phone (a GUID each,
    with System/Database/*.db inside): Indexed/ under its package's
    LocalCache. Proposed only; the connector checks what is there."""
    return (local_appdata() / "Packages" / PHONE_LINK_PACKAGE / "LocalCache"
            / "Indexed")


# ---------------------------------------------------------------- cloud drives

def icloud_drive() -> Path:
    """iCloud Drive's local folder on a Mac (what Finder shows as iCloud Drive)."""
    return home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


def onedrive_candidates() -> list[Path]:
    """Plausible OneDrive roots, most specific first.

    Windows sets OneDrive / OneDriveConsumer in the environment. On a Mac the
    current client syncs under ~/Library/CloudStorage/OneDrive-<account>; old
    clients used ~/OneDrive.
    """
    out: list[Path] = []
    for var in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
        v = os.environ.get(var)
        if v:
            out.append(Path(v))
    cloud = home() / "Library" / "CloudStorage"
    if cloud.is_dir():
        out.extend(sorted(p for p in cloud.iterdir() if p.name.startswith("OneDrive")))
    out.append(home() / "OneDrive")
    seen: set[str] = set()
    uniq = []
    for p in out:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def default_journal_root() -> Path:
    """Where a fresh install puts the VoiceJournal folder.

    Mac + iPhone: iCloud Drive — Shortcuts can save to a fixed iCloud Drive
    folder without a picker, which OneDrive cannot promise. Windows + Android:
    OneDrive, which Windows ships with and the Android sync apps target.
    """
    if is_mac():
        return icloud_drive() / "VoiceJournal"
    for root in onedrive_candidates():
        if root.is_dir():
            return root / "VoiceJournal"
    return onedrive_candidates()[0] / "VoiceJournal"


def is_icloud_placeholder(path: Path) -> bool:
    """iCloud's 'Optimize Mac Storage' replaces an evicted file with a hidden
    stub named `.<name>.icloud`; the real bytes are not on disk."""
    return path.name.startswith(".") and path.name.endswith(".icloud")


def icloud_placeholder_target(path: Path) -> str:
    """The real file name an iCloud stub stands for ('.a.m4a.icloud' -> 'a.m4a')."""
    return path.name[1:-len(".icloud")] if is_icloud_placeholder(path) else path.name


# ---------------------------------------------------------------- Apple data

def mobilesync_backup_dirs() -> list[Path]:
    """Folders Apple's tools write iPhone backups to, in search order."""
    out = []
    if is_windows():
        out.append(home() / "Apple" / "MobileSync" / "Backup")          # Apple Devices
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(Path(appdata) / "Apple Computer" / "MobileSync" / "Backup")
    out.append(home() / "Library" / "Application Support" / "MobileSync" / "Backup")
    return out


def mac_messages_db() -> Path:
    """The Mac's own Messages database (needs Full Disk Access to read)."""
    return home() / "Library" / "Messages" / "chat.db"


def mac_addressbook_dbs() -> list[Path]:
    """Every Contacts store on a Mac: one per account under Sources/, plus the
    local 'On My Mac' store at the top level."""
    base = home() / "Library" / "Application Support" / "AddressBook"
    out = []
    top = base / "AddressBook-v22.abcddb"
    if top.exists():
        out.append(top)
    sources = base / "Sources"
    if sources.is_dir():
        out.extend(sorted(sources.glob("*/AddressBook-v22.abcddb")))
    return out


def mac_photos_db() -> Path:
    return (home() / "Pictures" / "Photos Library.photoslibrary"
            / "database" / "Photos.sqlite")


def mac_notes_db() -> Path:
    return (home() / "Library" / "Group Containers" / "group.com.apple.notes"
            / "NoteStore.sqlite")

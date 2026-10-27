"""`lifecrm where`: what this machine holds that Life CRM can read.

Each connector knows where its own data lives, but someone setting up a Mac
or a Windows laptop needs one answer to "what is here, and what do I run for
it". This walks the platform's known places (platforms.py), the configured
journal folders and the usual places an export gets saved to, and prints
each with whether it exists and the command that reads it.

It only looks. Nothing is opened — not a database, not a zip (exports are
recognised by the names their apps give them) — nothing is imported or
written, no iCloud download is requested (intake does that) — and a place
macOS guards (Messages, Mail, iPhone backups, Contacts) is reported as 'no
access' with the Full Disk Access fix instead of a traceback.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

import typer

from .. import platforms
from ..config import PROJECT_ROOT, ConfigError, load_config

FULL_DISK_ACCESS = ("grant Full Disk Access to your terminal: System Settings › "
                    "Privacy & Security › Full Disk Access")

# Exports people save by hand, by the names the apps give them. Matched
# case-insensitively against the top level of each drop folder, by NAME only:
# nothing is opened (a zip's contents are the connector's to read, and
# opening a file iCloud has evicted would download it). A pattern ending in
# '/' matches a folder only. The first kind that matches a name wins.
EXPORTS = (
    ("health", "Apple Health", ("export.zip", "export *.zip", "export(*).zip",
                                "export (*).zip", "apple_health_export")),
    ("health", "Health Connect", ("health connect*.zip", "health_connect*.zip",
                                  "healthconnect*.zip")),
    ("sms", "SMS Backup & Restore", ("sms-*.xml", "calls-*.xml")),
    ("takeout", "Google Takeout", ("takeout-*.zip", "takeout")),
    ("whatsapp", "WhatsApp chat export", ("whatsapp chat*.zip", "whatsapp chat*.txt")),
    ("calendar", "calendar export", ("*.ics",)),
    ("email", "mailbox", ("*.mbox",)),
    ("meta", "Instagram / Facebook export",
     ("instagram-*.zip", "instagram_*.zip", "facebook-*.zip", "facebook_*.zip",
      "messenger-*.zip", "meta-*.zip", "instagram-*/", "facebook-*/",
      "your_instagram_activity/", "your_facebook_activity/",
      "your_activity_across_facebook/")),
    ("telegram", "Telegram Desktop export",
     ("telegram desktop/", "chatexport_*", "dataexport_*")),
    ("fitness", "Strong / Hevy / Strava export",
     ("strong*.csv", "workouts*.csv", "export_*.zip", "export_*/")),
    ("spotify", "Spotify download",
     ("my_spotify_data*.zip", "my_spotify_data*/", "spotify*/")),
    ("reading", "Kindle clippings", ("my clippings*.txt",)),
    ("reading", "Goodreads export", ("goodreads_library_export*.csv",)),
    ("nutrition", "Cronometer export", ("servings*.csv", "dailysummary*.csv")),
    ("nutrition", "MyFitnessPal export", ("nutrition-summary-*.csv",)),
    ("linkedin", "LinkedIn export", ("*linkedindataexport*",)),
    ("payments", "Venmo statement", ("venmo*.csv",)),
    ("payments", "PayPal activity report", ("download.csv", "download (*).csv",
                                            "paypal*.csv")),
    ("rides", "Uber data export", ("uber data*",)),
)
MAX_LISTED = 8

# The connectors that each read their own part of one Google Takeout. Not
# browser: it reads a browser's own SQLite History, and Takeout's Chrome
# history is a JSON file it refuses.
TAKEOUT_CONNECTORS = ("calendar", "email", "google_activity", "location", "notes",
                      "photos")

# Per kind: the group's title, a stand-in path for the command when nothing
# was found, and the command(s) that read one export. Listed in report order.
EXPORT_GROUPS = {
    "health": ("Health exports (Apple Health, Health Connect)", "<export.zip>",
               ("health import",)),
    "sms": ("SMS Backup & Restore (Android texts and calls)", "<sms-....xml>",
            ("observe android_sms",)),
    "takeout": ("Google Takeout", "<takeout-....zip>",
                tuple(f"observe {c}" for c in TAKEOUT_CONNECTORS)),
    "whatsapp": ("WhatsApp chat exports", "<WhatsApp Chat ....zip>",
                 ("observe whatsapp",)),
    "calendar": ("Calendar exports (.ics)", "<calendar.ics>", ("observe calendar",)),
    "email": ("Mail archives (.mbox)", "<mailbox.mbox>", ("observe email",)),
    "meta": ("Instagram and Facebook Messenger exports", "<instagram-....zip>",
             ("observe meta_messages",)),
    "telegram": ("Telegram Desktop exports", "<ChatExport_...>", ("observe telegram",)),
    "fitness": ("Workout exports (Strong, Hevy, Strava)", "<strong.csv>",
                ("observe fitness",)),
    "spotify": ("Spotify listening history", "<my_spotify_data.zip>",
                ("observe spotify",)),
    "reading": ("Reading (Kindle highlights, Goodreads)", "<My Clippings.txt>",
                ("observe reading",)),
    "nutrition": ("Food diary exports (Cronometer, MyFitnessPal)", "<servings.csv>",
                  ("observe nutrition",)),
    "linkedin": ("LinkedIn data export", "<Basic_LinkedInDataExport_....zip>",
                 ("observe linkedin",)),
    "payments": ("Venmo and PayPal statements", "<VenmoStatement_....csv>",
                 ("observe payments",)),
    "rides": ("Uber data export (rides and Eats)", "<Uber Data.zip>",
              ("observe rides",)),
}


def _app_subfolders() -> tuple[Path, ...]:
    """Where the Android backup app uploads inside a cloud drive — the very
    list `observe android_sms` searches (Apps\\SMS Backup & Restore first),
    so `where` never reports 'none' for a backup that command would read."""
    try:
        from ..connectors.android_sms import ONEDRIVE_FOLDERS
    except Exception:   # one broken connector module must not hide the report
        return (Path("SMSBackupRestore"), Path("SMS Backup & Restore"))
    return ONEDRIVE_FOLDERS


# ---------------------------------------------------------------- probing

def _state(path: Path) -> str:
    try:
        return "found" if path.exists() else "missing"
    except PermissionError:
        return "no access"
    except OSError:
        return "missing"


def _children(folder: Path) -> list[Path] | None:
    """A folder's entries, [] when it is absent, None when the OS refuses."""
    try:
        return sorted(folder.iterdir()) if folder.is_dir() else []
    except PermissionError:
        return None
    except OSError:
        return []


def _is_file(path: Path) -> bool:
    # Path.is_file re-raises EPERM; a guarded entry is simply not counted.
    try:
        return path.is_file()
    except OSError:
        return False


def _item(label: str, path: Path, detail: str = "") -> dict:
    state = _state(path)
    if state == "no access" and not detail:
        detail = FULL_DISK_ACCESS if platforms.is_mac() else "permission denied"
    return {"label": label, "path": str(path), "state": state, "detail": detail}


def _inbox_detail(folder: Path, exts: set[str]) -> str:
    from ..transcribe import icloud_stub
    entries = _children(folder)
    if entries is None:
        return FULL_DISK_ACCESS if platforms.is_mac() else "permission denied"
    waiting = sum(1 for p in entries if not p.name.startswith(".")
                  and p.suffix.lower() in exts and _is_file(p))
    in_cloud = sum(1 for p in entries if icloud_stub(p, exts))
    parts = [f"{waiting} waiting"] if waiting else []
    if in_cloud:
        parts.append(f"{in_cloud} still in iCloud (intake asks for them)")
    return ", ".join(parts)


# ---------------------------------------------------------------- the survey

def _journal_group(root: Path) -> tuple[dict, Path | None]:
    from ..transcribe import AUDIO_EXTS, TEXT_EXTS
    cfg_file = root / "config.toml"
    items, note, commands = [], "", ["python -m lifecrm intake"]
    cfg = None
    if cfg_file.exists():
        try:
            cfg = load_config(root)
        except ConfigError as e:
            note = str(e)
    if cfg is not None:
        for label, path, exts in (("inbox_dir", cfg.inbox_dir, AUDIO_EXTS),
                                  ("inbox_text_dir", cfg.inbox_text_dir, TEXT_EXTS),
                                  ("archive_dir", cfg.archive_dir, None),
                                  ("onedrive_backups_dir", cfg.onedrive_backups_dir, None)):
            if path is None:
                continue
            item = _item(label, path)
            if exts and item["state"] == "found":
                item["detail"] = _inbox_detail(path, exts)
            items.append(item)
        journal_root = cfg.inbox_dir.parent if cfg.inbox_dir else None
    else:
        proposed = platforms.default_journal_root()
        items.append(_item("proposed", proposed))
        note = note or "no config.toml yet"
        commands = ["python -m lifecrm configure", "python -m lifecrm init"]
        journal_root = proposed
    return ({"title": "Journal — voice notes and quick notes", "note": note,
             "items": items, "commands": commands}, journal_root)


def _iphone_backups() -> dict:
    items = []
    for folder in platforms.mobilesync_backup_dirs():
        item = _item("backups", folder)
        if item["state"] == "found":
            entries = _children(folder)
            if entries is None:
                item["state"], item["detail"] = "no access", (
                    FULL_DISK_ACCESS if platforms.is_mac() else "permission denied")
            else:
                n = sum(1 for p in entries if _is_file(p / "Manifest.db"))
                item["detail"] = f"{n} backup(s)"
        items.append(item)
    return {"title": "iPhone backups — texts and calls", "note": "",
            "items": items, "commands": ["python -m lifecrm observe ios_messages",
                                         "python -m lifecrm observe ios_calls"]}


def _mac_groups() -> list[dict]:
    contacts_base = (platforms.home() / "Library" / "Application Support"
                     / "AddressBook")
    try:
        stores = [_item("store", p) for p in platforms.mac_addressbook_dbs()]
    except PermissionError:
        stores = []
    if not stores:
        stores = [_item("folder", contacts_base)]
    return [
        {"title": "Messages on this Mac", "note": "",
         "items": [_item("chat.db", platforms.mac_messages_db())],
         "commands": ["python -m lifecrm observe mac_messages"]},
        {"title": "Contacts", "note": "", "items": stores,
         "commands": ["python -m lifecrm observe contacts"]},
        {"title": "Photos library (metadata only)", "note": "",
         "items": [_item("Photos.sqlite", platforms.mac_photos_db())],
         "commands": ["python -m lifecrm observe photos"]},
        {"title": "Apple Notes", "note": "",
         "items": [_item("NoteStore.sqlite", platforms.mac_notes_db())],
         "commands": ["python -m lifecrm observe notes"]},
        # Apple Mail's own store: what `observe email` reads with no path
        # when Downloads holds no Takeout with mail in it.
        {"title": "Apple Mail", "note": "",
         "items": [_item("Mail store", platforms.home() / "Library" / "Mail")],
         "commands": ["python -m lifecrm observe email"]},
    ]


def _windows_groups() -> list[dict]:
    """Phone Link's cache of the linked phone — texts, calls and contact
    names with nothing set up on the phone (docs/phone_link.md): one folder
    per phone ever linked, the live one written recently."""
    from datetime import datetime

    from ..connectors import phone_link
    root = platforms.phone_link_root()
    devices = phone_link.device_folders(root)
    items = [_item("cache", folder,
                   f"phone.db written {datetime.fromtimestamp(stamp).strftime('%Y-%m-%d')}"
                   if stamp else "") for folder, stamp in devices]
    if not items:
        items = [_item("cache", root)]
    return [{"title": "Phone Link (texts, calls and contacts from the linked phone)",
             "note": "" if devices else "no phone linked in Phone Link",
             "items": items, "commands": ["python -m lifecrm observe phone_link"]}]


def drop_folders(journal_root: Path | None) -> list[Path]:
    """Where a person saves an export by hand: Downloads, the cloud drive's
    top level, the journal folder, and the Android backup apps' folders."""
    home = platforms.home()
    out = [home / "Downloads"]
    clouds = []
    if platforms.is_mac():
        clouds.append(platforms.icloud_drive())
    clouds += platforms.onedrive_candidates()
    for cloud in clouds:
        out.append(cloud)
        out += [cloud / sub for sub in _app_subfolders()]
    if journal_root is not None:
        out.append(journal_root)
    seen, uniq = set(), []
    for p in out:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _export_commands(kind: str, arg: str) -> list[str]:
    # one Takeout zip feeds several connectors, each reading its own part
    return [f"python -m lifecrm {verb} {arg}" for verb in EXPORT_GROUPS[kind][2]]


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _matches(p: Path, pattern: str) -> bool:
    name = p.name.lower()
    if pattern.endswith("/"):
        return fnmatch.fnmatchcase(name, pattern[:-1]) and _is_dir(p)
    return fnmatch.fnmatchcase(name, pattern)


def _kindle_clippings() -> list[Path]:
    """A plugged-in Kindle's My Clippings.txt — the reading connector's own
    probe (a drive letter on Windows, /Volumes on a Mac). It only checks the
    file is there."""
    try:
        from ..connectors.reading import _kindle_volumes
        return list(_kindle_volumes())
    except Exception:   # one broken connector module must not hide the report
        return []


def _export_groups(folders: list[Path]) -> list[dict]:
    found: dict[str, list[tuple[str, Path]]] = {}
    for folder in folders:
        for p in _children(folder) or []:
            for kind, label, patterns in EXPORTS:
                if any(_matches(p, pat) for pat in patterns):
                    found.setdefault(kind, []).append((label, p))
                    break
    kindle = [("Kindle (plugged in)", p) for p in _kindle_clippings()]
    groups = []
    for kind, (title, generic, _) in EXPORT_GROUPS.items():
        # dated names (sms-20261021....xml) sort newest first this way
        hits = sorted(found.get(kind, []), key=lambda h: h[1].name.lower(),
                      reverse=True)
        if kind == "reading":
            hits = kindle + hits             # the device itself first
        if hits:
            items = [_item(label, p) for label, p in hits[:MAX_LISTED]]
            note = (f"{len(hits)} found; showing the last {MAX_LISTED} by name"
                    if len(hits) > MAX_LISTED else "")
            commands = []
            for _, p in hits[:3]:
                commands += _export_commands(kind, f'"{p}"')
        else:
            items = []
            looked = [_show(f) for f in folders if _state(f) == "found"]
            note = ("none in " + ", ".join(looked)) if looked else "none found"
            if kind == "reading":
                note += "; no Kindle plugged in"
            commands = _export_commands(kind, generic)
        groups.append({"title": title, "note": note, "items": items,
                       "commands": commands})
    return groups


def survey(root: Path) -> dict:
    journal, journal_root = _journal_group(root)
    groups = [journal, _iphone_backups()]
    if platforms.is_mac():
        groups += _mac_groups()
    if platforms.is_windows():
        groups += _windows_groups()
    groups += _export_groups(drop_folders(journal_root))
    known = _known_connectors()
    for g in groups:
        g["commands"] = [{"command": c, "available": _available(c, known)}
                         for c in g["commands"]]
    return {"system": platforms.SYSTEM,
            "config": str(root / "config.toml") if (root / "config.toml").exists() else None,
            "groups": groups}


def _known_connectors() -> set[str]:
    try:
        from ..connectors import base as conn_base
        return set(conn_base.registry())
    except Exception:   # one broken connector module must not hide the report
        return set()


def _available(command: str, known: set[str]) -> bool:
    words = command.split()
    if len(words) > 4 and words[3] == "observe":
        return words[4] in known
    return True


# ---------------------------------------------------------------- output

def _show(path: Path | str) -> str:
    p = Path(path)
    try:
        return str(Path("~") / p.relative_to(platforms.home()))
    except ValueError:
        return str(p)


def where(as_json: bool = typer.Option(False, "--json")) -> None:
    """Report what this machine has that the connectors can read (read-only)."""
    from ..cli import _echo_json
    env_root = os.environ.get("LIFECRM_ROOT")
    report = survey(Path(env_root) if env_root else PROJECT_ROOT)
    if as_json:
        _echo_json(report)
        return
    names = {"mac": "Mac", "windows": "Windows PC"}
    typer.echo(f"this machine: {names.get(report['system'], report['system'])}; "
               + (f"config: {report['config']}" if report["config"]
                  else "no config.toml yet — run: python -m lifecrm configure"))
    for g in report["groups"]:
        typer.echo("")
        typer.echo(g["title"])
        if g["note"]:
            typer.echo(f"  {g['note']}")
        width = max([len(i["label"]) for i in g["items"]] or [0])
        for i in g["items"]:
            typer.echo(f"  {i['state']:<9}  {i['label']:<{width}}  {_show(i['path'])}"
                       + (f" — {i['detail']}" if i["detail"] else ""))
        for c in g["commands"]:
            typer.echo(f"  run: {c['command']}"
                       + ("" if c["available"] else "   (connector not in this version)"))


def register(app: typer.Typer) -> None:
    app.command("where")(where)

"""`lifecrm where`: what this machine holds that Life CRM can read.

Each connector knows where its own data lives, but someone setting up a Mac
or a Windows laptop needs one answer to "what is here, and what do I run for
it". This walks the platform's known places (platforms.py), the configured
journal folders and the usual places an export gets saved to, and prints
each with whether it exists and the command that reads it.

It only looks. Nothing is opened as a database, nothing is imported or
written, no iCloud download is requested (intake does that) — and a place
macOS guards (Messages, iPhone backups, Contacts) is reported as 'no access'
with the Full Disk Access fix instead of a traceback.
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
# case-insensitively against the top level of each drop folder.
EXPORTS = (
    ("health", "Apple Health", ("export.zip", "export *.zip", "export(*).zip",
                                "export (*).zip", "apple_health_export")),
    ("health", "Health Connect", ("health connect*.zip", "health_connect*.zip",
                                  "healthconnect*.zip")),
    ("sms", "SMS Backup & Restore", ("sms-*.xml", "calls-*.xml")),
    ("takeout", "Google Takeout", ("takeout-*.zip", "takeout")),
    ("whatsapp", "WhatsApp chat export", ("whatsapp chat*.zip", "whatsapp chat*.txt")),
    ("calendar", "calendar export", ("*.ics",)),
)
# Subfolders the Android backup apps write into, inside a cloud drive.
_APP_SUBFOLDERS = ("SMSBackupRestore", "SMS Backup & Restore")
MAX_LISTED = 8


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
    ]


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
        out += [cloud / sub for sub in _APP_SUBFOLDERS]
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
    return {
        "health": [f"python -m lifecrm health import {arg}"],
        "sms": [f"python -m lifecrm observe android_sms {arg}"],
        # one Takeout zip feeds several connectors, each reading its own part
        "takeout": [f"python -m lifecrm observe {c} {arg}"
                    for c in ("calendar", "location", "browser", "notes", "photos")],
        "whatsapp": [f"python -m lifecrm observe whatsapp {arg}"],
        "calendar": [f"python -m lifecrm observe calendar {arg}"],
    }[kind]


def _export_groups(folders: list[Path]) -> list[dict]:
    found: dict[str, list[tuple[str, Path]]] = {}
    for folder in folders:
        for p in _children(folder) or []:
            name = p.name.lower()
            for kind, label, patterns in EXPORTS:
                if any(fnmatch.fnmatchcase(name, pat) for pat in patterns):
                    found.setdefault(kind, []).append((label, p))
                    break
    titles = {"health": "Health exports (Apple Health, Health Connect)",
              "sms": "SMS Backup & Restore (Android texts and calls)",
              "takeout": "Google Takeout",
              "whatsapp": "WhatsApp chat exports",
              "calendar": "Calendar exports (.ics)"}
    generic = {"health": "<export.zip>", "sms": "<sms-....xml>",
               "takeout": "<takeout-....zip>", "whatsapp": "<WhatsApp Chat ....zip>",
               "calendar": "<calendar.ics>"}
    groups = []
    for kind, title in titles.items():
        # dated names (sms-20261021....xml) sort newest first this way
        hits = sorted(found.get(kind, []), key=lambda h: h[1].name.lower(),
                      reverse=True)
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
            commands = _export_commands(kind, generic[kind])
        groups.append({"title": title, "note": note, "items": items,
                       "commands": commands})
    return groups


def survey(root: Path) -> dict:
    journal, journal_root = _journal_group(root)
    groups = [journal, _iphone_backups()]
    if platforms.is_mac():
        groups += _mac_groups()
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

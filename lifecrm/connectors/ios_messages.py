"""iOS Messages connector: iMessage, SMS and RCS from an iPhone backup.

The iPhone has no API for its messages. What it does have is a local backup:
the Apple Devices app on Windows (or Finder on a Mac) writes one to disk, and
an *unencrypted* backup stores the Messages database as a plain SQLite file.
This connector reads that file. It never talks to a phone, never reads a live
database in place, and never sends anything.

Accepted targets
----------------
- nothing at all: `default_target` finds the one backup on this machine where
  Apple's tools write them, and refuses to guess between several phones.
- a backup folder (the one holding Manifest.db) — the normal case. The
  Messages database is located through Manifest.db, and the phone's own
  address book is read from the same backup to name the handles.
- the MobileSync\\Backup folder above it, when it holds exactly one backup.
- a folder holding sms.db (or chat.db) and, optionally, AddressBook.sqlitedb.
- a bare sms.db / chat.db file. A Mac's ~/Library/Messages/chat.db has the
  same schema; it is copied through SQLite's backup API, never read in place.

What comes in
-------------
One observation per message, keyed on the message GUID:

  imessage / sms / rcs   the message itself, by the service that carried it
  text_reaction          a tapback — loved, liked, laughed, a custom emoji —
                         added or removed. Kept out of the message streams:
                         iOS writes a tapback as a row whose text reads
                         'Loved "see you at 6"', and letting that into message
                         search and "last talked to" counts would be noise
  text_event             something that happened to the thread: a member
                         added or removed, a number changed, a rename, an
                         icon or background change, someone leaving, a
                         location share started or stopped, an audio message
                         kept, SharePlay

Participants are **handles, never names**: phone numbers, email addresses,
and — for Business Chat and alphanumeric senders — an app_account handle. The
backup's address book contributes each handle's `display_name` ("what the
source system called it, for review"); it never links a handle to an entity.
That stays a decision the owner makes, with `handles list --suggest` offering
candidates.

Deletion semantics — the archive mirrors the phone (owner's decision, 2026-09-24)
-------------------------------------------------------------------------------
- A message deleted from the phone is absent from the next backup, and its
  observation is removed. An unsent message is treated the same way: never
  imported, and removed if it already was.
- "Gone" means gone from the phone's database — the scan reports every GUID
  the phone still holds (`present_ids`), and the shared write layer removes
  only what is missing from that set, scoped to rows first imported from the
  same account. A message this connector cannot read today is still on the
  phone and stays archived.
- A message recovered on the phone (Recently Deleted) returns as a new
  observation on the next import.
- An edited message is re-emitted with its current text; the edit time is its
  last_modified_at, so it updates through newer-wins. Its version history
  from message_summary_info rides along in value_json.
- Removal is guarded in domain/observations.py: a removal large enough to
  look like an incomplete backup is refused without --allow-mass-removal,
  a snapshot is taken first, and a row a confirmed entry cites as provenance
  is hidden rather than deleted.

Known limitations, by design
----------------------------
- Journal dates use the configured home timezone; the database records no
  zone per message.
- Attachments are recorded by name, type and path inside the phone. The files
  stay in the backup — media never enters the database.
- An Android phone's reactions arrive over SMS as ordinary text
  ('Loved "..."') and are imported as the messages they are.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import tempfile
from pathlib import Path

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base, typedstream

name = "ios_messages"
streams = ("imessage", "sms", "rcs", "text_reaction", "text_event")
# Texts flow daily; a newest message this old means backups have stopped.
stale_after_days = 14

APPLE_EPOCH = 978307200            # 2001-01-01T00:00:00Z in Unix seconds
SMS_DB = ("HomeDomain", "Library/SMS/sms.db")
ADDRESS_BOOK = ("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")
OBJECT_REPLACEMENT = "\N{OBJECT REPLACEMENT CHARACTER}"   # an attachment's place in the text
OWNER_LABEL = "you (this phone)"

_SERVICE_STREAMS = {"imessage": "imessage", "sms": "sms", "mms": "sms", "rcs": "rcs"}
_REACTIONS = {0: "loved", 1: "liked", 2: "disliked", 3: "laughed",
              4: "emphasized", 5: "questioned", 6: "emoji", 7: "sticker"}
_GROUP_STYLE = 43                  # chat.style: 43 group, 45 one-to-one

# Optional message columns: older iOS versions lack some of these; a missing
# column reads as NULL rather than failing the import.
_MESSAGE_COLUMNS = (
    "text", "attributedBody", "handle_id", "service", "date", "is_from_me",
    "associated_message_guid", "associated_message_type",
    "associated_message_emoji", "thread_originator_guid", "date_edited",
    "date_retracted", "message_summary_info", "item_type",
    "destination_caller_id", "account", "is_audio_message",
    "expressive_send_style_id", "balloon_bundle_id",
    "group_action_type", "other_handle", "group_title", "share_status",
    "share_direction",
)

# Thread events: (item_type, group_action_type) -> what happened. item_type 1
# carries the affected member in other_handle; an "added" row naming the
# sender themself is that person changing their number.
_EVENTS_3 = {0: "left", 1: "icon_changed", 2: "icon_removed",
             4: "background_changed", 6: "background_removed"}
_MEMBER_EVENTS = ("member_added", "member_removed", "number_changed")


# ---------------------------------------------------------------- time

def apple_iso(value) -> str | None:
    """Apple Messages time -> 'YYYY-MM-DDTHH:MM:SSZ'.

    Seconds since 2001 in older databases, nanoseconds since 2001 in newer
    ones — both occur, sometimes in one database. Any value past 1e10 is
    nanoseconds: in seconds that would be the year 2318.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        # Integer division: nanoseconds through a float can land a hair
        # below the second they belong to and print as the one before.
        seconds = value // 1_000_000_000 if value > 10**10 else value
    else:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        seconds = int(v // 1e9) if v > 1e10 else int(v)
    if seconds <= 0:
        return None
    return base.iso_utc(APPLE_EPOCH + seconds)


# ---------------------------------------------------------------- opening

def _fail_encrypted(target: Path) -> DomainError:
    return DomainError(
        f"{target.name} is an encrypted backup — its databases cannot be read "
        f"without the backup password. Make an unencrypted backup instead "
        f"(Apple Devices: untick 'Encrypt local backup') — see docs/texts.md")


def _backup_files(target: Path) -> dict:
    """Locate the Messages and address-book databases inside a backup folder.

    Manifest.db maps (domain, relativePath) to the hashed file name; the name
    is also SHA1('<domain>-<path>'), which is the fallback when the manifest
    has no row. Each path comes back with its -wal sibling when one exists:
    a backup can hold writes that were never checkpointed into the main file.
    """
    if _is_encrypted(target):
        raise _fail_encrypted(target)
    try:
        man = sqlite3.connect(f"{(target / 'Manifest.db').resolve().as_uri()}?mode=ro",
                              uri=True)
        try:
            man.execute("SELECT fileID, domain, relativePath FROM Files LIMIT 1")
        except sqlite3.DatabaseError as e:
            man.close()
            if "not a database" in str(e) or "encrypted" in str(e):
                raise _fail_encrypted(target) from e
            raise DomainError(f"{target.name}/Manifest.db has no Files table — "
                              f"is this an iOS backup folder? ({e})") from e
    except sqlite3.DatabaseError as e:
        raise DomainError(f"cannot open {target.name}/Manifest.db: {e}") from e

    def locate(domain: str, rel: str) -> Path | None:
        row = man.execute("SELECT fileID FROM Files WHERE domain=? AND "
                          "relativePath=?", (domain, rel)).fetchone()
        file_id = row[0] if row else hashlib.sha1(
            f"{domain}-{rel}".encode()).hexdigest()
        p = target / file_id[:2] / file_id
        return p if p.exists() else None

    try:
        out = {"messages": locate(*SMS_DB),
               "messages_wal": locate(SMS_DB[0], SMS_DB[1] + "-wal"),
               "contacts": locate(*ADDRESS_BOOK),
               "contacts_wal": locate(ADDRESS_BOOK[0], ADDRESS_BOOK[1] + "-wal")}
    finally:
        man.close()
    if out["messages"] is None:
        raise DomainError(
            f"{target.name} is an iOS backup with no Messages database "
            f"(Library/SMS/sms.db) — was Messages in iCloud with 'Optimize' on, "
            f"or is this a partial backup?")
    return out


def _device(target: Path) -> dict:
    """Device name and backup date from Info.plist, when present."""
    info = target / "Info.plist"
    if not info.exists():
        return {}
    try:
        data = plistlib.loads(info.read_bytes())
    except Exception:
        return {}
    out = {}
    if isinstance(data.get("Device Name"), str):
        out["device"] = data["Device Name"]
    when = data.get("Last Backup Date")
    if hasattr(when, "strftime"):
        out["backup_date"] = when.strftime("%Y-%m-%d")
    return out


def _is_encrypted(folder: Path) -> bool:
    manifest_plist = folder / "Manifest.plist"
    if not manifest_plist.exists():
        return False
    try:
        meta = plistlib.loads(manifest_plist.read_bytes())
    except Exception:
        return False
    return isinstance(meta, dict) and bool(meta.get("IsEncrypted"))


# ---------------------------------------------------------------- finding backups

def backup_roots() -> list[Path]:
    """Where Apple's tools write device backups: the Apple Devices app (and
    Microsoft Store iTunes), desktop iTunes, then a Mac's Finder."""
    roots = []
    if os.environ.get("USERPROFILE"):
        roots.append(Path(os.environ["USERPROFILE"]) / "Apple" / "MobileSync" / "Backup")
    if os.environ.get("APPDATA"):
        roots.append(Path(os.environ["APPDATA"]) / "Apple Computer" / "MobileSync"
                     / "Backup")
    roots.append(Path.home() / "Library" / "Application Support" / "MobileSync"
                 / "Backup")
    return list(dict.fromkeys(roots))


def find_backups(root: Path) -> list[dict]:
    """Every device backup directly under `root`, with what identifies it."""
    if not root.is_dir():
        return []
    found = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "Manifest.db").exists():
            found.append({"path": d, "encrypted": _is_encrypted(d), **_device(d)})
    return found


def _choose(found: list[dict], searched: str) -> Path:
    """The one backup there is — never a guess between several phones."""
    if not found:
        raise DomainError(
            f"no iPhone backup found in {searched} — make one first "
            f"(docs/texts.md), or pass the backup folder's path")
    if len(found) == 1:
        return found[0]["path"]
    listing = "; ".join(
        f"{b['path']} ({b.get('device') or 'unknown device'}, "
        f"{b.get('backup_date') or 'undated'}{', encrypted' if b['encrypted'] else ''})"
        for b in found)
    raise DomainError(f"{len(found)} iPhone backups found — pass one of them: {listing}")


def default_target(cfg: Config) -> Path:
    """The backup to read when no path is given: the only one on this
    machine, looked for where Apple's tools put them."""
    roots = backup_roots()
    return _choose([b for root in roots for b in find_backups(root)],
                   ", ".join(str(r) for r in roots))


def _resolve(target: Path) -> dict:
    """{'messages', 'messages_wal', 'contacts', 'contacts_wal', 'kind', 'folder'}."""
    if target.is_dir():
        if (target / "Manifest.db").exists():
            return {**_backup_files(target), "kind": "backup", "folder": target}
        for db in ("sms.db", "chat.db"):
            if (target / db).exists():
                book = target / "AddressBook.sqlitedb"
                return {"messages": target / db, "messages_wal": None,
                        "contacts": book if book.exists() else None,
                        "contacts_wal": None, "kind": "folder", "folder": target}
        backups = find_backups(target)
        if backups:          # the MobileSync\Backup folder itself: pick its one backup
            return _resolve(_choose(backups, str(target)))
        listing = ", ".join(sorted(p.name for p in target.iterdir())[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} is neither an iOS backup (no Manifest.db) nor a "
            f"folder holding sms.db or chat.db (has: {listing})")
    return {"messages": target, "messages_wal": None, "contacts": None,
            "contacts_wal": None, "kind": "file", "folder": target}


def _snapshot(src: Path, wal: Path | None, tmp: Path,
              label: str) -> tuple[sqlite3.Connection, bool]:
    """A private, consistent copy of a database, opened query-only, and
    whether a -wal (writes never checkpointed into the main file) was read.

    A backup's hashed files are copied under their real names so SQLite finds
    the -wal beside the main file. A bare file goes through the backup API,
    which yields a consistent snapshot even of a database another process is
    writing — the Mac's live chat.db, say. Either way, the original is never
    opened for writing.
    """
    dest = tmp / f"{label}.db"
    sibling_wal = src.with_name(src.name + "-wal")
    sibling_shm = src.with_name(src.name + "-shm")
    if wal is None and sibling_wal.exists() and not sibling_shm.exists():
        # Copied files: SQLite cannot open a WAL database read-only without
        # its -shm, so take both halves. (A live database has its -shm, and
        # the backup API below reads it consistently.)
        wal = sibling_wal
    try:
        if wal is not None:
            shutil.copyfile(src, dest)
            shutil.copyfile(wal, tmp / f"{label}.db-wal")
        else:
            source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
            try:
                copy = sqlite3.connect(dest)
                source.backup(copy)
                copy.close()
            finally:
                source.close()
        conn = sqlite3.connect(dest)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        return conn, wal is not None or sibling_wal.exists()
    except sqlite3.DatabaseError as e:
        raise DomainError(f"cannot read {src.name} as a database: {e}") from e


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


# ---------------------------------------------------------------- contacts

def _contact_names(conn: sqlite3.Connection | None) -> dict[tuple[str, str], str]:
    """(kind, value) -> the name on the phone's contact card.

    iOS's AddressBook.sqlitedb is its own schema, not the Mac's Core Data
    store: ABPerson holds the names, ABMultiValue the numbers and addresses
    (property 3 = phone, 4 = email), joined on record_id. A number on two
    cards keeps both names — "Mom / Dad" is the honest label for a shared
    landline, and picking one would be a guess.
    """
    if conn is None:
        return {}
    person_cols = _columns(conn, "ABPerson")
    if not person_cols or not _columns(conn, "ABMultiValue"):
        return {}
    pick = [c for c in ("First", "Last", "Nickname", "Organization") if c in person_cols]
    if not pick:
        return {}
    select = ", ".join(f"p.{c}" for c in pick)
    names: dict[tuple[str, str], set[str]] = {}
    for r in conn.execute(
            f"SELECT {select}, mv.property, mv.value FROM ABPerson p "
            f"JOIN ABMultiValue mv ON mv.record_id = p.ROWID "
            f"WHERE mv.property IN (3, 4) AND mv.value IS NOT NULL"):
        row = dict(zip(pick + ["property", "value"], r))
        full = " ".join(x for x in (row.get("First"), row.get("Last")) if x).strip()
        label = full or (row.get("Nickname") or "").strip() \
            or (row.get("Organization") or "").strip()
        h = base.handle_for(row["value"])
        if label and h:
            names.setdefault(h, set()).add(label)
    return {h: " / ".join(sorted(v)) for h, v in names.items()}


# ---------------------------------------------------------------- messages

def _edit_versions(blob) -> tuple[list[dict], list[int]]:
    """(versions, unsent part indexes) from message_summary_info.

    A binary plist: 'ec' maps a body-part index to that part's versions, each
    {'d': edit time, 't': typedstream of the text}; 'rp' lists parts unsent.
    Anything unreadable yields nothing rather than failing the message.
    """
    if not blob:
        return [], []
    try:
        info = plistlib.loads(bytes(blob))
    except Exception:
        return [], []
    if not isinstance(info, dict):
        return [], []
    versions = []
    ec = info.get("ec")
    if isinstance(ec, dict):
        for part in sorted(ec, key=lambda k: int(k) if str(k).isdigit() else 0):
            events = ec[part]
            if not isinstance(events, list):
                continue
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                t = ev.get("t")
                versions.append({
                    "part": int(part) if str(part).isdigit() else 0,
                    "at": apple_iso(ev.get("d")),
                    "text": _clean(typedstream.attributed_text(t))
                    if isinstance(t, bytes) else None})
    rp = info.get("rp")
    unsent = [int(i) for i in rp if isinstance(i, int)] if isinstance(rp, list) else []
    return versions, unsent


def _json(detail: dict) -> str | None:
    return canonical_json({k: v for k, v in detail.items() if v is not None})


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    text = text.replace(OBJECT_REPLACEMENT, "").strip()
    return text or None


def _target_guid(assoc: str) -> tuple[str, int | None]:
    """'p:1/GUID' -> ('GUID', 1); 'bp:GUID' -> ('GUID', None)."""
    if "/" in assoc:
        head, guid = assoc.split("/", 1)
        part = head.split(":", 1)[-1]
        return guid, int(part) if part.isdigit() else None
    if ":" in assoc:
        return assoc.split(":", 1)[1], None
    return assoc, None


def _reaction(code: int) -> tuple[str, bool] | None:
    """(name, removed) for a tapback code, or None if not a tapback."""
    if 2000 <= code < 2100:
        return _REACTIONS.get(code - 2000, f"type {code}"), False
    if 3000 <= code < 3100:
        return _REACTIONS.get(code - 3000, f"type {code}"), True
    if code == 1000:
        return "sticker", False
    return None


def _event(r: dict) -> dict | None:
    """A thread event's detail, or None when the row is not one we know."""
    kind, action = r["item_type"], r["group_action_type"] or 0
    if kind == 1 and r["other_handle"]:
        if action == 0:
            return {"event": "number_changed" if r["other_handle"] == r["handle_id"]
                    else "member_added"}
        if action == 1:
            return {"event": "member_removed"}
    if kind == 2 and r["group_title"] is not None:
        return {"event": "renamed", "name": r["group_title"]}
    if kind == 3 and action in _EVENTS_3:
        return {"event": _EVENTS_3[action]}
    if kind == 4 and action == 0:
        return {"event": "location_stopped" if r["share_status"] else "location_started"}
    if kind == 5:
        return {"event": "audio_kept"}
    if kind == 6:
        return {"event": "shareplay"}
    return None


def _read(conn: sqlite3.Connection, contacts: dict, cfg: Config) -> dict:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = {"message", "handle"} - tables
    if missing:
        raise DomainError(f"not a Messages database — no {', '.join(sorted(missing))} "
                          f"table (has: {', '.join(sorted(tables)[:10]) or 'nothing'})")
    mcols = _columns(conn, "message")
    select = ", ".join(f"m.{c}" if c in mcols else f"NULL AS {c}"
                       for c in _MESSAGE_COLUMNS)

    handle_ids = {r[0]: r[1] for r in conn.execute("SELECT ROWID, id FROM handle")}
    chats, chat_members, msg_chat = {}, {}, {}
    if "chat" in tables:
        ccols = _columns(conn, "chat")
        pick = [c for c in ("guid", "chat_identifier", "display_name", "style")
                if c in ccols]
        for r in conn.execute(f"SELECT ROWID, {', '.join(pick)} FROM chat"):
            chats[r[0]] = dict(zip(pick, r[1:]))
    if "chat_handle_join" in tables:
        for chat_id, hid in conn.execute(
                "SELECT chat_id, handle_id FROM chat_handle_join ORDER BY handle_id"):
            chat_members.setdefault(chat_id, []).append(hid)
    if "chat_message_join" in tables:
        for chat_id, mid in conn.execute(
                "SELECT chat_id, message_id FROM chat_message_join ORDER BY chat_id DESC"):
            msg_chat[mid] = chat_id          # lowest chat id wins when duplicated
    attachments: dict[int, list] = {}
    payloads = 0
    if {"attachment", "message_attachment_join"} <= tables:
        acols = _columns(conn, "attachment")
        pick = [c for c in ("filename", "transfer_name", "mime_type", "uti",
                            "is_sticker", "hide_attachment") if c in acols]
        for r in conn.execute(
                f"SELECT j.message_id, {', '.join('a.' + c for c in pick)} "
                f"FROM message_attachment_join j JOIN attachment a "
                f"ON a.ROWID = j.attachment_id ORDER BY j.message_id, a.ROWID"):
            a = dict(zip(pick, r[1:]))
            fname = a.get("filename") or ""
            if (str(a.get("uti") or "").startswith(
                    "com.apple.messages.MSMessageExtensionBalloonPlugin")
                    or fname.endswith(".pluginPayloadAttachment")):
                payloads += 1                  # link-preview plumbing, not content
                continue
            if a.get("hide_attachment"):
                continue
            item = {"name": a.get("transfer_name") or Path(fname).name or None,
                    "mime": a.get("mime_type"),
                    "path": fname[2:] if fname.startswith("~/") else (fname or None)}
            if a.get("is_sticker"):
                item["sticker"] = True
            attachments.setdefault(r[0], []).append(
                {k: v for k, v in item.items() if v is not None})

    records: list[dict] = []
    handles: dict[tuple[str, str], str | None] = {}
    owners: dict[str, int] = {}
    skipped: dict[str, int] = {}
    unmapped: dict[str, int] = {}
    examples: dict[str, tuple[str, str]] = {}      # raw address -> its handle
    present: set[str] = set()                      # GUIDs the phone still holds
    counts = {"messages": 0, "chats": len(chats)}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def seen(h: tuple[str, str], owner: bool = False) -> dict:
        label = OWNER_LABEL if owner else contacts.get(h)
        if h not in handles or (label and not handles[h]):
            handles[h] = label
        return {"kind": h[0], "value": h[1]}

    for row in conn.execute(f"SELECT m.ROWID, m.guid, {select} FROM message m "
                            f"ORDER BY m.ROWID"):
        r = dict(zip(["rowid", "guid", *_MESSAGE_COLUMNS], row))
        counts["messages"] += 1
        if not r["guid"]:
            skip("message without a GUID")
            continue
        # The archive mirrors the phone: an unsent message is treated exactly
        # like a deleted one — not imported, and removed if it already was.
        if apple_iso(r["date_retracted"]):
            skip("unsent message (not kept — the archive mirrors the phone)")
            continue
        present.add(r["guid"])        # on the phone, even if skipped below
        sent = apple_iso(r["date"])
        if sent is None:
            skip("message with no usable timestamp")
            continue

        chat = chats.get(msg_chat.get(r["rowid"]), {})
        members = [handle_ids[h] for h in chat_members.get(msg_chat.get(r["rowid"]), [])
                   if h in handle_ids]
        is_group = chat.get("style") == _GROUP_STYLE or len(members) > 1
        from_me = bool(r["is_from_me"])
        event = None
        if r["item_type"] not in (None, 0):
            event = _event(r)
            if event is None:
                label = (f"thread event item_type {r['item_type']}/"
                         f"{r['group_action_type'] or 0}")
                unmapped[label] = unmapped.get(label, 0) + 1
                continue
            if r["item_type"] == 4 and r["other_handle"] \
                    and r["share_direction"] is not None:
                # A location share is filed against the other party; the
                # direction flag says whose location it was.
                from_me = from_me or not r["share_direction"]
        owner = base.handle_for(r["destination_caller_id"] or r["account"])
        if owner:
            owners[owner[1]] = owners.get(owner[1], 0) + 1

        participants: list[dict] = []

        def add(raw, role, is_owner=False):
            h = owner if is_owner else base.handle_for(raw)
            if h:
                p = {**seen(h, owner=is_owner), "role": role}
                if p not in participants:
                    participants.append(p)
                if not is_owner and len(examples) < 50 and str(raw).strip() != h[1]:
                    examples.setdefault(str(raw).strip(), h)
            return h

        sender_raw = None if from_me else handle_ids.get(r["handle_id"])
        if from_me:
            add(None, "sender", is_owner=True)
        elif sender_raw:
            add(sender_raw, "sender")
        if event is not None:
            # A thread event names who did it and, for membership changes,
            # who it was done to — not everyone in the room.
            member_raw = handle_ids.get(r["other_handle"])
            if event["event"] in _MEMBER_EVENTS and member_raw:
                h = add(member_raw, "participant")
                if h:
                    event["member"] = f"{h[0]}:{h[1]}"
        else:
            for m in members or ([handle_ids[r["handle_id"]]]
                                 if r["handle_id"] in handle_ids else []):
                if base.handle_for(m) != base.handle_for(sender_raw):
                    add(m, "recipient")
            if not from_me:
                add(None, "recipient", is_owner=True)

        detail = {"chat": chat.get("guid") or chat.get("chat_identifier")}
        if is_group:
            detail["group"] = True
        title = (chat.get("display_name") or "").strip() or None if is_group else None
        direction = "out" if from_me else "in"
        common = {"journal_date": base.journal_date_for_iso(sent, cfg),
                  "ended_at": None, "utc_offset_sec": None, "direction": direction,
                  "value": None, "unit": None, "media_path": None,
                  "participants": participants}

        # --- thread events: membership, renames, location shares ---------
        if event is not None:
            records.append({**common, "stream": "text_event", "external_id": r["guid"],
                            "started_at": sent, "title": title, "body": None,
                            "value_json": _json({**detail, **event}),
                            "origin_app": "messages", "last_modified_at": sent})
            continue

        # --- tapbacks ----------------------------------------------------
        code = r["associated_message_type"] or 0
        if code and r["associated_message_guid"]:
            reaction = _reaction(int(code))
            if reaction is None:
                unmapped[f"association type {code}"] = \
                    unmapped.get(f"association type {code}", 0) + 1
                continue
            target, part = _target_guid(r["associated_message_guid"])
            d = {**detail, "reaction": reaction[0], "target": target}
            if part is not None:
                d["part"] = part
            if reaction[1]:
                d["removed"] = True
            if r["associated_message_emoji"]:
                d["emoji"] = r["associated_message_emoji"]
            records.append({**common, "stream": "text_reaction",
                            "external_id": r["guid"], "started_at": sent,
                            "title": title, "body": None,
                            "value_json": _json(d),
                            "origin_app": "messages", "last_modified_at": sent})
            continue

        # --- the message itself -----------------------------------------
        body = _clean(r["text"])
        undecodable = False
        if body is None and r["attributedBody"]:
            decoded = typedstream.attributed_text(r["attributedBody"])
            undecodable = decoded is None
            body = _clean(decoded)
        files = attachments.get(r["rowid"])
        if files:
            detail["attachments"] = files
        edited_at = apple_iso(r["date_edited"])
        versions, unsent_parts = _edit_versions(r["message_summary_info"])
        if edited_at:
            detail["edited_at"] = edited_at
        if versions:
            detail["versions"] = versions
        if unsent_parts:
            detail["unsent_parts"] = unsent_parts
        if r["thread_originator_guid"]:
            detail["reply_to"] = r["thread_originator_guid"]
        if r["is_audio_message"]:
            detail["audio"] = True
        if r["expressive_send_style_id"]:
            detail["effect"] = r["expressive_send_style_id"]
        if r["balloon_bundle_id"]:
            detail["app"] = r["balloon_bundle_id"]
        if body is None and not files and "app" not in detail:
            skip("message whose attributedBody could not be decoded" if undecodable
                 else "message with no text or attachment")
            continue
        if undecodable:
            label = "message kept without its text (attributedBody not decodable)"
            unmapped[label] = unmapped.get(label, 0) + 1
        service = (r["service"] or "").strip()
        stream = _SERVICE_STREAMS.get(service.lower(), "sms")
        if service and service.lower() not in _SERVICE_STREAMS:
            unmapped[f"service '{service}' (imported as sms)"] = \
                unmapped.get(f"service '{service}' (imported as sms)", 0) + 1
        detail["service"] = service or None
        records.append({**common, "stream": stream, "external_id": r["guid"],
                        "started_at": sent, "title": title, "body": body,
                        "value_json": _json(detail),
                        "origin_app": "messages",
                        "last_modified_at": edited_at or sent})

    if payloads:
        skipped["link-preview payload attachment"] = payloads
    account = max(owners, key=owners.get) if owners else None
    return {"records": records, "handles": handles, "account": account,
            "skipped": skipped, "unmapped": unmapped, "counts": counts,
            "examples": examples, "present": present}


# ---------------------------------------------------------------- scan

def _content_hash(*paths: Path | None) -> str:
    """One hash over the database and its -wal: new messages can live only in
    the -wal, and a backup that changed must not look like one that didn't."""
    h = hashlib.sha256()
    for p in paths:
        if p is not None and p.exists():
            h.update(base.file_hash(p).encode())
    return h.hexdigest()


def scan(target: Path, cfg: Config) -> base.Scan:
    files = _resolve(target)
    folder = files["folder"]
    info = _device(folder) if files["kind"] == "backup" else {}
    tmp = tempfile.TemporaryDirectory(prefix="lifecrm-msg-")
    try:
        tmp_path = Path(tmp.name)
        msg_conn, wal_read = _snapshot(files["messages"], files["messages_wal"],
                                       tmp_path, "messages")
        book_conn = None
        try:
            contacts = {}
            if files["contacts"] is not None:
                try:
                    book_conn, _ = _snapshot(files["contacts"], files["contacts_wal"],
                                             tmp_path, "contacts")
                    contacts = _contact_names(book_conn)
                except (DomainError, sqlite3.DatabaseError):
                    contacts = {}          # names are a courtesy; never fail on them
            result = _read(msg_conn, contacts, cfg)
        finally:
            msg_conn.close()
            if book_conn is not None:
                book_conn.close()
    finally:
        tmp.cleanup()

    records = sorted(result["records"],
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    handles = [{"kind": k, "value": v, "display_name": label}
               for (k, v), label in sorted(result["handles"].items())]
    skipped = [f"{v} × {k}" for k, v in sorted(result["skipped"].items())]
    unmapped = [{"what": k, "count": v} for k, v in sorted(result["unmapped"].items())]

    # The facts a first real import has to check, stated by the import itself
    # rather than left for someone to go and find out (docs/texts.md).
    dates = [r["journal_date"] for r in records if r["stream"] in ("imessage", "sms", "rcs")]
    names = dict(result["handles"])
    facts = {
        "folder": str(folder.resolve()),
        "wal_read": wal_read,
        "oldest_message": min(dates) if dates else None,
        "newest_message": max(dates) if dates else None,
        "normalized": [{"raw": raw, "handle": f"{h[0]}:{h[1]}", "name": names.get(h)}
                       for raw, h in sorted(result["examples"].items())[:5]],
    }
    return base.empty_scan(
        name,
        account=result["account"],
        source_file=folder.name,
        source_hash=_content_hash(files["messages"], files["messages_wal"]
                                  or files["messages"].with_name(
                                      files["messages"].name + "-wal")),
        records=records,
        handles=handles,
        unmapped=unmapped,
        skipped=skipped,
        facts=facts,
        present_ids=result["present"],
        mirror_streams=list(streams),
        target_kind=files["kind"],
        contacts_read=files["contacts"] is not None,
        named_handles=sum(1 for h in handles if h["display_name"]
                          and h["display_name"] != OWNER_LABEL),
        messages_seen=result["counts"]["messages"],
        chats=result["counts"]["chats"],
        **info)


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    who = "you" if rec["direction"] == "out" else "them"
    if rec["stream"] == "text_reaction":
        d = json.loads(rec["value_json"])
        what = ("removed " if d.get("removed") else "") + d.get("reaction", "?")
        if d.get("emoji"):
            what += f" {d['emoji']}"
        return f"{rec['journal_date']} {who}: {what}"
    if rec["stream"] == "text_event":
        d = json.loads(rec["value_json"])
        what = d.get("event", "?").replace("_", " ")
        if d.get("name"):
            what += f" to '{d['name']}'"
        return f"{rec['journal_date']} {who}: {what}"
    body = " ".join((rec["body"] or "[attachment]").split())
    if len(body) > 60:
        body = body[:59] + "…"
    return f"{rec['journal_date']} {who}: {body}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    where = {"backup": "iOS backup", "folder": "folder", "file": "database file"}
    head = f"ios-messages: {scan['source_file']} ({where.get(scan['target_kind'], '?')}"
    if scan.get("device"):
        head += f" of {scan['device']}"
    if scan.get("backup_date"):
        head += f", taken {scan['backup_date']}"
    lines = [head + f"; {scan['messages_seen']} message row(s), {scan['chats']} chat(s))"]
    facts = scan.get("facts") or {}
    if facts.get("folder"):
        lines.append(f"  read from: {facts['folder']}")
    if scan["account"]:
        lines.append(f"  account: {scan['account']}")
    if facts.get("oldest_message"):
        lines.append(f"  messages span {facts['oldest_message']}..{facts['newest_message']} "
                     f"— check the oldest against what the phone shows")
    lines.append("  sms.db-wal: " + ("read — writes not yet folded into the main file "
                                     "are included" if facts.get("wal_read") else "none"))
    if facts.get("normalized"):
        lines.append("  normalized, e.g.: " + "; ".join(
            f"{n['raw']} -> {n['handle'].split(':', 1)[1]}"
            + (f" ({n['name']})" if n.get("name") else "")
            for n in facts["normalized"]))

    by_stream: dict[str, dict] = {}
    for rec in plan["new"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["new"].append(rec)
    for _, rec, _ in plan["updates"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["upd"].append(rec)
    for stream in sorted(by_stream):
        info = by_stream[stream]
        recs = info["new"] or info["upd"]
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  {stream}: {len(info['new'])} new, {len(info['upd'])} changed "
                     f"| {span} | e.g. {_sample(latest)}")
    removed = plan.get("removed") or []
    if not by_stream and not removed:
        lines.append("  nothing new — the backup matches the database")
    if removed:
        per_stream: dict[str, int] = {}
        per_chat: dict[str, int] = {}
        for row in removed:
            per_stream[row["stream"]] = per_stream.get(row["stream"], 0) + 1
            chat = row["title"] or (json.loads(row["value_json"]).get("chat")
                                    if row["value_json"] else None) or "?"
            per_chat[chat] = per_chat.get(chat, 0) + 1
        top = sorted(per_chat.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        lines.append(
            f"  remove: {len(removed)} no longer on the phone (deleted or unsent) — "
            + ", ".join(f"{n} {s}" for s, n in sorted(per_stream.items()))
            + " | from " + ", ".join(f"{c} ({n})" for c, n in top))

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate GUID(s) inside the database")
    if tail:
        lines.append("  " + ", ".join(tail))

    people = [h for h in scan["handles"] if h["display_name"] != OWNER_LABEL]
    if people:
        named = scan["named_handles"]
        source = "from the backup's contacts" if scan["contacts_read"] \
            else "no address book in this target"
        lines.append(f"  handles: {len(people)} ({named} named {source}) — review "
                     f"with 'lifecrm handles list --suggest'")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:12]}…: differs "
                     f"from the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not understood: {u['count']} × {u['what']}")
    return lines

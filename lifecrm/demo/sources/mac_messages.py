"""Sample Mac Messages: the persona's texts as a MacBook's ~/Library/Messages.

Writes a fake home Library the way a Mac lays it out:

    Library/Messages/chat.db                         the Messages database
    Library/Messages/Attachments/<xx>/<nn>/<guid>/   attachment files
    Library/Application Support/AddressBook/Sources/<id>/AddressBook-v22.abcddb

and returns the chat.db path for `observe mac_messages`. The connector finds
the Contacts store from the chat.db's location, exactly as it does on a real
Mac, so the handles arrive with the persona's names.

chat.db uses the real table and column names (trimmed to what Messages
writes for these rows): message text sometimes only in `attributedBody` — an
archived NSAttributedString, encoded here from the typedstream format, as
current macOS does — tapbacks as associated messages, a group rename as a
thread event, an edit with its version history in message_summary_info, a
message waiting in Recently Deleted (chat_recoverable_message_join), and two
photos: one still on the Mac, one from the Bled trip four years ago that
lives only in iCloud now (described, not on disk).

Beyond persona.THREADS, the story details added here — the photo Alex sent
Jake from Veracruz, Jake's message from the trip, the edit, the deleted
message — agree with the persona's journal and photos.
"""

from __future__ import annotations

import hashlib
import plistlib
import sqlite3
import struct
import uuid
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import persona

APPLE_EPOCH = 978307200
_NS = uuid.UUID("6a1f5c0e-3b7d-4e42-9d51-2f0c7b8e4a10")      # demo GUID namespace
CONTACTS_SOURCE = "4F1B2C3D-DEMO-4E5F-8A9B-0C1D2E3F4A5B"

OWNER_PHONE = persona.OWNER.phone
OWNER_EMAIL = persona.OWNER.email


def guid(*parts) -> str:
    """A stable message/chat/attachment GUID for one piece of the story."""
    return str(uuid.uuid5(_NS, "/".join(str(p) for p in parts))).upper()


def apple_ns(dt: datetime) -> int:
    """Nanoseconds since 2001-01-01 UTC — how current Messages stores dates."""
    return (int(dt.astimezone(timezone.utc).timestamp()) - APPLE_EPOCH) * 1_000_000_000


# ---------------------------------------------------------------- typedstream

_HEADER = bytes([0x04, 0x0B]) + b"streamtyped" + bytes([0x81, 0xE8, 0x03])


def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x10000:
        return b"\x81" + n.to_bytes(2, "little")
    return b"\x82" + n.to_bytes(4, "little")


def attributed_body(text: str) -> bytes:
    """An archived NSAttributedString holding `text`, shaped like the blob
    Messages writes to attributedBody (attribute runs omitted)."""
    raw = text.encode("utf-8")
    return (_HEADER
            + b"\x84\x01@"                                   # type "@"
            + b"\x84"                                        # new object
            + b"\x84\x84\x12NSAttributedString\x00"          # its class
            + b"\x84\x84\x08NSObject\x00\x85"                # superclass, end
            + b"\x92"                                        # field type "@"
            + b"\x84"                                        # new object
            + b"\x84\x84\x08NSString\x01"                    # its class
            + b"\x94"                                        # super: NSObject
            + b"\x84\x01+"                                   # field type "+"
            + _length(len(raw)) + raw
            + b"\x86"                                        # end NSString
            + b"\x86")                                       # end attributed string


# ---------------------------------------------------------------- files

def tiny_png(rgb: tuple[int, int, int], size: int = 8) -> bytes:
    """A valid solid-colour PNG — a stand-in photo a viewer can open."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    row = b"\x00" + bytes(rgb) * size
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * size, 9))
            + chunk(b"IEND", b""))


def _attachment_dir(att_guid: str) -> str:
    """Attachments/<2 hex>/<2 digits>/<guid>, as Messages fans them out."""
    h = hashlib.sha1(att_guid.encode()).hexdigest()
    return f"Attachments/{h[:2]}/{int(h[2:4], 16) % 100:02d}/{att_guid}"


# ---------------------------------------------------------------- contacts

_ABCDDB_SCHEMA = """
CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZUNIQUEID VARCHAR, ZFIRSTNAME VARCHAR, ZLASTNAME VARCHAR, ZMIDDLENAME VARCHAR,
  ZNICKNAME VARCHAR, ZORGANIZATION VARCHAR, ZJOBTITLE VARCHAR, ZBIRTHDAY TIMESTAMP,
  ZCREATIONDATE TIMESTAMP, ZMODIFICATIONDATE TIMESTAMP, ZNOTE INTEGER);
CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
  Z_OPT INTEGER, ZOWNER INTEGER, ZORDERINGINDEX INTEGER, ZFULLNUMBER VARCHAR,
  ZLABEL VARCHAR);
CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
  Z_OPT INTEGER, ZOWNER INTEGER, ZORDERINGINDEX INTEGER, ZADDRESS VARCHAR,
  ZLABEL VARCHAR);
CREATE TABLE ZABCDNOTE (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZCONTACT INTEGER, ZTEXT VARCHAR);
"""


def _national(e164: str) -> str:
    """'+15125550142' -> '(512) 555-0142' — how a card usually spells it."""
    d = e164[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}"


def write_address_book(app_support: Path) -> Path:
    """The persona's cards as one iCloud Contacts store under
    `<app_support>/AddressBook/Sources/`. Deterministic, so a second writer
    (the call-history sample) can produce the same file in the same home."""
    path = app_support / "AddressBook" / "Sources" / CONTACTS_SOURCE / "AddressBook-v22.abcddb"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_ABCDDB_SCHEMA)
        for pk, p in enumerate((persona.OWNER, *persona.PEOPLE), start=1):
            nickname = "Mom" if p.key == "mom" else None
            conn.execute(
                "INSERT INTO ZABCDRECORD (Z_PK, Z_ENT, Z_OPT, ZUNIQUEID, ZFIRSTNAME, "
                "ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZCREATIONDATE, ZMODIFICATIONDATE) "
                "VALUES (?, 22, 1, ?, ?, ?, ?, ?, ?, ?)",
                (pk, f"{guid('card', p.key)}:ABPerson", p.first, p.last, nickname,
                 p.org, 600000000.0 + pk, 780000000.0 + pk))
            if p.phone:
                conn.execute("INSERT INTO ZABCDPHONENUMBER (Z_ENT, Z_OPT, ZOWNER, "
                             "ZORDERINGINDEX, ZFULLNUMBER, ZLABEL) VALUES "
                             "(25, 1, ?, 0, ?, '_$!<Mobile>!$_')", (pk, _national(p.phone)))
            if p.email:
                conn.execute("INSERT INTO ZABCDEMAILADDRESS (Z_ENT, Z_OPT, ZOWNER, "
                             "ZORDERINGINDEX, ZADDRESS, ZLABEL) VALUES "
                             "(23, 1, ?, 0, ?, '_$!<Home>!$_')", (pk, p.email))
            if p.note:
                conn.execute("INSERT INTO ZABCDNOTE (Z_ENT, Z_OPT, ZCONTACT, ZTEXT) "
                             "VALUES (24, 1, ?, ?)", (pk, p.note))
        conn.commit()
    finally:
        conn.close()
    return path


# ---------------------------------------------------------------- chat.db

_CHAT_SCHEMA = """
CREATE TABLE _SqliteDatabaseProperties (key TEXT, value TEXT, UNIQUE(key));
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE, id TEXT NOT NULL,
  country TEXT, service TEXT NOT NULL, uncanonicalized_id TEXT,
  person_centric_id TEXT, UNIQUE (id, service));
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  style INTEGER, state INTEGER, account_id TEXT, properties BLOB,
  chat_identifier TEXT, service_name TEXT, room_name TEXT, account_login TEXT,
  is_archived INTEGER DEFAULT 0, last_addressed_handle TEXT, display_name TEXT,
  group_id TEXT, is_filtered INTEGER DEFAULT 0, successful_query INTEGER,
  last_read_message_timestamp INTEGER DEFAULT 0);
CREATE TABLE chat_handle_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
  handle_id INTEGER REFERENCES handle (ROWID) ON DELETE CASCADE,
  UNIQUE(chat_id, handle_id));
CREATE TABLE chat_message_join (chat_id INTEGER REFERENCES chat (ROWID) ON DELETE CASCADE,
  message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE,
  message_date INTEGER DEFAULT 0, PRIMARY KEY (chat_id, message_id));
CREATE TABLE chat_recoverable_message_join (chat_id INTEGER REFERENCES chat (ROWID)
  ON DELETE CASCADE, message_id INTEGER REFERENCES message (ROWID) ON DELETE CASCADE,
  delete_date INTEGER, ck_sync_state INTEGER DEFAULT 0,
  PRIMARY KEY (chat_id, message_id));
CREATE TABLE deleted_messages (ROWID INTEGER PRIMARY KEY AUTOINCREMENT UNIQUE,
  guid TEXT NOT NULL);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  text TEXT, replace INTEGER DEFAULT 0, service_center TEXT,
  handle_id INTEGER DEFAULT 0, subject TEXT, country TEXT, attributedBody BLOB,
  version INTEGER DEFAULT 0, type INTEGER DEFAULT 0, service TEXT, account TEXT,
  account_guid TEXT, error INTEGER DEFAULT 0, date INTEGER, date_read INTEGER,
  date_delivered INTEGER, is_delivered INTEGER DEFAULT 0, is_finished INTEGER DEFAULT 0,
  is_emote INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0, is_empty INTEGER DEFAULT 0,
  is_delayed INTEGER DEFAULT 0, is_auto_reply INTEGER DEFAULT 0,
  is_prepared INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0,
  is_system_message INTEGER DEFAULT 0, is_sent INTEGER DEFAULT 0,
  has_dd_results INTEGER DEFAULT 0, cache_has_attachments INTEGER DEFAULT 0,
  item_type INTEGER DEFAULT 0, other_handle INTEGER DEFAULT 0, group_title TEXT,
  group_action_type INTEGER DEFAULT 0, share_status INTEGER DEFAULT 0,
  share_direction INTEGER DEFAULT 0, is_audio_message INTEGER DEFAULT 0,
  is_played INTEGER DEFAULT 0, date_played INTEGER, expressive_send_style_id TEXT,
  associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,
  associated_message_emoji TEXT, balloon_bundle_id TEXT, payload_data BLOB,
  thread_originator_guid TEXT, thread_originator_part TEXT,
  destination_caller_id TEXT, date_retracted INTEGER DEFAULT 0,
  date_edited INTEGER DEFAULT 0, message_summary_info BLOB,
  part_count INTEGER DEFAULT 0);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  created_date INTEGER DEFAULT 0, start_date INTEGER DEFAULT 0, filename TEXT,
  uti TEXT, mime_type TEXT, transfer_state INTEGER DEFAULT 0,
  is_outgoing INTEGER DEFAULT 0, user_info BLOB, transfer_name TEXT,
  total_bytes INTEGER DEFAULT 0, is_sticker INTEGER DEFAULT 0,
  sticker_user_info BLOB, attribution_info BLOB, hide_attachment INTEGER DEFAULT 0);
CREATE TABLE message_attachment_join (message_id INTEGER REFERENCES message (ROWID)
  ON DELETE CASCADE, attachment_id INTEGER REFERENCES attachment (ROWID)
  ON DELETE CASCADE, UNIQUE(message_id, attachment_id));
"""


def _story(anchor: date) -> list[dict]:
    """Every message row, in the order Messages would have written them."""
    rows = []
    for t in persona.THREADS:
        for i, (offset, hhmm, sender, text) in enumerate(t.messages):
            rows.append({"thread": t.key, "guid": guid(t.key, i), "sender": sender,
                         "text": text, "dt": persona.local_dt(anchor, offset, hhmm),
                         # current macOS often leaves `text` empty and keeps the
                         # words only in attributedBody
                         "body_only": i % 3 == 1})

    by_text = {(r["thread"], r["text"]): r for r in rows}
    birthday = by_text[("maya", "Happy birthday!! Dinner at Uchi tonight, 7pm")]
    rows.append({"thread": "maya", "guid": guid("maya", "tapback", 0), "sender": "maya",
                 "text": f"Loved “{birthday['text']}”",
                 "dt": persona.local_dt(anchor, -9, "07:46"),
                 "tapback": (2000, f"p:0/{birthday['guid']}")})
    veracruz = persona.photo_dt(next(p for p in persona.PHOTOS
                                     if p.name == "veracruz_tacos"), anchor)
    rows.append({"thread": "jake", "guid": guid("jake", "photo", 0), "sender": "me",
                 "text": "\N{OBJECT REPLACEMENT CHARACTER}",
                 "dt": veracruz + timedelta(minutes=2),
                 "attachment": ("veracruz_tacos.png", (196, 120, 64), True)})
    rowboat = persona.photo_dt(next(p for p in persona.PHOTOS
                                    if p.name == "bled_rowboat"), anchor)
    rows.append({"thread": "jake", "guid": guid("jake", "bled", 0), "sender": "jake",
                 "text": "Best day of the trip. That rowboat out to the island!",
                 "dt": rowboat.replace(hour=21, minute=15),
                 "attachment": ("bled_rowboat.png", (70, 130, 180), False)})
    rows.append({"thread": "bled_crew", "guid": guid("bled_crew", "rename", 0),
                 "sender": "me", "text": None, "dt": persona.local_dt(anchor, -16, "13:59"),
                 "rename": "Bled crew"})
    rows.append({"thread": "sam", "guid": guid("sam", "deleted", 0), "sender": "me",
                 "text": "wrong chat, ignore", "dt": persona.local_dt(anchor, -12, "06:12"),
                 "recently_deleted": persona.local_dt(anchor, -12, "06:13")})
    edited = by_text[("jake", "Veracruz at 7?")]
    edited["edit"] = ("Veracruz at 6?", persona.local_dt(anchor, -1, "12:17"))
    return sorted(rows, key=lambda r: (r["dt"], r["guid"]))


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo != "iphone":
        raise ValueError(f"mac_messages has no sample for combo '{combo}' — a Mac's "
                         f"Messages database belongs to the MacBook + iPhone setup")
    library = Path(dest) / "Library"
    messages_dir = library / "Messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    write_address_book(library / "Application Support")
    db = messages_dir / "chat.db"
    for stale in (db, db.with_name("chat.db-wal"), db.with_name("chat.db-shm")):
        if stale.exists():
            stale.unlink()

    conn = sqlite3.connect(db)
    try:
        conn.executescript(_CHAT_SCHEMA)
        conn.executemany("INSERT INTO _SqliteDatabaseProperties VALUES (?, ?)",
                         [("_ClientVersion", "17200"), ("counter_in_all", "0")])
        handle_ids = {}
        for p in persona.PEOPLE:
            if p.phone:
                cur = conn.execute("INSERT INTO handle (id, country, service, "
                                   "uncanonicalized_id) VALUES (?, 'us', 'iMessage', ?)",
                                   (p.phone, p.phone[2:]))
                handle_ids[p.key] = cur.lastrowid
        chat_ids = {}
        for t in persona.THREADS:
            group = len(t.members) > 1
            ident = f"chat{int(hashlib.sha1(t.key.encode()).hexdigest()[:12], 16)}" \
                if group else persona.person(t.members[0]).phone
            cur = conn.execute(
                "INSERT INTO chat (guid, style, state, chat_identifier, service_name, "
                "display_name, account_login, last_addressed_handle) "
                "VALUES (?, ?, 3, ?, 'iMessage', ?, ?, ?)",
                (f"iMessage;{'+' if group else '-'};{ident}", 43 if group else 45,
                 ident, t.name or "", f"E:{OWNER_EMAIL}", OWNER_PHONE))
            chat_ids[t.key] = cur.lastrowid
            for m in t.members:
                conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)",
                             (cur.lastrowid, handle_ids[m]))

        for r in _story(anchor):
            thread = next(t for t in persona.THREADS if t.key == r["thread"])
            from_me = r["sender"] == "me"
            if from_me:
                handle = 0 if len(thread.members) > 1 else handle_ids[thread.members[0]]
            else:
                handle = handle_ids[r["sender"]]
            # Work chats go through the Apple ID email; everything else the number.
            caller = OWNER_EMAIL if r["thread"] == "priya" else OWNER_PHONE
            row = {"guid": r["guid"], "handle_id": handle, "service": "iMessage",
                   "account": ("E:" if "@" in caller else "P:") + caller,
                   "destination_caller_id": caller, "date": apple_ns(r["dt"]),
                   "date_delivered": apple_ns(r["dt"]), "is_from_me": int(from_me),
                   "is_delivered": 1, "is_finished": 1, "is_sent": int(from_me),
                   "is_read": int(not from_me), "part_count": 1}
            text = r["text"]
            if r.get("edit"):
                original, at = r["edit"]
                row["date_edited"] = apple_ns(at)
                row["message_summary_info"] = plistlib.dumps({
                    "otr": {"0": {"le": len(text), "lo": 0}},
                    "ec": {"0": [{"d": apple_ns(r["dt"]) // 1_000_000_000,
                                  "t": attributed_body(original)},
                                 {"d": apple_ns(at) // 1_000_000_000,
                                  "t": attributed_body(text)}]}},
                    fmt=plistlib.FMT_BINARY)
            if r.get("rename"):
                row.update(item_type=2, group_title=r["rename"])
            elif r.get("tapback"):
                code, target = r["tapback"]
                row.update(text=text, associated_message_type=code,
                           associated_message_guid=target)
            else:
                row["attributedBody"] = attributed_body(text)
                if not r.get("body_only"):
                    row["text"] = text
            if r.get("attachment"):
                row["cache_has_attachments"] = 1
            cols = ", ".join(row)
            cur = conn.execute(f"INSERT INTO message ({cols}) VALUES "
                               f"({', '.join('?' for _ in row)})", list(row.values()))
            message_id = cur.lastrowid
            chat_id = chat_ids[r["thread"]]
            if r.get("recently_deleted"):
                conn.execute("INSERT INTO chat_recoverable_message_join "
                             "(chat_id, message_id, delete_date) VALUES (?, ?, ?)",
                             (chat_id, message_id,
                              apple_ns(r["recently_deleted"]) // 1_000_000_000))
            else:
                conn.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)",
                             (chat_id, message_id, row["date"]))
            if r.get("attachment"):
                fname, rgb, on_disk = r["attachment"]
                att_guid = guid("attachment", r["guid"])
                rel = f"{_attachment_dir(att_guid)}/{fname}"
                data = tiny_png(rgb)
                if on_disk:
                    path = messages_dir / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                cur = conn.execute(
                    "INSERT INTO attachment (guid, created_date, filename, uti, mime_type, "
                    "transfer_state, is_outgoing, transfer_name, total_bytes) "
                    "VALUES (?, ?, ?, 'public.png', 'image/png', 5, ?, ?, ?)",
                    (att_guid, apple_ns(r["dt"]) // 1_000_000_000,
                     f"~/Library/Messages/{rel}", int(from_me), fname, len(data)))
                conn.execute("INSERT INTO message_attachment_join VALUES (?, ?)",
                             (message_id, cur.lastrowid))
        conn.commit()
    finally:
        conn.close()
    return db

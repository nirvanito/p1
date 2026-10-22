"""Sample notes exports for the demo, rendered from `persona.NOTES`.

  android   a Google Takeout zip with Keep only — what takeout.google.com
            hands a Samsung owner whose notes live in Google Keep:
            Takeout/Keep/<title>.json and .html per note, plus Labels.txt
  iphone    the Mac's Apple Notes store, NoteStore.sqlite, as iCloud syncs
            the iPhone's notes into it: Core Data tables, note bodies as
            gzip-compressed protobuf in ZICNOTEDATA.ZDATA

Both carry the same four persona notes — the gift ideas for Maya, the board
deck outline, the book list, and the Bled packing list written on the trip
four years ago — plus what a real account also holds and the importer must
not keep: a note in the trash. The Mac store adds a locked note (the passport
details, saved the day the passport was renewed), which the importer counts
but cannot read.

What the persona leaves open — the hour each note was written, when it was
last edited, who it was shared with — is fixed here, consistent with the
story: the gift list's voucher is checked off the night of Maya's birthday,
the deck outline was last edited the morning Priya said to lead with the
customer story, and the packing list is shared with Jake, who was on the trip.

Deterministic for (anchor, combo): fixed zip timestamps, gzip with mtime 0,
name-derived UUIDs. The protobuf encoder below is the minimum the Notes
format needs, written from the wire-format specification.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import sqlite3
import uuid
import zipfile
from datetime import date, datetime
from pathlib import Path

from .. import persona

APPLE_EPOCH = 978307200
_NS = uuid.UUID("6f1c2a7e-3b55-4c0e-9d1a-2f4e8b7c9a10")

# key -> ((when written), (when last edited)); a 'trip' time is (trip_day,
# 'HH:MM') in Slovenia, anything else (anchor offset, 'HH:MM') at home.
_WHEN = {
    "gifts": ((-25, "21:40"), (-9, "21:30")),
    "deck_outline": ((-29, "22:15"), (-27, "09:30")),
    "books": ((-16, "19:50"), (-16, "19:52")),
    "bled_packing": (("trip", 0, "18:30"), ("trip", 4, "09:00")),
}
_SHARED_WITH = {"bled_packing": ("jake",), "deck_outline": ("priya",)}
_CHECKLISTS = {
    # item -> checked. The packing list was all packed; of the gift ideas,
    # the ceramics voucher is the one Alex gave.
    "gifts": lambda item: item.startswith("ceramics class voucher"),
    "bled_packing": lambda item: True,
}
_PINNED = {"books"}
_ARCHIVED = {"bled_packing"}
_LINKS = {"books": ({"title": "Project Hail Mary",
                     "url": "https://books.example.com/project-hail-mary"},)}
_APPLE_FOLDERS = {"personal": "Notes", "work": "Work", "travel": "Travel"}

# Not in the persona, and deliberately not kept by the importer.
_TRASHED = {"key": "groceries", "title": "Grocery list", "offset": -33, "time": "18:05",
            "body": "eggs\noat milk\ntortillas\nlimes"}
_LOCKED = {"key": "passport", "title": "Passport details", "offset": -23, "time": "10:15"}


def _dt(spec, anchor: date) -> datetime:
    if spec[0] == "trip":
        return persona.trip_dt(spec[1], spec[2], anchor)
    return persona.local_dt(anchor, spec[0], spec[1])


def _items(n: persona.NoteDoc) -> list[str]:
    """A checklist's items: the persona writes the gift list as '- ' lines and
    the packing list as one comma-separated line."""
    if "\n" in n.body:
        return [ln.lstrip("- ").strip() for ln in n.body.split("\n") if ln.strip()]
    return [x.strip() for x in n.body.split(",") if x.strip()]


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo == "android":
        return _keep_takeout(Path(dest), anchor)
    if combo == "iphone":
        return _notestore(Path(dest), anchor)
    raise ValueError(f"notes has no sample for combo '{combo}' "
                     f"(expected one of {', '.join(persona.COMBOS)})")


# ---------------------------------------------------------------- Google Keep

def _usec(dt: datetime) -> int:
    # Keep writes microseconds; a fixed sub-second part keeps the importer's
    # truncation honest.
    return int(dt.timestamp()) * 1_000_000 + 417_000


def _p(text: str) -> str:
    return ('<p dir="ltr" style="line-height:1.38;margin-top:0.0pt;margin-bottom:0.0pt;">'
            f'<span style="font-size:7.2pt;font-family:\'Google Sans\';color:#000000;'
            f'font-weight:400;">{html.escape(text)}</span></p>')


def _keep_note(n: persona.NoteDoc, anchor: date) -> dict:
    created, edited = (_dt(s, anchor) for s in _WHEN[n.key])
    note = {"color": "DEFAULT", "isTrashed": False, "isPinned": n.key in _PINNED,
            "isArchived": n.key in _ARCHIVED}
    checked = _CHECKLISTS.get(n.key)
    if checked is None:
        note["textContent"] = n.body
    note.update({"title": n.title, "userEditedTimestampUsec": _usec(edited),
                 "createdTimestampUsec": _usec(created)})
    if checked is None:
        note["textContentHtml"] = "".join(_p(ln) for ln in n.body.split("\n"))
    if n.labels:
        note["labels"] = [{"name": lb} for lb in n.labels]
    if n.key in _LINKS:
        note["annotations"] = [{"description": "", "source": "WEBLINK", **a}
                               for a in _LINKS[n.key]]
    if n.key in _SHARED_WITH:
        note["sharees"] = [{"isOwner": True, "type": "WRITER",
                            "email": persona.OWNER.email}] + [
            {"isOwner": False, "type": "WRITER", "email": persona.person(k).email}
            for k in _SHARED_WITH[n.key]]
    if checked is not None:
        note["listContent"] = [{"textHtml": _p(it), "text": it, "isChecked": checked(it)}
                               for it in _items(n)]
    return note


def _keep_html(note: dict) -> str:
    body = note.get("textContentHtml") or "".join(
        f'<li class="listitem{" checked" if it["isChecked"] else ""}">'
        f'<span class="bullet">&#9744;</span><span class="text">{html.escape(it["text"])}'
        f"</span></li>" for it in note.get("listContent", []))
    return (f'<?xml version="1.0" ?><html><head><title>{html.escape(note["title"])}'
            f'</title></head><body><div class="note DEFAULT"><div class="heading">'
            f'</div><div class="title">{html.escape(note["title"])}</div>'
            f'<div class="content">{body}</div></div></body></html>')


def _keep_takeout(dest: Path, anchor: date) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    notes = [_keep_note(n, anchor) for n in persona.NOTES]
    created = persona.local_dt(anchor, _TRASHED["offset"], _TRASHED["time"])
    notes.append({"color": "YELLOW", "isTrashed": True, "isPinned": False,
                  "isArchived": False, "textContent": _TRASHED["body"],
                  "title": _TRASHED["title"],
                  "userEditedTimestampUsec": _usec(created),
                  "createdTimestampUsec": _usec(created),
                  "textContentHtml": "".join(_p(x) for x in _TRASHED["body"].split("\n"))})
    labels = sorted({lb for n in persona.NOTES for lb in n.labels})

    out = dest / f"takeout-{anchor:%Y%m%d}T120000Z-001.zip"
    stamp = (anchor.year, anchor.month, anchor.day, 12, 0, 0)

    def put(zf, member: str, text: str) -> None:
        info = zipfile.ZipInfo(member, date_time=stamp)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        zf.writestr(info, text.encode("utf-8"))

    with zipfile.ZipFile(out, "w") as zf:
        put(zf, "Takeout/archive_browser.html",
            "<!DOCTYPE html><html><head><title>Your Google data archive</title></head>"
            "<body><h1>Archive for " + html.escape(persona.OWNER.email) + "</h1>"
            "<p>Keep: your notes and lists.</p></body></html>")
        for note in sorted(notes, key=lambda x: x["title"]):
            put(zf, f"Takeout/Keep/{note['title']}.json",
                json.dumps(note, ensure_ascii=False, separators=(",", ":")))
            put(zf, f"Takeout/Keep/{note['title']}.html", _keep_html(note))
        put(zf, "Takeout/Keep/Labels.txt", "".join(f"{lb}\n" for lb in labels))
    return out


# ---------------------------------------------------------------- Apple Notes

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _len_field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _int_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def note_data(text: str) -> bytes:
    """ZDATA for a note: NoteStoreProto{document(2): Document{version(2),
    note(3): Note{note_text(2), attribute_run(5)}}}, gzip-compressed."""
    run = _int_field(1, len(text))                 # AttributeRun.length
    note = _len_field(2, text.encode("utf-8")) + _len_field(5, run)
    document = _int_field(2, 0) + _len_field(3, note)
    return gzip.compress(_len_field(2, document), mtime=0)


def _core_data(dt: datetime) -> float:
    return dt.timestamp() - APPLE_EPOCH + 0.25


def _ident(key: str) -> str:
    return str(uuid.uuid5(_NS, f"note:{key}")).upper()


_SCHEMA = """
CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR,
    Z_SUPER INTEGER, Z_MAX INTEGER);
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255),
    Z_PLIST BLOB);
CREATE TABLE ZICCLOUDSYNCINGOBJECT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
    Z_OPT INTEGER, ZIDENTIFIER VARCHAR, ZMARKEDFORDELETION INTEGER,
    ZNEEDSINITIALFETCHFROMCLOUD INTEGER, ZISPASSWORDPROTECTED INTEGER,
    ZISPINNED INTEGER, ZFOLDERTYPE INTEGER, ZACCOUNT3 INTEGER, ZFOLDER INTEGER,
    ZNOTEDATA INTEGER, ZOWNER INTEGER, ZPARENT INTEGER, ZCREATIONDATE1 TIMESTAMP,
    ZCREATIONDATE3 TIMESTAMP, ZMODIFICATIONDATE1 TIMESTAMP, ZNAME VARCHAR,
    ZSNIPPET VARCHAR, ZTITLE1 VARCHAR, ZTITLE2 VARCHAR);
CREATE TABLE ZICNOTEDATA (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
    ZNOTE INTEGER, ZDATA BLOB, ZCRYPTOINITIALIZATIONVECTOR BLOB, ZCRYPTOTAG BLOB);
"""
_ENT = {"ICCloudSyncingObject": 7, "ICAccount": 8, "ICFolder": 11, "ICNote": 12,
        "ICNoteData": 19}


def _apple_text(n: persona.NoteDoc) -> str:
    # The first line of an Apple note is its title. The deck outline carries
    # a pasted chart, which the text marks with an object-replacement char.
    if n.key == "deck_outline":
        lines = n.body.split("\n")
        return "\n".join([n.title, lines[0], "\N{OBJECT REPLACEMENT CHARACTER}", *lines[1:]])
    return f"{n.title}\n{n.body}"


def _notestore(dest: Path, anchor: date) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "NoteStore.sqlite"
    for p in (out, out.with_name(out.name + "-wal"), out.with_name(out.name + "-shm"),
              out.with_name(out.name + "-journal")):
        if p.exists():
            p.unlink()
    c = sqlite3.connect(out)
    try:
        c.executescript(_SCHEMA)
        c.executemany("INSERT INTO Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX) "
                      "VALUES (?, ?, ?, 0)",
                      [(e, n, 0 if n == "ICCloudSyncingObject" else 7)
                       for n, e in _ENT.items()])
        c.execute("INSERT INTO Z_METADATA (Z_VERSION, Z_UUID, Z_PLIST) VALUES (1, ?, NULL)",
                  (str(uuid.uuid5(_NS, "notestore")).upper(),))
        obj = ("INSERT INTO ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, Z_OPT, ZIDENTIFIER, "
               "ZMARKEDFORDELETION, ZNEEDSINITIALFETCHFROMCLOUD, ZISPASSWORDPROTECTED, "
               "ZISPINNED, ZFOLDERTYPE, ZACCOUNT3, ZFOLDER, ZNOTEDATA, ZOWNER, ZPARENT, "
               "ZCREATIONDATE1, ZCREATIONDATE3, ZMODIFICATIONDATE1, ZNAME, ZSNIPPET, "
               "ZTITLE1, ZTITLE2) VALUES (?, ?, 1, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, NULL, "
               "?, ?, ?, ?, ?, ?, ?)")
        start = _core_data(persona.local_dt(anchor, -60, "09:00"))
        c.execute(obj, (1, _ENT["ICAccount"], str(uuid.uuid5(_NS, "account")).upper(),
                        0, 0, None, None, None, None, None, start, None, start,
                        "iCloud", None, None, None))
        folders = {}
        for pk, (title, ftype) in enumerate(
                (("Notes", 0), ("Work", 0), ("Travel", 0), ("Recently Deleted", 1)), start=2):
            folders[title] = pk
            c.execute(obj, (pk, _ENT["ICFolder"], _ident(f"folder:{title}"), 0, 0,
                            ftype, None, None, None, 1, start, None, start, None,
                            None, None, title))

        notes = []
        for n in persona.NOTES:
            created, edited = (_dt(s, anchor) for s in _WHEN[n.key])
            text = _apple_text(n)
            folder = _APPLE_FOLDERS.get(n.labels[0] if n.labels else "", "Notes")
            notes.append((n.key, n.title, text, folder, created, edited,
                          n.key in _PINNED, False))
        trashed = persona.local_dt(anchor, _TRASHED["offset"], _TRASHED["time"])
        notes.append((_TRASHED["key"], _TRASHED["title"],
                      f"{_TRASHED['title']}\n{_TRASHED['body']}", "Recently Deleted",
                      trashed, trashed, False, False))
        locked = persona.local_dt(anchor, _LOCKED["offset"], _LOCKED["time"])
        notes.append((_LOCKED["key"], _LOCKED["title"], None, "Notes", locked, locked,
                      False, True))

        for i, (key, title, text, folder, created, edited, pinned, is_locked) in \
                enumerate(notes):
            pk, data_pk = 20 + i, 100 + i
            snippet = None if is_locked else (text.split("\n", 2) + ["", ""])[1][:60]
            c.execute(obj, (pk, _ENT["ICNote"], _ident(key), int(is_locked), int(pinned),
                            None, 1, folders[folder], data_pk, None, None,
                            _core_data(created), _core_data(edited), None, snippet,
                            title, None))
            if is_locked:
                # A locked note's body is ciphertext; the importer only counts it.
                blob = hashlib.sha256(f"locked:{key}".encode()).digest() * 4
                c.execute("INSERT INTO ZICNOTEDATA (Z_PK, Z_ENT, Z_OPT, ZNOTE, ZDATA, "
                          "ZCRYPTOINITIALIZATIONVECTOR, ZCRYPTOTAG) VALUES (?, ?, 1, ?, ?, ?, ?)",
                          (data_pk, _ENT["ICNoteData"], pk, blob, blob[:16], blob[16:32]))
            else:
                c.execute("INSERT INTO ZICNOTEDATA (Z_PK, Z_ENT, Z_OPT, ZNOTE, ZDATA) "
                          "VALUES (?, ?, 1, ?, ?)",
                          (data_pk, _ENT["ICNoteData"], pk, note_data(text)))
        c.commit()
    finally:
        c.close()
    return out

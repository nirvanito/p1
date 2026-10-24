"""Sample iPhone backup: the persona's texts as a Windows laptop's Apple
Devices app backs the iPhone up (the windows-iphone combo).

An iPhone has no export for its messages; on Windows the one way in is a
local, unencrypted backup. This writes one the way Apple Devices lays it out
under %USERPROFILE%\\Apple\\MobileSync\\Backup\\<device id>\\ :

    Info.plist                the device: name, model, iOS build, the phone's
                              own number, the last backup's date
    Manifest.plist            IsEncrypted false, the lockdown summary
    Status.plist              the snapshot's state
    Manifest.db               Files: (domain, relativePath) -> fileID, with
                              each file's archived MBFile record
    <fileID[:2]>/<fileID>     every backed-up file, named
                              SHA1('<domain>-<relativePath>')

and returns that folder for `observe ios_messages`. Inside it:

    HomeDomain   Library/SMS/sms.db
                 the Messages database: the Mac's chat.db schema and the very
                 same rows mac_messages writes (an iMessage GUID is the same
                 on every device), with attachment paths under ~/Library/SMS/
    HomeDomain   Library/AddressBook/AddressBook.sqlitedb
                 the phone's own address book in iOS's schema (ABPerson,
                 ABMultiValue and their label and entry tables), which names
                 the handles: the persona's cards, Alex's own, and the
                 Veracruz takeout card
    MediaDomain  Library/SMS/Attachments/<xx>/<nn>/<guid>/veracruz_tacos.png
                 the one photo still on the phone; the Bled trip's photo from
                 four years ago lives only in iCloud, described in sms.db but
                 not in the backup

No call history. Apple keeps CallHistory.storedata for *encrypted* backups,
and an encrypted backup cannot be read without its password, so an ordinary
backup has none and `observe ios_calls` refuses it with a pointer to
docs/calls.md — the ios_calls generator skips this combo for that reason.

Deterministic for (anchor, combo): fixed device ids, plists written with
sorted keys, the backup dated by the anchor.
"""

from __future__ import annotations

import hashlib
import plistlib
import shutil
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from .. import persona
from .contacts import CARD_DATES, TITLES, TYPED, VERACRUZ, _birthday, _split_city
from .mac_messages import write_messages_db

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
UDID = "00008130-001C29D41A2B801E"        # an iPhone 15 Pro's shape; fictional
DEVICE_NAME = "Alex\N{RIGHT SINGLE QUOTATION MARK}s iPhone"
PRODUCT_TYPE = "iPhone16,1"
IOS_VERSION, IOS_BUILD = "18.0", "22A3354"
SERIAL = "F4GDEMO0P3QK"
BACKUP_TIME = "07:40"                     # the morning backup, on the anchor
_NS = uuid.UUID("2f9e4c1a-7b3d-4a58-9e60-1c2d3b4a5f68")

SMS_DB = ("HomeDomain", "Library/SMS/sms.db")
ADDRESS_BOOK = ("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")


def file_id(domain: str, rel: str) -> str:
    """The name a backup gives a file: SHA1 of '<domain>-<relativePath>'."""
    return hashlib.sha1(f"{domain}-{rel}".encode("utf-8")).hexdigest()


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if not persona.has_iphone(combo):
        raise ValueError(f"ios_messages has no sample for combo '{combo}' — an iPhone "
                         f"backup belongs to the iPhone setups")
    if persona.on_mac(combo):
        raise ValueError("ios_messages has no sample for the MacBook + iPhone combo — "
                         "the Mac reads the same texts from its own chat.db "
                         "(mac_messages); one route per account")
    return write_backup(Path(dest) / "Apple" / "MobileSync" / "Backup", anchor)


# ---------------------------------------------------------------- the backup

class _Backup:
    """One backup folder being written: files go under their hashed names and
    into Manifest.db's Files table, directories into Files only."""

    def __init__(self, folder: Path, when: datetime):
        self.folder = folder
        self.stamp = int(when.timestamp())
        self.rows: dict[str, tuple[str, str, int, bytes]] = {}
        self.inode = 2_000_000

    def _record(self, domain: str, rel: str, flags: int, size: int, mode: int) -> None:
        fid = file_id(domain, rel)
        if fid in self.rows:
            return
        self.inode += 1
        self.rows[fid] = (domain, rel, flags, _mbfile(rel, size, self.stamp, mode,
                                                      self.inode))

    def _dirs(self, domain: str, rel: str) -> None:
        parts = rel.split("/")[:-1]
        for i in range(len(parts)):
            self._record(domain, "/".join(parts[:i + 1]), 2, 0, 0o40755)

    def path_for(self, domain: str, rel: str) -> Path:
        """Where a file of the device goes inside the backup."""
        fid = file_id(domain, rel)
        p = self.folder / fid[:2] / fid
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def added(self, domain: str, rel: str) -> None:
        """Record a file already written at path_for(domain, rel)."""
        self._dirs(domain, rel)
        size = self.path_for(domain, rel).stat().st_size
        self._record(domain, rel, 1, size, 0o100644)

    def put(self, domain: str, rel: str, data: bytes) -> None:
        self.path_for(domain, rel).write_bytes(data)
        self.added(domain, rel)

    def write_manifest(self) -> None:
        path = self.folder / "Manifest.db"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                "relativePath TEXT, flags INTEGER, file BLOB);"
                "CREATE INDEX FilesDomainIdx ON Files(domain);"
                "CREATE INDEX FilesRelativePathIdx ON Files(relativePath);"
                "CREATE INDEX FilesFlagsIdx ON Files(flags);"
                "CREATE TABLE Properties (key TEXT PRIMARY KEY, value BLOB);")
            for fid in sorted(self.rows, key=lambda f: (self.rows[f][0], self.rows[f][1])):
                domain, rel, flags, blob = self.rows[fid]
                conn.execute("INSERT INTO Files VALUES (?, ?, ?, ?, ?)",
                             (fid, domain, rel, flags, blob))
            conn.commit()
        finally:
            conn.close()


def _mbfile(rel: str, size: int, stamp: int, mode: int, inode: int) -> bytes:
    """Manifest.db's `file` column: an NSKeyedArchiver plist of MBFile, the
    per-file record the backup keeps (the connector never reads it)."""
    return plistlib.dumps({
        "$version": 100000, "$archiver": "NSKeyedArchiver",
        "$top": {"root": plistlib.UID(1)},
        "$objects": ["$null",
                     {"$class": plistlib.UID(3), "RelativePath": plistlib.UID(2),
                      "Size": size, "Mode": mode, "UserID": 501, "GroupID": 501,
                      "InodeNumber": inode, "Flags": 0, "ProtectionClass": 3,
                      "Birth": stamp, "LastModified": stamp, "LastStatusChange": stamp},
                     rel,
                     {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]}]},
        fmt=plistlib.FMT_BINARY)


def write_backup(root: Path, anchor: date) -> Path:
    """An unencrypted backup of the persona's iPhone under `root` (the
    MobileSync\\Backup folder); returns the backup's own folder."""
    folder = Path(root) / UDID
    if folder.exists():                   # this generator's own earlier output
        shutil.rmtree(folder)
    folder.mkdir(parents=True, exist_ok=True)
    when = persona.local_dt(anchor, 0, BACKUP_TIME)
    backup = _Backup(folder, when)

    # The Messages database first, its kept photo beside it, then the
    # address book that names its handles.
    def save_attachment(rel: str, data: bytes) -> None:
        backup.put("MediaDomain", f"Library/SMS/{rel}", data)

    sms = backup.path_for(*SMS_DB)
    write_messages_db(sms, anchor, "SMS", save_attachment)
    backup.added(*SMS_DB)
    book = backup.path_for(*ADDRESS_BOOK)
    write_ios_address_book(book, anchor)
    backup.added(*ADDRESS_BOOK)
    backup.write_manifest()

    utc = when.astimezone(timezone.utc).replace(tzinfo=None)   # plist dates are UTC
    me = persona.OWNER
    phone = f"+1 ({me.phone[2:5]}) {me.phone[5:8]}-{me.phone[8:]}"
    guid = hashlib.sha1(f"guid/{UDID}".encode()).hexdigest()[:32].upper()
    (folder / "Info.plist").write_bytes(plistlib.dumps({
        "Build Version": IOS_BUILD, "Device Name": DEVICE_NAME,
        "Display Name": DEVICE_NAME, "GUID": guid, "Last Backup Date": utc,
        "Phone Number": phone, "Product Name": persona.DEVICE["windows-iphone"]["phone"],
        "Product Type": PRODUCT_TYPE, "Product Version": IOS_VERSION,
        "Serial Number": SERIAL, "Target Identifier": UDID, "Target Type": "Device",
        "Unique Identifier": UDID}))
    (folder / "Manifest.plist").write_bytes(plistlib.dumps({
        "Date": utc, "IsEncrypted": False, "SystemDomainsVersion": "24.0",
        "Version": "10.0", "WasPasscodeSet": True,
        "Lockdown": {"BuildVersion": IOS_BUILD, "DeviceName": DEVICE_NAME,
                     "ProductType": PRODUCT_TYPE, "ProductVersion": IOS_VERSION,
                     "SerialNumber": SERIAL, "UniqueDeviceID": UDID}},
        fmt=plistlib.FMT_BINARY))
    (folder / "Status.plist").write_bytes(plistlib.dumps({
        "BackupState": "new", "Date": utc, "IsFullBackup": False,
        "SnapshotState": "finished", "UUID": str(uuid.uuid5(_NS, "status")).upper(),
        "Version": "3.3"}, fmt=plistlib.FMT_BINARY))
    return folder


# ---------------------------------------------------------------- AddressBook.sqlitedb

_AB_SCHEMA = """
CREATE TABLE ABPerson (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, First TEXT, Last TEXT,
  Middle TEXT, FirstPhonetic TEXT, MiddlePhonetic TEXT, LastPhonetic TEXT,
  Organization TEXT, Department TEXT, Note TEXT, Kind INTEGER, Birthday TEXT,
  JobTitle TEXT, Nickname TEXT, Prefix TEXT, Suffix TEXT, FirstSort TEXT,
  LastSort TEXT, CreationDate INTEGER, ModificationDate INTEGER,
  CompositeNameFallback TEXT, ExternalIdentifier TEXT, StoreID INTEGER,
  DisplayName TEXT, guid TEXT);
CREATE TABLE ABMultiValue (UID INTEGER PRIMARY KEY, record_id INTEGER,
  property INTEGER, identifier INTEGER, label INTEGER, value TEXT, guid TEXT);
CREATE TABLE ABMultiValueLabel (value TEXT, UNIQUE(value));
CREATE TABLE ABMultiValueEntryKey (value TEXT, UNIQUE(value));
CREATE TABLE ABMultiValueEntry (parent_id INTEGER, key INTEGER, value TEXT,
  UNIQUE(parent_id, key));
CREATE TABLE ABStore (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, Name TEXT,
  ExternalIdentifier TEXT, Type INTEGER, Enabled INTEGER DEFAULT 1);
"""
PHONE, EMAIL, ADDRESS, URL = 3, 4, 5, 22


def _core(dt: datetime) -> int:
    return int((dt - APPLE_EPOCH).total_seconds())


class _Book:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.labels: dict[str, int] = {}
        self.keys: dict[str, int] = {}

    def _id(self, table: str, cache: dict, value: str) -> int:
        if value not in cache:
            cur = self.conn.execute(f"INSERT INTO {table} (value) VALUES (?)", (value,))
            cache[value] = cur.lastrowid
        return cache[value]

    def value(self, record: int, prop: int, ident: int, label: str, value: str | None,
              parts: dict | None = None) -> None:
        cur = self.conn.execute(
            "INSERT INTO ABMultiValue (record_id, property, identifier, label, value, guid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record, prop, ident, self._id("ABMultiValueLabel", self.labels, label), value,
             str(uuid.uuid5(_NS, f"mv/{record}/{prop}/{ident}")).upper()))
        for key, text in (parts or {}).items():
            if text:
                self.conn.execute(
                    "INSERT INTO ABMultiValueEntry (parent_id, key, value) VALUES (?, ?, ?)",
                    (cur.lastrowid, self._id("ABMultiValueEntryKey", self.keys, key), text))


def write_ios_address_book(path: Path, anchor: date) -> Path:
    """The phone's iCloud address book: Alex's own card, the persona's people
    and the Veracruz takeout card, phone numbers as they were typed."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_AB_SCHEMA)
        conn.execute("INSERT INTO ABStore (Name, ExternalIdentifier, Type) VALUES "
                     "('iCloud', ?, 1)", (str(uuid.uuid5(_NS, "store")).upper(),))
        book = _Book(conn)

        def stamp(key: str, which: int, hhmm: str) -> int:
            return _core(persona.local_dt(anchor, CARD_DATES[key][which], hhmm))

        for p in (persona.OWNER, *persona.PEOPLE):
            bday = _birthday(p, anchor)
            birthday = (f"{_core(datetime(bday.year, bday.month, bday.day, 12, tzinfo=timezone.utc)):.6f}"
                        if bday else None)
            cur = conn.execute(
                "INSERT INTO ABPerson (First, Last, Organization, Note, Kind, Birthday, "
                "JobTitle, Nickname, Prefix, CreationDate, ModificationDate, StoreID, "
                "DisplayName, guid) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 1, NULL, ?)",
                (p.first, p.last, p.org, p.note, birthday, TITLES.get(p.key),
                 p.aliases[0] if p.aliases else None,
                 "Dr." if p.name.startswith("Dr. ") else None,
                 stamp(p.key, 0, "19:05:12"), stamp(p.key, 1, "20:41:37"),
                 str(uuid.uuid5(_NS, f"person/{p.key}")).upper()))
            rid = cur.lastrowid
            work = p.key in ("priya", "ellis")
            if p.phone:
                book.value(rid, PHONE, 0, "_$!<Work>!$_" if p.key == "ellis"
                           else "_$!<Mobile>!$_", TYPED.get(p.key, p.phone))
            if p.email:
                book.value(rid, EMAIL, 0, "_$!<Work>!$_" if work else "_$!<Home>!$_",
                           p.email)
            town, state = _split_city(p.city)
            if town:
                book.value(rid, ADDRESS, 0, "_$!<Home>!$_", None,
                           {"City": town, "State": state, "CountryCode": "us"})

        v = VERACRUZ
        cur = conn.execute(
            "INSERT INTO ABPerson (Organization, Note, Kind, CreationDate, ModificationDate, "
            "StoreID, guid) VALUES (?, ?, 1, ?, ?, 1, ?)",
            (v["org"], "Order ahead for pickup. Jake's favorite.",
             stamp("veracruz", 0, "19:05:12"), stamp("veracruz", 1, "20:41:37"),
             str(uuid.uuid5(_NS, "person/veracruz")).upper()))
        rid = cur.lastrowid
        book.value(rid, PHONE, 0, "_$!<Main>!$_", v["phone"])
        book.value(rid, ADDRESS, 0, "_$!<Work>!$_", None,
                   {"Street": v["street"], "City": v["city"], "State": v["state"],
                    "ZIP": v["zip"], "CountryCode": "us"})
        book.value(rid, URL, 0, "_$!<HomePage>!$_", v["url"])
        conn.commit()
    finally:
        conn.close()
    return path

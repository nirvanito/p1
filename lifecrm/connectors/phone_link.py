"""Phone Link connector: texts, calls and contact names from the cache that
Windows' Phone Link app keeps for a linked Android phone — nothing to install
on the phone, nothing scheduled, nothing uploaded anywhere.

Phone Link (the Microsoft.YourPhone app behind *Link to Windows*) mirrors a
linked phone's recent messages, call log, contacts and photo metadata into
SQLite databases of its own under
`%LOCALAPPDATA%\\Packages\\Microsoft.YourPhone_8wekyb3d8bbwe\\LocalCache\\
Indexed\\<device>\\System\\Database\\`: `phone.db` (SMS in `message`, MMS in
`mms` + `mms_part`, threads in `participant`, the phone's own number in
`subscription`), `calling.db` (`call_history`) and `contacts.db` (`contact`,
`phonenumber`, `emailaddress`, `postaladdress`, `contactdate`, `contacturl`).
This reader turns them into the streams the SMS Backup & Restore import
writes — `android_sms`, `android_mms`, `android_call` — and `contact_card` in
the contacts connector's shape, so every read path (ask, People, the vault,
the daily series) already understands them.

What differs from a backup, and why:

- **A window, not the store.** Phone Link keeps roughly the last month of
  messages. The archive REMEMBERS what it read: a message gone from the cache
  aged out of it, and nothing says it was deleted, so texts and calls are
  never removed here and no day is declared complete. Contact cards are the
  phone's whole address book and mirror, scoped to this phone.
- **Ids.** An SMS or a call hashes to the very id a SMS Backup & Restore
  import gives the same message (thread, millisecond, direction, text), so
  the two routes dedupe where they overlap. An MMS cannot: a backup carries
  the carrier's message id and this cache does not, so it hashes by content.
  Use one route for texts per phone (docs/phone_link.md), as the Mac's
  Messages and the iPhone backup do.
- **Names** come from `contacts.db`, the phone's own address book, as the
  backup's `contact_name` does. They label handles; nothing is linked.
- **Whose phone:** `subscription.number`, else the sender of its sent MMS,
  else `[connectors.phone_link] account`. With none, the account is the
  cache folder's id and no owner handle is attached.
- **Times** are Windows FILETIME (100 ns since 1601-01-01), read back to the
  millisecond the phone stored (an MMS to the second, as Android keeps it).

The schema is Phone Link's own and undocumented; it was read from a real
cache (2026-10) and every column this reader needs is checked first, so a
changed format is one DomainError line, never a silent misread.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import platforms
from ..config import Config
from ..domain.util import DomainError
from . import _vcard, base
from . import android_sms as an
from . import contacts as ct

name = "phone_link"
streams = ("android_sms", "android_mms", "android_call", "contact_card")
# Texts flow daily; a newest record this old means Phone Link stopped syncing
# (the phone unlinked, the app closed, Bluetooth off).
stale_after_days = 14

OWNER_LABEL = an.OWNER_LABEL
DATABASES = ("phone.db", "calling.db", "contacts.db")
# A cache folder written within this many days belongs to a phone that is
# still linked; older ones are a phone that was replaced or unlinked.
RECENT_DAYS = 14
_FILETIME_EPOCH = 116444736000000000        # 1970-01-01 in 100 ns since 1601
_ORIGIN = "phone_link"

# android.provider.ContactsContract kinds, as Phone Link stores them.
_PHONE_TYPES = {1: "home", 2: "mobile", 3: "work", 4: "fax work", 5: "fax home",
                6: "pager", 7: "other"}
_EMAIL_TYPES = {1: "home", 2: "work", 3: "other", 4: "mobile"}
_ADDRESS_TYPES = {1: "home", 2: "work", 3: "other"}
_BIRTHDAY = 3                               # Event.TYPE_BIRTHDAY

# Every column read, per table; a missing one is a format change and is
# reported by name. Tables marked optional are read when present.
_TABLES = {
    "phone.db": {
        "message": ("message_id", "thread_id", "from_address", "to_address", "type",
                    "body", "subject", "timestamp"),
        "mms": ("message_id", "thread_id", "type", "subject", "timestamp",
                "from_address", "to_addresses"),
        "mms_part": ("message_id", "sequence_num", "content_type", "text", "name"),
        "participant": ("thread_id", "send_address", "normalized_address"),
        "subscription": ("number",),
        "rcs_chat?": ("message_id", "thread_id", "from_address", "type", "body",
                      "timestamp"),
        "rcs_conversation?": ("thread_id", "recipient_list"),
    },
    "calling.db": {
        "call_history": ("phone_number", "duration", "call_type", "start_time"),
    },
    "contacts.db": {
        "contact": ("contact_id", "display_name", "nickname", "company", "job_title",
                    "notes", "name_prefix", "name_suffix", "given_name", "middle_name",
                    "family_name", "last_updated_time"),
        "phonenumber": ("contact_id", "phone_number", "phone_number_type", "label"),
        "emailaddress?": ("contact_id", "type", "label", "address"),
        "postaladdress?": ("contact_id", "type", "label", "street", "city", "region",
                           "postal_code", "country_code"),
        "contactdate?": ("contact_id", "date_type", "display_date"),
        "contacturl?": ("contact_id", "label", "url_address"),
    },
}


# ---------------------------------------------------------------- settings

def _settings(cfg: Config) -> dict:
    s = cfg.connector_settings(name)
    out: dict = {"path": None, "account": None, "contacts": True}
    if s.get("path"):
        out["path"] = cfg.resolve(str(s["path"]))
    acct = s.get("account")
    if acct not in (None, ""):
        h = base.handle_for(str(acct))
        if h is None or h[0] != "phone":
            raise DomainError(f"[connectors.phone_link] account = {acct!r} is not a phone "
                              f"number — the phone's own number, e.g. \"+15125550100\"")
        out["account"] = h[1]
    contacts = s.get("contacts", True)
    if not isinstance(contacts, bool):
        raise DomainError(f"[connectors.phone_link] contacts must be true or false, "
                          f"got {contacts!r}")
    out["contacts"] = contacts
    return out


# ---------------------------------------------------------------- finding the cache

def database_dir(folder: Path) -> Path | None:
    """The System/Database folder of a cache folder, when it holds phone.db."""
    for candidate in (folder / "System" / "Database", folder):
        try:
            if (candidate / "phone.db").is_file():
                return candidate
        except OSError:
            continue
    return None


def device_folders(root: Path | None = None) -> list[tuple[Path, float]]:
    """(cache folder, when its phone.db was last written) for every linked
    phone Phone Link keeps a cache for, newest first. Nothing is opened."""
    root = root if root is not None else platforms.phone_link_root()
    out = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return out
    for folder in entries:
        db = database_dir(folder)
        if db is None:
            continue
        stamp = 0.0
        for part in ("phone.db", "phone.db-wal"):
            try:
                stamp = max(stamp, (db / part).stat().st_mtime)
            except OSError:
                continue
        out.append((folder, stamp))
    out.sort(key=lambda d: (-d[1], d[0].name))
    return out


def default_target(cfg: Config) -> Path:
    """`[connectors.phone_link] path`, else the one cache folder Phone Link
    writes for the linked phone. With several (a replaced phone's folder
    stays), the one written in the last RECENT_DAYS days; two live ones are
    refused — two phones, and guessing would mix them."""
    settings = _settings(cfg)
    if settings["path"] is not None:
        p = settings["path"]
        if not p.exists():
            raise DomainError(f"[connectors.phone_link] path {p} does not exist — fix it "
                              f"in config.toml, or pass the cache folder")
        return p
    if not platforms.is_windows():
        raise DomainError("Phone Link is a Windows app, so this computer keeps no cache "
                          "of the phone to read — texts come from SMS Backup & Restore "
                          "(docs/android-texts.md) or the Mac's Messages, or pass a "
                          "copied Phone Link Database folder")
    root = platforms.phone_link_root()
    devices = device_folders(root)
    if not devices:
        raise DomainError(f"no Phone Link cache under {root} — link the phone in Phone "
                          f"Link (Settings › Bluetooth & devices › Mobile devices) and "
                          f"open its Messages once (docs/phone_link.md)")
    if len(devices) == 1:
        return devices[0][0]
    cutoff = time.time() - RECENT_DAYS * 86400
    live = [d for d in devices if d[1] >= cutoff]
    if len(live) == 1:
        return live[0][0]
    if not live:
        return devices[0][0]                 # the newest; the phone may just be away
    raise base.Ambiguous(
        f"{len(live)} phones' caches were written in the last {RECENT_DAYS} days: "
        + ", ".join(d[0].name for d in live)
        + " — set [connectors.phone_link] path in config.toml to the one to read, "
          "or pass it")


def _resolve(target: Path) -> tuple[Path, str]:
    """(the Database folder, the cache folder's id) for a cache folder, its
    System or Database folder, or a phone.db inside one."""
    if target.is_file():
        if target.name.lower() != "phone.db":
            raise DomainError(f"{target.name} is not Phone Link's phone.db — pass the "
                              f"cache folder (…\\Indexed\\<device>) or its phone.db")
        target = target.parent
    db = database_dir(target) or database_dir(target / "Database")
    if db is None:
        raise DomainError(f"{target} holds no Phone Link phone.db — pass the cache "
                          f"folder under …\\Indexed\\, its System\\Database folder, "
                          f"or a copied phone.db")
    device = db.parent.parent if db.parent.name.lower() == "system" else db.parent
    return db, device.name


# ---------------------------------------------------------------- reading

def _snapshot(dbdir: Path, which: str, tmp: Path) -> sqlite3.Connection | None:
    """A private copy of one database (with its -wal and -shm, so writes not
    yet checkpointed are read too), opened query-only. None when the file is
    not there (a phone whose calls Phone Link never synced)."""
    src = dbdir / which
    try:
        if not src.is_file():
            return None
        for suffix in ("", "-wal", "-shm"):
            part = src.with_name(which + suffix)
            if part.exists():
                shutil.copyfile(part, tmp / (which + suffix))
    except PermissionError as e:
        why = e.strerror or "permission denied"
        raise DomainError(f"Phone Link is holding {which} ({why}) — try again in a "
                          f"moment, or after closing Phone Link") from e
    except OSError as e:
        raise DomainError(f"cannot copy {src}: {e}") from e
    try:
        conn = sqlite3.connect(tmp / which)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        conn.execute("PRAGMA query_only = ON")
        return conn
    except sqlite3.DatabaseError as e:
        raise DomainError(f"cannot read {src} as a database: {e}") from e


def _tables(conn: sqlite3.Connection, which: str) -> dict[str, bool]:
    """{table: present} after checking every column this reader needs; a
    required table or column that is missing names Phone Link's format."""
    have = {r[0].lower(): r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    out = {}
    for spec, cols in _TABLES[which].items():
        table, optional = spec.rstrip("?"), spec.endswith("?")
        if table not in have:
            if optional:
                out[table] = False
                continue
            raise DomainError(f"Phone Link's {which} has no {table} table — its format "
                              f"changed, or this is not a Phone Link cache "
                              f"(docs/phone_link.md)")
        present = {r[1].lower() for r in conn.execute(f'PRAGMA table_info("{have[table]}")')}
        missing = [c for c in cols if c not in present]
        if missing:
            raise DomainError(f"Phone Link's {which}: {table} has no column(s) "
                              f"{', '.join(missing)} — its format changed "
                              f"(docs/phone_link.md)")
        out[table] = True
    return out


def _ms(value) -> int | None:
    """A Phone Link time as epoch milliseconds: FILETIME (100 ns since 1601),
    or milliseconds / seconds where a table keeps those. None when absent."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n >= 10**16:
        n = (n - _FILETIME_EPOCH) // 10000
    elif n < 10**11:
        n *= 1000
    return n if 0 < n < an._YEAR_3000_MS else None


def _iso(ms: int | None) -> str | None:
    return None if ms is None else base.iso_utc(ms // 1000)


def _split(value) -> list[str]:
    """A comma-separated address list as Phone Link writes it."""
    if not value:
        return []
    return [a.strip() for a in str(value).split(",") if a.strip()]


def _file_hash(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode())
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    return h.hexdigest()


class _Cache:
    """One scan over the three databases. Texts and calls go through the
    SMS Backup & Restore reader's own record builder (`android_sms._Reader`),
    so the records are that import's records; cards go through the contacts
    connector's (`contacts._records`)."""

    def __init__(self, cfg: Config, device: str, scope: str):
        self.cfg = cfg
        self.device = device
        self.scope = scope
        self.reader = an._Reader(cfg)
        self.names: dict[tuple[str, str], set[str]] = {}
        self.threads: dict[int, list[str]] = {}
        self.cards: list[dict] = []
        self.read = {"sms": 0, "mms": 0, "rcs": 0, "calls": 0, "cards": 0}
        self.calls_table = False
        self.rcs_table = False
        self.owner_from_subscription: str | None = None

    # --- names from the phone's address book ----------------------------

    def label(self, raw: str | None) -> str | None:
        h = base.handle_for(raw)
        found = self.names.get(h) if h else None
        return " / ".join(sorted(found)) if found else None

    def handle(self, raw: str | None):
        return self.reader.handle(raw, self.label(raw))

    def contacts(self, conn: sqlite3.Connection, export_at: str, keep: bool) -> None:
        tables = _tables(conn, "contacts.db")
        by_contact: dict[int, dict] = {}

        def rows(table: str):
            return conn.execute(f"SELECT * FROM {table}") if tables.get(table) else []

        for r in rows("phonenumber"):
            label = (r["label"] or "").strip() or _PHONE_TYPES.get(r["phone_number_type"])
            by_contact.setdefault(r["contact_id"], {}).setdefault("phones", []).append(
                (label, (r["phone_number"] or "").strip()))
        for r in rows("emailaddress"):
            label = (r["label"] or "").strip() or _EMAIL_TYPES.get(r["type"])
            by_contact.setdefault(r["contact_id"], {}).setdefault("emails", []).append(
                (label, (r["address"] or "").strip()))
        for r in rows("postaladdress"):
            label = (r["label"] or "").strip() or _ADDRESS_TYPES.get(r["type"])
            by_contact.setdefault(r["contact_id"], {}).setdefault("addresses", []).append(
                {"label": label, "street": r["street"], "city": r["city"],
                 "state": r["region"], "zip": r["postal_code"], "country": r["country_code"]})
        for r in rows("contactdate"):
            if r["date_type"] == _BIRTHDAY and r["display_date"]:
                by_contact.setdefault(r["contact_id"], {})["birthday"] = \
                    _vcard.parse_birthday(str(r["display_date"]))
        for r in rows("contacturl"):
            if r["url_address"]:
                by_contact.setdefault(r["contact_id"], {}).setdefault("urls", []).append(
                    ((r["label"] or "").strip() or None, r["url_address"].strip()))

        for r in conn.execute("SELECT * FROM contact"):
            extra = by_contact.get(r["contact_id"], {})
            display = (r["display_name"] or "").strip()
            if not display:
                display = " ".join(x for x in ((r["given_name"] or "").strip(),
                                               (r["middle_name"] or "").strip(),
                                               (r["family_name"] or "").strip()) if x)
            for _, raw in extra.get("phones", []):
                h = base.handle_for(raw)
                if h and display:
                    self.names.setdefault(h, set()).add(display)
            for _, raw in extra.get("emails", []):
                h = base.handle_for(raw)
                if h and display:
                    self.names.setdefault(h, set()).add(display)
            self.read["cards"] += 1
            if not keep:
                continue
            modified = _iso(_ms(r["last_updated_time"]))
            self.cards.append({
                "uid": f"phonelink:{self.scope}:{r['contact_id']}",
                "display_name": display, "nickname": (r["nickname"] or "").strip() or None,
                "org": (r["company"] or "").strip() or None,
                "job_title": (r["job_title"] or "").strip() or None,
                "note": (r["notes"] or "").strip() or None,
                "phones": extra.get("phones", []), "emails": extra.get("emails", []),
                "urls": extra.get("urls", []), "addresses": extra.get("addresses", []),
                "birthday": extra.get("birthday"),
                "modified_at": modified, "created_at": None,
                "_export": export_at, "_origin": _ORIGIN, "_file": "contacts.db"})

    # --- texts ----------------------------------------------------------

    def _members(self, thread_id) -> list[str]:
        return self.threads.get(thread_id, [])

    def _thread(self, members_raw: list[str]) -> tuple[list, str | None]:
        handles = []
        for raw in members_raw:
            h = self.handle(raw)
            if h and h not in handles:
                handles.append(h)
        return handles, ",".join(sorted(f"{k}:{v}" for k, v in handles)) or None

    def texts(self, conn: sqlite3.Connection) -> None:
        tables = _tables(conn, "phone.db")
        for r in conn.execute("SELECT * FROM participant ORDER BY id"
                              if "id" in {c[1].lower() for c in conn.execute(
                                  'PRAGMA table_info("participant")')}
                              else "SELECT * FROM participant"):
            raw = (r["normalized_address"] or r["send_address"] or "").strip()
            if raw and raw not in self.threads.setdefault(r["thread_id"], []):
                self.threads[r["thread_id"]].append(raw)
        for r in conn.execute("SELECT number FROM subscription"):
            h = base.handle_for((r["number"] or "").strip() or None)
            if h and h[0] == "phone" and not self.owner_from_subscription:
                self.owner_from_subscription = h[1]
        for r in conn.execute("SELECT * FROM message ORDER BY timestamp, message_id"):
            self.sms(r)
        parts: dict[int, list] = {}
        for p in conn.execute("SELECT message_id, sequence_num, content_type, text, name "
                              "FROM mms_part ORDER BY message_id, sequence_num, part_id"
                              if "part_id" in {c[1].lower() for c in conn.execute(
                                  'PRAGMA table_info("mms_part")')}
                              else "SELECT message_id, sequence_num, content_type, text, "
                                   "name FROM mms_part ORDER BY message_id, sequence_num"):
            parts.setdefault(p["message_id"], []).append(p)
        for r in conn.execute("SELECT * FROM mms ORDER BY timestamp, message_id"):
            self.mms(r, parts.get(r["message_id"], []))
        if tables.get("rcs_chat"):
            self.rcs_table = True
            lists = {}
            if tables.get("rcs_conversation"):
                lists = {r["thread_id"]: _split(r["recipient_list"]) for r in
                         conn.execute("SELECT thread_id, recipient_list FROM rcs_conversation")}
            for r in conn.execute("SELECT * FROM rcs_chat ORDER BY timestamp, message_id"):
                self.rcs(r, lists.get(r["thread_id"]) or self._members(r["thread_id"]))

    def sms(self, r) -> None:
        rd = self.reader
        rd.read["texts"] += 1
        self.read["sms"] += 1
        kind = r["type"]
        ms = _ms(r["timestamp"])
        direction = an._SMS_DIRECTIONS.get(kind)
        raw_addr = ((r["from_address"] if direction == "in" else r["to_address"]) or "").strip()
        if not raw_addr:
            members = self._members(r["thread_id"])
            raw_addr = members[0] if len(members) == 1 else ""
        body = an._text(r["body"])
        h = self.handle(raw_addr or None)
        thread = f"{h[0]}:{h[1]}" if h else None
        ext = an._hid("sms", thread or raw_addr, ms,
                      "out" if kind in an._SMS_UNDELIVERED else direction or kind, body)
        if kind in an._SMS_UNDELIVERED:
            rd.skip(f"{an._SMS_UNDELIVERED[kind]} (never delivered)")
            return
        if direction is None:
            rd.odd(f"sms type {kind!r}")
            return
        if ms is None:
            rd.skip("message with no usable timestamp")
            return
        if body is None:
            rd.skip("text message with no body")
            return
        participants: list[dict] = []
        rd._add(participants, h, "sender" if direction == "in" else "recipient")
        rd.record(stream="android_sms", external_id=ext, ms=ms, direction=direction,
                  body=body, detail={"thread": thread}, participants=participants,
                  owner_role="recipient" if direction == "in" else "sender",
                  origin_app="messages")

    def mms(self, r, parts: list) -> None:
        rd = self.reader
        rd.read["texts"] += 1
        self.read["mms"] += 1
        box = r["type"]
        direction = an._MMS_DIRECTIONS.get(box)
        ms = _ms(r["timestamp"])
        members_raw = self._members(r["thread_id"])
        thread_handles, thread = self._thread(members_raw)

        texts, attachments = [], []
        for p in parts:
            ctype = (p["content_type"] or "").lower()
            if ctype == "application/smil" or p["sequence_num"] == -1:
                continue                                  # slide layout, not content
            if ctype == "text/plain":
                t = an._text(p["text"])
                if t:
                    texts.append(t)
                continue
            # The media bytes (`blob`) are never read.
            attachments.append({k: v for k, v in {
                "name": (p["name"] or "").strip() or None, "mime": ctype or None}.items()
                if v})
        body = "\n".join(texts) or None
        hash_dir = "out" if box in an._MMS_UNDELIVERED else direction or box
        ext = an._hid("mms", thread or "~".join(members_raw), ms, hash_dir, body,
                      "|".join(a.get("name", "") for a in attachments))
        if box in an._MMS_UNDELIVERED:
            rd.skip(f"{an._MMS_UNDELIVERED[box]} (never delivered)")
            return
        if direction is None:
            rd.odd(f"mms type {box!r}")
            return
        if ms is None:
            rd.skip("message with no usable timestamp")
            return
        if body is None and not attachments:
            rd.skip("MMS with no text or attachment")
            return

        from_raw = (r["from_address"] or "").strip()
        to_raws = _split(r["to_addresses"])
        participants: list[dict] = []
        if direction == "out":
            mine = set()
            h = base.handle_for(from_raw) if from_raw.lower() != an.OWNER_TOKEN else None
            if h and h[0] == "phone":
                mine.add(h)
                rd.sent_from[h[1]] = rd.sent_from.get(h[1], 0) + 1
            for raw in to_raws:
                h = self.handle(raw)
                if h not in mine:
                    rd._add(participants, h, "recipient")
            if not participants:
                for h in thread_handles:
                    rd._add(participants, h, "recipient")
        else:
            sender = self.handle(from_raw or None)
            rd._add(participants, sender, "sender")
            to_numbers = set()
            for raw in to_raws:
                h = self.handle(raw)
                if h and h != sender:
                    rd._add(participants, h, "recipient")
                    if h[0] == "phone":
                        to_numbers.add(h[1])
            if to_raws:
                rd.received_to = to_numbers if rd.received_to is None \
                    else rd.received_to & to_numbers
            if sender is None and len(thread_handles) == 1:
                rd._add(participants, thread_handles[0], "sender")
        detail = {"thread": thread, "group": True if len(thread_handles) > 1 else None,
                  "attachments": attachments or None,
                  "subject": an._text(r["subject"]) if r["subject"] else None}
        rd.record(stream="android_mms", external_id=ext, ms=ms, direction=direction,
                  body=body, detail=detail, participants=participants,
                  owner_role="sender" if direction == "out" else "recipient",
                  origin_app="messages")

    def rcs(self, r, members_raw: list[str]) -> None:
        """An RCS chat row (Google Messages' chat features). Read like an SMS
        in a thread, filed as android_mms with app 'rcs', as SMS Backup &
        Restore files RCS it sees. Unverified against a real cache."""
        rd = self.reader
        rd.read["texts"] += 1
        self.read["rcs"] += 1
        kind = r["type"]
        direction = an._SMS_DIRECTIONS.get(kind)
        ms = _ms(r["timestamp"])
        body = an._text(r["body"])
        thread_handles, thread = self._thread(members_raw)
        ext = an._hid("rcs", thread or "~".join(members_raw), ms,
                      "out" if kind in an._SMS_UNDELIVERED else direction or kind, body)
        if kind in an._SMS_UNDELIVERED:
            rd.skip(f"{an._SMS_UNDELIVERED[kind]} (never delivered)")
            return
        if direction is None:
            rd.odd(f"rcs type {kind!r}")
            return
        if ms is None or body is None:
            rd.skip("RCS message with no text" if ms else "message with no usable timestamp")
            return
        participants: list[dict] = []
        if direction == "in":
            sender = self.handle((r["from_address"] or "").strip() or None)
            if sender is None and len(thread_handles) == 1:
                sender = thread_handles[0]
            rd._add(participants, sender, "sender")
            for h in thread_handles:
                if h != sender:
                    rd._add(participants, h, "recipient")
        else:
            for h in thread_handles:
                rd._add(participants, h, "recipient")
        rd.record(stream="android_mms", external_id=ext, ms=ms, direction=direction,
                  body=body, detail={"thread": thread,
                                     "group": True if len(thread_handles) > 1 else None,
                                     "app": "rcs"},
                  participants=participants,
                  owner_role="sender" if direction == "out" else "recipient",
                  origin_app="messages")

    # --- calls ----------------------------------------------------------

    def calls(self, conn: sqlite3.Connection) -> None:
        _tables(conn, "calling.db")
        self.calls_table = True
        rd = self.reader
        for r in conn.execute("SELECT * FROM call_history ORDER BY start_time"):
            rd.read["calls"] += 1
            self.read["calls"] += 1
            raw = (r["phone_number"] or "").strip()
            ms = _ms(r["start_time"])
            kind = r["call_type"]
            withheld = None
            if not raw or raw.lower() in an._NO_NUMBER:
                raw, withheld = None, "unknown"
            h = None if withheld else self.handle(raw)
            ext = an._hid("call", f"{h[0]}:{h[1]}" if h else (raw or withheld), ms)
            if kind == an._CALL_BLOCKED:
                rd.skip("blocked call (not kept)")
                continue
            if kind not in an._CALL_TYPES:
                rd.odd(f"call type {kind!r}")
                continue
            if ms is None:
                rd.skip("call with no usable timestamp")
                continue
            direction, label = an._CALL_TYPES[kind]
            try:
                duration = max(int(r["duration"] or 0), 0)
            except (TypeError, ValueError):
                duration = 0
            participants: list[dict] = []
            rd._add(participants, h, "sender" if direction == "in" else "recipient")
            rd.record(stream="android_call", external_id=ext, ms=ms, direction=direction,
                      body=None, detail={"call": label, "number": withheld},
                      participants=participants, value=duration, unit="s",
                      ended_ms=ms + duration * 1000 if duration else None,
                      owner_role="recipient" if direction == "in" else "sender",
                      origin_app="phone")


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    settings = _settings(cfg)
    dbdir, device = _resolve(target)
    scope = device[:8]
    cache = _Cache(cfg, device, scope)
    tmp = tempfile.TemporaryDirectory(prefix="lifecrm-phone-link-",
                                      ignore_cleanup_errors=True)
    present_files, hashed = [], []
    try:
        tmpdir = Path(tmp.name)
        conns = {which: _snapshot(dbdir, which, tmpdir) for which in DATABASES}
        if conns["phone.db"] is None:
            raise DomainError(f"{dbdir} holds no phone.db")
        try:
            if conns["contacts.db"] is not None:
                export_at = _iso(int((dbdir / "contacts.db").stat().st_mtime * 1000)) \
                    or base.iso_utc(time.time())
                cache.contacts(conns["contacts.db"], export_at, settings["contacts"])
            cache.texts(conns["phone.db"])
            if conns["calling.db"] is not None:
                cache.calls(conns["calling.db"])
        finally:
            for c in conns.values():
                if c is not None:
                    c.close()
        for which in DATABASES:
            if conns[which] is not None:
                present_files.append(which)
                hashed += [p for p in (tmpdir / which, tmpdir / (which + "-wal"))
                           if p.exists()]
        source_hash = _file_hash(hashed)
    finally:
        tmp.cleanup()

    derived, how = cache.reader.owner_from_backup()
    if cache.owner_from_subscription:
        owner, how = cache.owner_from_subscription, "Phone Link's subscription row"
    elif derived:
        owner = derived
    elif settings["account"]:
        owner, how = settings["account"], "config.toml"
    else:
        owner, how = None, None
    records, handles = cache.reader.finish(owner)
    built = ct._records(cache.cards, cfg) if cache.cards else \
        {"records": [], "unmapped": {}, "skipped": {}, "undated": 0, "export_dates": []}
    cards = sorted(built["records"],
                   key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    # A card's handles carry the card's name (what --suggest matches); a
    # handle also seen in texts keeps the name the texts gave it, which is
    # the same name — both come from this address book.
    seen = {(h["kind"], h["value"]) for h in handles}
    for rec in cards:
        for p in rec["participants"]:
            key = (p["kind"], p["value"])
            if key not in seen:
                seen.add(key)
                handles.append({"kind": p["kind"], "value": p["value"],
                                "display_name": cache.label(p["value"])})
    handles.sort(key=lambda h: (h["kind"], h["value"]))
    records = sorted(records + cards,
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))

    unmapped = dict(cache.reader.unmapped)
    for what, n in built["unmapped"].items():
        unmapped[what] = unmapped.get(what, 0) + n
    skipped = dict(cache.reader.skipped)
    for what, n in built["skipped"].items():
        skipped[what] = skipped.get(what, 0) + n
    texts = [r["journal_date"] for r in records if r["stream"] in an._MESSAGE_STREAMS]
    calls = [r["journal_date"] for r in records if r["stream"] == "android_call"]
    names = {(h["kind"], h["value"]): h["display_name"] for h in handles}
    facts = {
        "read_from": str(dbdir),
        "device": device,
        "databases": present_files,
        "read": dict(cache.read),
        "calls_table": cache.calls_table,
        "rcs_table": cache.rcs_table,
        "account_source": how,
        "oldest_message": min(texts) if texts else None,
        "newest_message": max(texts) if texts else None,
        "oldest_call": min(calls) if calls else None,
        "newest_call": max(calls) if calls else None,
        "cards": len(cards),
        "cards_kept": settings["contacts"],
        "normalized": [{"raw": raw, "handle": f"{h[0]}:{h[1]}", "name": names.get(h)}
                       for raw, h in sorted(cache.reader.examples.items())[:5]],
    }
    extra = {}
    if cards:
        # The address book is the phone's whole list: a card gone from it was
        # deleted there. Texts and calls are a window and are only ever added.
        extra = {"present_ids": {r["external_id"] for r in cards},
                 "mirror_streams": ["contact_card"]}
    return base.empty_scan(
        name,
        account=owner or f"phonelink:{scope}",
        source_file=f"{scope}/" + " + ".join(present_files),
        source_hash=source_hash,
        records=records,
        handles=handles,
        unmapped=[{"what": k, "count": v} for k, v in sorted(unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(skipped.items())],
        facts=facts,
        coverage=[],
        owner=owner,
        named_handles=sum(1 for h in handles if h["display_name"]
                          and h["display_name"] != OWNER_LABEL),
        **extra)


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    if rec["stream"] == "contact_card":
        return f"{rec['journal_date']} card: {rec['title']}"
    return an._sample(rec)


def _label(row) -> str | None:
    if row["stream"] == "contact_card":
        return row["title"]
    return an._thread_label(row)


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    read = facts.get("read") or {}
    lines = [f"phone-link: {scan['source_file']} (Phone Link's cache for the linked phone, "
             f"folder {facts.get('device', '?')})"]
    if facts.get("read_from"):
        lines.append(f"  read from: {facts['read_from']}")
    texts = read.get("sms", 0) + read.get("mms", 0) + read.get("rcs", 0)
    span = (f", {facts['oldest_message']}..{facts['newest_message']}"
            if facts.get("oldest_message") else "")
    lines.append(f"  texts: {texts} message row(s) read (SMS {read.get('sms', 0)}, MMS "
                 f"{read.get('mms', 0)}" + (f", RCS {read['rcs']}" if read.get("rcs") else "")
                 + f"){span} — a window of what the phone holds; remembered, never "
                   f"removed here")
    if read.get("rcs"):
        lines.append("  rcs: read from Phone Link's rcs_chat table, whose format is "
                     "unverified — check the samples' direction (docs/phone_link.md)")
    if not facts.get("calls_table"):
        lines.append("  calls: no calling.db in the cache — Phone Link is not syncing "
                     "this phone's calls")
    elif read.get("calls"):
        lines.append(f"  calls: {read['calls']} call(s) read"
                     + (f", {facts['oldest_call']}..{facts['newest_call']}"
                        if facts.get("oldest_call") else "")
                     + " — kept even once the phone's call log drops them")
    else:
        lines.append("  calls: none in the cache — Phone Link syncs calls only with the "
                     "phone paired over Bluetooth (its Calls tab); texts and contacts "
                     "come in regardless")
    if facts.get("cards_kept"):
        lines.append(f"  contacts: {facts.get('cards', 0)} card(s), the phone's address "
                     f"book — a contact deleted on the phone is removed here")
    else:
        lines.append(f"  contacts: {read.get('cards', 0)} card(s) read for names only "
                     f"([connectors.phone_link] contacts = false)")
    if scan.get("owner"):
        lines.append(f"  account: {scan['owner']} (from {facts.get('account_source')})")
    else:
        lines.append("  account: unknown — the cache names no number and no sent MMS does; "
                     "set account = \"+1…\" under [connectors.phone_link] in config.toml")
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
    if not by_stream and not plan.get("removed"):
        lines.append("  nothing new — the cache matches the database")
    lines += base.removal_lines(plan, "no longer in the phone's contacts (deleted)", _label)

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate(s) inside the cache")
    if tail:
        lines.append("  " + ", ".join(tail))
    people = [h for h in scan["handles"] if h["display_name"] != OWNER_LABEL]
    if people:
        lines.append(f"  handles: {len(people)} ({scan['named_handles']} named by the "
                     f"phone's contacts) — review with 'lifecrm handles list --suggest'")
    lines.append("  one route for texts: an MMS this cache holds and a SMS Backup & Restore "
                 "backup of the same phone count twice (docs/phone_link.md)")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:20]}…: differs "
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

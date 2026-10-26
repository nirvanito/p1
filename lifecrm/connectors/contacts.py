"""Contacts connector: the address book as observed records — and the names
the rest of the observed tier's handles are suggested by.

Texts, calls and calendar invites arrive as handles: a phone number, an email
address. The address book is where those handles have names. Imported here,
each card becomes one `contact_card` observation whose participants are every
number and address on it, and each handle carries the card's name as its
`display_name`. That name is what `handles list --suggest` matches against
your entities; linking any one handle (Jake's mobile, say) attributes the card,
and every text from that number, to Jake — with no backfill pass. The card is
never asserted as fact and never links anything itself: the observed-tier
rule, unchanged.

Accepted targets
----------------
- a vCard export (.vcf) — Google Contacts, Samsung/Android, macOS, iCloud;
  vCard 2.1, 3.0 or 4.0 (connectors/_vcard.py) — or a folder or .zip of them
  (Google Takeout).
- a Mac's Contacts folder (~/Library/Application Support/AddressBook): every
  store in it is read — the local one at the top and one per account under
  Sources/<id>/ — or one AddressBook-v22.abcddb store on its own
  (connectors/_abcddb.py).
- an iPhone backup folder (the one holding Manifest.db): its
  AddressBook.sqlitedb is located through the manifest
  (connectors/_ios_addressbook.py); also a folder holding that file, or the
  bare file.

Databases are never opened in place: each is copied through SQLite's backup
API from a read-only URI, so a store Contacts.app is writing is read as one
consistent snapshot, and its -wal is included.

What a card becomes
-------------------
- title: the name on the card (a company card: the company).
- body: the text a search should find — "Director of Product at Acme
  Robotics", "Lives in Austin, TX", "Birthday: March 14, 1993", the card's
  notes ("Grew up in El Paso...").
- value_json: {phones, emails, addresses, birthday, org, title, nickname,
  urls} as the card holds them; phones carry their normalized handle.
- participants: every phone and email on the card, role 'participant'.
- external_id: the card's own id — vCard UID (or X-ABUID), the Mac store's
  ZUNIQUEID, 'ios:<device>:<ROWID>' for an iPhone. Google and Samsung exports
  carry no id at all; there the id is derived from the name, and from the
  name plus its numbers when two cards share a name. So renaming such a card
  is a removal plus an addition, and a second "Jake Morales" re-keys both.

Dating — the card's last change, not its creation
-------------------------------------------------
started_at (and so journal_date) is when the card last changed: REV in a
vCard, the modification date in a Mac or iPhone store. A card edited today —
a new number, a note added — shows up on today, which is when that
information appeared; and the same card read from a Mac store and from an
Apple vCard export gets the same date, so the two routes agree. Creation is
the fallback when a store has no modification date. A card with no date of
its own at all (Google and Samsung write none) is dated by its export — the
file's modification time — because "as of this export" is all the source
says. A re-export of such a file therefore re-dates every card; the report
counts those separately ("only re-dated") so they do not read as edits.

Mirroring — the archive mirrors the address book (per account)
---------------------------------------------------------------
A contacts export is the whole address book, so a card deleted in Contacts
is removed here by the next import (`present_ids`; removal is the shared
write layer's, with its snapshot, mass-removal guard and cited-provenance
protection). The account that scopes it:

- a Mac's Contacts folder: 'mac-addressbook:<id>', the top-level store's
  Core Data UUID — one per Mac user account, stable across Contacts' own
  account changes. A single store passed on its own is 'mac-store:<id>' (its
  Sources folder name), so it can never remove the other stores' cards.
- an iPhone backup: 'iphone:<device id>' (the backup folder's name).
- a vCard: what its cards say in X-LIFECRM-ACCOUNT — `google:<email>`, the
  file `lifecrm connect google_contacts` fetches each morning — else what
  config.toml names, [connectors.contacts] account, because a .vcf exported
  by hand does not say whose address book it is. Without either, nothing is
  removed and the report says why.

Not imported: photos (counted; media stays out of the database), group
memberships (CATEGORIES), social profiles, related names, and any property
not listed above — each counted by name in the report.
"""

from __future__ import annotations

import hashlib
import plistlib
import re
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import _abcddb, _ios_addressbook, _vcard, base

name = "contacts"
streams = ("contact_card",)
# No stale_after_days: cards are dated by their last edit, and an address
# book can rightly go months without one — an old newest card is not a
# stopped feed.

STORE = "AddressBook-v22.abcddb"
IOS_BOOK = ("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")
_VCF = (".vcf", ".vcard")
# What follows the number proper: an extension, or dialing pauses.
_PHONE_TAIL = re.compile(r"(?i)\s*(?:;\s*ext=|\bext\.?|\bx(?=\s*\d)|[,;#]|(?<=\d)[pw])")


# ---------------------------------------------------------------- errors

def _no_access(path, e: OSError) -> DomainError:
    """macOS keeps Contacts and device backups behind privacy permissions
    (TCC); reading them without Full Disk Access fails with EPERM, which says
    nothing about the fix."""
    where = path if path else "the address book"
    if platforms.is_mac() or "/Library/" in str(path).replace("\\", "/"):
        return DomainError(
            f"cannot read {where}: permission denied — give your terminal app "
            f"(or Python) Full Disk Access in System Settings → Privacy & "
            f"Security → Full Disk Access, then re-run")
    return DomainError(f"cannot read {where}: permission denied "
                       f"({e.strerror or e}) — is another program holding it?")


# ---------------------------------------------------------------- sqlite

def _snapshot(src: Path, wal: Path | None, tmp: Path, label: str) -> sqlite3.Connection:
    """A private, consistent copy of a database, opened query-only.

    A file with a -wal but no -shm (copied out of a backup) cannot be opened
    read-only, so both halves are copied; anything else — a live store with
    Contacts.app running — goes through the backup API from a read-only URI,
    which reads it consistently, -wal included, without ever writing to it.
    """
    dest = tmp / f"{label}.db"
    sibling_wal = src.with_name(src.name + "-wal")
    if wal is None and sibling_wal.exists() and \
            not src.with_name(src.name + "-shm").exists():
        wal = sibling_wal
    try:
        if wal is not None:
            shutil.copyfile(src, dest)
            shutil.copyfile(wal, tmp / f"{label}.db-wal")
        else:
            source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True)
            try:
                copy = sqlite3.connect(dest)
                try:
                    copy.execute("PRAGMA synchronous = OFF")   # a throwaway copy
                    base.sqlite_backup(source, copy, src)      # gives up, never hangs
                finally:
                    copy.close()
            finally:
                source.close()
        conn = sqlite3.connect(dest)
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        except sqlite3.DatabaseError:
            conn.close()           # an open copy would pin the temp folder
            raise
        return conn
    except sqlite3.DatabaseError as e:
        if "unable to open" in str(e) and (
                platforms.is_mac() or "/Library/" in src.as_posix()):
            raise _no_access(src, e) from e
        raise DomainError(f"cannot read {src.name} as a database: {e}") from e


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _hash_files(paths) -> str:
    h = hashlib.sha256()
    for p in paths:
        if p is not None and p.exists():
            h.update(base.file_hash(p).encode())
    return h.hexdigest()


def _mtime_iso(path: Path) -> str:
    return base.iso_utc(int(path.stat().st_mtime))


# ---------------------------------------------------------------- vCards

def _vcard_paths(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*")
                  if p.suffix.lower() in _VCF and p.is_file())


def _read_vcards(target: Path, cfg: Config) -> dict:
    if target.is_dir():
        paths = _vcard_paths(target)
        files = [(p.relative_to(target).as_posix(), p.read_bytes(), _mtime_iso(p))
                 for p in paths]
        digest = hashlib.sha256()
        for rel, raw, _ in files:
            digest.update(rel.encode() + b"\0" + hashlib.sha256(raw).digest())
        source_hash = digest.hexdigest()
    elif zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as zf:
            members = sorted(m for m in zf.namelist() if Path(m).suffix.lower() in _VCF)
            if not members:
                listing = ", ".join(zf.namelist()[:10]) or "(empty)"
                raise DomainError(f"{target.name} holds no .vcf file (members: {listing})")
            stamp = _mtime_iso(target)
            files = [(m, zf.read(m), stamp) for m in members]
        source_hash = base.file_hash(target)
    else:
        files = [(target.name, target.read_bytes(), _mtime_iso(target))]
        source_hash = base.file_hash(target)

    cards, unmapped, versions, origins = [], {}, {}, set()
    photos = unterminated = 0
    for fname, raw, stamp in files:
        try:
            parsed = _vcard.parse(raw, fname)
        except _vcard.VCardError as e:
            # Fail the whole import: a file that cannot be read would
            # otherwise look like every card in it had been deleted.
            raise DomainError(str(e)) from e
        prodid = (parsed["prodid"] or "").lower()
        origin = ("contacts" if "apple" in prodid
                  else "google_contacts" if "google" in prodid or "google" in parsed["hints"]
                  else "android_contacts" if "android" in parsed["hints"]
                  else "vcard")
        origins.add(origin)
        for card in parsed["cards"]:
            card.update(_origin=origin, _export=stamp, _file=fname)
            if card.get("bday_raw"):
                unmapped["BDAY that is not a date"] = \
                    unmapped.get("BDAY that is not a date", 0) + 1
            cards.append(card)
        for prop, n in parsed["unmapped"].items():
            unmapped[prop] = unmapped.get(prop, 0) + n
        for v, n in parsed["versions"].items():
            versions[v] = versions.get(v, 0) + n
        photos += parsed["photos"]
        unterminated += parsed["unterminated"]
    if unterminated:
        unmapped["card without END:VCARD (file cut short?) — read as far as it goes"] = \
            unterminated
    # Whose address book: what every card says (X-LIFECRM-ACCOUNT, written
    # by the Google Contacts fetcher), else what config.toml says. Cards that
    # disagree, or only some saying, mirror nothing unless config names one.
    stated = {c.get("account") for c in cards}
    account = (next(iter(stated)) if len(stated) == 1 and None not in stated
               else cfg.connector_settings(name).get("account"))
    detail = ("vCard " + "/".join(sorted(versions)) if len(files) == 1
              else f"{len(files)} vCard file(s)")
    return {"kind": "vcard", "cards": cards, "unmapped": unmapped,
            "skipped": {"contact photo (media stays out of the database)": photos}
            if photos else {},
            "account": str(account) if account else None,
            "source_file": target.name, "source_hash": source_hash,
            "detail": detail + ", " + "/".join(sorted(origins)),
            "facts": {"files": len(files), "versions": versions}}


# ---------------------------------------------------------------- Mac stores

def _store_id(store: Path) -> str:
    return store.parent.name if store.parent.parent.name == "Sources" else "local"


def _core_data_uuid(conn: sqlite3.Connection) -> str | None:
    """Every Core Data store records its own UUID in Z_METADATA."""
    if "Z_METADATA" not in _tables(conn):
        return None
    row = conn.execute("SELECT Z_UUID FROM Z_METADATA LIMIT 1").fetchone()
    return str(row[0]) if row and row[0] else None


def _mac_affixes(conn: sqlite3.Connection) -> dict[str, tuple]:
    """(prefix, suffix) per card — 'Dr.', 'Jr.' — which Contacts shows as part
    of the name (ZTITLE is the prefix; the job title is ZJOBTITLE)."""
    rc = _columns(conn, "ZABCDRECORD")
    if not {"ZTITLE", "ZSUFFIX"} & rc:
        return {}
    pick = ", ".join(c if c in rc else "NULL" for c in ("ZUNIQUEID", "ZTITLE", "ZSUFFIX"))
    return {(uid or f"pk:{pk}"): (pre, suf) for pk, uid, pre, suf in conn.execute(
        f"SELECT Z_PK, {pick} FROM ZABCDRECORD") if pre or suf}


def _with_affixes(card: dict, prefix, suffix) -> None:
    named = " ".join(x for x in (card.get("first"), card.get("middle"),
                                 card.get("last")) if x)
    if named and card.get("display_name") == named:
        card["display_name"] = " ".join(x for x in (prefix, named, suffix) if x)


def _mac_urls(conn: sqlite3.Connection) -> dict[str, list]:
    """URLs per card; _abcddb reads everything else."""
    if "ZABCDURLADDRESS" not in _tables(conn):
        return {}
    uc, rc = _columns(conn, "ZABCDURLADDRESS"), _columns(conn, "ZABCDRECORD")
    if not {"ZOWNER", "ZURL"} <= uc:
        return {}
    uniq = "r.ZUNIQUEID" if "ZUNIQUEID" in rc else "NULL"
    label = "u.ZLABEL" if "ZLABEL" in uc else "NULL"
    out: dict[str, list] = {}
    for pk, uid, lab, url in conn.execute(
            f"SELECT r.Z_PK, {uniq}, {label}, u.ZURL FROM ZABCDURLADDRESS u "
            f"JOIN ZABCDRECORD r ON r.Z_PK = u.ZOWNER WHERE u.ZURL IS NOT NULL "
            f"ORDER BY u.Z_PK"):
        out.setdefault(uid or f"pk:{pk}", []).append((_abcddb.clean_label(lab), str(url)))
    return out


def _mac_stores(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    stores = [target / STORE] if (target / STORE).exists() else []
    if (target / "Sources").is_dir():
        stores += sorted((target / "Sources").glob(f"*/{STORE}"))
    return stores


def _read_mac(target: Path, tmp: Path) -> dict:
    stores = _mac_stores(target)
    cards, store_ids, account = [], [], None
    for i, store in enumerate(stores):
        sid = _store_id(store)
        conn = _snapshot(store, None, tmp, f"store{i}")
        try:
            if not _abcddb.is_abcddb(conn):
                raise DomainError(f"{store.name} in {store.parent.name} is not a "
                                  f"Contacts store (no ZABCDRECORD table)")
            urls, affixes = _mac_urls(conn), _mac_affixes(conn)
            if target.is_dir() and store == target / STORE:
                uuid = _core_data_uuid(conn)
                account = f"mac-addressbook:{uuid}" if uuid else None
            elif target.is_file():
                if sid != "local":
                    account = f"mac-store:{sid}"
                else:
                    uuid = _core_data_uuid(conn)
                    account = f"mac-store:{uuid}" if uuid else None
            found = _abcddb.read_contacts(conn)
        finally:
            conn.close()
        stamp = _mtime_iso(store)
        for card in found:
            card["urls"] = urls.get(card["uid"], [])
            if card["uid"] in affixes:
                _with_affixes(card, *affixes[card["uid"]])
            if card["uid"].startswith("pk:"):         # no ZUNIQUEID: per-store row id
                card["uid"] = f"{sid}/{card['uid']}"
            card.update(_origin="contacts", _export=stamp, _file=sid)
            cards.append(card)
        store_ids.append(sid)
    return {"kind": "mac", "cards": cards, "unmapped": {}, "skipped": {},
            "account": account,
            "source_file": target.name if target.is_dir()
            else f"{target.parent.name}/{target.name}",
            "source_hash": _hash_files(p for s in stores
                                       for p in (s, s.with_name(s.name + "-wal"))),
            "detail": f"Mac Contacts, {len(stores)} store(s): {', '.join(store_ids)}",
            "facts": {"stores": store_ids}}


# ---------------------------------------------------------------- iPhone

def _ios_backup_book(folder: Path) -> tuple[Path, Path | None]:
    """The address book and its -wal inside a backup, through Manifest.db;
    a file's name is also SHA1('<domain>-<path>'), the fallback."""
    meta = folder / "Manifest.plist"
    if meta.exists():
        try:
            encrypted = bool(plistlib.loads(meta.read_bytes()).get("IsEncrypted"))
        except Exception:
            encrypted = False
        if encrypted:
            raise DomainError(
                f"{folder.name} is an encrypted backup — its databases cannot be "
                f"read without the backup password. Make an unencrypted backup "
                f"instead (see docs/texts.md)")
    try:
        man = sqlite3.connect(f"{(folder / 'Manifest.db').resolve().as_uri()}?mode=ro",
                              uri=True)
    except sqlite3.DatabaseError as e:
        raise DomainError(f"cannot open {folder.name}/Manifest.db: {e}") from e

    def locate(domain: str, rel: str) -> Path | None:
        try:
            row = man.execute("SELECT fileID FROM Files WHERE domain=? AND "
                              "relativePath=?", (domain, rel)).fetchone()
        except sqlite3.DatabaseError:
            row = None
        file_id = row[0] if row else hashlib.sha1(f"{domain}-{rel}".encode()).hexdigest()
        p = folder / file_id[:2] / file_id
        return p if p.exists() else None

    try:
        book = locate(*IOS_BOOK)
        wal = locate(IOS_BOOK[0], IOS_BOOK[1] + "-wal")
    finally:
        man.close()
    if book is None:
        raise DomainError(f"{folder.name} is an iOS backup with no address book "
                          f"(Library/AddressBook/AddressBook.sqlitedb) — is this a "
                          f"partial backup?")
    return book, wal


def _ios_device(folder: Path) -> dict:
    info = folder / "Info.plist"
    try:
        data = plistlib.loads(info.read_bytes()) if info.exists() else {}
    except Exception:
        return {}
    out = {}
    if isinstance(data.get("Device Name"), str):
        out["device"] = data["Device Name"]
    if hasattr(data.get("Last Backup Date"), "strftime"):
        out["backup_date"] = data["Last Backup Date"].strftime("%Y-%m-%d")
    return out


def _read_ios(book: Path, wal: Path | None, tmp: Path, *, udid: str | None,
              label: str, device: dict) -> dict:
    conn = _snapshot(book, wal, tmp, "ios")
    try:
        if not _ios_addressbook.is_ios_addressbook(conn):
            raise DomainError(f"{book.name} is not an iPhone address book "
                              f"(no ABPerson/ABMultiValue tables; has: "
                              f"{', '.join(sorted(_tables(conn))[:10]) or 'nothing'})")
        found = _ios_addressbook.read_contacts(conn)
    finally:
        conn.close()
    stamp = _mtime_iso(book)
    for card in found:
        card["uid"] = f"ios:{udid}:{card['uid']}" if udid else f"ios:{card['uid']}"
        card.update(_origin="contacts", _export=stamp, _file=label)
    head = "iPhone backup" if udid else "iPhone address book"
    if device.get("device"):
        head += f" of {device['device']}"
    if device.get("backup_date"):
        head += f", taken {device['backup_date']}"
    return {"kind": "ios", "cards": found, "unmapped": {}, "skipped": {},
            "account": f"iphone:{udid}" if udid else None, "source_file": label,
            "source_hash": _hash_files((book, wal)), "detail": head,
            "facts": {"device": device.get("device")}}


# ---------------------------------------------------------------- dispatch

def _read(target: Path, cfg: Config, tmp: Path) -> dict:
    if target.is_dir():
        if (target / "Manifest.db").exists():
            book, wal = _ios_backup_book(target)
            return _read_ios(book, wal, tmp, udid=target.name, label=target.name,
                             device=_ios_device(target))
        if _mac_stores(target):
            return _read_mac(target, tmp)
        if (target / "AddressBook.sqlitedb").exists():
            return _read_ios(target / "AddressBook.sqlitedb", None, tmp, udid=None,
                             label=target.name, device={})
        # The MobileSync Backup folder itself — checked before looking for
        # vCards, which would walk every one of a backup's hashed files.
        backups = [d for d in sorted(target.iterdir())
                   if d.is_dir() and (d / "Manifest.db").exists()]
        if len(backups) == 1:
            return _read(backups[0], cfg, tmp)
        if backups:
            raise DomainError(f"{len(backups)} iPhone backups in {target.name} — "
                              f"pass one of them: "
                              + "; ".join(str(b) for b in backups))
        if _vcard_paths(target):
            return _read_vcards(target, cfg)
        listing = ", ".join(sorted(p.name for p in target.iterdir())[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} holds no address book — no .vcf file, no Contacts "
            f"store ({STORE}), no iPhone backup (has: {listing})")

    if target.suffix.lower() in _VCF or zipfile.is_zipfile(target):
        return _read_vcards(target, cfg)
    with open(target, "rb") as f:
        head = f.read(64)
    if head.startswith(b"SQLite format 3"):
        probe = _snapshot(target, None, tmp, "probe")
        try:
            mac = _abcddb.is_abcddb(probe)
            ios = _ios_addressbook.is_ios_addressbook(probe)
            tables = sorted(_tables(probe))
        finally:
            probe.close()
        if mac:
            return _read_mac(target, tmp)
        if ios:
            return _read_ios(target, None, tmp, udid=None, label=target.name, device={})
        raise DomainError(
            f"{target.name} is a database but not an address book — no "
            f"ZABCDRECORD (Mac Contacts) or ABPerson (iPhone) table "
            f"(has: {', '.join(tables[:10]) or 'nothing'})")
    if b"BEGIN:VCARD" in head.upper():
        return _read_vcards(target, cfg)
    raise DomainError(f"{target.name} is not a vCard (.vcf), a Contacts store or "
                      f"an iPhone address book")


# ---------------------------------------------------------------- records

def phone_handle(raw: str) -> str | None:
    """The handle value for a number as typed on a card: an extension or
    dialing pause is not part of the line a text comes from."""
    head = _PHONE_TAIL.split(raw.strip(), maxsplit=1)[0]
    h = base.handle_for(head)
    return h[1] if h and h[0] == "phone" else None


def email_handle(raw: str) -> str | None:
    h = base.handle_for(raw)
    return h[1] if h and h[0] == "email" else None


def _place(a: dict) -> str:
    locality = ", ".join(x for x in (a.get("city"), a.get("state")) if x)
    if a.get("zip"):
        locality = f"{locality} {a['zip']}".strip()
    return ", ".join(x for x in (a.get("street"), locality, a.get("country")) if x)


def _address_line(a: dict, company: bool) -> str | None:
    full = _place(a)
    if not full:
        return None
    label = (a.get("label") or "").lower()
    if company:
        return f"{a['label']} address: {full}" if a.get("label") \
            and label not in ("work", "home") else f"Address: {full}"
    if label in ("", "home"):
        return f"Lives at {full}" if a.get("street") else f"Lives in {full}"
    if label == "work":
        return f"Works at {full}" if a.get("street") else f"Works in {full}"
    return f"{a['label']} address: {full}"


def _body(card: dict, display: str) -> str | None:
    lines = []
    job, org, nick = card.get("job_title"), card.get("org"), card.get("nickname")
    company = bool(org) and not (card.get("first") or card.get("last"))
    if job and org:
        lines.append(f"{job} at {org}")
    elif org and org != display:
        lines.append(org)
    elif job:
        lines.append(job)
    if nick and nick != display:
        lines.append(f"Nickname: {nick}")
    for a in card.get("addresses") or []:
        line = _address_line(a, company)
        if line:
            lines.append(line)
    when = _vcard.birthday_text(card.get("birthday"))
    if when:
        lines.append(f"Birthday: {when}")
    if card.get("note"):
        lines.append(card["note"].strip())
    return "\n".join(lines) or None


def _detail(card: dict, phones: list, emails: list) -> dict:
    def tidy(d: dict) -> dict:
        return {k: v for k, v in d.items() if v not in (None, "", [])}
    return tidy({
        "phones": [tidy({"label": lab, "number": raw, "handle": h})
                   for lab, raw, h in phones],
        "emails": [tidy({"label": lab, "address": raw}) for lab, raw, _ in emails],
        "addresses": [tidy({k: a.get(k) for k in ("label", "street", "city",
                                                  "state", "zip", "country")})
                      for a in card.get("addresses") or []],
        "urls": [tidy({"label": lab, "url": url}) for lab, url in card.get("urls") or []],
        "birthday": card.get("birthday"), "org": card.get("org"),
        "title": card.get("job_title"), "nickname": card.get("nickname"),
    })


def _key(text: str) -> str:
    return "vcf:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _records(cards: list[dict], cfg: Config) -> dict:
    built, unmapped, skipped = [], {}, {}

    def count(bucket: dict, what: str) -> None:
        bucket[what] = bucket.get(what, 0) + 1

    for card in cards:
        # One label vocabulary before anything is derived from the card, so
        # the same card reads identically from a vCard and from a store.
        card["phones"] = [(_vcard.norm_label(lab), raw) for lab, raw in card.get("phones") or []]
        card["emails"] = [(_vcard.norm_label(lab), raw) for lab, raw in card.get("emails") or []]
        card["urls"] = [(_vcard.norm_label(lab), url) for lab, url in card.get("urls") or []]
        card["addresses"] = [{**a, "label": _vcard.norm_label(a.get("label"))}
                             for a in card.get("addresses") or []]
        phones, emails = [], []
        for lab, raw in card.get("phones") or []:
            h = phone_handle(raw)
            if h is None:
                count(unmapped, "phone value that is not a number (kept on the card, no handle)")
            phones.append((lab, raw, h))
        for lab, raw in card.get("emails") or []:
            h = email_handle(raw)
            if h is None:
                count(unmapped, "email value without '@' (kept on the card, no handle)")
            emails.append((lab, raw, h))
        display = (card.get("display_name") or "").strip() or next(
            (raw for _, raw, _ in emails + phones), None)
        if not display:
            count(skipped, "card with no name, number or email")
            continue
        participants, seen = [], set()
        for kind_, items in (("phone", phones), ("email", emails)):
            for _, _, h in items:
                if h and (kind_, h) not in seen:
                    seen.add((kind_, h))
                    participants.append({"kind": kind_, "value": h, "role": "participant"})
        modified, created = card.get("modified_at"), card.get("created_at")
        started = modified or created or card["_export"]
        rec = {
            "stream": "contact_card", "external_id": None,
            "journal_date": base.journal_date_for_iso(started, cfg),
            "started_at": started, "ended_at": None, "utc_offset_sec": None,
            "direction": None, "title": display, "body": _body(card, display),
            "value": None, "unit": None,
            "value_json": canonical_json(_detail(card, phones, emails)),
            "media_path": None, "origin_app": card["_origin"],
            "last_modified_at": modified or created or card["_export"],
            "participants": participants,
        }
        # Candidate ids, most to least preferred; only cards without an id of
        # their own have more than one.
        if card.get("uid"):
            keys = [card["uid"]]
        else:
            folded = " ".join(display.casefold().split())
            handles = "|".join(sorted(f"{p['kind']}:{p['value']}" for p in participants))
            content = hashlib.sha1(f"{rec['body']}|{rec['value_json']}".encode()).hexdigest()
            keys = [_key(folded), _key(f"{folded}|{handles}"),
                    _key(f"{folded}|{handles}|{content}")]
        built.append({"rec": rec, "keys": keys, "level": 0,
                      "undated": not (modified or created)})

    # Two different cards that share a name move to a more specific id until
    # they differ; true duplicates (the same card in two Takeout label files)
    # keep one id and are collapsed below.
    def content(b):
        r = b["rec"]
        return (r["title"], r["body"], r["value_json"], r["origin_app"])
    while True:
        groups: dict[str, list] = {}
        for b in built:
            groups.setdefault(b["keys"][b["level"]], []).append(b)
        moved = False
        for members in groups.values():
            if len(members) > 1 and len({content(b) for b in members}) > 1:
                for b in members:
                    if b["level"] + 1 < len(b["keys"]):
                        b["level"] += 1
                        moved = True
        if not moved:
            break
    chosen: dict[str, dict] = {}
    for b in built:
        b["rec"]["external_id"] = b["keys"][b["level"]]
        prior = chosen.get(b["rec"]["external_id"])
        if prior is None:
            chosen[b["rec"]["external_id"]] = b
        elif content(prior) == content(b):
            count(skipped, "duplicate of a card already read (same card twice)")
        else:
            count(skipped, "card sharing another card's id — kept the newer")
            if (b["rec"]["last_modified_at"] or "") > (prior["rec"]["last_modified_at"] or ""):
                chosen[b["rec"]["external_id"]] = b
    kept = list(chosen.values())
    return {"records": [b["rec"] for b in kept], "unmapped": unmapped,
            "skipped": skipped, "undated": sum(1 for b in kept if b["undated"]),
            "export_dates": sorted({b["rec"]["journal_date"] for b in kept
                                    if b["undated"]})}


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    tmp = tempfile.TemporaryDirectory(prefix="lifecrm-contacts-",
                                      ignore_cleanup_errors=True)
    try:
        if not target.exists():
            raise DomainError(f"not found: {target}")
        src = _read(target, cfg, Path(tmp.name))
    except PermissionError as e:
        raise _no_access(e.filename or target, e) from e
    finally:
        tmp.cleanup()
    built = _records(src["cards"], cfg)
    records = sorted(built["records"],
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))

    names: dict[tuple[str, str], set[str]] = {}
    for rec in records:
        for p in rec["participants"]:
            names.setdefault((p["kind"], p["value"]), set()).add(rec["title"])
    # A number on two cards keeps both names — "Dad / Mom" is the honest
    # label for a shared landline, and picking one would be a guess.
    handles = [{"kind": k, "value": v, "display_name": " / ".join(sorted(n))}
               for (k, v), n in sorted(names.items())]
    unmapped = {**src["unmapped"]}
    for what, n in built["unmapped"].items():
        unmapped[what] = unmapped.get(what, 0) + n
    skipped = {**src["skipped"]}
    for what, n in built["skipped"].items():
        skipped[what] = skipped.get(what, 0) + n
    return base.empty_scan(
        name,
        account=src["account"],
        source_file=src["source_file"],
        source_hash=src["source_hash"],
        records=records,
        handles=handles,
        unmapped=[{"what": k, "count": v} for k, v in sorted(unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(skipped.items())],
        # The export is the whole address book, so the archive mirrors it —
        # scoped to the account, which only some sources can name.
        present_ids={r["external_id"] for r in records},
        mirror_streams=list(streams),
        source_kind=src["kind"],
        source_detail=src["detail"],
        cards=len(records),
        undated=built["undated"],
        export_dates=built["export_dates"],
        facts={**src["facts"], "cards": len(records), "undated": built["undated"]})


# ---------------------------------------------------------------- default target

def default_target(cfg: Config) -> Path:
    """config.toml's [connectors.contacts] path when set; else a Mac's
    Contacts folder; else the newest .vcf in Downloads — except when a vCard
    account is configured: then deletions mirror, and the file to mirror is
    never picked by guesswork."""
    settings = cfg.connector_settings(name)
    configured = settings.get("path")
    if configured:
        p = Path(str(configured)).expanduser()
        p = p if p.is_absolute() else cfg.root / p
        if not p.exists():
            raise DomainError(f"[connectors.contacts] path {p} does not exist — fix "
                              f"config.toml, or pass the export's path")
        return p
    looked = []
    try:
        if platforms.is_mac():
            book = platforms.home() / "Library" / "Application Support" / "AddressBook"
            looked.append(str(book))
            if book.is_dir():
                return book
        if settings.get("account"):
            raise DomainError(
                "[connectors.contacts] account is set, so this import mirrors "
                "deletions — name the export to mirror with [connectors.contacts] "
                "path, or pass its path (the newest .vcf in Downloads could be the "
                "wrong file)")
        downloads = platforms.home() / "Downloads"
        looked.append(str(downloads / "*.vcf"))
        if downloads.is_dir():
            found = [p for p in downloads.glob("*.vcf") if p.is_file()]
            if found:
                return max(found, key=lambda p: (p.stat().st_mtime, p.name))
    except PermissionError as e:
        raise _no_access(e.filename, e) from e
    raise DomainError(
        f"no address book found — looked in {'; '.join(looked)}. Export your "
        f"contacts as a .vcf (docs/contacts.md) and pass its path, or set "
        f"[connectors.contacts] path in config.toml")


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    bits = [p["value"] for p in rec["participants"][:1]]
    if rec["body"]:
        bits.append(rec["body"].splitlines()[0])
    text = "; ".join(bits)
    if len(text) > 60:
        text = text[:59] + "…"
    return f"{rec['title']}" + (f" — {text}" if text else "")


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    lines = [f"contacts: {scan['source_file']} ({scan['source_detail']}; "
             f"{scan['cards']} card(s))"]
    if scan["account"]:
        lines.append(f"  account: {scan['account']}")
    if scan["undated"]:
        lines.append(f"  dates: {scan['undated']} card(s) carry no date of their own "
                     f"— dated by the export ({', '.join(scan['export_dates'][:3])})")

    by_stream: dict[str, dict] = {}
    for rec in plan["new"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["new"].append(rec)
    redated = 0
    for _, rec, changes in plan["updates"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["upd"].append(rec)
        if set(changes) <= {"started_at", "journal_date"}:
            redated += 1
    for stream in sorted(by_stream):
        info = by_stream[stream]
        recs = info["new"] or info["upd"]
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda r: (r["started_at"], r["title"], r["external_id"]))
        lines.append(f"  {stream}: {len(info['new'])} new, {len(info['upd'])} changed "
                     f"| {span} | e.g. {_sample(latest)}")
    if redated:
        lines.append(f"  {redated} of the changed card(s) only re-dated — the "
                     f"source moved the card's date, not its contents")
    if not by_stream and not plan.get("removed"):
        lines.append("  nothing new — the address book matches the database")
    lines += base.removal_lines(plan, "no longer in the address book (deleted)",
                                lambda row: row["title"])
    if scan["source_kind"] == "vcard" and not scan["account"]:
        lines.append("  to mirror deletions from this export, name its account: "
                     "[connectors.contacts] account in config.toml (docs/contacts.md)")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate id(s) inside the export")
    if tail:
        lines.append("  " + ", ".join(tail))
    if scan["handles"]:
        kinds: dict[str, int] = {}
        for h in scan["handles"]:
            kinds[h["kind"]] = kinds.get(h["kind"], 0) + 1
        lines.append("  handles: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
                     + " — each named from its card; review with "
                       "'lifecrm handles list --suggest'")
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
    if scan["unmapped"]:
        lines.append("  not imported: " + ", ".join(
            f"{u['what']} ({u['count']})" for u in scan["unmapped"]))
    return lines

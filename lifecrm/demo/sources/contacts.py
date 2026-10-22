"""The persona's address book, as each combo's computer exports it.

  android  Google Contacts → Export → "vCard (for iOS Contacts)": one
           contacts.vcf, vCard 3.0. Google writes no UID and no REV — which is
           exactly what the connector has to cope with on a Samsung — plus
           its own quirks: `item1.TEL` + `item1.X-ABLabel` for a custom label,
           `CATEGORIES:myContacts`, and `\\:` escaped inside URLs.
  iphone   a Mac's Contacts folder (~/Library/Application Support/AddressBook)
           with the iPhone's iCloud contacts synced into it: the local store
           at the top (holding one card made on the Mac) and the iCloud
           account's store under Sources/<id>/, in the Core Data schema
           Contacts.app writes (the same columns `tests/test_platforms.py`
           make_abcddb uses, plus Z_METADATA and URLs).

Both render `persona.PEOPLE` — Jake's card carries his notes (El Paso,
Veracruz), every card its city and birthday — and add what an address book
holds beyond the story's people, consistent with it: Priya's job title, the
restaurant Alex calls for takeout, and (on the Mac) Alex's own "me" card.
Phone numbers are typed the way people type them, so normalization has work
to do. Output is deterministic for (anchor, combo); file times are pinned to
the anchor because an undated vCard is dated by its export.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from .. import persona

ICLOUD_SOURCE = "9C1A4F2E-6B7D-4E3A-8F51-2D0C7B9E4A16"
LOCAL_STORE_UUID = "3E8B1C75-0A94-4F6D-B2E7-58C1D9A04F23"
ICLOUD_STORE_UUID = "B7D24E91-3C58-4A0F-9E16-7F2A8C5D3B40"
_NS = uuid.UUID("6f1c2a9e-4b3d-4e8f-a7c5-1d2e3f405162")
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# Story details an address book holds beyond persona.PEOPLE.
TITLES = {"priya": "Director of Product", "me": "Product Manager"}
TYPED = {   # how each number was typed into the phone
    "jake": "+1 512-555-0142", "maya": "(512) 555-0187", "mom": "210-555-0110",
    "priya": "+1 (512) 555-0123", "sam": "512.555.0166", "ellis": "(512) 555-0199",
    "me": "+1 (512) 555-0100",
}
VERACRUZ = {"key": "veracruz", "org": "Veracruz All Natural",
            "phone": "(512) 555-0177", "url": "https://veracruz.example",
            "street": "1704 E Cesar Chavez St", "city": "Austin", "state": "TX",
            "zip": "78702"}
# (created, last modified) as anchor offsets in days, for the Mac store.
CARD_DATES = {"me": (-3100, -400), "jake": (-2600, -37), "maya": (-1400, -9),
              "mom": (-3100, -200), "priya": (-700, -41), "sam": (-1200, -300),
              "ellis": (-900, -14), "veracruz": (-950, -38)}


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo == "android":
        return _google_vcf(Path(dest), anchor)
    if combo == "iphone":
        return _mac_addressbook(Path(dest), anchor)
    raise ValueError(f"no contacts export for combo '{combo}' "
                     f"(expected one of {', '.join(persona.COMBOS)})")


# ---------------------------------------------------------------- the story

def _split_city(city: str | None) -> tuple[str | None, str | None]:
    if not city:
        return None, None
    town, _, state = city.partition(",")
    return town.strip() or None, state.strip() or None


def _birthday(p: persona.Person, anchor: date) -> date | None:
    """Maya's birthday falls inside the demo window (persona.birthday_date);
    the card holds that day in her birth year."""
    when = persona.birthday_date(p, anchor)
    if when is None or p.birth_year is None:
        return None
    try:
        return when.replace(year=p.birth_year)
    except ValueError:            # Feb 29 into a non-leap year
        return when.replace(year=p.birth_year, day=28)


def _pinned(path: Path, anchor: date) -> None:
    """The export's file time: noon of the anchor, local — the moment this
    export was made, as far as anyone reading the file can tell."""
    noon = persona.local_dt(anchor, 0, "12:00").timestamp()
    os.utime(path, (noon, noon))


# ---------------------------------------------------------------- Google vCard

def _esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _fold(line: str) -> list[str]:
    """RFC 6350 folding: at most 75 octets a line, never inside a UTF-8 character."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            out.append(cur)
            cur, size = " ", 1
        cur += ch
        size += n
    out.append(cur)
    return out


def _google_card(p: persona.Person, anchor: date) -> list[str]:
    town, state = _split_city(p.city)
    lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{_esc(p.name)}"]
    prefix = "Dr." if p.name.startswith("Dr. ") else ""
    lines.append(f"N:{_esc(p.last)};{_esc(p.first)};;{prefix};")
    if p.aliases:
        lines.append("NICKNAME:" + ",".join(_esc(a) for a in p.aliases))
    if p.email:
        kind = "WORK" if p.org and p.key in ("priya", "ellis") else "HOME"
        lines.append(f"EMAIL;TYPE=INTERNET;TYPE={kind}:{p.email}")
    if p.phone:
        if p.key == "ellis":       # a custom label: Google groups it with the number
            lines += [f"item1.TEL:{TYPED[p.key]}", "item1.X-ABLabel:Front desk"]
        else:
            lines.append(f"TEL;TYPE=CELL:{TYPED[p.key]}")
    if town:
        lines.append(f"ADR;TYPE=HOME:;;;{_esc(town)};{_esc(state or '')};;")
    if p.org:
        lines.append(f"ORG:{_esc(p.org)}")
    if TITLES.get(p.key):
        lines.append(f"TITLE:{_esc(TITLES[p.key])}")
    bday = _birthday(p, anchor)
    if bday:
        lines.append(f"BDAY:{bday.isoformat()}")
    if p.note:
        lines.append(f"NOTE:{_esc(p.note)}")
    lines.append("CATEGORIES:myContacts" + (",starred" if p.key in ("jake", "maya", "mom")
                                            else ""))
    lines.append("END:VCARD")
    return lines


def _google_vcf(dest: Path, anchor: date) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    v = VERACRUZ
    cards = [_google_card(p, anchor) for p in persona.PEOPLE]
    cards.append(["BEGIN:VCARD", "VERSION:3.0", f"FN:{v['org']}", "N:;;;;",
                  f"TEL;TYPE=WORK:{v['phone']}",
                  f"ADR;TYPE=WORK:;;{v['street']};{v['city']};{v['state']};{v['zip']};",
                  f"ORG:{v['org']}",
                  "URL:" + v["url"].replace(":", "\\:"),
                  "NOTE:Order ahead for pickup. Jake's favorite.",
                  "CATEGORIES:myContacts", "END:VCARD"])
    text = "".join(f"{piece}\r\n" for card in cards for line in card
                   for piece in _fold(line))
    out = dest / "contacts.vcf"
    out.write_bytes(text.encode("utf-8"))
    _pinned(out, anchor)
    return out


# ---------------------------------------------------------------- Mac store

SCHEMA = """
CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
    ZUNIQUEID TEXT, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZMIDDLENAME TEXT,
    ZTITLE TEXT, ZSUFFIX TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT, ZJOBTITLE TEXT,
    ZBIRTHDAY REAL, ZCREATIONDATE REAL, ZMODIFICATIONDATE REAL, ZNOTE INTEGER,
    ZNAME TEXT);
CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
    ZFULLNUMBER TEXT, ZLABEL TEXT, ZORDERINGINDEX INTEGER);
CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
    ZADDRESS TEXT, ZLABEL TEXT, ZORDERINGINDEX INTEGER);
CREATE TABLE ZABCDPOSTALADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
    ZSTREET TEXT, ZCITY TEXT, ZSTATE TEXT, ZZIPCODE TEXT, ZCOUNTRYNAME TEXT,
    ZLABEL TEXT);
CREATE TABLE ZABCDURLADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
    ZURL TEXT, ZLABEL TEXT);
CREATE TABLE ZABCDNOTE (Z_PK INTEGER PRIMARY KEY, ZCONTACT INTEGER, ZTEXT TEXT);
"""
ENT_PERSON, ENT_GROUP, ENT_CONTAINER = 22, 19, 17


def _core(dt: datetime) -> float:
    return (dt - APPLE_EPOCH).total_seconds()


def _uid(key: str) -> str:
    return str(uuid.uuid5(_NS, f"lifecrm-demo-contact-{key}")).upper() + ":ABPerson"


def _new_store(path: Path, store_uuid: str, container: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    c = sqlite3.connect(path)
    c.execute("PRAGMA synchronous = OFF")     # a generated sample, not a live store
    c.executescript(f"BEGIN; {SCHEMA} COMMIT;")
    c.execute("INSERT INTO Z_METADATA (Z_VERSION, Z_UUID) VALUES (1, ?)", (store_uuid,))
    # The account's own container record: no person fields, never a card.
    c.execute("INSERT INTO ZABCDRECORD (Z_PK, Z_ENT, ZUNIQUEID, ZNAME) VALUES (1, ?, ?, ?)",
              (ENT_CONTAINER, f"{store_uuid}:ABAccount", container))
    return c


def _add_card(c: sqlite3.Connection, anchor: date, key: str, *, first=None, last=None,
              prefix=None, nickname=None, org=None, job=None, bday: date | None = None,
              phones=(), emails=(), address: dict | None = None, url=None,
              note=None) -> None:
    pk = c.execute("SELECT COALESCE(MAX(Z_PK), 0) + 1 FROM ZABCDRECORD").fetchone()[0]
    created, modified = CARD_DATES[key]

    def stamp(offset: int, hhmm: str) -> float:
        return _core(persona.local_dt(anchor, offset, hhmm))

    birthday = _core(datetime(bday.year, bday.month, bday.day, 12, tzinfo=timezone.utc)) \
        if bday else None
    c.execute("INSERT INTO ZABCDRECORD (Z_PK, Z_ENT, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME, "
              "ZTITLE, ZNICKNAME, ZORGANIZATION, ZJOBTITLE, ZBIRTHDAY, ZCREATIONDATE, "
              "ZMODIFICATIONDATE) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (pk, ENT_PERSON, _uid(key), first, last, prefix, nickname, org, job, birthday,
               stamp(created, "19:05:12"), stamp(modified, "20:41:37")))
    for n, (label, number) in enumerate(phones):
        c.execute("INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER, ZLABEL, "
                  "ZORDERINGINDEX) VALUES (?,?,?,?)", (pk, number, label, n))
    for n, (label, addr) in enumerate(emails):
        c.execute("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS, ZLABEL, "
                  "ZORDERINGINDEX) VALUES (?,?,?,?)", (pk, addr, label, n))
    if address:
        c.execute("INSERT INTO ZABCDPOSTALADDRESS (ZOWNER, ZSTREET, ZCITY, ZSTATE, "
                  "ZZIPCODE, ZCOUNTRYNAME, ZLABEL) VALUES (?,?,?,?,?,?,?)",
                  (pk, address.get("street"), address.get("city"), address.get("state"),
                   address.get("zip"), address.get("country"), address["label"]))
    if url:
        c.execute("INSERT INTO ZABCDURLADDRESS (ZOWNER, ZURL, ZLABEL) VALUES (?,?,?)",
                  (pk, url, "_$!<HomePage>!$_"))
    if note:
        c.execute("INSERT INTO ZABCDNOTE (ZCONTACT, ZTEXT) VALUES (?,?)", (pk, note))


def _person_card(c, anchor: date, p: persona.Person) -> None:
    town, state = _split_city(p.city)
    work = p.key in ("priya", "ellis")
    _add_card(
        c, anchor, p.key, first=p.first, last=p.last,
        prefix="Dr." if p.name.startswith("Dr. ") else None,
        nickname=p.aliases[0] if p.aliases else None,
        org=p.org, job=TITLES.get(p.key), bday=_birthday(p, anchor),
        phones=[("_$!<Work>!$_" if p.key == "ellis" else "_$!<Mobile>!$_",
                 TYPED.get(p.key, p.phone))] if p.phone else [],
        emails=[("_$!<Work>!$_" if work else "_$!<Home>!$_", p.email)] if p.email else [],
        address={"label": "_$!<Home>!$_", "city": town, "state": state} if town else None,
        note=p.note)


def _mac_addressbook(dest: Path, anchor: date) -> Path:
    book = dest / "AddressBook"
    local = _new_store(book / "AddressBook-v22.abcddb", LOCAL_STORE_UUID, "On My Mac")
    v = VERACRUZ
    _add_card(local, anchor, "veracruz", org=v["org"],
              phones=[("_$!<Main>!$_", v["phone"])], url=v["url"],
              address={"label": "_$!<Work>!$_", "street": v["street"], "city": v["city"],
                       "state": v["state"], "zip": v["zip"]},
              note="Order ahead for pickup. Jake's favorite.")
    local.commit()
    local.close()

    icloud = _new_store(book / "Sources" / ICLOUD_SOURCE / "AddressBook-v22.abcddb",
                        ICLOUD_STORE_UUID, "iCloud")
    me = persona.OWNER
    _person_card(icloud, anchor, me)
    for p in persona.PEOPLE:
        _person_card(icloud, anchor, p)
    # A group record ("Bled crew"): in the same table, never a card.
    icloud.execute("INSERT INTO ZABCDRECORD (Z_ENT, ZUNIQUEID, ZNAME) VALUES (?, ?, ?)",
                   (ENT_GROUP, _uid("group-bled-crew").replace("ABPerson", "ABGroup"),
                    "Bled crew"))
    icloud.commit()
    icloud.close()
    for store in book.rglob("AddressBook-v22.abcddb"):
        _pinned(store, anchor)
    return book

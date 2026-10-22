"""Reader for an iPhone's address book (`AddressBook.sqlitedb`, from a backup).

A helper, not a connector (the leading underscore keeps it out of the
registry). It returns cards in the same shape `_abcddb.read_contacts` does for
a Mac's store, so `contacts.py` turns both into the same records.

iOS keeps its own schema, not the Mac's Core Data one (the layout
`ios_messages._contact_names` already relies on for names):

    ABPerson               ROWID, First, Last, Middle, Prefix, Suffix,
                           Nickname, Organization, JobTitle, Note, Kind,
                           Birthday, CreationDate, ModificationDate
    ABMultiValue           UID, record_id -> ABPerson.ROWID, property,
                           label -> ABMultiValueLabel.ROWID, value
                           (property 3 = phone, 4 = email, 5 = address,
                           22 = URL)
    ABMultiValueLabel      ROWID, value ('_$!<Mobile>!$_' or a custom label)
    ABMultiValueEntry      parent_id -> ABMultiValue.UID, key, value — the
                           parts of an address, keyed through
    ABMultiValueEntryKey   ROWID, value ('Street', 'City', 'State', 'ZIP',
                           'Country')

Dates are seconds since 2001-01-01 UTC, as on the Mac; a birthday without a
year sits in 1604, as on the Mac. Kind 1 is a company card. Columns vary by
iOS version, so every one is checked before it is used. Nothing here writes.
"""

from __future__ import annotations

import sqlite3

from . import _abcddb, _vcard

PHONE, EMAIL, ADDRESS, URL = 3, 4, 5, 22
_ENTRY_KEYS = {"street": "street", "city": "city", "state": "state",
               "zip": "zip", "country": "country"}


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def is_ios_addressbook(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "ABPerson") and _has_table(conn, "ABMultiValue")


def _birthday(value) -> str | None:
    """Seconds since 2001 (usually stored as text), or an ISO date string."""
    if value is None or value == "":
        return None
    try:
        return _abcddb.birthday(float(value))
    except (TypeError, ValueError):
        return _vcard.parse_birthday(str(value))


def read_contacts(conn: sqlite3.Connection) -> list[dict]:
    """Every person and company card, in ROWID order (see _abcddb.read_contacts
    for the dict shape; `uid` is the ROWID as text, and `urls` is added)."""
    if not is_ios_addressbook(conn):
        raise ValueError("not an iOS address book (no ABPerson/ABMultiValue tables)")
    pc = _columns(conn, "ABPerson")
    wanted = ("First", "Last", "Middle", "Nickname", "Organization", "JobTitle",
              "Note", "Kind", "Birthday", "CreationDate", "ModificationDate",
              "Prefix", "Suffix")
    rows = conn.execute(
        "SELECT ROWID, " + ", ".join(c if c in pc else f"NULL AS {c}" for c in wanted)
        + " FROM ABPerson ORDER BY ROWID").fetchall()

    labels: dict[int, str] = {}
    if _has_table(conn, "ABMultiValueLabel"):
        for r in conn.execute("SELECT ROWID, value FROM ABMultiValueLabel"):
            labels[r[0]] = r[1]
    keys: dict[int, str] = {}
    if _has_table(conn, "ABMultiValueEntryKey"):
        for r in conn.execute("SELECT ROWID, value FROM ABMultiValueEntryKey"):
            keys[r[0]] = str(r[1] or "").lower()
    entries: dict[int, dict] = {}
    if _has_table(conn, "ABMultiValueEntry"):
        ec = _columns(conn, "ABMultiValueEntry")
        if {"parent_id", "key", "value"} <= ec:
            for parent, key, value in conn.execute(
                    "SELECT parent_id, key, value FROM ABMultiValueEntry ORDER BY ROWID"):
                field = _ENTRY_KEYS.get(keys.get(key, ""))
                if field and value:
                    entries.setdefault(parent, {})[field] = str(value)

    mc = _columns(conn, "ABMultiValue")
    uid_col = "UID" if "UID" in mc else "NULL"
    label_col = "label" if "label" in mc else "NULL"
    order = "record_id" + (", identifier" if "identifier" in mc else "") + ", rowid"
    multi: dict[int, dict[int, list]] = {}
    for rid, prop, label, value, uid in conn.execute(
            f"SELECT record_id, property, {label_col}, value, {uid_col} "
            f"FROM ABMultiValue WHERE property IN (?, ?, ?, ?) ORDER BY {order}",
            (PHONE, EMAIL, ADDRESS, URL)):
        name = _abcddb.clean_label(labels.get(label)) if isinstance(label, int) \
            else _abcddb.clean_label(label)
        bucket = multi.setdefault(rid, {})
        if prop == ADDRESS:
            parts = entries.get(uid, {})
            if parts:
                bucket.setdefault(ADDRESS, []).append({
                    "label": name, **{k: parts.get(k) for k in _ENTRY_KEYS.values()}})
        elif value:
            bucket.setdefault(prop, []).append((name, str(value)))

    out = []
    for r in rows:
        rowid, first, last, middle, nick, org, job, note, kind = r[:9]
        values = multi.get(rowid, {})
        ph, em = values.get(PHONE, []), values.get(EMAIL, [])
        if not any((first, last, nick, org, ph, em)):
            continue
        named = " ".join(x for x in (first, middle, last) if x)
        if kind == 1 and org:
            display = org
        elif named:            # the prefix and suffix are part of the name shown
            display = " ".join(x for x in (r[12], named, r[13]) if x)
        else:
            display = nick or org
        out.append({
            "uid": str(rowid), "first": first, "last": last, "middle": middle,
            "nickname": nick, "org": org, "job_title": job,
            "display_name": display, "phones": ph, "emails": em,
            "addresses": values.get(ADDRESS, []), "urls": values.get(URL, []),
            "birthday": _birthday(r[9]), "note": note,
            "created_at": _abcddb.core_data_iso(r[10]),
            "modified_at": _abcddb.core_data_iso(r[11]),
        })
    return out

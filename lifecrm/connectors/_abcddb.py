"""Reader for a Mac's Contacts store (`AddressBook-v22.abcddb`).

A helper, not a connector (the leading underscore keeps it out of the
registry). The Mac keeps one Core Data SQLite store per account under
~/Library/Application Support/AddressBook/Sources/<id>/ plus a local one at
the top level; `lifecrm.platforms.mac_addressbook_dbs()` lists them.

Schema, as Contacts.app has written it for a decade (column sets vary a
little by macOS version, so every column is checked before it is used):

    ZABCDRECORD        Z_PK, ZFIRSTNAME, ZLASTNAME, ZMIDDLENAME, ZNICKNAME,
                       ZORGANIZATION, ZJOBTITLE, ZBIRTHDAY, ZUNIQUEID,
                       ZCREATIONDATE, ZMODIFICATIONDATE, ZNOTE
    ZABCDPHONENUMBER   ZOWNER -> ZABCDRECORD.Z_PK, ZFULLNUMBER, ZLABEL
    ZABCDEMAILADDRESS  ZOWNER, ZADDRESS, ZLABEL
    ZABCDPOSTALADDRESS ZOWNER, ZSTREET, ZCITY, ZSTATE, ZZIPCODE, ZCOUNTRYNAME, ZLABEL
    ZABCDNOTE          ZCONTACT -> ZABCDRECORD.Z_PK, ZTEXT

Dates are Core Data timestamps: seconds since 2001-01-01 UTC. A birthday
with no year is stored in the year 1604.

The caller opens the database (read-only, ideally on a snapshot copy — the
live store is TCC-protected and held open by Contacts). Nothing here writes.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
NO_YEAR = 1604

_LABEL = re.compile(r"^_\$!<(.*)>!\$_$")


def is_abcddb(conn: sqlite3.Connection) -> bool:
    return _has_table(conn, "ZABCDRECORD")


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def clean_label(raw) -> str | None:
    """'_$!<Mobile>!$_' -> 'Mobile'; custom labels pass through."""
    if not raw:
        return None
    m = _LABEL.match(str(raw))
    return m.group(1) if m else str(raw)


def core_data_iso(value) -> str | None:
    """Core Data timestamp -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    if value is None or value == "":
        return None
    try:
        dt = APPLE_EPOCH + timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def birthday(value) -> str | None:
    """'YYYY-MM-DD', or '--MM-DD' when the card has no birth year.

    A birthday is a date, but Core Data stores an instant: either noon GMT or
    the local midnight of whoever typed it in (seen both ways). A value near
    noon UTC is read as that UTC date; anything else is rounded to the
    nearest midnight, which recovers the date for any zone within +-12h.
    """
    if value is None or value == "":
        return None
    try:
        dt = APPLE_EPOCH + timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if not 10 <= dt.hour < 14:
        dt = dt + timedelta(hours=12)
    if dt.year == NO_YEAR:
        return f"--{dt.month:02d}-{dt.day:02d}"
    return dt.strftime("%Y-%m-%d")


def _sel(cols: set[str], name: str, alias: str | None = None) -> str:
    alias = alias or name
    return f"{name} AS {alias}" if name in cols else f"NULL AS {alias}"


def read_contacts(conn: sqlite3.Connection) -> list[dict]:
    """Every person card in one store, in Z_PK order.

    Each dict: uid, first, last, middle, nickname, org, job_title,
    display_name, phones [(label, raw)], emails [(label, raw)],
    addresses [{label, street, city, state, zip, country}], birthday,
    note, created_at, modified_at. Group records (no name, no org, no
    phone, no email) are skipped.
    """
    if not is_abcddb(conn):
        raise ValueError("not a Contacts store (no ZABCDRECORD table)")
    rc = _columns(conn, "ZABCDRECORD")
    rows = conn.execute(
        "SELECT Z_PK AS pk, "
        + ", ".join(_sel(rc, c, a) for c, a in (
            ("ZUNIQUEID", "uid"), ("ZFIRSTNAME", "first"), ("ZLASTNAME", "last"),
            ("ZMIDDLENAME", "middle"), ("ZNICKNAME", "nickname"),
            ("ZORGANIZATION", "org"), ("ZJOBTITLE", "job_title"),
            ("ZBIRTHDAY", "birthday"), ("ZCREATIONDATE", "created"),
            ("ZMODIFICATIONDATE", "modified"), ("ZNOTE", "note_ref")))
        + " FROM ZABCDRECORD ORDER BY Z_PK").fetchall()

    def multi(table: str, value_col: str) -> dict[int, list[tuple]]:
        out: dict[int, list[tuple]] = {}
        if not _has_table(conn, table):
            return out
        cols = _columns(conn, table)
        if "ZOWNER" not in cols or value_col not in cols:
            return out
        label = "ZLABEL" if "ZLABEL" in cols else "NULL"
        order = "ZORDERINGINDEX, Z_PK" if "ZORDERINGINDEX" in cols else "Z_PK"
        for r in conn.execute(f"SELECT ZOWNER, {label}, {value_col} FROM {table} "
                              f"WHERE {value_col} IS NOT NULL ORDER BY {order}"):
            out.setdefault(r[0], []).append((clean_label(r[1]), str(r[2])))
        return out

    phones = multi("ZABCDPHONENUMBER", "ZFULLNUMBER")
    emails = multi("ZABCDEMAILADDRESS", "ZADDRESS")

    addresses: dict[int, list[dict]] = {}
    if _has_table(conn, "ZABCDPOSTALADDRESS"):
        pc = _columns(conn, "ZABCDPOSTALADDRESS")
        if "ZOWNER" in pc:
            for r in conn.execute(
                    "SELECT ZOWNER AS owner, "
                    + ", ".join(_sel(pc, c, a) for c, a in (
                        ("ZLABEL", "label"), ("ZSTREET", "street"), ("ZCITY", "city"),
                        ("ZSTATE", "state"), ("ZZIPCODE", "zip"),
                        ("ZCOUNTRYNAME", "country")))
                    + " FROM ZABCDPOSTALADDRESS ORDER BY Z_PK"):
                addresses.setdefault(r[0], []).append({
                    "label": clean_label(r[1]), "street": r[2], "city": r[3],
                    "state": r[4], "zip": r[5], "country": r[6]})

    notes_by_contact: dict[int, str] = {}
    notes_by_pk: dict[int, str] = {}
    if _has_table(conn, "ZABCDNOTE"):
        nc = _columns(conn, "ZABCDNOTE")
        if "ZTEXT" in nc:
            contact = "ZCONTACT" if "ZCONTACT" in nc else "NULL"
            for r in conn.execute(f"SELECT Z_PK, {contact}, ZTEXT FROM ZABCDNOTE"):
                if r[2]:
                    notes_by_pk[r[0]] = str(r[2])
                    if r[1] is not None:
                        notes_by_contact[r[1]] = str(r[2])

    out = []
    for r in rows:
        pk = r[0]
        first, last, middle, nick, org = r[2], r[3], r[4], r[5], r[6]
        ph, em = phones.get(pk, []), emails.get(pk, [])
        if not any((first, last, nick, org, ph, em)):
            continue
        name = " ".join(x for x in (first, middle, last) if x) or nick or org
        note = notes_by_contact.get(pk)
        if note is None and isinstance(r[11], int):
            note = notes_by_pk.get(r[11])
        out.append({
            "uid": r[1] or f"pk:{pk}", "first": first, "last": last,
            "middle": middle, "nickname": nick, "org": org, "job_title": r[7],
            "display_name": name, "phones": ph, "emails": em,
            "addresses": addresses.get(pk, []), "birthday": birthday(r[8]),
            "note": note, "created_at": core_data_iso(r[9]),
            "modified_at": core_data_iso(r[10]),
        })
    return out

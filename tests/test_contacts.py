"""Contacts connector: the address book as contact_card observations, and the
display names that feed `handles list --suggest`.

Inputs are built in code, shaped like the real exports, including the parts
that are easy to get wrong: vCard 2.1's quoted-printable values with their own
CHARSET and soft line breaks, 3.0's item-grouped custom labels and escapes,
4.0's tel: URIs, Apple's no-year birthdays, folding that splits a UTF-8
character, a BOM, UTF-16; a Mac Contacts folder with several Core Data
stores, one of them live (WAL); and an iPhone backup's AddressBook.sqlitedb
found through Manifest.db.

The mirroring tests check that the archive follows the address book — a
deleted card leaves — and that removal cannot reach what it must not: another
account's cards, a vCard nobody said was whose, or most of the archive at once.
"""

import hashlib
import json
import os
import plistlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.connectors import _vcard, base, contacts
from lifecrm.domain import entities, observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks

runner = CliRunner()
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _core(iso: str) -> float:
    return (datetime.fromisoformat(iso.replace("Z", "+00:00")) - APPLE_EPOCH).total_seconds()


def _write(path: Path, text: str, *, encoding="utf-8", mtime: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\n", "\r\n").encode(encoding)
    path.write_bytes(data)
    if mtime:
        t = datetime.fromisoformat(mtime.replace("Z", "+00:00")).timestamp()
        os.utime(path, (t, t))
    return path


def _by_title(scan) -> dict:
    return {r["title"]: r for r in scan["records"]}


def _detail(rec) -> dict:
    return json.loads(rec["value_json"]) if rec["value_json"] else {}


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


GOOGLE = """BEGIN:VCARD
VERSION:3.0
FN:Jake Morales
N:Morales;Jake;;;
NICKNAME:Jakey
EMAIL;TYPE=INTERNET;TYPE=HOME:Jake.Morales@Example.com
TEL;TYPE=CELL:+1 512-555-0142
item1.TEL:(512) 555-0143
item1.X-ABLabel:Work cell
ADR;TYPE=HOME:;;12 Oak St\\, Apt 4;Austin;TX;78702;US
BDAY:1993-03-14
ORG:Acme Robotics;Platform
TITLE:Engineer
NOTE:Grew up in El Paso.\\nFavorite restaurant: Veracruz All Natural.
URL:http\\://jake.example
CATEGORIES:myContacts,starred
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Ellis Dental
ORG:Ellis Dental
TEL;TYPE=WORK:+1 512 555 0199 ext. 3
ADR;TYPE=WORK:;;3000 Medical Pkwy;Austin;TX;78705;
CATEGORIES:myContacts
END:VCARD
"""

SAMSUNG = """BEGIN:VCARD
VERSION:2.1
N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=4D=C3=BCller;J=C3=BCrgen;;;
FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:J=C3=BCrgen =4D=C3=BCller
TEL;CELL;PREF:+49 30 1234567
TEL;HOME:5125550111
EMAIL;HOME:juergen@example.com
NOTE;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:Met at the climbing gym in Ber=
lin. Likes caf=C3=A9s.
X-ANDROID-CUSTOM:vnd.android.cursor.item/nickname;Jü;1;;;;;;;;;;;;;
X-ANDROID-CUSTOM:vnd.android.cursor.item/relation;Anna;1;;;;;;;;;;;;;
PHOTO;ENCODING=BASE64;JPEG:/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgF
 DhIQDQ4RDhELCxAWEBETFBUVFQwPFxgWFBgSFBUU/9sAQwEDBAQFBAUJBQUJFA0LDRQUFBQUFBQU
 FBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU/8AAEQgAAQABAwEi==

END:VCARD
BEGIN:VCARD
VERSION:2.1
N;CHARSET=ISO-8859-1;ENCODING=QUOTED-PRINTABLE:Garc=EDa;Ana;;;
TEL;WORK;FAX:5125550112
END:VCARD
"""

V4 = """BEGIN:VCARD
VERSION:4.0
UID:urn:uuid:4fbe8971-0bc3-424c-9c26-36c3e1eff6b1
FN:Sam Okafor
N:Okafor;Sam;;;
TEL;VALUE=uri;TYPE="cell,voice";PREF=1:tel:+1-512-555-0166
EMAIL;TYPE=work:sam@example.com
BDAY:--0821
REV:20260901T120000Z
END:VCARD
"""


# ---------------------------------------------------------------- vCard reading

def test_a_google_export_reads_names_numbers_labels_addresses_and_notes():
    cards = _vcard.parse(GOOGLE.encode(), "contacts.vcf")["cards"]
    jake, ellis = cards
    assert jake["display_name"] == "Jake Morales"
    assert (jake["first"], jake["last"], jake["nickname"]) == ("Jake", "Morales", "Jakey")
    assert jake["phones"] == [("mobile", "+1 512-555-0142"), ("Work cell", "(512) 555-0143")]
    assert jake["emails"] == [("home", "Jake.Morales@Example.com")]
    assert jake["addresses"] == [{"label": "home", "street": "12 Oak St, Apt 4",
                                  "city": "Austin", "state": "TX", "zip": "78702",
                                  "country": "US"}]
    assert jake["birthday"] == "1993-03-14"
    assert (jake["org"], jake["job_title"]) == ("Acme Robotics", "Engineer")
    assert jake["note"] == "Grew up in El Paso.\nFavorite restaurant: Veracruz All Natural."
    assert jake["urls"] == [(None, "http://jake.example")]
    assert jake["uid"] is None and jake["modified_at"] is None
    assert ellis["display_name"] == "Ellis Dental" and ellis["phones"][0][0] == "work"


def test_a_samsung_2_1_export_decodes_quoted_printable_charsets_and_soft_breaks():
    parsed = _vcard.parse(SAMSUNG.encode(), "Contacts.vcf")
    juergen, ana = parsed["cards"]
    assert juergen["display_name"] == "Jürgen Müller"
    assert (juergen["first"], juergen["last"]) == ("Jürgen", "Müller")
    assert juergen["note"] == "Met at the climbing gym in Berlin. Likes cafés."
    assert juergen["nickname"] == "Jü"
    assert juergen["phones"] == [("mobile", "+49 30 1234567"), ("home", "5125550111")]
    assert ana["display_name"] == "Ana García"              # ISO-8859-1, not UTF-8
    assert ana["phones"] == [("work fax", "5125550112")]
    assert parsed["photos"] == 1
    assert parsed["unmapped"] == {"X-ANDROID-CUSTOM (relation)": 1}
    assert parsed["versions"] == {"2.1": 2}


def test_vcard_4_tel_uris_quoted_types_and_no_year_birthdays():
    sam = _vcard.parse(V4.encode(), "sam.vcf")["cards"][0]
    assert sam["phones"] == [("mobile", "+1-512-555-0166")]
    assert sam["emails"] == [("work", "sam@example.com")]
    assert sam["birthday"] == "--08-21"
    assert sam["uid"] == "urn:uuid:4fbe8971-0bc3-424c-9c26-36c3e1eff6b1"
    assert sam["modified_at"] == "2026-09-01T12:00:00Z"


def test_android_and_samsung_custom_labels_are_kept_as_typed():
    raw = ("BEGIN:VCARD\nVERSION:2.1\nFN:Priya Natarajan\n"
           "TEL;X-Office:5125550123\n"
           "TEL;X-CUSTOM(CHARSET=UTF-8,ENCODING=QUOTED-PRINTABLE,=42=C3=BCro):5125550124\n"
           "TEL;CELL;PREF:5125550125\nEND:VCARD\n"
           "BEGIN:VCARD\nVERSION:3.0\nFN:Sam Okafor\nTEL;TYPE=Gym front desk:5125550166\n"
           "EMAIL;TYPE=INTERNET;TYPE=WORK:sam@example.com\nEND:VCARD\n")
    priya, sam = _vcard.parse(raw.encode(), "Contacts.vcf")["cards"]
    assert priya["phones"] == [("Office", "5125550123"), ("Büro", "5125550124"),
                               ("mobile", "5125550125")]
    assert sam["phones"] == [("Gym front desk", "5125550166")]
    assert sam["emails"] == [("work", "sam@example.com")]


def test_an_apple_iphone_number_reads_as_iphone_as_the_mac_store_says():
    raw = (b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Maya Chen\r\n"
           b"TEL;type=IPHONE;type=CELL;type=VOICE;type=pref:(512) 555-0187\r\nEND:VCARD\r\n")
    assert _vcard.parse(raw, "Maya.vcf")["cards"][0]["phones"] == \
        [("iphone", "(512) 555-0187")]


def _fold_bytes(line: bytes, width: int = 75) -> bytes:
    """Fold at exact octet positions, as some exporters do — even mid-character."""
    out = [line[:width]]
    rest = line[width:]
    while rest:
        out.append(b" " + rest[:width - 1])
        rest = rest[width - 1:]
    return b"\r\n".join(out)


def test_apple_omit_year_birthday_x_abuid_and_folding_inside_a_utf8_character():
    # 'NOTE:' plus 69 characters is 74 octets, so the 75-octet fold lands on
    # the second of the coffee cup's three bytes.
    note = ("Coffee at Fleet on South Congress every Saturday, oat flat white"
            .ljust(69, ".") + "☕ — and a pastry for Jake.")
    lines = [b"BEGIN:VCARD", b"VERSION:3.0", b"PRODID:-//Apple Inc.//macOS 14.5//EN",
             b"N:Chen;Maya;;;", b"FN:Maya Chen",
             b"item1.TEL;type=pref:(512) 555-0187", b"item1.X-ABLabel:_$!<Mobile>!$_",
             b"BDAY;X-APPLE-OMIT-YEAR=1604:1604-10-12",
             _fold_bytes(f"NOTE:{note}".encode()),
             b"X-ABUID:5AD380FD-B2DE-4261-BA99-DE1D1DB52FBE:ABPerson",
             b"REV:2026-10-12T20:41:37Z", b"END:VCARD"]
    raw = b"\r\n".join(lines) + b"\r\n"
    with pytest.raises(UnicodeDecodeError):
        raw.split(b"\r\n")[8].decode("utf-8")                # the fold really splits it
    parsed = _vcard.parse(raw, "Maya Chen.vcf")
    maya = parsed["cards"][0]
    assert maya["note"] == note.strip()
    assert maya["birthday"] == "--10-12"
    assert maya["phones"] == [("mobile", "(512) 555-0187")]
    assert maya["uid"] == "5AD380FD-B2DE-4261-BA99-DE1D1DB52FBE:ABPerson"
    assert maya["uid_kind"] == "X-ABUID"
    assert parsed["prodid"].startswith("-//Apple")


def test_bom_utf16_and_bare_cr_line_endings_read_the_same():
    plain = _vcard.parse(V4.encode(), "a.vcf")["cards"]
    assert _vcard.parse(b"\xef\xbb\xbf" + V4.encode(), "a.vcf")["cards"] == plain
    assert _vcard.parse(V4.encode("utf-16"), "a.vcf")["cards"] == plain
    assert _vcard.parse(V4.replace("\n", "\r").encode(), "a.vcf")["cards"] == plain


def test_a_file_that_is_not_a_vcard_fails_naming_what_it_saw(cfg, tmp_path):
    with pytest.raises(_vcard.VCardError, match="no BEGIN:VCARD.*Name,Phone"):
        _vcard.parse(b"Name,Phone\nJake,5125550142\n", "contacts.csv")
    bad = tmp_path / "contacts.vcf"
    bad.write_text("Name,Phone\nJake,5125550142\n", encoding="utf-8")
    with pytest.raises(DomainError, match="contacts.vcf has no BEGIN:VCARD"):
        contacts.scan(bad, cfg)
    with pytest.raises(DomainError, match="not found"):
        contacts.scan(tmp_path / "missing.vcf", cfg)
    (tmp_path / "empty").mkdir()
    with pytest.raises(DomainError, match="holds no address book"):
        contacts.scan(tmp_path / "empty", cfg)


@pytest.mark.parametrize("raw, expected", [
    ("1993-03-14", "1993-03-14"), ("19930314", "1993-03-14"),
    ("1993-03-14T00:00:00Z", "1993-03-14"), ("--0314", "--03-14"),
    ("--03-14", "--03-14"), ("1604-03-14", "--03-14"), ("0000-02-29", "--02-29"),
    ("circa 1950", None), ("1993-02-30", None),
])
def test_birthday_shapes(raw, expected):
    assert _vcard.parse_birthday(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2026-10-01T12:00:00Z", "2026-10-01T12:00:00Z"),
    ("20261001T120000Z", "2026-10-01T12:00:00Z"),
    ("2026-10-01T12:00:00.250Z", "2026-10-01T12:00:00Z"),
    ("2026-10-01T07:00:00-05:00", "2026-10-01T12:00:00Z"),
    ("2026-10-01", "2026-10-01T00:00:00Z"), ("yesterday", None),
])
def test_rev_shapes(raw, expected):
    assert _vcard.parse_instant(raw) == expected


# ---------------------------------------------------------------- records

def test_a_card_becomes_one_record_with_searchable_body_and_structured_detail(cfg, tmp_path):
    vcf = _write(tmp_path / "contacts.vcf", GOOGLE, mtime="2026-10-21T17:00:00Z")
    scan = contacts.scan(vcf, cfg)
    assert scan["connector"] == "contacts"
    assert {r["stream"] for r in scan["records"]} == {"contact_card"}
    jake = _by_title(scan)["Jake Morales"]
    assert jake["body"].splitlines() == [
        "Engineer at Acme Robotics", "Nickname: Jakey",
        "Lives at 12 Oak St, Apt 4, Austin, TX 78702, US", "Birthday: March 14, 1993",
        "Grew up in El Paso.", "Favorite restaurant: Veracruz All Natural."]
    d = _detail(jake)
    assert set(d) == {"phones", "emails", "addresses", "birthday", "org", "title",
                      "nickname", "urls"}
    assert d["phones"][0] == {"label": "mobile", "number": "+1 512-555-0142",
                              "handle": "+15125550142"}
    assert jake["origin_app"] == "google_contacts"
    assert jake["direction"] is None and jake["value"] is None
    ellis = _by_title(scan)["Ellis Dental"]
    assert ellis["body"] == "Address: 3000 Medical Pkwy, Austin, TX 78705"


def test_every_number_and_address_on_a_card_is_a_participant_named_by_the_card(cfg, tmp_path):
    scan = contacts.scan(_write(tmp_path / "contacts.vcf", GOOGLE), cfg)
    jake = _by_title(scan)["Jake Morales"]
    assert jake["participants"] == [
        {"kind": "phone", "value": "+15125550142", "role": "participant"},
        {"kind": "phone", "value": "+15125550143", "role": "participant"},
        {"kind": "email", "value": "jake.morales@example.com", "role": "participant"}]
    handles = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert handles[("phone", "+15125550142")] == "Jake Morales"
    assert handles[("email", "jake.morales@example.com")] == "Jake Morales"
    named = {(p["kind"], p["value"]) for r in scan["records"] for p in r["participants"]}
    assert named == set(handles)             # every participant is a declared handle


def test_a_number_on_two_cards_keeps_both_names(cfg, tmp_path):
    vcf = _write(tmp_path / "home.vcf", "BEGIN:VCARD\nVERSION:3.0\nFN:Mom\n"
                 "TEL;TYPE=HOME:(210) 555-0100\nEND:VCARD\nBEGIN:VCARD\nVERSION:3.0\n"
                 "FN:Dad\nTEL;TYPE=HOME:210-555-0100\nEND:VCARD\n")
    scan = contacts.scan(vcf, cfg)
    assert scan["handles"] == [{"kind": "phone", "value": "+12105550100",
                                "display_name": "Dad / Mom"}]


def test_extensions_and_pauses_are_cut_and_non_numbers_get_no_handle(cfg, tmp_path):
    assert contacts.phone_handle("+1 512 555 0199 ext. 3") == "+15125550199"
    assert contacts.phone_handle("512-555-0199 x12") == "+15125550199"
    assert contacts.phone_handle("5125550199;ext=5") == "+15125550199"
    assert contacts.phone_handle("5125550199,,123#") == "+15125550199"
    assert contacts.phone_handle("5125550199p1") == "+15125550199"
    assert contacts.phone_handle("*86") is None
    vcf = _write(tmp_path / "v.vcf", "BEGIN:VCARD\nVERSION:3.0\nFN:Voicemail\n"
                 "TEL:*86\nEMAIL:not-an-address\nEND:VCARD\n")
    scan = contacts.scan(vcf, cfg)
    rec = scan["records"][0]
    assert rec["participants"] == [] and _detail(rec)["phones"] == [{"number": "*86"}]
    whats = {u["what"] for u in scan["unmapped"]}
    assert any(w.startswith("phone value that is not a number") for w in whats)
    assert any(w.startswith("email value without '@'") for w in whats)


def test_cards_without_an_id_get_a_stable_name_id_and_namesakes_are_told_apart(cfg, tmp_path):
    one = "BEGIN:VCARD\nVERSION:3.0\nFN:Chris Lee\nTEL:5125550131\nEND:VCARD\n"
    first = contacts.scan(_write(tmp_path / "a.vcf", one), cfg)["records"][0]["external_id"]
    edited = one.replace("TEL:5125550131", "TEL:5125550131\nNOTE:new number soon")
    again = contacts.scan(_write(tmp_path / "b.vcf", edited), cfg)["records"][0]
    assert again["external_id"] == first             # an edit keeps the id
    two = one + "BEGIN:VCARD\nVERSION:3.0\nFN:Chris Lee\nTEL:5125550132\nEND:VCARD\n"
    ids = [r["external_id"] for r in contacts.scan(_write(tmp_path / "c.vcf", two),
                                                   cfg)["records"]]
    assert len(set(ids)) == 2 and first not in ids  # namesakes re-key by their numbers
    sam = contacts.scan(_write(tmp_path / "d.vcf", V4), cfg)["records"][0]
    assert sam["external_id"] == "urn:uuid:4fbe8971-0bc3-424c-9c26-36c3e1eff6b1"


def test_the_same_card_in_two_takeout_files_is_read_once(cfg, tmp_path):
    takeout = tmp_path / "Takeout" / "Contacts"
    _write(takeout / "My Contacts" / "My Contacts.vcf", GOOGLE)
    _write(takeout / "starred" / "starred.vcf", GOOGLE.split("BEGIN:VCARD")[1].join(
        ["BEGIN:VCARD", ""]))
    scan = contacts.scan(tmp_path / "Takeout", cfg)
    assert sorted(_by_title(scan)) == ["Ellis Dental", "Jake Morales"]
    assert "1 × duplicate of a card already read (same card twice)" in scan["skipped"]
    assert scan["source_detail"].startswith("2 vCard file(s)")


def test_dates_come_from_rev_else_the_export_and_respect_the_rollover(cfg, tmp_path):
    vcf = _write(tmp_path / "mixed.vcf", GOOGLE + V4, mtime="2026-10-21T10:30:00Z")
    scan = contacts.scan(vcf, cfg)
    by = _by_title(scan)
    assert by["Sam Okafor"]["started_at"] == "2026-09-01T12:00:00Z"
    assert by["Sam Okafor"]["last_modified_at"] == "2026-09-01T12:00:00Z"
    # undated: the export's time, 10:30Z = 05:30 Chicago — after the rollover
    assert by["Jake Morales"]["started_at"] == "2026-10-21T10:30:00Z"
    assert by["Jake Morales"]["journal_date"] == "2026-10-21"
    os.utime(vcf, (datetime(2026, 10, 21, 9, 30, tzinfo=timezone.utc).timestamp(),) * 2)
    early = _by_title(contacts.scan(vcf, cfg))["Jake Morales"]
    assert early["journal_date"] == "2026-10-20"     # 04:30 Chicago: still yesterday
    assert scan["undated"] == 2 and scan["export_dates"] == ["2026-10-21"]


def test_two_scans_are_identical(cfg, tmp_path):
    vcf = _write(tmp_path / "contacts.vcf", GOOGLE + SAMSUNG + V4)
    a, b = contacts.scan(vcf, cfg), contacts.scan(vcf, cfg)
    assert a["records"] == b["records"] and a["handles"] == b["handles"]
    assert a["source_hash"] == b["source_hash"] == base.file_hash(vcf)


def test_report_names_counts_span_and_what_was_not_imported(cfg, conn, tmp_path):
    vcf = _write(tmp_path / "contacts.vcf", GOOGLE + SAMSUNG, mtime="2026-10-21T17:00:00Z")
    scan = contacts.scan(vcf, cfg)
    text = "\n".join(contacts.report_lines(scan, obs.build_plan(conn, scan)))
    assert "contacts: contacts.vcf (vCard 2.1/3.0" in text
    assert "contact_card: 4 new, 0 changed | 2026-10-21..2026-10-21" in text
    assert "dates: 4 card(s) carry no date of their own" in text
    assert "not mirroring deletions" in text and "[connectors.contacts] account" in text
    assert "1 × contact photo (media stays out of the database)" in text
    assert "CATEGORIES (2)" in text and "X-ANDROID-CUSTOM (relation) (1)" in text
    assert "handles: 2 email, 6 phone" in text


def test_apply_then_reapply_is_identical_and_the_confirmed_tier_is_untouched(cfg, conn, tmp_path):
    vcf = _write(tmp_path / "contacts.vcf", GOOGLE + SAMSUNG + V4)
    scan = contacts.scan(vcf, cfg)
    _, result = _apply(conn, scan)
    assert result["added"] == 5
    plan, again = _apply(conn, contacts.scan(vcf, cfg))
    assert again["added"] == 0 and again["identical"] == 5 and not plan["conflicts"]
    for table in ("entries", "measurements", "mentions", "entity_aliases",
                  "entry_sources", "extraction_runs"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0, table
    assert run_checks(conn, cfg)["failures"] == 0
    row = conn.execute("SELECT display_name, observation_count FROM entity_handles "
                       "WHERE value='+15125550142'").fetchone()
    assert (row["display_name"], row["observation_count"]) == ("Jake Morales", 1)
    assert conn.execute("SELECT entity_id FROM entity_handles WHERE entity_id IS NOT NULL"
                        ).fetchone() is None                     # nothing linked


def test_a_newer_export_of_an_undated_file_is_reported_as_only_re_dated(cfg, conn, tmp_path):
    vcf = _write(tmp_path / "contacts.vcf", GOOGLE, mtime="2026-10-01T17:00:00Z")
    _apply(conn, contacts.scan(vcf, cfg))
    _write(vcf, GOOGLE.replace("TITLE:Engineer", "TITLE:Staff Engineer"),
           mtime="2026-10-21T17:00:00Z")
    scan = contacts.scan(vcf, cfg)
    plan = obs.build_plan(conn, scan)
    text = "\n".join(contacts.report_lines(scan, plan))
    assert "contact_card: 0 new, 2 changed" in text
    assert "1 of the changed card(s) only re-dated" in text
    assert not plan["conflicts"]                    # a newer export wins
    old = _write(tmp_path / "old.vcf", GOOGLE.replace("Engineer", "Intern"),
                 mtime="2026-09-01T17:00:00Z")
    _apply(conn, scan)
    assert obs.build_plan(conn, contacts.scan(old, cfg))["conflicts"]   # older loses


# ---------------------------------------------------------------- Mac Contacts

def make_store(path: Path, people: list[dict], *, store_uuid: str | None = None,
               prefix: str = "A") -> Path:
    """An AddressBook-v22.abcddb with Contacts' columns (the make_abcddb schema
    of test_platforms, plus Z_METADATA, prefixes, nicknames and URLs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA synchronous = OFF")
    c.executescript("""
        BEGIN;
        CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), Z_PLIST BLOB);
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
            ZUNIQUEID TEXT, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZMIDDLENAME TEXT,
            ZTITLE TEXT, ZNICKNAME TEXT, ZORGANIZATION TEXT, ZJOBTITLE TEXT,
            ZBIRTHDAY REAL, ZCREATIONDATE REAL, ZMODIFICATIONDATE REAL, ZNOTE INTEGER);
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
        COMMIT;
    """)
    if store_uuid:
        c.execute("INSERT INTO Z_METADATA VALUES (1, ?, NULL)", (store_uuid,))
    for i, p in enumerate(people, start=1):
        c.execute("INSERT INTO ZABCDRECORD (Z_PK, Z_ENT, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME, "
                  "ZTITLE, ZNICKNAME, ZORGANIZATION, ZJOBTITLE, ZBIRTHDAY, ZCREATIONDATE, "
                  "ZMODIFICATIONDATE) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (i, 22, p.get("uid", f"{prefix}-{i}:ABPerson"), p.get("first"),
                   p.get("last"), p.get("prefix"), p.get("nick"), p.get("org"),
                   p.get("job"), p.get("bday"), _core("2019-05-02T18:00:00Z"),
                   _core(p.get("modified", "2026-10-01T15:00:00Z"))))
        for n, ph in enumerate(p.get("phones", [])):
            c.execute("INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER, ZLABEL, "
                      "ZORDERINGINDEX) VALUES (?,?,?,?)", (i, ph, "_$!<Mobile>!$_", n))
        for n, em in enumerate(p.get("emails", [])):
            c.execute("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS, ZLABEL, "
                      "ZORDERINGINDEX) VALUES (?,?,?,?)", (i, em, "_$!<Home>!$_", n))
        if p.get("city"):
            c.execute("INSERT INTO ZABCDPOSTALADDRESS (ZOWNER, ZCITY, ZSTATE, ZLABEL) "
                      "VALUES (?,?,?,?)", (i, p["city"], p.get("state"), "_$!<Home>!$_"))
        if p.get("url"):
            c.execute("INSERT INTO ZABCDURLADDRESS (ZOWNER, ZURL, ZLABEL) VALUES (?,?,?)",
                      (i, p["url"], "_$!<HomePage>!$_"))
        if p.get("note"):
            c.execute("INSERT INTO ZABCDNOTE (ZCONTACT, ZTEXT) VALUES (?,?)", (i, p["note"]))
    c.execute("INSERT INTO ZABCDRECORD (Z_PK, Z_ENT) VALUES (99, 19)")      # a group
    c.commit()
    c.close()
    return path


JAKE_MAC = {"first": "Jake", "last": "Morales", "nick": "Jakey",
            "phones": ["(512) 555-0142"], "emails": ["jake.morales@example.com"],
            "city": "Austin", "state": "TX", "note": "Grew up in El Paso.",
            "bday": _core("1993-03-14T12:00:00Z"), "url": "https://jake.example"}
ELLIS_MAC = {"first": "Rebecca", "last": "Ellis", "prefix": "Dr.", "org": "Ellis Dental",
             "phones": ["+1 512 555 0199"], "modified": "2026-10-07T15:00:00Z"}


def mac_folder(root: Path, *, local=(), icloud=(JAKE_MAC, ELLIS_MAC),
               top_uuid="TOP-UUID-1") -> Path:
    book = root / "AddressBook"
    make_store(book / contacts.STORE, list(local), store_uuid=top_uuid, prefix="L")
    make_store(book / "Sources" / "ICLOUD-1" / contacts.STORE, list(icloud),
               store_uuid="ICLOUD-STORE", prefix="C")
    return book


def test_a_mac_contacts_folder_reads_every_store(cfg, tmp_path):
    book = mac_folder(tmp_path, local=[{"org": "Veracruz All Natural",
                                        "phones": ["(512) 555-0177"]}])
    scan = contacts.scan(book, cfg)
    by = _by_title(scan)
    assert sorted(by) == ["Dr. Rebecca Ellis", "Jake Morales", "Veracruz All Natural"]
    assert scan["account"] == "mac-addressbook:TOP-UUID-1"
    jake = by["Jake Morales"]
    assert jake["external_id"] == "C-1:ABPerson"
    assert jake["origin_app"] == "contacts"
    assert jake["started_at"] == "2026-10-01T15:00:00Z"      # last change, not creation
    assert jake["body"].splitlines() == ["Nickname: Jakey", "Lives in Austin, TX",
                                         "Birthday: March 14, 1993", "Grew up in El Paso."]
    assert _detail(jake)["urls"] == [{"label": "homepage", "url": "https://jake.example"}]
    assert _detail(jake)["phones"][0]["label"] == "mobile"
    assert scan["source_detail"] == "Mac Contacts, 2 store(s): local, ICLOUD-1"


def test_a_live_store_is_read_through_a_snapshot_and_never_modified(cfg, tmp_path):
    store = make_store(tmp_path / "AddressBook" / contacts.STORE, [JAKE_MAC],
                       store_uuid="TOP")
    writer = sqlite3.connect(store)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO ZABCDRECORD (Z_PK, Z_ENT, ZUNIQUEID, ZFIRSTNAME) "
                   "VALUES (5, 22, 'NEW:ABPerson', 'Priya')")
    writer.execute("INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (5, '5125550123')")
    writer.commit()                       # only in the -wal while the writer stays open
    try:
        assert store.with_name(store.name + "-wal").exists()
        before = store.read_bytes()
        scan = contacts.scan(tmp_path / "AddressBook", cfg)
        assert sorted(_by_title(scan)) == ["Jake Morales", "Priya"]
        assert store.read_bytes() == before
    finally:
        writer.close()


def test_a_single_store_passed_alone_is_its_own_account(cfg, tmp_path):
    book = mac_folder(tmp_path)
    single = book / "Sources" / "ICLOUD-1" / contacts.STORE
    scan = contacts.scan(single, cfg)
    assert scan["account"] == "mac-store:ICLOUD-1"
    assert scan["source_file"] == f"ICLOUD-1/{contacts.STORE}"
    top = contacts.scan(book / contacts.STORE, cfg)
    assert top["account"] == "mac-store:TOP-UUID-1" and top["records"] == []


def test_a_mac_folder_without_store_metadata_does_not_mirror(cfg, conn, tmp_path):
    book = mac_folder(tmp_path, top_uuid=None)
    scan = contacts.scan(book, cfg)
    assert scan["account"] is None
    assert "not mirroring deletions" in "\n".join(
        contacts.report_lines(scan, obs.build_plan(conn, scan)))


# ---------------------------------------------------------------- iPhone backup

IOS_SCHEMA = """
CREATE TABLE ABPerson (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, First TEXT, Last TEXT,
  Middle TEXT, Prefix TEXT, Suffix TEXT, Nickname TEXT, Organization TEXT,
  JobTitle TEXT, Note TEXT, Kind INTEGER DEFAULT 0, Birthday TEXT,
  CreationDate INTEGER, ModificationDate INTEGER);
CREATE TABLE ABMultiValue (UID INTEGER PRIMARY KEY, record_id INTEGER, property INTEGER,
  identifier INTEGER, label INTEGER, value TEXT);
CREATE TABLE ABMultiValueLabel (value TEXT, UNIQUE(value));
CREATE TABLE ABMultiValueEntry (parent_id INTEGER, key INTEGER, value TEXT,
  UNIQUE(parent_id, key));
CREATE TABLE ABMultiValueEntryKey (value TEXT, UNIQUE(value));
"""


def _ios_book(path: Path) -> None:
    c = sqlite3.connect(path)
    c.executescript(IOS_SCHEMA)
    for v in ("_$!<Mobile>!$_", "_$!<Home>!$_", "Gym"):
        c.execute("INSERT INTO ABMultiValueLabel (value) VALUES (?)", (v,))
    for v in ("Street", "City", "State", "ZIP", "Country"):
        c.execute("INSERT INTO ABMultiValueEntryKey (value) VALUES (?)", (v,))
    c.execute("INSERT INTO ABPerson (First, Last, Nickname, Note, Birthday, "
              "CreationDate, ModificationDate) VALUES ('Sam', 'Okafor', NULL, "
              "'Spots me on bench.', ?, ?, ?)",
              (str(_core("1992-08-21T12:00:00Z")), int(_core("2020-01-05T12:00:00Z")),
               int(_core("2026-09-30T14:00:00Z"))))
    c.execute("INSERT INTO ABMultiValue (record_id, property, identifier, label, value) "
              "VALUES (1, 3, 0, 1, '(512) 555-0166'), (1, 3, 1, 3, '512-555-0167'), "
              "(1, 4, 0, 2, 'sam.okafor@example.com'), (1, 5, 0, 2, NULL)")
    addr = c.execute("SELECT UID FROM ABMultiValue WHERE property=5").fetchone()[0]
    for key, value in ((1, "88 Elm St"), (2, "Austin"), (3, "TX"), (4, "78704")):
        c.execute("INSERT INTO ABMultiValueEntry VALUES (?, ?, ?)", (addr, key, value))
    c.execute("INSERT INTO ABPerson (Organization, Kind) VALUES ('Movement Gym', 1)")
    c.execute("INSERT INTO ABMultiValue (record_id, property, identifier, label, value) "
              "VALUES (2, 3, 0, 3, '(512) 555-0150')")
    c.commit()
    c.close()


def make_ios_backup(root: Path, udid="00008110-CONTACTS", encrypted=False) -> Path:
    backup = root / udid
    backup.mkdir(parents=True)
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": encrypted}))
    (backup / "Info.plist").write_bytes(plistlib.dumps(
        {"Device Name": "Alex's iPhone", "Last Backup Date": datetime(2026, 10, 20, 8, 0)}))
    man = sqlite3.connect(backup / "Manifest.db")
    man.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                "relativePath TEXT, flags INTEGER, file BLOB)")
    domain, rel = contacts.IOS_BOOK
    file_id = hashlib.sha1(f"{domain}-{rel}".encode()).hexdigest()
    man.execute("INSERT INTO Files VALUES (?, ?, ?, 1, NULL)", (file_id, domain, rel))
    man.commit()
    man.close()
    (backup / file_id[:2]).mkdir()
    _ios_book(backup / file_id[:2] / file_id)
    return backup


def test_an_iphone_backup_reads_the_address_book_through_the_manifest(cfg, tmp_path):
    backup = make_ios_backup(tmp_path / "Backup")
    scan = contacts.scan(backup, cfg)
    assert scan["account"] == "iphone:00008110-CONTACTS"
    assert scan["source_detail"] == "iPhone backup of Alex's iPhone, taken 2026-10-20"
    sam, gym = _by_title(scan)["Sam Okafor"], _by_title(scan)["Movement Gym"]
    assert sam["external_id"] == "ios:00008110-CONTACTS:1"
    assert sam["body"].splitlines() == ["Lives at 88 Elm St, Austin, TX 78704",
                                        "Birthday: August 21, 1992", "Spots me on bench."]
    assert [(p["label"], p["number"]) for p in _detail(sam)["phones"]] == [
        ("mobile", "(512) 555-0166"), ("Gym", "512-555-0167")]
    assert sam["started_at"] == "2026-09-30T14:00:00Z"
    assert gym["participants"] == [{"kind": "phone", "value": "+15125550150",
                                    "role": "participant"}]
    # the MobileSync Backup folder holding just this one backup works too
    assert contacts.scan(tmp_path / "Backup", cfg)["records"] == scan["records"]


def test_an_encrypted_backup_is_refused(cfg, tmp_path):
    with pytest.raises(DomainError, match="encrypted backup"):
        contacts.scan(make_ios_backup(tmp_path, encrypted=True), cfg)


def test_the_texts_fixture_backup_names_handles_as_ios_messages_does(cfg, tmp_path):
    """The address book ios_messages reads names from is the same one read
    here, with the same 'Dad / Mom' answer for a shared landline."""
    from test_ios_messages import make_backup
    scan = contacts.scan(make_backup(tmp_path), cfg)
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", "+15125550000")] == "Dad / Mom"
    assert names[("phone", "+15125551234")] == "Jake Reilly"
    assert names[("email", "sam@icloud.com")] == "Sam Ortiz"
    assert "Acme Corp" in _by_title(scan)


# ---------------------------------------------------------------- mirroring
# Contacts mirror the address book (2026-10-21, with this connector): a card
# deleted in Contacts leaves the archive on the next import.

def _delete_card(store: Path, first: str) -> None:
    c = sqlite3.connect(store)
    c.execute("DELETE FROM ZABCDRECORD WHERE ZFIRSTNAME=?", (first,))
    c.commit()
    c.close()


def test_a_card_deleted_from_the_address_book_is_removed(cfg, conn, tmp_path):
    book = mac_folder(tmp_path)
    _apply(conn, contacts.scan(book, cfg))
    _delete_card(book / "Sources" / "ICLOUD-1" / contacts.STORE, "Rebecca")
    scan = contacts.scan(book, cfg)
    plan = obs.build_plan(conn, scan)
    assert [r["title"] for r in plan["removed"]] == ["Dr. Rebecca Ellis"]
    assert "remove: 1 no longer in the address book (deleted)" in "\n".join(
        contacts.report_lines(scan, plan))
    _, result = _apply(conn, scan)
    assert result["removed"] == 1
    assert run_checks(conn, cfg)["failures"] == 0


def test_another_accounts_cards_are_untouched(cfg, conn, tmp_path):
    _apply(conn, contacts.scan(mac_folder(tmp_path / "mac"), cfg))
    backup = make_ios_backup(tmp_path / "phone")
    _apply(conn, contacts.scan(backup, cfg))
    other = mac_folder(tmp_path / "mac2", icloud=[JAKE_MAC], top_uuid="TOP-UUID-2")
    plan = obs.build_plan(conn, contacts.scan(other, cfg))
    assert plan["removed"] == []              # Ellis belongs to TOP-UUID-1's scope


def test_a_vcard_mirrors_only_with_a_configured_account(cfg, conn, tmp_path):
    vcf = _write(tmp_path / "contacts.vcf", GOOGLE)
    _apply(conn, contacts.scan(vcf, cfg))
    jake_only = GOOGLE.split("BEGIN:VCARD\nVERSION:3.0\nFN:Ellis")[0]
    _write(vcf, jake_only)
    plan = obs.build_plan(conn, contacts.scan(vcf, cfg))
    assert plan["removed"] == [] and plan["mirror_note"]

    cfg.connectors = {"contacts": {"account": "google:alex"}}
    conn.execute("DELETE FROM observations")           # start over under the account
    _write(vcf, GOOGLE)
    _apply(conn, contacts.scan(vcf, cfg))
    _write(vcf, jake_only)
    scan = contacts.scan(vcf, cfg)
    assert scan["account"] == "google:alex"
    assert [r["title"] for r in obs.build_plan(conn, scan)["removed"]] == ["Ellis Dental"]


def test_mass_removal_is_refused(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "MASS_REMOVAL_MIN", 1)
    book = mac_folder(tmp_path)
    _apply(conn, contacts.scan(book, cfg))
    store = book / "Sources" / "ICLOUD-1" / contacts.STORE
    _delete_card(store, "Rebecca")
    _delete_card(store, "Jake")
    scan = contacts.scan(book, cfg)
    plan = obs.build_plan(conn, scan)
    assert len(plan["removed"]) == 2 and obs.mass_removal(plan)
    with pytest.raises(DomainError, match="--allow-mass-removal"):
        obs.apply_plan(conn, scan, plan)


# ---------------------------------------------------------------- finding it

def _home(monkeypatch, tmp_path, system):
    monkeypatch.setattr(platforms, "SYSTEM", system)
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))


def test_default_target_prefers_the_configured_path(cfg, tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path, "mac")
    vcf = _write(tmp_path / "exports" / "contacts.vcf", V4)
    cfg.connectors = {"contacts": {"path": "exports/contacts.vcf"}}
    assert contacts.default_target(cfg) == vcf                 # relative to the root
    cfg.connectors = {"contacts": {"path": str(tmp_path / "nope.vcf")}}
    with pytest.raises(DomainError, match="does not exist"):
        contacts.default_target(cfg)


def test_default_target_on_a_mac_is_the_contacts_folder(cfg, tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path, "mac")
    book = tmp_path / "Library" / "Application Support" / "AddressBook"
    book.mkdir(parents=True)
    assert contacts.default_target(cfg) == book


def test_default_target_elsewhere_is_the_newest_vcf_in_downloads(cfg, tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path, "windows")
    _write(tmp_path / "Downloads" / "old.vcf", V4, mtime="2026-01-01T00:00:00Z")
    new = _write(tmp_path / "Downloads" / "contacts.vcf", V4, mtime="2026-10-01T00:00:00Z")
    assert contacts.default_target(cfg) == new


def test_default_target_with_a_vcard_account_refuses_to_guess(cfg, tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path, "windows")
    _write(tmp_path / "Downloads" / "contacts.vcf", V4)
    cfg.connectors = {"contacts": {"account": "google:alex"}}
    with pytest.raises(DomainError, match="name the export to mirror"):
        contacts.default_target(cfg)


def test_default_target_says_where_it_looked(cfg, tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path, "mac")
    with pytest.raises(DomainError) as e:
        contacts.default_target(cfg)
    assert "AddressBook" in str(e.value) and "Downloads" in str(e.value)


def test_permission_denied_asks_for_full_disk_access(cfg, tmp_path, monkeypatch):
    book = mac_folder(tmp_path)
    monkeypatch.setattr(platforms, "SYSTEM", "mac")

    def denied(target):
        raise PermissionError(1, "Operation not permitted", str(target / "Sources"))
    monkeypatch.setattr(contacts, "_mac_stores", denied)
    with pytest.raises(DomainError, match="Full Disk Access"):
        contacts.scan(book, cfg)


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_apply_reimport_and_already_imported(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    vcf = _write(tmp_path / "exp" / "contacts.vcf", GOOGLE + V4)
    r = runner.invoke(app, ["observe", "contacts", str(vcf)])
    assert r.exit_code == 0, r.output
    assert "contact_card: 3 new" in r.output and "dry run — nothing written" in r.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0
    r = runner.invoke(app, ["observe", "contacts", str(vcf), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 3 added" in r.output
    again = runner.invoke(app, ["observe", "contacts", str(vcf), "--apply"])
    assert "0 added" in again.output and "3 identical skipped" in again.output
    assert "already imported" in runner.invoke(app, ["observe", "contacts", str(vcf)]).output
    r = runner.invoke(app, ["connectors"])
    assert "contacts" in r.output and "contact_card" in r.output


def test_cli_suggest_splits_joined_names_skips_placeholders_and_reads_card_names(
        tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    from lifecrm import db as db_mod
    from lifecrm.config import load_config
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    for ent_name, aliases in (("Alex Rivera", []), ("Linda Rivera", ["Mom"]),
                              ("Jake Morales", [])):
        entities.create(conn, "person", ent_name, aliases=aliases)
    # A texts-like import first: the owner's number under a placeholder, and
    # Jake's number with no name at all.
    texts = base.empty_scan("ios_messages", account="+15125550100", records=[{
        "stream": "imessage", "external_id": "T1", "journal_date": "2026-10-20",
        "started_at": "2026-10-20T15:00:00Z", "direction": "out", "body": "Veracruz at 7?",
        "origin_app": "messages", "last_modified_at": "2026-10-20T15:00:00Z",
        "participants": [{"kind": "phone", "value": "+15125550100", "role": "sender"},
                         {"kind": "phone", "value": "+15125550142", "role": "recipient"}]}],
        handles=[{"kind": "phone", "value": "+15125550100", "display_name": "you (this phone)"},
                 {"kind": "phone", "value": "+15125550142", "display_name": None}])
    _apply(conn, texts)
    jake_card = {"stream": "contact_card", "external_id": "J1", "journal_date": "2026-10-20",
                 "started_at": "2026-10-20T15:00:00Z", "title": "Jake Morales",
                 "origin_app": "contacts", "last_modified_at": "2026-10-20T15:00:00Z",
                 "participants": [{"kind": "phone", "value": "+15125550142",
                                   "role": "participant"}]}
    _apply(conn, base.empty_scan("contacts", records=[jake_card], handles=[
        {"kind": "phone", "value": "+15125550142", "display_name": None}]))
    conn.close()
    vcf = _write(tmp_path / "exp" / "contacts.vcf",
                 "BEGIN:VCARD\nVERSION:3.0\nFN:Alex Rivera\nTEL:+1 512 555 0100\nEND:VCARD\n"
                 "BEGIN:VCARD\nVERSION:3.0\nFN:Mom\nTEL:(210) 555-0100\nEND:VCARD\n"
                 "BEGIN:VCARD\nVERSION:3.0\nFN:Dad\nTEL:210-555-0100\nEND:VCARD\n")
    assert runner.invoke(app, ["observe", "contacts", str(vcf), "--apply"]).exit_code == 0

    r = runner.invoke(app, ["handles", "list", "--suggest", "--json"])
    assert r.exit_code == 0, r.output
    rows = {row["value"]: row for row in json.loads(r.output)}
    landline = rows["+12105550100"]
    assert landline["display_name"] == "Dad / Mom"
    assert [s["name"] for s in landline["suggestions"]] == ["Linda Rivera"]   # via 'Mom'
    owner = rows["+15125550100"]
    assert owner["display_name"] == "you (this phone)"          # first name sticks
    assert [s["name"] for s in owner["suggestions"]] == ["Alex Rivera"]      # the card's
    jake = rows["+15125550142"]
    assert jake["display_name"] is None
    assert [s["name"] for s in jake["suggestions"]] == ["Jake Morales"]
    text = runner.invoke(app, ["handles", "list", "--suggest"]).output
    assert "maybe: Linda Rivera (person" in text
    assert "handles link phone:+12105550100 --entity" in text
    r = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM entity_handles "
                                   "WHERE entity_id IS NOT NULL", "--json"])
    assert json.loads(r.output)[0]["c"] == 0                     # suggested, not linked


# ---------------------------------------------------------------- doctor

@pytest.mark.parametrize("source", ["vcard", "mac", "iphone"])
def test_doctor_stays_green_after_a_contacts_import(source, conn, cfg, tmp_path):
    from test_doctor import _seed
    _seed(conn, cfg)
    target = {"vcard": lambda: _write(tmp_path / "c.vcf", GOOGLE + SAMSUNG + V4),
              "mac": lambda: mac_folder(tmp_path),
              "iphone": lambda: make_ios_backup(tmp_path / "Backup")}[source]()
    _apply(conn, contacts.scan(target, cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [(c["check"], c.get("detail"))
                                     for c in report["checks"] if c["status"] == "fail"]

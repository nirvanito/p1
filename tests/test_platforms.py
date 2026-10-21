"""Platform plumbing: where things live on Windows vs a Mac, the --root
option, connector discovery, and the demo persona's internal consistency.

These run on any OS; Mac-specific behavior is exercised by pointing the
functions at a fake home directory rather than by needing a Mac.
"""

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.connectors import base as conn_base
from lifecrm.demo import persona

runner = CliRunner()


# ---------------------------------------------------------------- platforms

def test_venv_python_matches_the_os(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    assert platforms.venv_python(tmp_path) == tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(platforms, "SYSTEM", "mac")
    assert platforms.venv_python(tmp_path) == tmp_path / ".venv" / "bin" / "python"


def test_mac_journal_root_is_icloud_drive(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "SYSTEM", "mac")
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    root = platforms.default_journal_root()
    assert root == tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "VoiceJournal"


def test_icloud_placeholder_names():
    stub = Path(".2026-07-27 21.30.05 -0500.m4a.icloud")
    assert platforms.is_icloud_placeholder(stub)
    assert platforms.icloud_placeholder_target(stub) == "2026-07-27 21.30.05 -0500.m4a"
    assert not platforms.is_icloud_placeholder(Path("note.m4a"))


def test_mac_addressbook_finds_every_source(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    base = tmp_path / "Library" / "Application Support" / "AddressBook"
    (base / "Sources" / "A").mkdir(parents=True)
    (base / "Sources" / "B").mkdir(parents=True)
    for p in (base / "AddressBook-v22.abcddb",
              base / "Sources" / "A" / "AddressBook-v22.abcddb",
              base / "Sources" / "B" / "AddressBook-v22.abcddb"):
        p.write_bytes(b"")
    assert len(platforms.mac_addressbook_dbs()) == 3


def test_config_expands_home_in_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[paths]\ninbox_dir = '~/VoiceJournal/inbox'\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.inbox_dir == tmp_path / "VoiceJournal" / "inbox"


# ---------------------------------------------------------------- cli plumbing

def test_root_option_points_commands_at_another_root(tmp_path, monkeypatch):
    monkeypatch.delenv("LIFECRM_ROOT", raising=False)
    (tmp_path / "config.toml").write_text(
        '[paths]\ndata_dir = "data"\nbackups_dir = "backups"\n', encoding="utf-8")
    r = runner.invoke(app, ["--root", str(tmp_path), "init"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "data" / "lifecrm.db").exists()


def test_connectors_are_discovered_not_listed():
    reg = conn_base.registry()
    assert {"ai_chat", "ios_messages"} <= set(reg)
    assert "typedstream" not in reg and "base" not in reg
    for name, mod in reg.items():
        assert mod.name == name and callable(mod.scan) and mod.streams


# ---------------------------------------------------------------- persona

def test_persona_story_is_internally_consistent():
    anchor = date(2026, 10, 21)
    keys = {p.key for p in persona.PEOPLE} | {"me"}
    place_keys = {p.key for p in persona.PLACES}
    for t in persona.THREADS:
        for _, _, sender, _ in t.messages:
            assert sender in keys
    for j in persona.JOURNAL:
        assert -persona.WINDOW_DAYS < j.offset <= 0
        for n in j.notes:
            for _, spec in n.sentences:
                for ref, _role in (spec or {}).get("entities", []):
                    if ":" not in ref:
                        assert ref in keys | place_keys, ref
    trip = persona.bled_trip_days(anchor)
    assert trip[0].year == anchor.year - 4 and len(trip) == 5
    series = persona.health_series(anchor)
    assert len(series) == persona.WINDOW_DAYS and series[-1].date == anchor
    assert series == persona.health_series(anchor)          # deterministic
    said = {j.offset: j.sleep_hours for j in persona.JOURNAL if j.sleep_hours}
    for h in series:
        off = (h.date - anchor).days
        if off in said:
            assert h.sleep_hours == pytest.approx(said[off], abs=0.01)


def test_persona_keys_referenced_by_closures_exist():
    keyed = {spec["key"] for j in persona.JOURNAL for n in j.notes
             for _, spec in n.sentences if spec and "key" in spec}
    for j in persona.JOURNAL:
        for n in j.notes:
            for _, spec in n.sentences:
                if not spec:
                    continue
                if "closes" in spec:
                    assert spec["closes"] in keyed
                for _t, k in spec.get("links", []):
                    assert k in keyed, k


# ---------------------------------------------------------------- Mac Contacts store

def make_abcddb(path, people):
    """A minimal AddressBook-v22.abcddb with the columns Contacts writes.
    `people`: list of dicts with first/last/org/phones/emails/city/note/bday
    (bday as Core Data seconds)."""
    import sqlite3
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
            ZUNIQUEID TEXT, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZMIDDLENAME TEXT,
            ZNICKNAME TEXT, ZORGANIZATION TEXT, ZJOBTITLE TEXT, ZBIRTHDAY REAL,
            ZCREATIONDATE REAL, ZMODIFICATIONDATE REAL, ZNOTE INTEGER);
        CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
            ZFULLNUMBER TEXT, ZLABEL TEXT, ZORDERINGINDEX INTEGER);
        CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
            ZADDRESS TEXT, ZLABEL TEXT, ZORDERINGINDEX INTEGER);
        CREATE TABLE ZABCDPOSTALADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER,
            ZSTREET TEXT, ZCITY TEXT, ZSTATE TEXT, ZZIPCODE TEXT, ZCOUNTRYNAME TEXT,
            ZLABEL TEXT);
        CREATE TABLE ZABCDNOTE (Z_PK INTEGER PRIMARY KEY, ZCONTACT INTEGER, ZTEXT TEXT);
    """)
    for i, p in enumerate(people, start=1):
        c.execute("INSERT INTO ZABCDRECORD (Z_PK, Z_ENT, ZUNIQUEID, ZFIRSTNAME, ZLASTNAME, "
                  "ZORGANIZATION, ZBIRTHDAY, ZCREATIONDATE, ZMODIFICATIONDATE) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (i, 22, f"UID-{i}:ABPerson", p.get("first"), p.get("last"), p.get("org"),
                   p.get("bday"), 700000000.0, 750000000.0 + i))
        for n, ph in enumerate(p.get("phones", [])):
            c.execute("INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER, ZLABEL, ZORDERINGINDEX) "
                      "VALUES (?,?,?,?)", (i, ph, "_$!<Mobile>!$_", n))
        for n, em in enumerate(p.get("emails", [])):
            c.execute("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS, ZLABEL, ZORDERINGINDEX) "
                      "VALUES (?,?,?,?)", (i, em, "_$!<Home>!$_", n))
        if p.get("city"):
            c.execute("INSERT INTO ZABCDPOSTALADDRESS (ZOWNER, ZCITY, ZSTATE, ZLABEL) "
                      "VALUES (?,?,?,?)", (i, p["city"], p.get("state"), "_$!<Home>!$_"))
        if p.get("note"):
            c.execute("INSERT INTO ZABCDNOTE (ZCONTACT, ZTEXT) VALUES (?,?)", (i, p["note"]))
    c.execute("INSERT INTO ZABCDRECORD (Z_PK, Z_ENT) VALUES (99, 19)")   # a group
    c.commit()
    c.close()


def test_mac_contacts_store_reads_names_numbers_addresses_notes(tmp_path):
    import sqlite3
    from lifecrm.connectors import _abcddb
    db = tmp_path / "AddressBook-v22.abcddb"
    # 1993-03-14 12:00 UTC and a no-year (1604) birthday
    bday = (datetime(1993, 3, 14, 12, tzinfo=timezone.utc) - _abcddb.APPLE_EPOCH).total_seconds()
    noyear = (datetime(1604, 8, 21, 12, tzinfo=timezone.utc) - _abcddb.APPLE_EPOCH).total_seconds()
    make_abcddb(db, [
        {"first": "Jake", "last": "Morales", "phones": ["(512) 555-0142"],
         "emails": ["jake.morales@example.com"], "city": "Austin", "state": "TX",
         "note": "Grew up in El Paso.", "bday": bday},
        {"org": "Ellis Dental", "phones": ["+1 512 555 0199"], "bday": noyear},
    ])
    conn = sqlite3.connect(db)
    cards = _abcddb.read_contacts(conn)
    assert [c["display_name"] for c in cards] == ["Jake Morales", "Ellis Dental"]
    jake = cards[0]
    assert jake["phones"] == [("Mobile", "(512) 555-0142")]
    assert jake["emails"] == [("Home", "jake.morales@example.com")]
    assert jake["addresses"][0]["city"] == "Austin"
    assert jake["note"] == "Grew up in El Paso."
    assert jake["birthday"] == "1993-03-14"
    assert cards[1]["birthday"] == "--08-21"
    assert jake["modified_at"].endswith("Z")


def test_mac_contacts_birthday_survives_either_storage_convention():
    from lifecrm.connectors import _abcddb
    def secs(dt):
        return (dt - _abcddb.APPLE_EPOCH).total_seconds()
    noon = datetime(1993, 3, 14, 12, tzinfo=timezone.utc)
    chicago_midnight = datetime(1993, 3, 14, 6, tzinfo=timezone.utc)
    tokyo_midnight = datetime(1993, 3, 13, 15, tzinfo=timezone.utc)
    for dt in (noon, chicago_midnight, tokyo_midnight):
        assert _abcddb.birthday(secs(dt)) == "1993-03-14", dt

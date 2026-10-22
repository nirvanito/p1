"""iPhone call history connector against synthetic CallHistory.storedata stores.

The store is written with the real Core Data table and column names
(Z_METADATA, ZCALLRECORD and its Z-prefixed columns), placed where a Mac
keeps it — <home>/Library/Application Support/CallHistoryDB/ — or inside a
synthetic iPhone backup (test_ios_messages.make_backup) under its SHA1 file
id, exactly as the Manifest maps it.
"""

import hashlib
import json
import plistlib
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from lifecrm import platforms
from lifecrm.connectors import base, ios_calls as ic, ios_messages as im
from lifecrm.demo import persona
from lifecrm.demo.sources import ios_calls as sample
from lifecrm.demo.sources import mac_messages as messages_sample
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks
from test_ios_messages import make_backup
from test_platforms import make_abcddb

APPLE_EPOCH = 978307200
STORE_UUID = "5D0E3C3A-1B7F-4C1E-9A41-2B6F0D9E7C11"

COLUMNS = ("Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZANSWERED INTEGER, "
           "ZCALLTYPE INTEGER, ZHANDLE_TYPE INTEGER, ZORIGINATED INTEGER, ZREAD INTEGER, "
           "ZDATE TIMESTAMP, ZDURATION FLOAT, ZADDRESS VARCHAR, ZISO_COUNTRY_CODE VARCHAR, "
           "ZLOCATION VARCHAR, ZNAME VARCHAR, ZSERVICE_PROVIDER VARCHAR, ZUNIQUE_ID VARCHAR")


def _cd(iso: str) -> float:
    """Core Data time: seconds since 2001-01-01 UTC (with sub-second noise)."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.timestamp() - APPLE_EPOCH + 0.6


def _calls(variant="all"):
    rows = [
        dict(uid="C1", address="+15125551234", at="2026-09-01T18:00:00Z", dur=125.4,
             out=0, answered=1, kind=1, name="Jake R."),
        dict(uid="C2", address="5125559876", at="2026-09-02T06:30:00Z", dur=60.0,
             out=1, answered=1, kind=1, name="Dana"),
        dict(uid="C3", address="+15125551234", at="2026-09-03T15:00:00Z", dur=0.0,
             out=0, answered=0, kind=1),
        dict(uid="C4", address="+15125550000", at="2026-09-03T16:00:00Z", dur=0.0,
             out=1, answered=0, kind=1),
        dict(uid="C5", address="sam@icloud.com", at="2026-09-04T19:00:00Z", dur=600.0,
             out=1, answered=1, kind=8, provider="com.apple.FaceTime"),
        dict(uid="C6", address="Sam@iCloud.com", at="2026-09-05T19:00:00Z", dur=30.0,
             out=0, answered=1, kind=16, provider="com.apple.FaceTime"),
        dict(uid="C7", address="+447700900123", at="2026-09-06T12:00:00Z", dur=90.0,
             out=1, answered=1, kind=1, provider="net.whatsapp.WhatsApp", country="gb"),
        dict(uid="C8", address="+15125551234", at="2026-09-07T12:00:00Z", dur=10.0,
             out=0, answered=1, kind=7),
        dict(uid="C9", address=None, at="2026-09-08T12:00:00Z", dur=45.0,
             out=0, answered=1, kind=1),
        dict(uid="C10", address="+15125551234", at=None, dur=5.0, out=0, answered=1,
             kind=1),
        dict(uid="C11", address=b"+15125559876", at="2026-09-09T12:00:00Z", dur=20.0,
             out=0, answered=1, kind=1),
    ]
    if variant == "deleted":
        rows = [r for r in rows if r["uid"] != "C1"]
    if variant == "few":
        rows = [r for r in rows if r["uid"] in ("C1", "C2")]
    return rows


def make_store(path: Path, variant="all", store_uuid=STORE_UUID, columns=COLUMNS) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255), "
              "Z_PLIST BLOB)")
    if store_uuid:
        c.execute("INSERT INTO Z_METADATA VALUES (1, ?, NULL)", (store_uuid,))
    c.execute(f"CREATE TABLE ZCALLRECORD ({columns})")
    have = {r[1] for r in c.execute("PRAGMA table_info(ZCALLRECORD)")}
    for pk, r in enumerate(_calls(variant), start=1):
        row = {"Z_PK": pk, "Z_ENT": 1, "Z_OPT": 1, "ZANSWERED": r["answered"],
               "ZCALLTYPE": r["kind"], "ZORIGINATED": r["out"],
               "ZHANDLE_TYPE": 3 if r["address"] and "@" in str(r["address"]) else 2,
               "ZDATE": _cd(r["at"]) if r["at"] else None, "ZDURATION": r["dur"],
               "ZADDRESS": r["address"], "ZISO_COUNTRY_CODE": r.get("country", "us"),
               "ZNAME": r.get("name"),
               "ZSERVICE_PROVIDER": r.get("provider", "com.apple.Telephony"),
               "ZUNIQUE_ID": r["uid"]}
        row = {k: v for k, v in row.items() if k in have}
        c.execute(f"INSERT INTO ZCALLRECORD ({', '.join(row)}) VALUES "
                  f"({', '.join('?' for _ in row)})", list(row.values()))
    c.commit()
    c.close()
    return path


def make_mac_home(home: Path, variant="all", contacts=True, **kw) -> Path:
    support = home / "Library" / "Application Support"
    if contacts:
        book = support / "AddressBook" / "Sources" / "ICLOUD"
        book.mkdir(parents=True, exist_ok=True)
        make_abcddb(book / "AddressBook-v22.abcddb", [
            {"first": "Jake", "last": "Reilly", "phones": ["(512) 555-1234"]},
            {"first": "Sam", "last": "Ortiz", "emails": ["sam@icloud.com"]},
        ])
    return make_store(support / "CallHistoryDB" / "CallHistory.storedata", variant, **kw)


def add_call_history(backup: Path, variant="all", phone="+1 (512) 555-0100") -> Path:
    """Put the store in a backup the way Apple Devices does: under its SHA1
    file id, with a Manifest row — and the phone's number in Info.plist."""
    file_id = hashlib.sha1(b"HomeDomain-Library/CallHistoryDB/CallHistory.storedata"
                           ).hexdigest()
    make_store(backup / file_id[:2] / file_id, variant)
    man = sqlite3.connect(backup / "Manifest.db")
    man.execute("INSERT OR REPLACE INTO Files VALUES (?, 'HomeDomain', "
                "'Library/CallHistoryDB/CallHistory.storedata', 1, NULL)", (file_id,))
    man.commit()
    man.close()
    info = plistlib.loads((backup / "Info.plist").read_bytes())
    if phone:
        info["Phone Number"] = phone
    (backup / "Info.plist").write_bytes(plistlib.dumps(info))
    return backup


def _by_id(scan):
    return {r["external_id"]: r for r in scan["records"]}


def _detail(rec):
    return json.loads(rec["value_json"]) if rec["value_json"] else {}


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- reading

def test_each_call_record_is_one_observation(cfg, tmp_path):
    scan = ic.scan(make_mac_home(tmp_path), cfg)
    assert scan["connector"] == "ios_calls"
    assert sorted(_by_id(scan)) == ["C1", "C11", "C2", "C3", "C4", "C5", "C6", "C7",
                                    "C8", "C9"]
    assert {r["stream"] for r in scan["records"]} == {"ios_call"}
    assert ic.streams == ("ios_call",) and ic.stale_after_days == 14
    assert scan["records_seen"] == 11


def test_direction_duration_and_time(cfg, tmp_path):
    recs = _by_id(ic.scan(make_mac_home(tmp_path), cfg))
    jake = recs["C1"]
    assert jake["direction"] == "in" and jake["started_at"] == "2026-09-01T18:00:00Z"
    assert jake["value"] == 125 and jake["unit"] == "s"
    assert jake["ended_at"] == "2026-09-01T18:02:05Z"
    assert jake["title"] == "phone call" and jake["origin_app"] == "phone"
    assert _detail(jake) == {"answered": True, "call_type": "phone", "country": "us",
                             "service": "com.apple.Telephony"}
    # 06:30Z is 01:30 in Chicago, before the 05:00 rollover: the previous day
    assert recs["C2"]["direction"] == "out" and recs["C2"]["journal_date"] == "2026-09-01"


def test_a_missed_call_is_an_incoming_one_never_answered(cfg, tmp_path):
    recs = _by_id(ic.scan(make_mac_home(tmp_path), cfg))
    assert recs["C3"]["title"] == "missed phone call"
    assert _detail(recs["C3"])["missed"] is True and recs["C3"]["ended_at"] is None
    assert recs["C3"]["value"] == 0
    # an outgoing call nobody picked up is not a missed call
    assert recs["C4"]["title"] == "phone call, not answered"
    assert "missed" not in _detail(recs["C4"])


def test_facetime_and_app_calls_are_told_apart(cfg, tmp_path):
    scan = ic.scan(make_mac_home(tmp_path), cfg)
    recs = _by_id(scan)
    assert (recs["C5"]["title"], recs["C5"]["origin_app"]) == ("FaceTime video call",
                                                               "facetime")
    assert _detail(recs["C6"])["call_type"] == "facetime_audio"
    assert (recs["C7"]["title"], recs["C7"]["origin_app"]) == ("app call",
                                                               "net.whatsapp.WhatsApp")
    assert _detail(recs["C7"])["country"] == "gb"
    assert _detail(recs["C8"])["call_type"] == "type 7"
    assert {"what": "call type 7 (imported as a call)", "count": 1} in scan["unmapped"]


def test_participants_are_handles_named_from_contacts_then_the_phone(cfg, tmp_path):
    scan = ic.scan(make_mac_home(tmp_path), cfg)
    recs = _by_id(scan)
    assert recs["C1"]["participants"] == [{"kind": "phone", "value": "+15125551234",
                                           "role": "participant"}]
    assert recs["C6"]["participants"][0]["value"] == "sam@icloud.com"   # case folded
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", "+15125551234")] == "Jake Reilly"     # card beats ZNAME
    assert names[("phone", "+15125559876")] == "Dana"            # only the phone knew
    assert names[("email", "sam@icloud.com")] == "Sam Ortiz"
    assert names[("phone", "+447700900123")] is None
    assert scan["facts"]["address_books_read"] == 1


def test_an_address_stored_as_a_blob_still_reads(cfg, tmp_path):
    rec = _by_id(ic.scan(make_mac_home(tmp_path), cfg))["C11"]
    assert rec["participants"][0]["value"] == "+15125559876"


def test_a_call_with_no_number_is_kept_without_a_participant(cfg, tmp_path):
    rec = _by_id(ic.scan(make_mac_home(tmp_path), cfg))["C9"]
    assert rec["participants"] == [] and _detail(rec)["caller_unknown"] is True


def test_a_call_with_no_time_is_skipped_but_still_present(cfg, tmp_path):
    scan = ic.scan(make_mac_home(tmp_path), cfg)
    assert "C10" not in _by_id(scan) and "C10" in scan["present_ids"]
    assert "1 × call with no usable timestamp" in scan["skipped"]


def test_columns_an_older_ios_lacks_read_as_null(cfg, tmp_path):
    old = ("Z_PK INTEGER PRIMARY KEY, ZANSWERED INTEGER, ZCALLTYPE INTEGER, "
           "ZORIGINATED INTEGER, ZDATE TIMESTAMP, ZDURATION FLOAT, ZADDRESS VARCHAR, "
           "ZUNIQUE_ID VARCHAR")
    scan = ic.scan(make_store(tmp_path / "CallHistory.storedata", columns=old), cfg)
    rec = _by_id(scan)["C1"]
    assert rec["origin_app"] == "phone" and "service" not in _detail(rec)
    # without a service provider, the WhatsApp call reads as the phone call it says
    assert _by_id(scan)["C7"]["title"] == "phone call"


def test_a_record_without_a_unique_id_gets_a_stable_one(cfg, tmp_path):
    store = make_mac_home(tmp_path)
    c = sqlite3.connect(store)
    c.execute("UPDATE ZCALLRECORD SET ZUNIQUE_ID=NULL WHERE ZUNIQUE_ID='C1'")
    c.commit()
    c.close()
    a, b = ic.scan(store, cfg), ic.scan(store, cfg)
    made = [i for i in _by_id(a) if i.startswith("call:")]
    assert len(made) == 1 and made[0] in b["present_ids"]


def test_not_a_call_history_database_says_what_it_saw(cfg, tmp_path):
    db = tmp_path / "other.db"
    sqlite3.connect(db).executescript("CREATE TABLE misc (id INTEGER);").connection.close()
    with pytest.raises(DomainError, match="not a call history database.*misc"):
        ic.scan(db, cfg)


def test_a_folder_that_is_neither_says_what_it_saw(cfg, tmp_path):
    folder = tmp_path / "random"
    folder.mkdir()
    (folder / "notes.txt").write_text("x")
    with pytest.raises(DomainError, match="neither an iPhone backup.*notes.txt"):
        ic.scan(folder, cfg)


def test_the_folder_holding_the_store_is_accepted(cfg, tmp_path):
    store = make_mac_home(tmp_path)
    assert len(ic.scan(store.parent, cfg)["records"]) == 10


def test_two_scans_are_identical_and_the_source_is_untouched(cfg, tmp_path):
    store = make_mac_home(tmp_path)
    before = store.read_bytes()
    a, b = ic.scan(store, cfg), ic.scan(store, cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]
    assert store.read_bytes() == before


def test_the_report_reads_like_the_other_connectors(cfg, conn, tmp_path):
    scan = ic.scan(make_mac_home(tmp_path), cfg)
    text = "\n".join(ic.report_lines(scan, obs.build_plan(conn, scan)))
    assert "ios-calls: CallHistory.storedata (call history file; 11 call record(s))" in text
    assert f"account: callhistory:{STORE_UUID}" in text
    assert "ios_call: 10 new (1 missed), 0 changed | 2026-09-01..2026-09-09" in text
    assert "e.g. 2026-09-09 them: phone call (0m20s) with +15125559876" in text
    assert "5125559876 -> +15125559876 (Dana)" in text
    assert "not understood: 1 × call type 7" in text


# ---------------------------------------------------------------- backups

def test_the_file_id_is_sha1_of_domain_and_path():
    assert ic.CALL_HISTORY_FILE_ID == "5a4935c78a5255723f707230a451d79c540d2741"


def test_a_backup_with_call_history_is_read_through_its_manifest(cfg, conn, tmp_path):
    backup = add_call_history(make_backup(tmp_path))
    scan = ic.scan(backup, cfg)
    assert scan["target_kind"] == "backup" and len(scan["records"]) == 10
    assert scan["account"] == "+15125550100"                 # Info.plist's number
    assert scan["device"] == "Test iPhone"
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", "+15125551234")] == "Jake Reilly"   # the backup's book
    text = "\n".join(ic.report_lines(scan, obs.build_plan(conn, scan)))
    assert "(iOS backup of Test iPhone, taken 2026-09-20; 11 call record(s))" in text


def test_a_backup_without_call_history_points_at_the_mac(cfg, tmp_path):
    with pytest.raises(DomainError, match="no call history.*unencrypted backups.*Mac"):
        ic.scan(make_backup(tmp_path), cfg)


def test_an_encrypted_backup_is_refused(cfg, tmp_path):
    with pytest.raises(DomainError, match="encrypted backup"):
        ic.scan(make_backup(tmp_path, encrypted=True), cfg)


def test_a_backup_that_names_no_number_is_scoped_to_its_store(cfg, tmp_path):
    backup = add_call_history(make_backup(tmp_path), phone=None)
    assert ic.scan(backup, cfg)["account"] == f"callhistory:{STORE_UUID}"


# ---------------------------------------------------------------- finding the store

def test_no_path_prefers_this_macs_call_history(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    store = make_mac_home(tmp_path)
    monkeypatch.setattr(im, "backup_roots", lambda: [tmp_path / "nowhere"])
    assert ic.default_target(cfg) == store
    scan = ic.scan(store, cfg)
    assert scan["target_kind"] == "live" and scan["facts"]["address_books_read"] == 1


def test_no_path_without_a_mac_store_uses_the_one_backup(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    root = tmp_path / "Backup"
    backup = add_call_history(make_backup(root))
    monkeypatch.setattr(im, "backup_roots", lambda: [root])
    assert ic.default_target(cfg) == backup


def test_no_path_and_nothing_found_says_where_it_looked(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(im, "backup_roots", lambda: [tmp_path / "Backup"])
    with pytest.raises(DomainError, match="CallHistoryDB.*Backup"):
        ic.default_target(cfg)


def test_a_store_macos_will_not_open_says_grant_full_disk_access(cfg, tmp_path,
                                                                monkeypatch):
    store = make_mac_home(tmp_path)
    real = sqlite3.connect

    def guarded(target, *a, **k):
        if "mode=ro" in str(target) and "CallHistoryDB" in str(target):
            raise sqlite3.OperationalError("authorization denied")
        return real(target, *a, **k)
    monkeypatch.setattr(sqlite3, "connect", guarded)
    with pytest.raises(DomainError, match="macOS did not allow reading .*CallHistory"
                                          r"\.storedata.*Full Disk Access"):
        ic.scan(store, cfg)


# ---------------------------------------------------------------- mirroring

def test_a_call_deleted_from_recents_is_removed(cfg, conn, tmp_path):
    _apply(conn, ic.scan(make_mac_home(tmp_path / "a"), cfg))
    plan, result = _apply(conn, ic.scan(make_mac_home(tmp_path / "b", variant="deleted"),
                                        cfg))
    assert [r["external_id"] for r in plan["removed"]] == ["C1"]
    assert result["removed"] == 1
    assert run_checks(conn, cfg)["failures"] == 0


def test_the_backup_and_the_mac_never_remove_each_others_calls(cfg, conn, tmp_path):
    _apply(conn, ic.scan(make_mac_home(tmp_path / "mac"), cfg))
    phone = ic.scan(add_call_history(make_backup(tmp_path / "phone"), variant="few"), cfg)
    plan = obs.build_plan(conn, phone)
    assert plan["removed"] == [] and plan["mirror_scope"] == 0
    assert plan["identical"] == 2                 # the same calls: one record each


def test_a_store_that_names_no_account_removes_nothing(cfg, conn, tmp_path):
    scan = ic.scan(make_store(tmp_path / "CallHistory.storedata", store_uuid=None), cfg)
    assert scan["account"] is None
    plan = obs.build_plan(conn, scan)
    assert "does not say which account" in plan["mirror_note"]
    text = "\n".join(ic.report_lines(scan, plan))
    assert "not mirroring deletions" in text


def test_reimport_is_a_no_op_and_the_confirmed_tier_is_untouched(cfg, conn, tmp_path):
    from test_doctor import _seed
    _seed(conn, cfg)
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities",
              "entry_sources", "extraction_runs")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in tables}
    store = make_mac_home(tmp_path)
    _apply(conn, ic.scan(store, cfg))
    plan = obs.build_plan(conn, ic.scan(store, cfg))
    assert not plan["new"] and not plan["updates"] and plan["identical"] == 10
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in tables}
    assert after == before
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- staleness

def _status(report, check):
    return next((c["status"] for c in report["checks"] if c["check"] == check), None)


def test_doctor_warns_when_the_newest_call_is_old(cfg, conn, tmp_path):
    assert _status(run_checks(conn, cfg), "ios_calls_data_age") is None
    _apply(conn, ic.scan(make_mac_home(tmp_path), cfg))
    assert _status(run_checks(conn, cfg), "ios_calls_data_age") == "warn"


def test_doctor_is_quiet_when_calls_are_current(cfg, conn):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _apply(conn, base.empty_scan("ios_calls", records=[{
        "stream": "ios_call", "external_id": "FRESH", "journal_date": now[:10],
        "started_at": now, "direction": "in", "value": 60, "unit": "s",
        "title": "phone call", "origin_app": "phone", "last_modified_at": now,
        "participants": []}]))
    assert _status(run_checks(conn, cfg), "ios_calls_data_age") == "ok"


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_apply_and_identical_rerun(tmp_path, monkeypatch):
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    store = make_mac_home(tmp_path / "home")

    r = runner.invoke(app, ["observe", "ios_calls", str(store)])
    assert r.exit_code == 0, r.output
    assert "ios-calls: CallHistory.storedata" in r.output
    assert "dry run — nothing written" in r.output
    r = runner.invoke(app, ["observe", "ios_calls", str(store), "--apply"])
    assert r.exit_code == 0 and "applied: 10 added" in r.output
    r = runner.invoke(app, ["observe", "ios_calls", str(store), "--apply"])
    assert "0 added" in r.output and "10 identical skipped" in r.output
    r = runner.invoke(app, ["observe", "ios_calls", str(store)])
    assert "already imported" in r.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0


# ---------------------------------------------------------------- the demo sample

ANCHOR = date(2026, 10, 21)


def test_the_demo_sample_renders_the_persona(cfg, tmp_path):
    store = sample.write_sample(tmp_path, ANCHOR, "iphone")
    assert store.name == "CallHistory.storedata" and store.parent.name == "CallHistoryDB"
    scan = ic.scan(store, cfg)
    assert len(scan["records"]) == len(persona.CALLS)
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", persona.person("mom").phone)] == "Linda Rivera"
    assert names[("phone", persona.person("jake").phone)] == "Jake Morales"
    assert names[("phone", persona.person("ellis").phone)] == "Rebecca Ellis"

    mom = [r for r in scan["records"]
           if r["participants"][0]["value"] == persona.person("mom").phone]
    assert sorted(r["value"] for r in mom) == [480, 720, 1320]
    first = min(mom, key=lambda r: r["started_at"])
    assert first["direction"] == "out"
    assert first["journal_date"] == persona.day(ANCHOR, -34).isoformat()
    missed = [r for r in scan["records"] if _detail(r).get("missed")]
    assert [m["participants"][0]["value"] for m in missed] == [
        persona.person("priya").phone]
    assert scan["account"] == f"callhistory:{sample.STORE_UUID}"
    assert not scan["unmapped"] and not scan["skipped"]


def test_the_demo_sample_imports_cleanly_and_reimports_identically(cfg, conn, tmp_path):
    store = sample.write_sample(tmp_path, ANCHOR, "iphone")
    _, result = _apply(conn, ic.scan(store, cfg))
    assert result["added"] == len(persona.CALLS)
    plan = obs.build_plan(conn, ic.scan(sample.write_sample(tmp_path, ANCHOR, "iphone"),
                                        cfg))
    assert not plan["new"] and not plan["updates"] and not plan["conflicts"]
    assert plan["identical"] == len(persona.CALLS)
    assert run_checks(conn, cfg)["failures"] == 0


def test_the_demo_samples_share_one_contacts_store_in_one_home(cfg, tmp_path):
    messages_sample.write_sample(tmp_path, ANCHOR, "iphone")
    sample.write_sample(tmp_path, ANCHOR, "iphone")
    stores = list((tmp_path / "Library").rglob("AddressBook-v22.abcddb"))
    assert len(stores) == 1


def test_the_demo_sample_has_no_android_flavor(tmp_path):
    with pytest.raises(ValueError, match="android"):
        sample.write_sample(tmp_path, ANCHOR, "android")

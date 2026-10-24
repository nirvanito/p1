"""The third demo combo, `windows-iphone`: a Windows laptop with an iPhone.

Each source this setup has is rendered as the export that setup really
produces — an unencrypted Apple Devices backup for the texts, an iCloud.com
vCard, iCloud's Home.ics, an iCloud for Windows photos folder, Edge history,
the Gmail Takeout — and read by the real connector. The sources it does not
have (calls, Apple Notes, location) are skipped with their reason. Then the
whole demo is built through the real CLI (in-process) and must end with
doctor green and every persona question answered with citations. The two
original combos are not touched: `both` still means them.
"""

import json
import os
import plistlib
import sqlite3
import tomllib
import zipfile
from datetime import date
from pathlib import Path

import pytest
from test_failure_visibility import needs_powershell
from test_fix_ops_scripts import _demo_ps, _demo_sh, _fake_repo, _ps_fake_repo, _records
from test_mac_scripts import needs_bash
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.connectors import browser as browser_connector
from lifecrm.connectors import calendar as calendar_connector
from lifecrm.connectors import contacts as contacts_connector
from lifecrm.connectors import email as email_connector
from lifecrm.connectors import ios_calls as ios_calls_connector
from lifecrm.connectors import ios_messages as im
from lifecrm.connectors import mac_messages as mac_connector
from lifecrm.connectors import photos as photos_connector
from lifecrm.demo import build as demo_build
from lifecrm.demo import journal, persona
from lifecrm.demo.build import DemoBuilder
from lifecrm.demo.sources import browser as browser_sample
from lifecrm.demo.sources import calendar as calendar_sample
from lifecrm.demo.sources import contacts as contacts_sample
from lifecrm.demo.sources import email as email_sample
from lifecrm.demo.sources import ios_calls as ios_calls_sample
from lifecrm.demo.sources import ios_messages as sample
from lifecrm.demo.sources import location as location_sample
from lifecrm.demo.sources import mac_messages as mac_sample
from lifecrm.demo.sources import notes as notes_sample
from lifecrm.demo.sources import photos as photos_sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.obsidian import NO_REFRESH_ENV

COMBO = "windows-iphone"
ANCHOR = date(2026, 10, 21)
runner = CliRunner()


# ---------------------------------------------------------------- the combo

def test_the_combo_is_a_windows_laptop_with_an_iphone():
    assert persona.COMBOS == ("android", "iphone", "windows-iphone")
    assert persona.BOTH == ("android", "iphone")
    dev = persona.DEVICE[COMBO]
    assert (dev["computer"], dev["phone"]) == ("Windows laptop", "iPhone 15 Pro")
    assert persona.has_iphone(COMBO) and persona.on_windows(COMBO)
    assert not persona.on_mac(COMBO) and persona.browser(COMBO) == "edge"
    assert persona.has_iphone("iphone") and persona.on_mac("iphone")
    assert not persona.has_iphone("android") and persona.on_windows("android")
    with pytest.raises(ValueError, match="unknown combo"):
        persona.has_iphone("blackberry")


def test_notes_carry_the_iphone_shortcut_names():
    jd = next(j for j in persona.JOURNAL if j.offset == -38)
    assert journal.note_filename(ANCHOR, -38, jd.notes[0], COMBO) == \
        journal.note_filename(ANCHOR, -38, jd.notes[0], "iphone") == \
        "2026-09-13 12.41.08 -0500.txt"


def test_the_demo_config_is_onedrive_and_the_owner(tmp_path):
    text = demo_build._config_text(tmp_path, COMBO, ANCHOR)
    data = tomllib.loads(text)
    assert data["paths"]["inbox_dir"] == str(tmp_path / "OneDrive" / "VoiceJournal" / "inbox")
    assert data["owner"]["handles"] == [f"email:{persona.OWNER.email}",
                                        f"phone:{persona.OWNER.phone}"]
    assert data["ask"] == {"enabled": False}
    assert data["connectors"]["calendar"]["mirror_calendars"] == ["Home"]
    assert "Windows laptop + iPhone 15 Pro" in text.splitlines()[0]
    (tmp_path / "config.toml").write_text(text, encoding="utf-8")
    assert load_config(tmp_path).timezone == persona.TIMEZONE


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_no_combo_asks_the_local_model(tmp_path, combo):
    assert tomllib.loads(demo_build._config_text(tmp_path, combo, ANCHOR))["ask"] == \
        {"enabled": False}


# ---------------------------------------------------------------- the iPhone backup

@pytest.fixture
def backup(tmp_path) -> Path:
    return sample.write_sample(tmp_path / "ios_messages", ANCHOR, COMBO)


def test_the_backup_is_laid_out_as_apple_devices_writes_one(backup, tmp_path):
    assert backup.parent == tmp_path / "ios_messages" / "Apple" / "MobileSync" / "Backup"
    assert backup.name == sample.UDID
    info = plistlib.loads((backup / "Info.plist").read_bytes())
    assert info["Device Name"] == "Alex\N{RIGHT SINGLE QUOTATION MARK}s iPhone"
    assert info["Product Name"] == "iPhone 15 Pro" and info["Phone Number"]
    assert plistlib.loads((backup / "Manifest.plist").read_bytes())["IsEncrypted"] is False
    assert (backup / "Status.plist").is_file()
    man = sqlite3.connect(backup / "Manifest.db")
    try:
        rows = {(d, r): (fid, flags) for fid, d, r, flags in man.execute(
            "SELECT fileID, domain, relativePath, flags FROM Files")}
        blob = man.execute("SELECT file FROM Files WHERE relativePath='Library/SMS/sms.db'"
                           ).fetchone()[0]
    finally:
        man.close()
    # the well-known hashed names: SHA1('<domain>-<relativePath>')
    sms = rows[("HomeDomain", "Library/SMS/sms.db")]
    assert sms == ("3d0d7e5fb2ce288813306e4d4636395e047a3d28", 1)
    book = rows[("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")]
    assert book == ("31bb7ba8914766d4ba40d6dfb6113c8b614be442", 1)
    assert rows[("HomeDomain", "Library/SMS")][1] == 2              # a directory row
    assert (backup / sms[0][:2] / sms[0]).is_file()
    media = [k for k in rows if k[0] == "MediaDomain" and rows[k][1] == 1]
    assert len(media) == 1 and media[0][1].endswith("/veracruz_tacos.png")
    assert plistlib.loads(blob)["$archiver"] == "NSKeyedArchiver"
    # no call history: Apple keeps it for encrypted backups
    assert not any("CallHistory" in r for _, r in rows)


def test_the_backup_reads_as_the_phones_texts(backup, cfg):
    scan = im.scan(backup, cfg)
    assert scan["target_kind"] == "backup" and scan["device"] == sample.DEVICE_NAME
    assert scan["backup_date"] == ANCHOR.isoformat()
    assert scan["account"] == persona.OWNER.phone
    assert scan["contacts_read"] is True
    names = {h["value"]: h["display_name"] for h in scan["handles"]}
    for key in ("jake", "maya", "mom", "priya", "sam"):
        p = persona.person(key)
        assert names[p.phone] == p.name                    # named by the phone's own cards
    bodies = {r["body"]: r for r in scan["records"] if r["body"]}
    for t in persona.THREADS:
        for _, _, _, text in t.messages:
            assert text in bodies, text
    versions = json.loads(bodies["Veracruz at 7?"]["value_json"])["versions"]
    assert [v["text"] for v in versions] == ["Veracruz at 6?", "Veracruz at 7?"]
    assert any("Recently Deleted" in s for s in scan["skipped"])
    assert scan["unmapped"] == []
    photo = next(r for r in scan["records"]
                 if "attachments" in json.loads(r["value_json"] or "{}"))
    path = json.loads(photo["value_json"])["attachments"][0]["path"]
    assert path.startswith("Attachments/") and "Library" not in path


def test_the_phone_and_the_mac_hold_the_same_messages(backup, cfg, tmp_path):
    """The sms.db is mac_messages' writer: same GUIDs, same words."""
    phone = im.scan(backup, cfg)
    mac = mac_connector.scan(mac_sample.write_sample(tmp_path / "mac", ANCHOR, "iphone"), cfg)

    def story(scan):
        return sorted((r["external_id"], r["stream"], r["started_at"], r["body"])
                      for r in scan["records"])
    assert story(phone) == story(mac)


def test_the_backup_applies_once_and_doctor_stays_green(backup, cfg, conn):
    scan = im.scan(backup, cfg)
    first = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert first["added"] == len(scan["records"]) > 0
    plan = obs.build_plan(conn, im.scan(backup, cfg))
    assert plan["new"] == [] and plan["updates"] == [] and plan["conflicts"] == []
    assert plan["removed"] == [] and plan["identical"] == len(scan["records"])
    assert run_checks(conn, cfg)["failures"] == 0


def test_the_backup_is_deterministic(tmp_path):
    def tree(root):
        return [(p.relative_to(root).as_posix(), p.read_bytes())
                for p in sorted(root.rglob("*")) if p.is_file()]
    a = sample.write_sample(tmp_path / "a", ANCHOR, COMBO)
    b = sample.write_sample(tmp_path / "b", ANCHOR, COMBO)
    assert tree(a) == tree(b)
    again = sample.write_sample(tmp_path / "a", ANCHOR, COMBO)    # over an old one
    assert tree(again) == tree(b)


@pytest.mark.parametrize("combo", ["android", "iphone"])
def test_the_other_combos_have_no_backup_sample(tmp_path, combo):
    with pytest.raises(ValueError, match="ios_messages has no sample"):
        sample.write_sample(tmp_path, ANCHOR, combo)


def test_calls_are_skipped_and_the_connector_refuses_the_backup(backup, cfg, tmp_path):
    with pytest.raises(ValueError, match="encrypted backups"):
        ios_calls_sample.write_sample(tmp_path, ANCHOR, COMBO)
    with pytest.raises(DomainError, match="no call history"):
        ios_calls_connector.scan(backup, cfg)


def test_notes_and_location_are_skipped_with_their_reason(tmp_path):
    with pytest.raises(ValueError, match="Apple Notes has no export on Windows"):
        notes_sample.write_sample(tmp_path, ANCHOR, COMBO)
    with pytest.raises(ValueError, match="no export"):
        location_sample.write_sample(tmp_path, ANCHOR, COMBO)


# ---------------------------------------------------------------- iCloud and the laptop

def test_contacts_are_an_icloud_vcard_dated_by_each_cards_rev(tmp_path, cfg):
    vcf = contacts_sample.write_sample(tmp_path, ANCHOR, COMBO)
    assert vcf.name == contacts_sample.ICLOUD_VCF
    raw = vcf.read_bytes()
    assert raw.startswith(b"BEGIN:VCARD\r\nVERSION:3.0\r\nPRODID:-//Apple Inc.//iCloud")
    assert raw.count(b"\r\nUID:") == raw.count(b"\r\nREV:") == raw.count(b"BEGIN:VCARD") == 8
    scan = contacts_connector.scan(vcf, cfg)
    assert scan["source_detail"] == "vCard 3.0, contacts"
    cards = {r["title"]: r for r in scan["records"]}
    assert "Alex Rivera" in cards and "Veracruz All Natural" in cards
    # REV is the card's last change — the same day the Mac store says
    assert cards["Jake Morales"]["journal_date"] == persona.day(ANCHOR, -37).isoformat()


def test_photos_are_an_icloud_for_windows_folder_of_iphone_jpegs(tmp_path, cfg, conn):
    root = photos_sample.write_sample(tmp_path, ANCHOR, COMBO)
    assert root == tmp_path / "Pictures" / "iCloud Photos"
    files = sorted((root / "Photos").iterdir())
    assert len(files) == len(persona.PHOTOS)
    assert all(f.name.startswith("IMG_") and f.suffix == ".JPG" for f in files)
    data = files[0].read_bytes()
    assert data[:4] == b"\xff\xd8\xff\xe1" and data[6:14] == b"Exif\x00\x00MM"   # Apple: big-endian
    scan = photos_connector.scan(root, cfg)
    records = scan["records"]
    assert len(records) == len(persona.PHOTOS)
    details = [json.loads(r["value_json"]) for r in records]
    assert {(d["make"], d["model"]) for d in details} == {("Apple", "iPhone 15 Pro")}
    assert {d["time_source"] for d in details} == {"exif"}
    trip = {str(d) for d in persona.bled_trip_days(ANCHOR)}
    abroad = [r for r in records if r["journal_date"] in trip]
    assert len(abroad) == 6 and all(r["utc_offset_sec"] == 7200 for r in abroad)
    first = obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert first["added"] == len(records)
    plan = obs.build_plan(conn, photos_connector.scan(root, cfg))
    assert plan["identical"] == len(records) and plan["new"] == []
    assert run_checks(conn, cfg)["failures"] == 0


def test_the_browser_is_edge_on_windows(tmp_path, cfg):
    db = browser_sample.write_sample(tmp_path, ANCHOR, COMBO)
    assert db.relative_to(tmp_path).as_posix() == \
        "AppData/Local/Microsoft/Edge/User Data/Default/History"
    scan = browser_connector.scan(db, cfg)
    assert {r["origin_app"] for r in scan["records"]} == {"edge"}
    assert any("edge: URL" in s for s in scan["skipped"])
    assert any(json.loads(r["value_json"]).get("synced") for r in scan["records"])
    con = sqlite3.connect(db)
    try:
        urls = {r[0] for r in con.execute("SELECT url FROM urls")}
    finally:
        con.close()
    assert not any(u.startswith("chrome://") for u in urls)
    assert "file:///C:/Users/alex/Downloads/Q4-board-deck-final.pdf" in urls


def test_the_calendar_is_icloud_home_ics(tmp_path, cfg):
    ics = calendar_sample.write_sample(tmp_path / "w", ANCHOR, COMBO)
    mac = calendar_sample.write_sample(tmp_path / "m", ANCHOR, "iphone")
    assert ics.name == mac.name == "Home.ics"
    assert calendar_sample.ICLOUD_PRODID.encode() in ics.read_bytes()
    w, m = calendar_connector.scan(ics, cfg), calendar_connector.scan(mac, cfg)
    assert [r["external_id"] for r in w["records"]] == [r["external_id"] for r in m["records"]]


def test_mail_is_the_gmail_takeout_with_the_reply_from_the_iphone(tmp_path, cfg):
    me = persona.OWNER
    cfg.extra["owner"] = {"name": me.name, "handles": [f"email:{me.email}"]}
    path = email_sample.write_sample(tmp_path / "w", ANCHOR, COMBO)
    assert path.name == f"takeout-{ANCHOR:%Y%m%d}T120000Z-001.zip"
    android = email_connector.scan(email_sample.write_sample(tmp_path / "a", ANCHOR,
                                                             "android"), cfg)
    scan = email_connector.scan(path, cfg)

    def story(s):
        return sorted((r["external_id"], r["journal_date"], r["direction"], r["title"],
                       r["body"]) for r in s["records"])
    assert story(scan) == story(android)                 # the signature is trimmed
    with zipfile.ZipFile(path) as zf:
        text = zf.read(email_sample.TAKEOUT_MBOX).decode("utf-8")
    assert "X-Mailer: iPhone Mail" in text and "Sent from my iPhone" in text


# ---------------------------------------------------------------- the whole build

def _in_process(args):
    r = runner.invoke(app, args)
    if r.exception and not isinstance(r.exception, SystemExit):
        raise r.exception
    return r.exit_code, r.stdout, r.stderr


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """One in-process build, as `lifecrm demo --combo windows-iphone` runs it
    (the vault refreshed once at the end, as the subprocess runner does)."""
    saved = {k: os.environ.get(k) for k in ("LIFECRM_ROOT", NO_REFRESH_ENV)}
    os.environ.pop("LIFECRM_ROOT", None)
    os.environ[NO_REFRESH_ENV] = "1"
    root = tmp_path_factory.mktemp("wi") / "demo"
    try:
        summary = DemoBuilder(root, COMBO, ANCHOR, echo=lambda *_: None, ask=True,
                              runner=_in_process).run()
        yield {"root": root, "summary": summary}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_the_build_imports_this_setups_sources_and_doctor_is_green(built):
    s = built["summary"]
    assert s["combo"] == COMBO and s["doctor"]["failures"] == 0, s["doctor"]
    assert s["journal"]["days"] == len([j for j in persona.JOURNAL if not j.pending])
    imported = set(s["imported"])
    assert {"ios_messages", "apple_health", "contacts", "calendar", "photos", "browser",
            "whatsapp", "finance", "email"} <= imported
    assert not imported & {"android_sms", "mac_messages", "ios_calls", "notes",
                           "location", "health_connect"}
    skipped = " | ".join(s["skipped"])
    assert "ios_calls: not on this combo (an unencrypted iPhone backup" in skipped
    assert "notes: not on this combo (Apple Notes has no export on Windows" in skipped
    assert (built["root"] / "OneDrive" / "VoiceJournal" / "inbox-text").is_dir()
    assert len(s["status"]["pending_text"]) == 1         # today's note waits for /log


def test_every_question_is_answered_with_citations(built):
    root = str(built["root"])
    for q in persona.QUESTIONS:
        code, out, _ = _in_process(["--root", root, "ask", q, "--json", "--no-llm",
                                    "--no-log"])
        assert code == 0, q
        res = json.loads(out)
        c = res["citations"]
        assert c["cited"] and c["unknown"] == [] and c["unsupported"] == [], (q, c)
        assert res["answer"] and not res["answer"].lower().startswith("no data"), q


def test_mom_was_last_heard_from_by_mail_since_there_are_no_calls(built):
    code, out, _ = _in_process(["--root", str(built["root"]), "ask",
                                "when did I last talk to Mom", "--json", "--no-llm",
                                "--no-log"])
    res = json.loads(out)
    assert code == 0 and not any(o["stream"].endswith("_call")
                                 for o in res["evidence"]["observations"])


# ---------------------------------------------------------------- the command and the scripts

def test_both_still_means_android_and_iphone_and_all_adds_the_third(monkeypatch, tmp_path):
    built_combos = []

    class Builder:
        def __init__(self, root, combo, *a, **kw):
            built_combos.append(combo)

        def run(self):
            return {"root": "x", "combo": built_combos[-1]}
    monkeypatch.setattr(demo_build, "DemoBuilder", Builder)
    for arg, want in (("both", ["android", "iphone"]),
                      ("all", ["android", "iphone", "windows-iphone"]),
                      ("windows-iphone", ["windows-iphone"])):
        built_combos.clear()
        r = runner.invoke(app, ["demo", "--combo", arg, "--no-ask", "--json"])
        assert r.exit_code == 0, r.output
        assert built_combos == want
    r = runner.invoke(app, ["demo", "--combo", "windows"])
    assert r.exit_code == 1 and "windows-iphone" in r.output
    r = runner.invoke(app, ["demo", "--combo", "all", "--dir", str(tmp_path / "d")])
    assert r.exit_code == 1 and "--dir builds one demo" in r.output


@needs_powershell
def test_demo_ps1_takes_the_windows_iphone_combo(tmp_path):
    repo = _ps_fake_repo(tmp_path)
    record = tmp_path / "calls.jsonl"
    done = _demo_ps(repo, tmp_path, record, "-Combo", "windows-iphone", "-NoServe")
    assert done.returncode == 0, done.stdout + done.stderr
    (call,) = _records(record)
    assert call["argv"] == ["demo", "--combo", "windows-iphone"]


@needs_bash
def test_demo_sh_takes_the_windows_iphone_combo_and_refuses_others(tmp_path):
    repo = _fake_repo(tmp_path, "demo.sh")
    record = tmp_path / "calls.jsonl"
    done = _demo_sh(repo, tmp_path, record, "--combo", "windows-iphone", "--no-serve")
    assert done.returncode == 0, done.stdout + done.stderr
    (call,) = _records(record)
    assert call["argv"] == ["demo", "--combo", "windows-iphone"]
    bad = _demo_sh(repo, tmp_path, record, "--combo", "both", "--no-serve")
    assert bad.returncode == 2 and "--combo must be" in bad.stderr

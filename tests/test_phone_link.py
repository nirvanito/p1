"""Phone Link connector (lifecrm/connectors/phone_link.py) against a synthetic
Phone Link cache — the three SQLite databases with the tables and columns a
real cache has (2026-10), FILETIME timestamps, comma-separated address lists,
the owner's number in the subscription row.

The point to prove: the same texts and calls as a SMS Backup & Restore
backup import — same streams, same ids for SMS and calls — with nothing set
up on the phone; texts remembered, contact cards mirrored; the cache found
by itself, held once by collect, and the right folder picked when a
replaced phone's cache is still around.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import cli_project
from test_android_sms import DANA, JAKE, ME, SAM, call, document, ms, sms
from test_collect import home  # noqa: F401  (autouse: a home of its own)
from test_health_connect import make_hc_db
from test_phone import linked  # noqa: F401
from typer.testing import CliRunner

from lifecrm import collect, platforms
from lifecrm.cli import app
from lifecrm.connectors import android_sms as an
from lifecrm.connectors import base
from lifecrm.connectors import phone_link as pl
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()
DEVICE = "d4d763e2-dde0-42da-a226-be98c65aede2"
OLD_DEVICE = "7a6c98e6-370b-4f4a-ac87-ae51d0cd9e0e"
MOM_EMAIL = "mom@example.com"


def ft(iso: str, seconds: bool = False) -> int:
    """A Phone Link FILETIME: 100 ns since 1601, at the millisecond (SMS,
    calls, contacts) or the second (MMS) the phone stored."""
    m = ms(iso)
    if seconds:
        m = m // 1000 * 1000
    return m * 10000 + 116444736000000000


_PHONE_SQL = """
CREATE TABLE content_sequence (content_sequence_id INTEGER PRIMARY KEY, content_type TEXT, sequence INTEGER, expected INTEGER);
CREATE TABLE conversation (thread_id INTEGER PRIMARY KEY, recipient_list TEXT, timestamp INTEGER, msg_count INTEGER, unread_count INTEGER, has_rcs INTEGER, checksum INTEGER, phone_unread_count INTEGER);
CREATE TABLE message (message_id INTEGER PRIMARY KEY, thread_id INTEGER, status INTEGER, from_address TEXT, type INTEGER, subject TEXT, body TEXT, timestamp INTEGER, pc_status INTEGER, subscription_id INTEGER, checksum INTEGER, group_id INTEGER, to_address TEXT);
CREATE TABLE mms (message_id INTEGER PRIMARY KEY, thread_id INTEGER, status INTEGER, type INTEGER, subscription_id INTEGER, subject TEXT, charset INTEGER, timestamp INTEGER, pc_status INTEGER, from_address TEXT, checksum INTEGER, to_addresses TEXT);
CREATE TABLE mms_part (part_id INTEGER PRIMARY KEY, message_id INTEGER, sequence_num INTEGER, content_id TEXT, content_type TEXT, text TEXT, name TEXT, charset INTEGER, blob BLOB, size INTEGER);
CREATE TABLE participant (id INTEGER PRIMARY KEY, thread_id INTEGER, send_address TEXT, normalized_address TEXT);
CREATE TABLE rcs_chat (message_id INTEGER PRIMARY KEY, thread_id INTEGER, content_type TEXT, status INTEGER, from_address TEXT, type INTEGER, body TEXT, timestamp INTEGER, pc_status INTEGER, subscription_id INTEGER, message_type INTEGER, session_id TEXT, is_user_action_required INTEGER, checksum INTEGER);
CREATE TABLE rcs_conversation (thread_id INTEGER PRIMARY KEY, fallback_id INTEGER, recipient_list TEXT, timestamp INTEGER, msg_count INTEGER, unread_count INTEGER, type INTEGER, checksum INTEGER, phone_unread_count INTEGER);
CREATE TABLE subscription (subscription_id INTEGER PRIMARY KEY, sim_slot_index INTEGER, country_iso TEXT, name TEXT, is_roaming INTEGER, number TEXT, is_mms_enabled INTEGER, checksum INTEGER);
"""
_CALLING_SQL = """
CREATE TABLE call_history (call_id INTEGER PRIMARY KEY, phone_number TEXT, duration INTEGER, call_type INTEGER, start_time INTEGER, is_read INTEGER, phone_account_id TEXT, last_updated_time INTEGER);
"""
_CONTACTS_SQL = """
CREATE TABLE contact (contact_id INTEGER PRIMARY KEY, display_name TEXT, nickname TEXT, last_updated_time INTEGER, thumbnail BLOB, company TEXT, job_title TEXT, notes TEXT, name_prefix TEXT, name_suffix TEXT, given_name TEXT, middle_name TEXT, family_name TEXT, checksum INTEGER);
CREATE TABLE phonenumber (phone_number_id INTEGER PRIMARY KEY, contact_id INTEGER, phone_number TEXT, display_phone_number TEXT, phone_number_type INTEGER, label TEXT, checksum INTEGER);
CREATE TABLE emailaddress (email_address_id INTEGER PRIMARY KEY, contact_id INTEGER, type INTEGER, label TEXT, address TEXT, checksum INTEGER);
CREATE TABLE postaladdress (postal_address_id INTEGER PRIMARY KEY, contact_id INTEGER, type INTEGER, label TEXT, street TEXT, city TEXT, region TEXT, postal_code TEXT, country_code TEXT, sub_administrative_area TEXT, sub_locality TEXT, display_address TEXT, checksum INTEGER);
CREATE TABLE contactdate (contact_date_id INTEGER PRIMARY KEY, contact_id INTEGER, date_type INTEGER, label TEXT, display_date TEXT, checksum INTEGER);
CREATE TABLE contacturl (contact_url_id INTEGER PRIMARY KEY, contact_id INTEGER, type INTEGER, label TEXT, url_address TEXT, checksum INTEGER);
"""

# The phone's month, as Phone Link caches it. Threads: 1 = Jake, 2 = Dana,
# 3 = the Saturday group (Jake, Sam, Dana), 4 = Sam, 5 = AMAZON, 6 = Mom.
THREADS = {1: [JAKE], 2: ["(512) 555-9876"], 3: [JAKE, SAM, DANA], 4: [SAM],
           5: ["AMAZON"], 6: ["+12105550110"]}
SMS = [  # (id, thread, type, from, to, body, when)
    (1, 1, 1, JAKE, "", "see you at 6", "2026-09-01T18:00:00.250Z"),
    (2, 1, 2, "", JAKE, "perfect, bringing snacks", "2026-09-01T18:05:00.999Z"),
    (3, 2, 1, "5125559876", "", "running late", "2026-09-02T12:00:00Z"),
    (4, 5, 1, "AMAZON", "", "Your package was delivered", "2026-09-02T15:00:00Z"),
    (5, 1, 3, "", JAKE, "unsent thought", "2026-09-02T16:00:00Z"),            # draft
    (6, 1, 1, JAKE, "", "see you there 😀", "2026-09-03T06:30:00.987Z"),
]
MMS = [  # (id, thread, type, from, to_addresses, when, texts, media)
    (11, 3, 1, JAKE, f"{ME},{SAM},{DANA}", "2026-09-02T09:00:00Z", ["who's in for saturday"], []),
    (12, 3, 2, ME, f"{JAKE},{SAM},{DANA}", "2026-09-02T09:05:00Z", ["me!"],
     [("IMG_0001.jpg", "image/jpeg")]),
    (13, 4, 1, SAM, ME, "2026-09-02T10:00:00Z", [], [("climb.jpg", "image/jpeg")]),
    (14, 4, 1, SAM, ME, "2026-09-02T11:00:00Z", [], []),                          # nothing in it
]
CALLS = [  # (id, number, duration, type, when)
    (1, JAKE, 120, 2, "2026-09-01T19:00:00Z"),
    (2, "5125559876", 0, 3, "2026-09-02T12:30:00Z"),
    (3, "", 0, 1, "2026-09-02T13:00:00Z"),                                      # withheld
    (4, SAM, 30, 6, "2026-09-02T14:00:00Z"),                                    # blocked
]
CONTACTS = [  # (id, display, given, family, company, numbers [(number, type, label)], emails, when)
    (1, "Jake Reilly", "Jake", "Reilly", None, [(JAKE, 2, None)], [], "2026-08-01T12:00:00Z"),
    (2, "Dana Whitfield", "Dana", "Whitfield", None, [("+15125559876", 2, None)], [],
     "2026-08-02T12:00:00Z"),
    (3, "Sam Ortiz", "Sam", "Ortiz", "Austin Bouldering Project", [(SAM, 2, None)], [],
     "2026-08-03T12:00:00Z"),
    (4, "Mom", None, None, None, [("+12105550110", 1, None), ("*123#", 8, "Voicemail")],
     [(MOM_EMAIL, 1)], "2026-08-04T12:00:00Z"),
]


def make_cache(root: Path, device: str = DEVICE, *, owner: str | None = ME,
               texts=SMS, mmss=MMS, calls=CALLS, contacts=CONTACTS,
               rcs=(), written: float | None = None, with_calls_db: bool = True) -> Path:
    """A Phone Link cache folder under `root` (…\\Indexed) for one phone."""
    folder = root / device
    db = folder / "System" / "Database"
    db.mkdir(parents=True, exist_ok=True)
    for name in ("phone.db", "calling.db", "contacts.db"):
        for suffix in ("", "-wal", "-shm"):
            (db / (name + suffix)).unlink(missing_ok=True)

    conn = sqlite3.connect(db / "phone.db")
    conn.executescript(_PHONE_SQL)
    if owner:
        digits = owner.lstrip("+")
        conn.execute("INSERT INTO subscription (subscription_id, sim_slot_index, country_iso, "
                     "name, is_roaming, number, is_mms_enabled) VALUES (2, 0, 'us', "
                     "'Carrier', 0, ?, 1)", (digits,))
    pid = 0
    for tid, members in THREADS.items():
        conn.execute("INSERT INTO conversation (thread_id, recipient_list, timestamp, "
                     "msg_count, unread_count, has_rcs) VALUES (?, ?, 0, 0, 0, 0)",
                     (tid, ",".join(members)))
        for m in members:
            pid += 1
            h = base.handle_for(m)
            conn.execute("INSERT INTO participant (id, thread_id, send_address, "
                         "normalized_address) VALUES (?, ?, ?, ?)",
                         (pid, tid, m, h[1] if h and h[0] == "phone" else m))
    for mid, tid, kind, frm, to, body, when in texts:
        conn.execute("INSERT INTO message (message_id, thread_id, status, from_address, type, "
                     "subject, body, timestamp, pc_status, subscription_id, group_id, "
                     "to_address) VALUES (?, ?, 2, ?, ?, '', ?, ?, 0, 2, 0, ?)",
                     (mid, tid, frm, kind, body, ft(when), to))
    part_id = 0
    for mid, tid, kind, frm, tos, when, texts_, media in mmss:
        conn.execute("INSERT INTO mms (message_id, thread_id, status, type, subscription_id, "
                     "subject, charset, timestamp, pc_status, from_address, to_addresses) "
                     "VALUES (?, ?, 2, ?, 2, '', 0, ?, 0, ?, ?)",
                     (mid, tid, kind, ft(when, seconds=True), frm, tos))
        part_id += 1
        conn.execute("INSERT INTO mms_part (part_id, message_id, sequence_num, content_id, "
                     "content_type, text, name, size) VALUES (?, ?, -1, '<smil>', "
                     "'application/smil', '<smil></smil>', 'smil.xml', 0)", (part_id, mid))
        for t in texts_:
            part_id += 1
            conn.execute("INSERT INTO mms_part (part_id, message_id, sequence_num, content_id, "
                         "content_type, text, name, size) VALUES (?, ?, 0, '<text_0>', "
                         "'text/plain', ?, 'text_0.txt', ?)", (part_id, mid, t, len(t)))
        for nm, mime in media:
            part_id += 1
            conn.execute("INSERT INTO mms_part (part_id, message_id, sequence_num, content_id, "
                         "content_type, text, name, blob, size) VALUES (?, ?, 0, ?, ?, NULL, "
                         "?, ?, 24)", (part_id, mid, f"<{nm}>", mime, nm,
                                       b"\xff\xd8MEDIA-BYTES-NEVER-STORED\xff\xd9"))
    rid = 100
    for tid, members, kind, frm, body, when in rcs:
        rid += 1
        conn.execute("INSERT OR IGNORE INTO rcs_conversation (thread_id, fallback_id, "
                     "recipient_list, timestamp, msg_count, unread_count, type) VALUES "
                     "(?, 0, ?, 0, 0, 0, 0)", (tid, ",".join(members)))
        conn.execute("INSERT INTO rcs_chat (message_id, thread_id, content_type, status, "
                     "from_address, type, body, timestamp, pc_status, subscription_id, "
                     "message_type, session_id, is_user_action_required) VALUES (?, ?, "
                     "'text/plain', 2, ?, ?, ?, ?, 0, 2, 0, 's', 0)",
                     (rid, tid, frm, kind, body, ft(when)))
    conn.commit()
    conn.close()

    if with_calls_db:
        conn = sqlite3.connect(db / "calling.db")
        conn.executescript(_CALLING_SQL)
        for cid, number, duration, kind, when in calls:
            conn.execute("INSERT INTO call_history (call_id, phone_number, duration, call_type, "
                         "start_time, is_read, phone_account_id, last_updated_time) VALUES "
                         "(?, ?, ?, ?, ?, 1, 'acct', ?)",
                         (cid, number, duration, kind, ft(when), ft(when)))
        conn.commit()
        conn.close()

    conn = sqlite3.connect(db / "contacts.db")
    conn.executescript(_CONTACTS_SQL)
    for cid, display, given, family, company, numbers, emails, when in contacts:
        conn.execute("INSERT INTO contact (contact_id, display_name, nickname, last_updated_time, "
                     "company, job_title, notes, given_name, family_name) VALUES "
                     "(?, ?, NULL, ?, ?, NULL, NULL, ?, ?)",
                     (cid, display, ft(when), company, given, family))
        for number, kind, label in numbers:
            conn.execute("INSERT INTO phonenumber (contact_id, phone_number, display_phone_number, "
                         "phone_number_type, label) VALUES (?, ?, ?, ?, ?)",
                         (cid, number, number, kind, label))
        for address, kind in emails:
            conn.execute("INSERT INTO emailaddress (contact_id, type, label, address) VALUES "
                         "(?, ?, NULL, ?)", (cid, kind, address))
    conn.execute("INSERT INTO contactdate (contact_id, date_type, label, display_date) "
                 "VALUES (1, 3, NULL, '1990-05-14')")
    conn.execute("INSERT INTO postaladdress (contact_id, type, label, street, city, region, "
                 "postal_code, country_code) VALUES (3, 2, NULL, '979 Springdale Rd', "
                 "'Austin', 'TX', '78702', 'US')")
    conn.commit()
    conn.close()
    if written is not None:
        for p in db.iterdir():
            os.utime(p, (written, written))
    return folder


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(platforms, "is_windows", lambda: True)
    monkeypatch.setattr(platforms, "is_mac", lambda: False)


@pytest.fixture
def cache(home, windows):  # noqa: F811
    """One linked phone's cache where Phone Link keeps it, under this test's home."""
    return make_cache(platforms.phone_link_root())


def _by_stream(scan) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in scan["records"]:
        out.setdefault(r["stream"], []).append(r)
    return out


def _apply(conn, scan, **kw):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan, **kw)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- reading

def test_reads_texts_calls_and_contacts_the_way_the_backup_import_does(cfg, cache):
    scan = pl.scan(cache, cfg)
    assert scan["connector"] == "phone_link" and scan["account"] == ME
    assert scan["owner"] == ME and scan["facts"]["account_source"] == "Phone Link's subscription row"
    assert scan["source_file"] == f"{DEVICE[:8]}/phone.db + calling.db + contacts.db"
    by = _by_stream(scan)
    assert {k: len(v) for k, v in by.items()} == {
        "android_sms": 5, "android_mms": 3, "android_call": 3, "contact_card": 4}

    first = by["android_sms"][0]
    assert first["direction"] == "in" and first["body"] == "see you at 6"
    assert first["started_at"] == "2026-09-01T18:00:00Z"
    assert first["journal_date"] == "2026-09-01" and first["origin_app"] == "messages"
    assert json.loads(first["value_json"]) == {"thread": f"phone:{JAKE}"}
    assert {(p["kind"], p["value"], p["role"]) for p in first["participants"]} == {
        ("phone", JAKE, "sender"), ("phone", ME, "recipient")}
    sent = by["android_sms"][1]
    assert sent["direction"] == "out" and {p["value"] for p in sent["participants"]} == {JAKE, ME}
    dana = by["android_sms"][2]
    assert dana["participants"][0]["value"] == "+15125559876"      # normalized from the cache
    amazon = by["android_sms"][3]
    assert amazon["participants"][0] == {"kind": "app_account", "value": "messages:AMAZON",
                                         "role": "sender"}
    assert by["android_sms"][4]["body"] == "see you there 😀"

    group, mine, photo = by["android_mms"]
    g = json.loads(group["value_json"])
    assert g["group"] is True and g["thread"] == ",".join(sorted(f"phone:{n}" for n in (JAKE, SAM, DANA)))
    assert group["body"] == "who's in for saturday" and group["direction"] == "in"
    assert {(p["value"], p["role"]) for p in group["participants"]} == {
        (JAKE, "sender"), (ME, "recipient"), (SAM, "recipient"), (DANA, "recipient")}
    m = json.loads(mine["value_json"])
    assert mine["direction"] == "out" and m["attachments"] == [{"name": "IMG_0001.jpg",
                                                                 "mime": "image/jpeg"}]
    assert {(p["value"], p["role"]) for p in mine["participants"]} == {
        (ME, "sender"), (JAKE, "recipient"), (SAM, "recipient"), (DANA, "recipient")}
    assert photo["body"] is None and json.loads(photo["value_json"])["attachments"][0]["name"] == "climb.jpg"
    assert mine["started_at"] == "2026-09-02T09:05:00Z"           # MMS kept to the second

    jake_call, missed, withheld = by["android_call"]
    assert jake_call["direction"] == "out" and jake_call["value"] == 120 and jake_call["unit"] == "s"
    assert jake_call["ended_at"] == "2026-09-01T19:02:00Z"
    assert json.loads(missed["value_json"]) == {"call": "missed"}
    assert missed["participants"][0]["value"] == "+15125559876"
    assert json.loads(withheld["value_json"]) == {"call": "in", "number": "unknown"}
    assert [p["value"] for p in withheld["participants"]] == [ME]   # no one else to name

    cards = {c["title"]: c for c in by["contact_card"]}
    assert set(cards) == {"Jake Reilly", "Dana Whitfield", "Sam Ortiz", "Mom"}
    jake = json.loads(cards["Jake Reilly"]["value_json"])
    assert jake["phones"] == [{"label": "mobile", "number": JAKE, "handle": JAKE}]
    assert jake["birthday"] == "1990-05-14"
    assert cards["Jake Reilly"]["external_id"] == f"phonelink:{DEVICE[:8]}:1"
    assert cards["Jake Reilly"]["started_at"] == "2026-08-01T12:00:00Z"
    sam = json.loads(cards["Sam Ortiz"]["value_json"])
    assert sam["org"] == "Austin Bouldering Project"
    assert sam["addresses"] == [{"label": "work", "street": "979 Springdale Rd", "city": "Austin",
                                 "state": "TX", "zip": "78702", "country": "US"}]
    mom = json.loads(cards["Mom"]["value_json"])
    assert mom["emails"] == [{"label": "home", "address": MOM_EMAIL}]
    assert [p["label"] for p in mom["phones"]] == ["home", "Voicemail"]
    assert {(p["kind"], p["value"]) for p in cards["Mom"]["participants"]} == {
        ("phone", "+12105550110"), ("email", MOM_EMAIL)}

    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", JAKE)] == "Jake Reilly" and names[("phone", SAM)] == "Sam Ortiz"
    assert names[("phone", ME)] == pl.OWNER_LABEL
    assert names[("app_account", "messages:AMAZON")] is None
    assert names[("email", MOM_EMAIL)] == "Mom"
    assert scan["named_handles"] == 5                     # Jake, Dana, Sam, Mom's two
    assert scan["mirror_streams"] == ["contact_card"]
    assert scan["present_ids"] == {c["external_id"] for c in by["contact_card"]}
    assert scan["coverage"] == []                          # a window declares no day complete
    assert sorted(scan["skipped"]) == ["1 × MMS with no text or attachment",
                                       "1 × blocked call (not kept)",
                                       "1 × draft (never delivered)"]
    assert scan["unmapped"] == [{"what": "phone value that is not a number (kept on the "
                                         "card, no handle)", "count": 1}]
    assert scan["facts"]["read"] == {"sms": 6, "mms": 4, "rcs": 0, "calls": 4, "cards": 4}


def test_sms_and_call_ids_are_the_backup_imports_ids(cfg, cache, tmp_path):
    """The very messages, written as SMS Backup & Restore writes them."""
    backup = tmp_path / "backup"
    backup.mkdir()
    rows = [sms(JAKE, "2026-09-01T18:00:00.250Z", 1, "see you at 6", "Jake Reilly"),
            sms("(512) 555-0142", "2026-09-01T18:05:00.999Z", 2, "perfect, bringing snacks",
                "Jake Reilly"),
            sms("5125559876", "2026-09-02T12:00:00Z", 1, "running late", "Dana Whitfield"),
            sms("AMAZON", "2026-09-02T15:00:00Z", 1, "Your package was delivered")]
    (backup / "sms-20260920020003.xml").write_text(document("sms", rows), encoding="utf-8")
    (backup / "calls-20260920020003.xml").write_text(
        document("calls", [call(JAKE, "2026-09-01T19:00:00Z", 2, 120, "Jake Reilly"),
                           call("5125559876", "2026-09-02T12:30:00Z", 3, 0, "Dana Whitfield")]),
        encoding="utf-8")
    from_backup = _by_stream(an.scan(backup, cfg))
    from_cache = _by_stream(pl.scan(cache, cfg))
    for stream in ("android_sms", "android_call"):
        want = {r["external_id"]: r for r in from_backup[stream]}
        got = {r["external_id"]: r for r in from_cache[stream]}
        assert set(want) <= set(got), stream
        for ext, rec in want.items():
            other = got[ext]
            for key in ("direction", "body", "started_at", "journal_date", "value", "value_json"):
                assert other[key] == rec[key], (stream, key)
            theirs = [p for p in other["participants"] if p["value"] != ME]
            assert sorted(map(json.dumps, theirs)) == sorted(map(json.dumps, rec["participants"]))


def test_the_two_routes_dedupe_in_the_database_except_mms(conn, cfg, cache, tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "sms-20260920020003.xml").write_text(document("sms", [
        sms(JAKE, "2026-09-01T18:00:00.250Z", 1, "see you at 6", "Jake Reilly")]),
        encoding="utf-8")
    (backup / "calls-20260920020003.xml").write_text(document("calls", [
        call(JAKE, "2026-09-01T19:00:00Z", 2, 120, "Jake Reilly")]), encoding="utf-8")
    _apply(conn, an.scan(backup, cfg))
    plan = obs.build_plan(conn, pl.scan(cache, cfg))
    new = {r["stream"]: 0 for r in plan["new"]}
    for r in plan["new"]:
        new[r["stream"]] += 1
    assert plan["identical"] == 2                    # Jake's text and the call, already stored
    assert new == {"android_sms": 4, "android_mms": 3, "android_call": 2, "contact_card": 4}
    assert not plan["conflicts"]


def test_texts_are_remembered_and_contact_cards_mirror(conn, cfg, cache):
    _apply(conn, pl.scan(cache, cfg))
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE status='active'"
                        ).fetchone()[0] == 15
    # A month later: the oldest text aged out of the cache, Dana was deleted
    # from the phone's contacts, Sam's call log entry is gone.
    make_cache(platforms.phone_link_root(), texts=SMS[1:], calls=CALLS[1:],
               contacts=[c for c in CONTACTS if c[1] != "Dana Whitfield"])
    plan, result = _apply(conn, pl.scan(cache, cfg))
    removed = plan.get("removed") or []
    assert [r["stream"] for r in removed] == ["contact_card"]
    assert removed[0]["title"] == "Dana Whitfield"
    rows = conn.execute("SELECT stream, status, COUNT(*) n FROM observations GROUP BY 1, 2"
                        ).fetchall()
    counts = {(r["stream"], r["status"]): r["n"] for r in rows}
    assert counts[("android_sms", "active")] == 5         # the aged-out text stays
    assert counts[("android_call", "active")] == 3
    assert counts[("contact_card", "active")] == 3
    assert sum(n for (s, _), n in counts.items() if s == "contact_card") == 3
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE stream='contact_card' AND "
                        "title='Dana Whitfield'").fetchone()[0] == 0


def test_the_cache_matching_the_database_is_nothing_new(conn, cfg, cache):
    scan = pl.scan(cache, cfg)
    _apply(conn, scan)
    again = pl.scan(cache, cfg)
    assert again["source_hash"] == scan["source_hash"]
    plan = obs.build_plan(conn, again)
    assert not plan["new"] and not plan["updates"] and plan["identical"] == 15
    lines = pl.report_lines(again, plan)
    assert any("nothing new — the cache matches the database" in ln for ln in lines)


# ---------------------------------------------------------------- whose phone, names only, rcs

def test_the_owner_comes_from_the_subscription_row_then_sent_mms_then_config(cfg, home, windows):  # noqa: F811
    root = platforms.phone_link_root()
    scan = pl.scan(make_cache(root, owner=None), cfg)
    assert scan["owner"] == ME and scan["facts"]["account_source"] == \
        "the sender of this phone's sent MMS"
    scan = pl.scan(make_cache(root, owner=None, mmss=[m for m in MMS if m[2] == 1]), cfg)
    assert scan["owner"] == ME and scan["facts"]["account_source"] == \
        "the one number every received MMS went to"
    scan = pl.scan(make_cache(root, owner=None, mmss=[]), cfg)
    assert scan["owner"] is None and scan["account"] == f"phonelink:{DEVICE[:8]}"
    assert all(p["value"] != ME for r in scan["records"] for p in r["participants"])
    assert any("account: unknown" in ln for ln in pl.report_lines(scan, _empty_plan()))
    cfg.connectors = {"phone_link": {"account": "(512) 555-0100"}}
    scan = pl.scan(make_cache(root, owner=None, mmss=[]), cfg)
    assert scan["owner"] == ME and scan["facts"]["account_source"] == "config.toml"
    cfg.connectors = {"phone_link": {"account": "jake@example.com"}}
    with pytest.raises(DomainError, match=r"\[connectors.phone_link\] account .* is not a phone"):
        pl.scan(make_cache(root), cfg)


def _empty_plan() -> dict:
    return {"new": [], "updates": [], "identical": 0, "refreshed": [], "dup_incoming": 0,
            "conflicts": [], "invalid": [], "removed": []}


def test_contacts_can_stay_names_only(cfg, cache):
    cfg.connectors = {"phone_link": {"contacts": False}}
    scan = pl.scan(cache, cfg)
    assert "contact_card" not in _by_stream(scan)
    assert "mirror_streams" not in scan and "present_ids" not in scan
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", JAKE)] == "Jake Reilly"             # the names still label handles
    assert ("email", MOM_EMAIL) not in names                    # no card, so no card handles
    assert any("names only" in ln for ln in pl.report_lines(scan, _empty_plan()))
    cfg.connectors = {"phone_link": {"contacts": "no"}}
    with pytest.raises(DomainError, match="contacts must be true or false"):
        pl.scan(cache, cfg)


def test_rcs_rows_read_as_mms_marked_rcs(cfg, home, windows):  # noqa: F811
    folder = make_cache(platforms.phone_link_root(), rcs=[
        (7, [SAM], 1, SAM, "chat features say hi", "2026-09-04T10:00:00Z"),
        (7, [SAM], 2, "", "hi back", "2026-09-04T10:01:00Z")])
    scan = pl.scan(folder, cfg)
    rcs = [r for r in _by_stream(scan)["android_mms"] if '"rcs"' in r["value_json"]]
    assert [(r["direction"], r["body"]) for r in rcs] == [("in", "chat features say hi"),
                                                          ("out", "hi back")]
    assert {p["value"] for p in rcs[0]["participants"]} == {SAM, ME}
    assert rcs[0]["participants"][0] == {"kind": "phone", "value": SAM, "role": "sender"}
    assert scan["facts"]["read"]["rcs"] == 2
    assert any("rcs_chat table, whose format is unverified" in ln
               for ln in pl.report_lines(scan, _empty_plan()))


def test_no_calls_database_is_said_not_failed(cfg, home, windows):  # noqa: F811
    folder = make_cache(platforms.phone_link_root(), with_calls_db=False)
    scan = pl.scan(folder, cfg)
    assert "android_call" not in _by_stream(scan)
    assert scan["source_file"] == f"{DEVICE[:8]}/phone.db + contacts.db"
    assert any("no calling.db in the cache" in ln for ln in pl.report_lines(scan, _empty_plan()))
    folder = make_cache(platforms.phone_link_root(), calls=[])
    lines = pl.report_lines(pl.scan(folder, cfg), _empty_plan())
    assert any("calls: none in the cache" in ln and "Bluetooth" in ln for ln in lines)


def test_a_changed_format_is_one_line(cfg, cache):
    db = cache / "System" / "Database" / "phone.db"
    c = sqlite3.connect(db)
    c.execute("ALTER TABLE message DROP COLUMN timestamp")
    c.commit()
    c.close()
    with pytest.raises(DomainError, match=r"phone.db: message has no column\(s\) timestamp"):
        pl.scan(cache, cfg)
    make_cache(platforms.phone_link_root())
    c = sqlite3.connect(db)
    c.execute("DROP TABLE participant")
    c.commit()
    c.close()
    with pytest.raises(DomainError, match="has no participant table"):
        pl.scan(cache, cfg)
    with pytest.raises(DomainError, match="holds no Phone Link phone.db"):
        pl.scan(cache.parent, cfg)
    (cache / "System" / "Database" / "other.db").write_bytes(b"x")
    with pytest.raises(DomainError, match="is not Phone Link's phone.db"):
        pl.scan(cache / "System" / "Database" / "other.db", cfg)


# ---------------------------------------------------------------- finding the cache

def test_default_target_picks_the_phone_that_is_still_linked(cfg, home, windows):  # noqa: F811
    root = platforms.phone_link_root()
    with pytest.raises(DomainError, match="no Phone Link cache under"):
        pl.default_target(cfg)
    live = make_cache(root)
    assert pl.default_target(cfg) == live
    # A replaced phone's folder stays behind, months old: the live one wins.
    old = make_cache(root, OLD_DEVICE, written=time.time() - 120 * 86400)
    assert pl.default_target(cfg) == live
    assert [f.name for f, _ in pl.device_folders(root)] == [DEVICE, OLD_DEVICE]
    # Two phones both written this week: a question, never a guess.
    make_cache(root, OLD_DEVICE)
    with pytest.raises(base.Ambiguous, match="2 phones' caches were written in the last 14 days"):
        pl.default_target(cfg)
    cfg.connectors = {"phone_link": {"path": str(old)}}
    assert pl.default_target(cfg) == old
    cfg.connectors = {"phone_link": {"path": str(root / "gone")}}
    with pytest.raises(DomainError, match="does not exist"):
        pl.default_target(cfg)
    # Every folder gone quiet: the newest, since the phone may just be away.
    cfg.connectors = {}
    make_cache(root, written=time.time() - 40 * 86400)
    make_cache(root, OLD_DEVICE, written=time.time() - 50 * 86400)
    assert pl.default_target(cfg) == live
    # A phone.db alone, or a System\Database folder, read the same way.
    assert pl.scan(live / "System" / "Database", cfg)["facts"]["device"] == DEVICE
    assert pl.scan(live / "System" / "Database" / "phone.db", cfg)["facts"]["device"] == DEVICE


def test_a_mac_has_no_cache_and_says_so(cfg, home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(platforms, "is_windows", lambda: False)
    make_cache(platforms.phone_link_root())
    with pytest.raises(DomainError, match="Phone Link is a Windows app"):
        pl.default_target(cfg)


def test_local_appdata_follows_the_home_in_use(home, monkeypatch):  # noqa: F811
    assert platforms.local_appdata() == home                       # the fixture's LOCALAPPDATA
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone-else\AppData\Local")
    assert platforms.local_appdata() == home / "AppData" / "Local"   # not under this home: ignored
    monkeypatch.delenv("LOCALAPPDATA")
    assert platforms.phone_link_root() == (home / "AppData" / "Local" / "Packages"
                                           / platforms.PHONE_LINK_PACKAGE / "LocalCache"
                                           / "Indexed")


# ---------------------------------------------------------------- collect

def test_collect_finds_the_cache_with_no_config_and_holds_the_first_import(conn, cfg, cache):
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    held = by["phone_link"]
    assert held["outcome"] == "held" and held["hold"] == "first_import"
    assert "texts, calls and contact names (Phone Link)" in held["reason"]
    assert "docs/phone_link.md" in held["reason"]
    items = {it["source"]: it for it in collect.setup_checklist(conn, cfg, "android")["items"]}
    assert items["phone_link"]["state"] == "waiting" and not items["phone_link"]["steps"]
    assert items["android_sms"]["state"] == "to do"
    assert any("Link to Windows" in s for s in
               [s for it in collect.setup_checklist(conn, cfg, "android")["items"]
                if it["source"] == "phone_link" for s in it["steps"]] or
               [s for name, _, steps in collect.PHONE["android"][2] if name == "phone_link"
                for s in steps])

    preview = collect.approve(conn, cfg, "phone_link")
    assert preview["doc"] == "docs/phone_link.md"
    assert any(ln.startswith("phone-link:") for ln in preview["report"])
    collect.approve(conn, cfg, "phone_link", apply=True)
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE status='active'"
                        ).fetchone()[0] == 15
    items = {it["source"]: it for it in collect.setup_checklist(conn, cfg, "android")["items"]}
    assert items["phone_link"]["state"] == "collecting"

    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["phone_link"]["outcome"] == "unchanged"
    make_cache(platforms.phone_link_root(), texts=SMS + [
        (9, 1, 1, JAKE, "", "and tomorrow?", "2026-09-05T09:00:00Z")])
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["phone_link"]["outcome"] == "applied"
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE stream='android_sms' AND "
                        "status='active'").fetchone()[0] == 6


def test_collect_skips_quietly_without_a_cache_and_asks_with_two_phones(conn, cfg, home,  # noqa: F811
                                                                         windows):
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["phone_link"]["outcome"] == "skipped"
    make_cache(platforms.phone_link_root())
    make_cache(platforms.phone_link_root(), OLD_DEVICE)
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["phone_link"]["outcome"] == "error"
    assert "2 phones' caches" in by["phone_link"]["reason"]


def test_a_health_export_saved_on_the_phone_is_found(conn, cfg, linked):  # noqa: F811
    assert linked / "HealthConnect" not in collect._health_dirs(cfg)
    folder = linked / "HealthConnect"
    folder.mkdir()
    assert folder in collect._health_dirs(cfg)
    assert collect._found(cfg, "health") == "missing"
    make_hc_db(folder / "healthconnect.db")
    with zipfile.ZipFile(folder / "Health Connect export.zip", "w") as zf:
        zf.write(folder / "healthconnect.db", "healthconnect.db")
    (folder / "healthconnect.db").unlink()
    assert collect._found(cfg, "health") == "found"
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["health"]["hold"] == "first_import" and str(folder) in by["health"]["reason"]


# ---------------------------------------------------------------- where, the CLI

def test_where_lists_the_cache_on_windows(tmp_path, monkeypatch, home):  # noqa: F811
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(platforms, "home", lambda: home)
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "APPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("LIFECRM_ROOT", str(root))
    r = runner.invoke(app, ["where", "--json"])
    assert r.exit_code == 0, r.output
    groups = {g["title"]: g for g in json.loads(r.output)["groups"]}
    g = groups["Phone Link (texts, calls and contacts from the linked phone)"]
    assert g["note"] == "no phone linked in Phone Link" and g["items"][0]["state"] == "missing"
    make_cache(platforms.phone_link_root())
    r = runner.invoke(app, ["where", "--json"])
    g = {g["title"]: g for g in json.loads(r.output)["groups"]}[
        "Phone Link (texts, calls and contacts from the linked phone)"]
    assert g["items"][0]["state"] == "found" and DEVICE in g["items"][0]["path"]
    assert g["items"][0]["detail"].startswith("phone.db written ")
    assert g["commands"][0]["command"] == "python -m lifecrm observe phone_link"


def test_observe_phone_link_from_the_cli(tmp_path, monkeypatch, windows, home):  # noqa: F811
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    make_cache(platforms.phone_link_root())
    r = runner.invoke(app, ["observe", "phone_link"])
    assert r.exit_code == 0, r.output
    assert "phone-link:" in r.output and "android_sms: 5 new" in r.output
    assert "contact_card: 4 new" in r.output and "dry run" in r.output
    assert "them: see you there" in r.output          # the newest SMS, before 05:00 local: the 2nd
    assert "e.g. 2026-09-02 them: [image/jpeg]" in r.output
    r = runner.invoke(app, ["observe", "phone_link", "--apply"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0 and "phone_link" in r.output
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0, r.output
    assert "phone_link_data_age" in r.output

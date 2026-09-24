"""iOS Messages connector against a synthetic iPhone backup.

The fixture is laid out exactly like a real one: Manifest.db mapping
(domain, relativePath) to SHA1-named files in two-character folders, a
Messages sms.db, and the phone's AddressBook.sqlitedb. The schemas are the
real column names, trimmed to what the connector reads.

The deletion-semantics tests import one backup, then a later one, and check
that the archive remembers: an unsent message keeps its text, an edit updates
through newer-wins with the old body in the audit log, and a message deleted
from the phone stays.
"""

import hashlib
import json
import plistlib
import shutil
import sqlite3
from datetime import datetime, timezone

import pytest

from lifecrm.connectors import base, ios_messages as im
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks
from test_typedstream import attributed_body

APPLE_EPOCH = 978307200


def _secs(iso: str) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.timestamp()) - APPLE_EPOCH


def _ns(iso: str) -> int:
    return _secs(iso) * 1_000_000_000 + 123_456_789     # sub-second noise


SMS_SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL,
  country TEXT, service TEXT NOT NULL, uncanonicalized_id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  style INTEGER, chat_identifier TEXT, service_name TEXT, display_name TEXT);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER,
  UNIQUE(chat_id, handle_id));
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER,
  message_date INTEGER DEFAULT 0, PRIMARY KEY (chat_id, message_id));
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL,
  text TEXT, handle_id INTEGER DEFAULT 0, service TEXT, account TEXT, date INTEGER,
  date_read INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0, is_read INTEGER DEFAULT 0,
  item_type INTEGER DEFAULT 0, associated_message_guid TEXT,
  associated_message_type INTEGER DEFAULT 0, associated_message_emoji TEXT,
  attributedBody BLOB, thread_originator_guid TEXT, date_edited INTEGER DEFAULT 0,
  date_retracted INTEGER DEFAULT 0, message_summary_info BLOB,
  destination_caller_id TEXT, is_audio_message INTEGER DEFAULT 0,
  expressive_send_style_id TEXT, balloon_bundle_id TEXT,
  group_action_type INTEGER DEFAULT 0, other_handle INTEGER DEFAULT 0,
  group_title TEXT, share_status INTEGER DEFAULT 0, share_direction INTEGER DEFAULT 0);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT,
  filename TEXT, uti TEXT, mime_type TEXT, transfer_name TEXT,
  is_sticker INTEGER DEFAULT 0, hide_attachment INTEGER DEFAULT 0);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""

AB_SCHEMA = """
CREATE TABLE ABPerson (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, First TEXT,
  Last TEXT, Organization TEXT, Nickname TEXT);
CREATE TABLE ABMultiValue (UID INTEGER PRIMARY KEY, record_id INTEGER,
  property INTEGER, identifier INTEGER, label INTEGER, value TEXT);
"""

ME = "+15125550100"
HANDLES = [                           # ROWID order
    ("+15125551234", "iMessage"),    # 1 Jake
    ("5125559876", "SMS"),           # 2 Dana — stored without +1
    ("Sam@iCloud.com", "iMessage"),  # 3 Sam — mixed case
    ("+15125550000", "iMessage"),    # 4 a landline on two cards
    ("AMAZON", "SMS"),               # 5 alphanumeric sender
]
CHATS = [   # guid, style, identifier, service, display_name, member handle ids
    ("iMessage;-;+15125551234", 45, "+15125551234", "iMessage", None, [1]),
    ("SMS;-;5125559876", 45, "5125559876", "SMS", None, [2]),
    ("iMessage;+;chat123", 43, "chat123", "iMessage", "Climbing crew", [1, 3, 4]),
    ("SMS;-;AMAZON", 45, "AMAZON", "SMS", None, [5]),
]


def _summary(versions):
    """message_summary_info: 'ec' maps part index -> versions of that part."""
    return plistlib.dumps({"otr": {"0": {"le": 1, "lo": 0}}, "ec": {"0": [
        {"d": _secs(at), "t": attributed_body(text)} for at, text in versions]}},
        fmt=plistlib.FMT_BINARY)


def _messages(variant: str) -> list[dict]:
    """The phone's Messages database at one moment.

    variant 'before': M7 not yet edited, M8 not yet unsent.
    variant 'after':  M7 edited south -> north, M8 unsent.
    variant 'deleted': as 'after', with M1 deleted on the phone.
    """
    edited = variant in ("after", "deleted")
    rows = [
        dict(guid="M1", chat=1, handle_id=1, text="see you at 6", service="iMessage",
             date=_ns("2026-09-01T18:00:00Z")),
        dict(guid="M2", chat=1, handle_id=1, text=None, is_from_me=1,
             attributedBody=attributed_body("perfect, bringing snacks"),
             service="iMessage", date=_ns("2026-09-01T18:05:00Z"),
             thread_originator_guid="M1"),
        dict(guid="M3", chat=2, handle_id=2, text="running late", service="SMS",
             date=_secs("2019-03-03T12:00:00Z")),              # old: plain seconds
        dict(guid="M4", chat=3, handle_id=3, text="who's in for saturday 🧗",
             service="iMessage", date=_ns("2026-09-02T09:00:00Z")),
        dict(guid="M5", chat=1, handle_id=1, is_from_me=1, service="iMessage",
             text="Loved “see you at 6”", date=_ns("2026-09-01T18:06:00Z"),
             associated_message_type=2000, associated_message_guid="p:0/M1"),
        dict(guid="M6", chat=1, handle_id=1, is_from_me=1, service="iMessage",
             text="Removed a heart from “see you at 6”",
             date=_ns("2026-09-01T18:07:00Z"),
             associated_message_type=3000, associated_message_guid="p:0/M1"),
        dict(guid="M7", chat=1, handle_id=1, service="iMessage",
             date=_ns("2026-09-01T18:10:00Z"),
             **(dict(text="meet at the north entrance",
                     date_edited=_ns("2026-09-01T18:12:00Z"),
                     message_summary_info=_summary([
                         ("2026-09-01T18:10:00Z", "meet at the south entrance"),
                         ("2026-09-01T18:12:00Z", "meet at the north entrance")]))
                if edited else dict(text="meet at the south entrance"))),
        dict(guid="M8", chat=1, handle_id=1, service="iMessage",
             date=_ns("2026-09-01T18:20:00Z"),
             **(dict(text=None, date_retracted=_ns("2026-09-01T18:21:00Z"))
                if edited else dict(text="oops wrong chat"))),
        dict(guid="M9", chat=3, handle_id=1, text="\N{OBJECT REPLACEMENT CHARACTER}",
             service="iMessage",
             date=_ns("2026-09-02T09:05:00Z"), attachments=[1, 2]),
        dict(guid="M10", chat=3, handle_id=3, text=None, service="iMessage",
             date=_ns("2026-09-02T08:00:00Z"), item_type=1,      # Sam added
             group_action_type=0, other_handle=4),               # the landline
        dict(guid="M11", chat=1, handle_id=1, text="no clock", service="iMessage",
             date=0),
        dict(guid="M12", chat=4, handle_id=5, text="Your package was delivered",
             service="SMS", date=_ns("2026-09-03T15:00:00Z")),
        dict(guid="M13", chat=3, handle_id=4, text="hi everyone", service="iMessage",
             date=_ns("2026-09-02T10:00:00Z")),
        dict(guid="M14", chat=1, handle_id=1, is_from_me=1, service="iMessage",
             text="sent from my email address", date=_ns("2026-09-01T19:00:00Z"),
             destination_caller_id=None, account="E:me@icloud.com"),
        dict(guid="M15", chat=3, handle_id=1, service="iMessage",
             text="Reacted 🔥 to “who's in for saturday 🧗”",
             date=_ns("2026-09-02T09:01:00Z"), associated_message_type=2006,
             associated_message_guid="p:0/M4", associated_message_emoji="🔥"),
        dict(guid="M16", chat=1, handle_id=1, text=None, service="iMessage",
             attributedBody=b"\x04\x0bstreamtyped garbage", date=_ns("2026-09-01T20:00:00Z")),
        dict(guid="M17", chat=3, handle_id=1, text=None, service="iMessage",
             date=_ns("2026-09-02T09:02:00Z"), associated_message_type=3,
             associated_message_guid="p:0/M4"),
        # --- thread events -------------------------------------------------
        dict(guid="E1", chat=3, handle_id=3, service="iMessage", item_type=1,
             group_action_type=1, other_handle=4,                  # removed
             date=_ns("2026-09-03T08:00:00Z")),
        dict(guid="E2", chat=3, handle_id=1, service="iMessage", item_type=2,
             group_title="Crag rats", date=_ns("2026-09-03T08:05:00Z")),
        dict(guid="E3", chat=3, handle_id=4, service="iMessage", item_type=3,
             group_action_type=0, date=_ns("2026-09-03T08:10:00Z")),  # left
        dict(guid="E4", chat=3, is_from_me=1, service="iMessage", item_type=3,
             group_action_type=1, date=_ns("2026-09-03T08:15:00Z")),  # icon
        dict(guid="E5", chat=1, handle_id=1, service="iMessage", item_type=1,
             group_action_type=0, other_handle=1,                  # own number
             date=_ns("2026-09-03T08:20:00Z")),
        dict(guid="E6", chat=1, handle_id=1, service="iMessage", item_type=4,
             group_action_type=0, share_status=0, other_handle=1,
             share_direction=0,                                    # mine, shared
             date=_ns("2026-09-03T08:25:00Z")),
        dict(guid="E7", chat=1, handle_id=1, is_from_me=1, service="iMessage",
             item_type=5, date=_ns("2026-09-03T08:30:00Z")),       # audio kept
        dict(guid="E8", chat=1, handle_id=1, service="iMessage", item_type=6,
             date=_ns("2026-09-03T08:35:00Z")),                    # SharePlay
        dict(guid="E9", chat=1, handle_id=1, service="iMessage", item_type=9,
             date=_ns("2026-09-03T08:40:00Z")),                    # unknown
    ]
    if variant == "deleted":
        rows = [r for r in rows if r["guid"] != "M1"]
    return rows


def _write_sms_db(path, variant="after", wal_to=None):
    """Write the database. With wal_to=(main, wal), it is built in WAL mode
    and both halves are copied out BEFORE any checkpoint — a main file that
    on its own holds no tables at all, exactly the state a backup can catch."""
    conn = sqlite3.connect(path)
    if wal_to:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.executescript(SMS_SCHEMA)
    conn.executemany("INSERT INTO handle (id, service) VALUES (?, ?)", HANDLES)
    for guid, style, ident, service, name, members in CHATS:
        cur = conn.execute("INSERT INTO chat (guid, style, chat_identifier, "
                           "service_name, display_name) VALUES (?, ?, ?, ?, ?)",
                           (guid, style, ident, service, name))
        for h in members:
            conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (cur.lastrowid, h))
    conn.executemany(
        "INSERT INTO attachment (guid, filename, uti, mime_type, transfer_name) "
        "VALUES (?, ?, ?, ?, ?)",
        [("A1", "~/Library/SMS/Attachments/ab/11/A1/IMG_0001.HEIC", "public.heic",
          "image/heic", "IMG_0001.HEIC"),
         ("A2", "~/Library/SMS/Attachments/cd/22/A2/x.pluginPayloadAttachment",
          "com.apple.messages.MSMessageExtensionBalloonPlugin:0000:com.apple.messages.URLBalloonProvider",
          None, "x.pluginPayloadAttachment")])
    for m in _messages(variant):
        m = dict(m)
        chat, attach = m.pop("chat"), m.pop("attachments", [])
        m.setdefault("destination_caller_id", ME)
        cols = ", ".join(m)
        cur = conn.execute(f"INSERT INTO message ({cols}) VALUES "
                           f"({', '.join('?' for _ in m)})", list(m.values()))
        conn.execute("INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
                     (chat, cur.lastrowid))
        for a in attach:
            conn.execute("INSERT INTO message_attachment_join VALUES (?, ?)",
                         (cur.lastrowid, a))
    conn.commit()
    if wal_to:
        shutil.copyfile(path, wal_to[0])
        shutil.copyfile(f"{path}-wal", wal_to[1])
    conn.close()


def _write_address_book(path):
    conn = sqlite3.connect(path)
    conn.executescript(AB_SCHEMA)
    people = [  # (First, Last, Organization, [(property, value)])
        ("Jake", "Reilly", None, [(3, "+1 (512) 555-1234")]),
        ("Dana", "Whitfield", None, [(3, "(512) 555-9876")]),
        ("Sam", "Ortiz", None, [(4, "sam@icloud.com")]),
        ("Mom", None, None, [(3, "512-555-0000")]),
        ("Dad", None, None, [(3, "(512) 555-0000")]),
        (None, None, "Acme Corp", [(3, "+1 800 555 0199")]),   # never messaged
    ]
    for first, last, org, values in people:
        cur = conn.execute("INSERT INTO ABPerson (First, Last, Organization) "
                           "VALUES (?, ?, ?)", (first, last, org))
        for prop, value in values:
            conn.execute("INSERT INTO ABMultiValue (record_id, property, value) "
                         "VALUES (?, ?, ?)", (cur.lastrowid, prop, value))
    conn.commit()
    conn.close()


def make_backup(root, variant="after", contacts=True, encrypted=False, wal=False,
                udid="00008110-TESTUDID", device="Test iPhone"):
    """An iPhone backup folder, laid out as the Apple Devices app writes one."""
    backup = root / udid
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "Manifest.plist").write_bytes(plistlib.dumps(
        {"IsEncrypted": encrypted, "Version": "10.0"}))
    (backup / "Info.plist").write_bytes(plistlib.dumps(
        {"Device Name": device, "Last Backup Date": datetime(2026, 9, 20, 8, 0)}))
    if encrypted:
        (backup / "Manifest.db").write_bytes(bytes([0x9A]) * 4096)   # ciphertext
        return backup
    man = sqlite3.connect(backup / "Manifest.db")
    man.execute("CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, "
                "relativePath TEXT, flags INTEGER, file BLOB)")

    def place(domain, rel):
        file_id = hashlib.sha1(f"{domain}-{rel}".encode()).hexdigest()
        (backup / file_id[:2]).mkdir(exist_ok=True)
        man.execute("INSERT INTO Files VALUES (?, ?, ?, 1, NULL)", (file_id, domain, rel))
        target = backup / file_id[:2] / file_id
        if target.exists():
            target.unlink()
        return target

    sms = place("HomeDomain", "Library/SMS/sms.db")
    if wal:
        scratch = root / f"scratch-{udid}.db"
        _write_sms_db(scratch, variant,
                      wal_to=(sms, place("HomeDomain", "Library/SMS/sms.db-wal")))
    else:
        _write_sms_db(sms, variant)
    if contacts:
        _write_address_book(place("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb"))
    man.commit()
    man.close()
    return backup


def _by_guid(scan, stream=None):
    return {r["external_id"]: r for r in scan["records"]
            if stream is None or r["stream"] == stream}


def _detail(rec):
    return json.loads(rec["value_json"]) if rec["value_json"] else {}


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- reading

def test_reads_a_backup_into_the_right_streams(cfg, tmp_path):
    scan = im.scan(make_backup(tmp_path), cfg)
    by_stream = {}
    for r in scan["records"]:
        by_stream.setdefault(r["stream"], []).append(r["external_id"])
    assert sorted(by_stream["imessage"]) == ["M1", "M13", "M14", "M2", "M4", "M7", "M9"]
    assert sorted(by_stream["sms"]) == ["M12", "M3"]
    assert sorted(by_stream["text_reaction"]) == ["M15", "M5", "M6"]
    assert by_stream["text_unsend"] == ["M8"]
    assert scan["device"] == "Test iPhone" and scan["backup_date"] == "2026-09-20"
    assert scan["account"] == ME
    assert scan["source_hash"]


def test_text_comes_from_attributed_body_when_the_column_is_empty(cfg, tmp_path):
    rec = _by_guid(im.scan(make_backup(tmp_path), cfg))["M2"]
    assert rec["body"] == "perfect, bringing snacks"
    assert rec["direction"] == "out"
    assert _detail(rec)["reply_to"] == "M1"


def test_both_apple_clocks_land_on_the_same_kind_of_instant(cfg, tmp_path):
    recs = _by_guid(im.scan(make_backup(tmp_path), cfg))
    assert recs["M3"]["started_at"] == "2019-03-03T12:00:00Z"      # seconds
    assert recs["M4"]["started_at"] == "2026-09-02T09:00:00Z"      # nanoseconds
    # 09:00Z is 04:00 in Chicago — before the 05:00 rollover, so it belongs
    # to the 1st, exactly as a voice note recorded then would.
    assert recs["M4"]["journal_date"] == "2026-09-01"


def test_tapbacks_are_their_own_stream_and_carry_no_body(cfg, tmp_path):
    recs = _by_guid(im.scan(make_backup(tmp_path), cfg), "text_reaction")
    loved, removed, fire = recs["M5"], recs["M6"], recs["M15"]
    assert loved["body"] is None and removed["body"] is None
    assert _detail(loved) == {"chat": "iMessage;-;+15125551234", "part": 0,
                              "reaction": "loved", "target": "M1"}
    assert _detail(removed)["removed"] is True
    assert _detail(fire)["emoji"] == "🔥" and _detail(fire)["reaction"] == "emoji"
    assert _detail(fire)["group"] is True


def test_an_edit_is_the_current_text_with_its_history(cfg, tmp_path):
    rec = _by_guid(im.scan(make_backup(tmp_path), cfg))["M7"]
    assert rec["body"] == "meet at the north entrance"
    assert rec["last_modified_at"] == "2026-09-01T18:12:00Z"
    d = _detail(rec)
    assert d["edited_at"] == "2026-09-01T18:12:00Z"
    assert [v["text"] for v in d["versions"]] == [
        "meet at the south entrance", "meet at the north entrance"]


def test_an_unsent_message_is_an_unsend_record_not_a_message(cfg, tmp_path):
    scan = im.scan(make_backup(tmp_path), cfg)
    assert "M8" not in _by_guid(scan, "imessage")
    rec = _by_guid(scan, "text_unsend")["M8"]
    assert rec["started_at"] == "2026-09-01T18:21:00Z"
    assert _detail(rec)["sent_at"] == "2026-09-01T18:20:00Z"


def test_attachments_are_described_not_copied(cfg, tmp_path):
    scan = im.scan(make_backup(tmp_path), cfg)
    rec = _by_guid(scan)["M9"]
    assert rec["body"] is None                       # only U+FFFC in the text
    assert rec["media_path"] is None
    assert _detail(rec)["attachments"] == [{
        "mime": "image/heic", "name": "IMG_0001.HEIC",
        "path": "Library/SMS/Attachments/ab/11/A1/IMG_0001.HEIC"}]
    assert "1 × link-preview payload attachment" in scan["skipped"]


def test_a_group_message_names_every_member(cfg, tmp_path):
    rec = _by_guid(im.scan(make_backup(tmp_path), cfg))["M4"]
    assert rec["title"] == "Climbing crew"
    parts = {(p["kind"], p["value"], p["role"]) for p in rec["participants"]}
    assert parts == {("email", "sam@icloud.com", "sender"),
                     ("phone", "+15125551234", "recipient"),
                     ("phone", "+15125550000", "recipient"),
                     ("phone", ME, "recipient")}


def test_handles_are_normalized_and_named_from_the_phones_contacts(cfg, tmp_path):
    scan = im.scan(make_backup(tmp_path), cfg)
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", "+15125559876")] == "Dana Whitfield"   # +1 completed
    assert names[("email", "sam@icloud.com")] == "Sam Ortiz"      # case folded
    assert names[("phone", "+15125550000")] == "Dad / Mom"        # shared line
    assert names[("app_account", "messages:AMAZON")] is None
    assert names[("phone", ME)] == im.OWNER_LABEL
    assert names[("email", "me@icloud.com")] == im.OWNER_LABEL    # 'E:' prefix
    assert ("phone", "+18005550199") not in names   # a contact never messaged
    assert scan["named_handles"] == 4


def test_skips_and_unknowns_are_counted_not_hidden(cfg, tmp_path):
    scan = im.scan(make_backup(tmp_path), cfg)
    skipped = " | ".join(scan["skipped"])
    assert "1 × message with no usable timestamp" in skipped
    assert "1 × message whose attributedBody could not be decoded" in skipped
    assert {"what": "association type 3", "count": 1} in scan["unmapped"]
    assert {"what": "thread event item_type 9/0", "count": 1} in scan["unmapped"]


def test_the_report_reads_like_the_other_connectors(cfg, conn, tmp_path):
    scan = im.scan(make_backup(tmp_path), cfg)
    lines = im.report_lines(scan, obs.build_plan(conn, scan))
    text = "\n".join(lines)
    assert "of Test iPhone, taken 2026-09-20" in text
    assert "imessage: 7 new" in text and "text_unsend: 1 new" in text
    assert "handles: 5 (4 named from the backup's contacts)" in text
    assert "them: emoji 🔥" in text


# ---------------------------------------------------------------- targets

def test_a_bare_database_file_reads_without_names(cfg, tmp_path):
    db = tmp_path / "sms.db"
    _write_sms_db(db)
    scan = im.scan(db, cfg)
    assert len(_by_guid(scan, "imessage")) == 7
    assert not scan["contacts_read"]
    named = [h for h in scan["handles"] if h["display_name"] not in (None, im.OWNER_LABEL)]
    assert named == []


def test_a_folder_with_sms_db_and_address_book(cfg, tmp_path):
    folder = tmp_path / "copied"
    folder.mkdir()
    _write_sms_db(folder / "sms.db")
    _write_address_book(folder / "AddressBook.sqlitedb")
    scan = im.scan(folder, cfg)
    assert scan["target_kind"] == "folder" and scan["named_handles"] == 4


def test_the_source_database_is_never_opened_for_writing(cfg, tmp_path):
    db = tmp_path / "sms.db"
    _write_sms_db(db)
    before = db.read_bytes()
    im.scan(db, cfg)
    assert db.read_bytes() == before
    assert not (tmp_path / "sms.db-journal").exists()


def test_an_encrypted_backup_is_refused_with_the_way_out(cfg, tmp_path):
    with pytest.raises(DomainError, match="encrypted backup.*unencrypted"):
        im.scan(make_backup(tmp_path, encrypted=True), cfg)


def test_a_folder_that_is_neither_says_what_it_saw(cfg, tmp_path):
    folder = tmp_path / "random"
    folder.mkdir()
    (folder / "notes.txt").write_text("x")
    with pytest.raises(DomainError, match="neither an iOS backup.*notes.txt"):
        im.scan(folder, cfg)


def test_a_non_messages_database_is_refused(cfg, tmp_path):
    db = tmp_path / "other.db"
    sqlite3.connect(db).executescript("CREATE TABLE misc (id INTEGER);").connection.close()
    with pytest.raises(DomainError, match="not a Messages database"):
        im.scan(db, cfg)


# ---------------------------------------------------------------- the archive remembers

def test_reimporting_the_same_backup_is_a_no_op(cfg, conn, tmp_path):
    backup = make_backup(tmp_path)
    _apply(conn, im.scan(backup, cfg))
    plan = obs.build_plan(conn, im.scan(backup, cfg))
    assert not plan["new"] and not plan["updates"] and not plan["conflicts"]
    assert plan["identical"] == 22


def test_an_unsend_after_import_keeps_the_archived_text(cfg, conn, tmp_path):
    _apply(conn, im.scan(make_backup(tmp_path / "a", variant="before"), cfg))
    _, result = _apply(conn, im.scan(make_backup(tmp_path / "b", variant="after"), cfg))
    kept = conn.execute("SELECT body, status FROM observations WHERE stream='imessage' "
                        "AND external_id='M8'").fetchone()
    assert (kept["body"], kept["status"]) == ("oops wrong chat", "active")
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE stream='text_unsend' "
                        "AND external_id='M8'").fetchone()["c"] == 1


def test_an_edit_after_import_updates_and_the_audit_log_keeps_the_old_text(
        cfg, conn, tmp_path):
    _apply(conn, im.scan(make_backup(tmp_path / "a", variant="before"), cfg))
    plan, _ = _apply(conn, im.scan(make_backup(tmp_path / "b", variant="after"), cfg))
    assert [rec["external_id"] for _, rec, _ in plan["updates"]] == ["M7"]
    body = conn.execute("SELECT body FROM observations WHERE external_id='M7' "
                        "AND stream='imessage'").fetchone()["body"]
    assert body == "meet at the north entrance"
    audit = json.loads(conn.execute(
        "SELECT details FROM audit_log WHERE action='update' "
        "AND table_name='observations'").fetchone()["details"])
    assert audit["changes"]["body"]["old"] == "meet at the south entrance"


def test_a_message_deleted_on_the_phone_stays_in_the_archive(cfg, conn, tmp_path):
    _apply(conn, im.scan(make_backup(tmp_path / "a"), cfg))
    _apply(conn, im.scan(make_backup(tmp_path / "b", variant="deleted"), cfg))
    row = conn.execute("SELECT status FROM observations WHERE external_id='M1'"
                       ).fetchone()
    assert row["status"] == "active"


def test_the_importer_writes_none_of_the_confirmed_tables(cfg, conn, tmp_path):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in tables}
    _apply(conn, im.scan(make_backup(tmp_path), cfg))
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in tables}
    assert after == before


def test_doctor_is_green_after_an_import(cfg, conn, tmp_path):
    _apply(conn, im.scan(make_backup(tmp_path), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- handles

@pytest.mark.parametrize("raw,expected", [
    ("+15125551234", ("phone", "+15125551234")),
    ("5125551234", ("phone", "+15125551234")),
    ("(512) 555-1234", ("phone", "+15125551234")),
    ("1-512-555-1234", ("phone", "+15125551234")),
    ("+44 20 7946 0958", ("phone", "+442079460958")),
    ("020 7946 0958", ("phone", "02079460958")),     # national, country unknown
    ("0612345678", ("phone", "0612345678")),         # 10 digits, but not NANP
    ("12345", ("phone", "12345")),                   # short code
    ("Jake@iCloud.COM", ("email", "jake@icloud.com")),
    ("e:me@icloud.com", ("email", "me@icloud.com")),
    ("P:+15125550100", ("phone", "+15125550100")),
    ("AMAZON", ("app_account", "messages:AMAZON")),
    ("urn:biz:1a2b3c", ("app_account", "messages:urn:biz:1a2b3c")),
    ("", None), (None, None),
])
def test_handle_for(raw, expected):
    assert base.handle_for(raw) == expected


# ---------------------------------------------------------------- thread events

def test_thread_events_say_what_happened_and_to_whom(cfg, tmp_path):
    recs = _by_guid(im.scan(make_backup(tmp_path), cfg), "text_event")
    events = {g: _detail(r)["event"] for g, r in recs.items()}
    assert events == {
        "M10": "member_added", "E1": "member_removed", "E2": "renamed",
        "E3": "left", "E4": "icon_changed", "E5": "number_changed",
        "E6": "location_started", "E7": "audio_kept", "E8": "shareplay"}

    added = recs["M10"]
    assert added["body"] is None and added["title"] == "Climbing crew"
    assert _detail(added)["member"] == "phone:+15125550000"
    assert {(p["value"], p["role"]) for p in added["participants"]} == {
        ("sam@icloud.com", "sender"), ("+15125550000", "participant")}

    assert _detail(recs["E2"])["name"] == "Crag rats"
    assert recs["E4"]["direction"] == "out"               # you changed the icon
    assert recs["E6"]["direction"] == "out"               # your location, shared


def test_the_event_sample_reads_as_a_sentence(cfg, conn, tmp_path):
    scan = im.scan(make_backup(tmp_path), cfg)
    text = "\n".join(im.report_lines(scan, obs.build_plan(conn, scan)))
    assert "text_event: 9 new" in text
    assert "them: shareplay" in text


# ---------------------------------------------------------------- finding the backup

def test_no_path_finds_the_one_backup(cfg, tmp_path, monkeypatch):
    root = tmp_path / "Apple" / "MobileSync" / "Backup"
    backup = make_backup(root)
    monkeypatch.setattr(im, "backup_roots", lambda: [tmp_path / "nowhere", root])
    assert im.default_target(cfg) == backup


def test_no_path_refuses_to_guess_between_phones(cfg, tmp_path, monkeypatch):
    root = tmp_path / "Backup"
    make_backup(root, udid="AAAA", device="Casey's iPhone")
    make_backup(root, udid="BBBB", device="Old iPhone", contacts=False)
    monkeypatch.setattr(im, "backup_roots", lambda: [root])
    with pytest.raises(DomainError, match="2 iPhone backups found.*Casey's iPhone.*Old iPhone"):
        im.default_target(cfg)


def test_no_path_and_no_backup_says_where_it_looked(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(im, "backup_roots", lambda: [tmp_path / "Backup"])
    with pytest.raises(DomainError, match="no iPhone backup found in .*Backup.*docs/texts.md"):
        im.default_target(cfg)


def test_the_backup_roots_follow_the_windows_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "me"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "me" / "AppData" / "Roaming"))
    roots = im.backup_roots()
    assert roots[0] == tmp_path / "me" / "Apple" / "MobileSync" / "Backup"
    assert roots[1] == tmp_path / "me" / "AppData" / "Roaming" / "Apple Computer" \
        / "MobileSync" / "Backup"


def test_the_backup_folder_above_one_backup_is_accepted(cfg, tmp_path):
    root = tmp_path / "Backup"
    make_backup(root)
    scan = im.scan(root, cfg)
    assert scan["source_file"] == "00008110-TESTUDID"
    assert scan["device"] == "Test iPhone"


# ---------------------------------------------------------------- the -wal

def test_writes_only_in_the_wal_are_read(cfg, tmp_path):
    """A backup can catch the database mid-flight: here the main file holds no
    tables at all, and every message lives in sms.db-wal."""
    backup = make_backup(tmp_path, wal=True)
    file_id = hashlib.sha1(b"HomeDomain-Library/SMS/sms.db").hexdigest()
    alone = tmp_path / "main-alone.db"
    shutil.copyfile(backup / file_id[:2] / file_id, alone)
    tables = sqlite3.connect(alone).execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='message'").fetchone()[0]
    assert tables == 0, "fixture should leave the main file without the data"

    scan = im.scan(backup, cfg)
    assert len(_by_guid(scan, "imessage")) == 7
    assert scan["facts"]["wal_read"] is True


def test_a_changed_wal_is_a_changed_backup(cfg, tmp_path):
    before = im.scan(make_backup(tmp_path / "a", variant="before", wal=True), cfg)
    after = im.scan(make_backup(tmp_path / "b", variant="after", wal=True), cfg)
    assert before["source_hash"] != after["source_hash"]


def test_a_copied_folder_with_a_wal_and_no_shm(cfg, tmp_path):
    folder = tmp_path / "copied"
    folder.mkdir()
    _write_sms_db(tmp_path / "scratch.db",
                  wal_to=(folder / "sms.db", folder / "sms.db-wal"))
    scan = im.scan(folder, cfg)
    assert len(_by_guid(scan, "imessage")) == 7 and scan["facts"]["wal_read"]


# ---------------------------------------------------------------- the checklist, answered

def test_the_report_states_what_the_first_real_import_must_check(cfg, conn, tmp_path):
    scan = im.scan(make_backup(tmp_path), cfg)
    facts = scan["facts"]
    assert facts["folder"].endswith("00008110-TESTUDID")
    assert (facts["oldest_message"], facts["newest_message"]) == ("2019-03-03", "2026-09-03")
    assert facts["wal_read"] is False
    assert {"raw": "5125559876", "handle": "phone:+15125559876",
            "name": "Dana Whitfield"} in facts["normalized"]
    assert {"raw": "Sam@iCloud.com", "handle": "email:sam@icloud.com",
            "name": "Sam Ortiz"} in facts["normalized"]

    text = "\n".join(im.report_lines(scan, obs.build_plan(conn, scan)))
    assert "read from: " in text and "00008110-TESTUDID" in text
    assert "messages span 2019-03-03..2026-09-03" in text
    assert "sms.db-wal: none" in text
    assert "5125559876 -> +15125559876 (Dana Whitfield)" in text


# ---------------------------------------------------------------- staleness

def _status(report, name):
    return next((c["status"] for c in report["checks"] if c["check"] == name), None)


def test_doctor_warns_when_the_newest_text_is_old(cfg, conn, tmp_path):
    assert _status(run_checks(conn, cfg), "ios_messages_data_age") is None  # unused
    _apply(conn, im.scan(make_backup(tmp_path), cfg))      # newest: 2026-09-03
    report = run_checks(conn, cfg)
    assert _status(report, "ios_messages_data_age") == "warn"
    assert report["failures"] == 0


def test_doctor_is_quiet_when_texts_are_current(cfg, conn, tmp_path):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scan = base.empty_scan("ios_messages", records=[{
        "stream": "imessage", "external_id": "FRESH", "journal_date": now[:10],
        "started_at": now, "direction": "in", "body": "just now",
        "origin_app": "messages", "last_modified_at": now, "participants": []}])
    _apply(conn, scan)
    assert _status(run_checks(conn, cfg), "ios_messages_data_age") == "ok"


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_apply_and_suggest(tmp_path, monkeypatch):
    """The journal already knows Jake (a /log entity); the texts bring a handle
    whose contact card says 'Jake Reilly'. --suggest connects the two — and
    links nothing."""
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    from test_cli import _ingest_golden
    runner = CliRunner()
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    _ingest_golden(root)                   # creates Jake and Dana, among others
    backup = make_backup(tmp_path / "MobileSync")

    r = runner.invoke(app, ["observe", "ios_messages", str(backup)])
    assert r.exit_code == 0, r.output
    assert "iOS backup of Test iPhone" in r.output
    assert "dry run — nothing written" in r.output

    r = runner.invoke(app, ["observe", "ios_messages", str(backup), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 22 added" in r.output

    r = runner.invoke(app, ["handles", "list", "--suggest", "--json"])
    assert r.exit_code == 0, r.output
    rows = {row["value"]: row for row in json.loads(r.output)}
    assert rows["+15125551234"]["display_name"] == "Jake Reilly"
    assert [s["name"] for s in rows["+15125551234"]["suggestions"]] == ["Jake"]
    assert rows["messages:AMAZON"]["suggestions"] == []

    r = runner.invoke(app, ["handles", "list", "--suggest"])
    assert "maybe: Jake (person" in r.output
    assert "handles link phone:+15125551234 --entity" in r.output

    r = runner.invoke(app, ["sql", "SELECT entity_id FROM entity_handles "
                                   "WHERE value='+15125551234'", "--json"])
    assert json.loads(r.output)[0]["entity_id"] is None    # suggested, not linked
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_cli_observe_without_a_path_uses_the_backup_on_this_machine(tmp_path, monkeypatch):
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    root = tmp_path / "MobileSync" / "Backup"
    make_backup(root)
    monkeypatch.setattr(im, "backup_roots", lambda: [root])

    r = runner.invoke(app, ["observe", "ios_messages", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["facts"]["folder"].endswith("00008110-TESTUDID")
    assert out["new"] == 22

    r = runner.invoke(app, ["observe", "ai_chat"])
    assert r.exit_code == 1
    assert "ai_chat needs a path" in r.output

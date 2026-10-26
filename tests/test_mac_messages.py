"""Mac Messages connector against a synthetic Mac home Library.

The Mac's chat.db is the same schema lineage as the phone's sms.db, so the
fixture is the phone fixture's database (test_ios_messages) written where a
Mac keeps it — <home>/Library/Messages/chat.db, attachment paths under
~/Library/Messages/Attachments — with Contacts stores (AddressBook-v22.abcddb,
test_platforms.make_abcddb) where a Mac keeps those.

What these tests pin beyond the phone's:
- the Mac is its own mirroring scope: neither source's deletions can reach
  what only the other brought in;
- one message read from both is one record — identical, except where its
  attachment file is, which the report folds into one line;
- Recently Deleted counts as deleted (decision #2), for both connectors;
- macOS privacy (TCC) failures are one line naming Full Disk Access;
- the demo sample renders the persona and imports cleanly.
"""

import hashlib
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from lifecrm import platforms
from lifecrm.connectors import base, ios_messages as im, mac_messages as mm
from lifecrm.demo import persona
from lifecrm.demo.sources import mac_messages as sample
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks
from test_ios_messages import ME, _write_sms_db, make_backup
from test_platforms import make_abcddb

PHOTO = "Attachments/ab/11/A1/IMG_0001.HEIC"


def make_mac_home(home: Path, variant="after", owner=ME, photo_on_disk=True,
                  contacts=True) -> Path:
    """A Mac home's Library: Messages/chat.db (+ the photo's file) and two
    Contacts stores — an iCloud account under Sources/ and 'On My Mac'."""
    library = home / "Library"
    messages = library / "Messages"
    messages.mkdir(parents=True, exist_ok=True)
    db = messages / "chat.db"
    if db.exists():
        db.unlink()
    _write_sms_db(db, variant, owner=owner)
    c = sqlite3.connect(db)
    c.execute("UPDATE attachment SET filename = "
              "replace(filename, '/Library/SMS/', '/Library/Messages/')")
    c.commit()
    c.close()
    if photo_on_disk:
        (messages / PHOTO).parent.mkdir(parents=True, exist_ok=True)
        (messages / PHOTO).write_bytes(b"\x00heic")
    if contacts:
        book = library / "Application Support" / "AddressBook"
        (book / "Sources" / "ICLOUD").mkdir(parents=True, exist_ok=True)
        make_abcddb(book / "Sources" / "ICLOUD" / "AddressBook-v22.abcddb", [
            {"first": "Jake", "last": "Reilly", "phones": ["+1 (512) 555-1234"]},
            {"first": "Dana", "last": "Whitfield", "phones": ["(512) 555-9876"]},
            {"first": "Mom", "phones": ["512-555-0000"]},
        ])
        make_abcddb(book / "AddressBook-v22.abcddb", [
            {"first": "Sam", "last": "Ortiz", "emails": ["sam@icloud.com"]},
            {"first": "Dad", "phones": ["(512) 555-0000"]},
            {"org": "Acme Corp", "phones": ["+1 800 555 0199"]},
        ])
    return db


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


def _recently_delete(db: Path, *guids: str, keep_in_chat=False) -> None:
    """Do what Messages does on delete (iOS 16+ / macOS 13+): move the
    message's chat link into chat_recoverable_message_join."""
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE IF NOT EXISTS chat_recoverable_message_join (chat_id "
              "INTEGER, message_id INTEGER, delete_date INTEGER, ck_sync_state "
              "INTEGER DEFAULT 0, PRIMARY KEY (chat_id, message_id))")
    for g in guids:
        mid = c.execute("SELECT ROWID FROM message WHERE guid=?", (g,)).fetchone()[0]
        chat = c.execute("SELECT chat_id FROM chat_message_join WHERE message_id=?",
                         (mid,)).fetchone()[0]
        c.execute("INSERT INTO chat_recoverable_message_join (chat_id, message_id, "
                  "delete_date) VALUES (?, ?, 800000000)", (chat, mid))
        if not keep_in_chat:
            c.execute("DELETE FROM chat_message_join WHERE message_id=?", (mid,))
    c.commit()
    c.close()


# ---------------------------------------------------------------- reading

def test_reads_the_macs_chat_db_into_the_phones_streams(cfg, tmp_path):
    scan = mm.scan(make_mac_home(tmp_path), cfg)
    assert scan["connector"] == "mac_messages"
    by_stream = {}
    for r in scan["records"]:
        by_stream.setdefault(r["stream"], []).append(r["external_id"])
    assert sorted(by_stream["imessage"]) == ["M1", "M13", "M14", "M2", "M4", "M7", "M9"]
    assert sorted(by_stream["sms"]) == ["M12", "M3"]
    assert sorted(by_stream["text_reaction"]) == ["M15", "M5", "M6"]
    assert len(by_stream["text_event"]) == 9
    assert mm.streams == im.streams
    assert scan["source_file"] == "chat.db" and scan["source_hash"]
    assert scan["mirror_streams"] == list(im.streams) and "M1" in scan["present_ids"]


def test_the_same_reader_decodes_bodies_edits_and_tapbacks(cfg, tmp_path):
    recs = _by_guid(mm.scan(make_mac_home(tmp_path), cfg))
    assert recs["M2"]["body"] == "perfect, bringing snacks"          # attributedBody
    assert recs["M7"]["body"] == "meet at the north entrance"
    assert [v["text"] for v in _detail(recs["M7"])["versions"]] == [
        "meet at the south entrance", "meet at the north entrance"]
    assert _detail(recs["M5"])["reaction"] == "loved"
    assert "M8" not in recs                                          # unsent


def test_an_attachment_on_this_mac_becomes_media_path(cfg, tmp_path):
    db = make_mac_home(tmp_path)
    rec = _by_guid(mm.scan(db, cfg))["M9"]
    assert Path(rec["media_path"]) == (db.parent / PHOTO).resolve()
    assert _detail(rec)["attachments"] == [{"mime": "image/heic",
                                            "name": "IMG_0001.HEIC", "path": PHOTO}]


def test_an_attachment_not_downloaded_is_described_without_a_media_path(cfg, conn, tmp_path):
    scan = mm.scan(make_mac_home(tmp_path, photo_on_disk=False), cfg)
    rec = _by_guid(scan)["M9"]
    assert rec["media_path"] is None
    assert _detail(rec)["attachments"][0]["path"] == PHOTO
    assert scan["facts"]["attachments"] == {"on_disk": 0, "not_on_disk": 1}
    text = "\n".join(mm.report_lines(scan, obs.build_plan(conn, scan)))
    assert "attachments: 0 on this Mac (linked as media_path), 1 not downloaded" in text


def test_names_come_from_every_contacts_store_on_the_mac(cfg, tmp_path):
    scan = mm.scan(make_mac_home(tmp_path), cfg)
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert names[("phone", "+15125551234")] == "Jake Reilly"
    assert names[("phone", "+15125559876")] == "Dana Whitfield"
    assert names[("email", "sam@icloud.com")] == "Sam Ortiz"          # the local store
    assert names[("phone", "+15125550000")] == "Dad / Mom"            # across two stores
    assert names[("phone", ME)] == im.OWNER_LABEL
    assert ("phone", "+18005550199") not in names                     # never messaged
    assert scan["facts"]["address_books"] == 2
    assert scan["facts"]["address_books_read"] == 2
    assert scan["named_handles"] == 4


def test_a_mac_card_and_a_phone_card_name_a_handle_the_same_way(cfg, tmp_path):
    phone = {(h["kind"], h["value"]): h["display_name"]
             for h in im.scan(make_backup(tmp_path / "phone"), cfg)["handles"]}
    mac = {(h["kind"], h["value"]): h["display_name"]
           for h in mm.scan(make_mac_home(tmp_path / "mac"), cfg)["handles"]}
    assert mac == phone


def test_a_lone_copied_chat_db_imports_without_names(cfg, conn, tmp_path):
    db = tmp_path / "Downloads" / "chat.db"
    db.parent.mkdir()
    _write_sms_db(db)
    scan = mm.scan(db, cfg)
    assert len(_by_guid(scan, "imessage")) == 7
    assert not scan["contacts_read"] and scan["named_handles"] == 0
    text = "\n".join(mm.report_lines(scan, obs.build_plan(conn, scan)))
    assert "contacts: no Contacts store beside this database" in text
    assert "(0 named no address book in this target)" in text


def test_a_folder_holding_chat_db_is_accepted(cfg, tmp_path):
    db = make_mac_home(tmp_path)
    assert mm.scan(db.parent, cfg)["source_file"] == "chat.db"


def test_an_iphone_backup_is_sent_to_ios_messages(cfg, tmp_path):
    with pytest.raises(DomainError, match="iPhone backup.*observe ios_messages"):
        mm.scan(make_backup(tmp_path), cfg)
    folder = tmp_path / "copied"
    folder.mkdir()
    _write_sms_db(folder / "sms.db")
    with pytest.raises(DomainError, match="iPhone's sms.db.*observe ios_messages"):
        mm.scan(folder, cfg)


def test_a_folder_without_chat_db_says_what_it_saw(cfg, tmp_path):
    folder = tmp_path / "random"
    folder.mkdir()
    (folder / "notes.txt").write_text("x")
    with pytest.raises(DomainError, match="holds no chat.db.*notes.txt"):
        mm.scan(folder, cfg)


def test_the_source_database_is_never_opened_for_writing(cfg, tmp_path):
    db = make_mac_home(tmp_path)
    before = db.read_bytes()
    mm.scan(db, cfg)
    assert db.read_bytes() == before
    assert not db.with_name("chat.db-journal").exists()


def test_two_scans_are_identical(cfg, tmp_path):
    db = make_mac_home(tmp_path)
    a, b = mm.scan(db, cfg), mm.scan(db, cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]
    assert a["handles"] == b["handles"]


# ---------------------------------------------------------------- whose Messages

def test_the_account_is_the_phone_number_even_when_the_apple_id_sends_more(cfg, tmp_path):
    """A Mac sends from the number and the Apple ID in comparable amounts; a
    count that tips between them would move the mirroring scope."""
    db = make_mac_home(tmp_path)
    c = sqlite3.connect(db)
    c.execute("UPDATE message SET destination_caller_id='me@icloud.com' "
              "WHERE guid IN ('M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7', 'M9', 'M10', "
              "'M12', 'M13', 'M15', 'M16', 'M17')")
    c.commit()
    c.close()
    scan = mm.scan(db, cfg)
    accounts = {a["handle"]: a["messages"] for a in scan["facts"]["accounts"]}
    assert accounts["email:me@icloud.com"] > accounts[f"phone:{ME}"]
    assert scan["account"] == ME
    assert im.scan(make_backup(tmp_path / "phone"), cfg)["account"] == ME


def test_the_report_names_the_other_handles_the_mac_sends_from(cfg, conn, tmp_path):
    scan = mm.scan(make_mac_home(tmp_path), cfg)
    text = "\n".join(mm.report_lines(scan, obs.build_plan(conn, scan)))
    assert text.startswith("mac-messages: chat.db (copied Messages database; ")
    assert f"account: {ME}" in text
    assert "also sent from: email:me@icloud.com (1)" in text
    assert "contacts: 2 of 2 Contacts store(s) read" in text
    assert "imessage: 7 new" in text
    assert "handles: 5 (4 named from this Mac's Contacts)" in text
    assert "chat.db-wal: none" in text
    assert "check the oldest against what Messages shows" in text


# ---------------------------------------------------------------- this Mac

def test_no_path_reads_this_macs_own_messages(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    db = make_mac_home(tmp_path)
    target = mm.default_target(cfg)
    assert target == db
    scan = mm.scan(target, cfg)
    assert scan["target_kind"] == "live"
    assert scan["facts"]["address_books"] == 2           # via platforms.mac_addressbook_dbs
    import platform
    assert scan["facts"]["host"] == (platform.node() or None)
    text = "\n".join(mm.report_lines(scan, obs.build_plan(conn, scan)))
    assert "(this Mac's Messages" in text


def test_no_path_on_a_mac_without_messages_says_how_to_set_it_up(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(platforms, "SYSTEM", "mac")
    with pytest.raises(DomainError, match="no Messages database at .*Messages in iCloud"):
        mm.default_target(cfg)


def test_no_path_off_a_mac_points_at_the_iphone_backup(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    with pytest.raises(DomainError, match="not a Mac.*observe ios_messages"):
        mm.default_target(cfg)


# ---------------------------------------------------------------- mirroring

def test_a_message_deleted_on_the_mac_is_removed(cfg, conn, tmp_path):
    _apply(conn, mm.scan(make_mac_home(tmp_path / "a"), cfg))
    plan, result = _apply(conn, mm.scan(make_mac_home(tmp_path / "b", variant="deleted"),
                                        cfg))
    assert [r["external_id"] for r in plan["removed"]] == ["M1"]
    assert result["removed"] == 1
    imports = {r["connector"] for r in conn.execute("SELECT connector FROM imports")}
    assert imports == {"mac_messages"}
    assert run_checks(conn, cfg)["failures"] == 0


def test_removing_most_of_the_archive_at_once_is_refused(cfg, conn, tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "MASS_REMOVAL_MIN", 5)
    _apply(conn, mm.scan(make_mac_home(tmp_path / "a"), cfg))
    scan = mm.scan(make_mac_home(tmp_path / "b", variant="few"), cfg)
    plan = obs.build_plan(conn, scan)
    with pytest.raises(DomainError, match="--allow-mass-removal"):
        obs.apply_plan(conn, scan, plan)
    conn.rollback()


def test_a_phone_import_never_removes_mac_only_history(cfg, conn, tmp_path):
    """The Mac holds years the phone no longer does ('few' keeps two
    messages): the phone's import must not take them."""
    _apply(conn, mm.scan(make_mac_home(tmp_path / "mac"), cfg))
    phone = im.scan(make_backup(tmp_path / "phone", variant="few"), cfg)
    assert phone["account"] == ME                    # the same account...
    assert obs.build_plan(conn, phone)["removed"] == []   # ...another connector


def test_a_mac_import_never_removes_what_the_phone_brought_in(cfg, conn, tmp_path):
    _apply(conn, im.scan(make_backup(tmp_path / "phone"), cfg))
    mac = mm.scan(make_mac_home(tmp_path / "mac", variant="few"), cfg)
    plan = obs.build_plan(conn, mac)
    assert plan["removed"] == [] and plan["mirror_scope"] == 0


# ---------------------------------------------------------------- one message, two devices

@pytest.mark.parametrize("first", ["phone", "mac"])
def test_one_message_from_the_phone_and_the_mac_is_one_record(cfg, conn, tmp_path, first):
    phone = im.scan(make_backup(tmp_path / "phone"), cfg)
    mac = mm.scan(make_mac_home(tmp_path / "mac"), cfg)
    earlier, later = (phone, mac) if first == "phone" else (mac, phone)
    _apply(conn, earlier)
    plan = obs.build_plan(conn, later)
    assert plan["new"] == [] and plan["updates"] == []
    assert plan["identical"] == 20
    # the photo message differs only in where its file is
    assert [(c["external_id"], sorted(c["changes"])) for c in plan["conflicts"]] == [
        ("M9", ["media_path"])]
    module = mm if later is mac else im
    text = "\n".join(module.report_lines(later, plan))
    assert ("1 message(s) already archived from another device differ only in "
            "where the attachment file is") in text
    assert "CONFLICT" not in text
    obs.apply_plan(conn, later, plan)
    conn.commit()
    stored = conn.execute("SELECT media_path FROM observations WHERE external_id='M9'"
                          ).fetchone()["media_path"]
    assert (stored is not None) == (first == "mac")      # first recorded wins
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 21
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_conflict_in_the_text_itself_is_still_shown(cfg, conn, tmp_path):
    _apply(conn, im.scan(make_backup(tmp_path / "phone"), cfg))
    db = make_mac_home(tmp_path / "mac")
    c = sqlite3.connect(db)
    c.execute("UPDATE message SET text='see you at 7' WHERE guid='M1'")
    c.commit()
    c.close()
    mac = mm.scan(db, cfg)
    text = "\n".join(mm.report_lines(mac, obs.build_plan(conn, mac)))
    assert "CONFLICT imessage M1…" in text and "(body)" in text


# ---------------------------------------------------------------- Recently Deleted

@pytest.mark.parametrize("which", ["phone", "mac"])
def test_a_message_in_recently_deleted_is_deleted(cfg, conn, tmp_path, which):
    def source(folder, deleted=()):
        if which == "mac":
            target = db = make_mac_home(folder)
        else:
            target = make_backup(folder)
            file_id = hashlib.sha1(b"HomeDomain-Library/SMS/sms.db").hexdigest()
            db = target / file_id[:2] / file_id
        if deleted:
            _recently_delete(db, *deleted)
        return (mm if which == "mac" else im).scan(target, cfg)

    _apply(conn, source(tmp_path / "a"))
    scan = source(tmp_path / "b", deleted=("M1",))
    assert "M1" not in _by_guid(scan) and "M1" not in scan["present_ids"]
    assert any("1 × deleted message waiting in Recently Deleted" in s
               for s in scan["skipped"])
    plan, result = _apply(conn, scan)
    assert [r["external_id"] for r in plan["removed"]] == ["M1"]

    plan, _ = _apply(conn, source(tmp_path / "c"))          # recovered
    assert [r["external_id"] for r in plan["new"]] == ["M1"]


def test_a_message_still_shown_in_its_conversation_counts_as_present(cfg, tmp_path):
    db = make_mac_home(tmp_path)
    _recently_delete(db, "M1", keep_in_chat=True)
    scan = mm.scan(db, cfg)
    assert "M1" in _by_guid(scan) and "M1" in scan["present_ids"]


# ---------------------------------------------------------------- attachment paths

@pytest.mark.parametrize("raw,expected", [
    ("~/Library/SMS/Attachments/ab/11/A1/x.heic", "Attachments/ab/11/A1/x.heic"),
    ("~/Library/Messages/Attachments/ab/11/A1/x.heic", "Attachments/ab/11/A1/x.heic"),
    ("/var/mobile/Library/SMS/Attachments/ab/11/A1/x.heic", "Attachments/ab/11/A1/x.heic"),
    ("/Users/alex/Library/Messages/Attachments/ab/11/A1/x.heic",
     "Attachments/ab/11/A1/x.heic"),
    ("~/Library/Messages/StickerCache/s/1.png", "StickerCache/s/1.png"),
    ("~/Pictures/elsewhere.jpg", "Pictures/elsewhere.jpg"),
    ("", None), (None, None),
])
def test_attachment_paths_are_relative_to_the_messages_folder(raw, expected):
    assert im.attachment_path(raw) == expected


# ---------------------------------------------------------------- macOS privacy

def test_a_database_macos_will_not_open_says_grant_full_disk_access(cfg, tmp_path,
                                                                   monkeypatch):
    db = make_mac_home(tmp_path)
    real = sqlite3.connect

    def guarded(target, *a, **k):
        if "mode=ro" in str(target) and "Library/Messages" in str(target):
            raise sqlite3.OperationalError("unable to open database file")
        return real(target, *a, **k)
    monkeypatch.setattr(sqlite3, "connect", guarded)
    with pytest.raises(DomainError) as e:
        mm.scan(db, cfg)
    msg = str(e.value)
    assert "macOS did not allow reading" in msg and "chat.db" in msg
    assert "Full Disk Access" in msg and "Privacy & Security" in msg
    assert "\n" not in msg


def test_a_permission_error_anywhere_in_the_scan_is_one_line(cfg, tmp_path, monkeypatch):
    db = make_mac_home(tmp_path)

    def denied(target):
        raise PermissionError(1, "Operation not permitted", str(db))
    monkeypatch.setattr(mm, "_resolve", denied)
    with pytest.raises(DomainError, match=r"macOS did not allow reading .*chat\.db "
                                          r"\(Operation not permitted\).*Full Disk Access"):
        mm.scan(db, cfg)


def test_a_backup_folder_macos_guards_says_grant_full_disk_access(cfg, tmp_path,
                                                                 monkeypatch):
    root = tmp_path / "Library" / "Application Support" / "MobileSync" / "Backup"
    root.mkdir(parents=True)
    real = Path.iterdir

    def iterdir(self):
        if self.name == "Backup":
            raise PermissionError(1, "Operation not permitted", str(self))
        return real(self)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(im, "backup_roots", lambda: [root])
    with pytest.raises(DomainError, match=r"macOS did not allow reading .*MobileSync.*"
                                          r"Full Disk Access"):
        im.default_target(cfg)


@pytest.mark.parametrize("connector", ["ios_messages", "ios_calls"])
def test_a_finder_backup_whose_manifest_macos_will_not_open_says_grant_full_disk_access(
        cfg, tmp_path, monkeypatch, connector):
    from lifecrm.connectors import ios_calls
    root = tmp_path / "Library" / "Application Support" / "MobileSync" / "Backup"
    backup = make_backup(root)
    real = sqlite3.connect

    def guarded(target, *a, **k):
        if "Manifest.db" in str(target):
            raise sqlite3.OperationalError("authorization denied")
        return real(target, *a, **k)
    monkeypatch.setattr(sqlite3, "connect", guarded)
    module = im if connector == "ios_messages" else ios_calls
    with pytest.raises(DomainError) as e:
        module.scan(backup, cfg)
    msg = str(e.value)
    assert "macOS did not allow reading" in msg and "Manifest.db" in msg
    assert "authorization denied" in msg and "Full Disk Access" in msg
    assert "\n" not in msg


def test_a_permission_error_on_windows_is_not_blamed_on_macos(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    root = tmp_path / "Apple" / "MobileSync" / "Backup"
    root.mkdir(parents=True)
    real = Path.iterdir

    def iterdir(self):
        if self.name == "Backup":
            raise PermissionError(13, "Access is denied", str(self))
        return real(self)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(DomainError) as e:
        im.find_backups(root)
    assert str(e.value).startswith("permission denied reading ")
    assert "Full Disk Access" not in str(e.value)


def test_contacts_macos_will_not_list_still_import_the_messages(cfg, conn, tmp_path,
                                                               monkeypatch):
    def denied(app_support):
        raise PermissionError(1, "Operation not permitted", str(app_support))
    monkeypatch.setattr(mm, "address_books_under", denied)
    scan = mm.scan(make_mac_home(tmp_path), cfg)
    assert len(_by_guid(scan, "imessage")) == 7 and scan["named_handles"] == 0
    text = "\n".join(mm.report_lines(scan, obs.build_plan(conn, scan)))
    assert "contacts: not readable — give the app you run this from Full Disk Access" in text


def test_the_protected_folders():
    assert im.is_protected(Path("/Users/a/Library/Messages/chat.db"))
    assert im.is_protected(Path("/Users/a/Library/Application Support/MobileSync/Backup"))
    assert im.is_protected(Path("/Users/a/Library/Application Support/AddressBook/x"))
    assert im.is_protected(Path("/Users/a/Library/Application Support/CallHistoryDB/x"))
    assert not im.is_protected(Path("C:/Users/a/Apple/MobileSync/Backup"))
    assert not im.is_protected(Path("/Users/a/Downloads/chat.db"))


# ---------------------------------------------------------------- the confirmed tier

def test_the_importer_writes_none_of_the_confirmed_tables(cfg, conn, tmp_path):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities",
              "entry_sources", "extraction_runs")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in tables}
    _apply(conn, mm.scan(make_mac_home(tmp_path), cfg))
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
             for t in tables}
    assert after == before


def test_reimport_is_a_no_op_and_doctor_stays_green(cfg, conn, tmp_path):
    from test_doctor import _seed
    _seed(conn, cfg)
    db = make_mac_home(tmp_path)
    _apply(conn, mm.scan(db, cfg))
    plan = obs.build_plan(conn, mm.scan(db, cfg))
    assert not plan["new"] and not plan["updates"] and not plan["conflicts"]
    assert plan["identical"] == 21
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_texts_from_the_mac_have_a_freshness_check_of_their_own(cfg, conn, tmp_path):
    _apply(conn, mm.scan(make_mac_home(tmp_path), cfg))       # newest: 2026-09-03
    checks = {c["check"]: c["status"] for c in run_checks(conn, cfg)["checks"]}
    conn.rollback()
    assert checks.get("mac_messages_data_age") == "warn"
    assert "ios_messages_data_age" not in checks       # no backup read: not in use


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_apply_and_identical_rerun(tmp_path, monkeypatch):
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    db = make_mac_home(tmp_path / "home")

    r = runner.invoke(app, ["observe", "mac_messages", str(db)])
    assert r.exit_code == 0, r.output
    assert "mac-messages: chat.db" in r.output and "dry run — nothing written" in r.output
    r = runner.invoke(app, ["sql", "SELECT COUNT(*) n FROM observations", "--json"])
    assert json.loads(r.output)[0]["n"] == 0

    r = runner.invoke(app, ["observe", "mac_messages", str(db), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 21 added" in r.output
    r = runner.invoke(app, ["observe", "mac_messages", str(db), "--apply"])
    assert "0 added" in r.output and "21 identical skipped" in r.output

    r = runner.invoke(app, ["observe", "mac_messages", str(db), "--json"])
    out = json.loads(r.output)
    assert out["connector"] == "mac_messages" and out["identical"] == 21
    assert out["facts"]["address_books_read"] == 2
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_cli_lists_the_connector(tmp_path, monkeypatch):
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0 and "mac_messages" in r.output


# ---------------------------------------------------------------- the demo sample

ANCHOR = date(2026, 10, 21)


def test_the_demo_sample_renders_the_persona(cfg, tmp_path):
    db = sample.write_sample(tmp_path, ANCHOR, "iphone")
    assert db == tmp_path / "Library" / "Messages" / "chat.db"
    scan = mm.scan(db, cfg)
    names = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    jake = persona.person("jake")
    assert names[("phone", jake.phone)] == "Jake Morales"
    assert names[("phone", persona.person("mom").phone)] == "Linda Rivera"
    assert names[("phone", persona.OWNER.phone)] == im.OWNER_LABEL
    assert scan["account"] == persona.OWNER.phone

    bodies = [r["body"] for r in scan["records"] if r["body"]]
    assert "Veracruz tonight? you know it's my favorite" in bodies
    expected = sum(len(t.messages) for t in persona.THREADS)
    assert len([r for r in scan["records"] if r["stream"] == "imessage"]) == expected + 2

    trip = {d.isoformat() for d in persona.bled_trip_days(ANCHOR)}
    from_trip = [r for r in scan["records"] if r["journal_date"] in trip]
    assert [r["body"] for r in from_trip] == [
        "Best day of the trip. That rowboat out to the island!"]
    assert from_trip[0]["journal_date"][:4] == str(ANCHOR.year - 4)
    assert _detail(from_trip[0])["attachments"][0]["name"] == "bled_rowboat.png"
    assert from_trip[0]["media_path"] is None                    # in iCloud only

    group = [r for r in scan["records"] if r["title"] == "Bled crew"]
    assert len(group) == 5 and {r["stream"] for r in group} == {"imessage", "text_event"}
    photo = next(r for r in scan["records"] if r["media_path"])
    assert Path(photo["media_path"]).read_bytes().startswith(b"\x89PNG")
    edited = next(r for r in scan["records"] if r["body"] == "Veracruz at 7?")
    assert [v["text"] for v in _detail(edited)["versions"]] == [
        "Veracruz at 6?", "Veracruz at 7?"]
    assert any("Recently Deleted" in s for s in scan["skipped"])
    assert not scan["unmapped"]


def test_the_demo_sample_imports_cleanly_and_reimports_identically(cfg, conn, tmp_path):
    db = sample.write_sample(tmp_path, ANCHOR, "iphone")
    _, result = _apply(conn, mm.scan(db, cfg))
    assert result["added"] > 30
    plan = obs.build_plan(conn, mm.scan(db, cfg))
    assert not plan["new"] and not plan["updates"] and not plan["conflicts"]
    assert plan["identical"] == result["added"]
    assert run_checks(conn, cfg)["failures"] == 0
    fts = conn.execute("SELECT COUNT(*) c FROM observations_fts "
                       "WHERE observations_fts MATCH 'Veracruz'").fetchone()["c"]
    assert fts == 2              # "Veracruz tonight? ..." and "Veracruz at 7?"


def test_the_demo_sample_is_deterministic(cfg, tmp_path):
    a = mm.scan(sample.write_sample(tmp_path / "a", ANCHOR, "iphone"), cfg)
    b = mm.scan(sample.write_sample(tmp_path / "b", ANCHOR, "iphone"), cfg)
    strip = [{k: v for k, v in r.items() if k != "media_path"} for r in a["records"]]
    assert strip == [{k: v for k, v in r.items() if k != "media_path"}
                     for r in b["records"]]
    again = mm.scan(sample.write_sample(tmp_path / "a", ANCHOR, "iphone"), cfg)
    assert again["records"] == a["records"]


def test_the_demo_sample_has_no_android_flavor(tmp_path):
    with pytest.raises(ValueError, match="android"):
        sample.write_sample(tmp_path, ANCHOR, "android")


def test_the_samples_message_bodies_decode_from_attributed_body(tmp_path):
    from lifecrm.connectors import typedstream
    blob = sample.attributed_body("Booking the lakeside place, 4 of us")
    assert typedstream.attributed_text(blob) == "Booking the lakeside place, 4 of us"
    long = "x" * 300
    assert typedstream.attributed_text(sample.attributed_body(long)) == long


def test_message_dates_are_apple_nanoseconds():
    dt = datetime(2026, 9, 1, 18, 0, tzinfo=timezone.utc)
    assert im.apple_iso(sample.apple_ns(dt)) == "2026-09-01T18:00:00Z"


def test_the_handle_rule_is_shared():
    assert base.handle_for("(512) 555-0142") == ("phone", "+15125550142")

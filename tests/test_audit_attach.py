"""Texts stored by the reader before attachment-path normalization upgrade in
place instead of conflicting on every import.

Readers before 2026-10-22 kept an attachment's path as the Messages database
wrote it, minus a leading '~/' ('Library/SMS/Attachments/...', from a Mac's
chat.db 'Library/Messages/Attachments/...'); today's reader keeps it relative
to the Messages folder ('Attachments/...'). A message's last_modified_at is
its sent time, so the old row could never win as newer: every such message
was reported as a CONFLICT on every import, forever.

The fix is general, in domain/observations.py: a connector declares
`upgrade_stored(stream, value) -> value`, build_plan applies it to the stored
row before comparing, and a row whose upgraded value_json is exactly what the
source sends now is rewritten in place — counted, audited once per run, never
a conflict and never an update. The second half of this file holds the
shared layer to that contract with a hook declared on another connector.

The old reader is reproduced by swapping attachment_path back to what it was
(ab93a87): the database's path minus '~/'. Everything else in the reader is
unchanged since then.
"""

import hashlib
import json
import sqlite3

import pytest

from lifecrm.connectors import ios_messages as im, mac_messages as mm, notes
from lifecrm.domain import observations as obs
from lifecrm.doctor import run_checks
from test_ios_messages import make_backup
from test_mac_messages import PHOTO, make_mac_home

OLD_PHONE_PATH = "Library/SMS/Attachments/ab/11/A1/IMG_0001.HEIC"
UPGRADE_LINE = ("1 archived with the old full attachment path, rewritten to "
                "the current form (content unchanged)")


def _old_attachment_path(filename):
    """The reader before 2026-10-22: the database's path, minus '~/'."""
    if not filename:
        return None
    return filename[2:] if filename.startswith("~/") else filename


def _old_scan(monkeypatch, scanner, target, cfg):
    with monkeypatch.context() as m:
        m.setattr(im, "attachment_path", _old_attachment_path)
        return scanner(target, cfg)


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _paths(value_json):
    return [a.get("path") for a in json.loads(value_json).get("attachments", [])]


def _stored(conn, guid):
    return conn.execute("SELECT * FROM observations WHERE external_id=?",
                        (guid,)).fetchone()


def _sms_db(backup):
    file_id = hashlib.sha1(b"HomeDomain-Library/SMS/sms.db").hexdigest()
    return backup / file_id[:2] / file_id


def _audits(conn, action):
    return [json.loads(r["details"]) for r in conn.execute(
        "SELECT details FROM audit_log WHERE action=? ORDER BY id", (action,))]


# ---------------------------------------------------------------- texts

@pytest.mark.parametrize("raw,old", [
    ("~/Library/SMS/Attachments/ab/11/A1/IMG_0001.HEIC", OLD_PHONE_PATH),
    ("/var/mobile/Library/SMS/Attachments/ab/11/A1/IMG_0001.HEIC",
     "/var/mobile/Library/SMS/Attachments/ab/11/A1/IMG_0001.HEIC"),
])
def test_a_text_the_old_reader_stored_is_upgraded_not_a_conflict(
        cfg, conn, tmp_path, monkeypatch, raw, old):
    backup = make_backup(tmp_path / "phone")
    db = sqlite3.connect(_sms_db(backup))
    db.execute("UPDATE attachment SET filename=? WHERE guid='A1'", (raw,))
    db.commit()
    db.close()
    _apply(conn, _old_scan(monkeypatch, im.scan, backup, cfg))
    assert _paths(_stored(conn, "M9")["value_json"]) == [old]

    scan = im.scan(backup, cfg)
    plan = obs.build_plan(conn, scan)
    assert plan["conflicts"] == [] and plan["new"] == [] and plan["updates"] == []
    assert [row["external_id"] for row, _ in plan["format_upgrades"]] == ["M9"]
    assert plan["identical"] == 20                  # M9 is written, so not skipped
    text = "\n".join(im.report_lines(scan, plan))
    assert "CONFLICT" not in text
    assert UPGRADE_LINE in text

    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    assert result["format_upgraded"] == 1 and result["conflicts"] == 0
    assert result["updated"] == 0
    incoming = next(r for r in scan["records"] if r["external_id"] == "M9")
    row = _stored(conn, "M9")
    assert row["value_json"] == incoming["value_json"]
    assert _paths(row["value_json"]) == ["Attachments/ab/11/A1/IMG_0001.HEIC"]

    # one audit row for the run, ids only; nothing logged as a content update
    [audit] = _audits(conn, "format_upgrade")
    assert audit == {"connector": "ios_messages", "import": result["import_uuid"],
                     "field": "value_json", "upgraded": 1,
                     "streams": {"imessage": 1}, "external_ids": ["M9"]}
    assert _audits(conn, "update") == []
    counts = json.loads(conn.execute("SELECT counts_json FROM imports WHERE uuid=?",
                                     (result["import_uuid"],)).fetchone()["counts_json"])
    assert counts["format_upgraded"] == 1 and counts["conflicts"] == 0

    # settled: the next import is a plain no-op
    again = obs.build_plan(conn, im.scan(backup, cfg))
    assert again["identical"] == 21
    assert again["format_upgrades"] == [] and again["conflicts"] == []
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_an_old_phone_row_meets_the_mac_with_only_the_file_location_left(
        cfg, conn, tmp_path, monkeypatch):
    """The verifier's second case: a Mac import of the same message. Its
    value_json is the same once upgraded; media_path is the one honest
    difference (the Mac has the file, the phone's copy does not), still
    folded into one line and still kept as first recorded."""
    _apply(conn, _old_scan(monkeypatch, im.scan, make_backup(tmp_path / "phone"), cfg))
    mac = mm.scan(make_mac_home(tmp_path / "mac"), cfg)
    plan = obs.build_plan(conn, mac)
    assert plan["new"] == [] and plan["updates"] == []
    assert [(c["external_id"], sorted(c["changes"])) for c in plan["conflicts"]] == [
        ("M9", ["media_path"])]
    assert [row["external_id"] for row, _ in plan["format_upgrades"]] == ["M9"]
    text = "\n".join(mm.report_lines(mac, plan))
    assert "CONFLICT" not in text
    assert ("1 message(s) already archived from another device differ only in "
            "where the attachment file is") in text

    obs.apply_plan(conn, mac, plan)
    conn.commit()
    row = _stored(conn, "M9")
    assert _paths(row["value_json"]) == [PHOTO]
    assert row["media_path"] is None                 # the first recorded stays
    assert [a["connector"] for a in _audits(conn, "format_upgrade")] == ["mac_messages"]
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_macs_chat_db_the_old_reader_imported_upgrades_under_mac_messages(
        cfg, conn, tmp_path, monkeypatch):
    """Before mac_messages existed a Mac's chat.db went through ios_messages
    as a bare file, keeping 'Library/Messages/Attachments/...'."""
    db = make_mac_home(tmp_path / "mac")
    _apply(conn, _old_scan(monkeypatch, im.scan, db, cfg))
    assert _paths(_stored(conn, "M9")["value_json"]) == [
        "Library/Messages/Attachments/ab/11/A1/IMG_0001.HEIC"]

    mac = mm.scan(db, cfg)
    plan = obs.build_plan(conn, mac)
    assert [sorted(c["changes"]) for c in plan["conflicts"]] == [["media_path"]]
    assert len(plan["format_upgrades"]) == 1
    obs.apply_plan(conn, mac, plan)
    conn.commit()
    assert _paths(_stored(conn, "M9")["value_json"]) == [PHOTO]

    # and the phone's own reader settles on it too
    plan = obs.build_plan(conn, im.scan(db, cfg))
    assert plan["conflicts"] == [] and plan["format_upgrades"] == []


def test_a_real_difference_beside_an_old_path_is_still_a_conflict(
        cfg, conn, tmp_path, monkeypatch):
    """The upgrade only ever accounts for the spelling. An attachment renamed
    since — the stored row upgraded still differs — is reported as it always
    was, and the stored row is left exactly as it is."""
    backup = make_backup(tmp_path / "phone")
    _apply(conn, _old_scan(monkeypatch, im.scan, backup, cfg))
    before = _stored(conn, "M9")["value_json"]
    db = sqlite3.connect(_sms_db(backup))
    db.execute("UPDATE attachment SET transfer_name='renamed.HEIC' WHERE guid='A1'")
    db.commit()
    db.close()

    scan = im.scan(backup, cfg)
    plan = obs.build_plan(conn, scan)
    assert [(c["external_id"], sorted(c["changes"])) for c in plan["conflicts"]] == [
        ("M9", ["value_json"])]
    assert plan["format_upgrades"] == []
    assert "CONFLICT imessage M9…" in "\n".join(im.report_lines(scan, plan))
    obs.apply_plan(conn, scan, plan)
    conn.commit()
    assert _stored(conn, "M9")["value_json"] == before
    assert _audits(conn, "format_upgrade") == []


def test_the_cli_reports_the_upgrade_and_conflicts_no_more(tmp_path, monkeypatch):
    from conftest import cli_project
    from typer.testing import CliRunner

    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    backup = make_backup(tmp_path / "MobileSync")
    with monkeypatch.context() as m:
        m.setattr(im, "attachment_path", _old_attachment_path)
        r = runner.invoke(app, ["observe", "ios_messages", str(backup), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 21 added" in r.output

    for args in ([], ["--apply"]):
        r = runner.invoke(app, ["observe", "ios_messages", str(backup), *args])
        assert r.exit_code == 0, r.output
        assert "CONFLICT" not in r.output and "conflict(s)" not in r.output
        assert UPGRADE_LINE in r.output
    assert "applied: 0 added, 0 updated, 0 refreshed; 20 identical skipped" in r.output

    r = runner.invoke(app, ["observe", "ios_messages", str(backup), "--json"])
    out = json.loads(r.output)
    assert out["conflicts"] == [] and out["identical"] == 21
    r = runner.invoke(app, ["imports", "list", "--json"])
    newest = json.loads(r.output)[0]
    assert json.loads(newest["counts_json"])["format_upgraded"] == 1
    assert runner.invoke(app, ["doctor"]).exit_code == 0


@pytest.mark.parametrize("old,new", [
    ("Library/SMS/Attachments/ab/11/A1/x.heic", "Attachments/ab/11/A1/x.heic"),
    ("Library/Messages/Attachments/ab/11/A1/x.heic", "Attachments/ab/11/A1/x.heic"),
    ("/var/mobile/Library/SMS/Attachments/ab/11/A1/x.heic", "Attachments/ab/11/A1/x.heic"),
    ("/Users/alex/Library/Messages/Attachments/ab/11/A1/x.heic",
     "Attachments/ab/11/A1/x.heic"),
    ("Attachments/ab/11/A1/x.heic", "Attachments/ab/11/A1/x.heic"),    # current
])
def test_upgrade_stored_rewrites_only_the_attachment_path(old, new):
    value = {"chat": "iMessage;+;chat123", "service": "iMessage", "group": True,
             "attachments": [{"name": "x.heic", "mime": "image/heic", "path": old},
                             {"name": "no-path.vcf"}]}
    got = im.upgrade_stored("imessage", value)
    assert got == {**value, "attachments": [
        {"name": "x.heic", "mime": "image/heic", "path": new}, {"name": "no-path.vcf"}]}
    assert value["attachments"][0]["path"] == old          # the input is untouched


def test_upgrade_stored_leaves_other_streams_and_values_alone():
    reaction = {"reaction": "loved", "target": "M1", "attachments": [{"path": "x"}]}
    assert im.upgrade_stored("text_reaction", reaction) == reaction
    plain = {"chat": "SMS;-;5125559876", "service": "SMS"}
    assert im.upgrade_stored("sms", plain) == plain
    assert mm.upgrade_stored is im.upgrade_stored
    assert obs.declared_upgrade("mac_messages") is im.upgrade_stored
    assert obs.declared_upgrade("ios_messages") is im.upgrade_stored


# ---------------------------------------------------------------- the shared hook

def _note(**kw):
    rec = {"stream": "note", "external_id": "n1", "journal_date": "2026-03-10",
           "started_at": "2026-03-10T15:00:00Z", "title": "Groceries", "body": "eggs",
           "value_json": json.dumps({"folder": "Home"}, sort_keys=True),
           "last_modified_at": "2026-03-10T15:00:00Z", "participants": []}
    rec.update(kw)
    return rec


def _notes_scan(*records):
    return {"connector": "notes", "account": None, "source_file": "notes.zip",
            "source_hash": None, "records": list(records), "handles": [],
            "unmapped": [], "skipped": []}


OLD = json.dumps({"dir": "Home"}, sort_keys=True)          # the old spelling


@pytest.fixture
def renamed_key(monkeypatch):
    """A notes reader that renamed value_json's 'dir' to 'folder'."""
    calls = []

    def upgrade_stored(stream, value):
        calls.append(stream)
        value = dict(value)
        if "dir" in value:
            value["folder"] = value.pop("dir")
        return value
    monkeypatch.setattr(notes, "upgrade_stored", upgrade_stored, raising=False)
    return calls


def test_any_connector_can_declare_an_upgrade(conn, renamed_key):
    _apply(conn, _notes_scan(_note(value_json=OLD)))
    plan, result = _apply(conn, _notes_scan(_note()))
    assert plan["conflicts"] == [] and plan["identical"] == 0
    assert len(plan["format_upgrades"]) == 1 and result["format_upgraded"] == 1
    assert _stored(conn, "n1")["value_json"] == _note()["value_json"]
    assert [a["connector"] for a in _audits(conn, "format_upgrade")] == ["notes"]

    # a row already in the current spelling never reaches the hook
    renamed_key.clear()
    plan, result = _apply(conn, _notes_scan(_note()))
    assert plan["identical"] == 1 and renamed_key == []
    assert "format_upgraded" not in result


def test_without_a_declared_upgrade_the_old_spelling_stays_a_conflict(conn):
    _apply(conn, _notes_scan(_note(value_json=OLD)))
    plan = obs.build_plan(conn, _notes_scan(_note()))
    assert [sorted(c["changes"]) for c in plan["conflicts"]] == [["value_json"]]
    assert plan["format_upgrades"] == []


def test_an_upgrade_rides_along_with_a_refresh(conn, renamed_key):
    _apply(conn, _notes_scan(_note(value_json=OLD)))
    later = _note(last_modified_at="2026-03-11T09:00:00Z")
    plan, result = _apply(conn, _notes_scan(later))
    assert len(plan["refreshed"]) == 1 and len(plan["format_upgrades"]) == 1
    row = _stored(conn, "n1")
    assert row["value_json"] == later["value_json"]
    assert row["last_modified_at"] == "2026-03-11T09:00:00Z"
    assert _audits(conn, "update") == []


def test_an_update_audits_what_the_row_really_held(conn, renamed_key):
    _apply(conn, _notes_scan(_note(value_json=OLD)))
    edited = _note(body="eggs, milk", last_modified_at="2026-03-11T09:00:00Z")
    plan, result = _apply(conn, _notes_scan(edited))
    assert len(plan["updates"]) == 1 and plan["format_upgrades"] == []
    assert result["updated"] == 1 and "format_upgraded" not in result
    [audit] = _audits(conn, "update")
    assert audit["changes"]["body"] == {"old": "eggs", "new": "eggs, milk"}
    assert audit["changes"]["value_json"] == {"old": OLD, "new": edited["value_json"]}
    assert _audits(conn, "format_upgrade") == []


def test_an_upgrade_that_does_not_reach_todays_value_is_not_taken(conn, renamed_key):
    _apply(conn, _notes_scan(_note(value_json=OLD)))
    moved = _note(value_json=json.dumps({"folder": "Work"}, sort_keys=True))
    plan, _ = _apply(conn, _notes_scan(moved))
    assert plan["format_upgrades"] == []
    assert plan["conflicts"][0]["changes"]["value_json"] == {
        "existing": OLD, "incoming": moved["value_json"]}
    assert _stored(conn, "n1")["value_json"] == OLD


def test_an_upgrade_beside_another_conflict_is_still_written(conn, renamed_key):
    """The spelling is settled either way; what really differs (here the
    body, not newer) is still reported, and the stored body is kept."""
    _apply(conn, _notes_scan(_note(value_json=OLD)))
    plan, _ = _apply(conn, _notes_scan(_note(body="ham")))
    assert [sorted(c["changes"]) for c in plan["conflicts"]] == [["body"]]
    row = _stored(conn, "n1")
    assert row["value_json"] == _note()["value_json"] and row["body"] == "eggs"

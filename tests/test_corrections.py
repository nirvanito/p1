import json

import pytest

from lifecrm.domain import corrections, entities, entries, measurements
from lifecrm.domain.util import DomainError


def _entry(conn, title="Lunch", cat="food", kind="event"):
    return entries.create(conn, journal_date="2026-07-27", kind=kind,
                          title=title, primary_category=cat, actor="extraction")


def test_edit_records_revision_and_confirms(conn):
    e = _entry(conn)
    corrections.edit_entry(conn, e["uuid"], {"title": "Lunch: burrito bowl",
                                             "primary_category": "food",
                                             "secondary_categories": ["social"]})
    row = entries.get_by_uuid(conn, e["uuid"])
    assert row["title"] == "Lunch: burrito bowl"
    assert row["confirmed_at"] is not None
    revs = conn.execute("SELECT * FROM entry_revisions WHERE entry_uuid=? ORDER BY id",
                        (e["uuid"],)).fetchall()
    assert [r["change_kind"] for r in revs] == ["create", "edit"]
    old = json.loads(revs[-1]["old_json"])
    new = json.loads(revs[-1]["new_json"])
    assert old["title"] == "Lunch"
    assert new["categories"]["secondary"] == ["social"]


def test_edit_kind_and_dates(conn):
    e = _entry(conn, kind="event")
    corrections.edit_entry(conn, e["uuid"], {"kind": "plan", "due_at": "2026-08-15"})
    snap = entries.snapshot(conn, entries.get_by_uuid(conn, e["uuid"])["id"])
    assert snap["kind"] == "plan"
    assert snap["due_at"] == "2026-08-15"


def test_edit_unknown_field_rejected(conn):
    e = _entry(conn)
    with pytest.raises(DomainError, match="unknown field"):
        corrections.edit_entry(conn, e["uuid"], {"mood": 7})


def test_edit_invalid_value_rejected(conn):
    e = _entry(conn)
    with pytest.raises(DomainError, match="time_of_day"):
        corrections.edit_entry(conn, e["uuid"], {"time_of_day": "dawn"})


def test_retract_restore_history(conn):
    e = _entry(conn)
    corrections.retract_entry(conn, e["uuid"], reason="did not happen")
    assert entries.get_by_uuid(conn, e["uuid"])["status"] == "retracted"
    with pytest.raises(DomainError, match="already retracted"):
        corrections.retract_entry(conn, e["uuid"])
    with pytest.raises(DomainError, match="retracted"):
        corrections.edit_entry(conn, e["uuid"], {"title": "X"})
    corrections.restore_entry(conn, e["uuid"])
    assert entries.get_by_uuid(conn, e["uuid"])["status"] == "active"
    kinds = [r["change_kind"] for r in conn.execute(
        "SELECT change_kind FROM entry_revisions WHERE entry_uuid=? ORDER BY id",
        (e["uuid"],))]
    assert kinds == ["create", "retract", "restore"]
    actions = [r["action"] for r in conn.execute(
        "SELECT action FROM audit_log WHERE row_uuid=? ORDER BY id", (e["uuid"],))]
    assert "retract" in actions and "restore" in actions


def test_merge_entries_moves_everything(conn):
    a = _entry(conn, "Bouldering at Movement", "exercise")
    b = _entry(conn, "Climbing at Movement gym", "exercise")
    jake = entities.create(conn, "person", "Jake")
    entries.attach_entity(conn, a["id"], jake["id"], "participant")
    measurements.add(conn, entry_id=a["id"], metric="duration",
                     numeric_value=90, unit="min")

    corrections.merge_entries(conn, a["uuid"], b["uuid"])

    src = entries.get_by_uuid(conn, a["uuid"])
    assert src["status"] == "superseded"
    assert src["superseded_by"] == b["id"]
    dst_snap = entries.snapshot(conn, b["id"])
    assert [x["name"] for x in dst_snap["entities"]] == ["Jake"]
    assert dst_snap["measurements"][0]["metric"] == "duration"
    # both sides got revision rows
    assert conn.execute("SELECT COUNT(*) c FROM entry_revisions WHERE entry_uuid=? "
                        "AND change_kind='merge_out'", (a["uuid"],)).fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM entry_revisions WHERE entry_uuid=? "
                        "AND change_kind='merge_in'", (b["uuid"],)).fetchone()["c"] == 1


def test_merge_requires_active(conn):
    a = _entry(conn, "A")
    b = _entry(conn, "B")
    corrections.retract_entry(conn, b["uuid"])
    with pytest.raises(DomainError, match="not active"):
        corrections.merge_entries(conn, a["uuid"], b["uuid"])


def test_purge_day_is_explicit_and_audited(conn):
    _entry(conn, "A")
    _entry(conn, "B")
    measurements.add(conn, journal_date="2026-07-27", metric="mood", numeric_value=7)
    result = corrections.purge_day(conn, "2026-07-27")
    assert result["entries_deleted"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM measurements "
                        "WHERE journal_date='2026-07-27'").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM audit_log WHERE action='purge_day'"
                        ).fetchone()["c"] == 1

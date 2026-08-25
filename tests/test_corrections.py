import json

import pytest
from conftest import golden_draft

from lifecrm.domain import corrections, entities, entries, measurements, reconcile
from lifecrm.domain.util import DomainError
from lifecrm.doctor import run_checks
from lifecrm.importer import import_text
from test_reconcile import _ingest


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


# --- merge and provenance ---------------------------------------------------
# A real day is one voice note yielding many entries, so merging two of them
# means src and dst SHARE a source. entry_sources' primary key is
# (entry_id, source_id), so the old move (UPDATE OR IGNORE + DELETE) dropped
# src's row on the floor together with the original_json in it, and doctor's
# entries_have_provenance failed for the superseded entry from then on.

def _seeded_pair(conn, cfg):
    """Two real entries off one real source, with real provenance."""
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
    conn.commit()
    rows = conn.execute("SELECT id, uuid FROM entries ORDER BY id").fetchall()
    return rows[0], rows[1]


def _provenance(conn, entry_id):
    return {r["source_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM entry_sources WHERE entry_id=?", (entry_id,))}


def test_merge_keeps_the_source_entrys_provenance(conn, cfg):
    src, dst = _seeded_pair(conn, cfg)
    before = _provenance(conn, src["id"])
    assert before, "fixture should give src provenance"
    assert any(r["original_json"] for r in before.values())

    corrections.merge_entries(conn, src["uuid"], dst["uuid"])
    conn.commit()

    after = _provenance(conn, src["id"])
    assert after, "merge destroyed the superseded entry's provenance"
    assert after == before, "merge altered the superseded entry's provenance"
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"]
                                     if c["status"] == "fail"]


def test_merge_does_not_overwrite_the_destinations_own_provenance(conn, cfg):
    """Copying must not clobber: dst's row for the shared source is its own
    extraction, with its own offsets and original_json."""
    src, dst = _seeded_pair(conn, cfg)
    before = _provenance(conn, dst["id"])

    corrections.merge_entries(conn, src["uuid"], dst["uuid"])
    conn.commit()

    assert _provenance(conn, dst["id"]) == before


def test_merge_gives_the_destination_the_sources_it_lacked(conn, cfg):
    """The other half of copy-don't-move: when src cites a source dst does not
    (a second note about the same event), dst absorbs that citation."""
    src, dst = _seeded_pair(conn, cfg)
    second = import_text(conn, cfg, "A second note about the same morning.",
                         journal_date="2026-07-27", source_type="pasted")
    entries.add_provenance(conn, entry_id=src["id"], source_id=second["source_id"],
                           char_start=2, char_end=8,
                           original_json={"title": "from the second note"})
    conn.commit()

    corrections.merge_entries(conn, src["uuid"], dst["uuid"])
    conn.commit()

    copied = _provenance(conn, dst["id"])[second["source_id"]]
    assert (copied["char_start"], copied["char_end"]) == (2, 8)
    assert json.loads(copied["original_json"])["title"] == "from the second note"
    assert second["source_id"] in _provenance(conn, src["id"]), "src kept its own"
    assert run_checks(conn, cfg)["failures"] == 0


def test_merge_copies_observation_provenance(conn, cfg, tmp_path):
    """v4 provenance travels the same way. The entry_observations row is made
    by hand because nothing writes one yet — but doctor already accepts it as
    an entry's only provenance, so merge has to carry it."""
    from conftest import FIXTURES

    from lifecrm.connectors import ai_chat
    from lifecrm.domain import observations as obs

    src, dst = _seeded_pair(conn, cfg)
    export = tmp_path / "conversations.json"
    export.write_bytes((FIXTURES / "ai_chat_claude.json").read_bytes())
    scan = ai_chat.scan(export, cfg)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    obs_id = conn.execute("SELECT id FROM observations ORDER BY id LIMIT 1"
                          ).fetchone()["id"]
    conn.execute("INSERT INTO entry_observations (entry_id, observation_id, "
                 "char_start, char_end) VALUES (?, ?, 3, 9)", (src["id"], obs_id))
    conn.commit()

    corrections.merge_entries(conn, src["uuid"], dst["uuid"])
    conn.commit()

    rows = {r["entry_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM entry_observations WHERE observation_id=?", (obs_id,))}
    assert set(rows) == {src["id"], dst["id"]}
    assert (rows[dst["id"]]["char_start"], rows[dst["id"]]["char_end"]) == (3, 9)
    assert run_checks(conn, cfg)["failures"] == 0


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

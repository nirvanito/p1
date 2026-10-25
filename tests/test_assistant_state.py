"""Schema v6: the assistant layer's state — snooze/dismiss rows for nudges and
the published calendar's revisions — written only through the domain layer,
audited, exported, checked by doctor, and invisible to the as-of stamp."""

from __future__ import annotations

import shutil

import pytest

from lifecrm import SCHEMA_VERSION, db as db_mod, obsidian
from lifecrm.doctor import run_checks
from lifecrm.domain import corrections, entries
from lifecrm.domain import nudges as nudge_state
from lifecrm.domain import reminders as rev
from lifecrm.domain.util import DomainError
from lifecrm.export import export_jsonl
from tests.conftest import init_v1_db

NOW = "2026-10-21T12:00:00Z"


# ---------------------------------------------------------------- migration

def test_schema_sql_carries_migration_0006_verbatim():
    def norm(text):
        return text.replace("\r\n", "\n")
    mig = norm((db_mod.MIGRATIONS_DIR / "0006_assistant_state.sql")
               .read_text(encoding="utf-8-sig"))
    body = mig.split("\n\n", 1)[1]
    assert body in norm(db_mod.SCHEMA_FILE.read_text(encoding="utf-8"))


def test_a_v5_database_migrates_to_v6_and_gains_both_tables(cfg, tmp_path):
    conn = init_v1_db(cfg.db_path)
    mig = tmp_path / "mig"
    mig.mkdir()
    for f in sorted(db_mod.MIGRATIONS_DIR.glob("000[2345]_*.sql")):
        shutil.copy(f, mig / f.name)
    assert db_mod.migrate(conn, migrations_dir=mig) == [2, 3, 4, 5]
    try:
        assert db_mod.migrate(conn) == [6]
        assert db_mod.schema_version(conn) == SCHEMA_VERSION == 6
        for t in ("nudge_states", "reminder_revisions"):
            conn.execute(f"SELECT * FROM {t} LIMIT 0")
        assert run_checks(conn)["failures"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------- nudge states

def test_snooze_dismiss_and_restore_are_audited_and_keyed(conn):
    s = nudge_state.snooze(conn, "due:abc-123", until="2026-10-28", note="after the trip")
    assert s["state"] == "snoozed" and s["until"] == "2026-10-28"
    nudge_state.dismiss(conn, "stale:browser")
    assert set(nudge_state.state_for(conn)) == {"due:abc-123", "stale:browser"}
    # a second snooze of the same key updates the row, never a second row
    again = nudge_state.snooze(conn, "due:abc-123", until="2026-11-01")
    assert again["until"] == "2026-11-01" and len(nudge_state.states(conn)) == 2
    gone = nudge_state.restore(conn, "due:abc-123")
    assert gone["was"] == "snoozed" and "due:abc-123" not in nudge_state.state_for(conn)
    actions = [r["action"] for r in conn.execute(
        "SELECT action FROM audit_log WHERE table_name='nudge_states' ORDER BY id")]
    assert actions == ["snooze", "dismiss", "snooze", "restore"]
    with pytest.raises(DomainError, match="neither snoozed nor dismissed"):
        nudge_state.restore(conn, "due:abc-123")


@pytest.mark.parametrize("key", ["bogus:1", "due", "due:", "goal: x", ""])
def test_a_key_must_name_a_known_kind(conn, key):
    with pytest.raises(DomainError, match="nudge key must look like"):
        nudge_state.dismiss(conn, key)


def test_a_snooze_needs_a_date(conn):
    with pytest.raises(DomainError, match="YYYY-MM-DD"):
        nudge_state.snooze(conn, "due:abc", until="soon")
    with pytest.raises(DomainError, match="YYYY-MM-DD"):
        nudge_state.snooze(conn, "due:abc", until="2026-13-40")


# ---------------------------------------------------------------- reminder revisions

def test_record_publication_follows_the_revision_rules(conn):
    first = rev.record_publication(conn, {"u1": "fp1", "u2": "fp2"}, NOW)
    assert first["added"] == 2 and first["changed"] == 0
    assert first["stamps"]["u1"] == {"sequence": 0, "last_modified": NOW}

    same = rev.record_publication(conn, {"u1": "fp1", "u2": "fp2"}, "2026-10-21T13:00:00Z")
    assert same["stamps"] == first["stamps"]
    assert not any(same[k] for k in ("added", "changed", "restored", "removed"))

    moved = rev.record_publication(conn, {"u1": "fp1-moved"}, "2026-10-22T07:00:00Z")
    assert moved["changed"] == 1 and moved["removed"] == 1 and "u2" not in moved["stamps"]
    assert moved["stamps"]["u1"] == {"sequence": 1, "last_modified": "2026-10-22T07:00:00Z"}
    tomb = conn.execute("SELECT * FROM reminder_revisions WHERE uid='u2'").fetchone()
    assert tomb["present"] == 0 and tomb["sequence"] == 0

    # u2 comes back with its original content: a newer sequence all the same
    back = rev.record_publication(conn, {"u1": "fp1-moved", "u2": "fp2"},
                                  "2026-10-23T07:00:00Z")
    assert back["restored"] == 1
    assert back["stamps"]["u2"] == {"sequence": 1, "last_modified": "2026-10-23T07:00:00Z"}
    assert back["stamps"]["u1"]["sequence"] == 1          # untouched

    publishes = conn.execute("SELECT COUNT(*) FROM audit_log "
                             "WHERE table_name='reminder_revisions'").fetchone()[0]
    assert publishes == 3                                 # the identical one left no row


def test_record_publication_validates_its_input(conn):
    with pytest.raises(DomainError, match="ISO datetime"):
        rev.record_publication(conn, {"u1": "fp"}, "today")
    with pytest.raises(DomainError, match="non-empty UID"):
        rev.record_publication(conn, {"": "fp"}, NOW)
    with pytest.raises(DomainError, match="fingerprint"):
        rev.record_publication(conn, {"u1": ""}, NOW)


def test_a_publication_never_moves_the_content_stamp_but_a_snooze_does(conn, cfg):
    entries.create(conn, journal_date="2026-10-20", kind="task", title="Book flights",
                   primary_category="travel")
    conn.commit()
    before = obsidian.change_stamp(cfg)
    last = obsidian._last_change(conn)
    rev.record_publication(conn, {"u1": "fp1"}, NOW)
    conn.commit()
    assert obsidian.change_stamp(cfg) == before
    assert obsidian._last_change(conn) == last
    nudge_state.snooze(conn, "due:whatever", until="2026-10-30")
    conn.commit()
    assert obsidian.change_stamp(cfg) != before


# ---------------------------------------------------------------- doctor, export

def test_doctor_sees_the_state_rows_the_schema_cannot_check(conn):
    task = entries.create(conn, journal_date="2026-10-20", kind="task",
                          title="Renew passport", primary_category="chores")
    nudge_state.snooze(conn, f"due:{task['uuid']}", until="2026-10-30")
    nudge_state.dismiss(conn, "nutrient:vitamin_d")
    conn.commit()
    names = {c["check"]: c["status"] for c in run_checks(conn)["checks"]}
    assert names["nudge_states_keys"] == "ok" and names["nudge_states_until"] == "ok"
    assert names["nudge_states_closed_loops"] == "ok"
    assert names["reminder_revisions_dates"] == "ok"

    corrections.close_entry(conn, task["uuid"], "done")
    conn.execute("INSERT INTO nudge_states VALUES ('bogus:1','dismissed',NULL,NULL,"
                 "'user','x','x')")
    conn.execute("INSERT INTO nudge_states VALUES ('due:other','snoozed','soon',NULL,"
                 "'user','x','x')")
    conn.execute("INSERT INTO reminder_revisions VALUES ('u1', 0, 'fp', 'yesterday', 1, 'x')")
    conn.commit()
    result = run_checks(conn)
    status = {c["check"]: c for c in result["checks"]}
    assert status["nudge_states_keys"]["status"] == "fail"
    assert "bogus:1" in status["nudge_states_keys"]["detail"]
    assert status["nudge_states_until"]["status"] == "fail"
    assert status["nudge_states_closed_loops"]["status"] == "warn"
    assert task["uuid"] in status["nudge_states_closed_loops"]["detail"]
    assert status["reminder_revisions_dates"]["status"] == "fail"


def test_both_tables_are_in_the_jsonl_archive(conn, tmp_path):
    nudge_state.dismiss(conn, "stale:browser", note="never use it")
    rev.record_publication(conn, {"u1": "fp1"}, NOW)
    conn.commit()
    manifest = export_jsonl(conn, tmp_path / "archive")
    assert manifest["tables"]["nudge_states"] == 1
    assert manifest["tables"]["reminder_revisions"] == 1
    assert '"nudge_key": "stale:browser"' in (tmp_path / "archive" / "nudge_states.jsonl"
                                              ).read_text(encoding="utf-8")

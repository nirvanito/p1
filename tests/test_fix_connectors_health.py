"""Health Connect: sleep stored under the old day rule is re-dated, not a
conflict.

Sleep moved to the wake-day rule (a night belongs to the morning it ended,
with no 05:00 rollover). A night that ended at 04:30 and was imported before
that change sits on the previous day; its record has not changed at the
source, so its last-modified time is not newer, and newer-wins kept the old
date for good while reporting a CONFLICT on every later import.
"""

import json
import sqlite3
from datetime import datetime

from lifecrm import health_connect as hc

CDT = -18000


def _ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _sleep_db(path, *, minutes_asleep_end="2026-07-28T09:30:00Z",
              modified="2026-07-28T12:00:00Z") -> None:
    """One night, 22:00 -> 04:30 local (CDT): the wake day is 2026-07-28, the
    old rollover rule filed it on 2026-07-27."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
      CREATE TABLE application_info_table (
        row_id INTEGER PRIMARY KEY, package_name TEXT, app_name TEXT);
      CREATE TABLE sleep_session_record_table (
        row_id INTEGER PRIMARY KEY, uuid BLOB, app_info_id INTEGER,
        last_modified_time INTEGER, start_time INTEGER, end_time INTEGER,
        start_zone_offset INTEGER, end_zone_offset INTEGER, title TEXT, notes TEXT);
    """)
    conn.execute("INSERT INTO application_info_table VALUES (1, "
                 "'com.sec.android.app.shealth', 'Samsung Health')")
    conn.execute("INSERT INTO sleep_session_record_table VALUES "
                 "(1, ?, 1, ?, ?, ?, ?, ?, 'Night sleep', NULL)",
                 (bytes([5]) * 16, _ms(modified), _ms("2026-07-28T03:00:00Z"),
                  _ms(minutes_asleep_end), CDT, CDT))
    conn.commit()
    conn.close()


def _import_under_the_old_rule(conn, cfg, db) -> dict:
    """What an import made before the wake-day rule stored: the same record,
    dated with the rollover."""
    rec = dict(hc.read_export(db, cfg)["records"][0])
    rec["journal_date"] = hc._journal_date(_ms(rec["ended_at"]), rec["utc_offset_sec"],
                                           cfg, rollover=True)
    assert rec["journal_date"] == "2026-07-27"
    hc.apply_plan(conn, hc.build_plan(conn, [rec]))
    conn.commit()
    return rec


def test_a_night_stored_under_the_old_rule_is_re_dated_to_its_wake_day(cfg, conn, tmp_path):
    db = tmp_path / "hc.db"
    _sleep_db(db)
    _import_under_the_old_rule(conn, cfg, db)

    scan = hc.read_export(db, cfg)
    plan = hc.build_plan(conn, scan["records"])
    assert plan["conflicts"] == []
    assert len(plan["updates"]) == 1 and plan["redated"] == 1
    text = "\n".join(hc.report_lines(scan, plan))
    assert "CONFLICT" not in text
    assert "re-dated: 1 stored record(s)" in text
    assert hc.plan_summary(scan, plan)["redated"] == 1

    hc.apply_plan(conn, plan)
    conn.commit()
    row = conn.execute("SELECT journal_date FROM health_records").fetchone()
    assert row["journal_date"] == "2026-07-28"
    audit = conn.execute("SELECT details FROM audit_log WHERE action='update' "
                         "AND table_name='health_records'").fetchone()
    assert json.loads(audit["details"])["changes"] == {
        "journal_date": {"old": "2026-07-27", "new": "2026-07-28"}}

    again = hc.build_plan(conn, hc.read_export(db, cfg)["records"])
    assert again["identical"] == 1 and not again["updates"] and not again["conflicts"]


def test_a_changed_night_that_is_not_newer_is_still_a_conflict(cfg, conn, tmp_path):
    """Only a difference in the derived day is re-dated. The source's own data
    changing without a newer edit time (an old copy) is still refused."""
    db = tmp_path / "hc.db"
    _sleep_db(db)
    _import_under_the_old_rule(conn, cfg, db)

    older = tmp_path / "older.db"
    _sleep_db(older, minutes_asleep_end="2026-07-28T09:45:00Z",
              modified="2026-07-28T11:00:00Z")
    plan = hc.build_plan(conn, hc.read_export(older, cfg)["records"])
    assert plan["redated"] == 0 and not plan["updates"]
    assert len(plan["conflicts"]) == 1
    assert set(plan["conflicts"][0]["changes"]) >= {"ended_at", "journal_date"}

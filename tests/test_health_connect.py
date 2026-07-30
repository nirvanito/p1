"""Health Connect import: synthetic export fixture shaped exactly like the
AOSP HealthFitness storage schema (verified July 2026), zipped like the real
scheduled export. Covers unit conversions, per-record timezone journal dating,
uuid-BLOB identity, re-import dedupe, newer-wins updates, conflict refusal,
doctor freshness, and the CLI dry-run/apply flow."""

import json
import sqlite3
import zipfile
from datetime import datetime, timezone

import pytest
from conftest import cli_project

from lifecrm import db as db_mod
from lifecrm import health_connect as hc
from lifecrm.domain import health
from lifecrm.domain.util import DomainError, now_utc

CDT = -18000                       # zone offset (seconds) used by the fixture


def _ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _u(n: int) -> bytes:
    return bytes([n]) * 16


_BASE = ("uuid BLOB, app_info_id INTEGER, last_modified_time INTEGER, "
         "client_record_id TEXT, device_info_id INTEGER, recording_method INTEGER")
_INTERVAL = (_BASE + ", start_time INTEGER, end_time INTEGER, "
             "start_zone_offset INTEGER, end_zone_offset INTEGER")
_INSTANT = _BASE + ", time INTEGER, zone_offset INTEGER"


def make_hc_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(f"""
      CREATE TABLE application_info_table (
        row_id INTEGER PRIMARY KEY, package_name TEXT, app_name TEXT);
      CREATE TABLE steps_record_table (
        row_id INTEGER PRIMARY KEY, {_INTERVAL}, count INTEGER);
      CREATE TABLE weight_record_table (
        row_id INTEGER PRIMARY KEY, {_INSTANT}, weight REAL);
      CREATE TABLE sleep_session_record_table (
        row_id INTEGER PRIMARY KEY, {_INTERVAL}, title TEXT, notes TEXT);
      CREATE TABLE sleep_stages_table (
        row_id INTEGER PRIMARY KEY, parent_key INTEGER,
        stage_start_time INTEGER, stage_end_time INTEGER, stage_type INTEGER);
      CREATE TABLE heart_rate_record_table (
        row_id INTEGER PRIMARY KEY, {_INTERVAL});
      CREATE TABLE heart_rate_record_series_table (
        row_id INTEGER PRIMARY KEY, parent_key INTEGER,
        epoch_millis INTEGER, beats_per_minute INTEGER);
      CREATE TABLE exercise_session_record_table (
        row_id INTEGER PRIMARY KEY, {_INTERVAL},
        exercise_type INTEGER, title TEXT, notes TEXT, has_route INTEGER);
      CREATE TABLE nutrition_record_table (
        row_id INTEGER PRIMARY KEY, {_INTERVAL}, energy REAL, protein REAL,
        total_fat REAL, total_carbohydrate REAL, sugar REAL,
        meal_type INTEGER, meal_name TEXT);
      -- present but not mapped: must be REPORTED, never silently dropped
      CREATE TABLE speed_record_table (
        row_id INTEGER PRIMARY KEY, {_INTERVAL}, speed REAL);
      -- mapped metric whose expected value column is absent: skip WITH reason
      CREATE TABLE body_fat_record_table (
        row_id INTEGER PRIMARY KEY, {_INSTANT}, fat_pct REAL);
    """)
    lm = _ms("2026-07-28T12:00:00Z")
    conn.execute("INSERT INTO application_info_table VALUES (1, "
                 "'com.sec.android.app.shealth', 'Samsung Health')")

    def interval(table, row_id, u, start, end, extra_cols="", extra_vals=()):
        conn.execute(
            f"INSERT INTO {table} (row_id, uuid, app_info_id, last_modified_time, "
            f"start_time, end_time, start_zone_offset, end_zone_offset{extra_cols}) "
            f"VALUES (?, ?, 1, ?, ?, ?, ?, ?" + ", ?" * len(extra_vals) + ")",
            (row_id, u, lm, _ms(start), _ms(end), CDT, CDT, *extra_vals))

    interval("steps_record_table", 1, _u(1),
             "2026-07-28T13:00:00Z", "2026-07-28T14:00:00Z", ", count", (4000,))
    interval("steps_record_table", 2, _u(2),
             "2026-07-28T19:00:00Z", "2026-07-28T20:00:00Z", ", count", (4432,))
    interval("steps_record_table", 3, _u(3),
             "2026-07-27T19:00:00Z", "2026-07-27T20:00:00Z", ", count", (6000,))

    # 04:30 local (before the 05:00 rollover) -> belongs to the previous day
    conn.execute("INSERT INTO weight_record_table (row_id, uuid, app_info_id, "
                 "last_modified_time, time, zone_offset, weight) "
                 "VALUES (1, ?, 1, ?, ?, ?, 84500.0)",
                 (_u(4), lm, _ms("2026-07-28T09:30:00Z"), CDT))

    # sleep 22:00 -> 06:00 local; journal date = wake day
    interval("sleep_session_record_table", 1, _u(5),
             "2026-07-28T03:00:00Z", "2026-07-28T11:00:00Z",
             ", title, notes", ("Night sleep", None))
    for start, end, stype in (
            ("2026-07-28T03:00:00Z", "2026-07-28T03:30:00Z", 1),   # awake
            ("2026-07-28T03:30:00Z", "2026-07-28T07:30:00Z", 4),   # light
            ("2026-07-28T07:30:00Z", "2026-07-28T09:00:00Z", 5),   # deep
            ("2026-07-28T09:00:00Z", "2026-07-28T10:00:00Z", 6)):  # rem
        conn.execute("INSERT INTO sleep_stages_table (parent_key, "
                     "stage_start_time, stage_end_time, stage_type) "
                     "VALUES (1, ?, ?, ?)", (_ms(start), _ms(end), stype))

    interval("heart_rate_record_table", 1, _u(6),
             "2026-07-28T14:00:00Z", "2026-07-28T14:30:00Z")
    for t, bpm in (("2026-07-28T14:00:00Z", 60), ("2026-07-28T14:10:00Z", 70),
                   ("2026-07-28T14:20:00Z", 80)):
        conn.execute("INSERT INTO heart_rate_record_series_table "
                     "(parent_key, epoch_millis, beats_per_minute) "
                     "VALUES (1, ?, ?)", (_ms(t), bpm))
    # a heart-rate parent with no samples carries no value: skipped
    interval("heart_rate_record_table", 2, _u(7),
             "2026-07-28T15:00:00Z", "2026-07-28T15:30:00Z")

    interval("exercise_session_record_table", 1, _u(8),
             "2026-07-28T22:00:00Z", "2026-07-28T22:45:00Z",
             ", exercise_type, title, notes, has_route", (79, "Evening walk",
                                                          None, 0))
    interval("nutrition_record_table", 1, _u(9),
             "2026-07-28T17:00:00Z", "2026-07-28T17:20:00Z",
             ", energy, protein, total_fat, total_carbohydrate, sugar, "
             "meal_type, meal_name",
             (650000.0, 42.5, 20.0, 55.0, 12.0, 2, "Chicken bowl"))
    interval("speed_record_table", 1, _u(10),
             "2026-07-28T22:00:00Z", "2026-07-28T22:45:00Z", ", speed", (1.5,))
    interval("speed_record_table", 2, _u(11),
             "2026-07-27T22:00:00Z", "2026-07-27T22:45:00Z", ", speed", (1.6,))
    conn.commit()
    conn.close()


@pytest.fixture
def hc_zip(tmp_path):
    db = tmp_path / "healthconnect.db"
    make_hc_db(db)
    z = tmp_path / "health_connect_export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(db, "healthconnect.db")
    return z


def _by_metric(records):
    out = {}
    for r in records:
        out.setdefault(r["metric"], []).append(r)
    return out


# ---------------------------------------------------------------- reading

def test_read_export_maps_and_converts(cfg, tmp_path):
    db = tmp_path / "hc.db"
    make_hc_db(db)
    scan = hc.read_export(db, cfg)
    recs = _by_metric(scan["records"])

    assert len(scan["records"]) == 8
    assert sorted(recs) == ["exercise_session", "heart_rate", "nutrition",
                            "sleep_session", "steps", "weight"]

    w = recs["weight"][0]
    assert w["value"] == 84.5 and w["unit"] == "kg"           # grams -> kg
    assert w["journal_date"] == "2026-07-27"                  # 04:30 local
    assert w["ended_at"] is None
    assert w["external_id"] == ("04" * 16)
    assert w["origin_app"] == "com.sec.android.app.shealth"

    s = recs["sleep_session"][0]
    assert s["value"] == 390.0 and s["unit"] == "min"         # asleep stages only
    assert s["journal_date"] == "2026-07-28"                  # wake day
    detail = json.loads(s["value_json"])
    assert detail["stages"] == {"awake": 30.0, "light": 240.0,
                                "deep": 90.0, "rem": 60.0}
    assert detail["title"] == "Night sleep"

    h = recs["heart_rate"]
    assert len(h) == 1                                        # sampleless parent skipped
    assert h[0]["value"] == 70.0
    assert json.loads(h[0]["value_json"]) == {"min": 60, "max": 80, "samples": 3}

    n = recs["nutrition"][0]
    assert n["value"] == 650.0 and n["unit"] == "kcal"        # cal -> kcal
    nd = json.loads(n["value_json"])
    assert nd["protein_g"] == 42.5 and nd["meal"] == "lunch"
    assert nd["meal_name"] == "Chicken bowl"

    e = recs["exercise_session"][0]
    assert e["value"] == 45.0 and e["unit"] == "min"
    assert json.loads(e["value_json"])["exercise_type"] == 79

    assert {u["table"]: u["rows"] for u in scan["unmapped"]} == {
        "speed_record_table": 2}
    assert len(scan["skipped"]) == 1
    assert "body_fat" in scan["skipped"][0]
    assert "percentage" in scan["skipped"][0]


def test_journal_date_offset_and_fallback(cfg):
    ms = _ms("2026-07-28T09:30:00Z")
    assert hc._journal_date(ms, CDT, cfg) == "2026-07-27"     # 04:30 local
    assert hc._journal_date(ms, 0, cfg) == "2026-07-28"       # explicit UTC
    # no offset: falls back to the configured timezone (America/Chicago, -5h
    # in July -> 04:30 local, previous day)
    assert hc._journal_date(ms, None, cfg) == "2026-07-27"


# ---------------------------------------------------------------- apply / dedupe

def test_zip_apply_reimport_update_conflict(cfg, conn, hc_zip, tmp_path):
    scan = hc.read_export(hc_zip, cfg)
    assert scan["source_file"] == "health_connect_export.zip"
    plan = hc.build_plan(conn, scan["records"])
    assert len(plan["new"]) == 8 and plan["identical"] == 0

    summary = hc.apply_plan(conn, plan)
    conn.commit()
    assert summary["added"] == 8
    assert conn.execute("SELECT COUNT(*) c FROM health_records").fetchone()["c"] == 8
    assert conn.execute("SELECT COUNT(*) c FROM audit_log "
                        "WHERE table_name='health_records'").fetchone()["c"] == 8
    days = {r["journal_date"] for r in conn.execute("SELECT journal_date FROM days")}
    assert {"2026-07-27", "2026-07-28"} <= days

    # re-import of the same file: everything identical, nothing written
    plan2 = hc.build_plan(conn, hc.read_export(hc_zip, cfg)["records"])
    assert not plan2["new"] and not plan2["updates"] and plan2["identical"] == 8

    # source edits the weight record with a NEWER modification time: update wins
    db = tmp_path / "healthconnect.db"
    edit = sqlite3.connect(str(db))
    edit.execute("UPDATE weight_record_table SET weight=85000.0, "
                 "last_modified_time=last_modified_time + 3600000")
    edit.commit(); edit.close()
    with zipfile.ZipFile(hc_zip, "w") as zf:
        zf.write(db, "healthconnect.db")
    plan3 = hc.build_plan(conn, hc.read_export(hc_zip, cfg)["records"])
    assert len(plan3["updates"]) == 1 and not plan3["conflicts"]
    hc.apply_plan(conn, plan3)
    conn.commit()
    row = conn.execute("SELECT * FROM health_records WHERE metric='weight'").fetchone()
    assert row["value"] == 85.0
    audit = conn.execute("SELECT details FROM audit_log WHERE action='update' "
                         "AND table_name='health_records'").fetchone()
    assert "84.5" in audit["details"] and "85.0" in audit["details"]

    # a DIFFERENT value with an OLDER modification time: refused, surfaced
    edit = sqlite3.connect(str(db))
    edit.execute("UPDATE weight_record_table SET weight=86000.0, "
                 "last_modified_time=last_modified_time - 7200000")
    edit.commit(); edit.close()
    with zipfile.ZipFile(hc_zip, "w") as zf:
        zf.write(db, "healthconnect.db")
    plan4 = hc.build_plan(conn, hc.read_export(hc_zip, cfg)["records"])
    assert len(plan4["conflicts"]) == 1 and not plan4["updates"]
    hc.apply_plan(conn, plan4)
    conn.commit()
    row = conn.execute("SELECT * FROM health_records WHERE metric='weight'").fetchone()
    assert row["value"] == 85.0                               # kept existing


def test_domain_validation(conn):
    base = {"metric": "weight", "journal_date": "2026-07-27",
            "started_at": "2026-07-27T09:00:00Z", "value": 84.5, "unit": "kg",
            "external_id": "ff" * 16, "source_file": "x.zip"}
    with pytest.raises(DomainError, match="ends before it starts"):
        health.insert(conn, {**base, "ended_at": "2026-07-27T08:00:00Z"})
    with pytest.raises(DomainError, match="external_id"):
        health.insert(conn, {**base, "external_id": ""})
    with pytest.raises(DomainError, match="value or value_json"):
        health.insert(conn, {**base, "value": None})


def test_not_a_health_connect_db(cfg, tmp_path):
    other = tmp_path / "other.db"
    sqlite3.connect(str(other)).executescript(
        "CREATE TABLE misc (id INTEGER);").connection.close()
    with pytest.raises(DomainError, match="no \\*_record_table"):
        hc.read_export(other, cfg)


# ---------------------------------------------------------------- doctor

def test_doctor_health_freshness(conn, cfg):
    from lifecrm.doctor import run_checks

    def _status(report, name):
        for c in report["checks"]:
            if c["check"] == name:
                return c["status"]
        return None

    # no health rows at all: the check does not apply
    assert _status(run_checks(conn, cfg), "health_data_age") is None

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    health.insert(conn, {"metric": "weight", "journal_date": today,
                         "started_at": now_utc(), "value": 84.0, "unit": "kg",
                         "external_id": "aa" * 16, "source_file": "x.zip"})
    conn.commit()
    report = run_checks(conn, cfg)
    assert _status(report, "health_data_age") == "ok"
    assert report["healthy"]

    conn.execute("INSERT OR IGNORE INTO days (journal_date, created_at, "
                 "updated_at) VALUES ('2026-01-01', 'x', 'x')")
    conn.execute("UPDATE health_records SET journal_date='2026-01-01'")
    conn.commit()
    assert _status(run_checks(conn, cfg), "health_data_age") == "warn"


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_then_apply(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lifecrm.cli import app
    runner = CliRunner()

    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0

    db = tmp_path / "healthconnect.db"
    make_hc_db(db)
    z = tmp_path / "hc-export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(db, "healthconnect.db")

    r = runner.invoke(app, ["health", "import", str(z)])
    assert r.exit_code == 0, r.output
    assert "dry run — nothing written" in r.output
    assert "sleep_session: 1 new" in r.output
    assert "unmapped tables" in r.output and "speed_record_table" in r.output

    q = runner.invoke(app, ["sql", "SELECT COUNT(*) AS c FROM health_records",
                            "--json"])
    assert json.loads(q.output)[0]["c"] == 0                  # dry run wrote nothing

    r = runner.invoke(app, ["health", "import", str(z), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 8 added" in r.output
    q = runner.invoke(app, ["sql", "SELECT COUNT(*) AS c FROM health_records",
                            "--json"])
    assert json.loads(q.output)[0]["c"] == 8

    s = runner.invoke(app, ["status", "--json"])
    assert s.exit_code == 0, s.output
    latest = {h["metric"]: h for h in json.loads(s.output)["health_latest"]}
    assert latest["weight"]["latest"] == "2026-07-27"
    assert latest["steps"]["records"] == 3

    d = runner.invoke(app, ["doctor"])
    assert d.exit_code == 0, d.output

    out = tmp_path / "exp"
    e = runner.invoke(app, ["export", "--format", "jsonl", "--out", str(out)])
    assert e.exit_code == 0, e.output
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["tables"]["health_records"] == 8
    first = (out / "health_records.jsonl").read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first)["external_id"]                   # uuid hex survives export

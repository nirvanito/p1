"""Brief 2.5: two apps writing one metric. On the Samsung combo Samsung
Health and Google Fit can both count the same walk; on an iPhone the phone
and the watch both count it. Every day-total consumer (the steps and sleep
dashboards, `ask`) SUMs a day's records, so a second writer silently doubles
the day. The dry-run report — this pipeline's review gate — has to say so,
and doctor keeps saying so after the import."""

import sqlite3

from lifecrm import health_connect as hc
from lifecrm.doctor import run_checks
from test_health_connect import CDT, _ms, _u, make_hc_db

FIT = "com.google.android.apps.fitness"
SHEALTH = "com.sec.android.app.shealth"


def _add_second_writer(db, *, steps=True, weight=False):
    """Google Fit alongside the fixture's Samsung Health rows: the same
    morning walk on 2026-07-28 that Samsung Health already counted."""
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO application_info_table VALUES (2, ?, 'Fit')", (FIT,))
    lm = _ms("2026-07-28T12:00:00Z")
    if steps:
        conn.execute(
            "INSERT INTO steps_record_table (row_id, uuid, app_info_id, "
            "last_modified_time, start_time, end_time, start_zone_offset, "
            "end_zone_offset, count) VALUES (10, ?, 2, ?, ?, ?, ?, ?, 3950)",
            (_u(40), lm, _ms("2026-07-28T13:00:00Z"), _ms("2026-07-28T14:00:00Z"),
             CDT, CDT))
    if weight:
        conn.execute(
            "INSERT INTO weight_record_table (row_id, uuid, app_info_id, "
            "last_modified_time, time, zone_offset, weight) "
            "VALUES (10, ?, 2, ?, ?, ?, 84700.0)",
            (_u(41), lm, _ms("2026-07-28T14:30:00Z"), CDT))
    conn.commit()
    conn.close()


def _scan(cfg, tmp_path, **second):
    db = tmp_path / "hc.db"
    make_hc_db(db)
    if second:
        _add_second_writer(db, **second)
    return hc.read_export(db, cfg)


def _status(report, name):
    return next((c for c in report["checks"] if c["check"] == name), None)


def test_a_metric_written_by_two_apps_is_a_warning_in_the_dry_run(conn, cfg, tmp_path):
    scan = _scan(cfg, tmp_path, steps=True)
    lines = hc.report_lines(scan, hc.build_plan(conn, scan["records"]))
    warnings = [ln for ln in lines if "WARNING" in ln]
    assert warnings == [
        f"  WARNING steps: written by 2 apps ({FIT}, {SHEALTH}) — on 1 day(s) "
        f"more than one recorded it, so daily totals will double-count"]


def test_one_writer_per_metric_reports_nothing_extra(conn, cfg, tmp_path):
    scan = _scan(cfg, tmp_path)
    lines = hc.report_lines(scan, hc.build_plan(conn, scan["records"]))
    assert not [ln for ln in lines if "WARNING" in ln or "written by" in ln]
    assert hc.plan_summary(scan, hc.build_plan(conn, scan["records"]))[
        "origin_apps"]["steps"] == {"apps": [SHEALTH], "days_with_several": 0,
                                    "summed": True}


def test_an_averaged_metric_with_two_writers_is_a_note_not_a_warning(conn, cfg, tmp_path):
    scan = _scan(cfg, tmp_path, steps=False, weight=True)
    lines = hc.report_lines(scan, hc.build_plan(conn, scan["records"]))
    assert not [ln for ln in lines if "WARNING" in ln]
    assert f"  note weight: written by 2 apps ({FIT}, {SHEALTH}) — day figures " \
           f"mix both apps' readings" in lines


def test_plan_summary_carries_origin_apps(conn, cfg, tmp_path):
    scan = _scan(cfg, tmp_path, steps=True)
    summary = hc.plan_summary(scan, hc.build_plan(conn, scan["records"]))
    assert summary["origin_apps"]["steps"] == {
        "apps": [FIT, SHEALTH], "days_with_several": 1, "summed": True}
    assert summary["origin_apps"]["sleep_session"]["apps"] == [SHEALTH]


def test_doctor_keeps_warning_after_a_two_writer_import(conn, cfg, tmp_path):
    scan = _scan(cfg, tmp_path, steps=True)
    hc.apply_plan(conn, hc.build_plan(conn, scan["records"]))
    conn.commit()
    report = run_checks(conn, cfg)
    check = _status(report, "health_multi_origin")
    assert check["status"] == "warn"
    assert check["detail"].startswith(f"steps: {FIT}, {SHEALTH} on 1 day(s)")
    assert "docs/health-connect.md" in check["detail"]
    assert report["failures"] == 0


def test_doctor_is_quiet_for_a_single_writer_and_for_averaged_metrics(
        conn, cfg, tmp_path):
    scan = _scan(cfg, tmp_path, steps=False, weight=True)
    hc.apply_plan(conn, hc.build_plan(conn, scan["records"]))
    conn.commit()
    assert _status(run_checks(conn, cfg), "health_multi_origin")["status"] == "ok"


def test_doctor_says_nothing_about_origins_before_any_health_import(conn, cfg):
    assert _status(run_checks(conn, cfg), "health_multi_origin") is None

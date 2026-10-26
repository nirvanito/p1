"""`lifecrm collect` (schema v8): the imports the owner would run by hand, run
by themselves — routine plans applied, anything unusual held and recorded.

The load-bearing claims: a source's first import is never collected (the
owner runs it, with its doc's checklist); a removal observe would refuse is
held and removes nothing; a routine plan is written exactly as `observe
--apply` writes it (one shared write path, snapshot before removing); the
same file twice writes nothing, while a new file that only declares coverage
is applied (a quiet day is a known zero); one source failing never stops the
next; every outcome is a row in collect_runs, audited, that the nudges pack
cites as C#, doctor warns on, and the as-of date ignores.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import zipfile

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import SCHEMA_VERSION, collect, db as db_mod, nudges, obsidian, platforms
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.connectors import android_sms as an
from lifecrm.doctor import run_checks
from lifecrm.domain import collect as collect_dom
from lifecrm.domain import nudges as nudge_state
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError, new_uuid, now_utc
from lifecrm.export import export_jsonl
from test_android_sms import make_backup
from test_health_connect import CDT, _ms, _u, make_hc_db

runner = CliRunner()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """With no [collect] sources a run looks everywhere a connector looks:
    in a test that is a home of its own, never this machine's Downloads,
    OneDrive, browser profile or a plugged-in Kindle."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(platforms, "home", lambda: home)
    for var in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "HOME"):
        monkeypatch.setenv(var, str(home))
    for var in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
        monkeypatch.delenv(var, raising=False)
    from lifecrm.connectors import reading
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])
    return home


def _phone_backup(home, **kw):
    """An SMS backup where the app uploads it: OneDrive › Apps."""
    return make_backup(home / "OneDrive" / "Apps" / "SMS Backup & Restore", **kw)


def _sms_source(cfg, folder):
    cfg.connectors = {**(cfg.connectors or {}), "android_sms": {"folder": str(folder)}}
    cfg.extra = {**(cfg.extra or {}), "collect": {"sources": ["android_sms"]}}


def _owner_imports(conn, cfg, folder):
    """What the owner does once by hand: observe, read, --apply."""
    scan = an.scan(folder, cfg)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()


def _only(result):
    (r,) = result["sources"]
    return r


def _counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("imports", "observations", "collect_runs")}


# ---------------------------------------------------------------- schema

def test_schema_sql_carries_migration_0008_verbatim():
    def norm(text):
        return text.replace("\r\n", "\n")
    mig = norm((db_mod.MIGRATIONS_DIR / "0008_collect_runs.sql")
               .read_text(encoding="utf-8-sig"))
    body = mig.split("\n\n", 1)[1]
    assert body in norm(db_mod.SCHEMA_FILE.read_text(encoding="utf-8"))
    assert SCHEMA_VERSION == 8


def test_the_table_refuses_an_outcome_without_its_reason(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO collect_runs (run_uuid, source, outcome, started_at, "
                     "finished_at) VALUES ('r', 'browser', 'held', ?, ?)",
                     (now_utc(), now_utc()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO collect_runs (run_uuid, source, outcome, reason, "
                     "started_at, finished_at) VALUES ('r', 'browser', 'unchanged', 'x', "
                     "?, ?)", (now_utc(), now_utc()))


# ---------------------------------------------------------------- domain

def test_record_validates_and_audits_one_row(conn):
    now = now_utc()
    row = collect_dom.record(conn, run_uuid="run-1", source="browser", outcome="held",
                             hold="first_import", reason="first import\nfrom browser",
                             started_at=now, finished_at=now)
    assert row["reason"] == "first import from browser"          # one line, always
    audit = conn.execute("SELECT * FROM audit_log WHERE table_name='collect_runs'"
                         ).fetchall()
    assert len(audit) == 1 and audit[0]["action"] == "held"
    for kw, msg in ((dict(outcome="held", reason="x"), "names the rule"),
                    (dict(outcome="applied", hold="invalid", reason=None), "names the rule"),
                    (dict(outcome="missing"), "needs a one-line reason"),
                    (dict(outcome="unchanged", reason="x"), "carries no reason"),
                    (dict(outcome="skipped", reason="x"), "outcome must be one of")):
        with pytest.raises(DomainError, match=msg):
            collect_dom.record(conn, run_uuid="run-1", source="browser",
                               started_at=now, finished_at=now, **kw)


def test_a_collect_row_moves_the_refresh_stamp_but_not_the_as_of_date(conn, cfg):
    before_as_of = nudges.as_of_date(conn, cfg)
    stamp = obsidian.change_stamp(cfg)
    collect_dom.record(conn, run_uuid="run-1", source="browser", outcome="unchanged",
                       started_at="2030-01-01T12:00:00Z", finished_at="2030-01-01T12:00:00Z")
    conn.commit()
    assert nudges.as_of_date(conn, cfg) == before_as_of          # bookkeeping, not content
    assert obsidian.change_stamp(cfg) != stamp                   # but Home re-renders


# ---------------------------------------------------------------- settings

def test_settings_name_sources_and_refuse_what_cannot_be_collected(cfg):
    assert collect.settings(cfg) == {"sources": None, "health_dir": None}   # find them
    cfg.extra = {"collect": {"sources": ["health", "android_sms", "health"],
                             "health_dir": "exports/health"}}
    s = collect.settings(cfg)
    assert s["sources"] == ["health", "android_sms"]
    assert s["health_dir"] == cfg.root / "exports" / "health"      # relative: the root's
    for raw, msg in (({"sources": ["sms"]}, "unknown source 'sms'"),
                     ({"sources": ["ai_chat"]}, "needs the export's path every time"),
                     ({"sources": "android_sms"}, "must be a list"),
                     ({"health_dir": 3}, "health_dir must be a folder path"),
                     ({"source": ["browser"]}, "unknown setting")):
        cfg.extra = {"collect": raw}
        with pytest.raises(DomainError, match=msg):
            collect.settings(cfg)
    assert "health" in collect.collectable() and "ai_chat" not in collect.collectable()


# ---------------------------------------------------------------- one source, a week of mornings

def test_the_first_import_is_held_and_writes_nothing(conn, cfg, tmp_path):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["hold"]) == ("held", "first_import")
    assert r["reason"].startswith("new on this computer: texts and calls")
    assert "python -m lifecrm collect approve android_sms" in r["reason"]
    assert "python -m lifecrm collect decline android_sms" in r["reason"]
    assert re.search(r"[\d,]+ record\(s\) from \d{4}-\d\d-\d\d to \d{4}-\d\d-\d\d", r["reason"])
    assert "docs/android-texts.md" in r["reason"]
    assert r["counts"]["new"] > 0                       # what the owner's run will add
    assert _counts(conn) == {"imports": 0, "observations": 0, "collect_runs": 1}
    assert run_checks(conn, cfg)["failures"] == 0


def test_after_the_owners_import_routine_mornings_apply_and_repeat_nothing(
        conn, cfg, tmp_path):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    _owner_imports(conn, cfg, folder)
    before = _counts(conn)

    r = _only(collect.run(conn, cfg))                   # the same backup again
    assert r["outcome"] == "unchanged" and r["reason"] is None
    assert _counts(conn)["imports"] == before["imports"]

    # the next night's backup: a text was deleted on the phone
    shutil.rmtree(folder)
    make_backup(folder, variant="deleted", stamp="20260921020003")
    r = _only(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"]["removed"] == 1
    assert r["snapshot"] and (cfg.backups_dir / r["counts"]["snapshot"]).exists()
    imp = conn.execute("SELECT * FROM imports WHERE uuid=?", (r["import_uuid"],)).fetchone()
    assert imp["status"] == "applied"
    row = collect_dom.latest(conn)["android_sms"]
    assert row["import_uuid"] == r["import_uuid"] and row["counts"]["removed"] == 1
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_new_backup_with_no_new_texts_still_declares_the_quiet_day(conn, cfg, tmp_path):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    _owner_imports(conn, cfg, folder)
    known = obs.coverage_days(conn, ("android_sms",), "2026-09-01", "2026-09-30")
    sms = next(folder.glob("sms-*.xml"))
    text = sms.read_text(encoding="utf-8")
    later = _ms("2026-09-25T17:00:03Z")          # five days on, not one new text
    sms.write_text(re.sub(r'backup_date="\d+"', f'backup_date="{later}"', text),
                   encoding="utf-8")
    r = _only(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"].get("coverage")
    assert not r["counts"].get("added")
    now_known = obs.coverage_days(conn, ("android_sms",), "2026-09-01", "2026-09-30")
    assert max(now_known) == "2026-09-25" > max(known)


def test_a_mass_removal_is_held_and_removes_nothing(conn, cfg, tmp_path):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    _owner_imports(conn, cfg, folder)
    active = conn.execute("SELECT COUNT(*) FROM observations WHERE status='active'"
                          ).fetchone()[0]
    shutil.rmtree(folder)
    make_backup(folder, variant="gutted", stamp="20260921020003")
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["hold"]) == ("held", "mass_removal")
    assert "would remove" in r["reason"]
    assert "python -m lifecrm collect approve android_sms" in r["reason"]
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE status='active'"
                        ).fetchone()[0] == active
    assert list(cfg.backups_dir.glob("pre-removal-*.db")) == []


def test_a_new_account_starts_over_with_its_own_first_import(conn, cfg, tmp_path):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    _owner_imports(conn, cfg, folder)
    cfg.connectors["android_sms"]["account"] = "+15125559999"     # a second phone
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["hold"]) == ("held", "first_import")
    assert "for +15125559999" in r["reason"]


def test_invalid_records_are_held(conn, cfg, tmp_path, monkeypatch):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    _owner_imports(conn, cfg, folder)
    real = an.scan

    def broken(target, cfg_):
        scan = real(target, cfg_)
        scan["records"][0]["started_at"] = ""
        return scan
    monkeypatch.setattr(an, "scan", broken)
    shutil.rmtree(folder)
    make_backup(folder, variant="deleted", stamp="20260921020003")
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["hold"]) == ("held", "invalid")
    assert "cannot validate" in r["reason"]
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 1


def test_missing_and_failing_sources_are_recorded_and_the_next_still_runs(
        conn, cfg, tmp_path, monkeypatch):
    cfg.connectors = {"android_sms": {"folder": str(tmp_path / "nowhere")}}
    empty = tmp_path / "downloads"
    empty.mkdir()
    from lifecrm import health_import
    monkeypatch.setattr(health_import, "search_dirs", lambda: [empty])
    result = collect.run(conn, cfg, ["android_sms", "health"])
    outcomes = {r["source"]: r["outcome"] for r in result["sources"]}
    assert outcomes == {"android_sms": "missing", "health": "missing"}
    assert "does not exist" in result["sources"][0]["reason"]

    folder = tmp_path / "sms"
    folder.mkdir()
    (folder / "sms-20260920020003.xml").write_text("<smses count='1'><sms", encoding="utf-8")
    cfg.connectors = {"android_sms": {"folder": str(folder)}}
    result = collect.run(conn, cfg, ["android_sms", "health"])
    first, second = result["sources"]
    assert first["outcome"] == "error" and second["outcome"] == "missing"
    assert conn.execute("SELECT COUNT(*) FROM collect_runs").fetchone()[0] == 4
    assert run_checks(conn, cfg)["failures"] == 0


def test_a_pending_write_is_refused_never_a_hang(conn, cfg):
    """The backup API cannot copy a database its own connection is writing;
    Python retries SQLITE_LOCKED forever. Both doors say so instead."""
    now = now_utc()
    collect_dom.record(conn, run_uuid="r", source="browser", outcome="unchanged",
                       started_at=now, finished_at=now)          # left uncommitted
    with pytest.raises(DomainError, match="no uncommitted writes"):
        collect.run(conn, cfg, ["browser"])
    plan = {"removed": [object()], "mirror_scope": 100}
    with pytest.raises(DomainError, match="uncommitted writes"):
        collect.apply_observed(conn, cfg, {}, plan)
    assert list(cfg.backups_dir.glob("pre-removal-*.db")) == []
    conn.rollback()


def test_a_dry_run_decides_and_writes_nothing(conn, cfg, tmp_path):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    _owner_imports(conn, cfg, folder)
    shutil.rmtree(folder)
    make_backup(folder, variant="deleted", stamp="20260921020003")
    before = _counts(conn)
    r = _only(collect.run(conn, cfg, dry_run=True))
    assert r["outcome"] == "applied" and r["dry_run"] and r["counts"]["removed"] == 1
    assert _counts(conn) == before


# ---------------------------------------------------------------- health

def _hc_zip(folder, *, extra_steps=False):
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "healthconnect.db"
    db.unlink(missing_ok=True)
    make_hc_db(db)
    if extra_steps:
        c = sqlite3.connect(str(db))
        c.execute("INSERT INTO steps_record_table (row_id, uuid, app_info_id, "
                  "last_modified_time, start_time, end_time, start_zone_offset, "
                  "end_zone_offset, count) VALUES (99, ?, 1, ?, ?, ?, ?, ?, 1234)",
                  (_u(42), _ms("2026-07-29T12:00:00Z"), _ms("2026-07-29T13:00:00Z"),
                   _ms("2026-07-29T14:00:00Z"), CDT, CDT))
        c.commit()
        c.close()
    z = folder / "Health Connect.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(db, "healthconnect.db")
    db.unlink()
    return z


def test_health_is_held_once_then_collected(conn, cfg, tmp_path, monkeypatch):
    from lifecrm import health_connect as hc
    from lifecrm import health_import
    onedrive = tmp_path / "OneDrive"
    drop = onedrive / "Health Connect"        # a subfolder: never searched by default
    z = _hc_zip(drop)
    monkeypatch.setattr(health_import, "search_dirs", lambda: [onedrive])
    r = _only(collect.run(conn, cfg, ["health"]))
    assert r["outcome"] == "missing" and "[collect] health_dir" in r["reason"]
    cfg.extra = {"collect": {"health_dir": str(drop)}}
    r = _only(collect.run(conn, cfg, ["health"]))
    assert (r["outcome"], r["hold"]) == ("held", "first_import")
    assert "health data (Health Connect)" in r["reason"]
    assert "collect approve health" in r["reason"] and "docs/health-connect.md" in r["reason"]
    assert conn.execute("SELECT COUNT(*) FROM health_records").fetchone()[0] == 0

    hc.apply_plan(conn, hc.build_plan(conn, hc.read_export(z, cfg)["records"]))
    conn.commit()
    assert _only(collect.run(conn, cfg, ["health"]))["outcome"] == "unchanged"

    _hc_zip(drop, extra_steps=True)
    r = _only(collect.run(conn, cfg, ["health"]))
    assert r["outcome"] == "applied" and r["counts"]["added"] == 1
    assert run_checks(conn, cfg)["failures"] == 0


# ---------------------------------------------------------------- what the owner sees

def test_a_held_source_is_a_cited_nudge_until_a_run_is_routine(conn, cfg, tmp_path):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    _owner_imports(conn, cfg, folder)
    shutil.rmtree(folder)
    make_backup(folder, variant="gutted", stamp="20260921020003")
    collect.run(conn, cfg)

    pack = nudges.build(conn, cfg, today="2026-09-21")
    (n,) = [x for x in pack["nudges"] if x["kind"] == "collect"]
    assert n["key"] == "collect:android_sms:mass_removal"
    assert n["title"] == "android_sms: the daily import held a large removal for your yes"
    (cid,) = n["ids"]
    assert cid.startswith("C") and pack["index"][cid]["type"] == "collect"
    removed, scope = re.search(r"remove (\d+) of (\d+)", n["detail"]).groups()
    ok = nudges.check(f"The daily import held back removing {removed} of {scope} "
                      f"texts [{cid}].", pack)
    assert ok["unknown"] == [] and ok["unsupported"] == []
    bad = nudges.check(f"It held back removing {int(removed) + 40} texts [{cid}].", pack)
    assert bad["unsupported"]
    assert any(line.startswith(f"  {cid}: collect run ") for line in nudges.render_lines(pack))
    today = obsidian._today_section({"ents": {}}, pack)
    assert any("*(the daily import)*" in line and n["key"] in line for line in today)

    # snoozable by its durable key
    nudge_state.snooze(conn, n["key"], until="2026-09-30")
    conn.commit()
    assert not [x for x in nudges.build(conn, cfg, today="2026-09-21")["nudges"]
                if x["kind"] == "collect"]
    nudge_state.restore(conn, n["key"])
    conn.commit()

    # the phone's next backup is whole again: the nudge goes by itself
    shutil.rmtree(folder)
    make_backup(folder, stamp="20260922020003")
    assert _only(collect.run(conn, cfg))["outcome"] == "unchanged"
    assert not [x for x in nudges.build(conn, cfg, today="2026-09-22")["nudges"]
                if x["kind"] == "collect"]


def _doctor(conn, cfg):
    report = run_checks(conn, cfg)
    conn.rollback()           # doctor's FTS integrity probe leaves a transaction open
    return {c["check"]: c for c in report["checks"]}


def test_doctor_warns_on_waiting_sources_and_a_job_that_never_ran(conn, cfg, tmp_path):
    folder = make_backup(tmp_path / "sms")
    _sms_source(cfg, folder)
    status = _doctor(conn, cfg)
    assert status["collect_last_run"]["status"] == "warn"
    assert "never run" in status["collect_last_run"]["detail"]
    assert "install-tasks" in status["collect_last_run"]["detail"]

    collect.run(conn, cfg)
    status = _doctor(conn, cfg)
    assert status["collect_last_run"]["status"] == "ok"
    assert status["collect_waiting"]["status"] == "warn"
    assert "android_sms (first_import)" in status["collect_waiting"]["detail"]

    _owner_imports(conn, cfg, folder)
    collect.run(conn, cfg)
    assert _doctor(conn, cfg)["collect_waiting"]["status"] == "ok"


def test_doctor_fails_a_run_naming_an_import_that_does_not_exist(conn, cfg):
    now = now_utc()
    collect_dom.record(conn, run_uuid=new_uuid(), source="browser", outcome="applied",
                       import_uuid="no-such-import", started_at=now, finished_at=now)
    conn.commit()
    status = {c["check"]: c for c in run_checks(conn)["checks"]}
    assert status["collect_runs_imports"]["status"] == "fail"


def test_the_archive_carries_collect_runs(conn, cfg, tmp_path):
    _sms_source(cfg, make_backup(tmp_path / "sms"))
    collect.run(conn, cfg)
    manifest = export_jsonl(conn, tmp_path / "archive")
    assert manifest["tables"]["collect_runs"] == 1
    text = (tmp_path / "archive" / "collect_runs.jsonl").read_text(encoding="utf-8")
    assert '"hold": "first_import"' in text and '"run_uuid"' in text


# ---------------------------------------------------------------- CLI

def _project(tmp_path, monkeypatch, extra: str = ""):
    root = cli_project(tmp_path, monkeypatch)
    if extra:
        with (root / "config.toml").open("a", encoding="utf-8") as f:
            f.write(extra)
    assert runner.invoke(app, ["init"]).exit_code == 0
    return load_config(root)


def test_cli_on_a_computer_with_nothing_yet(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch)
    for args in (["collect", "--if-configured"], ["collect"]):
        r = runner.invoke(app, args)
        assert r.exit_code == 0, r.output
        assert "nothing on this computer to collect yet" in r.output
        assert "collect setup" in r.output
    r = runner.invoke(app, ["collect", "run", "sms"])
    assert r.exit_code == 1 and "unknown source 'sms'" in r.output


def test_cli_with_an_empty_sources_list(tmp_path, monkeypatch):
    _project(tmp_path, monkeypatch, "[collect]\nsources = []\n")
    r = runner.invoke(app, ["collect", "--if-configured"])
    assert r.exit_code == 0 and "nothing collected" in r.output
    r = runner.invoke(app, ["collect"])
    assert r.exit_code == 1 and "empty list" in r.output


def test_cli_collects_reports_and_shows_status(tmp_path, monkeypatch):
    folder = make_backup(tmp_path / "sms")
    cfg = _project(tmp_path, monkeypatch,
                   f"[connectors.android_sms]\nfolder = '{folder}'\n"
                   '[collect]\nsources = ["android_sms"]\n')
    r = runner.invoke(app, ["collect", "--if-configured"])
    assert r.exit_code == 0, r.output
    assert "android_sms     held" in r.output
    assert "waiting for your yes: android_sms" in r.output

    r = runner.invoke(app, ["observe", "android_sms", "--apply"])     # the owner's own run
    assert r.exit_code == 0, r.output
    shutil.rmtree(folder)
    make_backup(folder, variant="deleted", stamp="20260921020003")
    r = runner.invoke(app, ["collect", "--dry-run"])
    assert r.exit_code == 0 and "would apply" in r.output and "1 removed" in r.output
    r = runner.invoke(app, ["collect"])
    assert r.exit_code == 0 and "applied" in r.output and "snapshot before removing" in r.output

    r = runner.invoke(app, ["collect", "--status"])
    assert r.exit_code == 0, r.output
    assert "1 source(s) in [collect] sources" in r.output and "android_sms" in r.output
    r = runner.invoke(app, ["collect", "--status", "--json"])
    import json
    report = json.loads(r.output)
    assert report["sources"][0]["latest"]["outcome"] == "applied"

    conn = db_mod.connect(cfg.db_path)
    try:
        assert run_checks(conn, cfg)["failures"] == 0
    finally:
        conn.close()


def test_cli_exits_1_when_a_source_failed(tmp_path, monkeypatch):
    folder = tmp_path / "sms"
    folder.mkdir()
    (folder / "sms-20260920020003.xml").write_text("<smses><sms", encoding="utf-8")
    _project(tmp_path, monkeypatch,
             f"[connectors.android_sms]\nfolder = '{folder}'\n"
             '[collect]\nsources = ["android_sms"]\n')
    r = runner.invoke(app, ["collect"])
    assert r.exit_code == 1 and "android_sms     error" in r.output


# ---------------------------------------------------------------- no config: found, asked, answered

def test_with_no_config_a_source_on_this_computer_is_found_and_asked_about(conn, cfg, home):
    _phone_backup(home)
    result = collect.run(conn, cfg)
    assert result["auto"]
    by = {r["source"]: r for r in result["sources"]}
    assert (by["android_sms"]["outcome"], by["android_sms"]["hold"]) == ("held", "first_import")
    assert by["browser"]["outcome"] == "skipped" and by["browser"]["skip"] == "not found"
    assert set(collect.collectable()) == set(by)
    # only the source it found leaves a row; the rest are not questions
    assert _counts(conn) == {"imports": 0, "observations": 0, "collect_runs": 1}

    pack = nudges.build(conn, cfg, today="2026-09-21")
    (n,) = [x for x in pack["nudges"] if x["kind"] == "collect"]
    assert n["key"] == "collect:android_sms:first_import"
    assert n["title"] == ("New on this computer: Texts and calls (SMS Backup & Restore). "
                          "Import it?")
    assert "collect approve android_sms" in n["detail"]
    # /morning's question copies the detail's numbers, and the check holds it to them
    count, first, last = re.search(r"([\d,]+) record\(s\) from (\S+) to (\S+),",
                                   n["detail"]).groups()
    asked = f"Import your texts and calls — {count} records, {first} to {last}? [{n['id']}]"
    assert nudges.check(asked, pack)["unsupported"] == []
    wrong = f"Import your texts and calls — {int(count.replace(',', '')) + 9} records? [{n['id']}]"
    assert nudges.check(wrong, pack)["unsupported"]


def test_yes_shows_the_dry_run_then_imports_and_the_job_takes_over(conn, cfg, home):
    _phone_backup(home)
    collect.run(conn, cfg)
    preview = collect.approve(conn, cfg, "android_sms")
    assert not preview["applied"] and preview["first_import"]
    assert preview["counts"]["new"] > 0 and preview["span"]
    assert preview["report"] and preview["doc"] == "docs/android-texts.md"
    assert _counts(conn)["imports"] == 0                          # a preview writes nothing

    done = collect.approve(conn, cfg, "android_sms", apply=True)
    assert done["applied"] and done["import_uuid"] and done["applied_counts"]["added"] > 0
    row = collect_dom.latest(conn)["android_sms"]
    assert (row["outcome"], row["import_uuid"]) == ("applied", done["import_uuid"])
    audit = conn.execute("SELECT actor FROM audit_log WHERE table_name='collect_runs' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    assert audit["actor"] == "user"
    assert not [x for x in nudges.build(conn, cfg, today="2026-09-21")["nudges"]
                if x["kind"] == "collect"]
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["android_sms"]["outcome"] == "unchanged"
    assert collect.approve(conn, cfg, "android_sms")["nothing_new"]
    assert run_checks(conn, cfg)["failures"] == 0


def test_yes_to_a_held_removal_imports_it_with_a_snapshot(conn, cfg, home):
    folder = _phone_backup(home)
    collect.approve(conn, cfg, "android_sms", apply=True)
    shutil.rmtree(folder)
    _phone_backup(home, variant="gutted", stamp="20260921020003")
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["android_sms"]["hold"] == "mass_removal"
    preview = collect.approve(conn, cfg, "android_sms")
    assert "would remove" in preview["mass_removal"]
    done = collect.approve(conn, cfg, "android_sms", apply=True)
    assert done["applied_counts"]["removed"] > 1 and done["snapshot"]
    assert list(cfg.backups_dir.glob("pre-removal-*.db"))


def test_no_means_never_until_yes(conn, cfg, home):
    _phone_backup(home)
    collect.run(conn, cfg)
    assert collect.decline(conn, "android_sms")["key"] == "collect:android_sms:declined"
    assert collect.declined(conn) == {"android_sms"}
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert (by["android_sms"]["outcome"], by["android_sms"]["skip"]) == ("skipped", "declined")
    assert _counts(conn)["collect_runs"] == 1                     # not looked at again
    assert not [x for x in nudges.build(conn, cfg, today="2026-09-21")["nudges"]
                if x["kind"] == "collect"]
    collect.approve(conn, cfg, "android_sms", apply=True)         # yes takes it back
    assert collect.declined(conn) == set() and nudge_state.states(conn) == []


def test_dismissing_the_question_in_morning_is_a_no(conn, cfg, home):
    _phone_backup(home)
    collect.run(conn, cfg)
    nudge_state.dismiss(conn, "collect:android_sms:first_import")
    conn.commit()
    assert collect.declined(conn) == {"android_sms"}


def test_a_found_source_nobody_can_read_is_not_offered(conn, cfg, home):
    folder = home / "OneDrive" / "Apps" / "SMS Backup & Restore"
    folder.mkdir(parents=True)
    (folder / "sms-20260920020003.xml").write_text("<smses><sms", encoding="utf-8")
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert (by["android_sms"]["outcome"], by["android_sms"]["skip"]) == ("skipped", "unreadable")
    assert _counts(conn)["collect_runs"] == 0


def test_yes_is_refused_for_records_the_reader_cannot_validate(conn, cfg, home, monkeypatch):
    _phone_backup(home)
    real = an.scan

    def broken(target, cfg_):
        scan = real(target, cfg_)
        scan["records"][0]["started_at"] = ""
        return scan
    monkeypatch.setattr(an, "scan", broken)
    with pytest.raises(DomainError, match="needs a fix to the reader, not a yes"):
        collect.approve(conn, cfg, "android_sms", apply=True)
    assert _counts(conn)["imports"] == 0


def test_health_one_folder_down_in_onedrive_is_found(conn, cfg, home):
    _hc_zip(home / "OneDrive" / "Health Connect")
    r = _only(collect.run(conn, cfg, ["health"]))
    assert (r["outcome"], r["hold"]) == ("held", "first_import")
    assert r["target"].endswith("Health Connect.zip")


# ---------------------------------------------------------------- macOS privacy (Full Disk Access)

def _refused(path):
    """What a Mac connector raises when TCC refuses the read: its own one-line
    Full Disk Access error, caused by the PermissionError (ios_messages.privacy_guard)."""
    from lifecrm.connectors import ios_messages
    try:
        raise PermissionError(1, "Operation not permitted", str(path))
    except PermissionError as e:
        return ios_messages.access_denied(path, e.strerror)


def test_a_source_macos_refuses_is_said_never_skipped(conn, cfg, home, monkeypatch):
    from lifecrm.connectors import mac_messages, notes
    chat = home / "Library" / "Messages" / "chat.db"

    def no_access(cfg_):
        raise _refused(chat) from PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(mac_messages, "default_target", no_access)
    monkeypatch.setattr(notes, "default_target", no_access)
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    for name in ("mac_messages", "notes"):
        assert by[name]["outcome"] == "error" and by[name]["blocked"], by[name]
        assert "Full Disk Access" in by[name]["reason"]
    assert by["browser"]["outcome"] == "skipped"            # really absent: still quiet
    assert collect_dom.latest(conn)["mac_messages"]["outcome"] == "error"

    pack = nudges.build(conn, cfg, today="2026-09-21")
    found = [x for x in pack["nudges"] if x["kind"] == "collect"]
    assert [x["key"] for x in found] == ["collect:macos:blocked"]   # one thing to do
    (n,) = found
    assert n["title"] == ("macOS is keeping the daily import out of Messages on this "
                          "Mac, notes. Give Full Disk Access?")
    assert "/bin/bash" in n["detail"] and len(n["ids"]) == 2

    items = {it["source"]: it for it in
             collect.setup_checklist(conn, cfg, "iphone-mac")["items"]}
    assert items["mac_messages"]["state"] == "blocked"
    assert any("Full Disk Access" in s for s in items["mac_messages"]["steps"])
    with pytest.raises(DomainError, match="Full Disk Access"):
        collect.approve(conn, cfg, "mac_messages")


def test_a_refusal_while_reading_a_new_source_is_said_too(conn, cfg, home, monkeypatch):
    _phone_backup(home)

    def no_access(target, cfg_):
        raise PermissionError(1, "Operation not permitted", str(target))
    monkeypatch.setattr(an, "scan", no_access)
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert by["android_sms"]["outcome"] == "error" and by["android_sms"]["blocked"]


def test_cli_exits_1_while_macos_blocks_a_source(tmp_path, monkeypatch, home):
    from lifecrm.connectors import mac_messages
    chat = home / "Library" / "Messages" / "chat.db"

    def no_access(cfg_):
        raise _refused(chat) from PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(mac_messages, "default_target", no_access)
    _project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["collect"])
    assert r.exit_code == 1 and "mac_messages    error" in r.output
    r = runner.invoke(app, ["collect", "setup", "--phone", "iphone-mac"])
    assert r.exit_code == 0 and "[  !  ] Messages" in r.output and "/bin/bash" in r.output


# ---------------------------------------------------------------- the phone's side

def test_the_setup_checklist_says_what_is_left_and_how(conn, cfg, home):
    data = collect.setup_checklist(conn, cfg, "android")
    assert data["title"] == "Windows + Android"
    states = {it["source"]: it["state"] for it in data["items"]}
    assert states == {"android_sms": "to do", "health": "to do", "photos": "to do",
                      "browser": "to do", "gps_track": "to do"}
    sms = next(it for it in data["items"] if it["source"] == "android_sms")
    assert any("SMS Backup & Restore" in s for s in sms["steps"])

    _phone_backup(home)
    assert {it["source"]: it["state"] for it in
            collect.setup_checklist(conn, cfg, "android")["items"]}["android_sms"] == "waiting"
    collect.approve(conn, cfg, "android_sms", apply=True)
    collect.decline(conn, "photos")
    items = {it["source"]: it for it in collect.setup_checklist(conn, cfg, "android")["items"]}
    assert items["android_sms"]["state"] == "collecting" and not items["android_sms"]["steps"]
    assert items["photos"]["state"] == "declined"
    with pytest.raises(DomainError, match="phone must be one of"):
        collect.setup_checklist(conn, cfg, "nokia")


def test_the_phone_is_guessed_from_this_computer(home, monkeypatch):
    monkeypatch.setattr(platforms, "is_mac", lambda: True)
    assert collect.guess_phone() == "iphone-mac"
    monkeypatch.setattr(platforms, "is_mac", lambda: False)
    monkeypatch.setattr(platforms, "is_windows", lambda: True)
    assert collect.guess_phone() == "android"
    (home / "Apple" / "MobileSync" / "Backup").mkdir(parents=True)
    assert collect.guess_phone() == "iphone-windows"


def test_a_preview_names_a_checklist_only_where_the_doc_has_one():
    assert collect.has_checklist("docs/android-texts.md")
    assert not collect.has_checklist("docs/browser.md")
    assert not collect.has_checklist("docs/no-such-doc.md")


def test_no_answer_at_the_prompt_is_a_no_not_an_abort(monkeypatch):
    import typer
    from lifecrm.commands import collect as cmd

    def closed(question):
        raise typer.Abort()
    monkeypatch.setattr(typer, "confirm", closed)
    assert cmd._asked("Import this?") is False


def test_cli_approve_decline_and_setup(tmp_path, monkeypatch, home):
    _phone_backup(home)
    _project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["collect"])
    assert r.exit_code == 0, r.output
    assert "android_sms     held        new on this computer" in r.output
    assert "not on this computer: health" in r.output

    r = runner.invoke(app, ["collect", "approve", "android_sms"])       # not a terminal
    assert r.exit_code == 0, r.output
    assert "texts and calls (SMS Backup & Restore): " in r.output and "new record(s)" in r.output
    assert "docs/android-texts.md (its first-real-import checklist)" in r.output
    assert "collect approve android_sms --yes" in r.output
    r = runner.invoke(app, ["collect", "approve", "android_sms", "--yes"])
    assert r.exit_code == 0, r.output
    assert "imported texts and calls" in r.output and "imports retract" in r.output

    r = runner.invoke(app, ["collect", "decline", "photos"])
    assert r.exit_code == 0 and "declined: photos" in r.output
    r = runner.invoke(app, ["collect", "setup", "--phone", "android"])
    assert r.exit_code == 0, r.output
    assert "Phone setup — Windows + Android" in r.output
    assert "[done ] Texts and calls" in r.output and "[  no ] Photo dates" in r.output
    assert "1. Samsung Health" in r.output
    r = runner.invoke(app, ["collect", "status"])
    assert r.exit_code == 0 and "every source on this computer" in r.output
    r = runner.invoke(app, ["collect", "run", "android_sms", "--json"])
    import json
    assert json.loads(r.output)["sources"][0]["outcome"] == "unchanged"

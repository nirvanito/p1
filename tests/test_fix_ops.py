"""Operations fixes: the weekly review slot, doctor and the review packs, a
second `dashboard`, `where` and the SMS app's upload folder, and the demo's
target guard and default anchor.

Each test here failed before its fix. The script-level ones (run through
bash and powershell.exe) are in test_fix_ops_scripts.py.
"""

import json
import os
import re
import sqlite3
import time
import tomllib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import PROJECT_ROOT, cli_project
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.config import Config
from lifecrm.connectors import android_sms
from lifecrm.demo import build as demo_build
from lifecrm.demo.build import DemoBuilder, DemoError, check_target
from lifecrm.domain.days import journal_date_for
from lifecrm.review import resolve_period

runner = CliRunner()
SCRIPTS = PROJECT_ROOT / "scripts"
DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _bare_config() -> Config:
    """Chicago with the 05:00 rollover, and no connector settings — never
    this machine's own config.toml."""
    return Config(root=Path("."), data_dir=Path("data"), backups_dir=Path("backups"),
                  inbox_dir=None, inbox_text_dir=None, archive_dir=None,
                  onedrive_backups_dir=None, timezone="America/Chicago",
                  day_rollover_hour=5)


# ---------------------------------------------------------------- the review slot

def _windows_review_slot() -> tuple[int, int, int]:
    """(launchd weekday 0=Sunday, hour, minute) of the Task Scheduler job."""
    text = (SCRIPTS / "install-tasks.ps1").read_text(encoding="utf-8")
    m = re.search(r'"LifeCRM review" "review\.ps1" @\(\s*\(New-ScheduledTaskTrigger '
                  r'-Weekly -DaysOfWeek (\w+) -At (\d\d):(\d\d)\)', text)
    assert m, "install-tasks.ps1 no longer registers the review job as expected"
    return DAYS.index(m.group(1)), int(m.group(2)), int(m.group(3))


def _mac_review_slot() -> tuple[int, int, int]:
    text = (SCRIPTS / "install-tasks.sh").read_text(encoding="utf-8")
    m = re.search(r"review\) script=review\.sh; hour=(\d+); minute=(\d+); "
                  r"weekday=(\d)", text)
    assert m, "install-tasks.sh no longer schedules the review job as expected"
    return int(m.group(3)), int(m.group(1)), int(m.group(2))


def test_both_installers_run_the_review_job_in_the_same_slot():
    assert _windows_review_slot() == _mac_review_slot()


def test_the_review_job_builds_the_week_that_has_just_ended():
    """The job runs `review --write-only` with the default period, the last
    full Mon-Sun week. Run in the scheduled slot, that has to be the week
    ending the day before — complete, and the one /review-week builds by
    default for the next seven days. Sunday 19:00 got the week before."""
    weekday, hour, minute = _windows_review_slot()
    cfg = _bare_config()
    tz = ZoneInfo(cfg.timezone)
    for week_monday in (date(2026, 10, 19), date(2026, 3, 2), date(2026, 12, 28)):
        # the slot's first run once that week's Sunday has begun
        sunday = week_monday + timedelta(days=6)
        sunday_start = datetime(sunday.year, sunday.month, sunday.day, 5, tzinfo=tz)
        run = next(dt for dt in (
            datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)
            for d in (sunday + timedelta(days=i) for i in range(8)))
            if (dt.weekday() + 1) % 7 == weekday and dt > sunday_start)
        today = date.fromisoformat(journal_date_for(
            run.astimezone(timezone.utc), cfg.timezone, cfg.day_rollover_hour))
        period = resolve_period(cfg, today=today)
        assert period["start"] == week_monday.isoformat(), (run, period)
        assert period["end"] == sunday.isoformat()
        assert today > sunday, "the pack would cover a week still in progress"


# ---------------------------------------------------------------- doctor

def _age(p: Path, days: float = 3) -> None:
    old = time.time() - days * 86400
    os.utime(p, (old, old))


def _doctor_check(name: str) -> dict:
    r = runner.invoke(app, ["doctor", "--json"])
    report = json.loads(r.stdout)
    return next(c for c in report["checks"] if c["check"] == name)


def test_an_old_weekly_review_pack_is_not_a_stuck_draft(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["review", "--lens", "all", "--write-only"])
    assert r.exit_code == 0, r.output
    staging = root / "data" / "staging"
    packs = sorted(staging.glob("review-all-*"))
    assert {p.suffix for p in packs} == {".md", ".json"}
    for p in packs:
        _age(p)
    check = _doctor_check("staging_drafts")
    assert check["status"] == "ok", check


def test_an_old_brief_draft_still_warns(tmp_path, monkeypatch):
    """Only the packs are exempt: the brief drafted from one is a real draft
    that was never ingested or applied."""
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    staging = root / "data" / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    brief = staging / "draft-review-week-2026-W42.json"
    brief.write_text("{}", encoding="utf-8")
    pack = staging / "review-all-2026-W42.json"
    pack.write_text("{}", encoding="utf-8")
    _age(brief)
    _age(pack)
    check = _doctor_check("staging_drafts")
    assert check["status"] == "warn"
    assert check["detail"].startswith("1 file(s) older than 2 days")
    assert check["detail"].endswith(": draft-review-week-2026-W42.json")


# ---------------------------------------------------------------- dashboard

def _dashboard_project(tmp_path, monkeypatch) -> Path:
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    monkeypatch.setattr("lifecrm.cli.subprocess.call", lambda args: 0)
    assert runner.invoke(app, ["dashboard"]).exit_code == 0     # a first snapshot
    return root / "data" / "dashboard" / "lifecrm.db"


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to replace an open file")
def test_a_second_dashboard_says_the_snapshot_is_in_use(tmp_path, monkeypatch):
    snap = _dashboard_project(tmp_path, monkeypatch)
    held = sqlite3.connect(f"file:{snap.as_posix()}?immutable=1", uri=True)
    try:                     # what a running `datasette serve -i` holds open
        held.execute("SELECT count(*) FROM sqlite_master").fetchone()
        r = runner.invoke(app, ["dashboard", "--port", "8124"])
    finally:
        held.close()
    assert r.exit_code == 1
    assert not isinstance(r.exception, OSError), "a traceback, not a message"
    assert "is another `lifecrm dashboard` serving this root?" in r.output
    assert len([ln for ln in r.output.splitlines() if ln.startswith("error")]) == 1
    assert list(snap.parent.glob("*.next-*")) == []
    check = sqlite3.connect(f"file:{snap.as_posix()}?immutable=1", uri=True)
    try:                                       # the served snapshot is still whole
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()
    # once the first one stops, a rerun refreshes it as before
    assert runner.invoke(app, ["dashboard"]).exit_code == 0


def test_a_snapshot_that_cannot_be_swapped_in_is_one_line(tmp_path, monkeypatch):
    snap = _dashboard_project(tmp_path, monkeypatch)
    before = snap.read_bytes()

    def busy(src, dst):
        raise PermissionError(13, "The process cannot access the file because it "
                                  "is being used by another process", str(dst))
    monkeypatch.setattr("lifecrm.cli.os.replace", busy)
    r = runner.invoke(app, ["dashboard"])
    assert r.exit_code == 1 and not isinstance(r.exception, OSError)
    assert "is in use" in r.output and "Stop it (Ctrl+C), then rerun" in r.output
    assert snap.read_bytes() == before
    assert list(snap.parent.glob("*.next-*")) == []


# ---------------------------------------------------------------- where

def _windows_home(monkeypatch, tmp_path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: home))
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "APPDATA"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("LIFECRM_ROOT", str(root))
    return home


def _groups(output: str) -> dict:
    return {g["title"]: g for g in json.loads(output)["groups"]}


def test_where_finds_a_backup_in_the_apps_upload_folder(tmp_path, monkeypatch):
    home = _windows_home(monkeypatch, tmp_path)
    upload = home / "OneDrive" / "Apps" / "SMS Backup & Restore"
    upload.mkdir(parents=True)
    for name in ("sms-20261021213000.xml", "calls-20261021213000.xml"):
        (upload / name).write_text("<smses/>", encoding="utf-8")
    r = runner.invoke(app, ["where", "--json"])
    assert r.exit_code == 0, r.output
    sms = _groups(r.output)["SMS Backup & Restore (Android texts and calls)"]
    assert len(sms["items"]) == 2, sms
    assert all(i["state"] == "found" for i in sms["items"])
    assert any(str(upload / "sms-20261021213000.xml") in c["command"]
               for c in sms["commands"])


def test_where_looks_in_every_onedrive_folder_observe_android_sms_searches(
        tmp_path, monkeypatch):
    from lifecrm.commands.where import drop_folders
    home = _windows_home(monkeypatch, tmp_path)
    searched = {os.path.normcase(str(p))
                for p in android_sms.search_places(_bare_config())}
    looked = {os.path.normcase(str(p)) for p in drop_folders(None)}
    assert os.path.normcase(str(home / "OneDrive" / "Apps" / "SMS Backup & Restore")) \
        in searched
    assert searched <= looked, sorted(searched - looked)


def test_where_never_offers_observe_browser_for_a_takeout(tmp_path, monkeypatch):
    home = _windows_home(monkeypatch, tmp_path)
    (home / "Downloads").mkdir()
    (home / "Downloads" / "takeout-20261021T120000Z-001.zip").write_bytes(b"")
    r = runner.invoke(app, ["where", "--json"])
    takeout = _groups(r.output)["Google Takeout"]
    commands = [c["command"] for c in takeout["commands"]]
    assert any("observe calendar" in c for c in commands)
    assert not any("observe browser" in c for c in commands), commands


# ---------------------------------------------------------------- demo: target

@pytest.fixture
def fake_project(tmp_path, monkeypatch) -> Path:
    """A stand-in project root, so nothing near the real data/ is probed."""
    proj = tmp_path / "LifeCRM"
    for d in ("data/staging", "data/transcripts", "backups"):
        (proj / d).mkdir(parents=True)
    (proj / "data" / "lifecrm.db").write_bytes(b"")
    monkeypatch.setattr(demo_build, "PROJECT_ROOT", proj)
    return proj


@pytest.mark.parametrize("inside", ["data/newdemo", "data/staging", "data/transcripts",
                                    "backups/newdemo", "data", "backups"])
def test_the_demo_refuses_the_projects_data_and_backups(fake_project, inside):
    with pytest.raises(DemoError, match="refusing"):
        check_target(fake_project / inside)


def test_the_demo_refuses_a_link_into_the_data_folder(fake_project, tmp_path):
    link = tmp_path / "looks-harmless"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(fake_project / "data"), str(link))
    else:
        link.symlink_to(fake_project / "data", target_is_directory=True)
    with pytest.raises(DemoError, match="refusing"):
        check_target(link / "newdemo")


def test_the_demo_refuses_the_folders_the_real_config_names(fake_project, tmp_path):
    inbox = tmp_path / "OneDrive" / "VoiceJournal" / "inbox"
    inbox.mkdir(parents=True)
    (fake_project / "config.toml").write_text(
        f"[paths]\ninbox_dir = '{inbox}'\n", encoding="utf-8")
    with pytest.raises(DemoError, match="refusing"):
        check_target(inbox / "demo")
    assert check_target(tmp_path / "elsewhere") == (tmp_path / "elsewhere").resolve()


def test_force_rechecks_the_target_before_deleting(fake_project, tmp_path):
    root = tmp_path / "demo"
    builder = DemoBuilder(root, "android", date(2026, 10, 21), echo=lambda *_: None,
                          ask=False, force=True, runner=lambda args: (0, "", ""))
    root.mkdir()
    (root / demo_build.MARKER).write_text("lifecrm demo\n", encoding="utf-8")
    (root / "draft-2026-10-20.json").write_text("{}", encoding="utf-8")
    # Between the check in __init__ and the delete, the real config started
    # naming the folder (marked once, now holding real files).
    (fake_project / "config.toml").write_text(
        f"[paths]\narchive_dir = '{root}'\n", encoding="utf-8")
    with pytest.raises(DemoError, match="refusing"):
        builder.prepare()
    assert (root / "draft-2026-10-20.json").exists()


# ---------------------------------------------------------------- demo: anchor

def test_the_default_anchor_honors_the_rollover():
    after_midnight = datetime(2026, 10, 22, 6, 23, tzinfo=timezone.utc)   # 01:23 CDT
    assert demo_build.default_anchor(after_midnight) == date(2026, 10, 21)
    morning = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)          # 06:00 CDT
    assert demo_build.default_anchor(morning) == date(2026, 10, 22)


def test_the_anchor_rollover_is_the_one_the_demo_config_sets(tmp_path):
    text = demo_build._config_text(tmp_path, "android")
    assert tomllib.loads(text)["time"]["day_rollover_hour"] == demo_build.ROLLOVER_HOUR


def test_the_demo_command_anchors_a_build_after_midnight_on_the_journal_day(
        tmp_path, monkeypatch):
    fixed = datetime(2026, 10, 22, 6, 23, tzinfo=timezone.utc)            # 01:23 CDT

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz is None else fixed.astimezone(tz)
    monkeypatch.setattr(demo_build, "datetime", Clock)
    seen = []

    class Builder:
        def __init__(self, root, combo, anchor, **kw):
            seen.append(anchor)

        def run(self):
            raise DemoError("stop here")
    monkeypatch.setattr(demo_build, "DemoBuilder", Builder)
    r = runner.invoke(app, ["demo", "--combo", "android", "--dir", str(tmp_path / "d")])
    assert r.exit_code == 1 and "stop here" in r.output
    assert seen == [date(2026, 10, 21)]

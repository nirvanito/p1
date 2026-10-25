"""The reminders calendar: one publisher, recorded revisions, stable bytes.

The load-bearing claims: every open due item and every linked birthday (two
dated occurrences, never a yearly rule) is a component; an unchanged
calendar is byte-identical and leaves no trace; every change — a day rolling
over, a snooze, a dismissal, a restoration, an edit — moves SEQUENCE exactly
once; two publishers serialize; a commit that fails after the file was
written is repaired by the next publication; the file parses through the
project's own reader; the CLI, the after-write hook and the daily job all run
the same publication.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import reminders as mod
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.connectors import _ics
from lifecrm.domain import corrections, entries
from lifecrm.domain import nudges as state
from lifecrm.domain.util import DomainError
from test_nudges import NOW, TODAY, seed

runner = CliRunner()
DAY2 = TODAY + timedelta(days=1)
NOW2 = NOW + timedelta(days=1)


@pytest.fixture
def world(conn, cfg, tmp_path):
    w = seed(conn, cfg)
    cfg.extra = {"assistant": {"reminders_dir": str(tmp_path / "synced")}}
    w["synced"] = tmp_path / "synced" / mod.FILE_NAME
    return w


def _publish(cfg, today=TODAY, now=NOW, **kw):
    return mod.publish(cfg, today=today, now=now, **kw)


def _events(cfg) -> dict[str, dict]:
    roots, _ = _ics.parse(mod.data_path(cfg).read_bytes(), "reminders")
    assert len(roots) == 1 and roots[0].name == "VCALENDAR"
    out = {}
    for ev in roots[0].children:
        assert ev.name == "VEVENT"
        out[ev.first("UID").value] = {p.name: p.value for p in ev.props}
    return out


def _publishes(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM audit_log WHERE table_name="
                        "'reminder_revisions'").fetchone()[0]


# ---------------------------------------------------------------- components

def test_components_cover_due_items_and_two_birthday_occurrences(conn, cfg, world):
    comps = mod.components(conn, cfg, today=TODAY)
    due = {c["summary"]: c for c in comps if c["kind"] == "due"}
    assert set(due) == {"Book flights to Ljubljana", "Renew passport", "Visit Mom"}
    assert due["Renew passport"]["day"] == TODAY             # overdue: this morning
    assert "overdue by 3 day(s)" in due["Renew passport"]["description"]
    assert due["Visit Mom"]["day"] == date(2026, 11, 15)     # far out, still on the calendar
    bdays = sorted((c["summary"], c["day"]) for c in comps if c["kind"] == "birthday")
    assert bdays == [("Linda Rivera's birthday", date(2026, 11, 30)),
                     ("Linda Rivera's birthday", date(2027, 11, 30)),
                     ("Sam Okafor's birthday (turning 34)", date(2026, 10, 25)),
                     ("Sam Okafor's birthday (turning 35)", date(2027, 10, 25))]
    sam = world["people"]["Sam Okafor"]
    uids = [c["uid"] for c in comps]
    assert f"lifecrm-bday-{sam}-2026@lifecrm" in uids and uids == sorted(uids)
    assert all(c["all_day"] for c in comps if c["kind"] == "birthday")
    assert not any("Alex Rivera" in c["summary"] for c in comps)   # the owner
    assert all(len(c["fingerprint"]) == 64 for c in comps)


# ---------------------------------------------------------------- publication

def test_publish_writes_both_copies_and_an_identical_run_changes_nothing(conn, cfg, world):
    r = _publish(cfg)
    assert r["wrote"] and r["added"] == 7 and r["copies"] == [str(world["synced"])]
    body = mod.data_path(cfg).read_bytes()
    assert world["synced"].read_bytes() == body
    text = body.decode("utf-8")
    assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//lifecrm//reminders//EN")
    assert "METHOD:PUBLISH" in text and "RRULE" not in text
    events = _events(cfg)
    assert len(events) == 7 and all(e["SEQUENCE"] == "0" for e in events.values())
    assert all(e["DTSTAMP"] == "20261021T170000Z" == e["LAST-MODIFIED"]
               for e in events.values())
    passport = events[f"lifecrm-due-{world['passport']['uuid']}@lifecrm"]
    assert passport["DTSTART"] == "20261021T140000Z"          # 09:00 Chicago, overdue -> today
    assert passport["DTEND"] == "20261021T143000Z"
    sam = events[f"lifecrm-bday-{world['people']['Sam Okafor']}-2026@lifecrm"]
    assert sam["DTSTART"] == "20261025" and sam["DTEND"] == "20261026"
    assert _publishes(conn) == 1

    again = _publish(cfg, now=datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc))
    assert not again["wrote"] and not any(again[k] for k in ("added", "changed",
                                                              "restored", "removed"))
    assert mod.data_path(cfg).read_bytes() == body and _publishes(conn) == 1


def test_every_change_is_one_revision(conn, cfg, world):
    _publish(cfg)
    uid = f"lifecrm-due-{world['passport']['uuid']}@lifecrm"
    key = f"due:{world['passport']['uuid']}"

    # across midnight the overdue item moves to the new morning
    r = _publish(cfg, today=DAY2, now=NOW2)
    assert (r["changed"], r["removed"]) == (1, 0)
    ev = _events(cfg)[uid]
    assert ev["SEQUENCE"] == "1" and ev["DTSTART"] == "20261022T140000Z"
    assert ev["LAST-MODIFIED"] == "20261022T170000Z" and "overdue by 4 day(s)" in ev["DESCRIPTION"]

    # a snooze moves it to the snooze day
    state.snooze(conn, key, until="2026-10-28")
    conn.commit()
    r = _publish(cfg, today=DAY2, now=NOW2 + timedelta(hours=1))
    assert r["changed"] == 1
    ev = _events(cfg)[uid]
    assert ev["SEQUENCE"] == "2" and ev["DTSTART"] == "20261028T140000Z"
    assert "snoozed until 2026-10-28" in ev["DESCRIPTION"]

    # dismissed: left out, its row a tombstone
    state.dismiss(conn, key)
    conn.commit()
    r = _publish(cfg, today=DAY2, now=NOW2 + timedelta(hours=2))
    assert r["removed"] == 1 and uid not in _events(cfg)
    row = conn.execute("SELECT * FROM reminder_revisions WHERE uid=?", (uid,)).fetchone()
    assert row["present"] == 0 and row["sequence"] == 2

    # restored with the content it had before the snooze: still a newer sequence
    state.restore(conn, key)
    conn.commit()
    r = _publish(cfg, today=DAY2, now=NOW2 + timedelta(hours=3))
    assert r["restored"] == 1 and _events(cfg)[uid]["SEQUENCE"] == "3"

    # a title edit
    corrections.edit_entry(conn, world["passport"]["uuid"], {"title": "Renew passport (expedited)"})
    conn.commit()
    r = _publish(cfg, today=DAY2, now=NOW2 + timedelta(hours=4))
    ev = _events(cfg)[uid]
    assert r["changed"] == 1 and ev["SEQUENCE"] == "4"
    assert ev["SUMMARY"] == "Renew passport (expedited)"

    # closing the task takes it off the calendar
    corrections.close_entry(conn, world["passport"]["uuid"], "done")
    conn.commit()
    r = _publish(cfg, today=DAY2, now=NOW2 + timedelta(hours=5))
    assert r["removed"] == 1 and uid not in _events(cfg)
    assert _publishes(conn) == 7


def test_two_publishers_serialize_on_the_write_transaction(conn, cfg, world):
    locked, released = threading.Event(), threading.Event()

    def hold():                     # another publisher, mid-transaction
        other = db_mod.connect(cfg.db_path)
        other.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(0.6)
        other.rollback()
        other.close()
        released.set()

    threading.Thread(target=hold, daemon=True).start()
    assert locked.wait(5)
    t0 = time.monotonic()
    r = _publish(cfg)                              # waits for the lock, then runs
    assert time.monotonic() - t0 >= 0.4 and released.is_set()
    assert r["added"] == 7 and _publishes(conn) == 1


def test_a_commit_that_fails_after_the_file_was_written_heals_next_time(conn, cfg, world,
                                                                         monkeypatch):
    _publish(cfg)
    key = f"due:{world['passport']['uuid']}"
    uid = f"lifecrm-due-{world['passport']['uuid']}@lifecrm"
    state.snooze(conn, key, until="2026-10-28")
    conn.commit()

    real = mod.write_atomic

    def write_then_fail(path, body):
        real(path, body)
        raise sqlite3.OperationalError("disk I/O error")   # stands in for a failed commit

    monkeypatch.setattr(mod, "write_atomic", write_then_fail)
    with pytest.raises(sqlite3.OperationalError):
        _publish(cfg, now=NOW + timedelta(hours=1))
    monkeypatch.setattr(mod, "write_atomic", real)
    # the file is ahead of the stored revisions ...
    assert _events(cfg)[uid]["SEQUENCE"] == "1"
    assert conn.execute("SELECT sequence FROM reminder_revisions WHERE uid=?",
                        (uid,)).fetchone()[0] == 0
    # ... and the next publication records the change once, not twice
    r = _publish(cfg, now=NOW + timedelta(hours=2))
    assert r["changed"] == 1 and _events(cfg)[uid]["SEQUENCE"] == "1"
    assert r["wrote"] and _publishes(conn) == 2


def test_without_a_folder_it_refuses_unless_told_otherwise(conn, cfg, world, tmp_path):
    cfg.extra = {}
    with pytest.raises(DomainError, match="reminders_dir"):
        _publish(cfg)
    r = _publish(cfg, if_configured=True)
    assert r["copies"] == [] and mod.data_path(cfg).exists()
    out = tmp_path / "elsewhere" / "cal.ics"
    r = _publish(cfg, out=out)
    assert r["copies"] == [str(out)] and out.read_bytes() == mod.data_path(cfg).read_bytes()


def test_dry_run_writes_nothing(conn, cfg, world):
    r = _publish(cfg, dry_run=True)
    assert r["dry_run"] and r["would_change"] and r["components"] == 7
    assert not mod.data_path(cfg).exists() and not world["synced"].exists()
    assert conn.execute("SELECT COUNT(*) FROM reminder_revisions").fetchone()[0] == 0
    assert _publishes(conn) == 0


def test_a_failed_copy_is_a_warning_not_a_failure(conn, cfg, world, tmp_path):
    blocker = tmp_path / "synced"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    r = _publish(cfg)
    assert r["wrote"] and r["copies"] == [] and len(r["warnings"]) == 1
    assert "could not copy" in r["warnings"][0]


# ---------------------------------------------------------------- CLI, hook

def _project(tmp_path, monkeypatch, assistant: str = ""):
    root = cli_project(tmp_path, monkeypatch)
    if assistant:
        with (root / "config.toml").open("a", encoding="utf-8") as f:
            f.write(assistant)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    try:
        world = seed(conn, cfg)
    finally:
        conn.close()
    return cfg, world


def test_cli_publishes_and_reports(tmp_path, monkeypatch):
    cfg, _ = _project(tmp_path, monkeypatch,
                      '[assistant]\nreminders_dir = "reminders-sync"\n')
    r = runner.invoke(app, ["reminders", "publish", "--today", "2026-10-21",
                            "--now", "2026-10-21T17:00:00Z"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("published 7 component(s) (7 added): ")
    assert "copied to" in r.output and (cfg.root / "reminders-sync" / mod.FILE_NAME).exists()
    r = runner.invoke(app, ["reminders", "publish", "--today", "2026-10-21",
                            "--now", "2026-10-21T18:00:00Z"])
    assert r.exit_code == 0 and "(no revisions)" in r.output and "(unchanged)" in r.output
    r = runner.invoke(app, ["reminders", "publish", "--today", "2026-10-22", "--dry-run"])
    assert r.exit_code == 0 and r.output.startswith("would publish 7 component(s) (1 changed)")
    r = runner.invoke(app, ["reminders", "publish", "--now", "yesterday"])
    assert r.exit_code == 1 and "--now must be an ISO instant" in r.output


def test_cli_without_a_folder(tmp_path, monkeypatch):
    cfg, _ = _project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["reminders", "publish"])
    assert r.exit_code == 1 and "reminders_dir" in r.output
    r = runner.invoke(app, ["reminders", "publish", "--if-configured"])
    assert r.exit_code == 0 and "nothing published" in r.output
    assert not mod.data_path(cfg).exists()


def test_the_after_write_hook_publishes_only_after_a_change(tmp_path, monkeypatch):
    cfg, _ = _project(tmp_path, monkeypatch,
                      '[assistant]\nreminders_dir = "reminders-sync"\n')
    synced = cfg.root / "reminders-sync" / mod.FILE_NAME
    # `init` was a change too: the hook published an empty calendar then, and
    # the seed that followed bypassed the CLI, so the file is now behind.
    assert synced.exists() and b"VEVENT" not in synced.read_bytes()
    r = runner.invoke(app, ["kinds", "add", "errand"])  # a write: the hook fires
    assert r.exit_code == 0, r.output
    assert "reminders calendar published: 7 component(s) revised" in r.output
    assert synced.read_bytes().count(b"BEGIN:VEVENT") == 7
    assert mod.data_path(cfg).read_bytes() == synced.read_bytes()
    stamp = synced.stat().st_mtime_ns
    r = runner.invoke(app, ["status"])               # reads only: nothing happens
    assert r.exit_code == 0 and synced.stat().st_mtime_ns == stamp
    assert "reminders calendar" not in r.output
    conn = db_mod.connect(cfg.db_path, readonly=True)
    try:
        assert _publishes(conn) == 1
    finally:
        conn.close()


def test_the_hook_stays_quiet_without_a_folder(tmp_path, monkeypatch):
    cfg, _ = _project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["kinds", "add", "errand"])
    assert r.exit_code == 0 and "reminders" not in r.output
    assert not mod.data_path(cfg).exists()

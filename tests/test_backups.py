"""3.3 — backups you can trust.

`verify-backup` restores a snapshot into a scratch root through the real
`restore`, runs doctor on it, and holds it to a floor: every row the live
database had when the snapshot was taken. The PowerShell check it replaces
counted three tables and only failed on MORE rows than live, so an empty
restore printed OK — the first failing test below is that exact case.

Also here: local and cloud snapshot age checked apart (a fresh local copy
used to hide a dead cloud one), the observations index verified like the
other full-text indexes, and `cloud_backups_dir` — the name that is true on
a Mac — with the OneDrive name kept working.
"""

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm.cli import app
from lifecrm.commands import verify
from lifecrm.config import ConfigError, load_config
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import now_utc
from test_cli import _ingest_golden, _sql_json
from test_doctor import _ai_chat_scan, _seed

runner = CliRunner()


def _setup(tmp_path, monkeypatch) -> Path:
    """A project root with an initialized database, and a private temp root
    so the scratch restore is visible to the test (and cleaned up in it)."""
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return root


def _backup(root: Path) -> Path:
    r = runner.invoke(app, ["backup"])
    assert r.exit_code == 0, r.output
    return sorted((root / "backups").glob("lifecrm-*.db"))[-1]


def _verify(*args):
    r = runner.invoke(app, ["verify-backup", *args, "--json"])
    return r, json.loads(r.stdout)


def _edit_snapshot(snap: Path, sql: str) -> None:
    """Damage a snapshot file directly — the backup the domain layer would
    never write, which is exactly what verify-backup exists to catch."""
    c = sqlite3.connect(snap)
    try:
        c.execute(sql)
        c.commit()
    finally:
        c.close()


def _future_empty_snapshot(root: Path) -> Path:
    """A valid, empty lifecrm database, named as the newest snapshot."""
    snap = root / "backups" / "lifecrm-20991231-235959.db"
    db_mod.init_db(snap, now_utc())
    return snap


# ---------------------------------------------------------------- the floor

def test_a_good_snapshot_verifies_and_the_scratch_copy_is_removed(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    snap = _backup(root)

    r = runner.invoke(app, ["verify-backup"])
    assert r.exit_code == 0, r.output
    assert f"verifying {snap.name}" in r.output
    assert "doctor on the restored copy: 0 failure(s)" in r.output
    assert "verify-backup OK" in r.output
    assert list((tmp_path / "tmp").iterdir()) == []          # scratch removed
    assert not list((root / "backups").glob("pre-restore-*.db"))  # live untouched
    assert _sql_json("SELECT COUNT(*) c FROM entries")[0]["c"] == 6


def test_an_empty_snapshot_fails_where_the_old_check_printed_ok(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    _backup(root)
    _future_empty_snapshot(root)                 # newest by name: the default

    r, report = _verify()
    assert r.exit_code == 1
    assert report["ok"] is False and report["doctor"]["failures"] == 0
    tables = {t["table"]: t for t in report["tables"]}
    assert tables["entries"]["live"] == 6 and tables["entries"]["restored"] == 0
    assert tables["entries"]["floor"] == 6 and tables["entries"]["problem"]
    assert any(p.startswith("sources: restored copy has 0 row(s), but live had 1")
               for p in report["problems"])


def test_every_real_table_is_compared_and_no_index_is(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    _backup(root)
    r, report = _verify()
    assert r.exit_code == 0, r.output
    names = {t["table"] for t in report["tables"]}
    assert {"entries", "observations", "health_records", "extraction_run_sources",
            "meta"} <= names
    assert not any("_fts" in n for n in names)
    by = {t["table"]: t for t in report["tables"]}
    assert by["audit_log"]["floor_basis"] == "ts"
    assert by["entry_revisions"]["floor_basis"] == "revised_at"
    assert by["mentions"]["floor_basis"] is None and by["mentions"]["floor"] == 1


def test_rows_logged_after_the_snapshot_are_not_required(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    # The floor counts rows stamped strictly before the snapshot's second, so
    # each side of the backup gets a second of its own.
    time.sleep(1.1)
    _backup(root)
    time.sleep(1.1)
    r = runner.invoke(app, ["import", "--stdin", "--date", "2026-07-28"],
                      input="Walked to the lake after work.")
    assert r.exit_code == 0, r.output

    r, report = _verify()
    assert r.exit_code == 0, report["problems"]
    sources = next(t for t in report["tables"] if t["table"] == "sources")
    assert (sources["live"], sources["floor"], sources["restored"]) == (2, 1, 1)


def test_a_table_without_a_timestamp_must_not_restore_empty(tmp_path, monkeypatch):
    """mentions has no creation stamp; a snapshot that lost them all still
    passes doctor — only the floor sees it."""
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    snap = _backup(root)
    _edit_snapshot(snap, "DELETE FROM mentions")

    r, report = _verify(str(snap))
    assert r.exit_code == 1
    assert report["doctor"]["failures"] == 0
    assert any(p.startswith("mentions: live has") and p.endswith("the restored copy none")
               for p in report["problems"])


def test_a_restored_copy_with_more_entries_than_live_is_flagged(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    _backup(root)
    r = runner.invoke(app, ["admin", "purge-day", "2026-07-27", "--i-am-sure"])
    assert r.exit_code == 0, r.output

    r, report = _verify()
    assert r.exit_code == 1
    assert any("entries: restored copy has MORE rows than live (6 > 0)" in p
               for p in report["problems"])


def test_a_snapshot_that_breaks_an_invariant_fails_on_doctor(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _ingest_golden(root)
    snap = _backup(root)
    _edit_snapshot(snap, "DELETE FROM entry_categories WHERE is_primary=1 AND "
                         "entry_id=(SELECT MIN(id) FROM entries)")
    r = runner.invoke(app, ["verify-backup", str(snap)])
    assert r.exit_code == 1
    assert "FAIL doctor on the restored copy: one_primary_category" in r.output
    assert "verify-backup FAILED" in r.output


def test_a_file_restore_refuses_is_one_failure_not_a_traceback(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    junk = root / "backups" / "lifecrm-20991231-000000.db"
    junk.write_text("this is not a database", encoding="utf-8")
    r = runner.invoke(app, ["verify-backup"])
    assert r.exit_code == 1
    assert "FAIL restore refused the snapshot" in r.output
    assert "not a readable SQLite database" in r.output
    assert "Traceback" not in r.output
    assert list((tmp_path / "tmp").iterdir()) == []


def test_no_snapshot_is_one_error_line(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["verify-backup"])
    assert r.exit_code == 1
    assert r.output.strip() == (f"error: no snapshots in {root / 'backups'} — run: "
                                f"python -m lifecrm backup")


def test_the_backups_folder_comes_from_config_and_the_root_from_the_flag(
        tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    cfg_file = root / "config.toml"
    cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace(
        'backups_dir = "backups"', 'backups_dir = "vault"'), encoding="utf-8")
    _ingest_golden(root)
    r = runner.invoke(app, ["backup"])
    assert r.exit_code == 0, r.output
    snap = sorted((root / "vault").glob("lifecrm-*.db"))[-1]

    monkeypatch.delenv("LIFECRM_ROOT")
    r = runner.invoke(app, ["--root", str(root), "verify-backup"])
    assert r.exit_code == 0, r.output
    assert f"verifying {snap.name}" in r.output


def test_the_snapshot_time_comes_from_its_name_else_its_file_time(tmp_path):
    named = tmp_path / "lifecrm-20261021-213000.db"
    named.write_bytes(b"")
    assert verify.snapshot_time(named) == ("2026-10-21T21:30:00Z", "file name")
    pre = tmp_path / "pre-restore-20261020-080000.db"
    pre.write_bytes(b"")
    assert verify.snapshot_time(pre)[0] == "2026-10-20T08:00:00Z"
    renamed = tmp_path / "my copy.db"
    renamed.write_bytes(b"")
    os.utime(renamed, (1_700_000_000, 1_700_000_000))
    assert verify.snapshot_time(renamed) == ("2023-11-14T22:13:20Z",
                                             "file modification time")


def test_the_newest_snapshot_is_chosen_by_name_not_file_time(tmp_path, cfg):
    old, new = (cfg.backups_dir / "lifecrm-20260101-000000.db",
                cfg.backups_dir / "lifecrm-20260201-000000.db")
    new.write_bytes(b"")
    old.write_bytes(b"")                          # copied later: newer mtime
    (cfg.backups_dir / "pre-restore-20990101-000000.db").write_bytes(b"")
    assert verify.newest_snapshot(cfg) == new


def test_counted_tables_skip_every_index_and_shadow_table(conn):
    names = verify.counted_tables(conn)
    assert "entries" in names and "observations" in names
    assert not [n for n in names if "_fts" in n]
    assert not [n for n in names if n.startswith("sqlite_")]


# ---------------------------------------------------------------- doctor

def _status(report, name):
    found = [c for c in report["checks"] if c["check"] == name]
    return found[0]["status"] if found else None


def _snapshot_file(folder: Path, age_days: float = 0) -> Path:
    f = folder / "lifecrm-20261001-000000.db"
    f.write_bytes(b"")
    when = time.time() - age_days * 86400
    os.utime(f, (when, when))
    return f


def test_a_fresh_local_snapshot_no_longer_hides_a_dead_cloud_copy(conn, cfg):
    _seed(conn, cfg)
    _snapshot_file(cfg.backups_dir)
    report = run_checks(conn, cfg)
    assert _status(report, "backup_age") == "ok"
    assert _status(report, "backup_age_cloud") == "warn"
    detail = next(c["detail"] for c in report["checks"]
                  if c["check"] == "backup_age_cloud")
    assert "no snapshots in the cloud copy folder" in detail
    assert "OneDrive" not in detail


def test_a_fresh_cloud_copy_does_not_hide_a_dead_local_backup(conn, cfg):
    _seed(conn, cfg)
    _snapshot_file(cfg.cloud_backups_dir)
    _snapshot_file(cfg.backups_dir, age_days=10)
    report = run_checks(conn, cfg)
    assert _status(report, "backup_age") == "warn"
    assert _status(report, "backup_age_cloud") == "ok"
    detail = next(c["detail"] for c in report["checks"] if c["check"] == "backup_age")
    assert "10 days old" in detail


def test_a_stale_cloud_copy_says_how_old(conn, cfg):
    _seed(conn, cfg)
    _snapshot_file(cfg.backups_dir)
    _snapshot_file(cfg.cloud_backups_dir, age_days=9)
    report = run_checks(conn, cfg)
    assert _status(report, "backup_age_cloud") == "warn"
    assert "newest cloud copy is 9 days old" in next(
        c["detail"] for c in report["checks"] if c["check"] == "backup_age_cloud")


def test_local_only_by_choice_has_no_cloud_check(conn, cfg):
    _seed(conn, cfg)
    cfg.onedrive_backups_dir = None
    _snapshot_file(cfg.backups_dir)
    report = run_checks(conn, cfg)
    assert _status(report, "backup_age") == "ok"
    assert _status(report, "backup_age_cloud") is None


def test_doctor_catches_an_observations_index_out_of_sync(conn, cfg, tmp_path):
    scan = _ai_chat_scan(cfg, tmp_path)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    assert _status(run_checks(conn, cfg), "fts_sync_observations_fts") == "ok"

    row = conn.execute("SELECT id, title, body FROM observations LIMIT 1").fetchone()
    conn.execute("INSERT INTO observations_fts(observations_fts, rowid, title, body) "
                 "VALUES ('delete', ?, ?, ?)", (row["id"], row["title"], row["body"]))
    report = run_checks(conn, cfg)
    assert _status(report, "fts_sync_observations_fts") == "fail"
    assert report["healthy"] is False


def test_without_the_v4_index_there_is_nothing_to_check(conn, cfg):
    """observations_fts arrived with v4; its check must not turn a missing
    index into a crash or a second failure."""
    for trigger in ("observations_fts_ai", "observations_fts_ad", "observations_fts_au"):
        conn.execute(f"DROP TRIGGER {trigger}")
    conn.execute("DROP TABLE observations_fts")
    report = run_checks(conn, cfg)
    assert _status(report, "fts_sync_observations_fts") is None
    assert _status(report, "fts_sync_entries_fts") == "ok"


# ---------------------------------------------------------------- config

def _config(tmp_path, paths: str) -> Path:
    (tmp_path / "config.toml").write_text(
        "[paths]\n" + paths + '[time]\ntimezone = "America/Chicago"\n',
        encoding="utf-8")
    return tmp_path


def test_cloud_backups_dir_is_the_preferred_name(tmp_path):
    cfg = load_config(_config(tmp_path, "cloud_backups_dir = '~/iCloud/backups'\n"))
    assert cfg.cloud_backups_dir == Path.home() / "iCloud" / "backups"
    assert cfg.onedrive_backups_dir == cfg.cloud_backups_dir   # every old caller


def test_the_onedrive_name_still_works(tmp_path):
    cfg = load_config(_config(tmp_path, f"onedrive_backups_dir = '{tmp_path / 'od'}'\n"))
    assert cfg.cloud_backups_dir == tmp_path / "od"


def test_both_names_naming_the_same_folder_is_fine(tmp_path):
    folder = tmp_path / "cloud"
    cfg = load_config(_config(tmp_path, f"cloud_backups_dir = '{folder}'\n"
                                        f"onedrive_backups_dir = '{folder}'\n"))
    assert cfg.cloud_backups_dir == folder


def test_both_names_naming_different_folders_is_one_error(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(_config(tmp_path, "cloud_backups_dir = 'a'\n"
                                      "onedrive_backups_dir = 'b'\n"))
    assert "sets both cloud_backups_dir ('a') and onedrive_backups_dir ('b')" in str(e.value)
    assert "\n" not in str(e.value)


def test_no_cloud_folder_means_local_only(tmp_path):
    assert load_config(_config(tmp_path, 'data_dir = "data"\n')).cloud_backups_dir is None


def test_backup_and_restore_speak_of_a_cloud_copy_not_onedrive(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["backup"])
    assert r.exit_code == 0, r.output
    assert "cloud copy: " in r.output and "OneDrive" not in r.output
    assert list((root / "onedrive" / "backups").glob("lifecrm-*.db"))

    r = runner.invoke(app, ["restore"])
    assert r.exit_code == 0, r.output
    assert "(local)" in r.output and "(cloud copy)" in r.output


def test_a_failed_cloud_copy_warns_and_the_local_snapshot_stands(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    blocker = root / "not-a-folder"
    blocker.write_text("a file where the cloud folder should be", encoding="utf-8")
    cfg_file = root / "config.toml"
    text = cfg_file.read_text(encoding="utf-8")
    start = text.index("onedrive_backups_dir")
    end = text.index("\n", start)
    cfg_file.write_text(text[:start] + f"cloud_backups_dir = '{blocker}'"
                        + text[end:], encoding="utf-8")
    r = runner.invoke(app, ["backup"])
    assert r.exit_code == 0, r.output
    assert "WARNING: cloud copy failed" in r.output
    assert list((root / "backups").glob("lifecrm-*.db"))

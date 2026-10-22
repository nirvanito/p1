"""lifecrm verify-backup: prove a snapshot restores, without touching the live DB.

An untested backup is not a backup. This restores a snapshot into a scratch
project root through the real `restore` command — the same code a new machine
would run — runs doctor on the result, and compares every table's row count
with the live database.

The comparison has a floor, not just a ceiling. The PowerShell check it
replaces counted three tables and failed only when the restored copy had MORE
rows than live, so a snapshot that restored to an empty database printed OK.
Here every row the live database already had when the snapshot was taken must
be in the restored copy:

- a table with a creation stamp (created_at, ts, revised_at, asked_at): the
  restored copy holds at least as many rows as live has stamped strictly
  before the snapshot's time, read from its UTC file name
  (lifecrm-YYYYMMDD-HHMMSS.db). Strictly: the name is the second the backup
  started, and a row stamped in that same second may have landed just after.
- a table without one: when live has rows, the restored copy has some.

The ceiling stays for entries and sources: a restored copy holding more than
live means live lost rows since (admin purge-day?) or the snapshot belongs to
another database — worth a human look either way.

One false alarm is possible and stated rather than hidden: a write transaction
still open when the backup ran (a long import) stamps its rows before the
snapshot but commits them after. Re-run after the next backup.

Both platforms share this: scripts/verify-restore.ps1 (Windows) and
scripts/verify-restore.sh (macOS) are thin wrappers that pass its exit code on.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import typer

from .. import db as db_mod
from ..config import PROJECT_ROOT, Config

# Creation stamps, in the order a table's own column is preferred. Every one is
# written as 'YYYY-MM-DDTHH:MM:SSZ' at insert, so string order is time order.
TIMESTAMP_COLUMNS = ("created_at", "ts", "revised_at", "asked_at")
CEILING_TABLES = ("entries", "sources")
_SNAPSHOT_TIME_RE = re.compile(r"(\d{8})-(\d{6})\.db$")


# ---------------------------------------------------------------- pieces

def newest_snapshot(cfg: Config) -> Path | None:
    """Newest local lifecrm-*.db by name. Names are UTC timestamps, so name
    order is time order whatever the file times say after a copy."""
    if not cfg.backups_dir.is_dir():
        return None
    found = sorted(cfg.backups_dir.glob("lifecrm-*.db"))
    return found[-1] if found else None


def snapshot_time(path: Path) -> tuple[str, str]:
    """(when the snapshot was taken, in the schema's UTC format; where that
    came from). A renamed file falls back to its modification time."""
    m = _SNAPSHOT_TIME_RE.search(path.name)
    if m:
        try:
            when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            return when.strftime("%Y-%m-%dT%H:%M:%SZ"), "file name"
        except ValueError:
            pass
    when = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ"), "file modification time"


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def counted_tables(conn: sqlite3.Connection) -> list[str]:
    """Every real table. Full-text indexes and their shadow tables are derived
    from content tables (doctor verifies they agree); sqlite_* are SQLite's."""
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'").fetchall()
    virtual = [r["name"] for r in rows
               if (r["sql"] or "").upper().startswith("CREATE VIRTUAL TABLE")]
    return sorted(r["name"] for r in rows if r["name"] not in virtual
                  and not any(r["name"].startswith(v + "_") for v in virtual))


def timestamp_column(conn: sqlite3.Connection, table: str) -> str | None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({_quoted(table)})")}
    return next((c for c in TIMESTAMP_COLUMNS if c in cols), None)


def _count(conn: sqlite3.Connection, table: str, column: str | None = None,
           before: str | None = None) -> int:
    if column is None:
        return conn.execute(f"SELECT COUNT(*) FROM {_quoted(table)}").fetchone()[0]
    return conn.execute(f"SELECT COUNT(*) FROM {_quoted(table)} "
                        f"WHERE {_quoted(column)} < ?", (before,)).fetchone()[0]


def compare_counts(live: sqlite3.Connection, restored: sqlite3.Connection,
                   taken_at: str) -> list[dict]:
    """One row per live table: live count, the floor the restored copy must
    reach, what it holds, and the problem when it falls short."""
    in_restored = set(counted_tables(restored))
    out = []
    for table in counted_tables(live):
        column = timestamp_column(live, table)
        live_n = _count(live, table)
        floor = (_count(live, table, column, taken_at) if column
                 else min(live_n, 1))
        got = _count(restored, table) if table in in_restored else None
        problem = None
        if got is None:
            if live_n:
                problem = (f"{table}: missing from the restored copy (live has "
                           f"{live_n} row(s))")
        elif got < floor:
            problem = (f"{table}: restored copy has {got} row(s), but live had "
                       f"{floor} stamped before the snapshot ({column} < "
                       f"{taken_at})" if column else
                       f"{table}: live has {live_n} row(s), the restored copy none")
        elif table in CEILING_TABLES and got > live_n:
            problem = (f"{table}: restored copy has MORE rows than live ({got} > "
                       f"{live_n}) — live lost rows since the snapshot (admin "
                       f"purge-day?) or the snapshot is from another database")
        out.append({"table": table, "live": live_n, "floor": floor,
                    "floor_basis": column, "restored": got, "problem": problem})
    return out


def _restore_into(scratch: Path, snap: Path, cfg: Config) -> subprocess.CompletedProcess:
    """Restore exactly as a person would: the real CLI, in its own process,
    against a project root that is nothing but a config.toml."""
    (scratch / "config.toml").write_text(
        "[paths]\n"
        'data_dir = "data"\n'
        'backups_dir = "backups"\n'
        "[time]\n"
        f"timezone = {json.dumps(cfg.timezone)}\n"
        f"day_rollover_hour = {cfg.day_rollover_hour}\n", encoding="utf-8")
    env = {**os.environ, "LIFECRM_ROOT": str(scratch), "PYTHONUTF8": "1"}
    return subprocess.run(
        [sys.executable, "-m", "lifecrm", "--root", str(scratch), "restore", str(snap)],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- the check

def verify(cfg: Config, snap: Path) -> dict:
    """Restore snap into a scratch root, doctor it, compare it with live.
    Reads the live database only; the scratch root is always removed."""
    from ..doctor import run_checks
    taken_at, taken_from = snapshot_time(snap)
    report = {"snapshot": str(snap), "taken_at": taken_at,
              "taken_at_from": taken_from, "restore": None, "doctor": None,
              "tables": [], "notes": [], "problems": []}
    scratch = Path(tempfile.mkdtemp(prefix="lifecrm-verify-"))
    try:
        done = _restore_into(scratch, snap, cfg)
        lines = [ln for ln in (done.stdout + done.stderr).splitlines() if ln.strip()]
        report["restore"] = {"exit_code": done.returncode, "output": lines}
        if done.returncode != 0:
            last = lines[-1] if lines else f"exit {done.returncode}"
            report["problems"].append(f"restore refused the snapshot: {last}")
            return report

        restored = db_mod.connect(scratch / "data" / "lifecrm.db")
        try:
            # No cfg: the backup and inbox checks describe this machine's
            # folders, not the snapshot. Every invariant check runs.
            checks = run_checks(restored, None)
            restored.rollback()
            failed = [c for c in checks["checks"] if c["status"] == "fail"]
            report["doctor"] = {"failures": checks["failures"],
                                "warnings": checks["warnings"], "failed": failed}
            report["problems"] += [f"doctor on the restored copy: {c['check']}"
                                   + (f" — {c['detail']}" if c["detail"] else "")
                                   for c in failed]

            if not cfg.db_path.exists():
                report["notes"].append(f"no live database at {cfg.db_path} — "
                                       f"row counts not compared")
                return report
            live = db_mod.connect(cfg.db_path, readonly=True)
            try:
                report["tables"] = compare_counts(live, restored, taken_at)
            finally:
                live.close()
            report["problems"] += [t["problem"] for t in report["tables"]
                                   if t["problem"]]
        finally:
            restored.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if scratch.exists():
            report["notes"].append(f"could not remove the scratch copy {scratch} "
                                   f"— delete it by hand")
        report["ok"] = not report["problems"]
    return report


def _echo_report(report: dict) -> None:
    typer.echo(f"verifying {Path(report['snapshot']).name} (taken "
               f"{report['taken_at']}, per its {report['taken_at_from']})")
    if report["restore"]:
        for line in report["restore"]["output"]:
            typer.echo(f"  restore: {line}")
    if report["doctor"]:
        typer.echo(f"  doctor on the restored copy: {report['doctor']['failures']} "
                   f"failure(s), {report['doctor']['warnings']} warning(s)")
    if report["tables"]:
        width = max(len(t["table"]) for t in report["tables"])
        typer.echo(f"  {'table':{width}}  {'live':>7}  {'needs':>7}  {'restored':>8}")
        for t in report["tables"]:
            got = "-" if t["restored"] is None else t["restored"]
            typer.echo(f"  {t['table']:{width}}  {t['live']:>7}  {t['floor']:>7}  "
                       f"{got:>8}" + ("  FAIL" if t["problem"] else ""))
    for note in report["notes"]:
        typer.echo(f"note: {note}")
    for problem in report["problems"]:
        typer.echo(f"FAIL {problem}")
    if report["ok"]:
        typer.echo(f"verify-backup OK: {Path(report['snapshot']).name} restores "
                   f"cleanly, passes doctor, and holds every row live had when "
                   f"it was taken")
    else:
        typer.echo(f"verify-backup FAILED: {len(report['problems'])} problem(s) — "
                   f"see the FAIL lines above", err=True)


def verify_backup(
    snapshot: str = typer.Argument(
        None, help="Snapshot .db to verify; default: the newest lifecrm-*.db in "
                   "the backups folder"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Prove a snapshot restores: restore it into a scratch folder, run doctor
    on it, and check it holds every row the live database had when it was
    taken. Never touches the live database. Exits 1 on any problem."""
    from ..cli import _cfg, _echo_json, _fail
    cfg = _cfg()
    if snapshot is None:
        snap = newest_snapshot(cfg)
        if snap is None:
            _fail(f"no snapshots in {cfg.backups_dir} — run: python -m lifecrm backup")
    else:
        snap = Path(snapshot).expanduser()
        if not snap.is_file():
            _fail(f"snapshot not found: {snap}")
    report = verify(cfg, snap.resolve())
    if as_json:
        _echo_json(report)
    else:
        _echo_report(report)
    if not report["ok"]:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    app.command("verify-backup")(verify_backup)

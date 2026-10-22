"""The scheduled-task side of 3.3, run for real through powershell.exe.

Like 1.5's script tests, these point the real scripts at a throwaway project
(LIFECRM_ROOT) and check what Task Scheduler would see: the exit code, and
what lands in the log. One thing they add: the scripts are run from a working
directory that is not the project, because Task Scheduler starts tasks in
System32 — where `python -m lifecrm` finds no package at all.
"""

import os
import re
import sqlite3
import subprocess
from pathlib import Path

from conftest import PROJECT_ROOT
from test_failure_visibility import POWERSHELL, _project, needs_powershell

from lifecrm import db as db_mod
from lifecrm.domain.util import now_utc

SCRIPTS = PROJECT_ROOT / "scripts"
VENV_PY = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"


def _cli(root: Path, *args: str, stdin: str | None = None) -> None:
    env = {**os.environ, "LIFECRM_ROOT": str(root), "PYTHONUTF8": "1"}
    subprocess.run([str(VENV_PY), "-m", "lifecrm", *args], env=env, check=True,
                   capture_output=True, input=stdin, text=True, encoding="utf-8",
                   cwd=str(PROJECT_ROOT))


def _run(name: str, root: Path, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """A scheduled-task script on the OEM code page (as 1.5's helper), from
    a working directory that is not the project — as Task Scheduler runs it."""
    temp = root / "tmp"
    temp.mkdir(exist_ok=True)
    env = {**os.environ, "LIFECRM_ROOT": str(root), "TEMP": str(temp), "TMP": str(temp)}
    extra = "".join(f' "{a}"' for a in args)
    command = (f'cmd /c "chcp 437 >nul & "{POWERSHELL}" -NoProfile '
               f'-ExecutionPolicy Bypass -File "{SCRIPTS / name}"{extra}"')
    return subprocess.run(command, env=env, cwd=str(cwd), capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=300)


def _logged_project(tmp_path) -> Path:
    root = _project(tmp_path)
    _cli(root, "import", "--stdin", "--date", "2026-10-20",
         stdin="Bouldering with Sam after work. Sent the V4.")
    return root


def _log(root: Path, name: str) -> str:
    return (root / "data" / name).read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------- verify-restore

@needs_powershell
def test_verify_restore_reports_failure_for_an_empty_snapshot(tmp_path):
    root = _logged_project(tmp_path)
    db_mod.init_db(root / "backups" / "lifecrm-20991231-235959.db", now_utc())
    done = _run("verify-restore.ps1", root, cwd=tmp_path)
    assert done.returncode == 1, done.stdout + done.stderr
    log = _log(root, "verify-restore.log")
    assert "FAIL sources: restored copy has 0 row(s), but live had 1" in log
    assert "FAILED (exit 1)" in log
    assert list((root / "tmp").iterdir()) == []              # scratch removed


@needs_powershell
def test_verify_restore_passes_a_good_snapshot_and_takes_an_explicit_one(tmp_path):
    root = _logged_project(tmp_path)
    _cli(root, "backup")
    snap = sorted((root / "backups").glob("lifecrm-*.db"))[-1]
    done = _run("verify-restore.ps1", root, tmp_path, str(snap))
    assert done.returncode == 0, done.stdout + done.stderr
    log = _log(root, "verify-restore.log")
    assert f"verify-backup OK: {snap.name}" in log and "FAILED" not in log
    assert "verify-backup OK" in done.stdout                  # a person sees it too


# ---------------------------------------------------------------- doctor

@needs_powershell
def test_the_doctor_task_logs_problems_and_passes_the_exit_code_on(tmp_path):
    root = _logged_project(tmp_path)
    done = _run("doctor.ps1", root, cwd=tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    log = _log(root, "doctor.log")
    assert "0 failure(s)" in log
    assert "WARN  backup_age" in log                           # never backed up
    assert not re.search(r"^\S+ \S+ ok  ", log, re.M), "ok lines bury the problems"

    # tombstone the transcript out of its index: corruption only doctor sees
    c = sqlite3.connect(root / "data" / "lifecrm.db")
    row = c.execute("SELECT id, text FROM transcripts").fetchone()
    c.execute("INSERT INTO transcripts_fts(transcripts_fts, rowid, text) "
              "VALUES ('delete', ?, ?)", row)
    c.commit()
    c.close()
    done = _run("doctor.ps1", root, cwd=tmp_path)
    assert done.returncode == 1, done.stdout + done.stderr
    log = _log(root, "doctor.log")
    assert "FAIL  fts_sync_transcripts_fts" in log and "FAILED (exit 1)" in log


# ---------------------------------------------------------------- install-tasks

def test_install_tasks_schedules_doctor_and_survives_a_laptop():
    """Static: registering tasks for real would change the machine running
    the tests. What is checked is what the 1.5-style tests cannot reach."""
    text = (SCRIPTS / "install-tasks.ps1").read_text(encoding="utf-8")
    assert re.search(r'"LifeCRM doctor" "doctor\.ps1"', text)
    assert "-AllowStartIfOnBatteries" in text and "-DontStopIfGoingOnBatteries" in text
    assert "-WorkingDirectory $Root" in text
    for script in re.findall(r'"LifeCRM [^"]+" "([^"]+\.ps1)"', text):
        assert (SCRIPTS / script).exists(), f"install-tasks names missing {script}"


@needs_powershell
def test_the_scripts_parse():
    names = ("doctor.ps1", "verify-restore.ps1", "install-tasks.ps1")
    check = "; ".join(
        f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPTS / n}', [ref]$null, [ref]$e); if ($e.Count) {{ '{n}: ' + $e[0] }}"
        for n in names)
    done = subprocess.run([POWERSHELL, "-NoProfile", "-Command", check],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "", done.stdout + done.stderr

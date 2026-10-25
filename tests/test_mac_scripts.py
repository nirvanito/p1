"""The macOS scripts (scripts/*.sh): syntax, the scheduled-job semantics they
port from the PowerShell originals, and the launchd plists.

They run here under whatever bash exists — /bin/bash 3.2 on a Mac, Git Bash
on Windows — with LIFECRM_PYTHON pointing at the interpreter running this
suite, against a throwaway project root. The assertions are the ones the
PowerShell tests make (test_failure_visibility.py): a poison recording makes
the job exit 1 with the file named in the log, an empty inbox exits 0, a
broken config fails the backup. Nothing here calls launchctl: plists are
written into a temp folder (LIFECRM_LAUNCHAGENTS_DIR, --no-load) and read
back with plistlib.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT

from lifecrm import db as db_mod
from lifecrm.domain.util import now_utc

SCRIPTS = PROJECT_ROOT / "scripts"
SH_FILES = sorted(SCRIPTS.glob("*.sh"))
OLD = time.time() - 3 * 86400            # past intake's "still uploading" guard


def _find_bash() -> str | None:
    """A real bash: /bin/bash on a Mac; Git Bash on Windows. Never
    System32\\bash.exe — that is WSL, a different machine with other paths."""
    if os.name != "nt":            # /bin/bash is what launchd runs, not Homebrew's
        return "/bin/bash" if Path("/bin/bash").is_file() else shutil.which("bash")
    candidates = []
    git = shutil.which("git")
    if git:
        candidates += [parent / "bin" / "bash.exe" for parent in Path(git).resolve().parents]
    for var in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            candidates += [Path(base) / "Git" / "bin" / "bash.exe",
                           Path(base) / "Programs" / "Git" / "bin" / "bash.exe"]
    return next((str(c) for c in candidates if c.is_file()), None)


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="no bash (Git Bash on Windows)")


def _env(**extra) -> dict:
    env = {**os.environ, "LIFECRM_PYTHON": Path(sys.executable).as_posix(),
           "HF_HUB_OFFLINE": "1", "PYTHONUTF8": "1"}
    env.pop("LIFECRM_ROOT", None)
    env.update(extra)
    return env


def _run(script: str, *args, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, (SCRIPTS / script).as_posix(), *args], env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def _project(tmp_path, *, toml: str | None = None) -> Path:
    """A throwaway project root, as test_failure_visibility builds one; the
    database is created in-process (one python start fewer per test)."""
    root = tmp_path / "project"
    cloud = root / "cloud"
    for d in ("inbox", "inbox-text", "archive", "backups"):
        (cloud / d).mkdir(parents=True, exist_ok=True)
    empty_model = root / "empty-model"
    empty_model.mkdir()
    (root / "config.toml").write_text(toml if toml is not None else (
        "[paths]\n"
        'data_dir = "data"\n'
        'backups_dir = "backups"\n'
        f"inbox_dir = '{cloud / 'inbox'}'\n"
        f"inbox_text_dir = '{cloud / 'inbox-text'}'\n"
        f"archive_dir = '{cloud / 'archive'}'\n"
        f"onedrive_backups_dir = '{cloud / 'backups'}'\n"
        "[time]\n"
        'timezone = "America/Chicago"\n'
        "day_rollover_hour = 5\n"
        "[whisper]\n"
        f"model = '{empty_model}'\n"          # a model folder with no model in it
        "seed_initial_prompt = false\n"), encoding="utf-8")
    (root / "data").mkdir()
    db_mod.init_db(root / "data" / "lifecrm.db", now_utc())
    return root


def _root_env(root: Path) -> dict:
    return _env(LIFECRM_ROOT=root.as_posix())


# ---------------------------------------------------------------- the files

def test_every_windows_script_has_a_mac_port():
    names = {p.stem for p in SH_FILES}
    assert {"setup", "transcribe-inbox", "backup", "restore", "verify-restore",
            "install-tasks"} <= names
    assert {"doctor", "uninstall-tasks"} <= names        # Mac-side additions


def test_scripts_are_lf_and_start_with_bin_bash():
    for p in SH_FILES:
        raw = p.read_bytes()
        assert b"\r\n" not in raw, f"{p.name} has CRLF line endings"
        if not p.name.startswith("_"):            # _common.sh is sourced, not run
            assert raw.startswith(b"#!/bin/bash\n"), p.name


def test_gitattributes_keeps_shell_scripts_lf_on_a_windows_checkout():
    attrs = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh" in attrs and "eol=lf" in attrs


@needs_bash
@pytest.mark.parametrize("script", SH_FILES, ids=lambda p: p.name)
def test_every_script_parses(script):
    done = subprocess.run([BASH, "-n", script.as_posix()], capture_output=True,
                          text=True)
    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------------- scheduled jobs

@needs_bash
def test_a_poison_recording_makes_the_launchd_job_report_failure(tmp_path):
    root = _project(tmp_path)
    poison = root / "cloud" / "inbox" / "2026-09-23 Café.m4a"
    poison.write_bytes(bytes(64))
    os.utime(poison, (OLD, OLD))

    done = _run("transcribe-inbox.sh", env=_root_env(root))
    assert done.returncode == 1, done.stdout + done.stderr
    log = (root / "data" / "transcribe.log").read_text(encoding="utf-8")
    lines = log.splitlines()
    stamp = lines[0][:19]
    assert lines[0] == f"{stamp} starting inbox intake"
    assert all(line.startswith(stamp) for line in lines)    # one stamp per run
    assert "ERROR: 2026-09-23 Café.m4a" in log              # the name survives
    assert "— see the lines above" in log
    assert lines[-1] == f"{stamp} FAILED (exit 1) - see the lines above"
    assert "\r" not in log
    assert poison.exists()                                  # left for the next run


@needs_bash
def test_an_empty_inbox_run_reports_success(tmp_path):
    root = _project(tmp_path)
    done = _run("transcribe-inbox.sh", env=_root_env(root))
    assert done.returncode == 0, done.stdout + done.stderr
    log = (root / "data" / "transcribe.log").read_text(encoding="utf-8")
    assert "nothing to intake" in log and "FAILED" not in log


@needs_bash
def test_a_missing_interpreter_is_a_logged_failure_not_a_silent_one(tmp_path):
    root = _project(tmp_path)
    env = _env(LIFECRM_ROOT=root.as_posix(),
               LIFECRM_PYTHON=(tmp_path / "no-such-python").as_posix())
    done = _run("transcribe-inbox.sh", env=env)
    assert done.returncode == 127
    log = (root / "data" / "transcribe.log").read_text(encoding="utf-8")
    assert "FAILED (exit 127)" in log


@needs_bash
def test_the_doctor_job_logs_a_healthy_database_and_exits_0(tmp_path):
    root = _project(tmp_path)
    done = _run("doctor.sh", env=_root_env(root))
    assert done.returncode == 0, done.stdout + done.stderr
    log = (root / "data" / "doctor.log").read_text(encoding="utf-8")
    assert " starting doctor" in log.splitlines()[0]
    assert "0 failure(s)" in log and "FAILED" not in log


@needs_bash
def test_the_doctor_job_reports_failure_when_doctor_cannot_run(tmp_path):
    root = _project(tmp_path)
    (root / "config.toml").write_text("[paths\nbroken", encoding="utf-8")
    done = _run("doctor.sh", env=_root_env(root))
    assert done.returncode == 1
    log = (root / "data" / "doctor.log").read_text(encoding="utf-8")
    assert "is not valid TOML" in log
    assert "FAILED (exit 1) - see the lines above" in log


@needs_bash
def test_a_failed_backup_makes_the_job_report_failure(tmp_path):
    root = _project(tmp_path)
    (root / "config.toml").write_text("[paths\nbroken", encoding="utf-8")
    done = _run("backup.sh", env=_root_env(root))
    assert done.returncode == 1, done.stdout + done.stderr
    assert "is not valid TOML" in done.stderr


@needs_bash
def test_a_backup_job_writes_a_snapshot_restore_then_lists(tmp_path):
    root = _project(tmp_path)
    done = _run("backup.sh", env=_root_env(root))
    assert done.returncode == 0, done.stdout + done.stderr
    snaps = list((root / "backups").glob("lifecrm-*.db"))
    assert len(snaps) == 1
    done = _run("restore.sh", env=_root_env(root))         # no argument: list
    assert done.returncode == 0, done.stdout + done.stderr
    assert "available snapshots" in done.stdout and snaps[0].name in done.stdout


# ---------------------------------------------------------------- launchd plists

@needs_bash
def test_install_tasks_writes_seven_valid_launch_agents(tmp_path):
    agents = tmp_path / "LaunchAgents"
    done = _run("install-tasks.sh", "--no-load",
                env=_env(LIFECRM_LAUNCHAGENTS_DIR=agents.as_posix()))
    assert done.returncode == 0, done.stdout + done.stderr
    files = sorted(p.name for p in agents.iterdir())
    assert files == ["local.lifecrm.backup.plist", "local.lifecrm.collect.plist",
                     "local.lifecrm.doctor.plist",
                     "local.lifecrm.intake.plist", "local.lifecrm.reminders.plist",
                     "local.lifecrm.review.plist",
                     "local.lifecrm.verify-restore.plist"]
    jobs = {p.stem: plistlib.loads(p.read_bytes()) for p in agents.iterdir()}

    expected = {
        "local.lifecrm.intake": ("transcribe-inbox.sh", {"Hour": 7, "Minute": 0}),
        "local.lifecrm.backup": ("backup.sh", {"Hour": 21, "Minute": 30}),
        "local.lifecrm.doctor": ("doctor.sh", {"Hour": 8, "Minute": 0}),
        "local.lifecrm.verify-restore": ("verify-restore.sh",
                                         {"Weekday": 0, "Hour": 12, "Minute": 0}),
        # Monday after the rollover: the default period is then the week
        # that has just ended (test_fix_ops.py pins the reasoning).
        "local.lifecrm.review": ("review.sh", {"Weekday": 1, "Hour": 7, "Minute": 30}),
        # Daily after the rollover: an overdue reminder moves to this morning.
        "local.lifecrm.reminders": ("reminders.sh", {"Hour": 7, "Minute": 15}),
        # Before intake, the calendar and doctor: they see this morning's imports.
        "local.lifecrm.collect": ("collect.sh", {"Hour": 6, "Minute": 30}),
    }
    for label, (script, when) in expected.items():
        job = jobs[label]
        assert job["Label"] == label
        assert job["ProgramArguments"][0] == "/bin/bash"
        assert job["ProgramArguments"][1].endswith(f"scripts/{script}")
        assert job["StartCalendarInterval"] == when
        # _env sets LIFECRM_PYTHON, and launchd would not pass it on itself
        assert job["EnvironmentVariables"] == {
            "PYTHONUTF8": "1", "LIFECRM_PYTHON": Path(sys.executable).as_posix()}
        short = label.removeprefix("local.lifecrm.")
        assert job["StandardOutPath"].endswith(f"data/launchd-{short}.log")
        assert job["StandardErrorPath"] == job["StandardOutPath"]
        assert job["WorkingDirectory"] == job["ProgramArguments"][1].rsplit(
            "/scripts/", 1)[0]
    assert jobs["local.lifecrm.intake"]["RunAtLoad"] is True     # the "at login" run
    assert all("RunAtLoad" not in j for k, j in jobs.items()
               if k != "local.lifecrm.intake")


@needs_bash
def test_plists_escape_a_repo_path_with_xml_characters(tmp_path):
    # bash 5.2+ reads an unquoted '&' in a ${x//pat/rep} replacement as the
    # matched text; '<' and '>' cannot be in a Windows file name, '&' can.
    name = "Code & Stuff" if os.name == "nt" else "Code & <Stuff>"
    repo = tmp_path / name
    shutil.copytree(SCRIPTS, repo / "scripts")
    agents = tmp_path / "LaunchAgents"
    done = subprocess.run(
        [BASH, (repo / "scripts" / "install-tasks.sh").as_posix(), "--no-load"],
        env=_env(LIFECRM_LAUNCHAGENTS_DIR=agents.as_posix()),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    job = plistlib.loads((agents / "local.lifecrm.backup.plist").read_bytes())
    assert job["ProgramArguments"][1].endswith(f"/{name}/scripts/backup.sh")
    assert job["StandardOutPath"].endswith(f"/{name}/data/launchd-backup.log")


_BREW_PYTHONS = ("/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.12")


@needs_bash
@pytest.mark.skipif(any(Path(p).is_file() for p in _BREW_PYTHONS),
                    reason="setup.sh would find this machine's Python 3.12 and install")
def test_setup_warns_about_a_synced_folder_and_names_the_missing_python(tmp_path):
    home = tmp_path / "home"
    repo = home / "Documents" / "LifeCRM"
    shutil.copytree(SCRIPTS, repo / "scripts")
    env = _env(HOME=home.as_posix(),
               PATH=os.environ.get("SystemRoot", "C:/Windows") + "/System32"
               if os.name == "nt" else "/usr/bin:/bin")
    env.pop("LIFECRM_PYTHON")
    done = subprocess.run([BASH, (repo / "scripts" / "setup.sh").as_posix()], env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert done.returncode == 1
    assert "WARNING: this repo is under ~/Documents or ~/Desktop" in done.stderr
    assert "brew install python@3.12" in done.stderr
    assert not (repo / ".venv").exists() and not (repo / "config.toml").exists()


@needs_bash
def test_install_tasks_print_changes_nothing(tmp_path):
    agents = tmp_path / "LaunchAgents"
    done = _run("install-tasks.sh", "--print",
                env=_env(LIFECRM_LAUNCHAGENTS_DIR=agents.as_posix()))
    assert done.returncode == 0, done.stderr
    assert done.stdout.count("<?xml") == 7
    assert "local.lifecrm.verify-restore" in done.stdout
    assert not agents.exists()


@needs_bash
def test_install_tasks_rejects_an_unknown_option(tmp_path):
    done = _run("install-tasks.sh", "--frobnicate",
                env=_env(LIFECRM_LAUNCHAGENTS_DIR=(tmp_path / "a").as_posix()))
    assert done.returncode == 2 and "usage:" in done.stderr


@needs_bash
def test_uninstall_tasks_removes_what_install_wrote(tmp_path):
    agents = tmp_path / "LaunchAgents"
    env = _env(LIFECRM_LAUNCHAGENTS_DIR=agents.as_posix())
    assert _run("install-tasks.sh", "--no-load", env=env).returncode == 0
    done = _run("uninstall-tasks.sh", "--no-load", env=env)
    assert done.returncode == 0, done.stderr
    assert list(agents.iterdir()) == []
    assert done.stdout.count("removed: local.lifecrm.") == 7

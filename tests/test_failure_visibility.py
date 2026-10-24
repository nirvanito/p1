"""1.5 — failures that used to be silent.

Before this, a recording intake could not process was reported per file and
the command still exited 0; the scheduled-task scripts then reported success
regardless (measured: `powershell -File` returns 0 after a native command
exits non-zero unless the script passes the code on); a typo in an inbox path
simply meant nothing ever arrived; and a broken config.toml was a traceback.

The brief's done-when is end to end — "a poison file makes the scheduled task
report failure" — so the last tests run the real scripts through
powershell.exe against a throwaway project, with a genuinely poisonous
recording: whisper pointed at an empty model folder fails on it, offline, in
seconds.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT, cli_project
from typer.testing import CliRunner

from lifecrm import transcribe as tr
from lifecrm.cli import app

runner = CliRunner()
OLD = time.time() - 3 * 86400            # past intake's "still uploading" guard


def _setup(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    return root


def _aged(path: Path, when: float = OLD) -> Path:
    os.utime(path, (when, when))
    return path


def _model_fails(monkeypatch):
    def broken(cfg, model_name=None):
        raise RuntimeError("Unable to open file 'model.bin'")
    monkeypatch.setattr(tr, "load_model", broken)


# ---------------------------------------------------------------- exit codes

def test_intake_exits_1_when_a_file_fails_and_still_processes_the_rest(
        tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _model_fails(monkeypatch)
    inbox = root / "onedrive" / "inbox"
    (inbox / "2026-09-20 08.00.00.m4a").write_bytes(b"not really audio")
    _aged(inbox / "2026-09-20 08.00.00.m4a")
    note = root / "onedrive" / "inbox-text" / "2026-09-20 09.00.00.txt"
    note.write_text("Walked to the lake after lunch.", encoding="utf-8")
    _aged(note)

    r = runner.invoke(app, ["intake"])
    assert r.exit_code == 1
    assert "ERROR: 2026-09-20 08.00.00.m4a" in r.output
    assert "error: 1 file(s) need attention: 2026-09-20 08.00.00.m4a" in r.output
    assert "2026-09-20 09.00.00.txt: source id=" in r.output      # not blocked
    assert (inbox / "2026-09-20 08.00.00.m4a").exists()          # kept for retry


def test_transcribe_json_still_prints_then_exits_1(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    _model_fails(monkeypatch)
    audio = root / "onedrive" / "inbox" / "a.m4a"
    audio.write_bytes(b"x")
    _aged(audio)
    r = runner.invoke(app, ["transcribe", "--json"])
    assert r.exit_code == 1
    assert '"status": "failed"' in r.output


def test_a_clean_intake_still_exits_0(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["intake"])
    assert r.exit_code == 0 and "nothing to intake" in r.output


# ---------------------------------------------------------------- the archive

def test_a_missing_archive_root_is_an_error_not_a_new_folder(tmp_path, monkeypatch):
    """A typo'd archive_dir used to be created on the first archive, after
    which nothing could tell it was a typo."""
    root = _setup(tmp_path, monkeypatch)
    shutil.rmtree(root / "onedrive" / "archive")
    note = root / "onedrive" / "inbox-text" / "2026-09-21 10.00.00.txt"
    note.write_text("Called the landlord about the heater.", encoding="utf-8")
    _aged(note)

    r = runner.invoke(app, ["intake"])
    assert r.exit_code == 1
    assert "could not archive" in r.output and "does not exist" in r.output
    assert not (root / "onedrive" / "archive").exists()       # not conjured up
    assert note.exists()                                      # kept, not lost
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM sources", "--json"])
    assert json.loads(rows.output)[0]["c"] == 1                # but the data is in


# ---------------------------------------------------------------- import

def test_import_strips_a_byte_order_mark(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    f = root / "note.txt"
    f.write_bytes(b"\xef\xbb\xbf" + "Dinner at Nonna's.".encode("utf-8"))
    assert runner.invoke(app, ["import", str(f), "--date", "2026-09-22"]).exit_code == 0
    r = runner.invoke(app, ["sql", "SELECT text FROM transcripts", "--json"])
    assert json.loads(r.output)[0]["text"] == "Dinner at Nonna's."


def test_import_strips_a_bom_arriving_on_stdin(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["import", "--stdin", "--date", "2026-09-22"],
                      input="\N{ZERO WIDTH NO-BREAK SPACE}Piped from PowerShell.")
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["sql", "SELECT text FROM transcripts", "--json"])
    assert json.loads(r.output)[0]["text"] == "Piped from PowerShell."


def test_import_of_non_utf8_text_is_one_line(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    f = root / "latin1.txt"
    f.write_bytes("café".encode("latin-1"))
    r = runner.invoke(app, ["import", str(f)])
    assert r.exit_code == 1
    assert r.output.strip().startswith("error: latin1.txt is not UTF-8 text")
    assert "Traceback" not in r.output


# ---------------------------------------------------------------- config

@pytest.mark.parametrize("edit,expect", [
    (lambda t: t.replace("[time]", "[time\n"), "is not valid TOML"),
    (lambda t: t.replace('"America/Chicago"', '"America/Chicagoo"'),
     "timezone 'America/Chicagoo' is not a known IANA zone"),
    (lambda t: t.replace("day_rollover_hour = 5", "day_rollover_hour = 29"),
     "day_rollover_hour must be a whole hour 0-23"),
])
def test_a_broken_config_is_one_error_line(tmp_path, monkeypatch, edit, expect):
    root = _setup(tmp_path, monkeypatch)
    cfg_file = root / "config.toml"
    cfg_file.write_text(edit(cfg_file.read_text(encoding="utf-8")), encoding="utf-8")
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 1
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("error:") and expect in lines[0]


def test_a_missing_config_is_one_error_line(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFECRM_ROOT", str(tmp_path / "nowhere"))
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 1
    assert r.output.strip().startswith("error: no config.toml")


def test_a_locked_database_is_one_error_line(tmp_path, monkeypatch, capsys):
    """Through the real entry point, with a real write lock held elsewhere."""
    from lifecrm.__main__ import main
    root = _setup(tmp_path, monkeypatch)
    note = root / "n.txt"
    note.write_text("Something worth keeping.", encoding="utf-8")
    holder = sqlite3.connect(root / "data" / "lifecrm.db")
    holder.execute("BEGIN IMMEDIATE")                # a writer that will not let go
    try:
        monkeypatch.setattr(sys, "argv", ["lifecrm", "import", str(note)])
        with pytest.raises(SystemExit) as exit_info:
            main()
    finally:
        holder.rollback()
        holder.close()
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "error:" in err and "is locked by another program" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------- doctor

def _doctor(root):
    r = runner.invoke(app, ["doctor", "--json"])
    assert r.exit_code == 0, r.output       # warnings, not failures
    return {c["check"]: c for c in json.loads(r.output)["checks"]}


def test_doctor_warns_about_a_typo_in_an_inbox_path(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    cfg_file = root / "config.toml"
    cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace(
        "inbox-text'", "inbox-txet'"), encoding="utf-8")
    checks = _doctor(root)
    assert checks["configured_dirs"]["status"] == "warn"
    assert "inbox_text_dir" in checks["configured_dirs"]["detail"]
    assert "inbox-txet" in checks["configured_dirs"]["detail"]


def test_doctor_names_files_stuck_in_the_inbox(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    inbox = root / "onedrive" / "inbox"
    (inbox / "old.m4a").write_bytes(b"x")
    _aged(inbox / "old.m4a")
    (inbox / "fresh.m4a").write_bytes(b"x")                # just arrived: fine
    checks = _doctor(root)
    assert checks["inbox_stuck"]["status"] == "warn"
    assert "inbox_dir/old.m4a" in checks["inbox_stuck"]["detail"]
    assert "fresh.m4a" not in checks["inbox_stuck"]["detail"]


def test_doctor_names_files_intake_will_never_read(tmp_path, monkeypatch):
    root = _setup(tmp_path, monkeypatch)
    inbox = root / "onedrive" / "inbox"
    (inbox / "Notes 1.sdocx").write_bytes(b"x")           # Samsung Notes' own format
    (inbox / "desktop.ini").write_text("[.ShellClassInfo]")  # litter: ignored
    checks = _doctor(root)
    assert checks["inbox_unrecognized"]["status"] == "warn"
    detail = checks["inbox_unrecognized"]["detail"]
    assert "inbox_dir/Notes 1.sdocx" in detail and "desktop.ini" not in detail
    assert checks["inbox_stuck"]["status"] == "ok"          # not double-reported


def test_doctor_is_quiet_about_a_healthy_capture_side(tmp_path, monkeypatch):
    checks = _doctor(_setup(tmp_path, monkeypatch))
    for name in ("configured_dirs", "inbox_stuck", "inbox_unrecognized"):
        assert checks[name]["status"] == "ok", checks[name]


# ---------------------------------------------------------------- the real scripts

# The .ps1 scripts are the Windows setup's: they run .venv\Scripts\python.exe
# under Task Scheduler. A Mac can have PowerShell too (GitHub's macOS runners
# do), but its venv is .venv/bin — there the bash scripts are the ones tested.
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
needs_powershell = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None,
    reason="the .ps1 scripts are Windows-only (Windows PowerShell + .venv\\Scripts)")


def _project(tmp_path, *, toml: str | None = None) -> Path:
    """A throwaway project root the real scripts can be pointed at."""
    root = tmp_path / "project"
    onedrive = root / "onedrive"
    for d in ("inbox", "inbox-text", "archive", "backups"):
        (onedrive / d).mkdir(parents=True, exist_ok=True)
    empty_model = root / "empty-model"
    empty_model.mkdir()
    (root / "config.toml").write_text(toml if toml is not None else (
        "[paths]\n"
        'data_dir = "data"\n'
        'backups_dir = "backups"\n'
        f"inbox_dir = '{onedrive / 'inbox'}'\n"
        f"inbox_text_dir = '{onedrive / 'inbox-text'}'\n"
        f"archive_dir = '{onedrive / 'archive'}'\n"
        f"onedrive_backups_dir = '{onedrive / 'backups'}'\n"
        "[time]\n"
        'timezone = "America/Chicago"\n'
        "day_rollover_hour = 5\n"
        "[whisper]\n"
        f"model = '{empty_model}'\n"          # a model folder with no model in it
        "seed_initial_prompt = false\n"), encoding="utf-8")
    env = {**os.environ, "LIFECRM_ROOT": str(root), "PYTHONUTF8": "1",
           "HF_HUB_OFFLINE": "1"}
    subprocess.run([str(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"),
                    "-m", "lifecrm", "init"], env=env, check=True,
                   capture_output=True)
    return root


def _run_script(name: str, root: Path) -> subprocess.CompletedProcess:
    """Run a scheduled-task script the way Task Scheduler does: in a console
    on the system's OEM code page (437 on US Windows), not whatever code page
    the test runner's own console happens to have. The log's encoding only
    breaks under that condition, so that is the condition tested."""
    env = {**os.environ, "LIFECRM_ROOT": str(root), "HF_HUB_OFFLINE": "1"}
    script = PROJECT_ROOT / "scripts" / name
    command = (f'cmd /c "chcp 437 >nul & "{POWERSHELL}" -NoProfile '
               f'-ExecutionPolicy Bypass -File "{script}""')
    return subprocess.run(command, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


@needs_powershell
def test_a_poison_recording_makes_the_scheduled_task_report_failure(tmp_path):
    root = _project(tmp_path)
    poison = root / "onedrive" / "inbox" / "2026-09-23 Café.m4a"
    poison.write_bytes(bytes(64))
    _aged(poison)

    done = _run_script("transcribe-inbox.ps1", root)
    assert done.returncode == 1, done.stdout + done.stderr
    log = (root / "data" / "transcribe.log").read_text(encoding="utf-8-sig")
    assert "ERROR: 2026-09-23 Café.m4a" in log      # the name survives, not mojibake
    assert "— see the lines above" in log
    assert "FAILED (exit 1)" in log
    assert poison.exists()                          # left for the next run


@needs_powershell
def test_an_empty_inbox_run_reports_success(tmp_path):
    root = _project(tmp_path)
    done = _run_script("transcribe-inbox.ps1", root)
    assert done.returncode == 0, done.stdout + done.stderr
    log = (root / "data" / "transcribe.log").read_text(encoding="utf-8-sig")
    assert "nothing to intake" in log and "FAILED" not in log


@needs_powershell
def test_a_failed_backup_makes_the_scheduled_task_report_failure(tmp_path):
    root = _project(tmp_path)
    (root / "config.toml").write_text("[paths\nbroken", encoding="utf-8")
    done = _run_script("backup.ps1", root)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "is not valid TOML" in done.stderr

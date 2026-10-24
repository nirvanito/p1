"""Operations fixes in the scripts themselves, run for real through bash
(Git Bash on Windows) and powershell.exe.

Two kinds of run:

- the real scripts against a throwaway project (LIFECRM_ROOT), started from
  a folder that is not the repo, as Task Scheduler, launchd, Explorer's
  "Run with PowerShell" or a person in another terminal folder would;
- the demo scripts copied into a fake repo whose `lifecrm` package is a stub
  that records where it was started and with what — so the test sees the
  working directory and the arguments without building a demo, serving, or
  opening a browser.

Nothing registers a scheduled task or loads a launchd job: the plists go to
a temp folder (--no-load) and install-tasks.ps1 is checked as text.
"""

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT
from test_backups_scripts import _cli, _log
from test_backups_scripts import _run as _run_ps
from test_failure_visibility import POWERSHELL, needs_powershell
from test_failure_visibility import _project as _ps_project
from test_mac_scripts import BASH, _env, _root_env, needs_bash
from test_mac_scripts import _project as _sh_project
from test_mac_scripts import _run as _run_sh

SCRIPTS = PROJECT_ROOT / "scripts"
VENV = PROJECT_ROOT / ".venv"
STUB = (
    "import json, os, sys\n"
    "with open(os.environ['FIXOPS_RECORD'], 'a', encoding='utf-8') as f:\n"
    "    f.write(json.dumps({'cwd': os.getcwd(), 'argv': sys.argv[1:]}) + '\\n')\n"
)


def _fake_repo(tmp_path: Path, *scripts: str) -> Path:
    """A repo holding copies of `scripts` and a stub lifecrm package."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in scripts:
        shutil.copy2(SCRIPTS / name, repo / "scripts" / name)
    (repo / "lifecrm").mkdir()
    (repo / "lifecrm" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "lifecrm" / "__main__.py").write_text(STUB, encoding="utf-8")
    return repo


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _same(a: str | Path, b: Path) -> bool:
    return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(b.resolve()))


# ================================================================ PowerShell

def test_install_tasks_scopes_the_logon_trigger_to_the_current_user():
    """An unscoped -AtLogOn ("any user") needs an administrator: a normal
    session got "Access is denied" on the first task and registered none."""
    text = (SCRIPTS / "install-tasks.ps1").read_text(encoding="utf-8")
    logons = re.findall(r"New-ScheduledTaskTrigger -AtLogOn[^)]*", text)
    assert logons, "the intake task lost its logon trigger"
    assert all(re.search(r"-User \$Me\b", t) for t in logons), logons
    assert "[Security.Principal.WindowsIdentity]::GetCurrent().Name" in text
    assert re.search(r"New-ScheduledTaskPrincipal -UserId \$Me -LogonType Interactive "
                     r"-RunLevel Limited", text)
    assert re.search(r"Register-ScheduledTask [^\n]*`\n[^\n]*-Principal \$Principal", text)


def test_the_scripts_that_run_lifecrm_all_move_to_the_repo_root_first():
    """`python -m lifecrm` finds the package only from the repo root: the
    venv does not install it."""
    checked = set()
    for pattern, moves in (("*.ps1", ("Set-Location $Root",)),
                           ("*.sh", ('/_common.sh"', 'cd "$ROOT"'))):
        for p in sorted(SCRIPTS.glob(pattern)):
            text = "\n".join(ln for ln in p.read_text(encoding="utf-8").splitlines()
                             if not ln.lstrip().startswith("#"))     # code, not comments
            run = re.search(r'-m lifecrm|"-m", "lifecrm"|run_logged ', text)
            if run is None or p.name == "_common.sh":
                continue
            first_move = min((i for i in (text.find(m) for m in moves) if i >= 0),
                             default=-1)
            assert 0 <= first_move < run.start(), \
                f"{p.name} runs lifecrm before moving to the repo root"
            checked.add(p.name)
    assert {"demo.ps1", "transcribe-inbox.ps1", "backup.ps1", "restore.ps1",
            "demo.sh", "restore.sh"} <= checked


@needs_powershell
def test_the_changed_powershell_scripts_parse():
    names = ("demo.ps1", "transcribe-inbox.ps1", "backup.ps1", "restore.ps1",
             "verify-restore.ps1", "review.ps1", "install-tasks.ps1")
    check = "; ".join(
        f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{SCRIPTS / n}', [ref]$null, [ref]$e); if ($e.Count) {{ '{n}: ' + $e[0] }}"
        for n in names)
    done = subprocess.run([POWERSHELL, "-NoProfile", "-Command", check],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == "", done.stdout + done.stderr


@needs_powershell
def test_the_intake_task_works_from_system32(tmp_path):
    root = _ps_project(tmp_path)
    elsewhere = tmp_path / "System32"
    elsewhere.mkdir()
    done = _run_ps("transcribe-inbox.ps1", root, elsewhere)
    assert done.returncode == 0, done.stdout + done.stderr
    log = _log(root, "transcribe.log")
    assert "No module named lifecrm" not in log
    assert "nothing to intake" in log and "FAILED" not in log


@needs_powershell
def test_the_backup_task_works_from_system32(tmp_path):
    root = _ps_project(tmp_path)
    elsewhere = tmp_path / "System32"
    elsewhere.mkdir()
    done = _run_ps("backup.ps1", root, elsewhere)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len(list((root / "backups").glob("lifecrm-*.db"))) == 1


@needs_powershell
def test_restore_ps1_takes_a_snapshot_path_relative_to_where_it_was_run(tmp_path):
    root = _ps_project(tmp_path)
    _cli(root, "backup")
    snap = sorted((root / "backups").glob("lifecrm-*.db"))[-1]
    done = _run_ps("restore.ps1", root, root / "backups", snap.name)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"from {snap.name}" in done.stdout, done.stdout + done.stderr


@needs_powershell
def test_verify_restore_ps1_takes_a_relative_snapshot_path(tmp_path):
    root = _ps_project(tmp_path)
    _cli(root, "backup")
    snap = sorted((root / "backups").glob("lifecrm-*.db"))[-1]
    done = _run_ps("verify-restore.ps1", root, root / "backups", snap.name)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"verify-backup OK: {snap.name}" in _log(root, "verify-restore.log")


def _ps_fake_repo(tmp_path: Path) -> Path:
    """demo.ps1 runs `$Root\\.venv\\Scripts\\python.exe`: a copy of the venv's
    launcher (and its pyvenv.cfg) makes one in the fake repo — stdlib only,
    which is all the stub needs."""
    launcher = VENV / "Scripts" / "python.exe"
    if not launcher.is_file() or not (VENV / "pyvenv.cfg").is_file():
        pytest.skip("no project .venv with a Windows launcher")
    repo = _fake_repo(tmp_path, "demo.ps1")
    (repo / ".venv" / "Scripts").mkdir(parents=True)
    shutil.copy2(launcher, repo / ".venv" / "Scripts" / "python.exe")
    shutil.copy2(VENV / "pyvenv.cfg", repo / ".venv" / "pyvenv.cfg")
    return repo


def _demo_ps(repo: Path, cwd: Path, record: Path, *args: str):
    env = {**os.environ, "FIXOPS_RECORD": str(record)}
    env.pop("LIFECRM_ROOT", None)
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-File", str(repo / "scripts" / "demo.ps1"), *args],
                          cwd=str(cwd), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


@needs_powershell
def test_demo_ps1_builds_from_the_scripts_folder(tmp_path):
    """Explorer's "Run with PowerShell" starts in scripts\\."""
    repo = _ps_fake_repo(tmp_path)
    record = tmp_path / "calls.jsonl"
    done = _demo_ps(repo, repo / "scripts", record, "-NoServe")
    assert done.returncode == 0, done.stdout + done.stderr
    (call,) = _records(record)
    assert _same(call["cwd"], repo)
    # the windows-iphone branch's default: its own setup's demo
    assert call["argv"] == ["demo", "--combo", "windows-iphone"]


@needs_powershell
def test_demo_ps1_lets_serve_open_the_browser_once_it_listens(tmp_path):
    repo = _ps_fake_repo(tmp_path)
    marker = repo / "demo" / "windows-iphone" / ".lifecrm-demo"
    marker.parent.mkdir(parents=True)
    marker.write_text("lifecrm demo\n", encoding="utf-8")
    text = (repo / "scripts" / "demo.ps1").read_text(encoding="utf-8")
    assert "Start-Process" not in text            # nothing opens before it listens
    record = tmp_path / "calls.jsonl"
    done = _demo_ps(repo, tmp_path, record, "-Port", "8899")
    assert done.returncode == 0, done.stdout + done.stderr
    (call,) = _records(record)
    assert call["argv"][-4:] == ["serve", "--port", "8899", "--open"]
    assert _same(call["argv"][1], repo / "demo" / "windows-iphone")


# ================================================================ bash

def _demo_sh(repo: Path, cwd: Path, record: Path, *args: str):
    env = _env(FIXOPS_RECORD=str(record))
    return subprocess.run([BASH, (repo / "scripts" / "demo.sh").as_posix(), *args],
                          cwd=str(cwd), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


@needs_bash
def test_demo_sh_builds_from_any_folder(tmp_path):
    repo = _fake_repo(tmp_path, "demo.sh")
    elsewhere = tmp_path / "home"
    elsewhere.mkdir()
    record = tmp_path / "calls.jsonl"
    done = _demo_sh(repo, elsewhere, record, "--no-serve")
    assert done.returncode == 0, done.stdout + done.stderr
    (call,) = _records(record)
    assert _same(call["cwd"], repo)
    assert call["argv"] == ["demo", "--combo", "iphone"]


@needs_bash
def test_demo_sh_lets_serve_open_the_browser_once_it_listens(tmp_path):
    repo = _fake_repo(tmp_path, "demo.sh")
    marker = repo / "demo" / "iphone" / ".lifecrm-demo"
    marker.parent.mkdir(parents=True)
    marker.write_text("lifecrm demo\n", encoding="utf-8")
    text = (repo / "scripts" / "demo.sh").read_text(encoding="utf-8")
    assert "xdg-open" not in text and "sleep 2" not in text
    record = tmp_path / "calls.jsonl"
    done = _demo_sh(repo, tmp_path, record, "--port", "8898")
    assert done.returncode == 0, done.stdout + done.stderr
    (call,) = _records(record)
    assert call["argv"][-4:] == ["serve", "--port", "8898", "--open"]


def _snapshot_sh(root: Path) -> Path:
    done = _run_sh("backup.sh", env=_root_env(root))
    assert done.returncode == 0, done.stdout + done.stderr
    return sorted((root / "backups").glob("lifecrm-*.db"))[-1]


def _from(cwd: Path, script: str, *args: str, env: dict):
    return subprocess.run([BASH, (SCRIPTS / script).as_posix(), *args], cwd=str(cwd),
                          env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


@needs_bash
def test_restore_sh_takes_a_snapshot_path_relative_to_where_it_was_run(tmp_path):
    root = _sh_project(tmp_path)
    snap = _snapshot_sh(root)
    done = _from(root / "backups", "restore.sh", snap.name, env=_root_env(root))
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"from {snap.name}" in done.stdout


@needs_bash
def test_verify_restore_sh_takes_a_relative_snapshot_path(tmp_path):
    root = _sh_project(tmp_path)
    snap = _snapshot_sh(root)
    done = _from(root / "backups", "verify-restore.sh", snap.name, env=_root_env(root))
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"verify-backup OK: {snap.name}" in done.stdout


@needs_bash
def test_install_tasks_carries_lifecrm_python_into_every_plist(tmp_path):
    """launchd starts jobs with its own environment, not the shell's."""
    agents = tmp_path / "LaunchAgents"
    python = Path(sys.executable).as_posix()
    done = _run_sh("install-tasks.sh", "--no-load",
                   env=_env(LIFECRM_LAUNCHAGENTS_DIR=agents.as_posix(),
                            LIFECRM_PYTHON=python))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "warning" not in done.stderr
    plists = sorted(agents.glob("*.plist"))
    assert len(plists) == 5
    for p in plists:
        env = plistlib.loads(p.read_bytes())["EnvironmentVariables"]
        assert env == {"PYTHONUTF8": "1", "LIFECRM_PYTHON": python}, p.name


@needs_bash
def test_install_tasks_escapes_an_interpreter_path_with_xml_characters(tmp_path):
    agents = tmp_path / "LaunchAgents"
    odd = "/opt/R&D <tools>/bin/python3"
    done = _run_sh("install-tasks.sh", "--no-load",
                   env=_env(LIFECRM_LAUNCHAGENTS_DIR=agents.as_posix(),
                            LIFECRM_PYTHON=odd))
    assert done.returncode == 0, done.stdout + done.stderr
    job = plistlib.loads((agents / "local.lifecrm.review.plist").read_bytes())
    assert job["EnvironmentVariables"]["LIFECRM_PYTHON"] == odd


@needs_bash
def test_install_tasks_warns_about_an_interpreter_override_that_is_not_there(tmp_path):
    done = _run_sh("install-tasks.sh", "--no-load",
                   env=_env(LIFECRM_LAUNCHAGENTS_DIR=(tmp_path / "a").as_posix(),
                            LIFECRM_PYTHON=(tmp_path / "no-such-python").as_posix()))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "warning: LIFECRM_PYTHON=" in done.stderr
    assert "every job fails (exit 127)" in done.stderr


@needs_bash
def test_install_tasks_names_the_review_log(tmp_path):
    done = _run_sh("install-tasks.sh", "--no-load",
                   env=_env(LIFECRM_LAUNCHAGENTS_DIR=(tmp_path / "a").as_posix()))
    assert done.returncode == 0, done.stdout + done.stderr
    logs = next(ln for ln in done.stdout.splitlines() if ln.startswith("logs:"))
    assert "data/review.log" in logs

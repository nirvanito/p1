"""Privacy bundle (brief 1.7): what git can see, and what the dashboard says
when it is reachable from the network.

The old probe wrote sentinel files into the REAL data/ and backups/ and
deleted them afterwards — fine until someone extends it to config.toml or
.claude/settings.local.json, which are real files its cleanup would have
deleted. `git check-ignore --no-index` answers the same question about a
path whether or not it exists, so nothing is created anywhere.
"""

import shutil
import subprocess

import pytest
from conftest import PROJECT_ROOT, cli_project
from typer.testing import CliRunner

from lifecrm import cli, platforms
from lifecrm.cli import app

runner = CliRunner()

PRIVATE_PATHS = [
    "data/lifecrm.db",
    "data/staging/diff-2026-07-27-abcdef12.json",
    "data/transcripts/2026/2026-07-27-src1.txt",
    "data/dashboard/lifecrm.db",
    "data/web/lifecrm.db",
    "backups/lifecrm-20260727-213000.db",
    "anywhere/notes.jsonl",
    "exports/manifest.json",
    "exports/entries.jsonl",
    "archive/2026/2026-07-27.md",
    "archive-data/manifest.json",
    "x.zip",
    "downloads/health_connect_export.zip",
    "config.toml",
    ".claude/settings.local.json",
    "demo/anything",
    "demo/iphone/data/lifecrm.db",
    # Exports that arrive as something other than a zip, saved into the
    # project folder because the guides run imports from there.
    "contacts.vcf",
    "Contacts.VCF",
    "Home.ics",
    "Chase4417_Activity_20261021.CSV",
    "statement.ofx",
    "Statement.QFX",
    "statements/Chase4417_Activity.CSV",
    "statements/2026-09.ofx",
    "sms-20261021213000.xml",
    "calls-20261021213000.xml",
    "export.xml",
    "export_cda.xml",
    "apple_health_export/export.xml",
    "Timeline.json",
    "location-history.json",
    "Records.json",
    "conversations.json",
    "WhatsApp Chat with Mom.txt",
    "WhatsApp Chat - Jake Morales/WhatsApp Chat - Jake Morales.txt",
    "Takeout/Calendar/Home.ics",
    "takeout-20261021T120000Z-001/Takeout/Keep/note.json",
    "vault/Journal/2026/2026-10-21.md",
    "brief.md",
    "brief-sourced.txt",
    "kept.txt",
]

# Source that must stay visible — a too-greedy pattern is its own bug.
PUBLIC_PATHS = [
    "config.example.toml",
    "docs/privacy.md",
    "lifecrm/demo/persona.py",
    "lifecrm/demo/sources/__init__.py",
    "lifecrm/export.py",
    "tests/test_export.py",
    "dashboards/dashboards.yml",
    # the root-anchored export patterns must not reach source or fixtures
    "README.md",
    "CLAUDE.md",
    "AGENTS.md",
    "requirements.txt",
    "requirements-voice.txt",
    ".claude/settings.json",
    ".claude/skills/review-week/SKILL.md",
    ".github/workflows/ci.yml",
    "tests/fixtures/sample_day.json",
    "tests/fixtures/sample_day.txt",
    "tests/fixtures/ai_chat_claude.json",
    "tests/fixtures/contacts.vcf",
    "tests/fixtures/Home.ics",
    "tests/fixtures/statement.ofx",
    "tests/fixtures/sms-20261021213000.xml",
    "lifecrm/demo/sources/calendar.py",
    "lifecrm/demo/sources/finance.py",
    "docs/review.md",
]


def _check_ignore(path: str) -> int:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    if not (PROJECT_ROOT / ".git").exists():
        pytest.skip("project is not a git checkout")
    return subprocess.run(["git", "check-ignore", "-q", "--no-index", path],
                          cwd=PROJECT_ROOT, capture_output=True).returncode


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_private_paths_are_ignored(path):
    assert _check_ignore(path) == 0, f"{path} is visible to git — fix .gitignore"


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_source_paths_stay_visible_to_git(path):
    assert _check_ignore(path) == 1, f"{path} is ignored — .gitignore is too greedy"


# ---------------------------------------------------------------- dashboard host

def _dashboard(tmp_path, monkeypatch, *args):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    calls = []
    monkeypatch.setattr("lifecrm.cli.subprocess.call",
                        lambda a: calls.append(a) or 0)
    r = runner.invoke(app, ["dashboard", *args])
    assert r.exit_code == 0, r.output
    return r, calls


def test_the_default_loopback_host_prints_no_warning(tmp_path, monkeypatch):
    r, _ = _dashboard(tmp_path, monkeypatch)
    assert "warning" not in r.output
    assert "http://127.0.0.1:8001/-/dashboards" in r.output


def test_a_lan_host_warns_and_prints_the_url_a_phone_can_open(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_lan_ip", lambda: "192.168.1.23")
    r, calls = _dashboard(tmp_path, monkeypatch, "--host", "0.0.0.0", "--port", "8123")
    warning = [ln for ln in r.output.splitlines() if ln.startswith("warning:")]
    assert len(warning) == 1
    assert "anyone on this network can read the whole journal" in warning[0]
    assert "from your phone (same Wi-Fi): http://192.168.1.23:8123/-/dashboards" \
        in r.output
    assert "http://127.0.0.1:8123/-/dashboards" in r.output   # still usable locally
    (args,) = calls
    assert args[args.index("--host") + 1] == "0.0.0.0"


def test_a_lan_host_without_a_detectable_address_says_how_to_find_it(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_lan_ip", lambda: None)
    r, _ = _dashboard(tmp_path, monkeypatch, "--host", "0.0.0.0")
    assert "ipconfig" in r.output


@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.5.5.5", True),
    ("0.0.0.0", False), ("::", False), ("192.168.1.23", False), ("my-laptop", False),
])
def test_loopback_detection(host, loopback):
    assert cli._is_loopback(host) is loopback


def test_lan_ip_detection_never_returns_a_loopback_address():
    ip = cli._lan_ip()
    assert ip is None or not ip.startswith("127.")


def test_the_missing_datasette_hint_names_this_platforms_venv_python(
        tmp_path, monkeypatch):
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "datasette" else real(name, *a))
    cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["dashboard"])
    assert r.exit_code == 1
    assert f"{platforms.venv_python(cli.Path('.'))} -m pip install -r " \
           f"requirements.txt" in r.output

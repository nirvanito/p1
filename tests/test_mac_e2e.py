"""The Mac + iPhone setup end to end: every source in the place a Mac keeps
it, found by the daily job with no config, asked about, approved, collected
routinely after that — and the launchd job's script running it.

On a Mac (CI's macOS job) this is the real thing: the operating system's own
paths, file system and bash 3.2. Elsewhere the same flow runs with
`platforms.SYSTEM = "mac"`, every branch the code takes on a Mac short of
the operating system itself (connectors decide by `platforms.is_mac()`).

The sources are the demo's own generators (lifecrm/demo/sources), each
writing its app's real format for the iPhone persona, copied into a home
folder of the test's own — never this machine's.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from datetime import date

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm import db as db_mod
from lifecrm.doctor import run_checks
from test_mac_scripts import BASH, _env, _run, needs_bash

runner = CliRunner()
ON_A_MAC = sys.platform == "darwin"
# What a Mac with an iPhone has, and where `collect setup` lists it.
MAC_SOURCES = {"mac_messages", "ios_calls", "contacts", "notes", "photos", "browser",
               "health"}
ANCHOR = date(2026, 10, 20)


def _sample(name: str, dest):
    mod = importlib.import_module(f"lifecrm.demo.sources.{name}")
    dest.mkdir(parents=True, exist_ok=True)
    return mod.write_sample(dest, ANCHOR, "iphone")


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(platforms, "home", lambda: home)
    if not ON_A_MAC:
        monkeypatch.setattr(platforms, "SYSTEM", "mac")
    for var in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial", "LOCALAPPDATA",
                "APPDATA", "USERPROFILE"):
        monkeypatch.delenv(var, raising=False)
    from lifecrm.connectors import reading
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])

    gen = tmp_path / "generated"
    for name in ("mac_messages", "ios_calls", "browser"):   # written under Library/
        _sample(name, gen / name)
        shutil.copytree(gen / name, home, dirs_exist_ok=True)
    book = _sample("contacts", gen / "contacts")
    shutil.copytree(book, home / "Library" / "Application Support" / "AddressBook",
                    dirs_exist_ok=True)
    notes = platforms.mac_notes_db()
    notes.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_sample("notes", gen / "notes"), notes)
    library = platforms.mac_photos_db().parent.parent
    shutil.copytree(_sample("photos", gen / "photos"), library)
    (home / "Downloads").mkdir()
    shutil.copy2(_sample("apple_health", gen / "health"), home / "Downloads" / "export.zip")
    return home


def _json(args):
    r = runner.invoke(app, args)
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def test_a_mac_with_an_iphone_from_nothing_to_collected_every_morning(
        mac_home, tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0

    # morning one: every Mac source found, each a question, nothing written
    result = _json(["collect", "--json"])
    assert result["auto"]
    by = {s["source"]: s for s in result["sources"]}
    held = {n for n, s in by.items() if s["outcome"] == "held"}
    assert held == MAC_SOURCES, {n: (s["outcome"], (s.get("reason") or "")[:160])
                                 for n, s in by.items() if s["outcome"] != "skipped"}
    assert all(by[n]["hold"] == "first_import" for n in MAC_SOURCES)
    assert "Messages on this Mac" in by["mac_messages"]["reason"]
    assert by["health"]["target"].endswith("export.zip")

    checklist = _json(["collect", "setup", "--json"])
    assert checklist["phone"] == "iphone-mac" and checklist["title"] == "Mac + iPhone"
    assert {it["source"]: it["state"] for it in checklist["items"]} == {
        n: "waiting" for n in MAC_SOURCES}

    pack = _json(["nudges", "--json"])
    asked = {n["key"] for n in pack["nudges"] if n["kind"] == "collect"}
    assert asked == {f"collect:{n}:first_import" for n in MAC_SOURCES}

    # the owner's yes, source by source: the dry run, then the import
    for name in sorted(MAC_SOURCES):
        r = runner.invoke(app, ["collect", "approve", name])
        assert r.exit_code == 0 and "To import:" in r.output, r.output
        r = runner.invoke(app, ["collect", "approve", name, "--yes"])
        assert r.exit_code == 0 and "imported" in r.output, r.output

    # every morning after: routine, nothing asked
    again = {s["source"]: s["outcome"] for s in _json(["collect", "--json"])["sources"]}
    assert {n: again[n] for n in MAC_SOURCES} == {n: "unchanged" for n in MAC_SOURCES}
    assert not [n for n in _json(["nudges", "--json"])["nudges"] if n["kind"] == "collect"]
    assert {it["state"] for it in _json(["collect", "setup", "--json"])["items"]} == {
        "collecting"}

    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    try:
        report = run_checks(conn, cfg)
        assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
        streams = {r["stream"] for r in conn.execute(
            "SELECT DISTINCT stream FROM observations WHERE status='active'")}
        assert {"imessage", "ios_call", "contact_card", "note", "photo",
                "browser_visit"} <= streams, streams
        assert conn.execute("SELECT COUNT(*) FROM health_records").fetchone()[0] > 0
    finally:
        conn.close()


@needs_bash
@pytest.mark.skipif(not ON_A_MAC, reason="the launchd job's script, on the Mac it runs on")
def test_the_launchd_script_collects_on_a_mac(mac_home, tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    env = _env(LIFECRM_ROOT=root.as_posix(), HOME=mac_home.as_posix())
    done = _run("collect.sh", env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    log = (root / "data" / "collect.log").read_text(encoding="utf-8")
    assert "starting collect" in log and "on this computer" in log, log
    for name in MAC_SOURCES:
        assert f"{name} " in log and "held" in log, log

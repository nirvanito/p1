"""The newer sources, from the operator's side: `where` names every export
they read (by name only, opening nothing), `connectors` counts each
connector's own rows when two of them share a stream, the demo config says
whose reference intakes `nutrients` uses, and the demo's two food sources
agree on which app logged the meals.
"""

import json
import shutil
import subprocess
import tomllib
import zipfile
from datetime import date
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT, cli_project
from test_finance import CHASE_CARD, PRESETS, export
from test_payments import venmo
from typer.testing import CliRunner

from lifecrm import apple_health as ah
from lifecrm import nutrients
from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.config import Config, load_config
from lifecrm.connectors import reading
from lifecrm.demo import build as demo_build
from lifecrm.demo import persona
from lifecrm.demo.sources import apple_health as ah_sample

runner = CliRunner()


def _home(monkeypatch, tmp_path, system: str) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(platforms, "SYSTEM", system)
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: home))
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "APPDATA"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setenv("LIFECRM_ROOT", str(root))
    return home


def _groups(output: str) -> dict:
    return {g["title"]: g for g in json.loads(output)["groups"]}


def _commands(group: dict) -> list[str]:
    return [c["command"] for c in group["commands"]]


def _no_zip_opened(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("where must recognise exports by name, never open one")
    monkeypatch.setattr(zipfile, "ZipFile", refuse)


# ---------------------------------------------------------------- where

DOWNLOADS = {
    # file name -> the group it belongs to
    "takeout-20261021T190000Z-001.zip": "Google Takeout",
    "All mail Including Spam and Trash.mbox": "Mail archives (.mbox)",
    "instagram-alexrivera.atx-2026-10-21-dEm0iG7x.zip":
        "Instagram and Facebook Messenger exports",
    "facebook-alexrivera-2026-10-21-Qx81mZp.zip": "Instagram and Facebook Messenger exports",
    "strong.csv": "Workout exports (Strong, Hevy, Strava)",
    "workouts.csv": "Workout exports (Strong, Hevy, Strava)",
    "export_48213377.zip": "Workout exports (Strong, Hevy, Strava)",
    "my_spotify_data.zip": "Spotify listening history",
    "my_spotify_data (1).zip": "Spotify listening history",
    "goodreads_library_export.csv": "Reading (Kindle highlights, Goodreads)",
    "My Clippings.txt": "Reading (Kindle highlights, Goodreads)",
    "servings.csv": "Food diary exports (Cronometer, MyFitnessPal)",
    "dailysummary.csv": "Food diary exports (Cronometer, MyFitnessPal)",
    "Basic_LinkedInDataExport_10-21-2026.zip": "LinkedIn data export",
    "VenmoStatement_Oct_2026.csv": "Venmo and PayPal statements",
    "Download.CSV": "Venmo and PayPal statements",
    "Uber Data.zip": "Uber data export (rides and Eats)",
}
FOLDERS = {
    "INBOX.mbox": "Mail archives (.mbox)",
    "Telegram Desktop": "Telegram Desktop exports",
    "ChatExport_2026-10-21": "Telegram Desktop exports",
    "export_48213377": "Workout exports (Strong, Hevy, Strava)",
}
CONNECTOR = {
    "Mail archives (.mbox)": "email",
    "Instagram and Facebook Messenger exports": "meta_messages",
    "Telegram Desktop exports": "telegram",
    "Workout exports (Strong, Hevy, Strava)": "fitness",
    "Spotify listening history": "spotify",
    "Reading (Kindle highlights, Goodreads)": "reading",
    "Food diary exports (Cronometer, MyFitnessPal)": "nutrition",
    "LinkedIn data export": "linkedin",
    "Venmo and PayPal statements": "payments",
    "Uber data export (rides and Eats)": "rides",
}


def test_where_names_every_newer_export_and_the_command_that_reads_it(
        tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path, "windows")
    downloads = home / "Downloads"
    downloads.mkdir()
    for name in DOWNLOADS:
        (downloads / name).write_bytes(b"")
    for name in FOLDERS:
        (downloads / name).mkdir()
    # Names that only look like an export: a FILE where the export is a
    # folder, and a stray PDF beside Strava's archive.
    (downloads / "spotify").write_bytes(b"")
    (downloads / "export_notes.pdf").write_bytes(b"")
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])
    _no_zip_opened(monkeypatch)

    r = runner.invoke(app, ["where", "--json"])
    assert r.exit_code == 0, r.output
    groups = _groups(r.output)
    expected: dict[str, set[str]] = {}
    for name, title in {**DOWNLOADS, **FOLDERS}.items():
        expected.setdefault(title, set()).add(str(downloads / name))
    for title, paths in expected.items():
        g = groups[title]
        assert {i["path"] for i in g["items"]} == paths, title
        assert all(i["state"] == "found" for i in g["items"]), title
        assert all(c["available"] for c in g["commands"]), title
        if title in CONNECTOR:
            assert all(f"observe {CONNECTOR[title]} " in c for c in _commands(g)), title
    listed = {i["path"] for g in groups.values() for i in g["items"]}
    assert str(downloads / "spotify") not in listed
    assert str(downloads / "export_notes.pdf") not in listed


def test_one_takeout_is_offered_to_every_connector_that_reads_a_part_of_it(
        tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path, "windows")
    (home / "Downloads").mkdir()
    zip_path = home / "Downloads" / "takeout-20261021T190000Z-001.zip"
    zip_path.write_bytes(b"")
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])
    r = runner.invoke(app, ["where", "--json"])
    commands = _commands(_groups(r.output)["Google Takeout"])
    for connector in ("calendar", "email", "google_activity", "location", "notes",
                      "photos"):
        assert f'python -m lifecrm observe {connector} "{zip_path}"' in commands
    assert not any("observe browser" in c for c in commands)


def test_where_lists_a_plugged_in_kindle_first(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path, "windows")
    (home / "Downloads").mkdir()
    (home / "Downloads" / "goodreads_library_export.csv").write_bytes(b"")
    kindle = tmp_path / "E" / "documents" / "My Clippings.txt"
    kindle.parent.mkdir(parents=True)
    kindle.write_text("", encoding="utf-8")
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [kindle])
    r = runner.invoke(app, ["where", "--json"])
    g = _groups(r.output)["Reading (Kindle highlights, Goodreads)"]
    assert [i["label"] for i in g["items"]] == ["Kindle (plugged in)", "Goodreads export"]
    assert _commands(g)[0] == f'python -m lifecrm observe reading "{kindle}"'


def test_with_nothing_found_each_newer_source_still_shows_its_command(
        tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path, "windows")
    (home / "Downloads").mkdir()
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])
    r = runner.invoke(app, ["where", "--json"])
    groups = _groups(r.output)
    for title, connector in CONNECTOR.items():
        g = groups[title]
        assert not g["items"] and g["note"].startswith("none in"), title
        (cmd,) = g["commands"]
        assert cmd["command"].startswith(f"python -m lifecrm observe {connector} <")
        assert cmd["available"] is True, title
    assert groups["Reading (Kindle highlights, Goodreads)"]["note"].endswith(
        "no Kindle plugged in")
    text = runner.invoke(app, ["where"]).output
    assert "connector not in this version" not in text


def test_where_on_a_mac_reports_apple_mail_and_its_privacy_guard(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path, "mac")
    store = home / "Library" / "Mail"
    (store / "V10").mkdir(parents=True)
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])
    r = runner.invoke(app, ["where", "--json"])
    assert r.exit_code == 0, r.output
    mail = _groups(r.output)["Apple Mail"]
    assert mail["items"][0]["path"] == str(store) and mail["items"][0]["state"] == "found"
    assert _commands(mail) == ["python -m lifecrm observe email"]

    real_exists = Path.exists

    def exists(self, *a, **k):
        if self == store:
            raise PermissionError(1, "Operation not permitted", str(self))
        return real_exists(self, *a, **k)
    monkeypatch.setattr(Path, "exists", exists)
    item = _groups(runner.invoke(app, ["where", "--json"]).output)["Apple Mail"]["items"][0]
    assert item["state"] == "no access" and "Full Disk Access" in item["detail"]


def test_where_on_windows_has_no_apple_mail_group(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path, "windows")
    monkeypatch.setattr(reading, "_kindle_volumes", lambda: [])
    r = runner.invoke(app, ["where", "--json"])
    assert "Apple Mail" not in _groups(r.output)


# ---------------------------------------------------------------- connectors

def _stream_counts(output: str) -> dict[tuple[str, str], int]:
    counts, current = {}, None
    for line in output.splitlines():
        if not line.strip():
            continue
        if not line.startswith(" "):
            current = line.split()[0]
            continue
        stream, n = line.split()[:2]
        counts[(current, stream)] = int(n)
    return counts


def test_connectors_counts_only_the_rows_each_connectors_own_imports_created(
        tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    card = export(tmp_path / "statements", PRESETS[1][0], CHASE_CARD)
    r = runner.invoke(app, ["observe", "finance", str(card), "--apply"])
    assert r.exit_code == 0 and "3 added" in r.output, r.output
    r = runner.invoke(app, ["observe", "payments", str(venmo(tmp_path / "venmo")),
                            "--apply"])
    assert r.exit_code == 0 and "5 added" in r.output, r.output

    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0, r.output
    counts = _stream_counts(r.output)
    assert counts[("finance", "txn")] == 3
    assert counts[("payments", "txn")] == 5
    assert counts[("rides", "ride")] == 0

    # a retracted import's rows leave its own count, not the other's
    imports = json.loads(runner.invoke(app, ["imports", "list", "--json"]).output)
    uuid = next(i["uuid"] for i in imports if i["connector"] == "payments")
    assert runner.invoke(app, ["imports", "retract", uuid]).exit_code == 0
    counts = _stream_counts(runner.invoke(app, ["connectors"]).output)
    assert counts[("payments", "txn")] == 0 and counts[("finance", "txn")] == 3


# ---------------------------------------------------------------- the demo

@pytest.mark.parametrize("anchor, age", [(date(2026, 10, 22), 32),
                                         (date(2026, 4, 2), 32),
                                         (date(2026, 4, 1), 31),
                                         (date(2027, 1, 5), 32)])
def test_the_demo_config_names_whose_reference_intakes_to_use(tmp_path, anchor, age):
    text = demo_build._config_text(tmp_path, "android", anchor)
    section = tomllib.loads(text)["nutrition"]
    assert section == {"sex": "male", "age": age}


def test_the_demo_configs_nutrition_profile_needs_no_flag(tmp_path):
    (tmp_path / "config.toml").write_text(
        demo_build._config_text(tmp_path, "iphone", date(2026, 10, 22)), encoding="utf-8")
    prof = nutrients.profile(load_config(tmp_path))
    assert (prof["sex"], prof["age"]) == ("male", 32)
    assert prof["notes"] == []               # neither "sex not set" nor "age not set"


def test_both_food_sources_of_the_iphone_demo_name_one_app(tmp_path):
    """Apple Health's meals and the nutrition sample are the same diary."""
    assert ah_sample.FOOD_APP == "Cronometer"
    cfg = Config(root=tmp_path, data_dir=tmp_path / "data", backups_dir=tmp_path / "b",
                 inbox_dir=None, inbox_text_dir=None, archive_dir=None,
                 onedrive_backups_dir=None, timezone=persona.TIMEZONE,
                 day_rollover_hour=5)
    path = ah_sample.write_sample(tmp_path / "ah", date(2026, 10, 22), "iphone")
    recs = ah.read_export(path, cfg)["records"]
    food = {r["origin_app"] for r in recs if r["metric"] == "nutrition"}
    assert food == {"Cronometer"}


# ---------------------------------------------------------------- .gitignore

def test_no_committed_file_is_hidden_by_the_ignore_rules():
    """The root-anchored export patterns (/export_*/, /Spotify*/, /*.mbox,
    ...) must not reach anything already in the repository."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    if not (PROJECT_ROOT / ".git").exists():
        pytest.skip("project is not a git checkout")
    r = subprocess.run(["git", "ls-files", "-ci", "--exclude-standard"],
                       cwd=PROJECT_ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [], r.stdout

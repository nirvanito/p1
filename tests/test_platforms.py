"""Platform plumbing: where things live on Windows vs a Mac, the --root
option, connector discovery, and the demo persona's internal consistency.

These run on any OS; Mac-specific behavior is exercised by pointing the
functions at a fake home directory rather than by needing a Mac.
"""

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.connectors import base as conn_base
from lifecrm.demo import persona

runner = CliRunner()


# ---------------------------------------------------------------- platforms

def test_venv_python_matches_the_os(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    assert platforms.venv_python(tmp_path) == tmp_path / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(platforms, "SYSTEM", "mac")
    assert platforms.venv_python(tmp_path) == tmp_path / ".venv" / "bin" / "python"


def test_mac_journal_root_is_icloud_drive(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "SYSTEM", "mac")
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    root = platforms.default_journal_root()
    assert root == tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "VoiceJournal"


def test_icloud_placeholder_names():
    stub = Path(".2026-07-27 21.30.05 -0500.m4a.icloud")
    assert platforms.is_icloud_placeholder(stub)
    assert platforms.icloud_placeholder_target(stub) == "2026-07-27 21.30.05 -0500.m4a"
    assert not platforms.is_icloud_placeholder(Path("note.m4a"))


def test_mac_addressbook_finds_every_source(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: tmp_path))
    base = tmp_path / "Library" / "Application Support" / "AddressBook"
    (base / "Sources" / "A").mkdir(parents=True)
    (base / "Sources" / "B").mkdir(parents=True)
    for p in (base / "AddressBook-v22.abcddb",
              base / "Sources" / "A" / "AddressBook-v22.abcddb",
              base / "Sources" / "B" / "AddressBook-v22.abcddb"):
        p.write_bytes(b"")
    assert len(platforms.mac_addressbook_dbs()) == 3


def test_config_expands_home_in_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / "config.toml").write_text(
        "[paths]\ninbox_dir = '~/VoiceJournal/inbox'\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.inbox_dir == tmp_path / "VoiceJournal" / "inbox"


# ---------------------------------------------------------------- cli plumbing

def test_root_option_points_commands_at_another_root(tmp_path, monkeypatch):
    monkeypatch.delenv("LIFECRM_ROOT", raising=False)
    (tmp_path / "config.toml").write_text(
        '[paths]\ndata_dir = "data"\nbackups_dir = "backups"\n', encoding="utf-8")
    r = runner.invoke(app, ["--root", str(tmp_path), "init"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "data" / "lifecrm.db").exists()


def test_connectors_are_discovered_not_listed():
    reg = conn_base.registry()
    assert {"ai_chat", "ios_messages"} <= set(reg)
    assert "typedstream" not in reg and "base" not in reg
    for name, mod in reg.items():
        assert mod.name == name and callable(mod.scan) and mod.streams


# ---------------------------------------------------------------- persona

def test_persona_story_is_internally_consistent():
    anchor = date(2026, 10, 21)
    keys = {p.key for p in persona.PEOPLE} | {"me"}
    place_keys = {p.key for p in persona.PLACES}
    for t in persona.THREADS:
        for _, _, sender, _ in t.messages:
            assert sender in keys
    for j in persona.JOURNAL:
        assert -persona.WINDOW_DAYS < j.offset <= 0
        for n in j.notes:
            for _, spec in n.sentences:
                for ref, _role in (spec or {}).get("entities", []):
                    if ":" not in ref:
                        assert ref in keys | place_keys, ref
    trip = persona.bled_trip_days(anchor)
    assert trip[0].year == anchor.year - 4 and len(trip) == 5
    series = persona.health_series(anchor)
    assert len(series) == persona.WINDOW_DAYS and series[-1].date == anchor
    assert series == persona.health_series(anchor)          # deterministic
    said = {j.offset: j.sleep_hours for j in persona.JOURNAL if j.sleep_hours}
    for h in series:
        off = (h.date - anchor).days
        if off in said:
            assert h.sleep_hours == pytest.approx(said[off], abs=0.01)


def test_persona_keys_referenced_by_closures_exist():
    keyed = {spec["key"] for j in persona.JOURNAL for n in j.notes
             for _, spec in n.sentences if spec and "key" in spec}
    for j in persona.JOURNAL:
        for n in j.notes:
            for _, spec in n.sentences:
                if not spec:
                    continue
                if "closes" in spec:
                    assert spec["closes"] in keyed
                for _t, k in spec.get("links", []):
                    assert k in keyed, k

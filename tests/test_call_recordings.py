"""Call recordings are transcribed only by choice: intake holds every file
named like one, `status` and `calls list` show them, `calls take`
transcribes the picked ones into ordinary sources, `calls skip` archives
them untranscribed, and nothing about a held recording reaches the database
until then. [intake] call_recordings = "auto" restores the old behaviour."""

import json
from dataclasses import replace

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import transcribe as tr
from lifecrm.cli import app
from lifecrm.config import ConfigError, load_config
from lifecrm.doctor import run_checks
from test_android_filenames import _age, fake_whisper  # noqa: F401 — the fixture

runner = CliRunner()
MOM = "Call recording Mom_261020_200512.m4a"
JAKE = "Call recording Jake Morales_261021_131500.m4a"
NOTE = "Recording_20261021_213005.m4a"


def _drop(folder, *names):
    for i, name in enumerate(names):
        p = folder / name
        p.write_bytes(f"audio-{i}-{name}".encode())
        _age(p)
    return [folder / n for n in names]


def _ask(cfg, **intake):
    return replace(cfg, extra={**(cfg.extra or {}), "intake": intake})


# ---------------------------------------------------------------- recognizing one

@pytest.mark.parametrize("name", [
    MOM, JAKE, "Call recording +15125550142_261020_200512.3gp",
    "call recording mom.m4a", "Call with Linda 2026-10-20 20-05.m4a",
    "Call_+15125550142_20261020-200512.m4a", "call-20261020.mp3",
    "261020_200512.m4a",
])
def test_names_a_call_recorder_writes_are_calls(cfg, name):
    assert tr.is_call_recording(name, cfg)


@pytest.mark.parametrize("name", [
    NOTE, "Voice 001.m4a", "2026-10-20 21.30.05.m4a", "Callie birthday idea.m4a",
    "callback list.m4a", "Recording_20261020_200512.m4a",
])
def test_voice_notes_are_not(cfg, name):
    assert not tr.is_call_recording(name, cfg)


def test_call_patterns_add_a_recorder_of_your_own(cfg):
    assert not tr.is_call_recording("ACR_Mom_2026.m4a", cfg)
    assert tr.is_call_recording("ACR_Mom_2026.m4a", _ask(cfg, call_patterns=["acr_*"]))
    with pytest.raises(ConfigError, match=r"call_patterns must be a list"):
        tr.is_call_recording("x.m4a", _ask(cfg, call_patterns=[3]))


def test_who_comes_from_the_name_for_display_only():
    assert tr.call_who(MOM) == "Mom"
    assert tr.call_who(JAKE) == "Jake Morales"
    assert tr.call_who("Call recording +15125550142_261020_200512.m4a") == "+15125550142"
    assert tr.call_who("261020_200512.m4a") is None


def test_a_bad_mode_is_one_config_line(cfg):
    with pytest.raises(ConfigError, match=r'call_recordings must be "ask" or "auto"'):
        tr.call_mode(_ask(cfg, call_recordings="sometimes"))


# ---------------------------------------------------------------- intake holds them

def test_intake_holds_calls_and_transcribes_notes(conn, cfg, fake_whisper):
    _drop(cfg.inbox_dir, MOM, NOTE)
    results = tr.transcribe_inbox(conn, cfg)
    assert [r["file"] for r in results] == [NOTE]
    assert (cfg.inbox_dir / MOM).exists()                   # untouched, in place
    assert [p.name for p in tr.held_calls(cfg)] == [MOM]
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_take_transcribes_only_the_picked_ones(conn, cfg, fake_whisper):
    _drop(cfg.inbox_dir, MOM, JAKE)
    picked = tr.resolve_held(cfg, ["jake"])
    assert [p.name for p in picked] == [JAKE]
    (res,) = tr.transcribe_inbox(conn, cfg, files=picked)
    assert res["status"] == "transcribed" and res["journal_date"] == "2026-10-21"
    src = conn.execute("SELECT type, path FROM sources").fetchone()
    assert src["type"] == "audio" and src["path"].endswith(JAKE)
    assert [p.name for p in tr.held_calls(cfg)] == [MOM]   # Mom's still waits


def test_picks_by_number_name_or_all(cfg):
    _drop(cfg.inbox_dir, JAKE, MOM)
    held = [p.name for p in tr.held_calls(cfg)]
    assert held == [MOM, JAKE]                               # oldest first
    assert [p.name for p in tr.resolve_held(cfg, ["2"])] == [JAKE]
    assert [p.name for p in tr.resolve_held(cfg, [MOM.upper()])] == [MOM]
    assert [p.name for p in tr.resolve_held(cfg, [], all_=True)] == held
    with pytest.raises(tr.DomainError, match="matches 2 held recordings"):
        tr.resolve_held(cfg, ["Call recording"])
    with pytest.raises(tr.DomainError, match="no held call recording number 3"):
        tr.resolve_held(cfg, ["3"])
    with pytest.raises(tr.DomainError, match="no held call recording matches 'Sam'"):
        tr.resolve_held(cfg, ["Sam"])
    with pytest.raises(tr.DomainError, match="or pass --all"):
        tr.resolve_held(cfg, [])


def test_skip_archives_untranscribed_and_writes_nothing(conn, cfg):
    _drop(cfg.inbox_dir, MOM)
    (res,) = tr.skip_calls(cfg, tr.resolve_held(cfg, ["1"]))
    assert res["status"] == "skipped"
    assert (cfg.archive_dir / "2026" / MOM).exists()
    assert not (cfg.inbox_dir / MOM).exists()
    for table in ("sources", "transcripts", "audit_log"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_auto_mode_transcribes_calls_like_any_note(conn, cfg, fake_whisper):
    auto = _ask(cfg, call_recordings="auto")
    _drop(cfg.inbox_dir, MOM)
    assert tr.held_calls(auto) == []
    assert [r["status"] for r in tr.transcribe_inbox(conn, auto)] == ["transcribed"]


def test_a_named_file_is_already_a_choice(conn, cfg, fake_whisper):
    (mom,) = _drop(cfg.inbox_dir, MOM)
    (res,) = tr.transcribe_inbox(conn, cfg, only_file=mom)
    assert res["status"] == "transcribed"


# ---------------------------------------------------------------- doctor

def test_doctor_says_a_held_call_waits_for_a_pick_not_that_intake_fails(conn, cfg):
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(days=10)
    for p in _drop(cfg.inbox_dir, MOM):
        _age(p, old)
    checks = {c["check"]: c for c in run_checks(conn, cfg)["checks"]}
    assert checks["inbox_stuck"]["status"] == "ok"
    assert checks["calls_held"]["status"] == "warn"
    assert MOM in checks["calls_held"]["detail"] and "calls take" in checks["calls_held"]["detail"]


def test_doctor_reports_a_bad_call_setting_instead_of_crashing(conn, cfg):
    checks = {c["check"]: c for c in
              run_checks(conn, _ask(cfg, call_recordings="maybe"))["checks"]}
    assert checks["calls_held"]["status"] == "warn"
    assert "call_recordings" in checks["calls_held"]["detail"]


# ---------------------------------------------------------------- the CLI

def _project(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    return root, load_config(root)


def test_intake_status_list_take_skip_end_to_end(tmp_path, monkeypatch, fake_whisper):
    root, cfg = _project(tmp_path, monkeypatch)
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    _drop(cfg.inbox_dir, MOM, JAKE, NOTE)

    done = runner.invoke(app, ["intake"])
    assert done.exit_code == 0, done.output
    assert f"{NOTE}: source id=" in done.output
    assert f"{MOM}: held — a call recording with Mom" in done.output
    assert "2 call recording(s) held" in done.output

    status = json.loads(runner.invoke(app, ["status", "--json"]).stdout)
    assert [c["file"] for c in status["held_calls"]] == [MOM, JAKE]
    assert status["pending_audio"] == []                    # nothing intake will do
    assert status["held_calls"][0]["who"] == "Mom"

    listed = runner.invoke(app, ["calls", "list"])
    assert listed.exit_code == 0 and " 1. " in listed.stdout and "Mom" in listed.stdout

    took = runner.invoke(app, ["calls", "take", "2"])
    assert took.exit_code == 0, took.output
    assert f"{JAKE}: source id=" in took.stdout and "/log" in took.stdout

    skipped = runner.invoke(app, ["calls", "skip", "Mom"])
    assert skipped.exit_code == 0, skipped.output
    assert "archived untranscribed" in skipped.stdout
    assert runner.invoke(app, ["calls", "list"]).stdout.startswith("no call recordings held")

    status = json.loads(runner.invoke(app, ["status", "--json"]).stdout)
    assert status["held_calls"] == []
    assert len(status["unprocessed_sources"]) == 2           # the note and Jake's call


def test_take_with_nothing_named_says_how(tmp_path, monkeypatch):
    root, cfg = _project(tmp_path, monkeypatch)
    _drop(cfg.inbox_dir, MOM)
    done = runner.invoke(app, ["calls", "take"])
    assert done.exit_code == 1 and "or pass --all" in done.output
    assert (cfg.inbox_dir / MOM).exists()


def test_intake_json_lists_held_calls(tmp_path, monkeypatch):
    root, cfg = _project(tmp_path, monkeypatch)
    _drop(cfg.inbox_dir, MOM)
    done = runner.invoke(app, ["intake", "--json"])
    assert done.exit_code == 0
    (row,) = json.loads(done.stdout)
    assert row["status"] == "held" and row["who"] == "Mom"

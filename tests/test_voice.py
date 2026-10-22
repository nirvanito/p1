"""The voice loop: notes land in the quick-note inbox, questions get the same
cited answer as `ask`, and neither writes the database.

No microphone, whisper model or speech engine is used for real here: the
contract under test is the routing, the note's name (which decides its
journal date), and that the loop never becomes a write path. Speech output
and capture are in test_voice_speech.py.
"""

import hashlib
import json
import os
import sys
import time
import wave
from array import array
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from conftest import cli_project

from lifecrm import transcribe as tr
from lifecrm import voice
from lifecrm.config import ConfigError
from lifecrm.domain import entities, entries, health
from lifecrm.domain.util import DomainError


def _age(path, seconds=3600):
    """Backdate mtime so intake's still-uploading guard doesn't skip it."""
    t = time.time() - seconds
    os.utime(path, (t, t))


def _tone(seconds=1.0, amplitude=6000) -> bytes:
    n = int(voice.SAMPLE_RATE * seconds)
    return array("h", [amplitude if (i // 20) % 2 else -amplitude
                       for i in range(n)]).tobytes()


# ---------------------------------------------------------------- routing

@pytest.mark.parametrize("utterance,body", [
    ("Note: had lunch with Jake at Veracruz.", "Had lunch with Jake at Veracruz."),
    ("note that Maya's birthday dinner is Friday", "Maya's birthday dinner is Friday"),
    ("Note to self, call Mom on Sunday.", "Call Mom on Sunday."),
    ("remember that Jake grew up in El Paso", "Jake grew up in El Paso"),
    ("Remember to renew the passport.", "Remember to renew the passport."),
    ("Log that I ran 5k this morning.", "I ran 5k this morning."),
    ("Journal. Long day, the board deck is finally done.",
     "Long day, the board deck is finally done."),
    ("journal entry: slept badly", "Slept badly"),
    ("  NOTE — dentist moved to Tuesday", "Dentist moved to Tuesday"),
])
def test_note_triggers_become_notes_without_the_command_word(utterance, body):
    r = voice.route(utterance)
    assert (r.kind, r.text) == ("note", body)


@pytest.mark.parametrize("utterance", [
    "how did I sleep last week",
    "What's Jake's favorite restaurant?",
    "notes from Tuesday",                    # 'notes' is not the trigger 'note'
    "Notebook ideas this month",
    "journaling streak this month",
    "remembered anything about Bled",
    "Remember when we went to Lake Bled?",   # a question, whatever its first word
    "when did I last log that I ran",        # trigger words later on don't count
])
def test_everything_else_is_a_question(utterance):
    r = voice.route(utterance)
    assert r.kind == "question"
    assert r.text == " ".join(utterance.split())


@pytest.mark.parametrize("utterance", ["", "   ", "...", "?"])
def test_blank_utterances_route_to_nothing(utterance):
    assert voice.route(utterance).kind == "empty"


def test_a_bare_command_word_writes_nothing(cfg):
    turn = voice.handle("Note.", cfg=cfg, prefs=voice.settings(cfg),
                        open_conn=_no_db, use_llm=False)
    assert turn["route"] == "empty"
    assert list(cfg.inbox_text_dir.iterdir()) == []


def _no_db():
    raise AssertionError("a note must never open the database")


# ---------------------------------------------------------------- note files

def test_note_file_is_named_like_the_quick_note_shortcut(cfg):
    now = datetime(2026, 10, 21, 19, 5, 33, tzinfo=timezone.utc)   # 14:05:33 CDT
    note = voice.write_note(cfg, "Had lunch with Jake at Veracruz.", now=now)
    path = cfg.inbox_text_dir / "2026-10-21 14.05.33 -0500.txt"
    assert note["path"] == str(path)
    assert note["journal_date"] == "2026-10-21"
    assert note["recorded_at"] == "2026-10-21T19:05:33Z"
    assert path.read_text(encoding="utf-8") == "Had lunch with Jake at Veracruz.\n"


def test_note_before_rollover_files_under_the_previous_day(cfg):
    """00:30 on the 22nd belongs to the 21st's journal; the clock time stays
    real, and intake's parser recovers the true instant from the name."""
    now = datetime(2026, 10, 22, 5, 30, 0, tzinfo=timezone.utc)    # 00:30 CDT
    note = voice.write_note(cfg, "Couldn't sleep, thinking about the deck.", now=now)
    assert note["file"] == "2026-10-21 00.30.00 -0500.txt"
    assert tr.resolve_file_times(cfg.inbox_text_dir / note["file"], cfg) \
        == ("2026-10-21", "2026-10-22T05:30:00Z")


def test_note_name_carries_the_winter_offset(cfg):
    now = datetime(2026, 12, 1, 18, 0, 0, tzinfo=timezone.utc)     # 12:00 CST
    assert voice.write_note(cfg, "Passport photos done.", now=now)["file"] \
        == "2026-12-01 12.00.00 -0600.txt"


def test_two_notes_in_one_second_never_overwrite(cfg):
    now = datetime(2026, 10, 21, 19, 5, 33, tzinfo=timezone.utc)
    first = voice.write_note(cfg, "First thought.", now=now)
    second = voice.write_note(cfg, "Second thought.", now=now)
    assert second["file"] == "2026-10-21 14.05.33 -0500-2.txt"
    assert (cfg.inbox_text_dir / first["file"]).read_text(encoding="utf-8") \
        == "First thought.\n"
    # the suffix must not confuse intake's date and time parsing
    assert tr.resolve_file_times(cfg.inbox_text_dir / second["file"], cfg) \
        == ("2026-10-21", "2026-10-21T19:05:33Z")


def test_note_needs_an_existing_text_inbox_and_never_creates_one(cfg, tmp_path):
    from dataclasses import replace
    with pytest.raises(DomainError, match="inbox_text_dir is not configured"):
        voice.write_note(replace(cfg, inbox_text_dir=None), "x")
    missing = tmp_path / "typo-inbox"
    with pytest.raises(DomainError, match="does not exist"):
        voice.write_note(replace(cfg, inbox_text_dir=missing), "x")
    assert not missing.exists()


def test_voice_note_flows_through_intake_like_a_phone_note(conn, cfg):
    """The note is an ordinary inbox file: intake imports it as a
    phone_transcript with the instant it was spoken, and doctor stays green."""
    from lifecrm.doctor import run_checks
    now = datetime(2026, 10, 21, 19, 5, 33, tzinfo=timezone.utc)
    turn = voice.handle("Note that Jake's favorite restaurant is Veracruz All Natural.",
                        cfg=cfg, prefs=voice.settings(cfg), open_conn=_no_db,
                        use_llm=False, now=now)
    assert turn["route"] == "note" and turn["say"] == "Noted."
    _age(cfg.inbox_text_dir / turn["note"]["file"])

    results = tr.intake_text_inbox(conn, cfg)
    assert [r["status"] for r in results] == ["imported"]
    src = conn.execute("SELECT type, journal_date, recorded_at FROM sources").fetchone()
    assert tuple(src) == ("phone_transcript", "2026-10-21", "2026-10-21T19:05:33Z")
    text = conn.execute("SELECT text FROM transcripts").fetchone()["text"]
    assert text == "Jake's favorite restaurant is Veracruz All Natural."
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"]
                                     if c["status"] == "fail"]


# ---------------------------------------------------------------- settings

def test_voice_settings_default_to_low_latency(cfg):
    prefs = voice.settings(cfg)
    assert prefs["model"] == "small"
    assert prefs["use_llm"] is True and prefs["speak"] is True
    assert prefs["wake_model"] == "hey_jarvis"
    assert prefs["max_spoken_chars"] == 400


def test_voice_settings_reject_bad_values_by_name(cfg):
    from dataclasses import replace
    assert voice.settings(replace(cfg, extra={"voice": {"use_llm": "false"}}))[
        "use_llm"] is False
    with pytest.raises(ConfigError, match=r"\[voice\] use_llm"):
        voice.settings(replace(cfg, extra={"voice": {"use_llm": "maybe"}}))
    with pytest.raises(ConfigError, match=r"\[voice\] silence_sec"):
        voice.settings(replace(cfg, extra={"voice": {"silence_sec": "long"}}))


# ---------------------------------------------------------------- CLI

def _seed(conn):
    for i, (day, minutes) in enumerate([("2026-07-25", 430), ("2026-07-26", 465),
                                        ("2026-07-27", 388), ("2026-07-28", 442)]):
        health.insert(conn, {"metric": "sleep_session", "journal_date": day,
                             "started_at": f"{day}T03:00:00Z",
                             "ended_at": f"{day}T11:00:00Z", "value": minutes,
                             "unit": "min", "external_id": f"v{i:02d}" * 8,
                             "source_file": "hc.zip"})
    jake = entities.create(conn, "person", "Jake Morales")
    dinner = entries.create(conn, journal_date="2026-07-25", kind="event",
                            title="Dinner with Jake at Veracruz All Natural",
                            primary_category="social")
    entries.attach_entity(conn, dinner["id"], jake["id"], role="participant")
    conn.commit()


@pytest.fixture
def voice_root(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lifecrm import db as db_mod
    from lifecrm.cli import app

    root = cli_project(tmp_path, monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    conn = db_mod.connect(root / "data" / "lifecrm.db")
    _seed(conn)
    conn.close()
    monkeypatch.setattr("lifecrm.llm.status", lambda cfg: {
        "available": False, "reason": "no local model (test)",
        "model": "test", "models": []})
    spoken: list[str] = []
    monkeypatch.setattr(voice, "speak",
                        lambda text, prefs, **kw: spoken.append(text) or "test")
    return SimpleNamespace(runner=runner, app=app, root=root, spoken=spoken,
                           inbox=root / "onedrive" / "inbox-text",
                           db=root / "data" / "lifecrm.db")


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_text_note_never_touches_the_database(voice_root, monkeypatch):
    before = _digest(voice_root.db)

    def no_connect(*a, **kw):
        raise AssertionError("a voice note opened the database")
    monkeypatch.setattr("lifecrm.db.connect", no_connect)
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "note bought tulips for Maya", "--no-speak"])
    assert r.exit_code == 0, r.output
    assert "noted ->" in r.output and "nothing is applied" in r.output
    [note] = list(voice_root.inbox.iterdir())
    assert note.read_text(encoding="utf-8") == "Bought tulips for Maya\n"
    assert _digest(voice_root.db) == before


def test_cli_text_note_works_before_the_database_exists(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from lifecrm.cli import app
    root = cli_project(tmp_path, monkeypatch)
    r = CliRunner().invoke(app, ["voice", "--text", "remember to call Mom",
                                 "--no-speak"])
    assert r.exit_code == 0, r.output
    assert len(list((root / "onedrive" / "inbox-text").iterdir())) == 1
    assert not (root / "data" / "lifecrm.db").exists()


def test_cli_text_question_answers_with_citations_and_speaks_without_them(voice_root):
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "how did I sleep in July 2026"])
    assert r.exit_code == 0, r.output
    assert "Sleep totalled" in r.output and "[M1]" in r.output
    assert "sources:" in r.output and "[M1] sleep:" in r.output
    assert "window: July 2026" in r.output and "answer: deterministic" in r.output
    [said] = voice_root.spoken
    assert "Sleep totalled" in said
    assert "[" not in said and " h " not in said and "hours" in said
    assert list(voice_root.inbox.iterdir()) == []


def test_cli_questions_never_write_the_database(voice_root):
    """Unlike `ask`, the voice loop does not even log the question."""
    before = _digest(voice_root.db)
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "what did I eat at Veracruz", "--no-speak"])
    assert r.exit_code == 0, r.output
    assert "Dinner with Jake at Veracruz All Natural [E1]" in r.output
    assert _digest(voice_root.db) == before
    from lifecrm import db as db_mod
    conn = db_mod.connect(voice_root.db, readonly=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM recall_log").fetchone()[0] == 0
    finally:
        conn.close()


def test_cli_json_turn_carries_the_ask_shape(voice_root):
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "what did I eat at Veracruz", "--json"])
    assert r.exit_code == 0, r.output
    turn = json.loads(r.output)
    assert turn["route"] == "question"
    assert turn["answer"]["answer_source"] == "deterministic"
    assert turn["answer"]["citations"]["cited"] == ["E1"]
    assert set(turn["answer"]) >= {"question", "answer", "citations", "evidence",
                                   "evidence_text", "model"}
    assert "[E1]" not in turn["say"] and "Veracruz" in turn["say"]
    assert voice_root.spoken == []                 # --json speaks nothing

    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "log that Sam spotted me on bench", "--json"])
    turn = json.loads(r.output)
    assert turn["route"] == "note"
    assert turn["note"]["text"] == "Sam spotted me on bench"


def test_cli_model_answer_with_invented_citations_is_never_spoken(voice_root,
                                                                   monkeypatch):
    monkeypatch.setattr("lifecrm.llm.status", lambda cfg: {
        "available": True, "reason": "", "model": "fake:8b", "models": ["fake:8b"]})
    monkeypatch.setattr("lifecrm.llm.answer",
                        lambda cfg, evidence, question, model=None:
                        "You slept 9 hours a night [M7].")
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "how did I sleep in July 2026"])
    assert r.exit_code == 0, r.output
    assert "cited evidence that does not exist (M7)" in r.output
    assert "answer: deterministic (model answer rejected)" in r.output
    [said] = voice_root.spoken
    assert "slept 9 hours" not in said and "Sleep totalled" in said


def test_cli_model_answer_with_real_citations_is_spoken(voice_root, monkeypatch):
    monkeypatch.setattr("lifecrm.llm.status", lambda cfg: {
        "available": True, "reason": "", "model": "fake:8b", "models": ["fake:8b"]})
    monkeypatch.setattr("lifecrm.llm.answer",
                        lambda cfg, evidence, question, model=None:
                        "You slept about 7 hours a night [M1].")
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "how did I sleep in July 2026"])
    assert r.exit_code == 0, r.output
    assert "answer: model:fake:8b" in r.output
    assert voice_root.spoken == ["You slept about 7 hours a night."]


def test_cli_no_llm_never_probes_for_a_model(voice_root, monkeypatch):
    def fail(cfg):
        raise AssertionError("--no-llm must not probe for a model")
    monkeypatch.setattr("lifecrm.llm.status", fail)
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "how did I sleep in July 2026", "--no-llm", "--no-speak"])
    assert r.exit_code == 0, r.output


def test_cli_requires_something_to_say(voice_root):
    r = voice_root.runner.invoke(voice_root.app, ["voice", "--text", "  "])
    assert r.exit_code == 1 and "say what?" in r.output
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--text", "x", "--file", "y.wav"])
    assert r.exit_code == 1 and "not both" in r.output


# ---------------------------------------------------------------- --file

class FakeWhisper:
    """faster-whisper's surface: transcribe(path, **kw) -> (segments, info).
    It opens the WAV, so the test also sees what the loop recorded."""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def transcribe(self, path, **kw):
        with wave.open(path, "rb") as w:
            self.calls.append({"rate": w.getframerate(),
                               "channels": w.getnchannels(), **kw})
        seg = SimpleNamespace(start=0.0, end=2.0, text=" " + self.text)
        return iter([seg]), SimpleNamespace(duration=2.0, language="en")


@pytest.fixture
def fake_model(monkeypatch):
    loaded: list = []
    model = FakeWhisper("Remember that Jake's favorite restaurant is Veracruz All Natural.")

    def load_model(cfg, model_name=None):
        loaded.append(model_name)
        return model
    monkeypatch.setattr(tr, "load_model", load_model)
    model.loaded = loaded
    return model


def test_cli_file_transcribes_with_the_voice_model_and_whisper_language(
        voice_root, fake_model):
    with open(voice_root.root / "config.toml", "a", encoding="utf-8") as f:
        f.write('language = "en"\n[voice]\nmodel = "base.en"\n')
    clip = voice_root.root / "clip.wav"
    voice.write_wav(clip, _tone(1.0))
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--file", str(clip), "--no-speak"])
    assert r.exit_code == 0, r.output
    assert "heard: \"Remember that Jake's favorite" in r.output
    assert fake_model.loaded == ["base.en"]
    [call] = fake_model.calls
    assert call["language"] == "en" and call["vad_filter"] is True
    assert "Jake Morales" in call["initial_prompt"]      # known names seed whisper
    [note] = list(voice_root.inbox.iterdir())
    assert note.read_text(encoding="utf-8") == \
        "Jake's favorite restaurant is Veracruz All Natural.\n"
    assert clip.exists()                 # unlike `transcribe --file`, never archived


def test_cli_file_must_exist(voice_root):
    r = voice_root.runner.invoke(voice_root.app, [
        "voice", "--file", str(voice_root.root / "nope.wav")])
    assert r.exit_code == 1 and "file not found" in r.output


def test_cli_file_reports_a_transcription_failure(voice_root, monkeypatch):
    def broken(cfg, model_name=None):
        raise RuntimeError("model download blocked")
    monkeypatch.setattr(tr, "load_model", broken)
    clip = voice_root.root / "clip.wav"
    voice.write_wav(clip, _tone(1.0))
    r = voice_root.runner.invoke(voice_root.app, ["voice", "--file", str(clip)])
    assert r.exit_code == 1
    assert "transcription failed: model download blocked" in r.output


# ---------------------------------------------------------------- microphone

class FakeStream:
    """sounddevice.RawInputStream: on enter, 'records' a second of tone."""

    opened: list[dict] = []

    def __init__(self, **kw):
        FakeStream.opened.append(kw)
        self.callback = kw.get("callback")

    def __enter__(self):
        pcm = _tone(1.0)
        step = voice.FRAME_SAMPLES * 2
        for i in range(0, len(pcm), step):
            self.callback(pcm[i:i + step], voice.FRAME_SAMPLES, None, None)
        return self

    def __exit__(self, *exc):
        return False


def test_missing_sounddevice_says_how_to_install(voice_root, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    r = voice_root.runner.invoke(voice_root.app, ["voice", "--once", "--no-speak"])
    assert r.exit_code == 1
    assert "pip install -r" in r.output and "requirements-voice.txt" in r.output
    assert "--file note.wav" in r.output and "--text" in r.output


def test_missing_openwakeword_says_how_to_install(voice_root, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice",
                        SimpleNamespace(RawInputStream=FakeStream))
    monkeypatch.setitem(sys.modules, "openwakeword", None)
    monkeypatch.setitem(sys.modules, "openwakeword.model", None)
    r = voice_root.runner.invoke(voice_root.app, ["voice", "--wake", "--once"])
    assert r.exit_code == 1
    assert "openwakeword" in r.output and "pip install" in r.output


def test_push_to_talk_records_16k_mono_and_routes_the_note(voice_root, fake_model,
                                                          monkeypatch):
    FakeStream.opened.clear()
    monkeypatch.setitem(sys.modules, "sounddevice",
                        SimpleNamespace(RawInputStream=FakeStream))
    r = voice_root.runner.invoke(voice_root.app, ["voice", "--once", "--no-speak"],
                                 input="\n\n")
    assert r.exit_code == 0, r.output
    [opened] = FakeStream.opened
    assert (opened["samplerate"], opened["channels"], opened["dtype"]) \
        == (16000, 1, "int16")
    [call] = fake_model.calls
    assert (call["rate"], call["channels"]) == (16000, 1)
    assert len(list(voice_root.inbox.iterdir())) == 1


def test_push_to_talk_loop_ends_cleanly_when_input_closes(voice_root, fake_model,
                                                         monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice",
                        SimpleNamespace(RawInputStream=FakeStream))
    r = voice_root.runner.invoke(voice_root.app, ["voice", "--no-speak"],
                                 input="\n\n")       # one turn, then EOF
    assert r.exit_code == 0, r.output
    assert len(fake_model.calls) == 1

import os
import time
from datetime import datetime, timezone

import pytest

from lifecrm import transcribe as tr
from lifecrm.domain import sources as sources_mod


def _age(path, seconds=3600):
    """Backdate mtime so the still-uploading guard doesn't skip the file."""
    t = time.time() - seconds
    os.utime(path, (t, t))


FAKE_RESULT = {
    "text": "Went bouldering with Jake. Sent the blue V4.",
    "segments": [
        {"start_sec": 0.0, "end_sec": 2.5, "char_start": 0, "char_end": 26,
         "text": "Went bouldering with Jake."},
        {"start_sec": 2.5, "end_sec": 4.0, "char_start": 27, "char_end": 44,
         "text": "Sent the blue V4."},
    ],
    "duration_sec": 4.0,
    "language": "en",
}


@pytest.fixture
def fake_whisper(monkeypatch):
    """No model load, canned transcription result."""
    monkeypatch.setattr(tr, "load_model", lambda cfg, model_name=None: object())
    monkeypatch.setattr(
        tr, "transcribe_audio",
        lambda cfg, model, path, initial_prompt=None: dict(FAKE_RESULT))


# ---------------------------------------------------------------- filename parsing

def test_recorded_at_with_seconds_and_offset():
    # 21.30.05 CDT (-0500) -> 02:30:05 UTC next calendar day
    assert tr.recorded_at_from_filename(
        "2026-07-27 21.30.05 -0500.m4a", "2026-07-27", "America/Chicago", 5) \
        == "2026-07-28T02:30:05Z"


def test_recorded_at_without_offset_uses_config_timezone():
    # July: America/Chicago is CDT = UTC-5
    assert tr.recorded_at_from_filename(
        "2026-07-27 21.30.m4a", "2026-07-27", "America/Chicago", 5) \
        == "2026-07-28T02:30:00Z"


def test_recorded_at_early_morning_undoes_rollover():
    """A 00.30 clock time on journal date 2026-07-27 really happened on the
    28th — the Shortcut rolled the date back; the parser must roll it forward."""
    assert tr.recorded_at_from_filename(
        "2026-07-27 00.30.00 -0500.m4a", "2026-07-27", "America/Chicago", 5) \
        == "2026-07-28T05:30:00Z"


def test_recorded_at_no_time_returns_none():
    assert tr.recorded_at_from_filename(
        "2026-07-27.m4a", "2026-07-27", "America/Chicago", 5) is None
    assert tr.recorded_at_from_filename(
        "voice memo 42.m4a", "2026-07-27", "America/Chicago", 5) is None


def test_time_regex_rejects_impossible_times():
    assert tr._TIME_RE.search("27.75.99") is None
    assert tr._TIME_RE.search("2026.07.27") is None  # date-with-dots is not a time


# ---------------------------------------------------------------- precedence pin

def test_filename_date_precedence(conn, cfg, fake_whisper):
    """PINNED: the filename date wins over mtime+rollover — the phone knows
    things the file mtime does not. A refactor that inverts this breaks here."""
    audio = cfg.inbox_dir / "2026-07-20 21.30.00.m4a"
    audio.write_bytes(b"fake-audio")
    _age(audio)  # mtime = now (2026-07-27ish) -> mtime would say a different day

    results = tr.transcribe_inbox(conn, cfg)
    assert results[0]["status"] == "transcribed"
    assert results[0]["journal_date"] == "2026-07-20"
    src = conn.execute("SELECT * FROM sources WHERE id=?",
                       (results[0]["source_id"],)).fetchone()
    assert src["journal_date"] == "2026-07-20"
    # clock time comes from the filename, not the OneDrive upload mtime
    assert src["recorded_at"] == "2026-07-21T02:30:00Z"


def test_mtime_fallback_when_no_filename_date(conn, cfg, fake_whisper):
    audio = cfg.inbox_dir / "memo.m4a"
    audio.write_bytes(b"fake-audio-2")
    t = datetime(2026, 7, 21, 2, 30, tzinfo=timezone.utc).timestamp()  # 21:30 CDT Jul 20
    os.utime(audio, (t, t))
    results = tr.transcribe_inbox(conn, cfg)
    assert results[0]["journal_date"] == "2026-07-20"
    src = conn.execute("SELECT recorded_at FROM sources WHERE id=?",
                       (results[0]["source_id"],)).fetchone()
    assert src["recorded_at"] == "2026-07-21T02:30:00Z"


# ---------------------------------------------------------------- archive-after-commit

def test_failed_ingest_leaves_file_in_inbox(conn, cfg, fake_whisper, monkeypatch):
    """F1c: a transaction failure must NOT strand the file outside the inbox."""
    audio = cfg.inbox_dir / "2026-07-20 21.30.00.m4a"
    audio.write_bytes(b"fake-audio")
    _age(audio)

    def boom(*a, **k):
        raise RuntimeError("simulated transcript-write failure")
    monkeypatch.setattr(tr.sources, "add_transcript", boom)

    results = tr.transcribe_inbox(conn, cfg)
    assert results[0]["status"] == "failed"
    assert audio.exists(), "file must remain in the inbox for the next run"
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0
    assert not list(cfg.archive_dir.rglob("*.m4a"))


def test_archive_failure_after_commit_is_recoverable(conn, cfg, fake_whisper,
                                                     monkeypatch):
    """Rows committed but the move fails: warn, keep the file, and the next run
    archives it via the duplicate branch without re-transcribing."""
    audio = cfg.inbox_dir / "2026-07-20 21.30.00.m4a"
    audio.write_bytes(b"fake-audio")
    _age(audio)

    real_archive = tr.archive_file

    def flaky_archive(cfg_, path, journal_date):
        raise OSError("simulated OneDrive lock")
    monkeypatch.setattr(tr, "archive_file", flaky_archive)
    results = tr.transcribe_inbox(conn, cfg)
    assert results[0]["status"] == "transcribed"
    assert any("could not archive" in w for w in results[0]["warnings"])
    assert audio.exists()
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1

    # next run: duplicate branch moves it, creating nothing new
    monkeypatch.setattr(tr, "archive_file", real_archive)
    results2 = tr.transcribe_inbox(conn, cfg)
    assert results2[0]["status"] == "duplicate"
    assert not audio.exists()
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1


def test_success_path_archives_and_updates_source_path(conn, cfg, fake_whisper):
    audio = cfg.inbox_dir / "2026-07-20 21.30.00.m4a"
    audio.write_bytes(b"fake-audio")
    _age(audio)
    results = tr.transcribe_inbox(conn, cfg)
    archived = cfg.archive_dir / "2026" / audio.name
    assert archived.exists() and not audio.exists()
    src = conn.execute("SELECT path FROM sources WHERE id=?",
                       (results[0]["source_id"],)).fetchone()
    assert src["path"] == str(archived)


# ---------------------------------------------------------------- text intake (F1b)

def test_text_intake_creates_phone_transcript_source(conn, cfg):
    note = cfg.inbox_text_dir / "2026-07-27 14.05.33 -0500.txt"
    note.write_text("Quick thought after lunch. Follow up with Dana about the "
                    "renewal. Energy dipped around 2pm.", encoding="utf-8")
    _age(note)

    results = tr.intake_text_inbox(conn, cfg)
    assert results[0]["status"] == "imported"
    src = conn.execute("SELECT * FROM sources WHERE id=?",
                       (results[0]["source_id"],)).fetchone()
    assert src["type"] == "phone_transcript"
    assert src["journal_date"] == "2026-07-27"
    assert src["recorded_at"] == "2026-07-27T19:05:33Z"   # 14:05 CDT
    assert src["timezone"] == "America/Chicago"

    segs = conn.execute(
        "SELECT * FROM transcript_segments WHERE transcript_id=? ORDER BY idx",
        (results[0]["transcript_id"],)).fetchall()
    assert len(segs) == 3
    assert all(s["start_sec"] is None for s in segs)      # imported: no timestamps
    text = conn.execute("SELECT text FROM transcripts WHERE id=?",
                        (results[0]["transcript_id"],)).fetchone()["text"]
    for s in segs:
        assert text[s["char_start"]:s["char_end"]] == s["text"]

    archived = cfg.archive_dir / "2026" / note.name
    assert archived.exists() and not note.exists()
    assert src["path"] is not None  # updated to archive location after commit
    row = conn.execute("SELECT path FROM sources WHERE id=?",
                       (results[0]["source_id"],)).fetchone()
    assert row["path"] == str(archived)


def test_text_intake_duplicate_archived_not_reingested(conn, cfg):
    body = "Same note synced twice somehow."
    a = cfg.inbox_text_dir / "2026-07-27 14.05.33.txt"
    a.write_text(body, encoding="utf-8")
    _age(a)
    tr.intake_text_inbox(conn, cfg)

    b = cfg.inbox_text_dir / "2026-07-27 14.05.33 copy.txt"
    b.write_text(body + "\r\n", encoding="utf-8")   # CRLF round trip, same content
    _age(b)
    results = tr.intake_text_inbox(conn, cfg)
    assert results[0]["status"] == "duplicate"
    assert not b.exists()
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 1


def test_text_intake_multiple_notes_one_day(conn, cfg):
    for name, body in [("2026-07-27 09.15.00.txt", "Morning standup went long."),
                       ("2026-07-27 14.05.00.txt", "Lunch walk, felt better."),
                       ("2026-07-27 21.40.00.txt", "Wrapped the evening review.")]:
        p = cfg.inbox_text_dir / name
        p.write_text(body, encoding="utf-8")
        _age(p)
    results = tr.intake_text_inbox(conn, cfg)
    assert [r["status"] for r in results] == ["imported"] * 3
    days = conn.execute("SELECT DISTINCT journal_date FROM sources").fetchall()
    assert [d["journal_date"] for d in days] == ["2026-07-27"]
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 3


def test_text_intake_empty_file_archived(conn, cfg):
    p = cfg.inbox_text_dir / "2026-07-27 09.15.00.txt"
    p.write_text("   \n", encoding="utf-8")
    _age(p)
    results = tr.intake_text_inbox(conn, cfg)
    assert results[0]["status"] == "empty"
    assert not p.exists()
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0


def test_fresh_files_are_skipped(conn, cfg):
    """A file modified seconds ago may still be uploading — skip this run."""
    p = cfg.inbox_text_dir / "2026-07-27 09.15.00.txt"
    p.write_text("Still uploading maybe.", encoding="utf-8")  # fresh mtime
    assert tr.scan_text_inbox(cfg) == []
    a = cfg.inbox_dir / "2026-07-27 09.15.00.m4a"
    a.write_bytes(b"x")
    assert tr.scan_inbox(cfg) == []

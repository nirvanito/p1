from conftest import sample_text

from lifecrm.domain.sources import content_hash_text, duplicate_warnings
from lifecrm.importer import import_text, segment_text


def test_segments_cover_sentences_with_offsets():
    text = "First sentence. Second one! Was it a question? Yes."
    segs = segment_text(text)
    assert [s["text"] for s in segs] == [
        "First sentence.", "Second one!", "Was it a question?", "Yes."]
    for s in segs:
        assert text[s["char_start"]:s["char_end"]] == s["text"]


def test_decimals_do_not_split():
    text = "Ran 3.5 miles today. Felt great."
    segs = segment_text(text)
    assert [s["text"] for s in segs] == ["Ran 3.5 miles today.", "Felt great."]


def test_newlines_split():
    text = "line one has no punctuation\nline two."
    segs = segment_text(text)
    assert [s["text"] for s in segs] == ["line one has no punctuation", "line two."]
    assert all(text[s["char_start"]:s["char_end"]] == s["text"] for s in segs)


def test_trailing_text_without_punctuation():
    segs = segment_text("Done. And then")
    assert [s["text"] for s in segs] == ["Done.", "And then"]


def test_import_creates_source_transcript_segments(conn, cfg):
    result = import_text(conn, cfg, sample_text(), journal_date="2026-07-27",
                         source_type="phone_transcript")
    assert result["warnings"] == []
    src = conn.execute("SELECT * FROM sources WHERE id=?",
                       (result["source_id"],)).fetchone()
    assert src["type"] == "phone_transcript"
    assert src["timezone"] == "America/Chicago"     # per-source timezone at ingest
    assert src["journal_date"] == "2026-07-27"
    assert src["content_hash"] == content_hash_text(sample_text())

    segs = conn.execute(
        "SELECT * FROM transcript_segments WHERE transcript_id=? ORDER BY idx",
        (result["transcript_id"],)).fetchall()
    assert len(segs) >= 5
    text = conn.execute("SELECT text FROM transcripts WHERE id=?",
                        (result["transcript_id"],)).fetchone()["text"]
    for s in segs:
        # imported segments: NULL timestamps, populated char offsets (required)
        assert s["start_sec"] is None and s["end_sec"] is None
        assert text[s["char_start"]:s["char_end"]] == s["text"]

    # human-browsable transcript copy exists
    from pathlib import Path
    assert Path(result["transcript_file"]).exists()


def test_import_duplicate_warns_but_does_not_block(conn, cfg):
    first = import_text(conn, cfg, sample_text(), journal_date="2026-07-27")
    assert first["warnings"] == []
    second = import_text(conn, cfg, sample_text(), journal_date="2026-07-27")
    assert any("identical content" in w for w in second["warnings"])
    n = conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    assert n == 2   # warned, not blocked


def test_soft_duplicate_check_on_time_and_duration(conn, cfg):
    from lifecrm.domain import days, sources
    days.ensure_day(conn, "2026-07-27")
    src = sources.create(conn, source_type="audio", timezone_name="America/Chicago",
                         journal_date="2026-07-27",
                         recorded_at="2026-07-27T21:30:00Z")
    sources.add_transcript(conn, source_id=src["id"], engine="external",
                           text="hello there.", duration_sec=300,
                           segments=segment_text("hello there."))
    warnings = duplicate_warnings(
        conn, content_hash=None, journal_date="2026-07-27",
        recorded_at="2026-07-27T21:31:00Z", duration_sec=302)
    assert any("similar recording time and duration" in w for w in warnings)
    # outside tolerance: no warning
    assert duplicate_warnings(
        conn, content_hash=None, journal_date="2026-07-27",
        recorded_at="2026-07-27T23:00:00Z", duration_sec=302) == []


def test_audio_duplicate_hash_detected(conn, cfg, tmp_path):
    """Byte-identical audio re-synced to the inbox is archived, not re-transcribed."""
    from lifecrm.domain import days, sources as sources_mod
    from lifecrm.transcribe import process_file
    audio = cfg.inbox_dir / "2026-07-27 2130.m4a"
    audio.write_bytes(b"fake-audio-bytes")
    days.ensure_day(conn, "2026-07-27")
    sources_mod.create(conn, source_type="audio", timezone_name="America/Chicago",
                       journal_date="2026-07-27",
                       content_hash=sources_mod.content_hash_bytes(b"fake-audio-bytes"))
    result = process_file(conn, cfg, get_model=lambda: None, path=audio)  # never loads for dups
    assert result["status"] == "duplicate"
    assert not audio.exists()
    assert (cfg.archive_dir / "2026" / audio.name).exists()


def test_date_from_filename():
    from lifecrm.transcribe import date_from_filename
    assert date_from_filename("2026-07-27 2130.m4a") == "2026-07-27"
    assert date_from_filename("rec_20260727_213000.mp3") == "2026-07-27"
    assert date_from_filename("note 2026_07_27.wav") == "2026-07-27"
    assert date_from_filename("voice memo 42.m4a") is None
    assert date_from_filename("20261399.m4a") is None   # not a real date

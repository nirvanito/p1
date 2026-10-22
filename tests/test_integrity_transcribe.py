"""Brief 2.3: transcribe_audio had never executed — every other test swaps it
out — so its char-offset arithmetic was unpinned. A fake model feeds the
real function a segment iterator shaped like faster-whisper's; the offsets
must index the joined text exactly, and the result must pass the transcript
bounds check it will meet in production. Plus the date override, which now
happens inside process_file before anything is written."""

from types import SimpleNamespace

import pytest

from lifecrm import transcribe as tr
from lifecrm.domain import sources
from lifecrm.domain.util import DomainError
from test_transcribe import FAKE_RESULT, _age


class FakeModel:
    """faster-whisper's WhisperModel.transcribe: a LAZY segment generator
    plus an info object."""

    def __init__(self, segments, duration=7.456, language="en"):
        self.segments = segments
        self.info = SimpleNamespace(duration=duration, language=language)
        self.kw = None

    def transcribe(self, path, **kw):
        self.path, self.kw = path, kw
        return iter(self.segments), self.info


SEGS = [SimpleNamespace(start=0.0, end=2.345, text=" Went bouldering."),
        SimpleNamespace(start=2.345, end=3.0, text="   "),          # silence: dropped
        SimpleNamespace(start=3.0, end=5.555, text=" Sent the V4. ")]


def test_transcribe_audio_pins_the_char_offset_arithmetic(cfg, tmp_path):
    model = FakeModel(SEGS)
    out = tr.transcribe_audio(cfg, model, tmp_path / "a.m4a")
    assert out["text"] == "Went bouldering. Sent the V4."
    assert [s["text"] for s in out["segments"]] == ["Went bouldering.", "Sent the V4."]
    for s in out["segments"]:
        assert out["text"][s["char_start"]:s["char_end"]] == s["text"]
    second = out["segments"][1]
    assert second["char_start"] == len("Went bouldering.") + 1   # one joining space
    assert second["char_end"] == len(out["text"])
    assert out["segments"][0]["end_sec"] == 2.35                # rounded to 2 places
    assert (second["start_sec"], second["end_sec"]) == (3.0, 5.55)
    assert out["duration_sec"] == 7.46 and out["language"] == "en"


def test_whisper_options_come_from_config(cfg, tmp_path):
    model = FakeModel(SEGS)
    cfg.whisper.update(language="", vad_filter=False)
    tr.transcribe_audio(cfg, model, tmp_path / "a.m4a", initial_prompt="Names: Jake.")
    assert model.kw == {"language": None, "vad_filter": False,
                        "initial_prompt": "Names: Jake."}
    assert model.path == str(tmp_path / "a.m4a")

    cfg.whisper.update(language="en", vad_filter=True)
    tr.transcribe_audio(cfg, model, tmp_path / "a.m4a")
    assert model.kw == {"language": "en", "vad_filter": True, "initial_prompt": None}


def test_the_real_result_passes_the_transcript_bounds_check(conn, cfg, tmp_path):
    out = tr.transcribe_audio(cfg, FakeModel(SEGS), tmp_path / "a.m4a")
    src = sources.create(conn, source_type="audio", timezone_name=cfg.timezone,
                         journal_date="2026-07-27")
    tid = sources.add_transcript(conn, source_id=src["id"], engine="faster-whisper",
                                 text=out["text"], segments=out["segments"],
                                 duration_sec=out["duration_sec"])
    rows = conn.execute("SELECT idx, char_start, char_end, text FROM transcript_segments "
                        "WHERE transcript_id=? ORDER BY idx", (tid,)).fetchall()
    assert [(r["char_start"], r["char_end"]) for r in rows] == [(0, 16), (17, 29)]


def test_an_all_silent_recording_is_an_empty_transcript(cfg, tmp_path):
    out = tr.transcribe_audio(cfg, FakeModel([SEGS[1]]), tmp_path / "a.m4a")
    assert out["text"] == "" and out["segments"] == []


# ---------------------------------------------------------------- date override

def _audio(cfg, name="2026-07-20 21.30.00.m4a"):
    p = cfg.inbox_dir / name
    p.write_bytes(b"fake-audio-" + name.encode())
    _age(p)
    return p


@pytest.fixture
def canned(monkeypatch):
    monkeypatch.setattr(tr, "transcribe_audio",
                        lambda cfg, model, path, initial_prompt=None: dict(FAKE_RESULT))


def test_the_override_is_applied_before_any_row_or_file_is_written(conn, cfg, canned):
    audio = _audio(cfg)
    (res,) = tr.transcribe_inbox(conn, cfg, only_file=audio, date_override="2026-07-27")
    assert res["status"] == "transcribed" and res["journal_date"] == "2026-07-27"
    src = conn.execute("SELECT uuid, journal_date FROM sources").fetchone()
    assert src["journal_date"] == "2026-07-27"
    assert (cfg.transcripts_dir / "2026" / f"2026-07-27-src{res['source_id']}.txt").exists()
    assert [r["journal_date"] for r in conn.execute("SELECT journal_date FROM days")] \
        == ["2026-07-27"]


def test_an_override_for_a_whole_inbox_is_refused(conn, cfg, canned):
    _audio(cfg)
    with pytest.raises(DomainError, match="applies to one recording"):
        tr.transcribe_inbox(conn, cfg, date_override="2026-07-27")
    assert conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0


def test_a_bad_override_fails_before_whisper_is_loaded(conn, cfg):
    def must_not_load():
        raise AssertionError("whisper loaded before the date was checked")
    with pytest.raises(DomainError, match="invalid journal date '2026-02-30'"):
        tr.process_file(conn, cfg, must_not_load, _audio(cfg),
                        date_override="2026-02-30")

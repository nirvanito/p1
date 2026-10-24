"""Combo 1 capture (Windows laptop + Samsung): what a recording's filename says
about when it was made.

Android recorders stamp the CALENDAR date the phone's clock showed; the iPhone
Shortcut stamps the JOURNAL date, already rolled back for early-morning notes.
Before this, every name was read the Shortcut's way, so a 00:30 Android note
dated 07-28 landed on journal day 07-28 with an instant a full day late
(2026-07-29T05:30Z). These pin both conventions, the shapes that tell them
apart, the [time] filename_date override, and — against a verbatim copy of
the old parser — that every Shortcut name still reads exactly as it did.
"""

import json
import math
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import transcribe as tr
from lifecrm.cli import app
from lifecrm.config import ConfigError, load_config

runner = CliRunner()
TZ = "America/Chicago"


def _age(path: Path, when: datetime | None = None) -> Path:
    """Backdate mtime past intake's still-uploading guard (or to `when`)."""
    t = when.timestamp() if when else datetime.now(timezone.utc).timestamp() - 3600
    os.utime(path, (t, t))
    return path


def _times(name: str, rollover: int = 5, mode: str = "auto", tz: str = TZ):
    stamp = tr.filename_stamp(name)
    if stamp is None:
        return None
    return (tr.journal_date_from_filename(name, rollover, mode),
            tr.recorded_at_from_filename(name, stamp.date, tz, rollover, mode))


@pytest.fixture
def fake_whisper(monkeypatch):
    monkeypatch.setattr(tr, "load_model", lambda cfg, model_name=None: object())
    monkeypatch.setattr(
        tr, "transcribe_audio",
        lambda cfg, model, path, initial_prompt=None: {
            "text": "Called Mom about Sunday dinner.",
            "segments": [{"start_sec": 0.0, "end_sec": 2.0, "char_start": 0,
                          "char_end": 31, "text": "Called Mom about Sunday dinner."}],
            "duration_sec": 2.0, "language": "en"})


# ---------------------------------------------------------------- every Android form

# 21:30:05 on 2026-07-27 in Austin is CDT (UTC-5): 02:30:05Z the next day.
EVENING = ("2026-07-27", "2026-07-28T02:30:05Z")


@pytest.mark.parametrize("name", [
    "Recording_20260727_213005.m4a",
    "Voice 20260727_213005.m4a",
    "20260727_213005.m4a",
    "20260727 213005.m4a",
    "20260727T213005.m4a",
    "20260727213005.m4a",
    "2026-07-27_21-30-05.m4a",
    "Rec 2026-07-27 21-30-05.m4a",
    "2026_07_27-21_30_05.m4a",
    "2026-07-27-21-30-05.3ga",
    "2026-07-27 21.30.05.m4a",       # dotted, but no offset: not the Shortcut
    "Voice 20260727_213005.webm",
])
def test_every_android_form_reads_the_calendar_date_and_clock(name):
    assert _times(name) == EVENING
    assert tr.filename_stamp(name).convention == "calendar"


def test_a_separated_time_without_seconds_is_on_the_minute():
    assert _times("Rec 2026-07-27 21-30.m4a") == ("2026-07-27", "2026-07-28T02:30:00Z")


def test_samsung_call_recordings_carry_a_two_digit_year():
    # 2026-10-20 is still CDT (DST ends 2026-11-01): 20:05:12 local = 01:05:12Z.
    assert _times("Call recording Mom_261020_200512.m4a") == \
        ("2026-10-20", "2026-10-21T01:05:12Z")
    assert _times("Call recording Jake Morales_261020_200512.m4a") == \
        ("2026-10-20", "2026-10-21T01:05:12Z")
    assert tr.date_from_filename("Call recording Mom_261020_200512.m4a") == "2026-10-20"


def test_a_phone_number_in_a_call_recording_is_not_read_as_a_date():
    """'+12020101234' holds a valid-looking 2020-10-12; the stamp at the end wins
    and a date buried in a longer digit run is never read."""
    assert _times("Call recording +12020101234_261020_200512.m4a") == \
        ("2026-10-20", "2026-10-21T01:05:12Z")
    assert tr.date_from_filename("+12020101234.m4a") is None


def test_a_hex_id_after_a_compact_time_is_not_read_as_an_offset():
    # Mac Voice Memos: "20260727 213005-1234ABCD.m4a" — "-1234" is not -12:34.
    assert _times("20260727 213005-1234ABCD.m4a") == EVENING


# ---------------------------------------------------------------- after midnight

def test_an_android_note_after_midnight_belongs_to_the_previous_journal_day():
    """THE confirmed bug: a 00:30 note dated 07-28 is journal day 07-27, and it
    happened at 2026-07-28T05:30Z — not 07-29."""
    assert _times("Recording_20260728_003000.m4a") == ("2026-07-27", "2026-07-28T05:30:00Z")
    assert _times("2026-07-28 00.30.00.m4a") == ("2026-07-27", "2026-07-28T05:30:00Z")
    assert _times("Call recording Mom_261021_013000.m4a") == \
        ("2026-10-20", "2026-10-21T06:30:00Z")


def test_a_shortcut_note_after_midnight_is_already_on_its_journal_day():
    assert _times("2026-07-27 00.30.00 -0500.m4a") == ("2026-07-27", "2026-07-28T05:30:00Z")


def test_both_conventions_put_the_same_moment_on_the_same_day():
    assert _times("2026-07-27 00.30.00 -0500.m4a") == _times("Recording_20260728_003000.m4a")
    assert _times("2026-07-27 04.59.59 -0500.txt") == _times("20260728_045959.txt")


def test_the_rollover_hour_itself_starts_the_new_day():
    assert _times("20260728_045959.m4a") == ("2026-07-27", "2026-07-28T09:59:59Z")
    assert _times("20260728_050000.m4a") == ("2026-07-28", "2026-07-28T10:00:00Z")


def test_after_midnight_on_new_years_day_is_new_years_eve():
    # January: CST (UTC-6).
    assert _times("Recording_20270101_003000.m4a") == ("2026-12-31", "2027-01-01T06:30:00Z")


def test_a_rollover_hour_of_zero_never_moves_the_day():
    assert _times("Recording_20260728_003000.m4a", rollover=0) == \
        ("2026-07-28", "2026-07-28T05:30:00Z")


# ---------------------------------------------------------------- offsets

def test_the_shortcut_offset_wins_over_the_config_zone():
    # Recorded in Ljubljana (+0200) at 21:30 — 19:30Z, whatever config says.
    assert _times("2026-07-27 21.30.05 +0200.m4a") == ("2026-07-27", "2026-07-27T19:30:05Z")
    # After midnight abroad: journal date as written, instant on the next day.
    assert _times("2026-07-27 00.30.00 +0200.txt") == ("2026-07-27", "2026-07-27T22:30:00Z")


def test_an_offset_is_what_marks_the_shortcut_shape():
    assert tr.filename_stamp("2026-07-27 21.30.05 -0500.m4a").convention == "journal"
    assert tr.filename_stamp("2026-07-27 21.30 -0600.m4a").convention == "journal"
    assert tr.filename_stamp("2026-07-27 21.30.05.m4a").convention == "calendar"
    assert tr.filename_stamp("20260727_213005.m4a").convention == "calendar"


def test_an_impossible_offset_is_ignored_not_a_crash():
    # Nothing on Earth is -99:99; read as no offset (config zone, calendar).
    assert _times("2026-07-27 21.30.05 -9999.m4a") == EVENING


def test_android_names_use_the_config_zone_across_dst():
    # March 8 2026: US clocks spring forward at 02:00. 21:30 that evening is CDT.
    assert _times("20260308_213000.m4a")[1] == "2026-03-09T02:30:00Z"
    # March 7, still CST.
    assert _times("20260307_213000.m4a")[1] == "2026-03-08T03:30:00Z"


# ---------------------------------------------------------------- invalid and dateless

@pytest.mark.parametrize("name", [
    "Recording_20261332_213005.m4a",      # month 13
    "Recording_20260230_213005.m4a",      # February 30
    "Call recording Mom_261340_200512.m4a",
    "Call recording Mom_260229_200512.m4a",   # 2026 is not a leap year
    "2026-02-30 21.30.05 -0600.m4a",
    "Voice 001.m4a",                      # Samsung Voice Recorder's default
    "My recording 3.m4a",
    "New Recording 7.m4a",                # Voice Memos shared from the iPhone
    "261020.m4a",                         # six digits alone: a counter, not a date
])
def test_names_without_a_valid_date_fall_back_to_the_file_time(name):
    assert tr.filename_stamp(name) is None
    assert tr.date_from_filename(name) is None
    assert tr.journal_date_from_filename(name, 5) is None


def test_a_leap_day_call_recording_is_a_real_date():
    assert tr.date_from_filename("Call recording Mom_280229_200512.m4a") == "2028-02-29"


@pytest.mark.parametrize("name", [
    "Recording_20260727_253005.m4a",      # hour 25
    "Recording_20260727_216005.m4a",      # minute 60
    "20260727_0001.m4a",                  # a counter, not 00:01
    "2026-07-27 2130.m4a",                # four digits: as likely a counter
    "PXL_20260727_213005123.mp4",         # milliseconds run on: not a clean time
    "Recording 2026-07-27.m4a",
])
def test_a_date_without_a_readable_time_keeps_the_date_and_no_instant(name):
    """No clock, no rollover: the written date stands and recorded_at comes
    from mtime. An after-midnight shift needs a time to justify it."""
    assert _times(name) == ("2026-07-27", None)


def test_an_unreadable_time_takes_recorded_at_from_mtime(cfg):
    p = cfg.inbox_dir / "Recording_20260727_253005.m4a"
    p.write_bytes(b"x")
    _age(p, datetime(2026, 7, 28, 2, 30, tzinfo=timezone.utc))
    assert tr.resolve_file_times(p, cfg) == ("2026-07-27", "2026-07-28T02:30:00Z")


def test_samsung_voice_recorder_names_use_mtime_with_the_rollover(cfg):
    """'Voice 001.m4a' carries nothing: the file's own time decides, and a
    00:30 recording still files under the previous day."""
    p = cfg.inbox_dir / "Voice 001.m4a"
    p.write_bytes(b"x")
    _age(p, datetime(2026, 7, 28, 5, 30, tzinfo=timezone.utc))   # 00:30 CDT
    assert tr.resolve_file_times(p, cfg) == ("2026-07-27", "2026-07-28T05:30:00Z")


# ---------------------------------------------------------------- the config override

def test_journal_mode_trusts_every_date_as_the_journal_day():
    """For a phone automation that rolls the date back itself, Shortcut-style,
    without writing an offset."""
    assert _times("Recording_20260728_003000.m4a", mode="journal") == \
        ("2026-07-28", "2026-07-29T05:30:00Z")
    assert _times("2026-07-27 00.30.00 -0500.m4a", mode="journal") == \
        _times("2026-07-27 00.30.00 -0500.m4a")


def test_calendar_mode_reads_even_a_shortcut_name_as_the_calendar_date():
    assert _times("2026-07-27 00.30.00 -0500.m4a", mode="calendar") == \
        ("2026-07-26", "2026-07-27T05:30:00Z")
    assert _times("Recording_20260728_003000.m4a", mode="calendar") == \
        _times("Recording_20260728_003000.m4a")


def test_the_modes_agree_when_the_clock_is_past_the_rollover():
    for mode in ("auto", "journal", "calendar"):
        assert _times("Recording_20260727_213005.m4a", mode=mode) == EVENING
        assert _times("2026-07-27 21.30.05 -0500.m4a", mode=mode) == EVENING


def test_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="auto, journal or calendar"):
        tr.journal_date_from_filename("Recording_20260728_003000.m4a", 5, "yesterday")


def test_resolve_file_times_follows_the_configured_mode(cfg):
    p = cfg.inbox_dir / "Recording_20260728_003000.m4a"
    p.write_bytes(b"x")
    _age(p)
    assert tr.resolve_file_times(p, cfg) == ("2026-07-27", "2026-07-28T05:30:00Z")
    assert tr.resolve_file_times(p, replace(cfg, filename_date="journal")) == \
        ("2026-07-28", "2026-07-29T05:30:00Z")


def _write_config(root: Path, time_lines: str) -> None:
    (root / "config.toml").write_text(
        "[paths]\ndata_dir = \"data\"\nbackups_dir = \"backups\"\n"
        "[time]\ntimezone = \"America/Chicago\"\n" + time_lines, encoding="utf-8-sig")


def test_filename_date_defaults_to_auto(tmp_path):
    _write_config(tmp_path, "day_rollover_hour = 5\n")
    assert load_config(tmp_path).filename_date == "auto"


@pytest.mark.parametrize("mode", ["auto", "journal", "calendar"])
def test_filename_date_is_read_from_the_time_table(tmp_path, mode):
    _write_config(tmp_path, f'filename_date = "{mode}"\n')     # BOM-prefixed file
    assert load_config(tmp_path).filename_date == mode


@pytest.mark.parametrize("bad", ['"Calendar"', '"android"', "true", "1"])
def test_a_bad_filename_date_is_a_config_error_naming_the_key(tmp_path, bad):
    _write_config(tmp_path, f"filename_date = {bad}\n")
    with pytest.raises(ConfigError, match=r"\[time\] filename_date must be"):
        load_config(tmp_path)


def test_a_bad_filename_date_is_one_error_line_at_the_cli(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    cfg_file = root / "config.toml"
    cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace(
        "day_rollover_hour = 5", 'day_rollover_hour = 5\nfilename_date = "phone"'),
        encoding="utf-8")
    r = runner.invoke(app, ["status"])
    assert r.exit_code == 1
    lines = [ln for ln in r.output.splitlines() if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("error:")
    assert "filename_date" in lines[0] and "'phone'" in lines[0]


# ---------------------------------------------------------------- the Shortcut, unchanged

def _legacy_date(name):
    """transcribe.date_from_filename before this change, verbatim."""
    for rx in [re.compile(r"(20\d{2})-(\d{2})-(\d{2})"),
               re.compile(r"(20\d{2})_(\d{2})_(\d{2})"),
               re.compile(r"(20\d{2})(\d{2})(\d{2})")]:
        m = rx.search(name)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)),
                                int(m.group(3))).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def _legacy_recorded_at(name, journal_date, tz_name, rollover_hour):
    """transcribe.recorded_at_from_filename before this change, verbatim."""
    stem = Path(name).stem
    m = re.compile(r"(?<![\d.])([01]\d|2[0-3])\.([0-5]\d)(?:\.([0-5]\d))?(?![\d.])").search(stem)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    second = int(m.group(3) or 0)
    real_date = datetime.strptime(journal_date, "%Y-%m-%d")
    if hour < rollover_hour:
        real_date += timedelta(days=1)
    om = re.compile(r"(?<![\d])([+-])(\d{2})(\d{2})(?![\d])").search(stem[m.end():])
    if om:
        sign = 1 if om.group(1) == "+" else -1
        tz = timezone(sign * timedelta(hours=int(om.group(2)), minutes=int(om.group(3))))
    else:
        tz = ZoneInfo(tz_name)
    local = real_date.replace(hour=hour, minute=minute, second=second, tzinfo=tz)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# US DST start and end, a month end, a year end, a leap day.
SHORTCUT_DATES = ["2026-07-27", "2026-03-08", "2026-11-01", "2026-12-31",
                  "2027-01-01", "2028-02-29"]


def _shortcut_names():
    for d in SHORTCUT_DATES:
        for hour in range(24):
            ext = ".m4a" if hour % 2 else ".txt"      # audio and Quick Note alike
            for offset in ("-0500", "-0600", "+0000", "+0200", "+0530"):
                for clock in (f"{hour:02d}.30.05", f"{hour:02d}.07"):
                    yield f"{d} {clock} {offset}{ext}"


@pytest.mark.parametrize("rollover", [0, 3, 5])
def test_every_shortcut_name_reads_exactly_as_before(cfg, rollover):
    """Journal date and instant, for every hour, across DST, month and year
    ends, under the default auto mode and under journal mode."""
    checked = 0
    for name in _shortcut_names():
        before_date = _legacy_date(name)
        before = (before_date, _legacy_recorded_at(name, before_date, TZ, rollover))
        for mode in ("auto", "journal"):
            assert tr.filename_times(
                name, replace(cfg, day_rollover_hour=rollover, filename_date=mode)) == \
                before, (name, mode)
        assert tr.recorded_at_from_filename(name, before_date, TZ, rollover) == before[1]
        checked += 1
    assert checked == len(SHORTCUT_DATES) * 24 * 5 * 2


def test_dotted_names_without_an_offset_changed_only_before_the_rollover():
    """The one deliberate change for dotted names: with no offset they are not
    the Shortcut, so before 05:00 the day rolls back instead of the instant
    rolling forward. From 05:00 on, nothing moved."""
    for hour in range(24):
        name = f"2026-07-28 {hour:02d}.30.00.m4a"
        before = ("2026-07-28", _legacy_recorded_at(name, "2026-07-28", TZ, 5))
        if hour >= 5:
            assert _times(name) == before
        else:
            journal, instant = _times(name)
            assert journal == "2026-07-27"
            assert instant == (datetime.fromisoformat(before[1][:-1])
                               - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- through intake

def test_an_android_recording_after_midnight_through_intake(conn, cfg, fake_whisper):
    audio = cfg.inbox_dir / "Recording_20260728_003000.m4a"
    audio.write_bytes(b"android-audio")
    _age(audio)
    results = tr.transcribe_inbox(conn, cfg)
    assert results[0]["status"] == "transcribed"
    assert results[0]["journal_date"] == "2026-07-27"
    src = conn.execute("SELECT journal_date, recorded_at, path FROM sources WHERE id=?",
                       (results[0]["source_id"],)).fetchone()
    assert (src["journal_date"], src["recorded_at"]) == ("2026-07-27", "2026-07-28T05:30:00Z")
    assert Path(results[0]["transcript_file"]).name.startswith("2026-07-27-src")
    assert Path(src["path"]).parent == cfg.archive_dir / "2026"


def test_a_samsung_call_recording_through_intake(conn, cfg, fake_whisper):
    audio = cfg.inbox_dir / "Call recording Mom_261021_013000.m4a"
    audio.write_bytes(b"call-audio")
    _age(audio)
    assert tr.transcribe_inbox(conn, cfg) == []            # held for the owner's pick
    res = tr.transcribe_inbox(conn, cfg, files=tr.resolve_held(cfg, ["Mom"]))[0]
    src = conn.execute("SELECT journal_date, recorded_at FROM sources WHERE id=?",
                       (res["source_id"],)).fetchone()
    assert (src["journal_date"], src["recorded_at"]) == ("2026-10-20", "2026-10-21T06:30:00Z")


def test_an_android_text_note_after_midnight_through_intake(conn, cfg):
    note = cfg.inbox_text_dir / "2026-07-28 00.30.00.txt"
    note.write_text("﻿Remember to book flights to Ljubljana.", encoding="utf-8")
    _age(note)
    res = tr.intake_text_inbox(conn, cfg)[0]
    assert res["status"] == "imported"
    src = conn.execute("SELECT journal_date, recorded_at FROM sources WHERE id=?",
                       (res["source_id"],)).fetchone()
    assert (src["journal_date"], src["recorded_at"]) == ("2026-07-27", "2026-07-28T05:30:00Z")


def test_journal_mode_through_intake(conn, cfg, fake_whisper):
    audio = cfg.inbox_dir / "Recording_20260728_003000.m4a"
    audio.write_bytes(b"journal-mode-audio")
    _age(audio)
    res = tr.transcribe_inbox(conn, replace(cfg, filename_date="journal"))[0]
    assert res["journal_date"] == "2026-07-28"


# ---------------------------------------------------------------- audio containers

@pytest.mark.parametrize("name", ["Voice 001.3ga", "call.3gp", "clip.webm",
                                  "memo.caf", "Voice 002.M4A", "OLD.3GA"])
def test_android_and_apple_containers_are_picked_up_by_intake(cfg, name):
    (cfg.inbox_dir / name).write_bytes(b"x")
    _age(cfg.inbox_dir / name)
    assert [p.name for p in tr.scan_inbox(cfg)] == [name]


def test_doctor_does_not_flag_the_new_containers(conn, cfg):
    from lifecrm.doctor import run_checks
    for name in ("Voice 001.3ga", "call.3gp", "clip.webm", "memo.caf"):
        (cfg.inbox_dir / name).write_bytes(b"x")
    checks = {c["check"]: c for c in run_checks(conn, cfg)["checks"]}
    assert checks["inbox_unrecognized"]["status"] == "ok", checks["inbox_unrecognized"]


# (file name, PyAV container, encoder, sample rate). AMR-NB at 8 kHz is what
# older Samsung recorders and call recorders wrote into .3ga/.3gp.
CONTAINERS = [
    ("Voice 20260727_213005.3ga", "3gp", "aac", 16000),
    ("Voice 003.3ga", "3gp", "libopencore_amrnb", 8000),
    ("Call recording Mom_261020_200512.3gp", "3gp", "aac", 16000),
    ("call-amr.3gp", "3gp", "libopencore_amrnb", 8000),
    ("browser.webm", "webm", "libopus", 48000),
    ("memo.caf", "caf", "pcm_s16le", 16000),
    ("memo-alac.caf", "caf", "alac", 44100),
]


def _encode_tone(path: Path, fmt: str, codec: str, rate: int, seconds: float = 2.0):
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    container = av.open(str(path), mode="w", format=fmt)
    try:
        stream = container.add_stream(codec, rate=rate)
    except Exception as e:      # an encoder this PyAV build lacks: nothing to prove
        container.close()
        pytest.skip(f"PyAV cannot encode {codec} into {fmt} here: {e}")
    with container:
        stream.layout = "mono"
        ctx = stream.codec_context
        sample_fmt = ctx.codec.audio_formats[0].name if ctx.codec.audio_formats else "s16"
        frame = ctx.frame_size or 1024
        resampler = av.AudioResampler(format=sample_fmt, layout="mono", rate=rate,
                                      frame_size=frame)
        total, t = int(rate * seconds), 0
        while t < total:
            n = min(frame, total - t)
            tone = 0.3 * np.sin(2 * math.pi * 440 * np.arange(t, t + n) / rate)
            af = av.AudioFrame.from_ndarray(tone.astype(np.float32).reshape(1, -1),
                                            format="flt", layout="mono")
            af.sample_rate = rate
            for out in resampler.resample(af):
                for pkt in stream.encode(out):
                    container.mux(pkt)
            t += n
        for out in resampler.resample(None):
            for pkt in stream.encode(out):
                container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)


@pytest.mark.parametrize("name,fmt,codec,rate", CONTAINERS)
def test_each_new_container_decodes_the_way_whisper_opens_it(tmp_path, name, fmt,
                                                            codec, rate):
    """AUDIO_EXTS lists only what decodes: a tone encoded into each container
    comes back through faster-whisper's own decoder at 16 kHz."""
    audio_mod = pytest.importorskip("faster_whisper.audio")
    path = tmp_path / name
    _encode_tone(path, fmt, codec, rate)
    assert path.suffix.lower() in tr.AUDIO_EXTS
    samples = audio_mod.decode_audio(str(path), sampling_rate=16000)
    assert abs(len(samples) / 16000 - 2.0) < 0.25
    assert float(abs(samples).max()) > 0.1


# ---------------------------------------------------------------- lifecrm filedate

def test_filedate_explains_planned_names_without_touching_the_database(
        tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["filedate", "Recording_20260728_003000.m4a",
                            "2026-07-27 00.30.00 -0500.m4a", "Voice 002.m4a"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0] == ("Recording_20260728_003000.m4a: journal day 2026-07-27, "
                        "recorded 2026-07-28T05:30:00Z — calendar date + clock time "
                        "in the name")
    assert "journal day 2026-07-27, recorded 2026-07-28T05:30:00Z — journal date" in lines[1]
    assert lines[2].startswith("Voice 002.m4a: no date in the name")
    assert not (root / "data" / "lifecrm.db").exists()


def test_filedate_reads_mtime_for_a_dateless_file_on_disk(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    p = root / "onedrive" / "inbox" / "Voice 001.m4a"
    p.write_bytes(b"x")
    _age(p, datetime(2026, 7, 28, 5, 30, tzinfo=timezone.utc))
    r = runner.invoke(app, ["filedate", str(p), "--json"])
    assert r.exit_code == 0, r.output
    [d] = json.loads(r.output)
    assert d == {"name": "Voice 001.m4a", "kind": "audio",
                 "journal_date": "2026-07-27", "recorded_at": "2026-07-28T05:30:00Z",
                 "date_from": "mtime", "time_from": "mtime", "convention": None}


def test_filedate_warns_about_a_type_intake_never_reads(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["filedate", "Notes 2026-07-27.sdocx"])
    assert r.exit_code == 0
    assert "WARNING: intake does not read this file type" in r.output


def test_filedate_names_a_configured_override(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    cfg_file = root / "config.toml"
    cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace(
        "day_rollover_hour = 5", 'day_rollover_hour = 5\nfilename_date = "journal"'),
        encoding="utf-8")
    r = runner.invoke(app, ["filedate", "Recording_20260728_003000.m4a", "--json"])
    [d] = json.loads(r.output)
    assert (d["journal_date"], d["convention"]) == ("2026-07-28", "journal")
    r = runner.invoke(app, ["filedate", "Recording_20260728_003000.m4a"])
    assert 'filename_date = "journal"' in r.output.splitlines()[-1]

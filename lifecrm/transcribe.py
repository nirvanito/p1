"""Inbox intake: faster-whisper transcription for audio, direct import for
phone-dictated text. Both feed the same source/transcript/segment tables.

- OneDrive Files On-Demand guard: placeholder files are hydrated (read) with
  retries before processing; files modified in the last 60 seconds are
  skipped as possibly still uploading.
- Date resolution: filename date (YYYY-MM-DD / YYYYMMDD / Samsung's yyMMdd)
  wins; otherwise the file mtime mapped through the per-source timezone and
  rollover hour. The filename also carries the *clock* time (HH.mm.ss,
  HHmmss, HH-mm-ss, optional UTC offset) — the phone knows the recording
  instant; a OneDrive round trip can make mtime the upload time instead.
- Two filename conventions, told apart by shape: the iPhone Shortcut writes
  the JOURNAL date (already rolled back) with a dotted time and a UTC offset;
  every Android recorder writes the CALENDAR date. The rollover is undone for
  the first and applied for the second, so a 00:30 note lands on the same
  journal day with the same instant either way. [time] filename_date
  overrides the guess for a phone that breaks it.
- Recoverability: rows are committed BEFORE the file leaves the inbox. A
  crash between commit and archive leaves the file in the inbox, where the
  next run recognizes its content hash and archives it without re-ingesting.
- Content-hash duplicates warn (or archive-skip in the inbox flow) — never a
  hard block.
- initial_prompt seeding with known entity names is a marginal aid to roughly
  the first minute of audio only — the alias table is the real fix for
  misspelled names.
- Call recordings are never transcribed on their own. A file named like one
  (Samsung's "Call recording Mom_261020_200512.m4a", "Call with ...",
  [intake] call_patterns) is HELD in the inbox until you choose: `lifecrm
  calls take` transcribes it into a source like any note, `calls skip`
  archives it untranscribed. [intake] call_recordings = "auto" turns the
  hold off.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

from . import platforms
from .config import Config, ConfigError
from .domain import days, sources
from .domain.util import DomainError, is_iso_date
from .importer import _write_transcript_copy, import_text, normalize_text

# Every container here decodes through PyAV's bundled FFmpeg, which is what
# faster-whisper opens — checked by encoding a tone into each and decoding it
# back (tests/test_android_filenames.py). .3ga/.3gp: older Samsung recorders
# and call recordings (AAC or AMR-NB in a 3GPP box); .webm: Opus from browser
# and open-source recorders; .caf: Apple's Core Audio container.
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac", ".wma",
              ".amr", ".mp4", ".3ga", ".3gp", ".webm", ".caf"}
TEXT_EXTS = {".txt", ".md"}
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
# macOS st_flags bit for a File Provider file whose bytes are not on disk
# (iCloud Drive with Optimize Mac Storage). stat.SF_DATALESS is 3.13+ only.
SF_DATALESS = 0x40000000
MIN_FILE_AGE_SEC = 60
# Stubs already asked for in this process — intake scans each inbox twice.
_DOWNLOAD_REQUESTED: set[str] = set()

# No digit may precede the year: a date inside a longer digit run is a phone
# number or a counter ("+12020101234" holds a valid-looking 2020-10-12).
_DATE_RES = [
    re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})"),
    re.compile(r"(?<!\d)(20\d{2})_(\d{2})_(\d{2})"),
    re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})"),
]
# Samsung's call recorder: "Call recording Mom_261020_200512" — yyMMdd_HHmmss
# after an underscore. Tried first, and only with its time attached: a bare
# six-digit run is as often a counter as a date.
_CALL_RE = re.compile(
    r"(?:^|(?<=_))(\d{2})(\d{2})(\d{2})_([01]\d|2[0-3])([0-5]\d)([0-5]\d)(?!\d)")
# Clock time as the Shortcut writes it: "21.30.05" or "21.30", optionally
# followed by a UTC offset like "-0500". Dots, not colons — colons are illegal
# in filenames on every platform involved.
_TIME_RE = re.compile(r"(?<![\d.])([01]\d|2[0-3])\.([0-5]\d)(?:\.([0-5]\d))?(?![\d.])")
_OFFSET_RE = re.compile(r"(?<![\d])([+-])(\d{2})(\d{2})(?![\d])")
# Clock time as Android recorders write it, directly after the date:
# "_213005", " 213005", "T213005" (compact — seconds required, since a bare
# four-digit run is as likely a counter), or "_21-30-05", " 21-30",
# "-21_30_05" (one separator, used consistently). No offset is read after
# these: no Android recorder writes one, and what follows is often a counter
# or a hex id ("20260727 213005-1234ABCD", Voice Memos on the Mac) that
# would read as -12:34.
_ADJ_TIME_RE = re.compile(
    r"[ _T-]?(?:([01]\d|2[0-3])([0-5]\d)([0-5]\d)"
    r"|([01]\d|2[0-3])([-_])([0-5]\d)(?:\5([0-5]\d))?)(?!\d)")

FILENAME_CONVENTIONS = ("journal", "calendar")


class FilenameStamp(NamedTuple):
    """What a filename says about when it was recorded."""
    date: str                               # YYYY-MM-DD exactly as written
    clock: tuple[int, int, int] | None      # (hour, minute, second) local
    offset: timezone | None                 # a UTC offset written in the name
    shortcut: bool                          # dotted time + offset: iPhone Shortcut

    @property
    def convention(self) -> str:
        """'journal' — the name's date is the journal day, rolled back on the
        phone (the iPhone Shortcut) — or 'calendar' — the date the phone's
        clock showed (every Android recorder, Voice Memos renamed by hand)."""
        return "journal" if self.shortcut else "calendar"


def _offset_tz(m: re.Match | None) -> timezone | None:
    """A UTC offset no clock on Earth shows (past ±14:00) is a counter or a
    phone-number fragment, not a zone — read as none, never a crash."""
    if not m:
        return None
    hours, minutes = int(m.group(2)), int(m.group(3))
    if hours > 14 or minutes > 59:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _dotted_clock(stem: str):
    """The Shortcut's dotted time anywhere in the stem, and an offset anywhere
    after it — the original parse, unchanged for the names it already read."""
    m = _TIME_RE.search(stem)
    if not m:
        return None, None, False
    clock = (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    tz = _offset_tz(_OFFSET_RE.search(stem[m.end():]))
    return clock, tz, tz is not None


def filename_stamp(name: str) -> FilenameStamp | None:
    """Parse the date, clock time and UTC offset a filename carries; None when
    it carries no valid date. An invalid date is skipped, never guessed at."""
    stem = Path(name).stem
    m = _CALL_RE.search(stem)
    if m:
        yy, mo, d, hh, mi, ss = (int(g) for g in m.groups())
        try:
            day = datetime(2000 + yy, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            pass
        else:
            return FilenameStamp(day, (hh, mi, ss), None, False)
    for rx in _DATE_RES:
        m = rx.search(name)
        if not m:
            continue
        try:
            day = datetime(int(m.group(1)), int(m.group(2)),
                           int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            continue
        adj = _ADJ_TIME_RE.match(name, m.end())
        if adj:
            if adj.group(1):
                clock = (int(adj.group(1)), int(adj.group(2)), int(adj.group(3)))
            else:
                clock = (int(adj.group(4)), int(adj.group(6)), int(adj.group(7) or 0))
            return FilenameStamp(day, clock, None, False)
        return FilenameStamp(day, *_dotted_clock(stem))
    return None


def date_from_filename(name: str) -> str | None:
    """The date written in the name — a journal or a calendar date depending
    on who wrote it (see FilenameStamp.convention)."""
    stamp = filename_stamp(name)
    return stamp.date if stamp else None


def _convention(stamp: FilenameStamp, mode: str) -> str:
    if mode == "auto":
        return stamp.convention
    if mode not in FILENAME_CONVENTIONS:
        raise ValueError(f"filename date mode must be auto, journal or calendar, "
                         f"got {mode!r}")
    return mode


def _stamp_times(stamp: FilenameStamp, tz_name: str, rollover_hour: int,
                 mode: str) -> tuple[str, str | None]:
    """(journal_date, recorded_at_utc or None) for a parsed name.

    Journal convention: the date is the journal day; a clock time before the
    rollover hour happened on the calendar day AFTER it, so undo the rollover
    for the instant. Calendar convention: the date and clock are the instant;
    a clock time before the rollover hour belongs to the PREVIOUS journal day.
    Both read the day boundary off the clock in the name — the phone's own
    wall clock, as the Shortcut's subtraction does — not the config zone.
    """
    if stamp.clock is None:
        return stamp.date, None
    hour, minute, second = stamp.clock
    written = datetime.strptime(stamp.date, "%Y-%m-%d")
    early = hour < rollover_hour
    if _convention(stamp, mode) == "journal":
        journal, real = written, written + timedelta(days=1 if early else 0)
    else:
        journal, real = written - timedelta(days=1 if early else 0), written
    tz = stamp.offset if stamp.offset is not None else ZoneInfo(tz_name)
    local = real.replace(hour=hour, minute=minute, second=second, tzinfo=tz)
    return (journal.strftime("%Y-%m-%d"),
            local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))


def journal_date_from_filename(name: str, rollover_hour: int,
                               mode: str = "auto") -> str | None:
    """The journal day a filename puts its note on, or None without a date.
    mode: 'auto' (decide from the name's shape), 'journal' or 'calendar'."""
    stamp = filename_stamp(name)
    if stamp is None:
        return None
    # The zone only moves the instant, never the day — any valid zone will do.
    return _stamp_times(stamp, "UTC", rollover_hour, mode)[0]


def recorded_at_from_filename(name: str, file_date: str, tz_name: str,
                              rollover_hour: int, mode: str = "auto") -> str | None:
    """Reconstruct the recording instant from the clock time in a filename,
    anchored on file_date (the date the name carries).

    A Shortcut-style name carries the JOURNAL date (already rolled back for
    early-morning notes) plus the real CLOCK time, so a clock time before the
    rollover hour actually happened on the calendar day AFTER the journal
    date; an Android-style name carries the calendar date itself. The UTC
    offset in the name, when present, wins over tz_name.
    Returns UTC ISO, or None when the name carries no parseable time.
    """
    stamp = filename_stamp(name)
    if stamp is None:   # no date in the name: only the Shortcut's time can be read
        clock, tz, shortcut = _dotted_clock(Path(name).stem)
        stamp = FilenameStamp(file_date, clock, tz, shortcut)
    return _stamp_times(stamp._replace(date=file_date), tz_name, rollover_hour,
                        mode)[1]


def filename_times(name: str, cfg: Config) -> tuple[str, str | None] | None:
    """(journal_date, recorded_at_utc or None) from a filename under cfg's
    zone, rollover hour and [time] filename_date; None without a date."""
    stamp = filename_stamp(name)
    if stamp is None:
        return None
    return _stamp_times(stamp, cfg.timezone, cfg.day_rollover_hour,
                        cfg.filename_date)


def is_placeholder(path: Path) -> bool:
    """True when the file's bytes live only in the cloud: a OneDrive Files
    On-Demand placeholder (a Windows attribute) or a dataless iCloud Drive
    file (a macOS flag). Reading either one downloads it."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    flags = getattr(st, "st_flags", 0)
    return bool(attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS or flags & SF_DATALESS)


def read_hydrated(path: Path, retries: int = 3, wait_sec: float = 2.0) -> bytes:
    """Read file bytes; opening a placeholder makes the cloud client download it."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return path.read_bytes()
        except OSError as e:
            last_err = e
            time.sleep(wait_sec * (attempt + 1))
    raise DomainError(
        f"could not read {path.name} (cloud file offline or not downloaded yet): "
        f"{last_err}")


def icloud_stub(path: Path, exts: set[str]) -> str | None:
    """The note an iCloud stub stands for ('.a.m4a.icloud' -> 'a.m4a'), or
    None when `path` is not a stub for a file of one of these types."""
    if not platforms.is_icloud_placeholder(path):
        return None
    target = platforms.icloud_placeholder_target(path)
    return target if Path(target).suffix.lower() in exts else None


def _request_download(stub: Path, target: str) -> None:
    """Ask iCloud for an evicted note, once per process, and say so.

    A stub is never read: its bytes are a few hundred bytes of metadata, not
    the note. `brctl download` only queues the download — the real file
    appears beside the stub later and the next run picks it up. Elsewhere
    (a Mac-synced folder copied to Windows) there is nothing to ask, only
    something to report.
    """
    key = str(stub)
    if key in _DOWNLOAD_REQUESTED:
        return
    _DOWNLOAD_REQUESTED.add(key)
    asked = ""
    if platforms.is_mac():
        try:
            subprocess.run(["brctl", "download", key], capture_output=True,
                           timeout=30, check=False)
            asked = " — download requested"
        except (OSError, subprocess.SubprocessError):
            asked = " — could not ask for it (brctl failed)"
    print(f"waiting: {target} is still in iCloud, not on this computer{asked}; "
          f"the next run picks it up", file=sys.stderr)


def _scan(directory: Path, exts: set[str]) -> list[Path]:
    if not directory or not directory.exists():
        return []
    now = time.time()
    out = []
    for p in sorted(directory.iterdir()):
        if p.name.startswith("."):
            # Hidden names are never notes. AppleDouble '._x.m4a' (exFAT,
            # SMB, unzipped archives) even carries a real suffix, with no
            # audio in it. An iCloud stub '.x.m4a.icloud' IS a note — one not
            # downloaded yet — so it is asked for, never read.
            target = icloud_stub(p, exts)
            if target:
                _request_download(p, target)
            continue
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        try:
            if now - p.stat().st_mtime < MIN_FILE_AGE_SEC:
                continue  # possibly still uploading from the phone
        except OSError:
            continue
        out.append(p)
    return out


# ---------------------------------------------------------------- call recordings
#
# A call recording holds the other person's voice as well as yours, and a
# phone can record every call by itself (Samsung's "Auto record calls"). So
# intake decides nothing about one: it is held in the inbox, listed by
# `lifecrm calls list` and `status`, until you take it or skip it. Taking is
# the same transcription as any note; skipping moves the audio to the archive
# untouched, and nothing reaches the database either way until you choose.

CALL_MODES = ("ask", "auto")
# Matched against the whole file name, ignoring case. Samsung's own shape —
# a name, then _yyMMdd_HHmmss — is recognized by _CALL_RE as well.
DEFAULT_CALL_PATTERNS = ("call recording*", "call with *", "call_*", "call-*")
_CALL_PREFIX = re.compile(r"^(?:call recording|call with|call)[\s_-]*", re.IGNORECASE)


def _intake_settings(cfg: Config) -> dict:
    value = (cfg.extra or {}).get("intake")
    return value if isinstance(value, dict) else {}


def call_mode(cfg: Config) -> str:
    """'ask' (hold call recordings for your pick — the default) or 'auto'
    (transcribe them like any note)."""
    mode = _intake_settings(cfg).get("call_recordings", "ask")
    if mode not in CALL_MODES:
        raise ConfigError(f"[intake] call_recordings must be \"ask\" or \"auto\", "
                          f"got {mode!r}")
    return mode


def call_patterns(cfg: Config) -> tuple[str, ...]:
    """The built-in name patterns plus any `[intake] call_patterns` adds (for
    a recorder app that names its files another way)."""
    extra = _intake_settings(cfg).get("call_patterns", [])
    if isinstance(extra, str):
        extra = [extra]
    if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
        raise ConfigError("[intake] call_patterns must be a list of file-name "
                          "patterns, e.g. [\"*ACR*\"]")
    return DEFAULT_CALL_PATTERNS + tuple(extra)


def is_call_recording(name: str, cfg: Config) -> bool:
    """A file named like a phone-call recording."""
    if _CALL_RE.search(Path(name).stem):
        return True
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, p.lower()) for p in call_patterns(cfg))


def call_who(name: str) -> str | None:
    """Who the call was with, as the recorder wrote it into the name ("Mom"
    from "Call recording Mom_261020_200512.m4a") — for display only; it is
    never matched against people."""
    stem = Path(name).stem
    m = _CALL_RE.search(stem)
    if m:
        stem = stem[:m.start()]
    stem = _CALL_PREFIX.sub("", stem)
    who = stem.strip(" _-")
    return who or None


def scan_inbox(cfg: Config, include_calls: bool = False) -> list[Path]:
    """The audio intake will transcribe: everything in the inbox except the
    call recordings held for your pick (unless include_calls, or
    [intake] call_recordings = "auto")."""
    files = _scan(cfg.inbox_dir, AUDIO_EXTS)
    if include_calls or call_mode(cfg) == "auto":
        return files
    return [p for p in files if not is_call_recording(p.name, cfg)]


def held_calls(cfg: Config) -> list[Path]:
    """Call recordings waiting in the inbox for your pick, oldest first."""
    if call_mode(cfg) == "auto":
        return []
    calls = [p for p in _scan(cfg.inbox_dir, AUDIO_EXTS)
             if is_call_recording(p.name, cfg)]
    return sorted(calls, key=lambda p: (*resolve_file_times(p, cfg)[::-1], p.name))


def held_call_info(cfg: Config, paths: list[Path] | None = None) -> list[dict]:
    """What `calls list` and `status` show for each held recording."""
    out = []
    for i, p in enumerate(held_calls(cfg) if paths is None else paths, start=1):
        journal_date, recorded_at = resolve_file_times(p, cfg)
        at = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).astimezone(ZoneInfo(cfg.timezone))
        try:
            size = p.stat().st_size
        except OSError:
            size = None
        out.append({"n": i, "file": p.name, "who": call_who(p.name),
                    "journal_date": journal_date, "recorded_at": recorded_at,
                    "local_time": at.strftime("%Y-%m-%d %H:%M"), "bytes": size,
                    "path": str(p)})
    return out


def resolve_held(cfg: Config, picks: list[str], all_: bool = False) -> list[Path]:
    """The held recordings `picks` name: a number from `calls list`, the
    file's full name, or a piece of it that only one held file contains."""
    held = held_calls(cfg)
    if all_:
        if picks:
            raise DomainError("pass recordings to pick, or --all — not both")
        return held
    if not picks:
        raise DomainError("name the recordings to pick (a number from `lifecrm "
                          "calls list`, or part of the file name), or pass --all")
    chosen: list[Path] = []
    for pick in picks:
        if pick.isdigit():
            n = int(pick)
            if not 1 <= n <= len(held):
                raise DomainError(f"no held call recording number {n} — "
                                  f"`lifecrm calls list` shows {len(held)}")
            found = [held[n - 1]]
        else:
            exact = [p for p in held if p.name.lower() == pick.lower()]
            found = exact or [p for p in held if pick.lower() in p.name.lower()]
        if not found:
            raise DomainError(f"no held call recording matches '{pick}' — "
                              f"`lifecrm calls list` shows what is waiting")
        if len(found) > 1:
            raise DomainError(f"'{pick}' matches {len(found)} held recordings ("
                              + ", ".join(p.name for p in found[:4])
                              + ") — give more of the name, or its number")
        if found[0] not in chosen:
            chosen.append(found[0])
    return chosen


def skip_calls(cfg: Config, paths: list[Path]) -> list[dict]:
    """Set held recordings aside: each moves to the archive's year folder,
    untranscribed. The audio is kept — `lifecrm transcribe --file <archived
    path>` takes one later — and nothing is written to the database."""
    out = []
    for p in paths:
        journal_date, _ = resolve_file_times(p, cfg)
        archived = archive_file(cfg, p, journal_date)
        out.append({"file": p.name, "status": "skipped", "journal_date": journal_date,
                    "archived_to": str(archived)})
    return out


def scan_text_inbox(cfg: Config) -> list[Path]:
    return _scan(cfg.inbox_text_dir, TEXT_EXTS)


def resolve_file_times(path: Path, cfg: Config) -> tuple[str, str]:
    """(journal_date, recorded_at_utc) for an inbox file.

    Filename date wins over the mtime+rollover calculation — the phone knows
    things the file mtime does not (pinned by test_filename_date_precedence).
    Filename clock time likewise wins over mtime for recorded_at.
    """
    mtime_utc = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    from_name = filename_times(path.name, cfg)
    if from_name is None:
        journal_date = days.journal_date_for(
            mtime_utc, cfg.timezone, cfg.day_rollover_hour)
        recorded_at = None
    else:
        journal_date, recorded_at = from_name
    if recorded_at is None:
        recorded_at = mtime_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    return journal_date, recorded_at


def entity_prompt(conn: sqlite3.Connection, limit: int = 40) -> str | None:
    names = [r["name"] for r in conn.execute(
        "SELECT e.name, COUNT(m.entry_id) AS n FROM entities e "
        "LEFT JOIN mentions m ON m.entity_id=e.id "
        "WHERE e.status='active' GROUP BY e.id ORDER BY n DESC, e.name LIMIT ?",
        (limit,))]
    if not names:
        return None
    return "Names that may appear: " + ", ".join(names) + "."


def load_model(cfg: Config, model_name: str | None = None):
    from faster_whisper import WhisperModel  # heavy import, deferred
    w = cfg.whisper
    return WhisperModel(
        model_name or w.get("model", "small"),
        device=w.get("device", "cpu"),
        compute_type=w.get("compute_type", "int8"),
    )


def transcribe_audio(cfg: Config, model, audio_path: Path,
                     initial_prompt: str | None = None) -> dict:
    """Run whisper; return text, segments (timestamps + char offsets), duration."""
    w = cfg.whisper
    seg_iter, info = model.transcribe(
        str(audio_path),
        language=w.get("language") or None,
        vad_filter=bool(w.get("vad_filter", False)),
        initial_prompt=initial_prompt,
    )
    parts: list[str] = []
    segments: list[dict] = []
    pos = 0
    for seg in seg_iter:
        text = seg.text.strip()
        if not text:
            continue
        if parts:
            pos += 1  # joining space
        segments.append({"start_sec": round(seg.start, 2), "end_sec": round(seg.end, 2),
                         "char_start": pos, "char_end": pos + len(text), "text": text})
        parts.append(text)
        pos += len(text)
    full_text = " ".join(parts)
    return {"text": full_text, "segments": segments,
            "duration_sec": round(float(info.duration), 2),
            "language": info.language}


def process_file(conn: sqlite3.Connection, cfg: Config, get_model, path: Path,
                 date_override: str | None = None) -> dict:
    """Transcribe one inbox file into source+transcript rows.

    Does not commit and does NOT archive on the success path — the caller
    commits first and archives after, so a failure can never strand an
    un-ingested file outside the inbox. Duplicate/empty files are archived
    here (no rows are at stake for them).

    get_model is a zero-arg callable so duplicate-only runs never load whisper.

    date_override replaces the resolved journal date BEFORE anything is
    written: the source row, its audit entry, the duplicate check and the
    transcript copy's file name all see the same date. (It used to be a raw
    UPDATE after the fact, leaving all four on the old one.) The recording
    instant still comes from the file — the override says which day it is
    ABOUT, not when it was spoken.
    """
    if date_override is not None and not is_iso_date(date_override):
        raise DomainError(f"invalid journal date '{date_override}' (expected YYYY-MM-DD)")
    if is_placeholder(path):
        read_hydrated(path)  # hydrate before whisper opens it
    data = read_hydrated(path)
    content_hash = sources.content_hash_bytes(data)

    already = conn.execute(
        "SELECT id, journal_date FROM sources WHERE content_hash=? AND type='audio'",
        (content_hash,)).fetchone()
    journal_date, recorded_at = resolve_file_times(path, cfg)
    if date_override is not None:
        journal_date = date_override

    if already:
        archived = archive_file(cfg, path, journal_date)
        return {"file": path.name, "status": "duplicate",
                "warnings": [f"WARNING: {path.name} is byte-identical to source "
                             f"id={already['id']} ({already['journal_date']}) — moved to "
                             f"archive without re-transcribing ({archived})"]}

    prompt = entity_prompt(conn) if cfg.whisper.get("seed_initial_prompt", True) else None
    result = transcribe_audio(cfg, get_model(), path, initial_prompt=prompt)
    if not result["text"].strip():
        archived = archive_file(cfg, path, journal_date)
        return {"file": path.name, "status": "empty",
                "warnings": [f"WARNING: {path.name} produced an empty transcript — "
                             f"archived without creating a source ({archived})"]}

    warnings = sources.duplicate_warnings(
        conn, content_hash=None, journal_date=journal_date,
        recorded_at=recorded_at, duration_sec=result["duration_sec"],
        recorded_tolerance_sec=cfg.ingest.get("duplicate_recorded_at_tolerance_sec", 120),
        duration_tolerance_sec=cfg.ingest.get("duplicate_duration_tolerance_sec", 5))

    src = sources.create(conn, source_type="audio", timezone_name=cfg.timezone,
                         journal_date=journal_date, path=str(path),
                         content_hash=content_hash, recorded_at=recorded_at)
    transcript_id = sources.add_transcript(
        conn, source_id=src["id"], engine="faster-whisper",
        model=cfg.whisper.get("model", "small"), language=result["language"],
        duration_sec=result["duration_sec"], text=result["text"],
        segments=result["segments"])
    copy_path = _write_transcript_copy(cfg, journal_date, src["id"], result["text"])
    return {"file": path.name, "status": "transcribed", "source_id": src["id"],
            "transcript_id": transcript_id, "journal_date": journal_date,
            "duration_sec": result["duration_sec"], "chars": len(result["text"]),
            "segments": len(result["segments"]), "transcript_file": str(copy_path),
            "warnings": warnings}


def archive_file(cfg: Config, path: Path, journal_date: str) -> Path:
    if cfg.archive_dir is None:
        raise DomainError("archive_dir is not configured — set [paths].archive_dir "
                          "in config.toml")
    # Only the year folder is ever created. Creating the archive root too
    # would turn a typo in config.toml into a new folder nobody looks at,
    # silently, on the first archive — after which no check can tell.
    if not cfg.archive_dir.is_dir():
        raise FileNotFoundError(
            f"archive_dir {cfg.archive_dir} does not exist — create it, or fix "
            f"[paths].archive_dir in config.toml")
    year_dir = cfg.archive_dir / journal_date[:4]
    year_dir.mkdir(exist_ok=True)
    dest = year_dir / path.name
    n = 2
    while dest.exists():
        dest = year_dir / f"{path.stem}-{n}{path.suffix}"
        n += 1
    shutil.move(str(path), str(dest))
    return dest


def _archive_after_commit(conn: sqlite3.Connection, cfg: Config, path: Path,
                          res: dict) -> None:
    """Rows are durable; now move the file. If this fails (or the process dies
    first), the file stays in the inbox and the next run archives it via the
    content-hash duplicate branch — nothing is ever silently lost."""
    try:
        archived = archive_file(cfg, path, res["journal_date"])
        conn.execute("UPDATE sources SET path=? WHERE id=?",
                     (str(archived), res["source_id"]))
        conn.commit()
        res["archived_to"] = str(archived)
    except OSError as e:
        res["archive_failed"] = True      # data is safe; the run still needs attention
        res.setdefault("warnings", []).append(
            f"WARNING: could not archive {path.name}: {e} — its rows are "
            f"committed; the next run will move it as a duplicate")


def transcribe_inbox(conn: sqlite3.Connection, cfg: Config,
                     only_file: Path | None = None,
                     date_override: str | None = None,
                     files: list[Path] | None = None) -> list[dict]:
    """Transcribe the inbox (held call recordings left out), one named file,
    or `files` — the call recordings `lifecrm calls take` picked."""
    if date_override is not None and only_file is None:
        # One date for a whole inbox would re-date every recording in it.
        raise DomainError("date_override applies to one recording — pass only_file")
    if only_file:
        files = [only_file]
    elif files is None:
        files = scan_inbox(cfg)
    if not files:
        return []
    model_cache: list = []

    def get_model():
        if not model_cache:
            model_cache.append(load_model(cfg))
        return model_cache[0]

    results = []
    for f in files:
        path = Path(f)
        try:
            res = process_file(conn, cfg, get_model, path, date_override=date_override)
            conn.commit()  # per-file commit so one failure never loses prior work
        except Exception as e:  # keep going; report per file
            conn.rollback()
            results.append({"file": path.name, "status": "failed",
                            "warnings": [f"ERROR: {path.name}: {e}"]})
            continue
        if res["status"] == "transcribed":
            _archive_after_commit(conn, cfg, path, res)
        results.append(res)
    return results


def intake_text_inbox(conn: sqlite3.Connection, cfg: Config) -> list[dict]:
    """Ingest phone-dictated text notes from the text inbox: same rules as
    audio — rollover-aware dates, filename precedence, hash dedupe (warn,
    never block), sentence-boundary segments, commit-then-archive."""
    results = []
    for path in scan_text_inbox(cfg):
        try:
            if is_placeholder(path):
                read_hydrated(path)
            data = read_hydrated(path)
            text = data.decode("utf-8-sig", errors="replace")
            normalized = normalize_text(text)
            if not normalized:
                journal_date, _ = resolve_file_times(path, cfg)
                archived = archive_file(cfg, path, journal_date)
                results.append({"file": path.name, "status": "empty",
                                "warnings": [f"WARNING: {path.name} is empty — "
                                             f"archived without a source ({archived})"]})
                continue
            already = conn.execute(
                "SELECT id, journal_date FROM sources WHERE content_hash=?",
                (sources.content_hash_text(normalized),)).fetchone()
            journal_date, recorded_at = resolve_file_times(path, cfg)
            if already:
                archived = archive_file(cfg, path, journal_date)
                results.append({
                    "file": path.name, "status": "duplicate",
                    "warnings": [f"WARNING: {path.name} matches source "
                                 f"id={already['id']} ({already['journal_date']}) — "
                                 f"archived without re-ingesting ({archived})"]})
                continue
            res = import_text(conn, cfg, text, journal_date=journal_date,
                              source_type="phone_transcript",
                              recorded_at=recorded_at, path=str(path))
            conn.commit()
            res |= {"file": path.name, "status": "imported"}
        except Exception as e:
            conn.rollback()
            results.append({"file": path.name, "status": "failed",
                            "warnings": [f"ERROR: {path.name}: {e}"]})
            continue
        _archive_after_commit(conn, cfg, path, res)
        results.append(res)
    return results

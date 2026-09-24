"""Inbox intake: faster-whisper transcription for audio, direct import for
phone-dictated text. Both feed the same source/transcript/segment tables.

- OneDrive Files On-Demand guard: placeholder files are hydrated (read) with
  retries before processing; files modified in the last 60 seconds are
  skipped as possibly still uploading.
- Date resolution: filename date (YYYY-MM-DD / YYYYMMDD) wins; otherwise the
  file mtime mapped through the per-source timezone and rollover hour. The
  filename also carries the *clock* time (HH.mm.ss, optional UTC offset) —
  the phone knows the recording instant; a OneDrive round trip can make
  mtime the upload time instead.
- Recoverability: rows are committed BEFORE the file leaves the inbox. A
  crash between commit and archive leaves the file in the inbox, where the
  next run recognizes its content hash and archives it without re-ingesting.
- Content-hash duplicates warn (or archive-skip in the inbox flow) — never a
  hard block.
- initial_prompt seeding with known entity names is a marginal aid to roughly
  the first minute of audio only — the alias table is the real fix for
  misspelled names.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .domain import days, sources
from .domain.util import DomainError
from .importer import _write_transcript_copy, import_text, normalize_text

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac", ".wma", ".amr", ".mp4"}
TEXT_EXTS = {".txt", ".md"}
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
MIN_FILE_AGE_SEC = 60

_DATE_RES = [
    re.compile(r"(20\d{2})-(\d{2})-(\d{2})"),
    re.compile(r"(20\d{2})_(\d{2})_(\d{2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
]
# Clock time as the Shortcut writes it: "21.30.05" or "21.30", optionally
# followed by a UTC offset like "-0500". Dots, not colons — colons are illegal
# in filenames on every platform involved.
_TIME_RE = re.compile(r"(?<![\d.])([01]\d|2[0-3])\.([0-5]\d)(?:\.([0-5]\d))?(?![\d.])")
_OFFSET_RE = re.compile(r"(?<![\d])([+-])(\d{2})(\d{2})(?![\d])")


def date_from_filename(name: str) -> str | None:
    for rx in _DATE_RES:
        m = rx.search(name)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return datetime(y, mo, d).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return None


def recorded_at_from_filename(name: str, journal_date: str, tz_name: str,
                              rollover_hour: int) -> str | None:
    """Reconstruct the recording instant from a Shortcut-style filename.

    The filename carries the JOURNAL date (already rolled back for
    early-morning notes) plus the real CLOCK time. So a clock time before the
    rollover hour means the recording actually happened on the calendar day
    AFTER the journal date — undo the rollover to get the true instant.
    Returns UTC ISO, or None when the name carries no parseable time.
    """
    stem = Path(name).stem
    m = _TIME_RE.search(stem)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    second = int(m.group(3) or 0)
    real_date = datetime.strptime(journal_date, "%Y-%m-%d")
    if hour < rollover_hour:
        real_date += timedelta(days=1)
    om = _OFFSET_RE.search(stem[m.end():])
    if om:
        sign = 1 if om.group(1) == "+" else -1
        tz = timezone(sign * timedelta(hours=int(om.group(2)),
                                       minutes=int(om.group(3))))
    else:
        tz = ZoneInfo(tz_name)
    local = real_date.replace(hour=hour, minute=minute, second=second, tzinfo=tz)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_placeholder(path: Path) -> bool:
    """True when the file is a OneDrive Files On-Demand placeholder."""
    try:
        attrs = os.stat(path).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


def read_hydrated(path: Path, retries: int = 3, wait_sec: float = 2.0) -> bytes:
    """Read file bytes; opening a placeholder triggers OneDrive hydration."""
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            return path.read_bytes()
        except OSError as e:
            last_err = e
            time.sleep(wait_sec * (attempt + 1))
    raise DomainError(
        f"could not read {path.name} (OneDrive offline or hydration failed): {last_err}")


def _scan(directory: Path, exts: set[str]) -> list[Path]:
    if not directory or not directory.exists():
        return []
    now = time.time()
    out = []
    for p in sorted(directory.iterdir()):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        try:
            if now - p.stat().st_mtime < MIN_FILE_AGE_SEC:
                continue  # possibly still uploading from the phone
        except OSError:
            continue
        out.append(p)
    return out


def scan_inbox(cfg: Config) -> list[Path]:
    return _scan(cfg.inbox_dir, AUDIO_EXTS)


def scan_text_inbox(cfg: Config) -> list[Path]:
    return _scan(cfg.inbox_text_dir, TEXT_EXTS)


def resolve_file_times(path: Path, cfg: Config) -> tuple[str, str]:
    """(journal_date, recorded_at_utc) for an inbox file.

    Filename date wins over the mtime+rollover calculation — the phone knows
    things the file mtime does not (pinned by test_filename_date_precedence).
    Filename clock time likewise wins over mtime for recorded_at.
    """
    mtime_utc = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    file_date = date_from_filename(path.name)
    journal_date = file_date or days.journal_date_for(
        mtime_utc, cfg.timezone, cfg.day_rollover_hour)
    recorded_at = None
    if file_date:
        recorded_at = recorded_at_from_filename(
            path.name, file_date, cfg.timezone, cfg.day_rollover_hour)
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


def process_file(conn: sqlite3.Connection, cfg: Config, get_model, path: Path) -> dict:
    """Transcribe one inbox file into source+transcript rows.

    Does not commit and does NOT archive on the success path — the caller
    commits first and archives after, so a failure can never strand an
    un-ingested file outside the inbox. Duplicate/empty files are archived
    here (no rows are at stake for them).

    get_model is a zero-arg callable so duplicate-only runs never load whisper.
    """
    if is_placeholder(path):
        read_hydrated(path)  # hydrate before whisper opens it
    data = read_hydrated(path)
    content_hash = sources.content_hash_bytes(data)

    already = conn.execute(
        "SELECT id, journal_date FROM sources WHERE content_hash=? AND type='audio'",
        (content_hash,)).fetchone()
    journal_date, recorded_at = resolve_file_times(path, cfg)

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
                     date_override: str | None = None) -> list[dict]:
    files = [only_file] if only_file else scan_inbox(cfg)
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
            res = process_file(conn, cfg, get_model, path)
            if date_override and res.get("source_id"):
                days.ensure_day(conn, date_override)
                conn.execute("UPDATE sources SET journal_date=? WHERE id=?",
                             (date_override, res["source_id"]))
                res["journal_date"] = date_override
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

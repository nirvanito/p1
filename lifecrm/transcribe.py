"""Local transcription: faster-whisper over the OneDrive inbox.

- OneDrive Files On-Demand guard: placeholder files are hydrated (read) with
  retries before transcription; files modified in the last 60 seconds are
  skipped as possibly still uploading.
- Date resolution: filename date (YYYY-MM-DD / YYYYMMDD) wins; otherwise the
  file mtime mapped through the per-source timezone and rollover hour.
- Processed audio moves to the archive; the content hash detects re-synced
  duplicates (warning, never a hard block).
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
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .domain import days, sources
from .domain.util import DomainError
from .importer import _write_transcript_copy

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".flac", ".wma", ".amr", ".mp4"}
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
MIN_FILE_AGE_SEC = 60

_DATE_RES = [
    re.compile(r"(20\d{2})-(\d{2})-(\d{2})"),
    re.compile(r"(20\d{2})_(\d{2})_(\d{2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
]


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


def scan_inbox(cfg: Config) -> list[Path]:
    if not cfg.inbox_dir.exists():
        return []
    now = time.time()
    out = []
    for p in sorted(cfg.inbox_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
            continue
        try:
            if now - p.stat().st_mtime < MIN_FILE_AGE_SEC:
                continue  # possibly still uploading from the phone
        except OSError:
            continue
        out.append(p)
    return out


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


def process_file(conn: sqlite3.Connection, cfg: Config, model, path: Path) -> dict:
    """Transcribe one inbox file into source+transcript rows, then archive it.
    Does not commit — the caller owns the transaction."""
    if is_placeholder(path):
        read_hydrated(path)  # hydrate before whisper opens it
    data = read_hydrated(path)
    content_hash = sources.content_hash_bytes(data)

    already = conn.execute(
        "SELECT id, journal_date FROM sources WHERE content_hash=? AND type='audio'",
        (content_hash,)).fetchone()

    mtime_utc = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    file_date = date_from_filename(path.name)
    journal_date = file_date or days.journal_date_for(
        mtime_utc, cfg.timezone, cfg.day_rollover_hour)
    recorded_at = mtime_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    if already:
        archived = archive_file(cfg, path, journal_date)
        return {"file": path.name, "status": "duplicate",
                "warnings": [f"WARNING: {path.name} is byte-identical to source "
                             f"id={already['id']} ({already['journal_date']}) — moved to "
                             f"archive without re-transcribing ({archived})"]}

    prompt = entity_prompt(conn) if cfg.whisper.get("seed_initial_prompt", True) else None
    result = transcribe_audio(cfg, model, path, initial_prompt=prompt)
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

    archived = archive_file(cfg, path, journal_date)
    src = sources.create(conn, source_type="audio", timezone_name=cfg.timezone,
                         journal_date=journal_date, path=str(archived),
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
            "archived_to": str(archived), "warnings": warnings}


def archive_file(cfg: Config, path: Path, journal_date: str) -> Path:
    year_dir = cfg.archive_dir / journal_date[:4]
    year_dir.mkdir(parents=True, exist_ok=True)
    dest = year_dir / path.name
    n = 2
    while dest.exists():
        dest = year_dir / f"{path.stem}-{n}{path.suffix}"
        n += 1
    shutil.move(str(path), str(dest))
    return dest


def transcribe_inbox(conn: sqlite3.Connection, cfg: Config,
                     only_file: Path | None = None,
                     date_override: str | None = None) -> list[dict]:
    files = [only_file] if only_file else scan_inbox(cfg)
    if not files:
        return []
    model = load_model(cfg)
    results = []
    for f in files:
        try:
            res = process_file(conn, cfg, model, Path(f))
            if date_override and res.get("source_id"):
                days.ensure_day(conn, date_override)
                conn.execute("UPDATE sources SET journal_date=? WHERE id=?",
                             (date_override, res["source_id"]))
                res["journal_date"] = date_override
            conn.commit()  # per-file commit so one failure never loses prior work
            results.append(res)
        except Exception as e:  # keep going; report per file
            conn.rollback()
            results.append({"file": Path(f).name, "status": "failed",
                            "warnings": [f"ERROR: {Path(f).name}: {e}"]})
    return results

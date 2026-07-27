"""First-class transcript import: pasted text or phone-generated transcripts.

Imported text is split on sentence boundaries into transcript_segments rows
with NULL start_sec/end_sec and populated char_start/char_end — segment
granularity for imported sources is a requirement, not an optional case.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .domain import days, sources
from .domain.util import DomainError, is_iso_date, is_iso_datetime

_BOUNDARY_RE = re.compile(r"[.!?…]+(?=\s|$)")


def segment_text(text: str) -> list[dict]:
    """Sentence-boundary segments with character offsets into `text`.

    Boundaries are runs of sentence punctuation followed by whitespace or end
    of line, so decimals like '3.5' do not split. Newlines always split.
    Offsets index the exact stored transcript text.
    """
    segments: list[dict] = []
    for line_match in re.finditer(r"[^\n]+", text):
        line = line_match.group(0)
        base = line_match.start()
        start = 0
        bounds = [m.end() for m in _BOUNDARY_RE.finditer(line)]
        if not bounds or bounds[-1] < len(line):
            bounds.append(len(line))
        for b in bounds:
            raw = line[start:b]
            lead = len(raw) - len(raw.lstrip())
            trail = len(raw) - len(raw.rstrip())
            s, e = start + lead, b - trail
            if e > s:
                segments.append({"char_start": base + s, "char_end": base + e,
                                 "text": line[s:e]})
            start = b
    return segments


def import_text(conn: sqlite3.Connection, cfg: Config, text: str, *,
                journal_date: str | None = None, source_type: str = "pasted",
                recorded_at: str | None = None, path: str | None = None) -> dict:
    """Create source + transcript + segments for a text transcript.
    Does not commit — the caller owns the transaction."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise DomainError("transcript text is empty")
    if recorded_at is not None and not is_iso_datetime(recorded_at):
        raise DomainError(f"recorded_at '{recorded_at}' is not an ISO datetime")

    if journal_date is None:
        instant = (datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
                   if recorded_at else datetime.now(timezone.utc))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        journal_date = days.journal_date_for(instant, cfg.timezone, cfg.day_rollover_hour)
    elif not is_iso_date(journal_date):
        raise DomainError(f"invalid journal date '{journal_date}' (expected YYYY-MM-DD)")

    content_hash = sources.content_hash_text(text)
    warnings = sources.duplicate_warnings(
        conn, content_hash=content_hash, journal_date=journal_date,
        recorded_at=recorded_at, duration_sec=None,
        recorded_tolerance_sec=cfg.ingest.get("duplicate_recorded_at_tolerance_sec", 120),
        duration_tolerance_sec=cfg.ingest.get("duplicate_duration_tolerance_sec", 5))

    src = sources.create(conn, source_type=source_type, timezone_name=cfg.timezone,
                         journal_date=journal_date, path=path,
                         content_hash=content_hash, recorded_at=recorded_at)
    segments = segment_text(text)
    transcript_id = sources.add_transcript(
        conn, source_id=src["id"], engine="external", text=text, segments=segments)

    copy_path = _write_transcript_copy(cfg, journal_date, src["id"], text)
    return {"source_id": src["id"], "source_uuid": src["uuid"],
            "transcript_id": transcript_id, "journal_date": journal_date,
            "segments": len(segments), "chars": len(text),
            "transcript_file": str(copy_path), "warnings": warnings}


def _write_transcript_copy(cfg: Config, journal_date: str, source_id: int,
                           text: str) -> Path:
    """Human-browsable copy under data/transcripts/YYYY/. The database row is
    canonical; this file is a convenience mirror."""
    year_dir = cfg.transcripts_dir / journal_date[:4]
    year_dir.mkdir(parents=True, exist_ok=True)
    path = year_dir / f"{journal_date}-src{source_id}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path

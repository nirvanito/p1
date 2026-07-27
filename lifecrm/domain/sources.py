"""Source rows: immutable provenance for audio and imported transcripts.

Duplicate content hashes WARN and never block — the user decides. A soft
(recorded_at, duration_sec) similarity check also warns.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from . import audit, days
from .util import DomainError, new_uuid, now_utc

SOURCE_TYPES = ("audio", "pasted", "phone_transcript", "manual")


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def duplicate_warnings(conn: sqlite3.Connection, *, content_hash: str | None,
                       journal_date: str, recorded_at: str | None,
                       duration_sec: float | None,
                       recorded_tolerance_sec: int = 120,
                       duration_tolerance_sec: int = 5) -> list[str]:
    warnings = []
    if content_hash:
        dup = conn.execute(
            "SELECT id, journal_date, path FROM sources WHERE content_hash=?",
            (content_hash,)).fetchone()
        if dup:
            warnings.append(
                f"WARNING: identical content already ingested as source id={dup['id']} "
                f"({dup['journal_date']}{', ' + dup['path'] if dup['path'] else ''}) — "
                f"proceeding anyway; retract or purge if this is an accidental duplicate")
    if recorded_at and duration_sec is not None:
        try:
            rec = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError:
            return warnings
        for row in conn.execute(
                "SELECT s.id, s.recorded_at, t.duration_sec FROM sources s "
                "LEFT JOIN transcripts t ON t.source_id=s.id "
                "WHERE s.journal_date=? AND s.recorded_at IS NOT NULL",
                (journal_date,)).fetchall():
            if row["duration_sec"] is None:
                continue
            try:
                other = datetime.fromisoformat(row["recorded_at"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if (abs((rec - other).total_seconds()) <= recorded_tolerance_sec
                    and abs(duration_sec - row["duration_sec"]) <= duration_tolerance_sec):
                warnings.append(
                    f"WARNING: source id={row['id']} on {journal_date} has a very similar "
                    f"recording time and duration — possible duplicate recording")
    return warnings


def create(conn: sqlite3.Connection, *, source_type: str, timezone_name: str,
           journal_date: str, path: str | None = None,
           content_hash: str | None = None, recorded_at: str | None = None,
           actor: str = "system") -> dict:
    if source_type not in SOURCE_TYPES:
        raise DomainError(
            f"unknown source type '{source_type}' (expected one of: {', '.join(SOURCE_TYPES)})")
    days.ensure_day(conn, journal_date)
    uid = new_uuid()
    cur = conn.execute(
        "INSERT INTO sources (uuid, type, path, content_hash, recorded_at, timezone, "
        "journal_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, source_type, path, content_hash, recorded_at, timezone_name,
         journal_date, now_utc()))
    audit.log(conn, actor, "create", "sources", uid,
              {"type": source_type, "journal_date": journal_date, "path": path})
    return {"id": cur.lastrowid, "uuid": uid}


def add_transcript(conn: sqlite3.Connection, *, source_id: int, engine: str,
                   text: str, model: str | None = None, language: str | None = None,
                   duration_sec: float | None = None,
                   segments: list[dict] | None = None) -> int:
    """Insert a transcript plus its segments. Segments are required for every
    transcript: whisper provides timestamps + char offsets; imported text
    provides sentence-boundary segments with NULL timestamps."""
    if engine not in ("faster-whisper", "external"):
        raise DomainError(f"unknown transcript engine '{engine}'")
    if not segments:
        raise DomainError("a transcript requires at least one segment")
    cur = conn.execute(
        "INSERT INTO transcripts (source_id, engine, model, language, duration_sec, "
        "text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, engine, model, language, duration_sec, text, now_utc()))
    transcript_id = cur.lastrowid
    for i, seg in enumerate(segments):
        if not (0 <= seg["char_start"] < seg["char_end"] <= len(text)):
            raise DomainError(
                f"segment {i} char range [{seg['char_start']}, {seg['char_end']}) "
                f"out of bounds for transcript of length {len(text)}")
        conn.execute(
            "INSERT INTO transcript_segments (transcript_id, idx, start_sec, end_sec, "
            "char_start, char_end, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (transcript_id, i, seg.get("start_sec"), seg.get("end_sec"),
             seg["char_start"], seg["char_end"], seg["text"]))
    return transcript_id


def utc_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

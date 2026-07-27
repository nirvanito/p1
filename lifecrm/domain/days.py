"""Day rows and journal-date computation (per-source timezone + rollover)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .util import DomainError, is_iso_date, now_utc


def journal_date_for(recorded_at_utc: datetime, tz_name: str, rollover_hour: int) -> str:
    """Map a recording instant to its journal date in the given timezone.

    Recordings before the rollover hour (local) belong to the previous day —
    a note recorded at 00:30 is 'yesterday's' journal entry.
    """
    local = recorded_at_utc.astimezone(ZoneInfo(tz_name))
    if local.hour < rollover_hour:
        local = local - timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def ensure_day(conn: sqlite3.Connection, journal_date: str) -> None:
    if not is_iso_date(journal_date):
        raise DomainError(f"invalid journal_date '{journal_date}' (expected YYYY-MM-DD)")
    now = now_utc()
    conn.execute(
        "INSERT INTO days (journal_date, created_at, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(journal_date) DO NOTHING",
        (journal_date, now, now),
    )


def set_summary(conn: sqlite3.Connection, journal_date: str, summary: str) -> None:
    ensure_day(conn, journal_date)
    conn.execute(
        "UPDATE days SET summary=?, updated_at=? WHERE journal_date=?",
        (summary, now_utc(), journal_date),
    )

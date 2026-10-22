"""recall_log writes: what was asked, and whether the database could answer.

The table has existed since schema v1 as the extension point for this; the
value is longitudinal — questions that keep coming back unanswered are the
best possible guide to what to import or record next.
"""

from __future__ import annotations

import sqlite3

from .util import DomainError, now_utc

MAX_SUMMARY = 500


def answered(bundle: dict) -> bool:
    """The one rule `lifecrm ask` and `lifecrm serve` both log: retrieval's
    own verdict (a question that asked for numbers is answered only when it
    got some), else whether any evidence came back at all."""
    return bool(bundle.get("answered", bundle.get("has_evidence")))


def log_question(conn: sqlite3.Connection, question: str, *, answered: bool,
                 answer_summary: str | None = None) -> int:
    question = (question or "").strip()
    if not question:
        raise DomainError("recall question must be non-empty")
    summary = (answer_summary or "").strip() or None
    if summary and len(summary) > MAX_SUMMARY:
        summary = summary[:MAX_SUMMARY - 1] + "…"
    cur = conn.execute(
        "INSERT INTO recall_log (asked_at, question, answered, answer_summary) "
        "VALUES (?, ?, ?, ?)",
        (now_utc(), question, 1 if answered else 0, summary))
    return cur.lastrowid


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id, asked_at, question, answered, answer_summary "
        "FROM recall_log ORDER BY id DESC LIMIT ?", (limit,))]


def unanswered_themes(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Questions the database could not answer, most recent first — the
    build-this-next list."""
    return [dict(r) for r in conn.execute(
        "SELECT question, COUNT(*) AS times, MAX(asked_at) AS last_asked "
        "FROM recall_log WHERE answered=0 GROUP BY LOWER(question) "
        "ORDER BY times DESC, last_asked DESC LIMIT ?", (limit,))]

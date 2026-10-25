"""Entry creation and snapshots. Exactly-one-primary-category is enforced here,
in the domain layer — the partial unique index in the schema is a backstop only."""

from __future__ import annotations

import json
import sqlite3

from ..config import CONFIDENCE, TIME_OF_DAY
from . import audit, days
from .util import DomainError, is_iso_date, is_iso_datetime, new_uuid, now_utc


def category_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row is None:
        known = [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY id")]
        raise DomainError(f"unknown category '{name}' (known: {', '.join(known)})")
    return row["id"]


def kind_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute(
        "SELECT id FROM entry_kinds WHERE name=? COLLATE NOCASE", (name,)).fetchone()
    if row is None:
        known = [r["name"] for r in conn.execute("SELECT name FROM entry_kinds ORDER BY id")]
        raise DomainError(f"unknown entry kind '{name}' (known: {', '.join(known)})")
    return row["id"]


def get_by_uuid(conn: sqlite3.Connection, entry_uuid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM entries WHERE uuid=?", (entry_uuid,)).fetchone()
    if row is None:
        raise DomainError(f"entry {entry_uuid} does not exist")
    return row


def snapshot(conn: sqlite3.Connection, entry_id: int) -> dict:
    """Full JSON snapshot of an entry for revision records."""
    e = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    if e is None:
        raise DomainError(f"entry id={entry_id} does not exist")
    cats = conn.execute(
        "SELECT c.name, ec.is_primary FROM entry_categories ec "
        "JOIN categories c ON c.id=ec.category_id WHERE ec.entry_id=? "
        "ORDER BY ec.is_primary DESC, c.name", (entry_id,)).fetchall()
    kind = conn.execute(
        "SELECT name FROM entry_kinds WHERE id=?", (e["kind_id"],)).fetchone()["name"]
    mentions = conn.execute(
        "SELECT en.id, en.name, en.type, m.role FROM mentions m "
        "JOIN entities en ON en.id=m.entity_id WHERE m.entry_id=? ORDER BY en.name",
        (entry_id,)).fetchall()
    meas = conn.execute(
        "SELECT uuid, metric, numeric_value, text_value, unit, status FROM measurements "
        "WHERE entry_id=? ORDER BY id", (entry_id,)).fetchall()
    return {
        "uuid": e["uuid"], "journal_date": e["journal_date"], "kind": kind,
        "title": e["title"], "body": e["body"], "time_of_day": e["time_of_day"],
        "occurred_at": e["occurred_at"], "ended_at": e["ended_at"], "due_at": e["due_at"],
        "data": json.loads(e["data"]) if e["data"] else None,
        "confidence": e["confidence"], "status": e["status"],
        "confirmed_at": e["confirmed_at"],
        "closed_at": e["closed_at"], "closure": e["closure"],
        "categories": {"primary": next((c["name"] for c in cats if c["is_primary"]), None),
                       "secondary": [c["name"] for c in cats if not c["is_primary"]]},
        "entities": [dict(m) for m in mentions],
        "measurements": [dict(m) for m in meas],
    }


def _validate_common(time_of_day, occurred_at, ended_at, due_at, confidence) -> None:
    if time_of_day is not None and time_of_day not in TIME_OF_DAY:
        raise DomainError(
            f"time_of_day '{time_of_day}' must be one of: {', '.join(TIME_OF_DAY)}")
    for label, value in (("occurred_at", occurred_at), ("ended_at", ended_at),
                         ("due_at", due_at)):
        if value is not None and not is_iso_datetime(value) and not is_iso_date(value):
            raise DomainError(f"{label} '{value}' is not an ISO date or datetime")
    if confidence not in CONFIDENCE:
        raise DomainError(f"confidence must be one of: {', '.join(CONFIDENCE)}")


def set_categories(conn: sqlite3.Connection, entry_id: int, primary: str,
                   secondary: list[str] | None = None) -> None:
    """Replace an entry's categories. Exactly one primary, no duplicates."""
    if not primary:
        raise DomainError("an entry requires exactly one primary category")
    primary_id = category_id(conn, primary)
    seen = {primary_id}
    secondary_ids = []
    for s in secondary or []:
        sid = category_id(conn, s)
        if sid == primary_id:
            continue  # primary listed again as secondary — ignore, keep one primary
        if sid not in seen:
            seen.add(sid)
            secondary_ids.append(sid)
    conn.execute("DELETE FROM entry_categories WHERE entry_id=?", (entry_id,))
    conn.execute(
        "INSERT INTO entry_categories (entry_id, category_id, is_primary) VALUES (?, ?, 1)",
        (entry_id, primary_id))
    for sid in secondary_ids:
        conn.execute(
            "INSERT INTO entry_categories (entry_id, category_id, is_primary) VALUES (?, ?, 0)",
            (entry_id, sid))


def create(conn: sqlite3.Connection, *, journal_date: str, kind: str, title: str,
           primary_category: str, secondary_categories: list[str] | None = None,
           body: str | None = None, time_of_day: str | None = None,
           occurred_at: str | None = None, ended_at: str | None = None,
           due_at: str | None = None, data: dict | None = None,
           confidence: str = "high", actor: str = "user",
           confirmed: bool = False, note: str | None = None) -> dict:
    if not title or not title.strip():
        raise DomainError("entry title must be non-empty")
    days.ensure_day(conn, journal_date)
    kid = kind_id(conn, kind)
    _validate_common(time_of_day, occurred_at, ended_at, due_at, confidence)
    from . import goals as goals_mod
    goals_mod.validate_entry_data(kind, data)
    now = now_utc()
    uid = new_uuid()
    cur = conn.execute(
        "INSERT INTO entries (uuid, journal_date, kind_id, title, body, time_of_day, "
        "occurred_at, ended_at, due_at, data, confidence, status, confirmed_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (uid, journal_date, kid, title.strip(), body, time_of_day, occurred_at,
         ended_at, due_at, json.dumps(data, ensure_ascii=False) if data else None,
         confidence, now if (confirmed or actor == "user") else None, now, now))
    entry_id = cur.lastrowid
    set_categories(conn, entry_id, primary_category, secondary_categories)
    snap = snapshot(conn, entry_id)
    conn.execute(
        "INSERT INTO entry_revisions (entry_uuid, revised_at, actor, change_kind, "
        "old_json, new_json, note) VALUES (?, ?, ?, 'create', NULL, ?, ?)",
        (uid, now, actor, json.dumps(snap, ensure_ascii=False), note))
    audit.log(conn, actor, "create", "entries", uid,
              {"journal_date": journal_date, "title": title})
    return {"id": entry_id, "uuid": uid}


def attach_entity(conn: sqlite3.Connection, entry_id: int, entity_id: int,
                  role: str = "mention", actor: str = "user") -> None:
    from ..config import MENTION_ROLES
    from . import entities as entities_mod
    if role not in MENTION_ROLES:
        raise DomainError(f"mention role '{role}' must be one of: {', '.join(MENTION_ROLES)}")
    entities_mod.get_active(conn, entity_id)
    conn.execute(
        "INSERT OR IGNORE INTO mentions (entry_id, entity_id, role) VALUES (?, ?, ?)",
        (entry_id, entity_id, role))


def detach_entity(conn: sqlite3.Connection, entry_id: int, entity_id: int,
                  role: str | None = None) -> int:
    if role:
        cur = conn.execute("DELETE FROM mentions WHERE entry_id=? AND entity_id=? AND role=?",
                           (entry_id, entity_id, role))
    else:
        cur = conn.execute("DELETE FROM mentions WHERE entry_id=? AND entity_id=?",
                           (entry_id, entity_id))
    return cur.rowcount


# A second call for the same (entry, source) fills gaps and never replaces.
# The passage columns move as one unit: a row that already points at a
# passage keeps all four, so offsets from two different passages never mix.
_PASSAGE_COLS = ("segment_start", "segment_end", "char_start", "char_end")
_HAS_PASSAGE = ("entry_sources.segment_start IS NOT NULL "
                "OR entry_sources.char_start IS NOT NULL")
_UPSERT_PROVENANCE = (
    "INSERT INTO entry_sources (entry_id, source_id, transcript_id, "
    "segment_start, segment_end, char_start, char_end, extraction_run_id, "
    "original_json, confirmed_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(entry_id, source_id) DO UPDATE SET "
    + ", ".join(
        [f"{c}=COALESCE(entry_sources.{c}, excluded.{c})"
         for c in ("transcript_id", "extraction_run_id", "original_json",
                   "confirmed_json")]
        + [f"{c}=CASE WHEN {_HAS_PASSAGE} THEN entry_sources.{c} "
           f"ELSE excluded.{c} END" for c in _PASSAGE_COLS]))


def add_provenance(conn: sqlite3.Connection, *, entry_id: int, source_id: int,
                   transcript_id: int | None = None,
                   segment_start: int | None = None, segment_end: int | None = None,
                   char_start: int | None = None, char_end: int | None = None,
                   extraction_run_id: int | None = None,
                   original_json: dict | None = None,
                   confirmed_json: dict | None = None) -> None:
    """Record that a source supports an entry — without ever clobbering.

    The key is (entry_id, source_id). An entry_update that closes an entry
    from the same source that created it used to INSERT OR REPLACE that row,
    and the passage and original extraction JSON behind the entry were gone
    for good. An existing row now keeps what it has; only its gaps are filled.
    """
    conn.execute(
        _UPSERT_PROVENANCE,
        (entry_id, source_id, transcript_id, segment_start, segment_end, char_start,
         char_end, extraction_run_id,
         json.dumps(original_json, ensure_ascii=False) if original_json else None,
         json.dumps(confirmed_json, ensure_ascii=False) if confirmed_json else None))

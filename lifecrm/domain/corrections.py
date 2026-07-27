"""User corrections: edit, retract, restore, and merge entries.

Every correction records an entry_revisions row (old/new snapshots) plus an
audit_log row. User-driven corrections stamp confirmed_at so later automated
extractions can never silently overwrite them.
"""

from __future__ import annotations

import json
import sqlite3

from . import audit, days, entries
from .util import DomainError, now_utc

EDITABLE_FIELDS = {
    "title", "body", "kind", "time_of_day", "occurred_at", "ended_at", "due_at",
    "data", "confidence", "primary_category", "secondary_categories", "journal_date",
}


def _revision(conn: sqlite3.Connection, entry_uuid: str, actor: str, change_kind: str,
              old: dict | None, new: dict | None, note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO entry_revisions (entry_uuid, revised_at, actor, change_kind, "
        "old_json, new_json, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entry_uuid, now_utc(), actor, change_kind,
         json.dumps(old, ensure_ascii=False) if old else None,
         json.dumps(new, ensure_ascii=False) if new else None, note))


def edit_entry(conn: sqlite3.Connection, entry_uuid: str, changes: dict,
               actor: str = "user", note: str | None = None) -> dict:
    unknown = set(changes) - EDITABLE_FIELDS
    if unknown:
        raise DomainError(
            f"unknown field(s): {', '.join(sorted(unknown))} "
            f"(editable: {', '.join(sorted(EDITABLE_FIELDS))})")
    if not changes:
        raise DomainError("no changes given")
    row = entries.get_by_uuid(conn, entry_uuid)
    if row["status"] == "retracted":
        raise DomainError(f"entry {entry_uuid} is retracted — restore it before editing")
    old = entries.snapshot(conn, row["id"])
    now = now_utc()

    col_updates: dict[str, object] = {}
    if "title" in changes:
        if not str(changes["title"]).strip():
            raise DomainError("entry title must be non-empty")
        col_updates["title"] = str(changes["title"]).strip()
    if "body" in changes:
        col_updates["body"] = changes["body"]
    if "kind" in changes:
        col_updates["kind_id"] = entries.kind_id(conn, changes["kind"])
    for f in ("time_of_day", "occurred_at", "ended_at", "due_at", "confidence"):
        if f in changes:
            col_updates[f] = changes[f]
    if "data" in changes:
        col_updates["data"] = (json.dumps(changes["data"], ensure_ascii=False)
                               if changes["data"] is not None else None)
    if "journal_date" in changes:
        days.ensure_day(conn, changes["journal_date"])
        col_updates["journal_date"] = changes["journal_date"]

    # Validate enum/datetime fields using the same rules as create.
    entries._validate_common(
        col_updates.get("time_of_day", row["time_of_day"]),
        col_updates.get("occurred_at", row["occurred_at"]),
        col_updates.get("ended_at", row["ended_at"]),
        col_updates.get("due_at", row["due_at"]),
        col_updates.get("confidence", row["confidence"]))

    if col_updates:
        sets = ", ".join(f"{k}=?" for k in col_updates)
        conn.execute(f"UPDATE entries SET {sets}, updated_at=? WHERE id=?",
                     (*col_updates.values(), now, row["id"]))
    if "primary_category" in changes or "secondary_categories" in changes:
        primary = changes.get("primary_category", old["categories"]["primary"])
        secondary = changes.get("secondary_categories", old["categories"]["secondary"])
        entries.set_categories(conn, row["id"], primary, secondary)
    if actor == "user":
        conn.execute("UPDATE entries SET confirmed_at=? WHERE id=?", (now, row["id"]))

    new = entries.snapshot(conn, row["id"])
    _revision(conn, entry_uuid, actor, "edit", old, new, note)
    audit.log(conn, actor, "edit", "entries", entry_uuid,
              {"fields": sorted(changes.keys())})
    return new


def retract_entry(conn: sqlite3.Connection, entry_uuid: str, actor: str = "user",
                  reason: str | None = None) -> None:
    row = entries.get_by_uuid(conn, entry_uuid)
    if row["status"] == "retracted":
        raise DomainError(f"entry {entry_uuid} is already retracted")
    old = entries.snapshot(conn, row["id"])
    conn.execute("UPDATE entries SET status='retracted', updated_at=? WHERE id=?",
                 (now_utc(), row["id"]))
    new = entries.snapshot(conn, row["id"])
    _revision(conn, entry_uuid, actor, "retract", old, new, reason)
    audit.log(conn, actor, "retract", "entries", entry_uuid, {"reason": reason})


def restore_entry(conn: sqlite3.Connection, entry_uuid: str, actor: str = "user",
                  note: str | None = None) -> None:
    row = entries.get_by_uuid(conn, entry_uuid)
    if row["status"] != "retracted":
        raise DomainError(f"entry {entry_uuid} is not retracted (status: {row['status']})")
    old = entries.snapshot(conn, row["id"])
    now = now_utc()
    conn.execute(
        "UPDATE entries SET status='active', updated_at=?, confirmed_at=? WHERE id=?",
        (now, now if actor == "user" else row["confirmed_at"], row["id"]))
    new = entries.snapshot(conn, row["id"])
    _revision(conn, entry_uuid, actor, "restore", old, new, note)
    audit.log(conn, actor, "restore", "entries", entry_uuid, None)


def merge_entries(conn: sqlite3.Connection, src_uuid: str, dst_uuid: str,
                  actor: str = "user", note: str | None = None) -> dict:
    """Merge duplicate entries: src's mentions, measurements, and provenance move
    to dst; src becomes status='superseded' pointing at dst. Nothing is deleted."""
    if src_uuid == dst_uuid:
        raise DomainError("cannot merge an entry into itself")
    src = entries.get_by_uuid(conn, src_uuid)
    dst = entries.get_by_uuid(conn, dst_uuid)
    for label, row in (("source", src), ("destination", dst)):
        if row["status"] != "active":
            raise DomainError(f"{label} entry {row['uuid']} is not active (status: {row['status']})")
    old_src = entries.snapshot(conn, src["id"])
    old_dst = entries.snapshot(conn, dst["id"])
    now = now_utc()

    conn.execute("UPDATE OR IGNORE mentions SET entry_id=? WHERE entry_id=?",
                 (dst["id"], src["id"]))
    conn.execute("DELETE FROM mentions WHERE entry_id=?", (src["id"],))
    conn.execute("UPDATE measurements SET entry_id=? WHERE entry_id=?",
                 (dst["id"], src["id"]))
    conn.execute("UPDATE OR IGNORE entry_sources SET entry_id=? WHERE entry_id=?",
                 (dst["id"], src["id"]))
    conn.execute("DELETE FROM entry_sources WHERE entry_id=?", (src["id"],))
    conn.execute(
        "UPDATE entries SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
        (dst["id"], now, src["id"]))
    if actor == "user":
        conn.execute("UPDATE entries SET confirmed_at=?, updated_at=? WHERE id=?",
                     (now, now, dst["id"]))

    _revision(conn, src_uuid, actor, "merge_out", old_src,
              entries.snapshot(conn, src["id"]), note or f"merged into {dst_uuid}")
    _revision(conn, dst_uuid, actor, "merge_in", old_dst,
              entries.snapshot(conn, dst["id"]), note or f"absorbed {src_uuid}")
    audit.log(conn, actor, "merge", "entries", src_uuid, {"into": dst_uuid})
    return {"src": src_uuid, "dst": dst_uuid}


def confirm_entry(conn: sqlite3.Connection, entry_uuid: str) -> None:
    row = entries.get_by_uuid(conn, entry_uuid)
    conn.execute("UPDATE entries SET confirmed_at=?, updated_at=? WHERE id=?",
                 (now_utc(), now_utc(), row["id"]))
    audit.log(conn, "user", "confirm", "entries", entry_uuid, None)


def purge_day(conn: sqlite3.Connection, journal_date: str, actor: str = "user") -> dict:
    """Destructive: delete every entry (and cascaded rows) for a day. The only
    destructive escape hatch — routine cleanup uses retract/merge instead."""
    rows = conn.execute(
        "SELECT id, uuid, title FROM entries WHERE journal_date=?", (journal_date,)).fetchall()
    for r in rows:
        audit.log(conn, actor, "purge", "entries", r["uuid"],
                  {"journal_date": journal_date, "title": r["title"]})
    conn.execute("DELETE FROM entries WHERE journal_date=?", (journal_date,))
    conn.execute("DELETE FROM measurements WHERE journal_date=?", (journal_date,))
    conn.execute("UPDATE days SET summary=NULL, updated_at=? WHERE journal_date=?",
                 (now_utc(), journal_date))
    audit.log(conn, actor, "purge_day", "days", None, {"journal_date": journal_date,
                                                       "entries_deleted": len(rows)})
    return {"entries_deleted": len(rows)}

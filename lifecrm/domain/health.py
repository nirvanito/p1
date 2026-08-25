"""Imported health records: the deterministic metrics layer.

`measurements` holds what the journal said; `health_records` holds what
sensors and apps recorded, imported deterministically from export files.
Identity is (metric, external_id) — the source system's own record uuid — so
re-importing an export is always safe. Classification against an existing row:

- identical:  every data field matches — skip.
- refreshed:  data matches but the source's last_modified_at moved — refresh
              that one field silently (metadata, not data; no audit noise).
- update:     data differs AND the incoming last_modified_at is strictly
              newer — apply, audit-logged with old and new values.
- conflict:   data differs but the incoming record is NOT newer — never
              applied, surfaced in the import report instead.
"""

from __future__ import annotations

import json
import sqlite3

from . import audit, days
from .util import DomainError, canonical_json, new_uuid, now_utc

# Fields that constitute the record's data (identity and bookkeeping excluded).
COMPARE_FIELDS = ("journal_date", "started_at", "ended_at", "utc_offset_sec",
                  "value", "unit", "value_json", "origin_app")


def validate(rec: dict) -> str | None:
    """Single-line error or None. `rec` carries value_json already canonical."""
    for key in ("metric", "journal_date", "started_at", "external_id", "source_file"):
        if not isinstance(rec.get(key), str) or not rec[key].strip():
            return f"health record needs a non-empty '{key}'"
    if rec.get("value") is None and rec.get("value_json") is None:
        return f"health record '{rec['metric']}' needs value or value_json"
    if rec.get("value") is not None and not isinstance(rec["value"], (int, float)):
        return f"health record '{rec['metric']}' value must be a number"
    if rec.get("ended_at") is not None and rec["ended_at"] < rec["started_at"]:
        return (f"health record '{rec['metric']}' ends before it starts "
                f"({rec['ended_at']} < {rec['started_at']})")
    return None


def existing_by_key(conn: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    return {(r["metric"], r["external_id"]): r
            for r in conn.execute("SELECT * FROM health_records")}


def classify(row: sqlite3.Row, rec: dict) -> tuple[str, dict]:
    """('identical'|'refreshed'|'update'|'conflict', {field: (old, new)})."""
    changes = {f: (row[f], rec.get(f)) for f in COMPARE_FIELDS
               if row[f] != rec.get(f)}
    if not changes:
        return (("refreshed" if rec.get("last_modified_at") != row["last_modified_at"]
                 else "identical"), {})
    incoming, current = rec.get("last_modified_at"), row["last_modified_at"]
    if incoming is not None and (current is None or incoming > current):
        return "update", changes
    return "conflict", changes


def insert(conn: sqlite3.Connection, rec: dict, actor: str = "import") -> str:
    err = validate(rec)
    if err:
        raise DomainError(err)
    days.ensure_day(conn, rec["journal_date"])
    uid = new_uuid()
    now = now_utc()
    conn.execute(
        "INSERT INTO health_records (uuid, metric, journal_date, started_at, "
        "ended_at, utc_offset_sec, value, unit, value_json, origin_app, "
        "external_id, source_file, last_modified_at, status, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (uid, rec["metric"], rec["journal_date"], rec["started_at"],
         rec.get("ended_at"), rec.get("utc_offset_sec"), rec.get("value"),
         rec.get("unit"), rec.get("value_json"), rec.get("origin_app"),
         rec["external_id"], rec["source_file"], rec.get("last_modified_at"),
         now, now))
    audit.log(conn, actor, "create", "health_records", uid,
              {"metric": rec["metric"], "journal_date": rec["journal_date"],
               "value": rec.get("value"), "unit": rec.get("unit"),
               "external_id": rec["external_id"],
               "source_file": rec["source_file"]})
    return uid


def update(conn: sqlite3.Connection, row: sqlite3.Row, rec: dict,
           changes: dict, actor: str = "import") -> None:
    err = validate(rec)
    if err:
        raise DomainError(err)
    days.ensure_day(conn, rec["journal_date"])
    conn.execute(
        "UPDATE health_records SET journal_date=?, started_at=?, ended_at=?, "
        "utc_offset_sec=?, value=?, unit=?, value_json=?, origin_app=?, "
        "source_file=?, last_modified_at=?, updated_at=? WHERE uuid=?",
        (rec["journal_date"], rec["started_at"], rec.get("ended_at"),
         rec.get("utc_offset_sec"), rec.get("value"), rec.get("unit"),
         rec.get("value_json"), rec.get("origin_app"), rec["source_file"],
         rec.get("last_modified_at"), now_utc(), row["uuid"]))
    audit.log(conn, actor, "update", "health_records", row["uuid"],
              {"metric": rec["metric"], "external_id": rec["external_id"],
               "source_file": rec["source_file"],
               "changes": {f: {"old": old, "new": new}
                           for f, (old, new) in changes.items()}})


def refresh_modified(conn: sqlite3.Connection, row: sqlite3.Row, rec: dict) -> None:
    """Data unchanged, only the source's own edit clock moved. Metadata, not a
    revision — deliberately not audit-logged to keep the log meaningful."""
    conn.execute(
        "UPDATE health_records SET last_modified_at=?, source_file=?, "
        "updated_at=? WHERE uuid=?",
        (rec.get("last_modified_at"), rec["source_file"], now_utc(), row["uuid"]))

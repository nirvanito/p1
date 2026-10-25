"""Snooze and dismiss state for nudges (schema v6, `nudge_states`).

A nudge is recomputed from the records every time; the key is what stays
the same between runs (`due:<entry uuid>`, `birthday:<entity id>:<year>`,
...). The owner's "not now" or "never" attaches to the key, never to a
numbered line of one run's output, so an import or a closed task between two
runs can never move a snooze onto the wrong item. Every change here is an
audit row; `restore` deletes the row and the audit log keeps what it was.
"""

from __future__ import annotations

import re
import sqlite3

from . import audit
from .util import DomainError, is_iso_date, now_utc

KINDS = ("due", "birthday", "followup", "lapsed", "drift", "nutrient", "stale", "goal")
STATES = ("snoozed", "dismissed")
KEY_RE = re.compile(rf"^({'|'.join(KINDS)}):\S+$")


def check_key(key: str) -> str:
    if not isinstance(key, str) or not KEY_RE.match(key):
        raise DomainError(f"nudge key must look like <kind>:<id> with kind one of "
                          f"{', '.join(KINDS)}, got {key!r}")
    return key


def get(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM nudge_states WHERE nudge_key=?",
                        (key,)).fetchone()


def states(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM nudge_states ORDER BY state, until, nudge_key")]


def state_for(conn: sqlite3.Connection) -> dict[str, dict]:
    """{key: row} for every snoozed or dismissed nudge."""
    return {r["nudge_key"]: r for r in states(conn)}


def _set(conn: sqlite3.Connection, key: str, state: str, until: str | None,
         note: str | None, actor: str) -> dict:
    row = get(conn, key)
    now = now_utc()
    if row is None:
        conn.execute(
            "INSERT INTO nudge_states (nudge_key, state, until, note, actor, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, state, until, note, actor, now, now))
    else:
        conn.execute(
            "UPDATE nudge_states SET state=?, until=?, note=?, actor=?, updated_at=? "
            "WHERE nudge_key=?", (state, until, note, actor, now, key))
    audit.log(conn, actor, state[:-1] if state == "snoozed" else "dismiss",
              "nudge_states", key,
              {"until": until, "note": note,
               "was": {"state": row["state"], "until": row["until"]} if row else None})
    return dict(get(conn, key))


def snooze(conn: sqlite3.Connection, key: str, *, until: str,
           note: str | None = None, actor: str = "user") -> dict:
    """Hide the nudge until `until` (an ISO date); it comes back that day."""
    check_key(key)
    if not is_iso_date(until):
        raise DomainError(f"snooze until must be a date YYYY-MM-DD, got {until!r}")
    return _set(conn, key, "snoozed", until, note, actor)


def dismiss(conn: sqlite3.Connection, key: str, *, note: str | None = None,
            actor: str = "user") -> dict:
    """Hide the nudge for good (until `restore`)."""
    check_key(key)
    return _set(conn, key, "dismissed", None, note, actor)


def restore(conn: sqlite3.Connection, key: str, actor: str = "user") -> dict:
    """Forget a snooze or dismissal; the nudge shows again if it still holds."""
    check_key(key)
    row = get(conn, key)
    if row is None:
        raise DomainError(f"nudge {key} is neither snoozed nor dismissed")
    conn.execute("DELETE FROM nudge_states WHERE nudge_key=?", (key,))
    audit.log(conn, actor, "restore", "nudge_states", key,
              {"was": {"state": row["state"], "until": row["until"],
                       "note": row["note"]}})
    return {"nudge_key": key, "was": row["state"]}

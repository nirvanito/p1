"""Revision state of the published reminders calendar (schema v6,
`reminder_revisions`).

The calendar is regenerated from the records on every publication. A
calendar client only re-reads a component whose SEQUENCE moved (RFC 5545
3.8.7.4), so the publisher asks this module, inside its own write
transaction, which components changed since the last publication and what
their sequence numbers are now. This is the only writer of the table; the
audit row it leaves says how many components moved, never what they say.
`obsidian.change_stamp` ignores that row: a publication is bookkeeping, not
a change to the database's content.
"""

from __future__ import annotations

import sqlite3

from . import audit
from .collect import TABLE as COLLECT_TABLE
from .util import DomainError, is_iso_datetime

TABLE = "reminder_revisions"

# What counts as a change to the database's content: every audited write
# except a publication here, which is bookkeeping about the records and must
# not re-date the vault's start pages or re-trigger the hook that caused it,
# and a collect run's outcome (domain/collect.py), bookkeeping about the
# imports. obsidian's as-of date and nudges.as_of_date filter by this.
CONTENT_CHANGE = f"table_name NOT IN ('{TABLE}', '{COLLECT_TABLE}')"
# What re-renders the vault and republishes the calendar after a command
# (obsidian.change_stamp): content, and a collect outcome too — a source held
# this morning belongs on Home's Today this morning. Never a publication,
# which the hook itself writes.
REFRESH_CHANGE = f"table_name IS NOT '{TABLE}'"


def revisions(conn: sqlite3.Connection) -> dict[str, dict]:
    """{uid: row} for every component ever published, tombstones included."""
    return {r["uid"]: dict(r) for r in conn.execute(
        f"SELECT * FROM {TABLE} ORDER BY uid")}


def record_publication(conn: sqlite3.Connection, components: dict[str, str],
                       now: str, actor: str = "system") -> dict:
    """Apply one publication's components (uid -> fingerprint) to the stored
    revisions and return what to stamp them with.

    Rules: a new uid starts at sequence 0; a present component whose
    fingerprint changed moves to sequence + 1 and last_modified = now; one
    that comes back after being left out does the same (from its tombstone,
    so a client that saw the old sequence sees a newer one); an unchanged
    component keeps both; a component no longer published becomes a tombstone
    (present = 0) and keeps its sequence for the day it returns.

    Returns {"stamps": {uid: {"sequence", "last_modified"}}, "added",
    "changed", "restored", "removed"}. One audit row, only when something
    moved; an identical publication leaves no trace.
    """
    if not isinstance(now, str) or not is_iso_datetime(now):
        raise DomainError(f"publication instant must be an ISO datetime, got {now!r}")
    for uid, fp in components.items():
        if not isinstance(uid, str) or not uid.strip():
            raise DomainError("every calendar component needs a non-empty UID")
        if not isinstance(fp, str) or not fp.strip():
            raise DomainError(f"component {uid} needs a non-empty fingerprint")
    stored = revisions(conn)
    stamps: dict[str, dict] = {}
    counts = {"added": 0, "changed": 0, "restored": 0, "removed": 0}
    for uid in sorted(components):
        fp = components[uid]
        row = stored.get(uid)
        if row is None:
            conn.execute(
                f"INSERT INTO {TABLE} (uid, sequence, fingerprint, last_modified, "
                f"present, updated_at) VALUES (?, 0, ?, ?, 1, ?)", (uid, fp, now, now))
            stamps[uid] = {"sequence": 0, "last_modified": now}
            counts["added"] += 1
        elif not row["present"]:
            seq = row["sequence"] + 1
            conn.execute(
                f"UPDATE {TABLE} SET sequence=?, fingerprint=?, last_modified=?, "
                f"present=1, updated_at=? WHERE uid=?", (seq, fp, now, now, uid))
            stamps[uid] = {"sequence": seq, "last_modified": now}
            counts["restored"] += 1
        elif row["fingerprint"] != fp:
            seq = row["sequence"] + 1
            conn.execute(
                f"UPDATE {TABLE} SET sequence=?, fingerprint=?, last_modified=?, "
                f"updated_at=? WHERE uid=?", (seq, fp, now, now, uid))
            stamps[uid] = {"sequence": seq, "last_modified": now}
            counts["changed"] += 1
        else:
            stamps[uid] = {"sequence": row["sequence"],
                           "last_modified": row["last_modified"]}
    for uid, row in stored.items():
        if row["present"] and uid not in components:
            conn.execute(f"UPDATE {TABLE} SET present=0, updated_at=? WHERE uid=?",
                         (now, uid))
            counts["removed"] += 1
    if any(counts.values()):
        audit.log(conn, actor, "publish", TABLE, None,
                  {**counts, "components": len(components)})
    return {"stamps": stamps, **counts}

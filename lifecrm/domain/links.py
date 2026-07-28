"""Entry links: typed edges between entries — the thread structure that is
only knowable while the memory is fresh.

Direction conventions (documented in schema.sql):
    later continues earlier      new_event resolves old_task
    child part_of parent         cause caused effect
    newer contradicts older      relates is loosely symmetric; store once
"""

from __future__ import annotations

import sqlite3

from . import audit, entries
from .util import DomainError, now_utc

LINK_TYPES = ("continues", "caused", "resolves", "contradicts", "part_of", "relates")


def add(conn: sqlite3.Connection, from_uuid: str, to_uuid: str, link_type: str,
        note: str | None = None, actor: str = "user") -> bool:
    """Create a link. Returns False when the identical link already exists."""
    if link_type not in LINK_TYPES:
        raise DomainError(f"unknown link type '{link_type}' "
                          f"(expected one of: {', '.join(LINK_TYPES)})")
    if from_uuid == to_uuid:
        raise DomainError("a link cannot connect an entry to itself")
    entries.get_by_uuid(conn, from_uuid)   # raises if missing
    entries.get_by_uuid(conn, to_uuid)
    cur = conn.execute(
        "INSERT OR IGNORE INTO entry_links (from_uuid, to_uuid, type, note, "
        "actor, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (from_uuid, to_uuid, link_type, note, actor, now_utc()))
    if cur.rowcount:
        audit.log(conn, actor, "link", "entry_links", from_uuid,
                  {"to": to_uuid, "type": link_type, "note": note})
    return bool(cur.rowcount)


def remove(conn: sqlite3.Connection, from_uuid: str, to_uuid: str,
           link_type: str, actor: str = "user") -> None:
    cur = conn.execute(
        "DELETE FROM entry_links WHERE from_uuid=? AND to_uuid=? AND type=?",
        (from_uuid, to_uuid, link_type))
    if not cur.rowcount:
        raise DomainError(
            f"no {link_type} link from {from_uuid} to {to_uuid}")
    audit.log(conn, actor, "unlink", "entry_links", from_uuid,
              {"to": to_uuid, "type": link_type})


def for_entry(conn: sqlite3.Connection, entry_uuid: str) -> dict:
    """Outgoing and incoming links with enough context to read."""
    def rows(where: str, key: str):
        return [dict(r) for r in conn.execute(
            f"SELECT l.type, l.note, l.actor, l.created_at, "
            f"l.from_uuid, l.to_uuid, e.title AS other_title, "
            f"e.journal_date AS other_date, e.status AS other_status "
            f"FROM entry_links l JOIN entries e ON e.uuid = l.{key} "
            f"WHERE l.{where} = ? ORDER BY l.id", (entry_uuid,))]
    return {"outgoing": rows("from_uuid", "to_uuid"),
            "incoming": rows("to_uuid", "from_uuid")}

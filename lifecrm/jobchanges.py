"""LinkedIn job changes: a linked person's position or company moved between
two exports.

Dex tells you when a connection's title changes, from a live sync. Here the
same news comes from the exports you download: LinkedIn's Connections.csv
carries each connection's current position and company, the connector
stamps every row with the export's own date (connectors/linkedin.py), so a
newer export with a new job is an `update` of that row — and the audit log
keeps what it held before ("audit-logged with old and new values",
domain/observations.py). Nothing here asks LinkedIn anything.

Only people you linked count (an unlinked connection is a handle, never a
name). Reads only; each change cites the connection row and names the audit
row that recorded it.
"""

from __future__ import annotations

import json
import sqlite3

STREAM = "linkedin_connection"


def _loads(text) -> dict:
    if isinstance(text, dict):
        return text
    try:
        d = json.loads(text) if text else {}
    except (ValueError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _role(detail: dict) -> tuple[str | None, str | None]:
    return ((detail.get("position") or "").strip() or None,
            (detail.get("company") or "").strip() or None)


def _day(cfg, ts: str) -> str:
    """The journal day an audit stamp (UTC) falls on."""
    from datetime import datetime, timezone

    from .domain import days as days_mod
    if cfg is None:
        return ts[:10]
    try:
        stamp = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ts[:10]
    return days_mod.journal_date_for(stamp, cfg.timezone, cfg.day_rollover_hour)


def changes(conn: sqlite3.Connection, cfg=None, *, start: str | None = None,
            end: str | None = None, owners: frozenset = frozenset()) -> list[dict]:
    """Every recorded position or company change on a linked person's
    connection row, oldest first: who, from, to, the day the newer export
    was imported, the row."""
    try:
        audits = conn.execute(
            "SELECT id, ts, row_uuid, details FROM audit_log WHERE action = 'update' "
            "AND table_name = 'observations' AND details IS NOT NULL "
            "AND json_valid(details) AND json_extract(details, '$.stream') = ? "
            "ORDER BY id", (STREAM,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for a in audits:
        d = _loads(a["details"])
        ch = (d.get("changes") or {}).get("value_json")
        if not isinstance(ch, dict):
            continue
        old, new = _role(_loads(ch.get("old"))), _role(_loads(ch.get("new")))
        if old == new or not any(new):
            continue
        row = conn.execute(
            "SELECT o.id, o.uuid, o.stream, o.title, o.journal_date, o.started_at, "
            "o.direction, o.last_modified_at, o.status, i.uuid AS import_uuid "
            "FROM observations o JOIN imports i ON i.id = o.import_id WHERE o.uuid = ?",
            (a["row_uuid"],)).fetchone()
        if row is None or row["status"] != "active":
            continue
        seen_on = _day(cfg, a["ts"])
        if start and seen_on < start or end and seen_on > end:
            continue
        person = conn.execute(
            "SELECT CASE WHEN e.status = 'merged' THEN e.merged_into ELSE e.id END AS eid, "
            "CASE WHEN e.status = 'merged' THEN m.name ELSE e.name END AS name, "
            "CASE WHEN e.status = 'merged' THEN m.type ELSE e.type END AS etype "
            "FROM observation_participants p "
            "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            "JOIN entities e ON e.id = h.entity_id LEFT JOIN entities m ON m.id = e.merged_into "
            "WHERE p.observation_id = ? AND h.ignored = 0 ORDER BY p.handle_kind LIMIT 1",
            (row["id"],)).fetchone()
        if person is None or person["etype"] != "person" or person["eid"] in owners:
            continue
        out.append({"audit_id": a["id"], "entity_id": person["eid"], "name": person["name"],
                    "from": {"position": old[0], "company": old[1]},
                    "to": {"position": new[0], "company": new[1]},
                    "seen_on": seen_on,
                    "row": {"type": "observation", "tier": "observed", "uuid": row["uuid"],
                            "stream": row["stream"], "date": row["journal_date"],
                            "occurred_at": row["started_at"], "direction": row["direction"],
                            "title": row["title"], "import_uuid": row["import_uuid"]}})
    return out


def role_text(role: dict) -> str:
    return " at ".join(x for x in (role.get("position"), role.get("company")) if x) or "?"


def describe(c: dict) -> str:
    return (f"{c['name']}: {role_text(c['to'])} in the LinkedIn export imported "
            f"{c['seen_on']}, {role_text(c['from'])} in the one before")


def for_person(conn: sqlite3.Connection, cfg, entity_id: int) -> list[dict]:
    """A person page's work history from LinkedIn exports, newest first."""
    from .recall import _entity_family
    family = set(_entity_family(conn, entity_id)) | {entity_id}
    return [c for c in reversed(changes(conn, cfg)) if c["entity_id"] in family]

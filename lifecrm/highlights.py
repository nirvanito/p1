"""Highlights: a few of your book highlights each day, in a fixed rotation.

Readwise's Daily Review brings back what you highlighted so it is not
forgotten. Here the Kindle highlights the reading connector imported come
back a few a day: every highlight sorted by a hash of its uuid (a fixed,
shuffled order no import date or title can bunch up), and day N takes the
next PER_DAY of that order, so each one comes round once per cycle and the
same day always shows the same ones. No state is kept — nothing records
what was shown — and a new highlight simply joins the order.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date

from .recall import STREAM_GROUPS

STREAMS = STREAM_GROUPS["highlight"]
PER_DAY = 3
EPOCH = date(2000, 1, 3)               # a Monday; any fixed day works


def _order(uuid: str) -> str:
    return hashlib.sha256(uuid.encode("utf-8")).hexdigest()


def for_day(conn: sqlite3.Connection, day: date, per_day: int = PER_DAY) -> dict:
    """{'date', 'items': [...], 'total', 'cycle_day'}: day's highlights."""
    out = {"date": day.isoformat(), "items": [], "total": 0, "per_day": per_day}
    marks = ",".join("?" * len(STREAMS))
    try:
        rows = conn.execute(
            "SELECT o.uuid, o.stream, o.title, o.body, o.journal_date, o.started_at, "
            "o.direction, o.value_json, i.uuid AS import_uuid FROM observations o "
            "JOIN imports i ON i.id = o.import_id WHERE o.status = 'active' "
            f"AND o.stream IN ({marks}) AND o.body IS NOT NULL AND TRIM(o.body) <> '' "
            # what you underlined — a note you typed beside it is not a highlight
            "AND COALESCE(CASE WHEN json_valid(o.value_json) "
            "THEN json_extract(o.value_json, '$.kind') END, 'highlight') = 'highlight'",
            STREAMS).fetchall()
    except sqlite3.OperationalError:
        return out
    if not rows:
        return out
    ordered = sorted(rows, key=lambda r: _order(r["uuid"]))
    n = len(ordered)
    per_day = min(per_day, n)
    start = ((day - EPOCH).days * per_day) % n
    picked = [ordered[(start + i) % n] for i in range(per_day)]
    out["total"] = n
    out["cycle_day"] = ((day - EPOCH).days % max(1, -(-n // per_day))) + 1
    for r in picked:
        out["items"].append({"type": "observation", "tier": "observed", "uuid": r["uuid"],
                             "stream": r["stream"], "date": r["journal_date"],
                             "occurred_at": r["started_at"], "direction": r["direction"],
                             "title": r["title"], "import_uuid": r["import_uuid"],
                             "text": " ".join((r["body"] or "").split())})
    return out


def render_lines(data: dict) -> list[str]:
    if not data["items"]:
        return ["No highlights imported (a Kindle's My Clippings.txt: docs/reading.md)."]
    out = [f"From your highlights, {data['date']} ({len(data['items'])} of {data['total']}, "
           f"a fixed rotation; observed):"]
    for h in data["items"]:
        out.append(f"  - \"{h['text']}\" — {h['title'] or 'a book'} "
                   f"(highlighted {h['date']}) · {h['uuid']}")
    return out

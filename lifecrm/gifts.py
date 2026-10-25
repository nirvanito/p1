"""Gift ideas beside a birthday, and anniversaries as nudges (Monica's gift
tracker and important dates, from your own confirmed entries).

A gift idea is an active entry that mentions the person and either carries
`data.gift` (lifecrm/domain/gifts.py) or is an `idea` whose title says gift
or present. "Given" ideas are what you gave; a dropped one is left out. An
anniversary is an entry with `data.anniversary` that mentions its people.
Both are confirmed — you said them — and every line cites its entry.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

from .domain.gifts import parse_day
from .review import NOT_DERIVED

_GIFT_WORD = re.compile(r"\b(?:gift|gifts|present|presents)\b", re.IGNORECASE)
MAX_IDEAS = 5


def _data(text: str | None) -> dict:
    try:
        d = json.loads(text) if text else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _status(row) -> str | None:
    """'idea', 'given' or None (not a gift entry, or a dropped one)."""
    d = _data(row["data"])
    g = d.get("gift")
    if g is True:
        return "idea"
    if isinstance(g, dict):
        s = g.get("status", "idea")
        return None if s == "dropped" else s
    if row["kind"] == "idea" and _GIFT_WORD.search(row["title"] or ""):
        return "idea"
    return None


def ideas_for(conn: sqlite3.Connection, family: list[int], *,
              include_given: bool = False) -> list[dict]:
    """Gift entries mentioning this person (or anyone merged into them), open
    ideas first and newest first; `include_given` adds what you gave."""
    marks = ",".join("?" for _ in family)
    rows = conn.execute(
        "SELECT e.uuid, e.title, e.journal_date, e.data, e.closed_at, k.name AS kind "
        "FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        f"WHERE e.status = 'active' AND {NOT_DERIVED} AND e.id IN "
        f"(SELECT entry_id FROM mentions WHERE entity_id IN ({marks})) "
        "ORDER BY e.journal_date DESC, e.id DESC", family).fetchall()
    out = []
    for r in rows:
        s = _status(r)
        if s is None or (s == "given" and not include_given):
            continue
        if s == "idea" and r["closed_at"]:
            continue                          # an idea closed as done is a gift given
        out.append({"type": "entry", "tier": "confirmed", "uuid": r["uuid"],
                    "date": r["journal_date"], "kind": r["kind"], "title": r["title"],
                    "gift_status": s})
    ideas = [x for x in out if x["gift_status"] == "idea"]
    ideas.sort(key=lambda x: x["date"], reverse=True)
    given = [x for x in out if x["gift_status"] == "given"]
    given.sort(key=lambda x: x["date"], reverse=True)
    return (ideas[:MAX_IDEAS] + given[:MAX_IDEAS]) if include_given else ideas[:MAX_IDEAS]


def recorded(conn: sqlite3.Connection, owners: frozenset = frozenset()) -> list[dict]:
    """Every entry carrying a usable data.anniversary: the entry, (year or
    None, month, day), what it is, and the people it names (never the owner)."""
    out = []
    for r in conn.execute(
            "SELECT e.id, e.uuid, e.title, e.journal_date, e.data, k.name AS kind "
            "FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
            f"WHERE e.status = 'active' AND {NOT_DERIVED} AND e.data IS NOT NULL "
            "AND json_valid(e.data) AND json_extract(e.data, '$.anniversary') IS NOT NULL "
            "ORDER BY e.journal_date, e.id"):
        ann = _data(r["data"]).get("anniversary") or {}
        parsed = parse_day(ann.get("date"))
        if parsed is None:
            continue
        people = [p["name"] for p in conn.execute(
            "SELECT DISTINCT CASE WHEN en.status = 'merged' THEN m.name ELSE en.name END AS name, "
            "CASE WHEN en.status = 'merged' THEN en.merged_into ELSE en.id END AS eid "
            "FROM mentions mn JOIN entities en ON en.id = mn.entity_id "
            "LEFT JOIN entities m ON m.id = en.merged_into "
            "WHERE mn.entry_id = ? AND (en.type = 'person' OR m.type = 'person') "
            "ORDER BY name", (r["id"],)) if p["eid"] not in owners]
        out.append({"uuid": r["uuid"], "title": r["title"], "date": r["journal_date"],
                    "kind": r["kind"], "what": (ann.get("what") or "").strip() or None,
                    "parsed": parsed, "people": people, "on": ann.get("date")})
    return out


def label(a: dict, occurs: date) -> str:
    """'Wedding anniversary with Linda Rivera, 38 years' for one occurrence."""
    year = a["parsed"][0]
    what = f"{a['what'].capitalize()} anniversary" if a["what"] else "Anniversary"
    who = f" with {', '.join(a['people'])}" if a["people"] else ""
    years = occurs.year - year if year else None
    return what + who + (f", {years} year{'' if years == 1 else 's'}" if years else "")


def anniversaries(conn: sqlite3.Connection, today: date, within_days: int,
                  owners: frozenset = frozenset()) -> list[dict]:
    """Every anniversary whose next occurrence is within `within_days`: the
    entry, the date, who it names (never the owner alone), years when the
    year is known."""
    from .nudges import next_occurrence
    out = []
    for a in recorded(conn, owners):
        year, month, day = a["parsed"]
        occ = next_occurrence(month, day, today)
        delta = (occ - today).days
        if delta > within_days:
            continue
        out.append({**{k: v for k, v in a.items() if k != "parsed"},
                    "occurs": occ.isoformat(), "in_days": delta,
                    "years": occ.year - year if year else None})
    out.sort(key=lambda a: (a["occurs"], a["title"]))
    return out

"""On this day: what earlier years recorded on today's month and day.

Day One's "On This Day" and the Obsidian journal-review plugins bring back
what you wrote a year ago today. Here the same date in every earlier year
brings back both tiers: your journal entries (confirmed) and a few of the
records your imports hold for that date (observed) — photos, places,
meetings, workouts, highlights, notes, and how many texts and with whom.

Reads only, with no model and no clock: the same database and the same day
give the same result. Every item names its row (an entry's uuid, an
observation's uuid and import, or a count with a sample of uuids), so the
nudges pack can cite it and a brief can be checked against it. February 29
comes back only in leap years — and on February 28 of a year without one.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from .config import Config
from .recall import STREAM_GROUPS, TEXT_STREAMS
from .review import NOT_DERIVED

MAX_ENTRIES = 5
# Per stream: how many single records to list (a count goes beside them).
SINGLE_STREAMS = (
    ("place_visit", 3),
    ("calendar_event", 3),
    ("workout", 2),
    ("highlight", 2),
    ("note", 2),
    ("ride", 1),
)
PHOTO_STREAMS = STREAM_GROUPS["photo"]
MAX_NAMES = 3


def _detail(value_json: str | None) -> dict:
    try:
        d = json.loads(value_json) if value_json else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _excerpt(text: str | None, n: int = 120) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[:n - 1].rstrip() + "…"


def target_dates(day: date, first_year: int) -> list[date]:
    """The same month and day in each earlier year back to `first_year`,
    newest first. February 29 exists only in leap years; on February 28 of
    a year without one, the leap years' February 29 come along too."""
    out = []
    leap_eve = (day.month, day.day) == (2, 28) and not _leap(day.year)
    for year in range(day.year - 1, first_year - 1, -1):
        try:
            out.append(date(year, day.month, day.day))
        except ValueError:
            continue                              # Feb 29 in a common year
        if leap_eve and _leap(year):
            out.append(date(year, 2, 29))
    return out


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _first_year(conn: sqlite3.Connection, observed: bool) -> int | None:
    years = []
    row = conn.execute("SELECT MIN(journal_date) FROM entries WHERE status='active'").fetchone()
    if row and row[0]:
        years.append(int(row[0][:4]))
    if observed:
        row = conn.execute("SELECT MIN(journal_date) FROM observations "
                           "WHERE status='active'").fetchone()
        if row and row[0]:
            years.append(int(row[0][:4]))
    return min(years) if years else None


def _observed(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='observations'").fetchone() is not None


_OBS = ("SELECT o.id, o.uuid, o.stream, o.title, o.body, o.journal_date, o.started_at, "
        "o.utc_offset_sec, o.direction, o.value, o.unit, o.value_json, o.origin_app, "
        "i.uuid AS import_uuid FROM observations o JOIN imports i ON i.id = o.import_id ")


def _names(conn: sqlite3.Connection, cfg: Config, obs_ids: list[int],
           owners: frozenset, owner_handles: frozenset) -> list[str]:
    """The linked people on these rows, most rows first — never the owner,
    never an unlinked handle (a name is only ever the owner's link)."""
    from .web import api
    counts: dict[str, int] = {}
    for parts in api._participants(conn, obs_ids).values():
        seen = set()
        for p in parts:
            if not p["entity"] or p["entity_id"] in owners \
                    or (p["kind"], (p["value"] or "").lower()) in owner_handles:
                continue
            if p["entity"] in seen:
                continue
            seen.add(p["entity"])
            counts[p["entity"]] = counts.get(p["entity"], 0) + 1
    return [n for n, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))]


def _day(conn: sqlite3.Connection, cfg: Config, d: str, observed: bool,
         owners: frozenset, owner_handles: frozenset, places: bool) -> dict | None:
    out: dict = {"date": d, "summary": None, "entries": [], "records": []}
    row = conn.execute("SELECT summary FROM days WHERE journal_date=?", (d,)).fetchone()
    if row and (row["summary"] or "").strip():
        out["summary"] = row["summary"].strip()
    for e in conn.execute(
            "SELECT e.uuid, e.title, e.journal_date, e.time_of_day, k.name AS kind "
            "FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
            f"WHERE e.status = 'active' AND e.journal_date = ? AND {NOT_DERIVED} "
            "ORDER BY CASE e.time_of_day WHEN 'morning' THEN 1 WHEN 'afternoon' THEN 2 "
            "WHEN 'evening' THEN 3 WHEN 'night' THEN 4 ELSE 5 END, e.id LIMIT ?",
            (d, MAX_ENTRIES)):
        out["entries"].append({"type": "entry", "tier": "confirmed", "uuid": e["uuid"],
                               "date": e["journal_date"], "kind": e["kind"],
                               "title": e["title"]})
    if observed:
        marks = ",".join("?" * len(PHOTO_STREAMS))
        photos = conn.execute(
            _OBS + f"WHERE o.status = 'active' AND o.journal_date = ? "
            f"AND o.stream IN ({marks}) ORDER BY o.started_at, o.id",
            (d, *PHOTO_STREAMS)).fetchall()
        if photos:
            out["records"].append({
                "type": "observation_count", "tier": "observed", "stream": "photo",
                "what": f"photos taken on {d}", "count": len(photos),
                "first_date": d, "last_date": d,
                "first_uuid": photos[0]["uuid"], "last_uuid": photos[-1]["uuid"],
                "uuids": [p["uuid"] for p in photos][:20],
                "imports": sorted({p["import_uuid"] for p in photos}),
                "text": f"{len(photos)} photo{'' if len(photos) == 1 else 's'}"})
        for stream, limit in SINGLE_STREAMS:
            rows = conn.execute(
                _OBS + "WHERE o.status = 'active' AND o.journal_date = ? AND o.stream = ? "
                "ORDER BY o.started_at, o.id", (d, stream)).fetchall()
            if stream == "calendar_event":
                rows = [r for r in rows
                        if (_detail(r["value_json"]).get("status") or "").lower()
                        != "cancelled"]
            for r in rows[:limit]:
                out["records"].append(_record(conn, cfg, r, owners, owner_handles, places))
        marks = ",".join("?" * len(TEXT_STREAMS))
        texts = conn.execute(
            _OBS + f"WHERE o.status = 'active' AND o.journal_date = ? "
            f"AND o.stream IN ({marks}) ORDER BY o.started_at, o.id",
            (d, *TEXT_STREAMS)).fetchall()
        if texts:
            names = _names(conn, cfg, [t["id"] for t in texts], owners, owner_handles)
            shown = names[:MAX_NAMES]
            more = len(names) - len(shown)
            who = (" with " + ", ".join(shown) + (f" and {more} more" if more else "")
                   if shown else "")
            out["records"].append({
                "type": "observation_count", "tier": "observed", "stream": "text",
                "what": f"texts on {d}", "count": len(texts), "first_date": d,
                "last_date": d, "first_uuid": texts[0]["uuid"],
                "last_uuid": texts[-1]["uuid"], "uuids": [t["uuid"] for t in texts][:20],
                "imports": sorted({t["import_uuid"] for t in texts}), "people": shown,
                "text": f"{len(texts)} text{'' if len(texts) == 1 else 's'}{who}"})
    if not (out["summary"] or out["entries"] or out["records"]):
        return None
    return out


def _record(conn: sqlite3.Connection, cfg: Config, r, owners: frozenset,
            owner_handles: frozenset, places: bool) -> dict:
    stream, title = r["stream"], (r["title"] or "").strip()
    d = _detail(r["value_json"])
    if stream == "place_visit":
        text = f"at {title or 'a place'}"
        if places:
            from . import places as places_mod
            near = places_mod.label_for(cfg, d.get("lat"), d.get("lon"))
            if near:
                text += f" (near {near})"
    elif stream == "calendar_event":
        names = _names(conn, cfg, [r["id"]], owners, owner_handles)
        text = (title or "an event") + (f" with {', '.join(names[:MAX_NAMES])}" if names else "")
    elif stream == "workout":
        text = f"workout: {title or 'a session'}"
    elif stream == "highlight":
        text = f"highlighted in {title or 'a book'}: \"{_excerpt(r['body'], 100) or ''}\""
    elif stream == "note":
        text = f"note: {title or _excerpt(r['body'], 60) or 'untitled'}"
    elif stream == "ride":
        text = f"ride: {title or 'a ride'}"
    else:
        text = title or stream
    return {"type": "observation", "tier": "observed", "uuid": r["uuid"],
            "stream": stream, "date": r["journal_date"], "occurred_at": r["started_at"],
            "direction": r["direction"], "title": r["title"],
            "import_uuid": r["import_uuid"], "text": text}


def on_this_day(conn: sqlite3.Connection, cfg: Config, day: date) -> dict:
    """{'date', 'years': [{'year', 'date', 'years_ago', 'summary', 'entries',
    'records'}]}, newest year first, only years that recorded something."""
    observed = _observed(conn)
    first = _first_year(conn, observed)
    out = {"date": day.isoformat(), "years": []}
    if first is None or first >= day.year:
        return out
    owners, owner_handles = frozenset(), frozenset()
    if observed:
        try:
            from . import owner as owner_mod
            ident = owner_mod.identity(conn, cfg)
            owners, owner_handles = ident.entities, ident.handles
        except Exception:                     # a bad [owner] table: nobody is left out
            pass
    from . import features
    places = features.enabled(cfg, "places")
    for t in target_dates(day, first):
        found = _day(conn, cfg, t.isoformat(), observed, owners, owner_handles, places)
        if found:
            years_ago = day.year - t.year
            out["years"].append({"year": t.year, "years_ago": years_ago, **found})
    return out


def _same_day(a: date, b: date) -> bool:
    """Same month and day — and a February 29 matches the 28th of a year
    without one (the day it comes back on), both ways."""
    if (a.month, a.day) == (b.month, b.day):
        return True
    for x, y in ((a, b), (b, a)):
        if (x.month, x.day) == (2, 29) and (y.month, y.day) == (2, 28) and not _leap(y.year):
            return True
    return False


def same_day_other_years(day: str, days: list[str]) -> list[str]:
    """The vault's link row: the days in `days` (ISO dates) that fall on this
    day's month and day in another year, newest first. Pure: no database."""
    d = date.fromisoformat(day)
    return sorted((x for x in days if x[:4] != day[:4]
                   and _same_day(d, date.fromisoformat(x))), reverse=True)

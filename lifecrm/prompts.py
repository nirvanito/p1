"""Journal prompts: moments in a day's imports worth a word in the journal.

Apple's Journal suggests moments to write about — a new place, photos, a
workout, the people you were with. Here the moments come from your own
imports for one journal day: a first recorded visit to a place, a burst of
photos, a long call, a meeting with someone, a workout, a day whose spending
stood out against your own recent days.

A prompt is a question, never an entry: nothing here is written anywhere,
and only what you then say (in a voice note, through /log) becomes part of
the journal. A moment the day's journal already covers — an entry naming
the person or the place, an exercise entry for a workout — is left out.
Reads only, fixed wording per kind, every prompt cited to its rows.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import date, timedelta

from .config import Config
from .recall import CALL_STREAMS, STREAM_GROUPS
from .review import NOT_DERIVED

PHOTO_BURST = 8
LONG_CALL_SEC = 20 * 60
SPEND_BASELINE_DAYS = 60
SPEND_FACTOR = 2.0
SPEND_MIN = 50.0
MAX_PROMPTS = 6
KIND_ORDER = ("first_visit", "meeting", "long_call", "photos", "workout", "spend")

_OBS = ("SELECT o.id, o.uuid, o.stream, o.title, o.body, o.journal_date, o.started_at, "
        "o.ended_at, o.direction, o.value, o.unit, o.value_json, o.origin_app, "
        "i.uuid AS import_uuid FROM observations o JOIN imports i ON i.id = o.import_id ")


def _detail(value_json: str | None) -> dict:
    try:
        d = json.loads(value_json) if value_json else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _item(r, **extra) -> dict:
    return {"type": "observation", "tier": "observed", "uuid": r["uuid"],
            "stream": r["stream"], "date": r["journal_date"],
            "occurred_at": r["started_at"], "direction": r["direction"],
            "title": r["title"], "import_uuid": r["import_uuid"], **extra}


def _count(rows, what: str, **extra) -> dict:
    return {"type": "observation_count", "tier": "observed", "what": what,
            "count": len(rows), "first_date": rows[0]["journal_date"],
            "last_date": rows[-1]["journal_date"], "first_uuid": rows[0]["uuid"],
            "last_uuid": rows[-1]["uuid"], "uuids": [r["uuid"] for r in rows][:20],
            "imports": sorted({r["import_uuid"] for r in rows}), **extra}


def _journal_people_places(conn: sqlite3.Connection, day: str) -> tuple[set[int], bool]:
    """Entities the day's journal names (merges followed) and whether it has
    an exercise entry."""
    ids = set()
    for r in conn.execute(
            "SELECT CASE WHEN en.status = 'merged' THEN en.merged_into ELSE en.id END AS eid "
            "FROM mentions m JOIN entries e ON e.id = m.entry_id "
            "JOIN entities en ON en.id = m.entity_id "
            f"WHERE e.status = 'active' AND e.journal_date = ? AND {NOT_DERIVED}", (day,)):
        ids.add(r["eid"])
    exercise = conn.execute(
        "SELECT 1 FROM entries e JOIN entry_categories ec ON ec.entry_id = e.id "
        "JOIN categories c ON c.id = ec.category_id WHERE e.status = 'active' "
        "AND e.journal_date = ? AND c.name = 'exercise' LIMIT 1", (day,)).fetchone()
    return ids, exercise is not None


def _linked(conn: sqlite3.Connection, obs_id: int, owners: frozenset,
            owner_handles: frozenset) -> list[tuple[int, str, str]]:
    """(entity id, name, type) for each linked entity on a row, never the owner."""
    out, seen = [], set()
    for r in conn.execute(
            "SELECT p.handle_kind, p.handle_value, "
            "CASE WHEN e.status = 'merged' THEN e.merged_into ELSE e.id END AS eid, "
            "CASE WHEN e.status = 'merged' THEN m.name ELSE e.name END AS name, "
            "CASE WHEN e.status = 'merged' THEN m.type ELSE e.type END AS etype "
            "FROM observation_participants p "
            "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            "JOIN entities e ON e.id = h.entity_id LEFT JOIN entities m ON m.id = e.merged_into "
            "WHERE p.observation_id = ? AND h.ignored = 0 "
            "ORDER BY p.role, p.handle_kind, p.handle_value", (obs_id,)):
        if r["eid"] in owners or (r["handle_kind"], (r["handle_value"] or "").lower()) \
                in owner_handles or r["eid"] in seen:
            continue
        seen.add(r["eid"])
        out.append((r["eid"], r["name"], r["etype"]))
    return out


def _place_key(r) -> str | None:
    d = _detail(r["value_json"])
    if d.get("place_id"):
        return f"id:{d['place_id']}"
    lat, lon = d.get("lat"), d.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return f"geo:{round(lat, 3)},{round(lon, 3)}"
    return None


def moments(conn: sqlite3.Connection, cfg: Config, day: str) -> dict:
    """{'date', 'prompts': [{kind, text, record}], 'covered': n} for one
    journal day. Empty on a database without the observed tier."""
    out = {"date": day, "prompts": [], "covered": 0}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='observations'").fetchone() is None:
        return out
    owners, owner_handles = frozenset(), frozenset()
    try:
        from . import owner as owner_mod
        ident = owner_mod.identity(conn, cfg)
        owners, owner_handles = ident.entities, ident.handles
    except Exception:                          # a bad [owner] table: nobody left out
        pass
    from . import features
    places = features.enabled(cfg, "places")
    named, exercised = _journal_people_places(conn, day)
    found: list[dict] = []

    def keep(kind: str, text: str, record: dict, about: set[int] = frozenset()) -> None:
        if about and about & named:
            out["covered"] += 1
            return
        found.append({"kind": kind, "text": text, "record": record})

    # a first recorded visit to a place
    visits = conn.execute(_OBS + "WHERE o.status = 'active' AND o.stream = 'place_visit' "
                          "AND o.journal_date = ? ORDER BY o.started_at, o.id", (day,)).fetchall()
    seen_keys = set()
    for r in visits:
        key = _place_key(r)
        if key is None or key in seen_keys:
            continue
        seen_keys.add(key)
        d = _detail(r["value_json"])
        if d.get("semantic_type") in ("HOME", "INFERRED_HOME", "WORK", "INFERRED_WORK"):
            continue
        if key.startswith("id:"):
            earlier = conn.execute(
                "SELECT 1 FROM observations WHERE status = 'active' AND stream = 'place_visit' "
                "AND journal_date < ? AND json_valid(value_json) "
                "AND json_extract(value_json, '$.place_id') = ? LIMIT 1",
                (day, key[3:])).fetchone()
        else:
            lat, lon = (float(x) for x in key[4:].split(","))
            earlier = conn.execute(
                "SELECT 1 FROM observations WHERE status = 'active' AND stream = 'place_visit' "
                "AND journal_date < ? AND json_valid(value_json) "
                "AND ROUND(json_extract(value_json, '$.lat'), 3) = ? "
                "AND ROUND(json_extract(value_json, '$.lon'), 3) = ? LIMIT 1",
                (day, lat, lon)).fetchone()
        if earlier:
            continue
        title = (r["title"] or "a place").strip()
        if places:
            from . import places as places_mod
            near = places_mod.label_for(cfg, d.get("lat"), d.get("lon"))
            if near:
                title += f" (near {near})"
        about = {eid for eid, _n, _t in _linked(conn, r["id"], owners, owner_handles)}
        keep("first_visit", f"a first recorded visit to {title}", _item(r), about)
    # meetings with someone
    cal = conn.execute(_OBS + "WHERE o.status = 'active' AND o.stream = 'calendar_event' "
                       "AND o.journal_date = ? ORDER BY o.started_at, o.id", (day,)).fetchall()
    for r in cal:
        d = _detail(r["value_json"])
        if d.get("all_day") or (d.get("status") or "").lower() == "cancelled":
            continue
        people = [(eid, n) for eid, n, t in _linked(conn, r["id"], owners, owner_handles)
                  if t == "person"]
        if not people:
            continue
        names = ", ".join(n for _e, n in people[:3])
        keep("meeting", f"{(r['title'] or 'a meeting').strip()} with {names}", _item(r),
             {e for e, _n in people})
    # a long call
    marks = ",".join("?" * len(CALL_STREAMS))
    for r in conn.execute(_OBS + f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
                          "AND o.journal_date = ? AND o.value >= ? ORDER BY o.value DESC, o.id",
                          (*CALL_STREAMS, day, LONG_CALL_SEC)).fetchall()[:2]:
        people = [(eid, n) for eid, n, t in _linked(conn, r["id"], owners, owner_handles)
                  if t == "person"]
        if not people:
            continue
        minutes = int(round(r["value"] / 60))
        keep("long_call", f"a {minutes}-minute call with {people[0][1]}",
             _item(r, minutes=minutes), {people[0][0]})
    # a burst of photos
    pmarks = ",".join("?" * len(STREAM_GROUPS["photo"]))
    photos = conn.execute(_OBS + f"WHERE o.status = 'active' AND o.stream IN ({pmarks}) "
                          "AND o.journal_date = ? ORDER BY o.started_at, o.id",
                          (*STREAM_GROUPS["photo"], day)).fetchall()
    if len(photos) >= PHOTO_BURST:
        where = ""
        if places:
            from . import places as places_mod
            towns: dict[str, int] = {}
            for p in photos:
                d = _detail(p["value_json"])
                lab = places_mod.label_for(cfg, d.get("lat"), d.get("lon"))
                if lab:
                    towns[lab] = towns.get(lab, 0) + 1
            if towns:
                where = " near " + min(towns.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        keep("photos", f"{len(photos)} photos taken{where}",
             _count(photos, f"photos on {day}"))
    # a workout
    wmarks = ",".join("?" * len(STREAM_GROUPS["workout"]))
    for r in conn.execute(_OBS + f"WHERE o.status = 'active' AND o.stream IN ({wmarks}) "
                          "AND o.journal_date = ? ORDER BY o.started_at, o.id LIMIT 1",
                          (*STREAM_GROUPS["workout"], day)).fetchall():
        if exercised:
            out["covered"] += 1
            continue
        keep("workout", f"a workout: {(r['title'] or 'a session').strip()}", _item(r))
    # a day whose spending stood out
    spend = _spend(conn, day)
    if spend:
        keep("spend", spend["text"], spend["record"])
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    found.sort(key=lambda p: rank[p["kind"]])
    out["prompts"] = found[:MAX_PROMPTS]
    return out


def _spend(conn: sqlite3.Connection, day: str) -> dict | None:
    """The day's spending when it is at least SPEND_FACTOR times the median
    of your spending days in the SPEND_BASELINE_DAYS before (and SPEND_MIN)."""
    from .review import _money_flow
    start = (date.fromisoformat(day) - timedelta(days=SPEND_BASELINE_DAYS)).isoformat()
    rows = conn.execute(_OBS + "WHERE o.status = 'active' AND o.stream = 'txn' "
                        "AND o.journal_date BETWEEN ? AND ? ORDER BY o.journal_date, o.id",
                        (start, day)).fetchall()
    by_day: dict[tuple[str, str], list] = {}
    for r in rows:
        if _detail(r["value_json"]).get("transfer") is True:
            continue
        flow, amount = _money_flow(r)
        if flow != "spent":
            continue
        by_day.setdefault((r["journal_date"], r["unit"] or ""), []).append((r, amount))
    today_keys = [k for k in by_day if k[0] == day]
    for key in sorted(today_keys, key=lambda k: k[1]):
        cur = key[1]
        total = round(sum(a for _r, a in by_day[key]), 2)
        prior = [sum(a for _r, a in v) for k, v in by_day.items() if k[0] < day and k[1] == cur]
        if len(prior) < 5:
            continue
        median = statistics.median(prior)
        if total < SPEND_MIN or total < SPEND_FACTOR * median:
            continue
        txns = [r for r, _a in by_day[key]]
        unit = f" {cur}" if cur else ""
        return {"text": f"spending of {total:,.2f}{unit} across {len(txns)} transaction(s), "
                        f"against a median of {median:,.2f}{unit} on your spending days in "
                        f"the {SPEND_BASELINE_DAYS} days before",
                "record": _count(txns, f"transactions on {day}", total=total,
                                 median=round(median, 2), currency=cur or None)}
    return None


def render_lines(data: dict) -> list[str]:
    out = [f"Worth a word in the journal for {data['date']} (from your imports, observed; "
           f"a prompt, never an entry):"]
    if not data["prompts"]:
        out.append("  nothing stands out" + (f" ({data['covered']} moment(s) the journal "
                                              f"already covers)" if data["covered"] else ""))
    for p in data["prompts"]:
        rec = p["record"]
        ref = rec.get("uuid") or rec.get("first_uuid")
        out.append(f"  - {p['text']} · {rec['type'].replace('_', ' ')} {ref}")
    return out

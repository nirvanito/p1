"""Meeting prep: what the records hold about the people on an upcoming event.

Dex and Clay put a brief in front of you before a meeting — shared history,
the last conversation, your notes. Here it is built from your own records,
for each linked person on today's and tomorrow's calendar events: the last
message each way, the last few messages between you, open tasks and plans
that name them, the newest journal entry they took part in, relationships
you confirmed, their LinkedIn position, a birthday coming up, gift ideas you
noted (when that feature is checked).

Reads only, cites every line to its row, and states what the records hold —
never what the person is like or what to say. "Today" is the caller's, so
the Obsidian Home page built for the as-of date gets the same brief every
time.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from .config import Config
from .recall import EMAIL_STREAMS, TEXT_STREAMS, _entity_family
from .review import NOT_DERIVED

RECENT_MESSAGES = 3
OPEN_LOOPS = 4
BIRTHDAY_DAYS = 30
MESSAGE_STREAMS = TEXT_STREAMS + EMAIL_STREAMS

_OBS = ("SELECT o.id, o.uuid, o.stream, o.title, o.body, o.journal_date, o.started_at, "
        "o.direction, o.value_json, o.origin_app, i.uuid AS import_uuid "
        "FROM observations o JOIN imports i ON i.id = o.import_id ")


def _marks(items) -> str:
    return ",".join("?" for _ in items)


def _detail(value_json: str | None) -> dict:
    try:
        d = json.loads(value_json) if value_json else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _excerpt(text: str | None, n: int = 90) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= n else flat[:n - 1].rstrip() + "…"


def obs_item(r, **extra) -> dict:
    return {"type": "observation", "tier": "observed", "uuid": r["uuid"],
            "stream": r["stream"], "date": r["journal_date"],
            "occurred_at": r["started_at"], "direction": r["direction"],
            "title": r["title"], "import_uuid": r["import_uuid"], **extra}


def entry_item(r) -> dict:
    return {"type": "entry", "tier": "confirmed", "uuid": r["uuid"],
            "date": r["journal_date"], "kind": r["kind"], "title": r["title"]}


def _theirs(family: list[int], roles: tuple[str, ...]) -> tuple[str, list]:
    """SQL: one of this person's linked handles is on row o in one of `roles`."""
    return (f"EXISTS (SELECT 1 FROM observation_participants p "
            f"JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            f"WHERE p.observation_id = o.id AND h.entity_id IN ({_marks(family)}) "
            f"AND h.ignored = 0 AND p.role IN ({_marks(roles)}))", [*family, *roles])


def _one_to_one(conn: sqlite3.Connection, obs_id: int, owners: frozenset,
                owner_handles: frozenset) -> bool:
    """A record between you and one other party (a group thread is not a
    conversation with each of its members)."""
    others = set()
    for p in conn.execute(
            "SELECT p.handle_kind, p.handle_value, h.entity_id FROM observation_participants p "
            "LEFT JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            "WHERE p.observation_id = ?", (obs_id,)):
        if (p["handle_kind"], (p["handle_value"] or "").lower()) in owner_handles \
                or p["entity_id"] in owners:
            continue
        others.add((p["handle_kind"], (p["handle_value"] or "").lower()))
    return len(others) <= 1


def person_prep(conn: sqlite3.Connection, cfg: Config, entity_id: int, today: date,
                owners: frozenset = frozenset(),
                owner_handles: frozenset = frozenset()) -> dict:
    """Every line the brief can say about one person as of `today`, each a
    plain record dict (`type`, `uuid`, ...) for the caller to cite."""
    name_row = conn.execute("SELECT name FROM entities WHERE id = ?", (entity_id,)).fetchone()
    family = _entity_family(conn, entity_id)
    out = {"entity_id": entity_id, "name": name_row["name"] if name_row else f"entity {entity_id}",
           "last_in": None, "last_out": None, "recent": [], "open_loops": [],
           "last_entry": None, "relationships": [], "linkedin": None, "birthday": None,
           "gift_ideas": []}
    t = today.isoformat()
    observed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='observations'").fetchone() is not None
    if observed:
        sent, sent_args = _theirs(family, ("sender",))
        got, got_args = _theirs(family, ("recipient", "participant"))
        base = (f"WHERE o.status = 'active' AND o.stream IN ({_marks(MESSAGE_STREAMS)}) "
                f"AND o.journal_date <= ? ")
        newest = "ORDER BY o.journal_date DESC, o.started_at DESC, o.id DESC"
        rows_in = conn.execute(
            _OBS + base + f"AND o.direction = 'in' AND {sent} {newest} LIMIT 20",
            (*MESSAGE_STREAMS, t, *sent_args)).fetchall()
        rows_out = conn.execute(
            _OBS + base + f"AND o.direction = 'out' AND {got} {newest} LIMIT 20",
            (*MESSAGE_STREAMS, t, *got_args)).fetchall()
        rows_in = [r for r in rows_in if _one_to_one(conn, r["id"], owners, owner_handles)]
        rows_out = [r for r in rows_out if _one_to_one(conn, r["id"], owners, owner_handles)]
        if rows_in:
            r = rows_in[0]
            out["last_in"] = obs_item(r, excerpt=_excerpt(r["body"] or r["title"]))
        if rows_out:
            r = rows_out[0]
            out["last_out"] = obs_item(r, excerpt=_excerpt(r["body"] or r["title"]))
        both = sorted(rows_in + rows_out, key=lambda r: (r["started_at"] or "", r["id"]),
                      reverse=True)[:RECENT_MESSAGES]
        out["recent"] = [obs_item(r, excerpt=_excerpt(r["body"] or r["title"]))
                         for r in both]
        li = conn.execute(
            _OBS + "WHERE o.status = 'active' AND o.stream = 'linkedin_connection' AND "
            + _theirs(family, ("participant", "sender", "recipient", "subject"))[0]
            + " ORDER BY COALESCE(o.last_modified_at, o.started_at) DESC, o.id DESC LIMIT 1",
            _theirs(family, ("participant", "sender", "recipient", "subject"))[1]).fetchone()
        if li is not None:
            d = _detail(li["value_json"])
            out["linkedin"] = obs_item(li, position=d.get("position") or None,
                                       company=d.get("company") or None)
        out["birthday"] = _birthday(conn, family, today)
    for r in conn.execute(
            "SELECT e.uuid, e.title, e.journal_date, e.due_at, k.name AS kind FROM entries e "
            "JOIN entry_kinds k ON k.id = e.kind_id "
            "WHERE e.status = 'active' AND e.closed_at IS NULL AND k.name IN ('task', 'plan') "
            f"AND e.id IN (SELECT entry_id FROM mentions WHERE entity_id IN ({_marks(family)})) "
            f"AND {NOT_DERIVED} ORDER BY (e.due_at IS NULL), e.due_at, e.journal_date, e.id "
            f"LIMIT ?", (*family, OPEN_LOOPS)):
        item = entry_item(r)
        if r["due_at"]:
            item["due_at"] = r["due_at"]
        out["open_loops"].append(item)
    r = conn.execute(
        "SELECT e.uuid, e.title, e.journal_date, k.name AS kind FROM entries e "
        "JOIN entry_kinds k ON k.id = e.kind_id WHERE e.status = 'active' "
        f"AND e.journal_date <= ? AND {NOT_DERIVED} AND e.id IN (SELECT entry_id FROM mentions "
        f"WHERE entity_id IN ({_marks(family)}) AND role = 'participant') "
        "ORDER BY e.journal_date DESC, e.id DESC LIMIT 1", (t, *family)).fetchone()
    if r is not None:
        out["last_entry"] = entry_item(r)
    from .recall import _relationships
    for rel in _relationships(conn, entity_id):
        e = rel["entry"]
        out["relationships"].append({
            "text": rel["text"],
            "entry": {"type": "entry", "tier": "confirmed", "uuid": e["uuid"],
                      "date": e["date"], "kind": e["kind"], "title": e["title"]}})
    from . import features
    if features.enabled(cfg, "gift_ideas"):
        from . import gifts
        out["gift_ideas"] = gifts.ideas_for(conn, family)
    return out


def _birthday(conn: sqlite3.Connection, family: list[int], today: date) -> dict | None:
    from .nudges import next_occurrence, parse_birthday
    rows = conn.execute(
        _OBS + "WHERE o.status = 'active' AND o.stream = 'contact_card' AND "
        + _theirs(family, ("participant",))[0] + " ORDER BY o.id",
        _theirs(family, ("participant",))[1]).fetchall()
    best = None
    for r in rows:
        parsed = parse_birthday(_detail(r["value_json"]).get("birthday"))
        if parsed is None:
            continue
        year, month, day = parsed
        occ = next_occurrence(month, day, today)
        if (occ - today).days > BIRTHDAY_DAYS:
            continue
        if best is None or (best[0] is None and year is not None):
            best = (year, occ, r)
    if best is None:
        return None
    year, occ, r = best
    return {"date": occ.isoformat(), "in_days": (occ - today).days,
            "turning": occ.year - year if year else None, "card": obs_item(r)}


def lines(p: dict, cite) -> list[dict]:
    """The brief for one person as [{text, ids}] — `cite(item)` turns a
    record dict into its pack id. Fixed wording: what the records hold."""
    out = []

    def add(text: str, *items) -> None:
        out.append({"text": text, "ids": [cite(i) for i in items if i]})

    if p["last_in"]:
        x = p["last_in"]
        add(f"last message from them {x['date']}"
            + (f': "{x["excerpt"]}"' if x.get("excerpt") else ""), x)
    if p["last_out"]:
        x = p["last_out"]
        add(f"last message from you {x['date']}"
            + (f': "{x["excerpt"]}"' if x.get("excerpt") else ""), x)
    if not (p["last_in"] or p["last_out"]) and p["last_entry"]:
        x = p["last_entry"]
        add(f"last in the journal together {x['date']}: {x['title']}", x)
    for x in p["open_loops"]:
        due = f", due {x['due_at'][:10]}" if x.get("due_at") else ""
        add(f"open {x['kind']}: {x['title']}{due}", x)
    for rel in p["relationships"][:3]:
        add(rel["text"], rel["entry"])
    if p["linkedin"] and (p["linkedin"].get("position") or p["linkedin"].get("company")):
        x = p["linkedin"]
        role = " at ".join(v for v in (x.get("position"), x.get("company")) if v)
        add(f"LinkedIn: {role}", x)
    if p["birthday"]:
        b = p["birthday"]
        when = "today" if b["in_days"] == 0 else f"in {b['in_days']} day(s), {b['date']}"
        add(f"birthday {when}" + (f", turning {b['turning']}" if b.get("turning") else ""),
            b["card"])
    for g in p["gift_ideas"][:3]:
        add(f"gift idea you noted {g['date']}: {g['title']}", g)
    return out


def agenda_people(conn: sqlite3.Connection, obs_ids: list[int], owners: frozenset,
                  owner_handles: frozenset) -> dict[int, list[int]]:
    """Each event's linked people (surviving entity ids, never the owner)."""
    out: dict[int, list[int]] = {}
    if not obs_ids:
        return out
    for r in conn.execute(
            "SELECT p.observation_id, p.role, p.handle_kind, p.handle_value, "
            "CASE WHEN e.status = 'merged' THEN e.merged_into ELSE e.id END AS eid, "
            "CASE WHEN e.status = 'merged' THEN m.type ELSE e.type END AS etype "
            "FROM observation_participants p "
            "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            "JOIN entities e ON e.id = h.entity_id LEFT JOIN entities m ON m.id = e.merged_into "
            f"WHERE p.observation_id IN ({_marks(obs_ids)}) AND h.ignored = 0 "
            "AND p.role IN ('participant', 'organizer') "
            "ORDER BY p.observation_id, p.handle_kind, p.handle_value", obs_ids):
        if r["etype"] != "person" or r["eid"] in owners \
                or (r["handle_kind"], (r["handle_value"] or "").lower()) in owner_handles:
            continue
        ids = out.setdefault(r["observation_id"], [])
        if r["eid"] not in ids:
            ids.append(r["eid"])
    return out


def window_days(today: date) -> tuple[str, str]:
    return today.isoformat(), (today + timedelta(days=1)).isoformat()

"""The reads behind the web UI's JSON API, one function per page.

Every function takes a read-only connection (the server's snapshot) and
returns plain dicts — the same discipline as lifecrm/views.py, whose entry
view this reuses. Nothing here writes, and nothing here decides what is true.

The two tiers stay apart all the way to the page. Confirmed rows (entries,
measurements, mentions) and observed rows (observations, health_records)
come back under separate keys, and every observed row carries its uuid and
the import that brought it in, so the page can cite it rather than assert it
(CLAUDE.md: observed rows are never stated as fact without citing the row).

Health numbers come from recall's own per-day rule (recall._health_days: a
summed metric takes the one app with the larger total, an averaged one pools
them), for the Health page's series and the day view's tiles alike, so they
and `ask` can never disagree about what a day's sleep or steps were. "Last
contact" is recall.last_contact's rule too: a text (on any messaging app) or
an email that passed between you and them, a call that connected or a
calendar event, nothing dated after today.

Stream groups come from recall's shared lists (TEXT_STREAMS, EMAIL_STREAMS,
STREAM_GROUPS), so a connector added there is counted here the same way.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import recall
from ..config import Config
from ..domain import days as days_mod
from ..domain.util import DomainError, is_iso_date


class NotFound(LookupError):
    """The id names nothing: the server answers 404 with this message."""


# ---------------------------------------------------------------- helpers

# A contact card is not a record of anything that happened with the person:
# it stays off the recent-records list (it has its own panel).
NOT_CONTACT_STREAMS = ("contact_card",)

# Whose side of a directed record the person is on, for the "you → them" and
# "them → you" tiles: an incoming record is from its sender, an outgoing one
# to its recipients, and a call log names only the other number, as a plain
# participant. A group message Jake sent is not one from Maya, who was only
# in the room (review._contacts reads roles the same way).
_THEIR_ROLES = {"in": ("sender", "participant"), "out": ("recipient", "participant")}

# Health metrics in the order the day view and the Health page show them.
HEALTH_ORDER = ("sleep_session", "steps", "resting_heart_rate", "heart_rate",
                "weight", "exercise_session", "distance", "nutrition",
                "total_calories", "body_fat", "hydration")
JOURNAL_ORDER = ("mood", "energy", "sleep_hours", "sleep_quality")

_AI_NAMES = {"claude": "Claude", "chatgpt": "ChatGPT"}
_EXCERPT = 280
_PER_STREAM = 60
_RECENT_EMAILS = 8
_PERSON_EMAILS = 10

# The day view's activity strip: streams whose rows are things you did,
# summed rather than only listed (recall's group names, never re-spelled).
_WORKOUT = recall.STREAM_GROUPS["workout"]
_RIDE = recall.STREAM_GROUPS["ride"]
_FOOD_ORDER = recall.STREAM_GROUPS["food order"]
_LISTENING = recall.STREAM_GROUPS["listening"]
_SEARCH = recall.STREAM_GROUPS["search"]
_FOOD_LOG = recall.STREAM_GROUPS["food"]
_NUTRITION_DAY = recall.STREAM_GROUPS["nutrition day"]
_LINKEDIN = recall.STREAM_GROUPS["LinkedIn connection"]
# The day's food totals: these nutrients, from the day's summary when the
# diary wrote one, else summed over its food log.
_FOOD_TOTALS = (("energy", "kcal"), ("protein", "g"), ("carbohydrate", "g"),
                ("fat", "g"), ("fiber", "g"))


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _observed(conn: sqlite3.Connection) -> bool:
    """The observed tier is schema v4. A snapshot of an older database (a
    restore target that has not been migrated yet) answers with empty
    observed lists rather than `no such table`."""
    return _has_table(conn, "observations")


def _loads(text: str | None) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {"value": value}


def _excerpt(text: str | None, limit: int = _EXCERPT) -> str | None:
    if text is None:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def parse_date(value: str | None, label: str = "date") -> str:
    if not value or not is_iso_date(value):
        raise ValueError(f"{label} must be YYYY-MM-DD, got '{value or ''}'")
    return value


def local_time(iso_utc: str | None, tz_name: str,
               offset_sec: int | None = None) -> str | None:
    """'YYYY-MM-DD HH:MM' in the record's own zone offset when the source
    recorded one, else the configured home timezone."""
    if not iso_utc:
        return None
    try:
        dt = datetime.strptime(iso_utc[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None
    if offset_sec is not None:
        dt = dt.astimezone(timezone(timedelta(seconds=int(offset_sec))))
    else:
        dt = dt.astimezone(ZoneInfo(tz_name))
    return dt.strftime("%Y-%m-%d %H:%M")


def today(cfg: Config) -> str:
    return recall.local_today(cfg).isoformat()


def _marks(items) -> str:
    return ",".join("?" for _ in items)


def _chunks(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def speakable(answer: str) -> str:
    """The answer as a speech synthesizer should say it: no [M1] citations,
    no snippet markers."""
    text = re.sub(r"\s*\[[A-Za-z]{1,6}\d*(?:,\s*[A-Za-z]{1,6}\d*)*\]", "", answer)
    return re.sub(r"\s+", " ", text.replace("«", "").replace("»", "")).strip()


# ---------------------------------------------------------------- observations

def _participants(conn: sqlite3.Connection, obs_ids: list[int]) -> dict[int, list[dict]]:
    """Who each observation names, resolved through the handle table. A
    handle on a merged entity reports the entity it was merged into."""
    out: dict[int, list[dict]] = {}
    for chunk in _chunks(list(obs_ids)):
        for r in conn.execute(
                "SELECT p.observation_id, p.role, p.handle_kind, p.handle_value, "
                "h.display_name, "
                "CASE WHEN e.status='merged' THEN e.merged_into ELSE e.id END AS eid, "
                "CASE WHEN e.status='merged' THEN m.name ELSE e.name END AS ename "
                "FROM observation_participants p "
                "JOIN entity_handles h ON h.kind=p.handle_kind AND h.value=p.handle_value "
                "LEFT JOIN entities e ON e.id=h.entity_id "
                "LEFT JOIN entities m ON m.id=e.merged_into "
                f"WHERE p.observation_id IN ({_marks(chunk)}) "
                "ORDER BY p.observation_id, p.role, p.handle_value", chunk):
            out.setdefault(r["observation_id"], []).append({
                "role": r["role"], "kind": r["handle_kind"],
                "value": r["handle_value"], "display_name": r["display_name"],
                "entity_id": r["eid"], "entity": r["ename"]})
    return out


def _who(stream: str, direction: str | None, origin_app: str | None,
         parts: list[dict], owners: frozenset | set = frozenset()) -> str | None:
    """One readable line for who said it to whom, computed here so every page
    words it the same way. The owner is 'you': the sender of an outgoing
    record, a recipient of an incoming one — and, on a record with no
    direction (a calendar event), never listed as a third party."""
    def name(p: dict) -> str:
        return p["entity"] or p["display_name"] or f"{p['kind']}:{p['value']}"

    def names(ps: list[dict]) -> str:
        unique = list(dict.fromkeys(name(p) for p in ps))
        return ", ".join(unique[:3]) + (f" +{len(unique) - 3}" if len(unique) > 3 else "")

    if stream.endswith("_chat"):
        bot = _AI_NAMES.get((origin_app or "").lower(), origin_app or "assistant")
        return f"you → {bot}" if direction == "out" else f"{bot} → you"
    # A payment names the other side as its counterparty; your own account
    # is its sender (money out) or recipient (money in).
    counter = [p for p in parts if p["role"] == "counterparty"]
    if counter and direction in ("out", "in"):
        return f"you → {names(counter)}" if direction == "out" \
            else f"{names(counter)} → you"
    senders = [p for p in parts if p["role"] == "sender"]
    receivers = [p for p in parts if p["role"] in ("recipient", "participant")]
    if direction == "out":
        return f"you → {names(receivers)}" if receivers else "you"
    if direction == "in" and senders:
        return f"{names(senders)} → you"
    if direction == "self":          # an email to yourself, Saved Messages
        return "you → you"
    others = [p for p in parts if p.get("entity_id") not in owners]
    if not others and parts:
        return "you"
    # Nothing says which side is yours (an email before [owner] is set): who
    # wrote to whom, as the headers say — "you" wherever a handle is yours.
    def side(ps: list[dict]) -> str:
        shown = list(dict.fromkeys("you" if p.get("entity_id") in owners else name(p)
                                   for p in ps))
        return ", ".join(shown[:3]) + (f" +{len(shown) - 3}" if len(shown) > 3 else "")

    if direction is None and senders and receivers:
        return f"{side(senders)} → {side(receivers)}"
    return names(others) or None


def _thread_key(detail: dict) -> tuple[str, str] | None:
    for key in ("chat", "conversation_id", "thread", "thread_id"):
        value = detail.get(key)
        if isinstance(value, (str, int)) and str(value):
            return key, str(value)
    return None


_OBS_SELECT = ("SELECT o.*, i.uuid AS import_uuid, i.connector, i.account, "
               "i.source_file, i.started_at AS import_started_at, "
               "i.status AS import_status "
               "FROM observations o JOIN imports i ON i.id=o.import_id ")


def _obs_items(conn: sqlite3.Connection, cfg: Config, rows: list,
               full: bool = False) -> list[dict]:
    parts = _participants(conn, [r["id"] for r in rows])
    owners = (frozenset(_owner_entities(conn, cfg, _merged_into_map(conn)))
              if rows and _observed(conn) else frozenset())
    out = []
    for r in rows:
        p = parts.get(r["id"], [])
        detail = _loads(r["value_json"])
        thread = _thread_key(detail)
        item = {
            "uuid": r["uuid"], "tier": "observed", "stream": r["stream"],
            "date": r["journal_date"], "started_at": r["started_at"],
            "local_time": local_time(r["started_at"], cfg.timezone,
                                     r["utc_offset_sec"]),
            "direction": r["direction"], "title": r["title"],
            "text": r["body"] if full else _excerpt(r["body"]),
            "value": r["value"], "unit": r["unit"],
            "origin_app": r["origin_app"],
            "thread": thread[1] if thread else None,
            "participants": p,
            "who": _who(r["stream"], r["direction"], r["origin_app"], p, owners),
            "import": {"uuid": r["import_uuid"], "connector": r["connector"],
                       "account": r["account"], "source_file": r["source_file"]},
        }
        if full:
            item.update({
                "ended_at": r["ended_at"],
                "local_end": local_time(r["ended_at"], cfg.timezone,
                                        r["utc_offset_sec"]),
                "status": r["status"],
                "detail": detail, "media_path": r["media_path"],
                "external_id": r["external_id"],
                "last_modified_at": r["last_modified_at"],
                "created_at": r["created_at"], "updated_at": r["updated_at"]})
            item["import"].update({"started_at": r["import_started_at"],
                                   "status": r["import_status"]})
        out.append(item)
    return out


def observation(conn: sqlite3.Connection, cfg: Config, obs_uuid: str) -> dict:
    """One observed row in full: what, when, who, and the import it came
    from — plus the few rows around it in the same thread, for context."""
    if not _observed(conn):
        raise NotFound(f"observation {obs_uuid} does not exist (no observed tier "
                       f"in this database)")
    row = conn.execute(_OBS_SELECT + "WHERE o.uuid=?", (obs_uuid,)).fetchone()
    if row is None:
        raise NotFound(f"observation {obs_uuid} does not exist")
    item = _obs_items(conn, cfg, [row], full=True)[0]
    item["cited_by"] = [dict(r) for r in conn.execute(
        "SELECT e.uuid, e.journal_date AS date, e.title FROM entry_observations eo "
        "JOIN entries e ON e.id=eo.entry_id WHERE eo.observation_id=? "
        "AND e.status='active' ORDER BY e.journal_date", (row["id"],))]
    item["context"] = _thread_context(conn, cfg, row)
    return item


def _thread_context(conn: sqlite3.Connection, cfg: Config, row) -> list[dict]:
    """Up to six rows either side in the same thread (same stream, same chat
    or conversation id), looked for within a day either side."""
    key = _thread_key(_loads(row["value_json"]))
    if key is None:
        return []
    d = date.fromisoformat(row["journal_date"])
    try:
        rows = conn.execute(
            _OBS_SELECT + "WHERE o.stream=? AND o.status='active' "
            "AND o.journal_date BETWEEN ? AND ? "
            f"AND CAST(json_extract(o.value_json, '$.{key[0]}') AS TEXT)=? "
            "ORDER BY o.started_at, o.id LIMIT 400",
            (row["stream"], (d - timedelta(days=1)).isoformat(),
             (d + timedelta(days=1)).isoformat(), key[1])).fetchall()
    except sqlite3.OperationalError:
        return []                     # value_json that is not JSON
    at = next((i for i, r in enumerate(rows) if r["id"] == row["id"]), None)
    if at is None:
        return []
    window = rows[max(0, at - 6):at + 7]
    items = _obs_items(conn, cfg, window)
    for it in items:
        it["current"] = it["uuid"] == row["uuid"]
    return items


# ---------------------------------------------------------------- entries

def _entry_rows(conn: sqlite3.Connection, where: str, args: tuple,
                order: str = "e.id", limit: int | None = None) -> list[dict]:
    sql = ("SELECT e.id, e.uuid, e.journal_date, e.title, e.body, e.time_of_day, "
           "e.occurred_at, e.due_at, e.closed_at, e.closure, e.confidence, "
           "k.name AS kind FROM entries e JOIN entry_kinds k ON k.id=e.kind_id "
           f"WHERE {where} ORDER BY {order}")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, args).fetchall()
    ids = [r["id"] for r in rows]
    cats: dict[int, list] = {}
    ents: dict[int, list] = {}
    meas: dict[int, list] = {}
    for chunk in _chunks(ids):
        for c in conn.execute(
                "SELECT ec.entry_id, c.name, ec.is_primary FROM entry_categories ec "
                "JOIN categories c ON c.id=ec.category_id "
                f"WHERE ec.entry_id IN ({_marks(chunk)}) "
                "ORDER BY ec.is_primary DESC, c.name", chunk):
            cats.setdefault(c["entry_id"], []).append(c)
        for m in conn.execute(
                "SELECT m.entry_id, en.id, en.name, en.type, m.role FROM mentions m "
                "JOIN entities en ON en.id=m.entity_id "
                f"WHERE m.entry_id IN ({_marks(chunk)}) ORDER BY en.name", chunk):
            ents.setdefault(m["entry_id"], []).append(
                {"id": m["id"], "name": m["name"], "type": m["type"],
                 "role": m["role"]})
        for m in conn.execute(
                "SELECT entry_id, metric, numeric_value, text_value, unit "
                f"FROM measurements WHERE entry_id IN ({_marks(chunk)}) "
                "AND status='active' ORDER BY id", chunk):
            meas.setdefault(m["entry_id"], []).append(
                {"metric": m["metric"], "value": m["numeric_value"]
                 if m["numeric_value"] is not None else m["text_value"],
                 "unit": m["unit"]})
    out = []
    for r in rows:
        c = cats.get(r["id"], [])
        out.append({
            "uuid": r["uuid"], "tier": "confirmed", "date": r["journal_date"],
            "kind": r["kind"], "title": r["title"], "body": r["body"],
            "time_of_day": r["time_of_day"], "occurred_at": r["occurred_at"],
            "due_at": r["due_at"], "closed_at": r["closed_at"],
            "closure": r["closure"], "confidence": r["confidence"],
            "category": next((x["name"] for x in c if x["is_primary"]), None),
            "categories": [x["name"] for x in c],
            "entities": ents.get(r["id"], []),
            "measurements": meas.get(r["id"], [])})
    return out


def entry(conn: sqlite3.Connection, cfg: Config, entry_uuid: str) -> dict:
    """views.entry_detail (categories, entities, measurements, provenance
    with the transcript passage, revisions, links), plus any observations
    the entry cites as its source."""
    from .. import views
    try:
        detail = views.entry_detail(conn, entry_uuid)
    except DomainError as e:
        raise NotFound(str(e)) from e
    detail["tier"] = "confirmed"
    for p in detail["provenance"]:
        s = conn.execute("SELECT uuid, recorded_at, timezone FROM sources WHERE id=?",
                         (p["source_id"],)).fetchone()
        if s:
            p["source_uuid"] = s["uuid"]
            p["recorded_at"] = s["recorded_at"]
    detail["observations"] = []
    if _observed(conn):
        rows = conn.execute(
            _OBS_SELECT + "JOIN entry_observations eo ON eo.observation_id=o.id "
            "JOIN entries e ON e.id=eo.entry_id WHERE e.uuid=? ORDER BY o.started_at",
            (entry_uuid,)).fetchall()
        detail["observations"] = _obs_items(conn, cfg, rows)
    return detail


# ---------------------------------------------------------------- day

_TIME_ORDER = ("CASE e.time_of_day WHEN 'morning' THEN 1 WHEN 'afternoon' THEN 2 "
               "WHEN 'evening' THEN 3 WHEN 'night' THEN 4 ELSE 5 END, "
               "COALESCE(e.occurred_at, ''), e.id")


def _day_health(conn: sqlite3.Connection, journal_date: str) -> list[dict]:
    """The day's sensor numbers, each by the rule `ask` and the Health page
    use (recall._health_days). Samsung Health and Google Fit both write the
    same walk into Health Connect, and an iPhone and a Watch both count the
    same steps, so a summed metric takes the one app with the larger total
    rather than adding them; `origin_app` names it when more than one
    recorded the day. A metric recall has no spec for is averaged."""
    rows = conn.execute(
        "SELECT metric, AVG(value) AS mean, COUNT(*) AS n, "
        "MAX(unit) AS unit, GROUP_CONCAT(DISTINCT source_file) AS files "
        "FROM health_records WHERE journal_date=? AND status='active' "
        "AND value IS NOT NULL GROUP BY metric", (journal_date,)).fetchall()
    by_metric = {r["metric"]: r for r in rows}
    order = list(HEALTH_ORDER) + sorted(m for m in by_metric if m not in HEALTH_ORDER)
    window = {"start": journal_date, "end": journal_date}
    out = []
    for metric in order:
        r = by_metric.get(metric)
        if r is None:
            continue
        spec = recall.HEALTH_METRICS.get(metric)
        origin = None
        files = sorted((r["files"] or "").split(",")) if r["files"] else []
        if spec:
            found, _, _, files = recall._health_days(conn, metric, spec, window)
            _, raw, records, origin = found[0]
            value = recall._round(raw * spec.get("factor", 1), spec["unit"])
            unit, label, agg = spec["unit"], spec["label"], spec["agg"]
        else:
            value, unit, label, agg = round(r["mean"], 2), r["unit"] or "", metric, "avg"
            records = r["n"]
        item = {"metric": metric, "label": label, "value": value,
                "unit": unit, "agg": agg, "records": records,
                "origin": "sensor import", "source_files": files}
        if origin is not None:
            item["origin_app"] = origin
        out.append(item)
    return out


def _minutes(rows) -> float:
    return round(sum(r["value"] for r in rows
                     if r["value"] is not None and (r["unit"] or "") == "min"), 1)


def _plays(r) -> int:
    """How many times a listening row was played that day (one at least)."""
    n = _loads(r["value_json"]).get("plays")
    return int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) \
        and n >= 1 else 1


def _priced(rows, tz_name: str) -> dict:
    """Rides or food orders: each with its fare, and the total when every
    row is in one currency (a fare is a cost, not a signed transaction)."""
    units = {r["unit"] for r in rows if r["value"] is not None}
    total = (round(sum(r["value"] for r in rows if r["value"] is not None), 2)
             if len(units) == 1 else None)
    return {"count": len(rows), "total": total,
            "unit": next(iter(units)) if len(units) == 1 else None,
            "items": [{"uuid": r["uuid"], "title": r["title"],
                       "text": _excerpt(r["body"], 120), "value": r["value"],
                       "unit": r["unit"], "origin_app": r["origin_app"],
                       "local_time": local_time(r["started_at"], tz_name,
                                                r["utc_offset_sec"])}
                      for r in rows]}


def _food_totals(day_rows: list, food_rows: list) -> dict | None:
    """The day's intake: the diary's own daily summary when it wrote one
    (the newest row wins, as `lifecrm nutrients` reads it), else the food
    log summed. Only the headline nutrients; `lifecrm nutrients` has the rest."""
    if not day_rows and not food_rows:
        return None
    if day_rows:
        row = day_rows[-1]
        values = _loads(row["value_json"]).get("nutrients") or {}
        basis, uuids = "nutrition_day", [row["uuid"]]
        totals = {}
        for key, unit in _FOOD_TOTALS:
            pair = values.get(key)
            if isinstance(pair, list) and len(pair) == 2 and pair[1] == unit \
                    and isinstance(pair[0], (int, float)):
                totals[key] = round(float(pair[0]), 1)
        if "energy" not in totals and row["value"] is not None and row["unit"] == "kcal":
            totals["energy"] = round(row["value"], 1)
    else:
        basis, uuids = "food_log", [r["uuid"] for r in food_rows]
        totals = {}
        for r in food_rows:
            values = _loads(r["value_json"]).get("nutrients") or {}
            for key, unit in _FOOD_TOTALS:
                pair = values.get(key)
                if isinstance(pair, list) and len(pair) == 2 and pair[1] == unit \
                        and isinstance(pair[0], (int, float)):
                    totals[key] = totals.get(key, 0.0) + float(pair[0])
        if "energy" not in totals:
            kcal = [r["value"] for r in food_rows
                    if r["value"] is not None and r["unit"] == "kcal"]
            if kcal:
                totals["energy"] = sum(kcal)
        totals = {k: round(v, 1) for k, v in totals.items()}
    return {"basis": basis, "foods": len(food_rows), "uuids": uuids[:20],
            "totals": [{"key": k, "value": totals[k], "unit": u}
                       for k, u in _FOOD_TOTALS if k in totals]}


def _day_activity(conn: sqlite3.Connection, cfg: Config, journal_date: str) -> dict:
    """What the day's imports say you did, added up: workouts, rides and
    food orders (each listed with its row), the food log's totals, minutes
    listened and how many searches. Every figure is observed and names the
    rows behind it, so the page cites rather than asserts."""
    if not _observed(conn):
        return {}

    def rows(streams):
        return conn.execute(
            _OBS_SELECT + "WHERE o.journal_date=? AND o.status='active' "
            f"AND o.stream IN ({_marks(streams)}) ORDER BY o.started_at, o.id",
            (journal_date, *streams)).fetchall()

    out: dict = {}
    workouts = rows(_WORKOUT)
    if workouts:
        out["workouts"] = {"count": len(workouts), "minutes": _minutes(workouts),
                           "items": [{
                               "uuid": r["uuid"], "title": r["title"],
                               "text": _excerpt((r["body"] or "").split("\n")[0], 160),
                               "minutes": r["value"] if r["unit"] == "min" else None,
                               "origin_app": r["origin_app"],
                               "local_time": local_time(r["started_at"], cfg.timezone,
                                                        r["utc_offset_sec"])}
                               for r in workouts]}
    for key, streams in (("rides", _RIDE), ("orders", _FOOD_ORDER)):
        found = rows(streams)
        if found:
            out[key] = _priced(found, cfg.timezone)
    food = _food_totals(rows(_NUTRITION_DAY), rows(_FOOD_LOG))
    if food:
        out["food"] = food
    plays = rows(_LISTENING)
    if plays:
        top = sorted(plays, key=lambda r: (-(r["value"] or 0), r["started_at"] or "",
                                           r["id"]))[:3]
        out["listening"] = {
            "minutes": _minutes(plays), "tracks": len(plays),
            "plays": sum(_plays(r) for r in plays),
            "top": [{"uuid": r["uuid"], "title": r["title"], "by": r["body"],
                     "minutes": r["value"]} for r in top]}
    searches = conn.execute(
        f"SELECT COUNT(*) AS n FROM observations WHERE journal_date=? "
        f"AND status='active' AND stream IN ({_marks(_SEARCH)})",
        (journal_date, *_SEARCH)).fetchone()["n"]
    if searches:
        out["searches"] = {"count": searches}
    return out


def _neighbor(conn: sqlite3.Connection, journal_date: str, before: bool) -> str | None:
    op, agg = ("<", "MAX") if before else (">", "MIN")
    parts = [f"SELECT {agg}(journal_date) AS d FROM entries "
             f"WHERE status='active' AND journal_date {op} ?",
             f"SELECT {agg}(journal_date) FROM days WHERE summary IS NOT NULL "
             f"AND journal_date {op} ?",
             f"SELECT {agg}(journal_date) FROM health_records "
             f"WHERE status='active' AND journal_date {op} ?"]
    if _observed(conn):
        parts.append(f"SELECT {agg}(journal_date) FROM observations "
                     f"WHERE status='active' AND journal_date {op} ?")
    sql = (f"SELECT {agg}(d) AS d FROM (" + " UNION ALL ".join(parts) + ")")
    return conn.execute(sql, (journal_date,) * len(parts)).fetchone()["d"]


def day(conn: sqlite3.Connection, cfg: Config, journal_date: str) -> dict:
    """Everything filed under one journal date: the day's summary and
    measurements, confirmed entries grouped by primary category, sensor
    numbers, and observed records grouped by stream."""
    journal_date = parse_date(journal_date)
    d = conn.execute("SELECT summary FROM days WHERE journal_date=?",
                     (journal_date,)).fetchone()
    day_meas = [dict(r) for r in conn.execute(
        "SELECT metric, COALESCE(normalized_value, numeric_value) AS value, "
        "COALESCE(normalized_unit, unit) AS unit, text_value FROM measurements "
        "WHERE journal_date=? AND status='active' ORDER BY id", (journal_date,))]
    entries = _entry_rows(conn, "e.journal_date=? AND e.status='active'",
                          (journal_date,), order=_TIME_ORDER)
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(e["category"] or "other", []).append(e)

    observed = []
    if _observed(conn):
        counts = conn.execute(
            "SELECT stream, COUNT(*) AS n FROM observations WHERE journal_date=? "
            "AND status='active' GROUP BY stream ORDER BY MIN(started_at)",
            (journal_date,)).fetchall()
        for c in counts:
            rows = conn.execute(
                _OBS_SELECT + "WHERE o.journal_date=? AND o.stream=? "
                "AND o.status='active' ORDER BY o.started_at, o.id LIMIT ?",
                (journal_date, c["stream"], _PER_STREAM)).fetchall()
            observed.append({"stream": c["stream"], "count": c["n"],
                             "truncated": c["n"] > len(rows),
                             "items": _obs_items(conn, cfg, rows)})

    def first(metric):
        return next((m["value"] for m in day_meas if m["metric"] == metric), None)

    sources = [dict(r) for r in conn.execute(
        "SELECT id, uuid, type, recorded_at FROM sources WHERE journal_date=? "
        "ORDER BY COALESCE(recorded_at, created_at), id", (journal_date,))]
    extras = _day_extras(conn, cfg, journal_date, observed)
    return {
        **extras,
        "date": journal_date,
        "weekday": date.fromisoformat(journal_date).strftime("%A"),
        "today": today(cfg),
        "summary": d["summary"] if d else None,
        "prev": _neighbor(conn, journal_date, before=True),
        "next": _neighbor(conn, journal_date, before=False),
        "mood": first("mood"), "energy": first("energy"),
        "sleep_hours": first("sleep_hours"),
        "measurements": day_meas,
        "entries_count": len(entries),
        "by_category": [{"category": k, "entries": v} for k, v in groups.items()],
        "health": _day_health(conn, journal_date),
        "activity": _day_activity(conn, cfg, journal_date),
        "observed": observed,
        "sources": sources,
    }


def days(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """Per-day activity for a calendar heat map: confirmed entries, observed
    records, sensor metrics present, and the journal's mood."""
    start, end = parse_date(start, "start"), parse_date(end, "end")
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    if hi < lo:
        raise ValueError(f"end {end} is before start {start}")
    if (hi - lo).days > 400:
        raise ValueError(f"{start} to {end} is {(hi - lo).days + 1} days — "
                         f"ask for 400 days or fewer")
    window = (start, end)
    counts: dict[str, dict] = {}

    def put(sql: str, key: str):
        for r in conn.execute(sql, window):
            counts.setdefault(r["d"], {})[key] = r["v"]

    put("SELECT journal_date AS d, COUNT(*) AS v FROM entries WHERE status='active' "
        "AND journal_date BETWEEN ? AND ? GROUP BY d", "entries")
    put("SELECT journal_date AS d, COUNT(DISTINCT metric) AS v FROM health_records "
        "WHERE status='active' AND journal_date BETWEEN ? AND ? GROUP BY d", "health")
    put("SELECT journal_date AS d, ROUND(AVG(COALESCE(normalized_value, "
        "numeric_value)), 1) AS v FROM measurements WHERE metric='mood' "
        "AND status='active' AND journal_date BETWEEN ? AND ? GROUP BY d", "mood")
    if _observed(conn):
        put("SELECT journal_date AS d, COUNT(*) AS v FROM observations "
            "WHERE status='active' AND journal_date BETWEEN ? AND ? GROUP BY d",
            "observations")
    out = []
    cur = lo
    while cur <= hi:
        c = counts.get(cur.isoformat(), {})
        out.append({"date": cur.isoformat(), "entries": c.get("entries", 0),
                    "observations": c.get("observations", 0),
                    "health": c.get("health", 0), "mood": c.get("mood")})
        cur += timedelta(days=1)
    return {"start": start, "end": end, "days": out}


# ---------------------------------------------------------------- people

def _merged_into_map(conn: sqlite3.Connection) -> dict[int, int]:
    """merged entity id -> the surviving entity at the end of its chain."""
    parent = {r["id"]: r["merged_into"] for r in conn.execute(
        "SELECT id, merged_into FROM entities WHERE status='merged' "
        "AND merged_into IS NOT NULL")}
    out = {}
    for eid in parent:
        seen, cur = {eid}, parent[eid]
        while cur in parent and cur not in seen:
            seen.add(cur)
            cur = parent[cur]
        out[eid] = cur
    return out


def _owner_entities(conn: sqlite3.Connection, cfg: Config,
                    roots: dict[int, int]) -> set[int]:
    """The entities that are the owner, by lifecrm/owner.py's rule — the one
    `ask` and review._load_people use: linked to a handle config.toml lists
    under [owner], to one that sends the outgoing records or that an import
    names as its own account, or to one the source itself labels as the
    owner ('you (this phone)'). The owner takes part in every text, call and
    meeting, so listed as a person they would head People as the most
    recently contacted — the dashboards' `me` leaves them out the same way."""
    if not _observed(conn):
        return set()
    from .. import owner as owner_mod
    return {roots.get(e, e) for e in owner_mod.identity(conn, cfg).entities}


def _contact_clause(today_iso: str, they_sent: str,
                    they_sent_args: tuple = ()) -> tuple[str, list]:
    """What counts as contact with someone — the rule `ask` answers 'when did
    I last talk to X' with (recall.last_contact): a text on any messaging app
    or an email that passed between you and them (one they sent, or one you
    sent), a call that connected, or a calendar event, never dated after
    today. A group message someone else sent is not them texting you, an
    email they were only copied on is not them writing, a missed call is an
    attempt, a reaction or a note is not a conversation, and next week's
    meeting has not happened yet. `they_sent` is the SQL saying this person
    sent row o."""
    texts = recall.TEXT_STREAMS + recall.EMAIL_STREAMS
    calls = recall.CALL_STREAMS
    calendar = recall.STREAM_GROUPS["calendar"]
    return ((f"((o.stream IN ({_marks(texts)}) "
             f"AND (o.direction = 'out' OR {they_sent})) "
             f"OR o.stream IN ({_marks(calendar)}) "
             f"OR (o.stream IN ({_marks(calls)}) "
             "AND (o.value IS NULL OR o.value > 0))) AND o.journal_date <= ?"),
            [*texts, *they_sent_args, *calendar, *calls, today_iso])


_RECENT_FIRST = "o.journal_date DESC, o.started_at DESC, o.id DESC"


def people(conn: sqlite3.Connection, cfg: Config, as_of: str | None = None) -> dict:
    """Every person entity with when you were last in touch, across both
    tiers: the latest journal entry mentioning them and the latest text,
    connected call or past calendar event naming one of their handles. The
    owner is not a contact and is left out. `as_of` stands in for today
    (the Obsidian vault's start pages use the database's last change)."""
    roots = _merged_into_map(conn)
    owners = _owner_entities(conn, cfg, roots)
    now = as_of or today(cfg)
    persons = [p for p in conn.execute(
        "SELECT id, uuid, name, notes FROM entities WHERE type='person' "
        "AND status='active' ORDER BY name COLLATE NOCASE") if p["id"] not in owners]
    aliases: dict[int, list] = {}
    for r in conn.execute("SELECT entity_id, alias FROM entity_aliases ORDER BY alias"):
        aliases.setdefault(roots.get(r["entity_id"], r["entity_id"]), []).append(r["alias"])
    mentions = {r["entity_id"]: r for r in conn.execute(
        "SELECT m.entity_id, COUNT(DISTINCT e.id) AS n, MAX(e.journal_date) AS last "
        "FROM mentions m JOIN entries e ON e.id=m.entry_id WHERE e.status='active' "
        "GROUP BY m.entity_id")}
    handles: dict[int, int] = {}
    observed: dict[int, dict] = {}
    if _observed(conn):
        for r in conn.execute("SELECT entity_id, COUNT(*) AS n FROM entity_handles "
                              "WHERE entity_id IS NOT NULL GROUP BY entity_id"):
            eid = roots.get(r["entity_id"], r["entity_id"])
            handles[eid] = handles.get(eid, 0) + r["n"]
        # p is this person's own participant row on o.
        contact, contact_args = _contact_clause(now, "p.role = 'sender'")
        for r in conn.execute(
                "SELECT h.entity_id, COUNT(DISTINCT o.id) AS n, "
                f"MAX(CASE WHEN {contact} THEN o.journal_date END) AS last "
                "FROM entity_handles h "
                "JOIN observation_participants p ON p.handle_kind=h.kind "
                "AND p.handle_value=h.value "
                "JOIN observations o ON o.id=p.observation_id "
                "WHERE h.entity_id IS NOT NULL AND o.status='active' "
                f"AND o.stream NOT IN ({_marks(NOT_CONTACT_STREAMS)}) "
                "GROUP BY h.entity_id", (*contact_args, *NOT_CONTACT_STREAMS)):
            eid = roots.get(r["entity_id"], r["entity_id"])
            prev = observed.get(eid)
            observed[eid] = {"n": r["n"] + (prev["n"] if prev else 0),
                             "last": max(filter(None, [r["last"],
                                                       prev and prev["last"]]),
                                         default=None)}
    out = []
    for p in persons:
        m = mentions.get(p["id"])
        o = observed.get(p["id"])
        last_m = m["last"] if m else None
        last_o = o["last"] if o else None
        if last_m and (not last_o or last_m >= last_o):
            last = {"date": last_m, "tier": "confirmed"}
        elif last_o:
            last = {"date": last_o, "tier": "observed"}
        else:
            last = None
        out.append({"id": p["id"], "uuid": p["uuid"], "name": p["name"],
                    "notes": p["notes"], "aliases": aliases.get(p["id"], []),
                    "mentions": m["n"] if m else 0, "last_mention": last_m,
                    "handles": handles.get(p["id"], 0),
                    "observations": o["n"] if o else 0, "last_observed": last_o,
                    "last_contact": last})
    # Most recent contact first; a stable sort keeps the never-contacted in
    # name order at the end.
    out.sort(key=lambda x: (x["last_contact"] or {}).get("date") or "", reverse=True)
    recent_emails = []
    if _observed(conn):
        emails = recall.EMAIL_STREAMS
        rows = conn.execute(
            _OBS_SELECT + f"WHERE o.stream IN ({_marks(emails)}) AND o.status='active' "
            f"AND o.journal_date <= ? ORDER BY {_RECENT_FIRST} LIMIT ?",
            (*emails, now, _RECENT_EMAILS)).fetchall()
        recent_emails = _obs_items(conn, cfg, rows)
    return {"today": now, "people": out, "recent_emails": recent_emails}


def person(conn: sqlite3.Connection, cfg: Config, entity_id: int) -> dict:
    """One entity across both tiers: aliases, linked handles, journal
    mentions, recent observed records (and upcoming ones, apart), contact-card
    fields, last contact. On the owner's own page (`is_owner`) there is no
    last contact: every record they took part in would claim to be one."""
    row = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        raise NotFound(f"entity id={entity_id} does not exist")
    roots = _merged_into_map(conn)
    # Merging moves mentions but leaves handles on the merged entity, so the
    # survivor's view includes everything merged into it.
    ids = [entity_id] + [e for e, root in roots.items() if root == entity_id]
    now = today(cfg)
    is_owner = roots.get(entity_id, entity_id) in _owner_entities(conn, cfg, roots)
    out = {"id": row["id"], "uuid": row["uuid"], "name": row["name"],
           "type": row["type"], "notes": row["notes"], "status": row["status"],
           "merged_into": roots.get(entity_id), "today": now, "is_owner": is_owner}
    out["aliases"] = [r["alias"] for r in conn.execute(
        f"SELECT DISTINCT alias FROM entity_aliases WHERE entity_id IN ({_marks(ids)}) "
        "ORDER BY alias", ids)]
    out["mentions"] = [dict(r) for r in conn.execute(
        "SELECT e.uuid, e.journal_date AS date, e.title, k.name AS kind, "
        "GROUP_CONCAT(DISTINCT m.role) AS roles FROM mentions m "
        "JOIN entries e ON e.id=m.entry_id JOIN entry_kinds k ON k.id=e.kind_id "
        f"WHERE m.entity_id IN ({_marks(ids)}) AND e.status='active' "
        "GROUP BY e.id ORDER BY e.journal_date DESC, e.id DESC LIMIT 50", ids)]
    out["mention_count"] = conn.execute(
        "SELECT COUNT(DISTINCT m.entry_id) AS c FROM mentions m "
        "JOIN entries e ON e.id=m.entry_id "
        f"WHERE m.entity_id IN ({_marks(ids)}) AND e.status='active'", ids).fetchone()["c"]
    out.update({"handles": [], "observations": [], "upcoming": [], "streams": [],
                "contact_cards": [], "linkedin": None, "emails": [],
                "last_contact": None, "last_in": None, "last_out": None})
    latest_entry = out["mentions"][0] if out["mentions"] else None
    latest_obs = None
    if _observed(conn):
        out["handles"] = [dict(r) for r in conn.execute(
            "SELECT kind, value, display_name, observation_count, first_seen_at, "
            "last_seen_at, confirmed_at FROM entity_handles "
            f"WHERE entity_id IN ({_marks(ids)}) "
            "ORDER BY observation_count DESC, kind, value", ids)]

        def party(roles: tuple[str, ...] = ()) -> tuple[str, list]:
            """Observations naming one of the person's handles — in one of
            `roles` when given."""
            role = f"AND p.role IN ({_marks(roles)}) " if roles else ""
            return ("o.id IN (SELECT p.observation_id FROM observation_participants p "
                    "JOIN entity_handles h ON h.kind=p.handle_kind "
                    "AND h.value=p.handle_value "
                    f"WHERE h.entity_id IN ({_marks(ids)}) {role}) "
                    "AND o.status='active' ", [*ids, *roles])

        involving, args = party()
        not_card = f"AND o.stream NOT IN ({_marks(NOT_CONTACT_STREAMS)}) "
        rows = conn.execute(
            _OBS_SELECT + "WHERE " + involving + not_card + "AND o.journal_date <= ? "
            "ORDER BY o.started_at DESC, o.id DESC LIMIT 30",
            (*args, *NOT_CONTACT_STREAMS, now)).fetchall()
        out["observations"] = _obs_items(conn, cfg, rows)
        # A calendar export reaches months ahead: the next few events are
        # shown apart, never as "recent".
        rows = conn.execute(
            _OBS_SELECT + "WHERE " + involving + not_card + "AND o.journal_date > ? "
            "ORDER BY o.journal_date, o.started_at, o.id LIMIT 5",
            (*args, *NOT_CONTACT_STREAMS, now)).fetchall()
        out["upcoming"] = _obs_items(conn, cfg, rows)
        out["streams"] = [dict(r) for r in conn.execute(
            "SELECT o.stream, COUNT(*) AS count, MAX(o.started_at) AS latest "
            "FROM observations o WHERE " + involving +
            "GROUP BY o.stream ORDER BY count DESC", args)]
        cards = conn.execute(
            _OBS_SELECT + "WHERE " + involving + "AND o.stream='contact_card' "
            "ORDER BY o.started_at DESC LIMIT 3", args).fetchall()
        out["contact_cards"] = [
            {**item, "fields": _card_fields(_loads(r["value_json"]))}
            for item, r in zip(_obs_items(conn, cfg, cards, full=True), cards)]
        # Where they work, as their LinkedIn connection row says it — the
        # newest export's row wins (a new job updates the row in place).
        li = conn.execute(
            _OBS_SELECT + "WHERE " + involving +
            f"AND o.stream IN ({_marks(_LINKEDIN)}) "
            "ORDER BY COALESCE(o.last_modified_at, o.started_at) DESC, o.id DESC "
            "LIMIT 1", (*args, *_LINKEDIN)).fetchone()
        if li is not None:
            d = _loads(li["value_json"])
            out["linkedin"] = {
                "uuid": li["uuid"], "tier": "observed", "name": li["title"],
                "position": d.get("position") or None,
                "company": d.get("company") or None,
                "headline": li["body"], "connected_on": li["journal_date"],
                "import": {"uuid": li["import_uuid"], "connector": li["connector"]}}
        emails = recall.EMAIL_STREAMS
        rows = conn.execute(
            _OBS_SELECT + "WHERE " + involving +
            f"AND o.stream IN ({_marks(emails)}) AND o.journal_date <= ? "
            f"ORDER BY {_RECENT_FIRST} LIMIT ?",
            (*args, *emails, now, _PERSON_EMAILS)).fetchall()
        out["emails"] = _obs_items(conn, cfg, rows)
        if not is_owner:
            sent_by_them = (
                "EXISTS (SELECT 1 FROM observation_participants ps "
                "JOIN entity_handles hs ON hs.kind=ps.handle_kind "
                "AND hs.value=ps.handle_value WHERE ps.observation_id=o.id "
                f"AND ps.role='sender' AND hs.entity_id IN ({_marks(ids)}))")
            contact, contact_args = _contact_clause(now, sent_by_them, tuple(ids))
            hit = conn.execute(
                _OBS_SELECT + "WHERE " + involving + f"AND {contact} "
                f"ORDER BY {_RECENT_FIRST} LIMIT 1", (*args, *contact_args)).fetchall()
            latest_obs = _obs_items(conn, cfg, hit)[0] if hit else None
            for direction, roles in _THEIR_ROLES.items():
                theirs, their_args = party(roles)
                hit = conn.execute(
                    _OBS_SELECT + "WHERE " + theirs + f"AND {contact} "
                    "AND o.direction=? "
                    f"ORDER BY {_RECENT_FIRST} LIMIT 1",
                    (*their_args, *contact_args, direction)).fetchall()
                out[f"last_{direction}"] = _obs_items(conn, cfg, hit)[0] if hit else None
    out.update(_person_extras(conn, cfg, entity_id, ids))
    if is_owner:
        latest_entry = None
    if latest_entry or latest_obs:
        observed_wins = latest_obs is not None and (
            latest_entry is None or latest_obs["date"] > latest_entry["date"])
        out["last_contact"] = {
            "tier": "observed" if observed_wins else "confirmed",
            "date": latest_obs["date"] if observed_wins else latest_entry["date"],
            "entry": latest_entry, "observation": latest_obs}
    return out


def _card_fields(detail: dict) -> list[dict]:
    """A contact card's value_json flattened to label/value rows for display,
    whatever shape the contacts connector gives it."""
    rows = []
    for key in sorted(detail):
        value = detail[key]
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            text = ", ".join(_card_text(v) for v in value if v not in (None, ""))
        else:
            text = _card_text(value)
        if text:
            rows.append({"label": key.replace("_", " "), "value": text})
    return rows


def _card_text(value) -> str:
    if isinstance(value, dict):
        label = value.get("label") or value.get("type")
        main = value.get("value") or value.get("address") or value.get("number") \
            or ", ".join(str(v) for k, v in value.items() if k not in ("label", "type") and v)
        return f"{main} ({label})" if label and main else str(main or label or "")
    return str(value)


# ---------------------------------------------------------------- loops

def loops(conn: sqlite3.Connection, cfg: Config, as_of: str | None = None) -> dict:
    """Open tasks and plans, overdue first, then by due date, then oldest;
    plus the most recently closed, so finishing things is visible too.
    `as_of` stands in for today, as in people()."""
    now = as_of or today(cfg)
    open_rows = _entry_rows(
        conn, "k.name IN ('task','plan') AND e.status='active' AND e.closed_at IS NULL",
        (), order="(e.due_at IS NULL), e.due_at, e.journal_date, e.id")
    t = date.fromisoformat(now)
    for e in open_rows:
        e["overdue"] = bool(e["due_at"]) and e["due_at"][:10] < now
        try:
            e["age_days"] = (t - date.fromisoformat(e["date"])).days
        except ValueError:
            e["age_days"] = None
    open_rows.sort(key=lambda e: (not e["overdue"], e["due_at"] is None,
                                  e["due_at"] or "", e["date"]))
    closed = _entry_rows(
        conn, "k.name IN ('task','plan') AND e.status='active' "
        "AND e.closed_at IS NOT NULL", (), order="e.closed_at DESC, e.id DESC",
        limit=10)
    return {"today": now, "open": open_rows,
            "overdue": sum(1 for e in open_rows if e["overdue"]),
            "recently_closed": closed}


# ---------------------------------------------------------------- sources

def imports(conn: sqlite3.Connection, cfg: Config,
            now: datetime | None = None) -> dict:
    """Where the data came from and how fresh it is: importer runs, the
    unresolved-handle queue, each connector's newest record, health files,
    and the journal's own sources. `now` (UTC) stands in for the clock, as
    people()'s `as_of` does for today."""
    as_of = (recall.local_today(cfg).isoformat() if now is None else
             days_mod.journal_date_for(now, cfg.timezone, cfg.day_rollover_hour))
    out = {"today": as_of, "imports": [], "unresolved_handles": 0,
           "unresolved": [], "connectors": [], "connectors_error": None}
    if _observed(conn):
        for r in conn.execute(
                "SELECT i.*, (SELECT COUNT(*) FROM observations o "
                "WHERE o.import_id=i.id AND o.status='active') AS live "
                "FROM imports i ORDER BY i.id DESC LIMIT 50"):
            item = {k: r[k] for k in ("uuid", "connector", "account", "source_file",
                                      "started_at", "finished_at", "status", "live")}
            item["counts"] = _loads(r["counts_json"])
            out["imports"].append(item)
        out["unresolved_handles"] = conn.execute(
            "SELECT COUNT(*) AS c FROM entity_handles WHERE entity_id IS NULL "
            "AND ignored=0 AND observation_count > 0").fetchone()["c"]
        out["unresolved"] = [dict(r) for r in conn.execute(
            "SELECT kind, value, display_name, observation_count, last_seen_at "
            "FROM entity_handles WHERE entity_id IS NULL AND ignored=0 "
            "AND observation_count > 0 ORDER BY observation_count DESC, kind, value "
            "LIMIT 10")]
        out["connectors"], out["connectors_error"] = _connector_freshness(conn, now)
    out["health_files"] = [dict(r) for r in conn.execute(
        "SELECT source_file, COUNT(*) AS records, COUNT(DISTINCT metric) AS metrics, "
        "MIN(journal_date) AS first_date, MAX(journal_date) AS last_date, "
        "MAX(updated_at) AS imported_at FROM health_records WHERE status='active' "
        "GROUP BY source_file ORDER BY imported_at DESC LIMIT 20")]
    out["journal"] = {
        "sources": [dict(r) for r in conn.execute(
            "SELECT type, COUNT(*) AS count, MAX(journal_date) AS last_date "
            "FROM sources GROUP BY type ORDER BY count DESC")],
        "entries": conn.execute("SELECT COUNT(*) AS c FROM entries "
                                "WHERE status='active'").fetchone()["c"],
        "days": conn.execute("SELECT COUNT(DISTINCT journal_date) AS c FROM entries "
                             "WHERE status='active'").fetchone()["c"],
        "last_entry": conn.execute("SELECT MAX(journal_date) AS d FROM entries "
                                   "WHERE status='active'").fetchone()["d"],
        "open_drafts": conn.execute("SELECT COUNT(*) AS c FROM extraction_runs "
                                    "WHERE status IN ('draft','reviewed')"
                                    ).fetchone()["c"]}
    return out


def nudges(conn: sqlite3.Connection, cfg: Config) -> dict:
    """What matters today (lifecrm/nudges.py) on the clock's date, as loops()
    counts overdue: the page is live, the vault's start pages are not."""
    from .. import nudges as nudges_mod
    day, now = nudges_mod.today_and_now(cfg)
    return nudges_mod.build(conn, cfg, today=day, now=now)


def _connector_freshness(conn: sqlite3.Connection, now: datetime | None = None
                         ) -> tuple[list[dict], str | None]:
    """Each discovered connector: active rows per stream, newest record, last
    run, and whether it has gone stale by its own `stale_after_days`
    (counted back from `now`, the clock unless given)."""
    try:
        from ..connectors import base as conn_base
        reg = conn_base.registry()
    except Exception as e:             # one broken connector module must not
        return [], f"{type(e).__name__}: {e}"   # take the Sources page down
    counts = {r["stream"]: r for r in conn.execute(
        "SELECT stream, COUNT(*) AS n, MAX(started_at) AS latest FROM observations "
        "WHERE status='active' GROUP BY stream")}
    runs = {r["connector"]: r for r in conn.execute(
        "SELECT connector, MAX(started_at) AS last_run, COUNT(*) AS runs "
        "FROM imports WHERE status='applied' GROUP BY connector")}
    now = now or datetime.now(timezone.utc)
    out = []
    for name in sorted(reg):
        c = reg[name]
        streams = [{"stream": s, "count": counts[s]["n"] if s in counts else 0,
                    "latest": counts[s]["latest"] if s in counts else None}
                   for s in getattr(c, "streams", ())]
        latest = max((s["latest"] for s in streams if s["latest"]), default=None)
        limit = getattr(c, "stale_after_days", None)
        run = runs.get(name)
        stale = None
        if limit and latest:
            cutoff = (now - timedelta(days=limit)).strftime("%Y-%m-%dT%H:%M:%SZ")
            stale = latest < cutoff
        out.append({"connector": name, "streams": streams,
                    "records": sum(s["count"] for s in streams),
                    "latest": latest, "last_run": run["last_run"] if run else None,
                    "runs": run["runs"] if run else 0,
                    "stale_after_days": limit, "stale": stale})
    return out, None


# ---------------------------------------------------------------- health

def health(conn: sqlite3.Connection, cfg: Config, days: int = 14,
           end: str | None = None, as_of: str | None = None) -> dict:
    """Daily series for the key sensor metrics and the journal's own
    measures over the last `days` days — the same series `ask` cites.
    `as_of` stands in for today (the nutrient report leaves today's
    still-open diary out), as in people()."""
    if not 1 <= days <= 366:
        raise ValueError(f"days must be 1-366, got {days}")
    hi = date.fromisoformat(parse_date(end, "end")) if end else recall.local_today(cfg)
    lo = hi - timedelta(days=days - 1)
    window = {"start": lo.isoformat(), "end": hi.isoformat(),
              "label": f"last {days} days", "explicit": True}

    def series(table: str, metric: str, spec: dict) -> dict | None:
        found = recall._metric_series(conn, table=table, metric=metric,
                                      spec=spec, window=window)
        if found:
            found.pop("table", None)
        return found

    metrics, missing = [], []
    for name in HEALTH_ORDER:
        found = series("health_records", name, recall.HEALTH_METRICS[name])
        (metrics.append(found) if found else missing.append(name))
    journal = [s for s in (series("measurements", n, recall.JOURNAL_METRICS[n])
                           for n in JOURNAL_ORDER) if s]
    latest = {r["metric"]: r["d"] for r in conn.execute(
        "SELECT metric, MAX(journal_date) AS d FROM health_records "
        "WHERE status='active' GROUP BY metric")}
    return {"start": window["start"], "end": window["end"], "days": days,
            "metrics": metrics, "journal": journal,
            "missing": [{"metric": m, "label": recall.HEALTH_METRICS[m]["label"],
                         "latest": latest.get(m)} for m in missing if m in latest],
            "latest": latest,
            "workouts": _workouts_per_week(conn, hi, days),
            "nutrition": _nutrition_summary(conn, cfg, window, days, as_of=as_of)}


_MIN_WEEKS = 8


def _workouts_per_week(conn: sqlite3.Connection, end: date, days: int) -> dict | None:
    """Logged workouts (the fitness connector's `workout` rows: Strong, Hevy,
    Strava) per Monday-to-Sunday week, over at least eight weeks ending with
    `end`'s week — a fortnight is two bars. None when none were imported."""
    if not _observed(conn) or conn.execute(
            f"SELECT 1 FROM observations WHERE status='active' "
            f"AND stream IN ({_marks(_WORKOUT)}) LIMIT 1", _WORKOUT).fetchone() is None:
        return None
    weeks = max(_MIN_WEEKS, -(-days // 7))
    last_monday = end - timedelta(days=end.weekday())
    first = last_monday - timedelta(weeks=weeks - 1)
    per: dict[str, dict] = {}
    for r in conn.execute(
            "SELECT journal_date, value, unit, uuid FROM observations "
            f"WHERE status='active' AND stream IN ({_marks(_WORKOUT)}) "
            "AND journal_date BETWEEN ? AND ? ORDER BY started_at, id",
            (*_WORKOUT, first.isoformat(), end.isoformat())):
        d = date.fromisoformat(r["journal_date"])
        week = (d - timedelta(days=d.weekday())).isoformat()
        slot = per.setdefault(week, {"sessions": 0, "minutes": 0.0, "uuids": []})
        slot["sessions"] += 1
        if r["value"] is not None and r["unit"] == "min":
            slot["minutes"] += r["value"]
        slot["uuids"].append(r["uuid"])
    out = []
    for i in range(weeks):
        week = (first + timedelta(weeks=i)).isoformat()
        slot = per.get(week, {"sessions": 0, "minutes": 0.0, "uuids": []})
        out.append({"week": week, "sessions": slot["sessions"],
                    "minutes": round(slot["minutes"], 1), "uuids": slot["uuids"][:20]})
    latest = conn.execute(
        f"SELECT MAX(journal_date) AS d FROM observations WHERE status='active' "
        f"AND stream IN ({_marks(_WORKOUT)})", _WORKOUT).fetchone()["d"]
    return {"start": first.isoformat(), "end": end.isoformat(), "weeks": out,
            "sessions": sum(w["sessions"] for w in out),
            "minutes": round(sum(w["minutes"] for w in out), 1), "latest": latest,
            "tier": "observed"}


def _nutrition_summary(conn: sqlite3.Connection, cfg: Config, window: dict,
                       days: int, as_of: str | None = None) -> dict | None:
    """`lifecrm nutrients` over the page's window (lifecrm/nutrients.py, the
    same report): which nutrients came out low, borderline or high against
    the reference intakes, with the report's not-medical-advice line. None
    when nothing was logged in the window."""
    from .. import nutrients
    if not _observed(conn) or conn.execute(
            f"SELECT 1 FROM observations WHERE status='active' "
            f"AND stream IN ({_marks(nutrients.STREAMS)}) "
            "AND journal_date BETWEEN ? AND ? LIMIT 1",
            (*nutrients.STREAMS, window["start"], window["end"])).fetchone() is None:
        return None
    try:
        prof = nutrients.profile(cfg)
    except DomainError as e:
        return {"error": str(e), "disclaimer": nutrients.DISCLAIMER}
    report = nutrients.build_report(conn, cfg, {
        "start": window["start"], "end": window["end"], "days": days,
        "label": window["label"], "today": as_of or today(cfg)}, prof=prof)
    items = {x["key"]: x for x in report["nutrients"]}
    uuid_of = {}
    ids = sorted({i for x in report["nutrients"]
                  for i in x["cites"]["nutrition_day"] + x["cites"]["food_log"]})
    for chunk in _chunks(ids):
        for r in conn.execute(f"SELECT id, uuid FROM observations "
                              f"WHERE id IN ({_marks(chunk)})", chunk):
            uuid_of[r["id"]] = r["uuid"]

    def line(key: str) -> dict:
        x = items[key]
        ref = x["reference"]
        cited = x["cites"]["nutrition_day"] + x["cites"]["food_log"]
        return {"key": key, "label": ref["label"], "average": x["average"],
                "unit": x["unit"], "percent": x["percent"], "status": x["status"],
                "reference_kind": ref["kind"], "reference": ref["value"],
                "upper_kind": ref["upper_kind"], "upper": ref["upper"],
                "days_over": len(x["days_over"]), "rows": len(cited),
                "latest_uuid": uuid_of.get(max(cited)) if cited else None,
                "sources": [{"food": s["food"], "share_percent": s["share_percent"]}
                            for s in x.get("sources") or []]}

    cov = report["coverage"]
    return {"disclaimer": report["disclaimer"], "profile": report["profile"]["label"],
            "start": window["start"], "end": window["end"],
            "days_counted": cov["days_counted"], "tier": "observed",
            "low": [line(k) for k in report["low"]],
            "borderline": [line(k) for k in report["borderline"]],
            "high": [line(k) for k in report["high"]],
            "over_on_some_days": [line(k) for k in report["over_on_some_days"]],
            "meets": len(report["meets"]), "notes": report["notes"]}


# ---------------------------------------------------------------- status

GENERIC_QUESTIONS = (
    "how did I sleep last week",
    "what did I do yesterday",
    "what are my open tasks",
    "how was my mood this month",
    "how many steps did I average this month",
)


def is_demo(conn: sqlite3.Connection, cfg: Config) -> bool:
    """The demo persona's database: a [demo] section in config.toml, or the
    persona's own people (by name or by their fictional 555-01xx numbers)."""
    if (cfg.extra or {}).get("demo"):
        return True
    from ..demo import persona
    names = [p.name for p in persona.PEOPLE]
    hits = conn.execute(
        f"SELECT COUNT(*) AS c FROM entities WHERE status='active' AND type='person' "
        f"AND name COLLATE NOCASE IN ({_marks(names)})", names).fetchone()["c"]
    if hits >= 3:
        return True
    if not _observed(conn):
        return False
    phones = [p.phone for p in persona.PEOPLE if p.phone]
    return conn.execute(
        f"SELECT COUNT(*) AS c FROM entity_handles WHERE kind='phone' "
        f"AND value IN ({_marks(phones)})", phones).fetchone()["c"] >= 3


def examples(conn: sqlite3.Connection, cfg: Config, demo: bool) -> list[str]:
    if demo:
        from ..demo import persona
        return list(persona.QUESTIONS)
    out = list(GENERIC_QUESTIONS)
    top = conn.execute(
        "SELECT e.name FROM entities e JOIN mentions m ON m.entity_id=e.id "
        "JOIN entries en ON en.id=m.entry_id WHERE e.type='person' "
        "AND e.status='active' AND en.status='active' GROUP BY e.id "
        "ORDER BY COUNT(*) DESC, e.name LIMIT 1").fetchone()
    if top:
        out.insert(2, f"when did I last see {top['name'].split()[0]}")
    return out


def status(conn: sqlite3.Connection, cfg: Config) -> dict:
    """What the page needs at load: today, counts, whether this is the demo,
    and the example questions to offer."""
    from .. import db as db_mod
    demo = is_demo(conn, cfg)

    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    counts = {
        "entries": count("SELECT COUNT(*) FROM entries WHERE status='active'"),
        "days": count("SELECT COUNT(DISTINCT journal_date) FROM entries "
                      "WHERE status='active'"),
        "people": count("SELECT COUNT(*) FROM entities WHERE status='active' "
                        "AND type='person'"),
        "entities": count("SELECT COUNT(*) FROM entities WHERE status='active'"),
        "health_records": count("SELECT COUNT(*) FROM health_records "
                                "WHERE status='active'"),
        "open_loops": count("SELECT COUNT(*) FROM entries e JOIN entry_kinds k "
                            "ON k.id=e.kind_id WHERE k.name IN ('task','plan') "
                            "AND e.status='active' AND e.closed_at IS NULL"),
        "observations": 0, "imports": 0, "unresolved_handles": 0,
    }
    if _observed(conn):
        counts["observations"] = count("SELECT COUNT(*) FROM observations "
                                       "WHERE status='active'")
        counts["imports"] = count("SELECT COUNT(*) FROM imports "
                                  "WHERE status='applied'")
        counts["unresolved_handles"] = count(
            "SELECT COUNT(*) FROM entity_handles WHERE entity_id IS NULL "
            "AND ignored=0 AND observation_count > 0")
    span = conn.execute("SELECT MIN(journal_date) AS lo, MAX(journal_date) AS hi "
                        "FROM entries WHERE status='active'").fetchone()
    from .. import features
    return {"today": today(cfg), "timezone": cfg.timezone,
            "schema_version": db_mod.schema_version(conn),
            "counts": counts, "first_entry": span["lo"], "last_entry": span["hi"],
            "demo": demo, "examples": examples(conn, cfg, demo),
            "features": features.listing(cfg)}


# ---------------------------------------------------------------- read-side features
# (lifecrm/features.py) — each read is empty or absent while its feature is
# unchecked, and none of them writes.

def _feature(cfg: Config, key: str) -> bool:
    from .. import features
    try:
        return features.enabled(cfg, key)
    except DomainError:
        return False


def _day_extras(conn: sqlite3.Connection, cfg: Config, journal_date: str,
                observed: list[dict]) -> dict:
    """What the day view adds when the features are checked: earlier years
    on this date, the day's prompts, the trip it belongs to, and the town
    each located record is near."""
    out: dict = {"on_this_day": None, "prompts": None, "trip": None, "near": {}}
    d = date.fromisoformat(journal_date)
    if _feature(cfg, "on_this_day"):
        from .. import memories
        out["on_this_day"] = memories.on_this_day(conn, cfg, d)
    if _feature(cfg, "journal_prompts") and journal_date <= today(cfg) and _observed(conn):
        from .. import prompts
        out["prompts"] = prompts.moments(conn, cfg, journal_date)
    if _feature(cfg, "places") and _observed(conn):
        from .. import places
        for group in observed:
            if group["stream"] not in places.LOCATED_STREAMS:
                continue
            for item in group["items"]:
                row = conn.execute("SELECT value_json FROM observations WHERE uuid=?",
                                   (item["uuid"],)).fetchone()
                det = _loads(row["value_json"]) if row else {}
                label = places.label_for(cfg, det.get("lat"), det.get("lon"))
                if label:
                    out["near"][item["uuid"]] = label
        tl = places.timeline(conn, cfg)
        if tl["ok"]:
            trip = next((t for t in tl["trips"] if t["start"] <= journal_date <= t["end"]),
                        None)
            if trip:
                out["trip"] = {"line": places.describe_trip(trip),
                               "start": trip["start"], "end": trip["end"],
                               "day": (d - date.fromisoformat(trip["start"])).days + 1,
                               "days": trip["days"], "first_uuid": trip["first_uuid"]}
    return out


def trips(conn: sqlite3.Connection, cfg: Config) -> dict:
    """The Places page: home, trips (newest first), countries — from the
    local GeoNames file (lifecrm/places.py)."""
    from .. import places
    if not _feature(cfg, "places"):
        return {"enabled": False, "ok": False, "error": "unchecked in [features]",
                "trips": [], "countries": [], "home": None}
    if not _observed(conn):
        return {"enabled": True, "ok": False, "error": "no observed tier in this database",
                "trips": [], "countries": [], "home": None}
    tl = places.timeline(conn, cfg)
    if not tl["ok"]:
        return {"enabled": True, "ok": False, "error": tl["error"], "trips": [],
                "countries": [], "home": None}
    return {"enabled": True, "ok": True, "error": None, "home": tl["home"],
            "trip_km": tl["trip_km"], "attribution": places.ATTRIBUTION,
            "trips": [{**places.json_safe_trip(t), "line": places.describe_trip(t)}
                      for t in reversed(tl["trips"])],
            "countries": tl["countries"]}


def search(conn: sqlite3.Connection, cfg: Config, q: str, **filters) -> dict:
    """lifecrm/search.py for the Search page."""
    from .. import search as search_mod
    if not _feature(cfg, "search"):
        return {"enabled": False, "query": q, "total": 0, "results": [],
                "facets": {"tier": {}, "stream": {}, "year": {}, "person": []}}
    return {"enabled": True, **search_mod.search(conn, cfg, q, **filters)}


_CONVERSATION_STREAMS = recall.TEXT_STREAMS + recall.EMAIL_STREAMS + recall.CALL_STREAMS


def conversation(conn: sqlite3.Connection, cfg: Config, entity_id: int, *,
                 before: str | None = None, limit: int = 60) -> dict:
    """One thread with a person across every app (Timelinize's view): the
    messages and emails that passed between you and them, and the calls —
    a group thread is not a conversation with each member — oldest first,
    `limit` at a time going back from `before` (a started_at)."""
    if not _feature(cfg, "conversations"):
        return {"enabled": False, "items": [], "older": None}
    row = conn.execute("SELECT id, name FROM entities WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        raise NotFound(f"entity id={entity_id} does not exist")
    out = {"enabled": True, "entity_id": entity_id, "name": row["name"], "items": [],
           "older": None, "streams": []}
    if not _observed(conn):
        return out
    roots = _merged_into_map(conn)
    ids = [entity_id] + [e for e, root in roots.items() if root == entity_id]
    owners = frozenset(_owner_entities(conn, cfg, roots))
    from .. import owner as owner_mod
    owner_handles = owner_mod.identity(conn, cfg).handles
    rows = conn.execute(
        _OBS_SELECT + f"WHERE o.status='active' AND o.stream IN ({_marks(_CONVERSATION_STREAMS)}) "
        "AND o.id IN (SELECT p.observation_id FROM observation_participants p "
        "JOIN entity_handles h ON h.kind=p.handle_kind AND h.value=p.handle_value "
        f"WHERE h.entity_id IN ({_marks(ids)}) AND h.ignored=0) "
        + ("AND o.started_at < ? " if before else "")
        + "ORDER BY o.started_at DESC, o.id DESC LIMIT ?",
        (*_CONVERSATION_STREAMS, *ids, *([before] if before else []), limit * 3 + 1)).fetchall()
    parts = _participants(conn, [r["id"] for r in rows])
    kept = []
    for r in rows:
        others = {(p["kind"], (p["value"] or "").lower()) for p in parts.get(r["id"], [])
                  if p["entity_id"] not in owners and p["entity_id"] not in ids
                  and (p["kind"], (p["value"] or "").lower()) not in owner_handles
                  and not (p["display_name"] or "").lower().startswith("you (")}
        if others:
            continue                     # a group thread or a Cc: not between the two of you
        kept.append(r)
        if len(kept) > limit:
            break
    more = len(kept) > limit
    kept = kept[:limit]
    out["items"] = list(reversed(_obs_items(conn, cfg, kept)))
    out["older"] = kept[-1]["started_at"] if more and kept else None
    out["streams"] = sorted({r["stream"] for r in kept})
    return out


def _person_extras(conn: sqlite3.Connection, cfg: Config, entity_id: int,
                   family: list[int]) -> dict:
    out: dict = {"gift_ideas": [], "job_changes": [], "links": [],
                 "conversation": _feature(cfg, "conversations")}
    if _feature(cfg, "gift_ideas"):
        from .. import gifts
        out["gift_ideas"] = gifts.ideas_for(conn, family, include_given=True)
    if _observed(conn) and _feature(cfg, "job_changes"):
        from .. import jobchanges
        out["job_changes"] = jobchanges.for_person(conn, cfg, entity_id)
    if _observed(conn) and _feature(cfg, "link_memory"):
        from .. import links
        out["links"] = links.from_person(conn, cfg, entity_id, limit=12)
    return out

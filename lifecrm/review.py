"""The weekly review: a deterministic evidence pack for one period.

`lifecrm ask` answers one question; this answers "what happened this week,
and what does it add up to?" It is the input to the /review-week and /advise
skills, and it keeps ask's split for ask's reason: everything here is SQL
over a read-only connection, every item carries a citable id, and nothing
here interprets. The brief a skill writes from the pack is the
interpretation — derived, regenerable from the pack, and only ever stored
through the normal review gate as a cited reflection, never as a fact.

Ids are pack-local and mean one thing each:

    D#  a journal day (summary, mood, energy, sleep as said)
    E#  a confirmed entry (uuid)
    M#  a journal metric over the period, with the prior period beside it
    H#  a sensor/app health metric over the period, likewise
    O#  one imported observation (uuid), or a count over observations
        (a stream, a person's messages, a merchant), naming the imports

Determinism is the contract: the same database and the same arguments give
the same pack, byte for byte. That is what lets `review --check BRIEF`
re-verify a brief's citations after the fact, and what makes the scheduled
weekly run a no-op when nothing changed.

The two tiers stay distinct here as everywhere. Entries and measurements are
what the owner confirmed; observations and health_records were imported in
bulk and never reviewed, and every item says which (`tier`). A person is only
named through a handle the owner linked — an unresolved handle appears under
the name its source gave it, marked unresolved, and is never guessed onto an
entity. "You" are recognized the way the connectors mark the owner: the
sender of outgoing records, and the account an import names.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from . import db as db_mod
from .config import Config
from .domain import days as days_mod
from .domain.util import DomainError
from .recall import (HEALTH_METRICS, JOURNAL_METRICS, _metric_series, _round,
                     fmt_value, local_today)

PACK_VERSION = 1

LENSES = ("week", "health", "career", "relationships", "finance", "all")

SECTIONS = ("days", "entries", "loops", "journal_metrics", "health",
            "observations", "titles", "relationships", "career", "finance")

# What each lens is about. The week is the overview; an advisor lens trades
# breadth for its own section and the entries that bear on it.
LENS_SECTIONS: dict[str, tuple[str, ...]] = {
    "week": ("days", "entries", "loops", "journal_metrics", "health",
             "observations", "titles"),
    "health": ("days", "entries", "loops", "journal_metrics", "health"),
    "career": ("entries", "loops", "career", "titles"),
    "relationships": ("entries", "loops", "relationships", "observations"),
    "finance": ("entries", "loops", "finance"),
    "all": SECTIONS,
}

# A focused lens keeps the entries with any of these categories (primary or
# secondary). Relationships also keeps any entry that names a person, and
# finance any entry that carries a money measurement.
LENS_CATEGORIES: dict[str, frozenset[str]] = {
    "health": frozenset({"health", "exercise", "sleep", "food"}),
    "career": frozenset({"work", "learning"}),
    "relationships": frozenset({"social"}),
    "finance": frozenset({"purchase"}),
}

# Fixed stream names (a global namespace shared by every connector).
MESSAGE_STREAMS = ("imessage", "sms", "rcs", "android_sms", "android_mms",
                   "whatsapp")
CALL_STREAMS = ("ios_call", "android_call")
CALENDAR_STREAMS = ("calendar_event",)
AI_STREAMS = ("claude_chat", "chatgpt_chat")
NOTE_STREAMS = ("note",)
TXN_STREAMS = ("txn",)
CONTACT_CHANNELS = (("messages", MESSAGE_STREAMS), ("calls", CALL_STREAMS),
                    ("meetings", CALENDAR_STREAMS))
# A card or a place can sit on a contact stream (a meeting's room, a paid
# call) but is never a person you were in touch with.
NON_PERSON_KINDS = frozenset({"card", "place_id"})

HEALTH_TRENDS = ("sleep_session", "steps", "exercise_session", "weight",
                 "resting_heart_rate")
JOURNAL_TRENDS = ("mood", "energy", "sleep_hours", "sleep_quality")
SHORT_NIGHT_H = 6.5
LOOP_KINDS = ("task", "plan")

# A saved brief is an entry like any other; this mark keeps it out of the
# next pack's evidence, so an interpretation is never re-read as a fact.
BRIEF_MARK = "lifecrm review"
_NOT_BRIEF = ("(data IS NULL OR NOT json_valid(data) "
              f"OR json_extract(data, '$.derived_from') IS NOT '{BRIEF_MARK}')")

DEFAULT_QUIET_WEEKS = 3
TOP_PEOPLE = 8
MAX_ENTRIES = 200
MAX_LOOPS = 50
MAX_PEOPLE = 40
MAX_QUIET = 12
MAX_MERCHANTS = 30
MAX_TITLES = 50
MAX_PERIOD_DAYS = 3660
EXCERPT = 280
# A calendar block this long is an all-day marker, not a meeting.
MEETING_MAX_MIN = 12 * 60


# ---------------------------------------------------------------- period

_WEEK_RE = re.compile(r"^(\d{4})-?[Ww](\d{1,2})$")


def _parse_day(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise DomainError(f"{flag} '{value}' is not a date (expected YYYY-MM-DD)") \
            from None


def resolve_period(cfg: Config, *, week: str | None = None,
                   start: str | None = None, end: str | None = None,
                   days: int | None = None, today: date | None = None) -> dict:
    """The period a pack covers, and the equally long period before it.

    Default: the last FULL week, Monday to Sunday, in the configured timezone
    (with the rollover hour) — on a Wednesday that is the week that ended
    three days ago, never the half-week in progress. `--days N` is the N days
    ending today; `--start` alone runs through today.
    """
    today = today or local_today(cfg)
    chosen = [flag for flag, v in (("--week", week), ("--start/--end", start or end),
                                   ("--days", days)) if v not in (None, "")]
    if len(chosen) > 1:
        raise DomainError(f"give one of --week, --start/--end or --days, "
                          f"not {' and '.join(chosen)}")
    label = None
    if week:
        m = _WEEK_RE.match(week.strip())
        if not m:
            raise DomainError(f"--week '{week}' is not an ISO week (expected "
                              f"YYYY-Www, e.g. 2026-W42)")
        try:
            s = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            raise DomainError(f"--week '{week}': {m.group(1)} has no week "
                              f"{int(m.group(2))}") from None
        e, kind = s + timedelta(days=6), "week"
    elif start or end:
        if not start:
            raise DomainError("--end needs --start")
        s = _parse_day(start, "--start")
        e = _parse_day(end, "--end") if end else today
        if e < s:
            raise DomainError(f"--end {e} is before --start {s}")
        kind = "range"
    elif days is not None:
        if not 1 <= days <= MAX_PERIOD_DAYS:
            raise DomainError(f"--days must be 1-{MAX_PERIOD_DAYS}, got {days}")
        s, e, kind = today - timedelta(days=days - 1), today, "days"
        label = f"last {days} day{'s' if days != 1 else ''}"
    else:
        monday = today - timedelta(days=today.weekday())
        s, e, kind = monday - timedelta(days=7), monday - timedelta(days=1), "week"
    n = (e - s).days + 1
    if n > MAX_PERIOD_DAYS:
        raise DomainError(f"period {s} to {e} is {n} days — at most "
                          f"{MAX_PERIOD_DAYS} per pack")
    if kind == "week":
        y, w, _ = s.isocalendar()
        slug = f"{y}-W{w:02d}"
        label = f"week {slug}"
    else:
        slug = f"{s.isoformat()}_{e.isoformat()}"
        label = label or f"{s.isoformat()} to {e.isoformat()}"
    prior_end = s - timedelta(days=1)
    prior_start = prior_end - timedelta(days=n - 1)
    return {"start": s.isoformat(), "end": e.isoformat(), "days": n,
            "kind": kind, "label": label, "slug": slug,
            "as_of": today.isoformat(),
            "prior": {"start": prior_start.isoformat(),
                      "end": prior_end.isoformat(), "days": n}}


# ---------------------------------------------------------------- ids

class _Ids:
    """Pack-local citation ids, numbered in build order and memoized on what
    they point at — an entry cited from three sections is one id."""

    def __init__(self) -> None:
        self.index: dict[str, dict] = {}
        self._by_key: dict[tuple, str] = {}
        self._next: dict[str, int] = {}

    def cite(self, prefix: str, key, item: dict) -> str:
        k = (prefix, key)
        if k in self._by_key:
            return self._by_key[k]
        n = self._next.get(prefix, 0) + 1
        self._next[prefix] = n
        cid = f"{prefix}{n}"
        self._by_key[k] = cid
        self.index[cid] = {"id": cid, **item}
        return cid


@dataclass
class _Ctx:
    conn: sqlite3.Connection
    cfg: Config
    period: dict
    lens: str
    ids: _Ids
    observed: bool
    quiet_weeks: int
    entity_names: dict[int, str] = field(default_factory=dict)
    entity_types: dict[int, str] = field(default_factory=dict)
    merged_into: dict[int, int] = field(default_factory=dict)
    owner_handles: set[tuple[str, str]] = field(default_factory=set)
    owner_entities: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _excerpt(text: str | None, limit: int = EXCERPT) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def _delta(cur, prior):
    if cur is None or prior is None:
        return None
    return round(cur - prior, 2)


def _local_day(cfg: Config, stamp: str | None) -> str | None:
    """A closed_at stamp as a local journal date: UTC datetimes go through
    the timezone and rollover like everything else; a bare date is taken as
    the owner wrote it."""
    if not stamp:
        return None
    if len(stamp) == 10:
        return stamp
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp[:10]
    if dt.tzinfo is None:
        return stamp[:10]
    return days_mod.journal_date_for(dt, cfg.timezone, cfg.day_rollover_hour)


# ---------------------------------------------------------------- people

def _load_people(ctx: _Ctx) -> None:
    for r in ctx.conn.execute("SELECT id, name, type, merged_into FROM entities"):
        ctx.entity_names[r["id"]] = r["name"]
        ctx.entity_types[r["id"]] = r["type"]
        if r["merged_into"] is not None:
            ctx.merged_into[r["id"]] = r["merged_into"]
    if not ctx.observed:
        return
    # The owner, by lifecrm/owner.py's rule (the one `ask` and the web UI
    # use): the [owner] handles in config.toml, whoever sends the outgoing
    # records, and any handle an import names as its account. Their linked
    # entity is "you", not a contact.
    from . import owner as owner_mod
    ident = owner_mod.identity(ctx.conn, ctx.cfg)
    ctx.owner_handles |= set(ident.handles)
    ctx.owner_entities |= {_final_entity(ctx, e) for e in ident.entities}


def _final_entity(ctx: _Ctx, entity_id: int) -> int:
    """Follow merges to the surviving entity: `entities merge` moves
    mentions but leaves handles on the merged row."""
    seen = set()
    while entity_id in ctx.merged_into and entity_id not in seen:
        seen.add(entity_id)
        entity_id = ctx.merged_into[entity_id]
    return entity_id


def _person(ctx: _Ctx, kind: str, value: str, entity_id: int | None,
            display_name: str | None) -> tuple[tuple, dict]:
    if entity_id is not None:
        eid = _final_entity(ctx, entity_id)
        return ("entity", eid), {"name": ctx.entity_names.get(eid, f"entity {eid}"),
                                 "entity_id": eid, "resolved": True}
    handle = f"{kind}:{value}"
    return ("handle", kind, value.lower()), {
        "name": display_name or handle, "entity_id": None, "resolved": False,
        "handle": handle}


# ---------------------------------------------------------------- entries

def _is_brief(data: str | None) -> bool:
    if not data:
        return False
    try:
        parsed = json.loads(data)
    except ValueError:
        return False
    return isinstance(parsed, dict) and parsed.get("derived_from") == BRIEF_MARK


def _load_entries(ctx: _Ctx, where: str, args: tuple, *,
                  briefs: bool = False) -> list[dict]:
    """Active entries matching `where`, with categories, people and money
    attached. Saved review briefs are left out unless `briefs` asks for
    exactly them."""
    rows = [r for r in ctx.conn.execute(
        "SELECT e.id, e.uuid, e.journal_date, k.name AS kind, e.title, e.body, "
        "e.time_of_day, e.due_at, e.closed_at, e.closure, e.confirmed_at, e.data "
        "FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        f"WHERE e.status = 'active' AND ({where}) "
        "ORDER BY e.journal_date, e.id", args)
        if _is_brief(r["data"]) == briefs]
    by_id = {r["id"]: {
        "uuid": r["uuid"], "date": r["journal_date"], "kind": r["kind"],
        "title": r["title"], "body": _excerpt(r["body"]),
        "time_of_day": r["time_of_day"], "primary": None, "categories": [],
        "due": r["due_at"][:10] if r["due_at"] else None,
        "closure": r["closure"], "closed_on": _local_day(ctx.cfg, r["closed_at"]),
        "confirmed": r["confirmed_at"] is not None, "people": [], "money": [],
        "_data": r["data"]} for r in rows}
    ids = list(by_id)
    for part in _chunks(ids):
        marks = ",".join("?" * len(part))
        for c in ctx.conn.execute(
                "SELECT ec.entry_id, c.name, ec.is_primary FROM entry_categories ec "
                "JOIN categories c ON c.id = ec.category_id "
                f"WHERE ec.entry_id IN ({marks}) "
                "ORDER BY ec.entry_id, ec.is_primary DESC, c.name", part):
            e = by_id[c["entry_id"]]
            e["categories"].append(c["name"])
            if c["is_primary"]:
                e["primary"] = c["name"]
        for m in ctx.conn.execute(
                "SELECT m.entry_id, en.id, en.name, m.role FROM mentions m "
                "JOIN entities en ON en.id = m.entity_id "
                f"WHERE en.type = 'person' AND m.entry_id IN ({marks}) "
                "ORDER BY m.entry_id, en.name, m.role", part):
            people = by_id[m["entry_id"]]["people"]
            if not any(p["entity_id"] == m["id"] for p in people):
                people.append({"name": m["name"], "entity_id": m["id"],
                               "role": m["role"]})
            elif m["role"] == "participant":
                next(p for p in people if p["entity_id"] == m["id"])["role"] = \
                    "participant"
        for m in ctx.conn.execute(
                "SELECT entry_id, numeric_value, unit FROM measurements "
                f"WHERE status = 'active' AND metric = 'money' "
                f"AND numeric_value IS NOT NULL AND entry_id IN ({marks}) "
                "ORDER BY entry_id, id", part):
            by_id[m["entry_id"]]["money"].append(
                {"amount": round(m["numeric_value"], 2), "unit": m["unit"]})
    return list(by_id.values())


def _public(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")}


def _cite_entry(ctx: _Ctx, e: dict) -> str:
    return ctx.ids.cite("E", e["uuid"], {"type": "entry", "tier": "confirmed",
                                         **_public(e)})


def _compact(ctx: _Ctx, e: dict) -> dict:
    """An entry as a list item elsewhere in the pack: its id plus enough to
    read the line without the index."""
    out = {"id": _cite_entry(ctx, e), "date": e["date"], "kind": e["kind"],
           "title": e["title"]}
    for k in ("due", "closure", "closed_on"):
        if e.get(k):
            out[k] = e[k]
    return out


def _lens_keeps(lens: str, e: dict) -> bool:
    if lens not in LENS_CATEGORIES:
        return True
    if set(e["categories"]) & LENS_CATEGORIES[lens]:
        return True
    if lens == "relationships" and e["people"]:
        return True
    if lens == "finance" and e["money"]:
        return True
    return False


def _period_entries(ctx: _Ctx) -> list[dict]:
    p = ctx.period
    return _load_entries(ctx, "e.journal_date BETWEEN ? AND ?",
                         (p["start"], p["end"]))


# ---------------------------------------------------------------- sections

def _section_days(ctx: _Ctx) -> list[dict]:
    p = ctx.period
    summaries = {r["journal_date"]: r["summary"] for r in ctx.conn.execute(
        "SELECT journal_date, summary FROM days WHERE journal_date BETWEEN ? AND ? "
        "AND summary IS NOT NULL AND summary <> ''", (p["start"], p["end"]))}
    said: dict[str, dict] = {}
    for r in ctx.conn.execute(
            "SELECT journal_date, metric, "
            "AVG(COALESCE(normalized_value, numeric_value)) AS v FROM measurements "
            "WHERE status = 'active' AND journal_date BETWEEN ? AND ? "
            "AND metric IN ('mood', 'energy', 'sleep_hours') "
            "AND COALESCE(normalized_value, numeric_value) IS NOT NULL "
            "GROUP BY journal_date, metric", (p["start"], p["end"])):
        said.setdefault(r["journal_date"], {})[r["metric"]] = round(r["v"], 2)
    counts = {r["journal_date"]: r["n"] for r in ctx.conn.execute(
        "SELECT journal_date, COUNT(*) AS n FROM entries WHERE status = 'active' "
        f"AND journal_date BETWEEN ? AND ? AND {_NOT_BRIEF} GROUP BY journal_date",
        (p["start"], p["end"]))}
    out = []
    for d in sorted(set(summaries) | set(said) | set(counts)):
        item = {"date": d, "summary": summaries.get(d),
                "mood": said.get(d, {}).get("mood"),
                "energy": said.get(d, {}).get("energy"),
                "sleep_hours": said.get(d, {}).get("sleep_hours"),
                "entries": counts.get(d, 0)}
        cid = ctx.ids.cite("D", d, {"type": "day", "tier": "confirmed", **item})
        out.append({"id": cid, **item})
    return out


def _section_entries(ctx: _Ctx, entries: list[dict]) -> dict:
    by_cat: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for e in entries:
        by_cat[e["primary"] or "other"] = by_cat.get(e["primary"] or "other", 0) + 1
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
    items = []
    for e in entries[:MAX_ENTRIES]:
        cid = _cite_entry(ctx, e)
        items.append({"id": cid, **_public(e)})
    out = {"total": len(entries),
           "by_category": dict(sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))),
           "by_kind": dict(sorted(by_kind.items(), key=lambda kv: (-kv[1], kv[0]))),
           "items": items}
    if ctx.lens in LENS_CATEGORIES:
        out["filter"] = sorted(LENS_CATEGORIES[ctx.lens])
    if len(entries) > MAX_ENTRIES:
        out["truncated"] = len(entries) - MAX_ENTRIES
    return out


def _section_loops(ctx: _Ctx) -> dict:
    p = ctx.period
    start, end = p["start"], p["end"]
    # closed_at is UTC; a day of margin, then exact local dates in Python.
    margin = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
    marks = ",".join("?" * len(LOOP_KINDS))
    loops = [e for e in _load_entries(
        ctx, f"k.name IN ({marks}) AND e.journal_date <= ? "
             f"AND (e.closed_at IS NULL OR substr(e.closed_at, 1, 10) >= ?)",
        (*LOOP_KINDS, end, margin)) if _lens_keeps(ctx.lens, e)]
    as_of = date.fromisoformat(p["as_of"])
    # A period that has fully elapsed counts its last day as passed: a task
    # due Sunday and still open after Sunday is overdue.
    cutoff = end if date.fromisoformat(end) >= as_of else \
        (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    opened, closed, still_open, overdue = [], [], [], []
    for e in loops:
        closed_on = e["closed_on"]
        if start <= e["date"] <= end:
            opened.append(e)
        if closed_on and start <= closed_on <= end:
            closed.append(e)
        if closed_on is None or closed_on > end:
            still_open.append(e)
            if e["due"] and e["due"] < cutoff:
                overdue.append(e)
    still_open.sort(key=lambda e: (e["due"] is None, e["due"] or "", e["date"]))
    out = {"opened": [_compact(ctx, e) for e in opened],
           "closed": [_compact(ctx, e) for e in closed],
           "open": [_compact(ctx, e) for e in still_open[:MAX_LOOPS]],
           "overdue": [_compact(ctx, e) for e in overdue],
           "open_total": len(still_open)}
    if ctx.lens in LENS_CATEGORIES:
        out["filter"] = sorted(LENS_CATEGORIES[ctx.lens])
    return out


def _trend(ctx: _Ctx, *, table: str, metric: str, spec: dict) -> tuple[dict | None, dict | None]:
    p = ctx.period
    cur = _metric_series(ctx.conn, table=table, metric=metric, spec=spec,
                         window={"start": p["start"], "end": p["end"]})
    prior = _metric_series(ctx.conn, table=table, metric=metric, spec=spec,
                           window={"start": p["prior"]["start"],
                                   "end": p["prior"]["end"]})
    return cur, prior


def _trend_item(kind: str, tier: str, cur: dict, prior: dict | None) -> dict:
    item = {"type": kind, "tier": tier, "metric": cur["metric"],
            "label": cur["label"], "unit": cur["unit"], "origin": cur["origin"],
            "per_day": cur["per_day_agg"], "days": cur["days"],
            "mean": cur["mean"], "min": cur["min"], "min_date": cur["min_date"],
            "max": cur["max"], "max_date": cur["max_date"],
            "latest": cur["latest"], "latest_date": cur["latest_date"],
            "series": [{"date": s["date"], "value": s["value"]} for s in cur["series"]],
            "prior": ({"days": prior["days"], "mean": prior["mean"]} if prior else None),
            "delta": _delta(cur["mean"], prior["mean"] if prior else None)}
    if cur.get("source_files"):
        item["source_files"] = cur["source_files"]
    return item


def _section_journal_metrics(ctx: _Ctx) -> dict:
    items, no_data = [], []
    for metric in JOURNAL_TRENDS:
        spec = JOURNAL_METRICS[metric]
        cur, prior = _trend(ctx, table="measurements", metric=metric, spec=spec)
        if not cur:
            no_data.append(spec["label"])
            continue
        item = _trend_item("journal_metric", "confirmed", cur, prior)
        cid = ctx.ids.cite("M", metric, item)
        items.append({"id": cid, **item})
    return {"metrics": items, "no_data": no_data}


def _section_health(ctx: _Ctx, entries: list[dict]) -> dict:
    items, no_data = [], []
    for metric in HEALTH_TRENDS:
        spec = HEALTH_METRICS[metric]
        cur, prior = _trend(ctx, table="health_records", metric=metric, spec=spec)
        if not cur:
            last = ctx.conn.execute(
                "SELECT MAX(journal_date) AS d FROM health_records "
                "WHERE metric = ? AND status = 'active'", (metric,)).fetchone()["d"]
            no_data.append({"metric": metric, "label": spec["label"],
                            "last_record": last})
            continue
        item = _trend_item("health_metric", "observed", cur, prior)
        if metric == "sleep_session":
            item["short_nights"] = sum(1 for s in cur["series"]
                                       if s["value"] < SHORT_NIGHT_H)
            item["short_nights_prior"] = (sum(1 for s in prior["series"]
                                              if s["value"] < SHORT_NIGHT_H)
                                          if prior else None)
            item["short_night_hours"] = SHORT_NIGHT_H
        elif metric == "exercise_session":
            item["sessions"] = sum(s["records"] for s in cur["series"])
            item["minutes"] = cur["total"]
            item["sessions_prior"] = (sum(s["records"] for s in prior["series"])
                                      if prior else None)
        elif metric == "weight" and cur["days"] >= 2:
            item["change_in_period"] = round(cur["series"][-1]["value"]
                                             - cur["series"][0]["value"], 2)
        cid = ctx.ids.cite("H", metric, item)
        items.append({"id": cid, **item})
    exercise = [e for e in entries if "exercise" in e["categories"]]
    return {"metrics": items, "no_data": no_data,
            "exercise_entries": [_compact(ctx, e) for e in exercise]}


def _stream_counts(ctx: _Ctx, start: str, end: str) -> dict[str, dict]:
    out = {}
    for r in ctx.conn.execute(
            "SELECT o.stream, COUNT(*) AS n, MIN(o.journal_date) AS first, "
            "MAX(o.journal_date) AS last, GROUP_CONCAT(DISTINCT i.uuid) AS imports "
            "FROM observations o JOIN imports i ON i.id = o.import_id "
            "WHERE o.status = 'active' AND o.journal_date BETWEEN ? AND ? "
            "GROUP BY o.stream", (start, end)):
        out[r["stream"]] = {"count": r["n"], "first_date": r["first"],
                            "last_date": r["last"],
                            "imports": sorted(set((r["imports"] or "").split(",")) - {""})}
    return out


def _contacts(ctx: _Ctx) -> dict[tuple, dict]:
    """Who the period's messages, calls and meetings were with, per person.

    Counted from the other side's role: an incoming record counts its sender
    (a group message from Jake is contact with Jake, not with everyone else
    in the room), an outgoing one its recipients, an undirected one (a
    meeting) everyone but the location."""
    p = ctx.period
    streams = [s for _, group in CONTACT_CHANNELS for s in group]
    channel_of = {s: ch for ch, group in CONTACT_CHANNELS for s in group}
    marks = ",".join("?" * len(streams))
    people: dict[tuple, dict] = {}
    for r in ctx.conn.execute(
            "SELECT o.id, o.uuid, o.stream, o.journal_date, o.started_at, "
            "o.ended_at, o.direction, o.value, o.unit, p.role, p.handle_kind, "
            "p.handle_value, h.entity_id, h.display_name, h.ignored "
            "FROM observations o "
            "JOIN observation_participants p ON p.observation_id = o.id "
            "JOIN entity_handles h ON h.kind = p.handle_kind "
            "AND h.value = p.handle_value "
            f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
            "AND o.journal_date BETWEEN ? AND ? "
            "ORDER BY o.started_at, o.id, p.handle_kind, p.handle_value",
            (*streams, p["start"], p["end"])):
        if r["ignored"] or r["role"] == "location" or \
                r["handle_kind"] in NON_PERSON_KINDS:
            continue
        if r["direction"] == "in" and r["role"] == "recipient":
            continue
        if r["direction"] == "out" and r["role"] == "sender":
            continue
        if (r["handle_kind"], r["handle_value"].lower()) in ctx.owner_handles:
            continue
        key, info = _person(ctx, r["handle_kind"], r["handle_value"],
                            r["entity_id"], r["display_name"])
        if info["entity_id"] in ctx.owner_entities:
            continue
        person = people.setdefault(key, {**info, "channels": {}})
        ch = person["channels"].setdefault(channel_of[r["stream"]], {
            "obs": [], "seen": set(), "streams": set(), "in": 0, "out": 0,
            "minutes": 0.0, "timed": 0})
        if r["id"] in ch["seen"]:
            continue
        ch["seen"].add(r["id"])
        ch["obs"].append({"id": r["id"], "uuid": r["uuid"], "date": r["journal_date"]})
        ch["streams"].add(r["stream"])
        if r["direction"] in ("in", "out"):
            ch[r["direction"]] += 1
        minutes = _minutes(r)
        if minutes is not None:
            ch["minutes"] += minutes
            ch["timed"] += 1
    return people


def _minutes(r) -> float | None:
    """A call's or meeting's length: its own time range when it has one,
    else a duration value in a unit we can read."""
    if r["ended_at"] and r["started_at"]:
        try:
            a = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(r["ended_at"].replace("Z", "+00:00"))
        except ValueError:
            return None
        m = (b - a).total_seconds() / 60
        return m if 0 <= m < MEETING_MAX_MIN else None
    unit = (r["unit"] or "").lower()
    if r["value"] is None:
        return None
    if unit in ("s", "sec", "secs", "seconds"):
        return r["value"] / 60
    if unit in ("min", "mins", "minutes"):
        return float(r["value"])
    return None


def _person_rows(ctx: _Ctx, entries: list[dict]) -> list[dict]:
    """One row per person the period touched: entries that name them
    (confirmed) beside the messages, calls and meetings (observed)."""
    people = _contacts(ctx) if ctx.observed else {}
    for e in entries:
        for person in e["people"]:
            eid = person["entity_id"]
            if eid in ctx.owner_entities:
                continue
            row = people.setdefault(("entity", eid), {
                "name": person["name"], "entity_id": eid, "resolved": True,
                "channels": {}})
            row.setdefault("_entries", []).append((e, person["role"]))

    def total(person: dict) -> int:
        return len(person.get("_entries", [])) + sum(
            len(ch["obs"]) for ch in person["channels"].values())

    # Ranked before any id is handed out, so the ids follow the ranking.
    ranked = sorted(people.items(), key=lambda kv: (
        -total(kv[1]), kv[1]["name"].lower(), kv[1].get("handle") or ""))
    rows = []
    for key, person in ranked:
        row = {k: person[k] for k in ("name", "entity_id", "resolved")}
        if "handle" in person:
            row["handle"] = person["handle"]
        cited = person.get("_entries", [])
        row["entries"] = [_cite_entry(ctx, e) for e, _ in cited]
        row["saw"] = sum(1 for _, role in cited if role == "participant")
        for channel, _ in CONTACT_CHANNELS:
            ch = person["channels"].get(channel)
            if not ch:
                row[channel] = None
                continue
            obs = ch["obs"]
            item = {"type": "observation_count", "tier": "observed",
                    "what": f"{channel} with {row['name']}",
                    "streams": sorted(ch["streams"]), "count": len(obs),
                    "in": ch["in"], "out": ch["out"],
                    "first_date": obs[0]["date"], "last_date": obs[-1]["date"],
                    "first_uuid": obs[0]["uuid"], "last_uuid": obs[-1]["uuid"]}
            if ch["timed"]:
                item["minutes"] = round(ch["minutes"], 1)
            cid = ctx.ids.cite("O", ("contact", key, channel), item)
            row[channel] = {"id": cid, "count": len(obs), "in": ch["in"],
                            "out": ch["out"], **({"minutes": item["minutes"]}
                                                 if "minutes" in item else {})}
        row["total"] = total(person)
        rows.append(row)
    return rows


def _section_observations(ctx: _Ctx, people) -> dict:
    """Counts per stream, then the period's most-contacted people (`people`
    is called after the streams are cited, so the ids read in order)."""
    p = ctx.period
    cur = _stream_counts(ctx, p["start"], p["end"])
    prior = _stream_counts(ctx, p["prior"]["start"], p["prior"]["end"])
    streams = []
    for stream in sorted(cur, key=lambda s: (-cur[s]["count"], s)):
        item = {"type": "observation_count", "tier": "observed",
                "what": f"{stream} records", "stream": stream, **cur[stream],
                "prior_count": prior.get(stream, {}).get("count", 0)}
        cid = ctx.ids.cite("O", ("stream", stream), item)
        streams.append({"id": cid, "stream": stream, "count": item["count"],
                        "prior_count": item["prior_count"]})
    return {"total": sum(c["count"] for c in cur.values()),
            "prior_total": sum(c["count"] for c in prior.values()),
            "streams": streams, "top_people": people()[:TOP_PEOPLE]}


def _cite_observation(ctx: _Ctx, r, **extra) -> str:
    item = {"type": "observation", "tier": "observed", "uuid": r["uuid"],
            "stream": r["stream"], "date": r["journal_date"],
            "occurred_at": r["started_at"], "direction": r["direction"],
            "title": r["title"], "import_uuid": r["import_uuid"], **extra}
    return ctx.ids.cite("O", ("obs", r["uuid"]), item)


def _obs_rows(ctx: _Ctx, streams: tuple[str, ...], start: str, end: str) -> list:
    marks = ",".join("?" * len(streams))
    return ctx.conn.execute(
        "SELECT o.id, o.uuid, o.stream, o.journal_date, o.started_at, o.ended_at, "
        "o.direction, o.title, o.body, o.value, o.unit, o.value_json, "
        "i.uuid AS import_uuid FROM observations o "
        "JOIN imports i ON i.id = o.import_id "
        f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
        "AND o.journal_date BETWEEN ? AND ? ORDER BY o.started_at, o.id",
        (*streams, start, end)).fetchall()


def _detail(r) -> dict:
    try:
        parsed = json.loads(r["value_json"]) if r["value_json"] else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _section_titles(ctx: _Ctx) -> dict:
    p = ctx.period
    chats: dict[tuple, dict] = {}
    for r in _obs_rows(ctx, AI_STREAMS, p["start"], p["end"]):
        conv = _detail(r).get("conversation_id") or r["title"] or r["uuid"]
        key = (r["stream"], conv)
        if key not in chats:
            chats[key] = {"row": r, "turns": 0}
        chats[key]["turns"] += 1
    ai = []
    for key, c in list(chats.items())[:MAX_TITLES]:
        r = c["row"]
        title = r["title"] or "(untitled conversation)"
        cid = _cite_observation(ctx, r, conversation=key[1], turns=c["turns"])
        ai.append({"id": cid, "stream": r["stream"], "date": r["journal_date"],
                   "title": title, "turns": c["turns"]})
    notes = []
    for r in _obs_rows(ctx, NOTE_STREAMS, p["start"], p["end"])[:MAX_TITLES]:
        title = r["title"] or (_excerpt((r["body"] or "").split("\n")[0], 80)
                               or "(untitled note)")
        cid = _cite_observation(ctx, r)
        notes.append({"id": cid, "date": r["journal_date"], "title": title})
    return {"ai_chats": ai, "ai_chats_total": len(chats), "notes": notes}


def _last_contacts(ctx: _Ctx) -> dict[int, dict]:
    """Every person's last contact up to the period's end, either tier."""
    end = ctx.period["end"]
    last: dict[int, dict] = {}

    def offer(eid: int, d: str, via: str, n: int) -> None:
        if eid in ctx.owner_entities or ctx.entity_types.get(eid) != "person":
            return
        cur = last.setdefault(eid, {"date": d, "via": via, "history": 0})
        cur["history"] += n
        if d > cur["date"]:
            cur.update(date=d, via=via)

    for r in ctx.conn.execute(
            "SELECT m.entity_id, MAX(e.journal_date) AS d, "
            "COUNT(DISTINCT e.id) AS n FROM mentions m "
            "JOIN entries e ON e.id = m.entry_id WHERE e.status = 'active' "
            "AND e.journal_date <= ? GROUP BY m.entity_id", (end,)):
        offer(r["entity_id"], r["d"], "entry", r["n"])
    if ctx.observed:
        streams = [s for _, group in CONTACT_CHANNELS for s in group]
        marks = ",".join("?" * len(streams))
        for r in ctx.conn.execute(
                "SELECT h.kind, h.value, h.entity_id, MAX(o.journal_date) AS d, "
                "COUNT(DISTINCT o.id) AS n FROM entity_handles h "
                "JOIN observation_participants p ON p.handle_kind = h.kind "
                "AND p.handle_value = h.value "
                "JOIN observations o ON o.id = p.observation_id "
                f"WHERE h.entity_id IS NOT NULL AND h.ignored = 0 "
                f"AND o.status = 'active' AND o.stream IN ({marks}) "
                "AND o.journal_date <= ? GROUP BY h.kind, h.value, h.entity_id",
                (*streams, end)):
            if (r["kind"], r["value"].lower()) in ctx.owner_handles or \
                    r["kind"] in NON_PERSON_KINDS:
                continue
            offer(_final_entity(ctx, r["entity_id"]), r["d"], "observed", r["n"])
    return last


def _section_relationships(ctx: _Ctx, people: list[dict]) -> dict:
    end = date.fromisoformat(ctx.period["end"])
    quiet = []
    for eid, info in _last_contacts(ctx).items():
        since = (end - date.fromisoformat(info["date"])).days
        if since >= ctx.quiet_weeks * 7:
            quiet.append({"name": ctx.entity_names.get(eid, f"entity {eid}"),
                          "entity_id": eid, "last_contact": info["date"],
                          "days_since": since, "via": info["via"],
                          "history": info["history"]})
    quiet.sort(key=lambda q: (-q["history"], q["days_since"], q["name"].lower(),
                              q["entity_id"]))
    quiet = quiet[:MAX_QUIET]
    for q in quiet:
        q["id"] = _cite_last_contact(ctx, q)
    return {"people": people[:MAX_PEOPLE], "people_total": len(people),
            "quiet": quiet, "quiet_weeks": ctx.quiet_weeks}


def _cite_last_contact(ctx: _Ctx, q: dict) -> str | None:
    """The record behind a 'last contact' date, so the claim is citable."""
    if q["via"] == "entry":
        found = _load_entries(
            ctx, "e.journal_date = ? AND e.id IN (SELECT entry_id FROM mentions "
                 "WHERE entity_id = ?)", (q["last_contact"], q["entity_id"]))
        return _cite_entry(ctx, found[-1]) if found else None
    streams = [s for _, group in CONTACT_CHANNELS for s in group]
    marks = ",".join("?" * len(streams))
    merged = [eid for eid in ctx.entity_names
              if _final_entity(ctx, eid) == q["entity_id"]]
    emarks = ",".join("?" * len(merged))
    r = ctx.conn.execute(
        "SELECT o.uuid, o.stream, o.journal_date, o.started_at, o.direction, "
        "o.title, i.uuid AS import_uuid FROM observations o "
        "JOIN imports i ON i.id = o.import_id "
        "JOIN observation_participants p ON p.observation_id = o.id "
        "JOIN entity_handles h ON h.kind = p.handle_kind "
        "AND h.value = p.handle_value "
        f"WHERE h.entity_id IN ({emarks}) AND h.ignored = 0 "
        f"AND o.status = 'active' AND o.stream IN ({marks}) "
        "AND o.journal_date = ? ORDER BY o.started_at DESC, o.id DESC LIMIT 1",
        (*merged, *streams, q["last_contact"])).fetchone()
    if r is None:
        return None
    q["via"] = next(ch for ch, group in CONTACT_CHANNELS if r["stream"] in group)
    return _cite_observation(ctx, r)


def _section_career(ctx: _Ctx, entries: list[dict]) -> dict:
    p = ctx.period
    work = [e for e in entries if "work" in e["categories"]]
    decisions = [e for e in entries if e["kind"] == "decision"]
    events, by_attendee = [], {}
    if ctx.observed:
        rows = _obs_rows(ctx, CALENDAR_STREAMS, p["start"], p["end"])
        attendees: dict[int, list] = {}
        for part in _chunks([r["id"] for r in rows]):
            marks = ",".join("?" * len(part))
            for a in ctx.conn.execute(
                    "SELECT p.observation_id, p.role, p.handle_kind, p.handle_value, "
                    "h.entity_id, h.display_name, h.ignored "
                    "FROM observation_participants p JOIN entity_handles h "
                    "ON h.kind = p.handle_kind AND h.value = p.handle_value "
                    f"WHERE p.observation_id IN ({marks}) "
                    "ORDER BY p.observation_id, p.handle_kind, p.handle_value", part):
                if a["ignored"] or a["role"] == "location" or \
                        a["handle_kind"] in NON_PERSON_KINDS or \
                        (a["handle_kind"], a["handle_value"].lower()) in ctx.owner_handles:
                    continue
                key, info = _person(ctx, a["handle_kind"], a["handle_value"],
                                    a["entity_id"], a["display_name"])
                if info["entity_id"] in ctx.owner_entities:
                    continue
                if all(k != key for k, _ in attendees.get(a["observation_id"], [])):
                    attendees.setdefault(a["observation_id"], []).append((key, info))
        for r in rows:
            who = attendees.get(r["id"], [])
            minutes = _minutes(r)
            cid = _cite_observation(ctx, r, attendees=[i["name"] for _, i in who],
                                    minutes=round(minutes) if minutes is not None else None)
            events.append({"id": cid, "date": r["journal_date"],
                           "title": r["title"] or "(untitled event)",
                           "minutes": round(minutes) if minutes is not None else None,
                           "attendees": [i["name"] for _, i in who]})
            for key, info in who:
                row = by_attendee.setdefault(key, {**info, "count": 0, "minutes": 0,
                                                   "events": []})
                row["count"] += 1
                row["minutes"] += round(minutes) if minutes is not None else 0
                row["events"].append(cid)
    attendees_out = sorted(by_attendee.values(),
                           key=lambda a: (-a["count"], a["name"].lower()))
    timed = [e["minutes"] for e in events if e["minutes"] is not None]
    return {"work_entries": [_compact(ctx, e) for e in work],
            "decisions": [_compact(ctx, e) for e in decisions],
            "meetings": {"total": len(events),
                         "hours": round(sum(timed) / 60, 1) if timed else 0,
                         "events": events[:MAX_ENTRIES],
                         "by_attendee": attendees_out[:MAX_PEOPLE]}}


def _money_flow(r) -> tuple[str | None, float]:
    """('spent' | 'received' | None, amount) for one transaction. A bank
    export either signs the amount or says the direction; both are read,
    and the direction wins when present."""
    if r["value"] is None:
        return None, 0.0
    amount = abs(float(r["value"]))
    if r["direction"] == "out":
        return "spent", amount
    if r["direction"] == "in":
        return "received", amount
    if r["value"] < 0:
        return "spent", amount
    if r["value"] > 0:
        return "received", amount
    return None, 0.0


def _txn_totals(rows) -> dict[str, dict]:
    totals: dict[str, dict] = {}
    for r in rows:
        flow, amount = _money_flow(r)
        t = totals.setdefault(r["unit"] or "", {"spent": 0.0, "received": 0.0,
                                                "count": 0})
        t["count"] += 1
        if flow:
            t[flow] += amount
    return {cur: {k: (round(v, 2) if isinstance(v, float) else v)
                  for k, v in t.items()} for cur, t in sorted(totals.items())}


def _section_finance(ctx: _Ctx, entries: list[dict]) -> dict:
    p = ctx.period
    out = {"totals": [], "merchants": [], "merchants_total": 0}
    if ctx.observed:
        rows = _obs_rows(ctx, TXN_STREAMS, p["start"], p["end"])
        prior = _txn_totals(_obs_rows(ctx, TXN_STREAMS, p["prior"]["start"],
                                      p["prior"]["end"]))
        for cur, t in _txn_totals(rows).items():
            pt = prior.get(cur)
            out["totals"].append({
                "currency": cur or None, **t,
                "prior_spent": pt["spent"] if pt else 0.0,
                "delta_spent": round(t["spent"] - (pt["spent"] if pt else 0.0), 2)})
        merchants: dict[tuple, dict] = {}
        for r in rows:
            name = (r["title"] or _excerpt(r["body"], 80) or "(no description)").strip()
            m = merchants.setdefault((name, r["unit"] or ""), {
                "merchant": name, "currency": r["unit"], "count": 0, "spent": 0.0,
                "received": 0.0, "rows": []})
            flow, amount = _money_flow(r)
            m["count"] += 1
            if flow:
                m[flow] += amount
            m["rows"].append(r)
        ranked = sorted(merchants.values(),
                        key=lambda m: (-m["spent"], -m["received"], m["merchant"]))
        for m in ranked[:MAX_MERCHANTS]:
            rows_m = m.pop("rows")
            m["spent"], m["received"] = round(m["spent"], 2), round(m["received"], 2)
            item = {"type": "observation_count", "tier": "observed",
                    "what": f"transactions: {m['merchant']}", "stream": "txn", **m,
                    "first_date": rows_m[0]["journal_date"],
                    "last_date": rows_m[-1]["journal_date"],
                    "uuids": [r["uuid"] for r in rows_m][:20],
                    "imports": sorted({r["import_uuid"] for r in rows_m})}
            cid = ctx.ids.cite("O", ("merchant", m["merchant"], m["currency"]), item)
            out["merchants"].append({"id": cid, **m})
        out["merchants_total"] = len(merchants)
    said = [e for e in entries if e["money"]]
    out["journal_spending"] = [{**_compact(ctx, e), "money": e["money"]} for e in said]
    out["purchase_entries"] = [_compact(ctx, e) for e in entries
                               if "purchase" in e["categories"]]
    return out


# ---------------------------------------------------------------- the pack

def build_pack(conn: sqlite3.Connection, cfg: Config, period: dict, *,
               lens: str = "week", quiet_weeks: int | None = None) -> dict:
    """The evidence pack for `period` (from resolve_period) through `lens`.

    Reads only — callers pass a read-only connection. A database before the
    observed tier (schema < 4) still gets every confirmed-tier section, and a
    note saying what is missing.
    """
    if lens not in LENSES:
        raise DomainError(f"lens '{lens}' must be one of: {', '.join(LENSES)}")
    weeks = quiet_weeks if quiet_weeks is not None else int(
        (cfg.extra.get("review") or {}).get("quiet_weeks", DEFAULT_QUIET_WEEKS))
    if weeks < 1:
        raise DomainError(f"quiet weeks must be at least 1, got {weeks}")
    ctx = _Ctx(conn=conn, cfg=cfg, period=period, lens=lens, ids=_Ids(),
               observed=db_mod.schema_version(conn) >= 4, quiet_weeks=weeks)
    _load_people(ctx)
    sections = LENS_SECTIONS[lens]

    all_entries = _period_entries(ctx)
    entries = [e for e in all_entries if _lens_keeps(lens, e)]
    pack: dict = {"pack_version": PACK_VERSION, "lens": lens,
                  "sections": list(sections),
                  "period": {k: period[k] for k in ("start", "end", "days", "kind",
                                                    "label", "slug")},
                  "prior": period["prior"], "as_of": period["as_of"],
                  "timezone": cfg.timezone, "observed_tier": ctx.observed}
    if not ctx.observed:
        ctx.notes.append("observed tier unavailable (database schema below v4) "
                         "— texts, calls, chats and transactions are missing; "
                         "run: python -m lifecrm migrate")

    # Built on first use, after the sections listed before it, so ids number
    # in reading order (the entries section's E1 is the period's first entry).
    people_cache: list[list[dict]] = []

    def people() -> list[dict]:
        if not people_cache:
            people_cache.append(_person_rows(ctx, all_entries))
        return people_cache[0]

    builders = {
        "days": lambda: _section_days(ctx),
        "entries": lambda: _section_entries(ctx, entries),
        "loops": lambda: _section_loops(ctx),
        "journal_metrics": lambda: _section_journal_metrics(ctx),
        "health": lambda: _section_health(ctx, all_entries),
        "observations": lambda: (_section_observations(ctx, people)
                                 if ctx.observed else None),
        "titles": lambda: _section_titles(ctx) if ctx.observed else None,
        "relationships": lambda: _section_relationships(ctx, people()),
        "career": lambda: _section_career(ctx, all_entries),
        "finance": lambda: _section_finance(ctx, all_entries),
    }
    for name in sections:
        pack[name] = builders[name]()

    briefs = _load_entries(ctx, "e.journal_date BETWEEN ? AND ?",
                           (period["start"], period["end"]), briefs=True)
    pack["briefs"] = [_brief_item(ctx, e) for e in briefs]

    pack["coverage"] = _coverage(ctx, len(all_entries))
    cov = pack["coverage"]
    pack["empty"] = not (cov["entries"] or cov["days_with_summary"]
                         or cov["health_records"] or cov["observations"]
                         or cov["day_measurements"])
    if pack["empty"]:
        ctx.notes.append(f"nothing was recorded for {period['label']} "
                         f"({period['start']} to {period['end']})")
    else:
        if not cov["entries"]:
            ctx.notes.append("no journal entries in this period — the brief can "
                             "only speak to imported data")
        if "health" in sections and not cov["health_records"]:
            ctx.notes.append("no sensor/app health data in this period")
    if lens in LENS_CATEGORIES:
        ctx.notes.append(f"lens {lens}: entries and loops filtered to "
                         f"{', '.join(sorted(LENS_CATEGORIES[lens]))}"
                         + (", or naming a person" if lens == "relationships" else "")
                         + (", or carrying an amount" if lens == "finance" else ""))
    pack["notes"] = ctx.notes
    pack["index"] = ctx.ids.index
    return pack


def _brief_item(ctx: _Ctx, e: dict) -> dict:
    try:
        data = json.loads(e["_data"]) if e["_data"] else {}
    except ValueError:
        data = {}
    cid = _cite_entry(ctx, e)
    return {"id": cid, "date": e["date"], "title": e["title"],
            "lens": data.get("lens"), "period": data.get("period")}


def _coverage(ctx: _Ctx, n_entries: int) -> dict:
    p = ctx.period

    def one(sql: str, *args) -> int:
        return ctx.conn.execute(sql, args).fetchone()[0]

    return {
        "days_in_period": p["days"],
        "days_with_entries": one(
            "SELECT COUNT(DISTINCT journal_date) FROM entries WHERE status='active' "
            f"AND journal_date BETWEEN ? AND ? AND {_NOT_BRIEF}", p["start"], p["end"]),
        "days_with_summary": one(
            "SELECT COUNT(*) FROM days WHERE summary IS NOT NULL AND summary <> '' "
            "AND journal_date BETWEEN ? AND ?", p["start"], p["end"]),
        "day_measurements": one(
            "SELECT COUNT(*) FROM measurements WHERE status='active' "
            "AND journal_date BETWEEN ? AND ?", p["start"], p["end"]),
        "entries": n_entries,
        "health_records": one(
            "SELECT COUNT(*) FROM health_records WHERE status='active' "
            "AND journal_date BETWEEN ? AND ?", p["start"], p["end"]),
        "observations": one(
            "SELECT COUNT(*) FROM observations WHERE status='active' "
            "AND journal_date BETWEEN ? AND ?", p["start"], p["end"])
        if ctx.observed else 0,
    }


# ---------------------------------------------------------------- citations

CITE_RE = re.compile(r"\[((?:[DEMHO]\d+)(?:\s*[,;]\s*[DEMHO]\d+)*)\]")


def cited_ids(text: str) -> list[str]:
    """Pack ids a brief cites, in first-seen order. `[E1]`, `[E1, H2]` and
    `[E1][H2]` all count."""
    seen: list[str] = []
    for group in CITE_RE.findall(text):
        for cid in re.split(r"\s*[,;]\s*", group.strip()):
            if cid not in seen:
                seen.append(cid)
    return seen


def check_citations(text: str, pack: dict) -> dict:
    """What a brief cites, and anything it cites that the pack does not
    hold. An unknown id means the brief claims evidence that does not exist."""
    found = cited_ids(text)
    known = pack["index"]
    return {"cited": [c for c in found if c in known],
            "unknown": [c for c in found if c not in known],
            "uncited": not any(c in known for c in found)}


def index_line(item: dict) -> str:
    """One line naming what an id points at, durably: uuids, dates and
    source files, so a saved brief stays checkable after the pack is gone."""
    t = item["type"]
    if t == "entry":
        return (f"entry {item['uuid']} · {item['date']} · {item['kind']} · "
                f"{item['title']}")
    if t == "day":
        return f"day {item['date']} (journal summary and day measurements)"
    if t in ("journal_metric", "health_metric"):
        dates = [s["date"] for s in item["series"]]
        src = (f" · sources: {', '.join(item['source_files'])}"
               if item.get("source_files") else "")
        table = "measurements" if t == "journal_metric" else "health_records"
        return (f"{table} {item['metric']} · {dates[0]} to {dates[-1]} · "
                f"{item['days']} day(s), mean {fmt_value(item['mean'], item['unit'])}"
                f"{src}")
    if t == "observation":
        return (f"observation {item['uuid']} · {item['date']} · {item['stream']}"
                + (f" · {item['title']}" if item.get("title") else "")
                + f" · import {item['import_uuid']}")
    span = f"{item['first_date']} to {item['last_date']}"
    sample = ""
    if item.get("first_uuid"):
        sample = f" · first {item['first_uuid']}, last {item['last_uuid']}"
    elif item.get("uuids"):
        sample = f" · e.g. {item['uuids'][0]}"
    imports = (f" · imports {', '.join(item['imports'])}"
               if item.get("imports") else "")
    return f"observations: {item['what']} · {item['count']} · {span}{sample}{imports}"


def sources_footer(pack: dict, ids: list[str]) -> str:
    """The block appended to a brief before it is saved: every cited id
    spelled out as durable references."""
    p = pack["period"]
    lines = ["---",
             f"Sources (lifecrm review pack, lens {pack['lens']}, {p['label']}: "
             f"{p['start']} to {p['end']}; pack ids are only meaningful with "
             f"these lines):"]
    for cid in ids:
        if cid in pack["index"]:
            lines.append(f"[{cid}] {index_line(pack['index'][cid])}")
    return "\n".join(lines)


# ---------------------------------------------------------------- markdown

def _num(value, unit: str) -> str:
    return fmt_value(_round(value, unit) if value is not None else None, unit)


def _signed(value, unit: str) -> str:
    if value is None:
        return "n/a"
    return ("+" if value > 0 else "") + _num(value, unit)


def _entry_line(e: dict) -> str:
    cats = f" ({', '.join(e['categories'])})" if e.get("categories") else ""
    people = (f" — with {', '.join(p['name'] for p in e['people'])}"
              if e.get("people") else "")
    extra = ""
    if e.get("due"):
        extra += f" · due {e['due']}"
    if e.get("closure"):
        extra += f" · {e['closure']} {e.get('closed_on') or ''}".rstrip()
    return f"- [{e['id']}] {e['date']} {e['kind']}{cats}: {e['title']}{people}{extra}"


def _loop_line(e: dict) -> str:
    extra = f" · due {e['due']}" if e.get("due") else ""
    if e.get("closure"):
        extra += f" · {e['closure']} {e.get('closed_on') or ''}".rstrip()
    return f"- [{e['id']}] {e['title']} ({e['kind']}, {e['date']}{extra})"


def _trend_line(m: dict) -> str:
    agg = "per-day total" if m["per_day"] == "sum" else "per-day average"
    prior = (f"; prior {_num(m['prior']['mean'], m['unit'])} over "
             f"{m['prior']['days']} day(s), change {_signed(m['delta'], m['unit'])}"
             if m["prior"] else "; no prior-period data")
    line = (f"- [{m['id']}] {m['label']}: mean {_num(m['mean'], m['unit'])} "
            f"({agg}) over {m['days']} day(s), min {_num(m['min'], m['unit'])} "
            f"{m['min_date']}, max {_num(m['max'], m['unit'])} {m['max_date']}{prior}")
    if "short_nights" in m:
        line += (f"; {m['short_nights']} night(s) under {m['short_night_hours']:g} h"
                 + (f" (prior {m['short_nights_prior']})"
                    if m["short_nights_prior"] is not None else ""))
    if "sessions" in m:
        line += (f"; {m['sessions']} session(s), {_num(m['minutes'], 'min')}"
                 + (f" (prior {m['sessions_prior']} session(s))"
                    if m["sessions_prior"] is not None else ""))
    if "change_in_period" in m:
        line += f"; {_signed(m['change_in_period'], m['unit'])} first to last"
    if m.get("source_files"):
        line += f" · source: {', '.join(m['source_files'])}"
    return line


def _n(count: int, word: str) -> str:
    """'1 call', '3 calls', '1 entry', '2 entries'."""
    if count == 1:
        return f"1 {word}"
    return f"{count} {word[:-1] + 'ies' if word.endswith('y') else word + 's'}"


def _person_line(row: dict) -> str:
    parts = []
    if row["entries"]:
        parts.append(f"in {_n(len(row['entries']), 'entry')} "
                     + "".join(f"[{c}]" for c in row["entries"]))
    for channel in ("messages", "calls", "meetings"):
        ch = row.get(channel)
        if ch:
            detail = ""
            if channel == "messages":
                detail = f" ({ch['in']} in, {ch['out']} out)"
            elif ch.get("minutes"):
                detail = f" ({_num(ch['minutes'], 'min')})"
            parts.append(f"{_n(ch['count'], channel[:-1])}{detail} [{ch['id']}]")
    who = row["name"] + ("" if row["resolved"] else f" (unresolved {row['handle']})")
    return f"- {who}: " + ", ".join(parts)


def render_markdown(pack: dict) -> str:
    p, prior = pack["period"], pack["prior"]
    out = [f"# Review pack — {p['label']} ({p['start']} to {p['end']})", "",
           f"Lens: {pack['lens']} · prior period {prior['start']} to {prior['end']} "
           f"· as of {pack['as_of']} · {pack['timezone']}", "",
           "Built by `lifecrm review`: deterministic, read-only, no model. Every "
           "line carries an id a brief must cite — D day, E entry, M journal "
           "metric, H health metric, O observation(s). Nothing here is "
           "interpretation; observed items were imported, never reviewed.", ""]
    cov = pack["coverage"]
    out.append(f"Coverage: entries on {cov['days_with_entries']} of "
               f"{cov['days_in_period']} day(s), {cov['entries']} entries, "
               f"{cov['health_records']} health records, {cov['observations']} "
               f"observations.")
    for note in pack["notes"]:
        out.append(f"> note: {note}")
    out.append("")

    if "days" in pack:
        out += ["## Days", ""]
        for d in pack["days"]:
            bits = [f"{k.replace('_hours', '')} {_num(d[k], u)}"
                    for k, u in (("mood", "/10"), ("energy", "/10"), ("sleep_hours", "h"))
                    if d[k] is not None]
            bits.append(_n(d["entries"], "entry"))
            out.append(f"- [{d['id']}] {d['date']} · " + " · ".join(bits)
                       + (f" — {d['summary']}" if d["summary"] else ""))
        if not pack["days"]:
            out.append("- (no journal days)")
        out.append("")
    if "entries" in pack:
        s = pack["entries"]
        out += [f"## Entries ({s['total']})", ""]
        if s["by_category"]:
            out.append("By category: " + ", ".join(f"{k} {v}" for k, v in s["by_category"].items()))
            out.append("By kind: " + ", ".join(f"{k} {v}" for k, v in s["by_kind"].items()))
            out.append("")
        out += [_entry_line(e) for e in s["items"]] or ["- (none)"]
        if s.get("truncated"):
            out.append(f"- … and {s['truncated']} more (see --json)")
        out.append("")
    if "loops" in pack:
        s = pack["loops"]
        out += ["## Loops", ""]
        for key, title in (("opened", "Opened"), ("closed", "Closed"),
                           ("overdue", "Overdue"),
                           ("open", f"Still open at period end ({s['open_total']})")):
            out.append(f"{title}:")
            out += [_loop_line(e) for e in s[key]] or ["- (none)"]
        out.append("")
    if "journal_metrics" in pack:
        s = pack["journal_metrics"]
        out += ["## Mood and energy (as said in the journal)", ""]
        out += [_trend_line(m) for m in s["metrics"]] or ["- (no journal metrics)"]
        out.append("")
    if "health" in pack:
        s = pack["health"]
        out += ["## Health (sensor and app imports)", ""]
        out += [_trend_line(m) for m in s["metrics"]] or ["- (no health data)"]
        for miss in s["no_data"]:
            out.append(f"- no {miss['label']} data"
                       + (f" (last record {miss['last_record']})"
                          if miss["last_record"] else ""))
        if s["exercise_entries"]:
            out.append("Exercise in the journal: " + ", ".join(
                f"[{e['id']}] {e['date']} {e['title']}" for e in s["exercise_entries"]))
        out.append("")
    if pack.get("observations"):
        s = pack["observations"]
        out += [f"## Observed ({s['total']} imported records; prior period "
                f"{s['prior_total']})", ""]
        out += [f"- [{o['id']}] {o['stream']}: {o['count']} (prior {o['prior_count']})"
                for o in s["streams"]] or ["- (no imported records)"]
        if s["top_people"]:
            out += ["", "Top people by contact:"]
            out += [_person_line(r) for r in s["top_people"]]
        out.append("")
    if pack.get("titles"):
        s = pack["titles"]
        out += ["## AI chats and notes", ""]
        out += [f"- [{c['id']}] {c['date']} {c['stream']}: {c['title']} "
                f"({c['turns']} turn(s))" for c in s["ai_chats"]] or ["- (no AI chats)"]
        out += [f"- [{n['id']}] {n['date']} note: {n['title']}" for n in s["notes"]]
        out.append("")
    if "relationships" in pack:
        s = pack["relationships"]
        out += ["## Relationships", ""]
        out += [_person_line(r) for r in s["people"]] or ["- (nobody in this period)"]
        out += ["", f"Not in contact for {s['quiet_weeks']}+ week(s) before the "
                    f"period end:"]
        out += [f"- {q['name']}: last {q['last_contact']} ({q['days_since']} days, "
                f"via {q['via']})" + (f" [{q['id']}]" if q["id"] else "")
                for q in s["quiet"]] or ["- (nobody)"]
        out.append("")
    if "career" in pack:
        s = pack["career"]
        out += ["## Career", "", "Work entries:"]
        out += [_loop_line(e) for e in s["work_entries"]] or ["- (none)"]
        out.append("Decisions:")
        out += [_loop_line(e) for e in s["decisions"]] or ["- (none)"]
        m = s["meetings"]
        out.append(f"Calendar: {m['total']} event(s), {m['hours']:g} h timed")
        out += [f"- {a['name']}{'' if a['resolved'] else ' (unresolved ' + a['handle'] + ')'}"
                f": {a['count']} event(s), {a['minutes']} min "
                + "".join(f"[{c}]" for c in a["events"]) for a in m["by_attendee"]]
        out += [f"- [{e['id']}] {e['date']} {e['title']}"
                + (f" ({e['minutes']} min)" if e["minutes"] is not None else "")
                + (f" with {', '.join(e['attendees'])}" if e["attendees"] else "")
                for e in m["events"]]
        out.append("")
    if "finance" in pack:
        s = pack["finance"]
        out += ["## Money (the bank's own descriptions; no categories)", ""]
        for t in s["totals"]:
            cur = f" {t['currency']}" if t["currency"] else ""
            out.append(f"{t['count']} transaction(s): spent {t['spent']:,.2f}{cur}, "
                       f"received {t['received']:,.2f}{cur}; prior period spent "
                       f"{t['prior_spent']:,.2f}{cur} "
                       f"({'+' if t['delta_spent'] > 0 else ''}{t['delta_spent']:,.2f})")
        out += [f"- [{m['id']}] {m['merchant']}: {m['spent']:,.2f} spent"
                + (f", {m['received']:,.2f} received" if m["received"] else "")
                + f" ({m['count']} txn)" for m in s["merchants"]] or ["- (no transactions)"]
        if s["merchants_total"] > len(s["merchants"]):
            out.append(f"- … and {s['merchants_total'] - len(s['merchants'])} more merchants")
        if s["journal_spending"]:
            out.append("Amounts said in the journal:")
            out += [f"- [{e['id']}] {e['date']} {e['title']}: "
                    + ", ".join(f"{m['amount']:g} {m['unit'] or ''}".strip()
                                for m in e["money"]) for e in s["journal_spending"]]
        out.append("")
    if pack["briefs"]:
        out += ["## Briefs already saved for this period (interpretations, not evidence)", ""]
        out += [f"- [{b['id']}] {b['date']} {b['title']}" for b in pack["briefs"]]
        out.append("")
    return "\n".join(out).rstrip() + "\n"

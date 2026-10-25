"""`lifecrm nudges` — what the records say matters today, cited.

Reads only. Every nudge is recomputed from the database on each run and
rests on ids the reader can check: `E#` a confirmed entry, `O#` an observed
row or a count over observed rows (always "observed, not confirmed"), and the
nudge itself is `N#`. Nothing here is a model's opinion; the wording is fixed
per kind and says what the records show, never what the owner did.

The owner's only input is state: a nudge snoozed until a date or dismissed
for good (lifecrm/domain/nudges.py) goes to `hidden`. State attaches to a
durable key (`due:<entry uuid>`, `birthday:<entity id>:<year>`, ...), never to
the `N#` of one run, because an import or a closed task between two runs
renumbers everything.

A run can be saved (`--out FILE`); its `pack_id` hashes the pack's whole
content, so a changed title, date, value or state means a different pack. A
brief written from a saved pack is checked against that file (`check`),
never against a rebuild. `check` verifies that cited ids exist and that
every number and date in a sentence appears in the lines it cites; whether
the sentence's claim follows is the reader's judgement.

"Today" is the caller's: the CLI and /morning pass the clock's journal date
(recall.local_today), the Obsidian Home note passes the database's as-of
date, so the vault stays a pure function of the database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import nutrients
from .config import Config, ConfigError
from .domain import nudges as nudge_state
from .domain.reminders import CONTENT_CHANGE
from .domain import days as days_mod
from .domain.util import DomainError
from .recall import STREAM_GROUPS, check_text, local_today
from .review import NOT_DERIVED, _Ids, index_line
from .web import api

PACK_VERSION = 1
SECTION = "assistant"
DEFAULTS = {"due_days": 7, "birthday_days": 14, "nutrient_days": 14,
            "follow_up_days": 3, "follow_up_window": 30, "lapsed_days": 180,
            "drift_recent": 7, "drift_baseline": 30,
            "reminder_hour": 9, "reminders_dir": None, "auto_publish": True}
_DAY_KEYS = ("due_days", "birthday_days", "nutrient_days", "follow_up_days",
             "follow_up_window", "lapsed_days", "drift_recent", "drift_baseline")
_FLAG_WORDS = {"true": True, "yes": True, "on": True, "1": True,
               "false": False, "no": False, "off": False, "0": False}
KIND_ORDER = ("reminder", "follow_up", "goal", "drift", "birthday", "nutrient", "stale",
              "lapsed")
# Drift: what counts as a change between the last `drift_recent` days and the
# `drift_baseline` days before them. A level compares means and needs 3
# recent and 8 baseline known days; a weekly rate needs 80 % of both windows
# declared or recorded (an unknown day is never a zero).
DRIFT = {"sleep": ("level", 0.5), "sleep_hours": ("level", 0.5), "mood": ("level", 1.0),
         "energy": ("level", 1.0), "steps": ("level", 1500.0),
         "workouts": ("weekly", 1.0), "spending": ("weekly_pct", 0.25)}
DRIFT_MIN_RECENT, DRIFT_MIN_BASELINE, DRIFT_COVERAGE = 3, 8, 0.8
SPENDING_MIN_DELTA = 50.0
LAPSED_MIN_CONTACTS = 6
_TABLE_PREFIX = {"measurements": "M", "health_records": "H", "observations": "O",
                 "entries": "E"}
TIER_NOTE = "(observed, not confirmed)"
CALENDAR_STREAMS = STREAM_GROUPS["calendar"]


# ---------------------------------------------------------------- settings

def settings(cfg: Config) -> dict:
    """[assistant] in config.toml with defaults; one-line errors for a value
    the layer cannot use."""
    raw = (cfg.extra or {}).get(SECTION)
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    for key in _DAY_KEYS:
        if key in raw:
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 365:
                raise DomainError(f"[{SECTION}] {key} must be a whole number of days, "
                                  f"1-365, got {v!r}")
            out[key] = v
    if "reminder_hour" in raw:
        v = raw["reminder_hour"]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 23:
            raise DomainError(f"[{SECTION}] reminder_hour must be an hour 0-23, got {v!r}")
        out["reminder_hour"] = v
    if "reminders_dir" in raw:
        v = raw["reminders_dir"]
        if v in ("", None):
            out["reminders_dir"] = None
        elif isinstance(v, str):
            out["reminders_dir"] = v
        else:
            raise DomainError(f"[{SECTION}] reminders_dir must be a folder path in "
                              f"quotes, got {v!r}")
    if "auto_publish" in raw:
        v = raw["auto_publish"]
        if isinstance(v, str) and v.strip().lower() in _FLAG_WORDS:
            v = _FLAG_WORDS[v.strip().lower()]
        if not isinstance(v, bool):
            raise DomainError(f"[{SECTION}] auto_publish must be true or false, got {v!r}")
        out["auto_publish"] = v
    known = set(DEFAULTS)
    unknown = sorted(set(raw) - known)
    if unknown:
        raise DomainError(f"[{SECTION}] has unknown setting(s) {', '.join(unknown)} — "
                          f"known: {', '.join(sorted(known))}")
    return out


def today_and_now(cfg: Config, today: str | None = None) -> tuple[date, datetime]:
    """The CLI's reference: the clock's journal date (timezone and rollover),
    or `today` as given with noon local as the instant."""
    if today is None:
        return local_today(cfg), datetime.now(timezone.utc)
    try:
        d = date.fromisoformat(today)
    except ValueError:
        raise DomainError(f"--today must be YYYY-MM-DD, got {today!r}") from None
    from zoneinfo import ZoneInfo
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(cfg.timezone))
    return d, noon.astimezone(timezone.utc)


def as_of_date(conn: sqlite3.Connection, cfg: Config) -> str | None:
    """The journal date of the database's last content change (the rule the
    vault's start pages are dated by), or None for an empty audit log."""
    row = conn.execute(f"SELECT MAX(ts) FROM audit_log WHERE {CONTENT_CHANGE}").fetchone()
    if not row or not row[0]:
        return None
    try:
        stamp = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return days_mod.journal_date_for(stamp, cfg.timezone, cfg.day_rollover_hour)


# ---------------------------------------------------------------- helpers

def _days(n: int) -> str:
    return f"{n} day{'' if n == 1 else 's'}"


def _obs_item(r, **extra) -> dict:
    return {"type": "observation", "tier": "observed", "uuid": r["uuid"],
            "stream": r["stream"], "date": r["journal_date"],
            "occurred_at": r["started_at"], "direction": r["direction"],
            "title": r["title"], "import_uuid": r["import_uuid"], **extra}


_OBS_COLS = ("o.id, o.uuid, o.stream, o.title, o.body, o.journal_date, o.started_at, "
             "o.ended_at, o.utc_offset_sec, o.direction, o.value_json, o.origin_app, "
             "i.uuid AS import_uuid")


def _detail(value_json: str | None) -> dict:
    try:
        d = json.loads(value_json) if value_json else {}
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------- the kinds

def _reminders(conn: sqlite3.Connection, s: dict, today: date, ids: _Ids) -> list[dict]:
    """Open tasks and plans due within due_days, overdue first. Confirmed."""
    limit = (today + timedelta(days=s["due_days"])).isoformat()
    rows = conn.execute(
        "SELECT e.uuid, e.title, e.due_at, e.journal_date, k.name AS kind "
        "FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        "WHERE e.status = 'active' AND e.closed_at IS NULL "
        "AND k.name IN ('task', 'plan') AND e.due_at IS NOT NULL "
        f"AND substr(e.due_at, 1, 10) <= ? AND {NOT_DERIVED} "
        "ORDER BY substr(e.due_at, 1, 10), e.journal_date, e.id", (limit,)).fetchall()
    out = []
    for r in rows:
        due = date.fromisoformat(r["due_at"][:10])
        delta = (due - today).days
        eid = ids.cite("E", r["uuid"], {
            "type": "entry", "tier": "confirmed", "uuid": r["uuid"],
            "date": r["journal_date"], "kind": r["kind"], "title": r["title"],
            "due_at": r["due_at"]})
        if delta < 0:
            title = f"Overdue by {_days(-delta)}: {r['title']}"
        elif delta == 0:
            title = f"Due today: {r['title']}"
        else:
            title = f"Due in {_days(delta)} ({due.isoformat()}): {r['title']}"
        out.append({"kind": "reminder", "key": f"due:{r['uuid']}", "title": title,
                    "detail": f"{r['kind']} recorded {r['journal_date']}, due "
                              f"{due.isoformat()}",
                    "date": due.isoformat(), "ids": [eid], "tier": "confirmed",
                    "_sort": (0 if delta < 0 else 1, due.isoformat(), r["journal_date"])})
    return out


def parse_birthday(value) -> tuple[int | None, int, int] | None:
    """A contact card's birthday: 'YYYY-MM-DD' or '--MM-DD' (no year) ->
    (year or None, month, day); None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        if value.startswith("--") and len(value) == 7:
            m, d = int(value[2:4]), int(value[5:7])
            date(2000, m, d)                      # a leap year: Feb 29 is valid
            return None, m, d
        d_ = date.fromisoformat(value)
        return d_.year, d_.month, d_.day
    except ValueError:
        return None


def next_occurrence(month: int, day: int, today: date) -> date:
    """The next calendar day with this month and day, today included; a
    February 29 falls on the 28th in a year without one."""
    for year in (today.year, today.year + 1):
        try:
            occ = date(year, month, day)
        except ValueError:
            occ = date(year, month, 28)
        if occ >= today:
            return occ
    return date(today.year + 1, month, min(day, 28))


def _birthdays(conn: sqlite3.Connection, cfg: Config, s: dict, today: date,
               ids: _Ids, owners: frozenset) -> list[dict]:
    """People whose contact card carries a birthday within birthday_days;
    one nudge per person, through the card's handle link. Observed."""
    try:
        rows = conn.execute(
            f"SELECT {_OBS_COLS}, json_extract(o.value_json, '$.birthday') AS birthday, "
            "CASE WHEN e.status = 'merged' THEN e.merged_into ELSE e.id END AS entity_id, "
            "CASE WHEN e.status = 'merged' THEN m.name ELSE e.name END AS name, "
            "CASE WHEN e.status = 'merged' THEN m.type ELSE e.type END AS etype "
            "FROM observations o JOIN imports i ON i.id = o.import_id "
            "JOIN observation_participants p ON p.observation_id = o.id "
            "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            "JOIN entities e ON e.id = h.entity_id "
            "LEFT JOIN entities m ON m.id = e.merged_into "
            "WHERE o.stream = 'contact_card' AND o.status = 'active' "
            "AND json_valid(o.value_json) "
            "AND json_extract(o.value_json, '$.birthday') IS NOT NULL "
            "AND h.ignored = 0 ORDER BY o.id, p.handle_kind, p.handle_value").fetchall()
    except sqlite3.OperationalError:
        return []                                  # pre-v4: no observed tier
    best: dict[int, tuple] = {}
    for r in rows:
        if r["etype"] != "person" or r["entity_id"] in owners:
            continue
        parsed = parse_birthday(r["birthday"])
        if parsed is None:
            continue
        year, month, day = parsed
        occ = next_occurrence(month, day, today)
        if (occ - today).days > s["birthday_days"]:
            continue
        current = best.get(r["entity_id"])
        # Prefer a card that knows the year; otherwise the first card seen.
        if current is None or (current[0] is None and year is not None):
            best[r["entity_id"]] = (year, occ, r)
    out = []
    for entity_id, (year, occ, r) in best.items():
        delta = (occ - today).days
        when = ("is today" if delta == 0 else "is tomorrow" if delta == 1
                else f"is in {_days(delta)} ({occ.isoformat()})")
        age = f", turning {occ.year - year}" if year else ""
        oid = ids.cite("O", ("obs", r["uuid"]), _obs_item(r))
        out.append({"kind": "birthday", "key": f"birthday:{entity_id}:{occ.year}",
                    "title": f"{r['name']}'s birthday {when}{age}",
                    "detail": f"from a contact card dated {r['journal_date']} {TIER_NOTE}",
                    "date": occ.isoformat(), "ids": [oid], "tier": "observed",
                    "entity_id": entity_id, "_sort": (occ.isoformat(), r["name"])})
    return out


def _nutrients(conn: sqlite3.Connection, cfg: Config, s: dict, today: date,
               ids: _Ids, notes: list[str]) -> list[dict]:
    """Nutrients below 70 % of the reference over nutrient_days, one nudge
    each, the report's disclaimer first. Observed."""
    try:
        window = nutrients.resolve_window(cfg, days=s["nutrient_days"], today=today)
        report = nutrients.build_report(conn, cfg, window)
    except DomainError as e:
        notes.append(f"nutrients: {e}")
        return []
    except sqlite3.OperationalError:
        return []
    if not report.get("low"):
        return []
    by_key = {n["key"]: n for n in report["nutrients"]}
    out = []
    for key in report["low"]:
        n = by_key.get(key)
        if n is None:
            continue
        row_ids = sorted(set(n["cites"].get("nutrition_day", []))
                         | set(n["cites"].get("food_log", [])))
        cited = _rows_by_id(conn, row_ids)
        if not cited:
            continue
        oid = ids.cite("O", ("nutrient", key), {
            "type": "observation_count", "tier": "observed",
            "what": f"food diary rows behind {n['label']}", "count": len(cited),
            "first_date": cited[0]["journal_date"], "last_date": cited[-1]["journal_date"],
            "uuids": [c["uuid"] for c in cited],
            "imports": sorted({c["import_uuid"] for c in cited})})
        ref = n["reference"]
        title = (f"{n['label']} averaged {n['average']} {n['unit']} a day over "
                 f"{_days(n['days_with_value'])}, {n['percent']}% of the "
                 f"{ref['value']} {ref['unit']} reference")
        out.append({"kind": "nutrient", "key": f"nutrient:{key}", "title": title,
                    "detail": f"{nutrients.DISCLAIMER} Food diary, {window['label']} "
                              f"{TIER_NOTE}.",
                    "date": None, "ids": [oid], "tier": "observed",
                    "_sort": (n["percent"], key)})
    return out


def _rows_by_id(conn: sqlite3.Connection, row_ids: list[int]) -> list:
    out = []
    for i in range(0, len(row_ids), 500):
        chunk = row_ids[i:i + 500]
        marks = ",".join("?" for _ in chunk)
        out += conn.execute(
            f"SELECT o.uuid, o.journal_date, i.uuid AS import_uuid FROM observations o "
            f"JOIN imports i ON i.id = o.import_id WHERE o.id IN ({marks}) "
            f"ORDER BY o.journal_date, o.id", chunk).fetchall()
    return out


def _stale(conn: sqlite3.Connection, now: datetime, ids: _Ids) -> list[dict]:
    """Connectors whose newest record is older than their own
    stale_after_days — the rule doctor and the Sources page share. Observed."""
    try:
        rows, _err = api._connector_freshness(conn, now)
    except sqlite3.OperationalError:
        return []
    out = []
    for c in rows:
        if not c["stale"]:
            continue
        streams = [s_["stream"] for s_ in c["streams"]]
        marks = ",".join("?" for _ in streams)
        newest = conn.execute(
            f"SELECT {_OBS_COLS} FROM observations o JOIN imports i ON i.id = o.import_id "
            f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
            f"ORDER BY o.started_at DESC, o.id DESC LIMIT 1", streams).fetchone()
        if newest is None:
            continue
        oid = ids.cite("O", ("obs", newest["uuid"]), _obs_item(newest))
        out.append({"kind": "stale", "key": f"stale:{c['connector']}",
                    "title": f"{c['connector']}: newest record is {c['latest'][:10]}, "
                             f"nothing newer in over {_days(c['stale_after_days'])}",
                    "detail": "the export or backup feeding it may have stopped; "
                              "`python -m lifecrm where` lists what this machine can "
                              f"import {TIER_NOTE}",
                    "date": c["latest"][:10], "ids": [oid], "tier": "observed",
                    "_sort": (c["connector"],)})
    return out


def _agenda(conn: sqlite3.Connection, cfg: Config, today: date, ids: _Ids,
            owners: frozenset, owner_handles: frozenset) -> list[dict]:
    """Today's and tomorrow's calendar events, local time, who is on them;
    cancelled ones marked. Observed, never snoozable."""
    days = (today.isoformat(), (today + timedelta(days=1)).isoformat())
    marks = ",".join("?" for _ in CALENDAR_STREAMS)
    try:
        rows = conn.execute(
            f"SELECT {_OBS_COLS} FROM observations o JOIN imports i ON i.id = o.import_id "
            f"WHERE o.stream IN ({marks}) AND o.status = 'active' "
            f"AND o.journal_date IN (?, ?) ORDER BY o.started_at, o.id",
            (*CALENDAR_STREAMS, *days)).fetchall()
    except sqlite3.OperationalError:
        return []
    parts = api._participants(conn, [r["id"] for r in rows]) if rows else {}
    out = []
    for r in rows:
        d = _detail(r["value_json"])
        all_day = bool(d.get("all_day"))
        local = api.local_time(r["started_at"], cfg.timezone, r["utc_offset_sec"])
        who = sorted({
            p["entity"] or p["display_name"]
            for p in parts.get(r["id"], [])
            if p["role"] in ("participant", "organizer")
            and (p["entity"] or p["display_name"])
            and p["entity_id"] not in owners
            and (p["kind"], (p["value"] or "").lower()) not in owner_handles})
        oid = ids.cite("O", ("obs", r["uuid"]), _obs_item(r))
        out.append({"id": oid, "date": r["journal_date"],
                    "time": None if all_day or not local else local[11:16],
                    "all_day": all_day, "title": r["title"] or "(untitled)",
                    "with": who, "location": d.get("location"),
                    "calendar": d.get("calendar"),
                    "cancelled": (d.get("status") or "").lower() == "cancelled"})
    return out


def _cite_rows(ids: _Ids, key: tuple, refs: list[tuple[str, str]], days: list[str],
               what: str) -> list[str]:
    """One `rows` item per table behind a computed value (a series' days)."""
    out = []
    for table in sorted({t for t, _u in refs}):
        uuids = sorted({u for t, u in refs if t == table})
        tier = "confirmed" if table in ("measurements", "entries") else "observed"
        out.append(ids.cite(_TABLE_PREFIX[table], ("rows", table, *key), {
            "type": "rows", "tier": tier, "table": table, "what": what,
            "count": len(uuids), "first_date": min(days), "last_date": max(days),
            "uuids": uuids}))
    return out


def _series_refs(s, start: str, end: str) -> tuple[list, list[str]]:
    from . import series as series_mod
    days = [d for d in series_mod._days(start, end) if d in s.known]
    return [r for d in days for r in s.refs.get(d, [])], days


def _goals(conn: sqlite3.Connection, cfg: Config, today: date, ids: _Ids) -> list[dict]:
    """Every open goal (lifecrm/goals.py), as it stands today. The plan is the
    confirmed citation; the rows behind the value come along."""
    from . import goals as goals_mod
    out = []
    try:
        items = goals_mod.progress(conn, cfg, today)
    except sqlite3.OperationalError:
        return []
    for g in items:
        eid = ids.cite("E", g["uuid"], {
            "type": "entry", "tier": "confirmed", "uuid": g["uuid"], "date": g["date"],
            "kind": "plan", "title": g["title"]})
        cites = [eid]
        detail = "a plan you set, measured from the records"
        if g.get("error"):
            detail = f"cannot be measured: {g['error']}"
        else:
            cur = g["current"]
            if g.get("refs"):
                cites += _cite_rows(ids, ("goal", g["uuid"]), g["refs"],
                                    [cur["start"], min(cur["end"], today.isoformat())],
                                    f"rows behind {g['title']} this {g['goal']['per']}")
            if cur["known"] < cur["days_elapsed"]:
                detail += (f"; {cur['known']} of {cur['days_elapsed']} day(s) known so "
                           f"far — an unknown day is not a zero")
            if g["series"]["tier"] == "observed":
                detail += f" {TIER_NOTE}"
            if g.get("note"):
                detail += f"; {g['note']}"
        out.append({"kind": "goal", "key": f"goal:{g['uuid']}", "title": goals_mod.describe(g),
                    "detail": detail, "date": today.isoformat(), "ids": cites,
                    "tier": "confirmed", "_sort": (0 if g.get("error") else 1, g["title"])})
    return out


def _drift(conn: sqlite3.Connection, cfg: Config, s: dict, today: date, ids: _Ids,
           notes: list[str]) -> list[dict]:
    """The last drift_recent days against the drift_baseline days before them,
    per series in DRIFT; a comparison with your own recent record, never a
    verdict. Below the coverage floor it is a note, not a nudge."""
    from . import series as series_mod
    n, m = s["drift_recent"], s["drift_baseline"]
    recent_start = today - timedelta(days=n - 1)
    base_end = recent_start - timedelta(days=1)
    base_start = base_end - timedelta(days=m - 1)
    keys = ["sleep", "steps", "workouts", "mood", "energy", "spending"]
    out = []
    for key in keys:
        ser = series_mod.daily(conn, cfg, key, base_start.isoformat(), today.isoformat())
        if key == "sleep" and not ser.known:
            key = "sleep_hours"
            ser = series_mod.daily(conn, cfg, key, base_start.isoformat(), today.isoformat())
        how, threshold = DRIFT[key]
        rec_days = [d for d in series_mod._days(recent_start.isoformat(), today.isoformat())
                    if d in ser.known]
        base_days = [d for d in series_mod._days(base_start.isoformat(), base_end.isoformat())
                     if d in ser.known]
        unit = ser.unit or ""
        if how == "level":
            if len(rec_days) < DRIFT_MIN_RECENT or len(base_days) < DRIFT_MIN_BASELINE:
                notes.append(f"{ser.label}: insufficient coverage for drift "
                             f"({len(rec_days)} of {n} recent days known, {len(base_days)} "
                             f"of {m} baseline)")
                continue
            rec_v = round(sum(ser.values[d] for d in rec_days) / len(rec_days), 2)
            base_v = round(sum(ser.values[d] for d in base_days) / len(base_days), 2)
            delta = rec_v - base_v
            if abs(delta) < threshold:
                continue
            what = (f"{ser.label} {'up' if delta > 0 else 'down'}: last {n} days averaged "
                    f"{rec_v:g} {unit} vs {base_v:g} {unit} over the {m} before").replace("  ", " ")
        else:
            if len(rec_days) < DRIFT_COVERAGE * n or len(base_days) < DRIFT_COVERAGE * m:
                notes.append(f"{ser.label}: insufficient coverage for drift "
                             f"({len(rec_days)} of {n} recent days known, {len(base_days)} "
                             f"of {m} baseline; days an import declared complete)")
                continue
            rec_v = round(sum(ser.values[d] for d in rec_days), 2)
            base_total = sum(ser.values[d] for d in base_days)
            base_v = round(base_total / (len(base_days) / 7), 2)
            delta = rec_v - base_v
            if how == "weekly" and abs(delta) < threshold:
                continue
            if how == "weekly_pct" and (abs(delta) < SPENDING_MIN_DELTA
                                        or abs(delta) < threshold * max(base_v, 1e-9)):
                continue
            what = (f"{ser.label} {'up' if delta > 0 else 'down'}: {rec_v:g} {unit} in the "
                    f"last {n} days vs {base_v:g} {unit} a week over the {m} before"
                    ).replace("  ", " ")
        refs_r, _ = _series_refs(ser, recent_start.isoformat(), today.isoformat())
        refs_b, _ = _series_refs(ser, base_start.isoformat(), base_end.isoformat())
        cites = (_cite_rows(ids, ("drift", key, "recent"), refs_r, rec_days,
                            f"{ser.label}, last {n} days")
                 + _cite_rows(ids, ("drift", key, "baseline"), refs_b, base_days,
                              f"{ser.label}, the {m} days before"))
        detail = (f"{len(rec_days)} of {n} recent days known, {len(base_days)} of {m} "
                  f"baseline; a comparison with your own recent record, not a verdict"
                  + (f" {TIER_NOTE}" if ser.tier == "observed" else ""))
        out.append({"kind": "drift", "key": f"drift:{key}", "title": what, "detail": detail,
                    "date": today.isoformat(), "ids": cites, "tier": ser.tier,
                    "_sort": (keys.index(key if key != "sleep_hours" else "sleep"),)})
    return out


def _contact_events(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    """Every participant row of a contact-channel observation in the window,
    resolved to a person; plus how many non-owner parties each row has."""
    from .series import CONTACT_STREAMS
    marks = ",".join("?" for _ in CONTACT_STREAMS)
    rows = [dict(r) for r in conn.execute(
        "SELECT o.id, o.uuid, o.stream, o.journal_date, o.started_at, o.direction, "
        "o.value, o.value_json, o.title, o.body, i.uuid AS import_uuid, p.role, "
        "p.handle_kind, p.handle_value, "
        "CASE WHEN e.status = 'merged' THEN e.merged_into ELSE e.id END AS eid, "
        "CASE WHEN e.status = 'merged' THEN m.type ELSE e.type END AS etype "
        "FROM observations o JOIN imports i ON i.id = o.import_id "
        "JOIN observation_participants p ON p.observation_id = o.id "
        "LEFT JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
        "AND h.ignored = 0 "
        "LEFT JOIN entities e ON e.id = h.entity_id "
        "LEFT JOIN entities m ON m.id = e.merged_into "
        f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
        f"AND o.journal_date BETWEEN ? AND ? ORDER BY o.started_at, o.id, p.role",
        (*CONTACT_STREAMS, start, end))]
    return rows


def _follow_ups(conn: sqlite3.Connection, cfg: Config, s: dict, today: date, ids: _Ids,
                owners: frozenset, owner_handles: frozenset, notes: list[str]
                ) -> tuple[list[dict], set[int]]:
    """The newest incoming 1:1 message from a linked person, at least
    follow_up_days old, with no later contact in the imported records: no
    message or email from you to them, no answered call, no past meeting that
    was not cancelled, no confirmed entry naming them. Suppressed, with a
    note, when the channel's imports stop before the reply window closed."""
    from .review import NOT_DERIVED
    from .series import CALENDAR_STREAMS, CALL_STREAMS, EMAIL_STREAMS, TEXT_STREAMS
    window_start = today - timedelta(days=s["follow_up_window"])
    cutoff = today - timedelta(days=s["follow_up_days"])
    events = _contact_events(conn, window_start.isoformat(), today.isoformat())
    by_obs: dict[int, list[dict]] = {}
    for ev in events:
        by_obs.setdefault(ev["id"], []).append(ev)
    message_streams = set(TEXT_STREAMS) | set(EMAIL_STREAMS)
    newest_by_group: dict[str, str] = {}
    for grp, streams in (("text", TEXT_STREAMS), ("email", EMAIL_STREAMS)):
        marks = ",".join("?" for _ in streams)
        row = conn.execute(f"SELECT MAX(journal_date) FROM observations WHERE status = 'active' "
                           f"AND stream IN ({marks})", streams).fetchone()
        newest_by_group[grp] = row[0] if row else None
    def is_group(parts: list[dict]) -> bool:
        others = {(p["handle_kind"], (p["handle_value"] or "").lower()) for p in parts
                  if (p["handle_kind"], (p["handle_value"] or "").lower()) not in owner_handles
                  and p["eid"] not in owners}
        return len(others) > 1 or bool(_detail(parts[0]["value_json"]).get("group"))

    groups = {obs_id for obs_id, parts in by_obs.items() if is_group(parts)}
    # the newest incoming 1:1 message per person
    candidates: dict[int, dict] = {}
    for obs_id, parts in by_obs.items():
        head = parts[0]
        if head["stream"] not in message_streams or head["direction"] != "in":
            continue
        if head["journal_date"] > cutoff.isoformat():
            continue
        if obs_id in groups:
            continue
        sender = next((p for p in parts if p["role"] == "sender" and p["eid"] is not None
                       and p["etype"] == "person" and p["eid"] not in owners), None)
        if sender is None:
            continue
        cur = candidates.get(sender["eid"])
        if cur is None or head["started_at"] > cur["started_at"]:
            candidates[sender["eid"]] = {**head, "eid": sender["eid"]}
    mentioned = {}
    for r in conn.execute(
            "SELECT e.journal_date, "
            "CASE WHEN en.status = 'merged' THEN en.merged_into ELSE en.id END AS eid "
            "FROM entries e JOIN mentions mn ON mn.entry_id = e.id "
            "JOIN entities en ON en.id = mn.entity_id "
            f"WHERE e.status = 'active' AND mn.role = 'participant' "
            f"AND e.journal_date >= ? AND {NOT_DERIVED}", (window_start.isoformat(),)):
        mentioned.setdefault(r["eid"], []).append(r["journal_date"])
    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM entities WHERE id IN (%s)" % ",".join(
            "?" for _ in candidates), list(candidates))} if candidates else {}
    out, followed = [], set()
    for eid, msg in sorted(candidates.items(), key=lambda kv: kv[1]["started_at"]):
        later = False
        for ev in events:
            if ev["eid"] != eid or ev["started_at"] <= msg["started_at"]:
                continue
            # A reply is a message to THEM: a line you sent in a group they
            # are in does not answer their text.
            if ev["stream"] in message_streams and ev["direction"] == "out" \
                    and ev["role"] == "recipient" and ev["id"] not in groups:
                later = True
            elif ev["stream"] in CALL_STREAMS and (ev["value"] or 0) > 0:
                later = True
            elif ev["stream"] in CALENDAR_STREAMS and ev["journal_date"] <= today.isoformat() \
                    and (_detail(ev["value_json"]).get("status") or "").lower() != "cancelled":
                later = True
            if later:
                break
        if not later and any(d >= msg["journal_date"] for d in mentioned.get(eid, [])):
            later = True
        if later:
            continue
        grp = "text" if msg["stream"] in TEXT_STREAMS else "email"
        needed = (date.fromisoformat(msg["journal_date"])
                  + timedelta(days=s["follow_up_days"])).isoformat()
        newest = newest_by_group.get(grp)
        name = names.get(eid, "someone")
        if newest is None or newest < needed:
            notes.append(f"follow-up for {name}'s {grp} from {msg['journal_date']} not "
                         f"judged: the newest imported {grp} is {newest or 'none'}, before "
                         f"{needed} — import again first")
            continue
        snippet = (msg["body"] or msg["title"] or "").strip().replace("\n", " ")
        if len(snippet) > 80:
            snippet = snippet[:79] + "…"
        oid = ids.cite("O", ("obs", msg["uuid"]), _obs_item(msg))
        out.append({"kind": "follow_up", "key": f"followup:{msg['uuid']}",
                    "title": f"No reply found in imported records: {name}'s {grp} from "
                             f"{msg['journal_date']}",
                    "detail": (f"\"{snippet}\" — nothing from you to them since, in texts, "
                               f"emails, calls, meetings or the journal; newest imported "
                               f"{grp} is {newest} {TIER_NOTE}"),
                    "date": msg["journal_date"], "ids": [oid], "tier": "observed",
                    "entity_id": eid, "_sort": (msg["journal_date"], name)})
        followed.add(eid)
    return out, followed


def _lapsed(conn: sqlite3.Connection, cfg: Config, s: dict, today: date, ids: _Ids,
            skip: set[int]) -> list[dict]:
    """A person whose own rhythm has lapsed: at least LAPSED_MIN_CONTACTS
    contact days in lapsed_days, and the days since the last one more than
    twice the median gap (and more than a week). Observed, worded as what
    the records hold."""
    import statistics
    from . import series as series_mod
    start = today - timedelta(days=s["lapsed_days"])
    people = series_mod.contact_days(conn, cfg, start.isoformat(), today.isoformat())
    out = []
    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM entities WHERE type = 'person'")}
    for eid, days in people.items():
        if eid in skip or len(days) < LAPSED_MIN_CONTACTS:
            continue
        dates = sorted(date.fromisoformat(d) for d in days)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        median = statistics.median(gaps)
        since = (today - dates[-1]).days
        if since <= max(2 * median, 7):
            continue
        last_day = dates[-1].isoformat()
        table, uuid = days[last_day][0]
        if table == "observations":
            row = conn.execute(
                f"SELECT {_OBS_COLS} FROM observations o JOIN imports i ON i.id = o.import_id "
                f"WHERE o.uuid = ?", (uuid,)).fetchone()
            cid = ids.cite("O", ("obs", uuid), _obs_item(row)) if row else None
        else:
            row = conn.execute(
                "SELECT e.uuid, e.journal_date, e.title, k.name AS kind FROM entries e "
                "JOIN entry_kinds k ON k.id = e.kind_id WHERE e.uuid = ?", (uuid,)).fetchone()
            cid = ids.cite("E", uuid, {"type": "entry", "tier": "confirmed", "uuid": uuid,
                                       "date": row["journal_date"], "kind": row["kind"],
                                       "title": row["title"]}) if row else None
        name = names.get(eid, f"entity {eid}")
        out.append({"kind": "lapsed", "key": f"lapsed:{eid}",
                    "title": f"{name}: no recorded contact in {_days(since)}; the usual gap "
                             f"was about {median:g} days over {len(dates)} contact days since "
                             f"{dates[0].isoformat()}",
                    "detail": f"last contact {last_day}; counts messages, calls, meetings, "
                              f"emails and journal mentions {TIER_NOTE}",
                    "date": last_day, "ids": [cid] if cid else [], "tier": "observed",
                    "entity_id": eid, "_sort": (-since, name)})
    return out


# ---------------------------------------------------------------- the pack

def build(conn: sqlite3.Connection, cfg: Config, *, today: date | str,
          now: datetime | None = None, include_hidden: bool = True) -> dict:
    """Everything the records say matters on `today`, as one pack."""
    if isinstance(today, str):
        today = date.fromisoformat(today)
    now = now or datetime.now(timezone.utc)
    s = settings(cfg)
    ids = _Ids()
    notes: list[str] = []
    try:
        from . import owner as owner_mod
        ident = owner_mod.identity(conn, cfg)
        owners, owner_handles = ident.entities, ident.handles
    except (ConfigError, DomainError, sqlite3.Error) as e:   # a bad [owner] table
        owners, owner_handles = frozenset(), frozenset()
        notes.append(f"owner: {e}")

    found: list[dict] = []
    found += _reminders(conn, s, today, ids)
    try:
        follow_ups, followed = _follow_ups(conn, cfg, s, today, ids, owners, owner_handles,
                                           notes)
    except sqlite3.OperationalError:
        follow_ups, followed = [], set()
    found += follow_ups
    found += _goals(conn, cfg, today, ids)
    found += _drift(conn, cfg, s, today, ids, notes)
    found += _birthdays(conn, cfg, s, today, ids, owners)
    found += _nutrients(conn, cfg, s, today, ids, notes)
    found += _stale(conn, now, ids)
    try:
        found += _lapsed(conn, cfg, s, today, ids, followed)
    except sqlite3.OperationalError:
        pass
    agenda = _agenda(conn, cfg, today, ids, owners, owner_handles)

    try:
        states = nudge_state.state_for(conn)
    except sqlite3.OperationalError:
        states = {}
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    found.sort(key=lambda n: (rank[n["kind"]], n["_sort"]))
    shown, hidden = [], []
    for n in found:
        st = states.get(n["key"])
        state = None
        if st is not None:
            state = {"state": st["state"], "until": st["until"], "note": st["note"]}
            if st["state"] == "dismissed" or (st["until"] or "") > today.isoformat():
                hidden.append((n, state))
                continue
            state["expired"] = True
        shown.append((n, state))
    nudges_out, hidden_out = [], []
    counter = 0
    for bucket, src in ((nudges_out, shown), (hidden_out, hidden)):
        for n, state in src:
            counter += 1
            item = {k: v for k, v in n.items() if not k.startswith("_")}
            item = {"id": f"N{counter}", **item, "state": state}
            bucket.append(item)
            ids.index[item["id"]] = {"type": "nudge", **item}
    if not include_hidden:
        for h in hidden_out:
            ids.index.pop(h["id"], None)
        hidden_out = []

    pack = {"pack_version": PACK_VERSION, "today": today.isoformat(),
            "as_of": as_of_date(conn, cfg), "settings": s,
            "nudges": nudges_out, "agenda": agenda, "hidden": hidden_out,
            "notes": notes, "index": ids.index}
    pack["pack_id"] = pack_id(pack)
    return pack


def pack_id(pack: dict) -> str:
    """sha256 of the pack's content (everything but the id itself)."""
    body = {k: v for k, v in pack.items() if k != "pack_id"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- lines, check

def nudge_line(item: dict) -> str:
    """One durable line for an N#: its key, title and detail (so every number
    a brief copies is here), and the ids it rests on."""
    state = ""
    if item.get("state"):
        st = item["state"]
        state = (f" · {st['state']}" + (f" until {st['until']}" if st.get("until") else "")
                 + (" (expired)" if st.get("expired") else ""))
    return (f"nudge {item['kind']} · {item['key']} · {item['title']} · {item['detail']}"
            f" · rests on {', '.join(item['ids'])}{state}")


def lines(pack: dict) -> dict[str, str]:
    out = {}
    for cid, item in pack["index"].items():
        out[cid] = nudge_line(item) if item.get("type") == "nudge" else index_line(item)
    return out


def check(text: str, pack: dict) -> dict:
    """check_text over a saved pack: ids must be in its index, and every
    number or date a sentence states must appear in the lines it cites (or
    in the context every sentence may use: today's date, the disclaimer)."""
    context = [f"TODAY: {pack['today']}"]
    if any(n["kind"] == "nutrient" for n in pack["nudges"] + pack.get("hidden", [])):
        context.append(f"NUTRITION: {nutrients.DISCLAIMER}")
    return check_text(text, lines(pack), context)


def render_lines(pack: dict, *, show_hidden: bool = False) -> list[str]:
    out = [f"Nudges for {pack['today']}: {len(pack['nudges'])} shown, "
           f"{len(pack['hidden'])} hidden (snoozed or dismissed)"
           + (f"; database as of {pack['as_of']}" if pack.get("as_of") else "")]
    if not pack["nudges"] and not pack["agenda"]:
        out.append("  nothing due, no birthday near, nothing stale.")
    for n in pack["nudges"]:
        out += [f"  [{n['id']}] {n['title']}",
                f"       {n['detail']}",
                f"       key {n['key']} · rests on {', '.join(n['ids'])}"
                + (" · snooze expired" if n.get("state") else "")]
    if pack["agenda"]:
        out.append("Agenda (calendar, observed):")
        for a in pack["agenda"]:
            when = f"{a['date']} {a['time'] or 'all day':>7}"
            who = f" with {', '.join(a['with'])}" if a["with"] else ""
            out.append(f"  [{a['id']}] {when}  {a['title']}{who}"
                       + (" (cancelled)" if a["cancelled"] else ""))
    if show_hidden and pack["hidden"]:
        out.append("Hidden:")
        for n in pack["hidden"]:
            st = n["state"]
            how = st["state"] + (f" until {st['until']}" if st.get("until") else "")
            out.append(f"  [{n['id']}] {n['title']} — {how} · key {n['key']}")
    for note in pack["notes"]:
        out.append(f"note: {note}")
    out.append("Sources:")
    for cid, line in lines(pack).items():
        if not cid.startswith("N"):
            out.append(f"  {cid}: {line}")
    return out

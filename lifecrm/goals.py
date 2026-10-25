"""Goal progress: every open `plan` with a `data.goal` measured against the
daily series it names (lifecrm/series.py), period by period.

Semantics (docs/contract.md, "Goals"):

- the goal starts at `goal.start` or the plan's journal date; earlier periods
  never count; periods are calendar-aligned (Monday-Sunday weeks, calendar
  months, journal days); the first period is partial and never enters a
  streak;
- a CUMULATIVE metric (a count or sum) is met as soon as the period's value
  reaches a ">=" target, even with unknown days; it is missed only when the
  period is over and every day was known; otherwise undecided (None).
  `projected` = value / elapsed fraction, for the current period. A "<="
  target fails as soon as the value exceeds it and is met only at the end
  with full coverage;
- a LEVEL (sleep, mood, energy) reports its observed mean and coverage and
  `above_target` as a running comparison; it is decided only when the period
  is over and at least `min_known` days are known;
- the streak counts consecutive complete periods met, newest first, and
  says what ended it: a miss, or coverage;
- a closed plan ends the goal; an edited target applies to every period.

Reads only. The rows behind the current period's value come back as
references for the pack to cite.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from . import series
from .config import Config
from .domain import entities as entities_mod
from .domain import goals as rules
from .domain.util import DomainError

HISTORY = 12                   # complete periods a streak may look back over


# ---------------------------------------------------------------- periods

def period_bounds(per: str, day: date) -> tuple[date, date]:
    if per == "day":
        return day, day
    if per == "week":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=6)
    first = day.replace(day=1)
    nxt = (first.replace(year=first.year + 1, month=1) if first.month == 12
           else first.replace(month=first.month + 1))
    return first, nxt - timedelta(days=1)


def period_label(per: str, start: date) -> str:
    if per == "day":
        return start.isoformat()
    if per == "week":
        y, w, _ = start.isocalendar()
        return f"{y}-W{w:02d}"
    return start.strftime("%Y-%m")


def periods_between(per: str, start: date, today: date, *, history: int = HISTORY
                    ) -> list[dict]:
    """The periods from the goal's start through the one holding today, the
    first clipped to the start date; at most `history` complete ones."""
    out = []
    cursor = start
    while cursor <= today:
        p_start, p_end = period_bounds(per, cursor)
        out.append({"label": period_label(per, p_start),
                    "start": max(p_start, start).isoformat(), "end": p_end.isoformat(),
                    "partial": p_start < start, "complete": p_end < today})
        cursor = p_end + timedelta(days=1)
    if len(out) > history + 1:
        out = out[-(history + 1):]
    return out


def _ndays(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days + 1


# ---------------------------------------------------------------- evaluation

def evaluate(s: series.Series, goal: dict, period: dict, today: date) -> dict:
    """One period of one goal, by the metric's aggregation (module docstring)."""
    end = min(period["end"], today.isoformat())
    pv = s.period(period["start"], end)
    agg = rules.aggregation(goal)
    target, op = goal["target"], goal["op"]
    out = {"label": period["label"], "start": period["start"], "end": period["end"],
           "partial": period["partial"], "complete": period["complete"],
           "known": pv["known"], "days_elapsed": _ndays(period["start"], end),
           "days": _ndays(period["start"], period["end"]), "agg": agg}
    if agg in ("sum", "count"):
        value = pv["value"] if pv["value"] is not None else 0.0
        out["value"] = value
        full = period["complete"] and pv["complete"]
        if op == ">=":
            out["met"] = True if value >= target else (False if full else None)
        else:
            out["met"] = False if value > target else (True if full else None)
        if not period["complete"] and pv["known"] > 0 and out["days"] > 0:
            elapsed = out["days_elapsed"] / out["days"]
            out["projected"] = round(value / elapsed, 2) if elapsed > 0 else None
    else:
        mean = pv["value"]
        out["observed_mean"] = mean
        out["above_target"] = (None if mean is None else
                               (mean >= target if op == ">=" else mean <= target))
        out["min_known"] = goal["min_known"]
        out["met"] = (bool(out["above_target"])
                      if period["complete"] and mean is not None
                      and pv["known"] >= goal["min_known"] else None)
    return out


def _streak(s: series.Series, goal: dict, complete: list[dict], today: date) -> dict:
    count, ended_by = 0, None
    for p in reversed(complete):
        if p["partial"]:
            ended_by = "start"
            break
        met = evaluate(s, goal, p, today)["met"]
        if met is True:
            count += 1
            continue
        ended_by = "missed" if met is False else "coverage"
        break
    return {"count": count, "ended_by": ended_by}


def _series_key(conn: sqlite3.Connection, goal: dict) -> tuple[str | None, str | None]:
    """The series a goal measures; (None, error) when a named person cannot be
    resolved without guessing."""
    metric = goal["metric"]
    if metric != "contact":
        return metric, None
    found = entities_mod.match(conn, goal["entity"], "person")
    if found["status"] not in ("exact", "alias") or len(found["matches"]) != 1:
        names = ", ".join(m["name"] for m in found.get("matches", [])[:4])
        return None, (f"'{goal['entity']}' does not name exactly one person"
                      + (f" (candidates: {names})" if names else "") + " — never guessed")
    return f"contact:{found['matches'][0]['id']}", None


def open_goals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    from .review import NOT_DERIVED
    return conn.execute(
        "SELECT e.uuid, e.journal_date, e.title, e.data FROM entries e "
        "JOIN entry_kinds k ON k.id = e.kind_id "
        "WHERE k.name = 'plan' AND e.status = 'active' AND e.closed_at IS NULL "
        "AND json_valid(e.data) AND json_extract(e.data, '$.goal') IS NOT NULL "
        f"AND {NOT_DERIVED} ORDER BY e.journal_date, e.id").fetchall()


def progress(conn: sqlite3.Connection, cfg: Config, today: date | str, *,
             history: int = HISTORY) -> list[dict]:
    """Every open goal: its current period, the last complete one, its streak,
    and the rows behind the current value."""
    if isinstance(today, str):
        today = date.fromisoformat(today)
    out = []
    for row in open_goals(conn):
        base = {"uuid": row["uuid"], "title": row["title"], "date": row["journal_date"]}
        try:
            goal = rules.validate_goal(json.loads(row["data"])["goal"])
        except (DomainError, ValueError, KeyError, TypeError) as e:
            out.append({**base, "goal": None, "error": f"unreadable goal: {e}"})
            continue
        start = date.fromisoformat(goal.get("start") or row["journal_date"])
        item = {**base, "goal": goal, "start": start.isoformat(), "agg": rules.aggregation(goal)}
        if start > today:
            out.append({**item, "error": f"starts {start.isoformat()}, after today"})
            continue
        key, err = _series_key(conn, goal)
        if err:
            out.append({**item, "error": err})
            continue
        periods = periods_between(goal["per"], start, today, history=history)
        s = series.daily(conn, cfg, key, periods[0]["start"], today.isoformat())
        if goal["metric"] == "spending" and goal.get("currency") and s.unit \
                and s.unit != goal["currency"]:
            item["note"] = (f"the goal is in {goal['currency']} but the imported "
                            f"transactions are in {s.unit}")
        current = evaluate(s, goal, periods[-1], today)
        complete = [p for p in periods[:-1] if p["complete"]]
        previous = evaluate(s, goal, complete[-1], today) if complete else None
        item.update({
            "series": {"key": key, "label": s.label, "unit": s.unit, "tier": s.tier},
            "current": current, "previous": previous,
            "streak": _streak(s, goal, complete, today),
            "refs": [ref for d in series._days(current["start"],
                                               min(current["end"], today.isoformat()))
                     for ref in s.refs.get(d, [])],
            "error": None,
        })
        if goal["per"] == "day":
            week_start, _ = period_bounds("week", today)
            days = [d for d in series._days(max(week_start, start).isoformat(),
                                            today.isoformat()) if d in s.known]
            met = [d for d in days if (s.values[d] >= goal["target"] if goal["op"] == ">="
                                        else s.values[d] <= goal["target"])]
            item["week"] = {"days_met": len(met), "days_known": len(days),
                            "start": max(week_start, start).isoformat()}
        out.append(item)
    return out


def in_period(conn: sqlite3.Connection, cfg: Config, start: str, end: str, *,
              history: int = HISTORY) -> list[dict]:
    """For a review pack: every open goal's periods that overlap [start, end],
    decided as of the day after `end` (so a period ending on the pack's last
    day is complete), with the streak as of then."""
    after = date.fromisoformat(end) + timedelta(days=1)
    out = []
    for row in open_goals(conn):
        base = {"uuid": row["uuid"], "title": row["title"], "date": row["journal_date"]}
        try:
            goal = rules.validate_goal(json.loads(row["data"])["goal"])
        except (DomainError, ValueError, KeyError, TypeError) as e:
            out.append({**base, "goal": None, "error": f"unreadable goal: {e}", "periods": []})
            continue
        g_start = date.fromisoformat(goal.get("start") or row["journal_date"])
        item = {**base, "goal": goal, "start": g_start.isoformat(),
                "agg": rules.aggregation(goal), "periods": [], "error": None}
        if g_start > date.fromisoformat(end):
            continue                       # it did not exist yet: not this period's goal
        key, err = _series_key(conn, goal)
        if err:
            out.append({**item, "error": err})
            continue
        periods = periods_between(goal["per"], g_start, after, history=history)
        overlap = [p for p in periods if p["start"] <= end and p["end"] >= start]
        if not overlap:
            continue
        s = series.daily(conn, cfg, key, periods[0]["start"], end)
        item["series"] = {"key": key, "label": s.label, "unit": s.unit, "tier": s.tier}
        item["periods"] = [evaluate(s, goal, p, after) for p in overlap]
        complete = [p for p in periods if p["complete"] and p["end"] <= end]
        item["streak"] = _streak(s, goal, complete, after)
        item["refs"] = [ref for p in overlap for d in series._days(p["start"], min(p["end"], end))
                        for ref in s.refs.get(d, [])]
        out.append(item)
    return out


def describe_periods(item: dict) -> str:
    """One line for a review pack: the goal and each period it covers."""
    g = item.get("goal")
    if not g:
        return f"{item['title']}: {item.get('error')}"
    unit = item.get("series", {}).get("unit") or ""
    head = f"{item['title']} — {g['metric']} {g['op']} {g['target']:g}{' ' + unit if unit else ''}" \
           f" per {g['per']}"
    if item.get("error"):
        return f"{head}: {item['error']}"
    parts = []
    for p in item["periods"]:
        word = {True: "met", False: "missed", None: "undecided"}[p["met"]]
        if p["agg"] in ("sum", "count"):
            value = f"{p['value']:g} of {g['target']:g}"
        elif p["observed_mean"] is None:
            value = "nothing known"
        else:
            value = f"averaged {p['observed_mean']:g}{' ' + unit if unit else ''}"
        cov = f", {p['known']} of {p['days_elapsed']} day(s) known" \
            if p["known"] < p["days_elapsed"] else ""
        parts.append(f"{p['label']}: {value} ({word}{cov})")
    streak = item.get("streak") or {}
    tail = f"; streak {streak['count']}" if streak.get("count") else ""
    return f"{head}: " + "; ".join(parts) + tail


def describe(item: dict) -> str:
    """One line a pack or a nudge can print: the goal and how it stands."""
    g = item.get("goal")
    if not g:
        return f"{item['title']}: {item.get('error')}"
    unit = item.get("series", {}).get("unit") or ""
    target = f"{g['target']:g}{(' ' + unit) if unit and unit != '/10' else unit or ''}"
    head = f"{item['title']} — {g['metric']} {g['op']} {target} per {g['per']}"
    if item.get("error"):
        return f"{head}: {item['error']}"
    c = item["current"]
    if c["agg"] in ("sum", "count"):
        state = f"{c['value']:g} of {g['target']:g} so far this {g['per']} ({c['label']})"
        if c["met"] is True:
            state += " — met"
        elif c["met"] is False:
            state += " — missed"
        if c.get("projected") is not None and c["met"] is None:
            state += f", on pace for {c['projected']:g}"
    else:
        if c["observed_mean"] is None:
            state = f"nothing known yet this {g['per']} ({c['label']})"
        else:
            state = (f"averaging {c['observed_mean']:g}{(' ' + unit) if unit else ''} over "
                     f"{c['known']} known day(s) this {g['per']} ({c['label']}), "
                     f"{'above' if c['above_target'] else 'below'} target")
    if c["known"] < c["days_elapsed"]:
        state += f"; {c['known']} of {c['days_elapsed']} day(s) known"
    prev = item.get("previous")
    tail = ""
    if prev is not None:
        word = {True: "met", False: "missed", None: "undecided"}[prev["met"]]
        tail = f"; last {g['per']} ({prev['label']}) {word}"
    streak = item["streak"]
    if streak["count"]:
        tail += f"; streak {streak['count']}"
    return f"{head}: {state}{tail}"

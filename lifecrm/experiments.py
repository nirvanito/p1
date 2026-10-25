"""Self-experiments, measured: a plan's `data.experiment` against its outcome.

The change runs for `days` days from `start` (the plan's journal date when
not given); the baseline is the `baseline_days` days just before it. Each
side is the outcome series' mean over the days it KNOWS
(lifecrm/series.py) — for a count or a sum that is its average per known
day — so a week without a sensor is never a week of zero sleep. A side with
fewer than `min_known` known days is "not enough known days", and is said
so rather than compared.

What it reports is a comparison of two windows of your own records,
co-occurrence and never cause: the change, the weather, a cold and a new job
all overlap. The wording says "while", never "because". Reads only.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from .config import Config
from .domain import experiments as rules
from .domain.util import DomainError


def _plans(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT e.uuid, e.title, e.journal_date, e.data, e.closed_at, e.closure "
        "FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        "WHERE e.status = 'active' AND k.name = 'plan' AND e.data IS NOT NULL "
        "AND json_valid(e.data) AND json_extract(e.data, '$.experiment') IS NOT NULL "
        "ORDER BY e.journal_date, e.id").fetchall()


def evaluate(conn: sqlite3.Connection, cfg: Config, plan, today: date) -> dict:
    """One plan's experiment as of `today`: the windows, each side's mean and
    known days, the change, the status, and the rows behind each side."""
    from . import series as series_mod
    data = json.loads(plan["data"])
    out = {"uuid": plan["uuid"], "title": plan["title"], "date": plan["journal_date"],
           "closed": bool(plan["closed_at"]), "closure": plan["closure"]}
    try:
        exp = rules.validate_experiment(data["experiment"])
        meta = series_mod.info(exp["outcome"])
    except DomainError as e:
        return {**out, "error": str(e)}
    start = date.fromisoformat(exp.get("start") or plan["journal_date"])
    end = start + timedelta(days=exp["days"] - 1)
    base_end = start - timedelta(days=1)
    base_start = start - timedelta(days=exp["baseline_days"])
    upto = min(end, today)
    status = ("planned" if today < start else "running" if today <= end else "done")
    ser = series_mod.daily(conn, cfg, exp["outcome"], base_start.isoformat(),
                           max(upto, base_end).isoformat())

    def side(lo: date, hi: date) -> dict:
        if hi < lo:
            return {"start": lo.isoformat(), "end": hi.isoformat(), "known": 0,
                    "mean": None, "refs": [], "days": []}
        days = [d for d in series_mod._days(lo.isoformat(), hi.isoformat()) if d in ser.known]
        vals = [ser.values.get(d, 0.0) for d in days]
        return {"start": lo.isoformat(), "end": hi.isoformat(), "known": len(days),
                "mean": round(sum(vals) / len(vals), 2) if vals else None,
                "refs": [r for d in days for r in ser.refs.get(d, [])], "days": days}

    base = side(base_start, base_end)
    during = side(start, upto)
    enough = base["known"] >= exp["min_known"] and during["known"] >= exp["min_known"]
    delta = (round(during["mean"] - base["mean"], 2)
             if during["mean"] is not None and base["mean"] is not None else None)
    pct = (round(100 * delta / abs(base["mean"]), 1)
           if delta is not None and base["mean"] not in (None, 0) else None)
    went = None
    if delta is not None:
        went = "up" if delta > 0 else "down" if delta < 0 else "unchanged"
    return {**out, "experiment": exp, "label": meta["label"], "unit": meta["unit"],
            "tier": meta["tier"], "agg": meta["agg"], "status": status,
            "start": start.isoformat(), "end": end.isoformat(),
            "elapsed_days": max(0, (upto - start).days + 1) if status != "planned" else 0,
            "baseline": base, "during": during, "enough": enough, "delta": delta,
            "delta_pct": pct, "went": went,
            "as_hoped": (went == exp.get("hope")) if exp.get("hope") and went else None}


def current(conn: sqlite3.Connection, cfg: Config, today: date, *,
            include_closed: bool = False) -> list[dict]:
    """Every experiment plan (open ones, or all), evaluated as of `today`."""
    out = []
    for plan in _plans(conn):
        if plan["closed_at"] and not include_closed:
            continue
        out.append(evaluate(conn, cfg, plan, today))
    return out


def overlapping(conn: sqlite3.Connection, cfg: Config, start: str, end: str) -> list[dict]:
    """Experiments whose change overlaps [start, end], evaluated as of the
    day after `end` (the review pack's rule for goals)."""
    as_of = date.fromisoformat(end) + timedelta(days=1)
    out = []
    for e in current(conn, cfg, as_of, include_closed=True):
        if e.get("error"):
            out.append(e)
        elif e["start"] <= end and e["end"] >= start:
            out.append(e)
    return out


def _num(v, unit: str) -> str:
    if v is None:
        return "n/a"
    text = f"{v:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip() if unit and unit != "/10" else text + (unit or "")


def describe(e: dict) -> str:
    """One line with every number a brief may repeat."""
    if e.get("error"):
        return f"{e['title']}: cannot be measured — {e['error']}"
    x, b, d = e["experiment"], e["baseline"], e["during"]
    per = " a day" if e["agg"] in ("sum", "count") else ""
    head = f"{x['change']} ({e['label']}, {e['start']} to {e['end']}, {e['status']})"
    if e["status"] == "planned":
        return f"{head}: starts {e['start']}; baseline so far {_num(b['mean'], e['unit'])}{per} " \
               f"over {b['known']} known day(s)"
    line = (f"{head}: {_num(d['mean'], e['unit'])}{per} over {d['known']} known day(s) "
            f"since {e['start']}, against {_num(b['mean'], e['unit'])}{per} over "
            f"{b['known']} known day(s) from {b['start']} to {b['end']}")
    if e["delta"] is not None:
        line += f" — {e['went']} {_num(abs(e['delta']), e['unit'])}"
        if e["delta_pct"] is not None:
            line += f" ({abs(e['delta_pct']):g}%)"
    if not e["enough"]:
        line += f"; not enough known days to compare (at least {x['min_known']} each side)"
    line += "; co-occurrence over two windows, not cause"
    return line


def render_lines(items: list[dict], today: date) -> list[str]:
    out = [f"Experiments as of {today.isoformat()} (plans with data.experiment):"]
    if not items:
        out.append("  none — a plan carrying data.experiment starts one (docs/contract.md)")
    for e in items:
        out.append(f"  - {describe(e)} · entry {e['uuid']}")
    return out

"""Weekday profile: each series averaged by day of the week.

Exist's first insight was a chart of your averages for each day of the
week — the Wednesday dip, the weekend that sleeps in. Here every series in
the registry (lifecrm/series.py) is averaged per weekday over the days it
KNOWS in a window: a weekday with fewer than MIN_PER_WEEKDAY known days is
"not enough days", never a guess, and an unknown day is never a zero.
Reads only; each series cites the rows behind it.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .config import Config

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DEFAULT_KEYS = ("mood", "energy", "sleep", "sleep_hours", "steps", "workouts", "spending",
                "texts", "contacts")
MIN_PER_WEEKDAY = 3
MIN_NONZERO_DAYS = 6
DEFAULT_DAYS = 90


def profile(conn: sqlite3.Connection, cfg: Config, key: str, start: str, end: str) -> dict:
    """{'key', 'label', 'unit', 'tier', 'agg', 'weekdays': [7 x {name, mean,
    known}], 'low', 'high', 'refs', 'known_days'} — per weekday the mean
    over its known days (a count or sum is its per-day mean)."""
    from . import series as series_mod
    s = series_mod.daily(conn, cfg, key, start, end)
    buckets: list[list[float]] = [[] for _ in range(7)]
    known_days = []
    for d in series_mod._days(start, end):
        if d in s.known:
            buckets[date.fromisoformat(d).weekday()].append(s.values.get(d, 0.0))
            known_days.append(d)
    rows = []
    for i, vals in enumerate(buckets):
        enough = len(vals) >= MIN_PER_WEEKDAY
        rows.append({"weekday": WEEKDAYS[i], "known": len(vals),
                     "mean": round(sum(vals) / len(vals), 2) if enough else None})
    judged = [r for r in rows if r["mean"] is not None]
    low = min(judged, key=lambda r: (r["mean"], WEEKDAYS.index(r["weekday"]))) \
        if len(judged) >= 2 else None
    high = max(judged, key=lambda r: (r["mean"], -WEEKDAYS.index(r["weekday"]))) \
        if len(judged) >= 2 else None
    return {"key": key, "label": s.label, "unit": s.unit, "tier": s.tier, "agg": s.agg,
            "start": start, "end": end, "weekdays": rows, "known_days": len(known_days),
            "nonzero_days": sum(1 for d in known_days if s.values.get(d)),
            "low": low, "high": high,
            "refs": [r for d in known_days for r in s.refs.get(d, [])],
            "first_date": known_days[0] if known_days else None,
            "last_date": known_days[-1] if known_days else None}


def profiles(conn: sqlite3.Connection, cfg: Config, start: str, end: str,
             keys=DEFAULT_KEYS) -> list[dict]:
    """The profile of each key that knows enough weekdays to compare and
    holds at least MIN_NONZERO_DAYS days with a value (a count known to be
    zero all year is no rhythm); sleep from the journal only when the sensor
    knows nothing."""
    out = []
    for key in keys:
        p = profile(conn, cfg, key, start, end)
        if p["low"] is None or p["nonzero_days"] < MIN_NONZERO_DAYS:
            continue
        out.append(p)
    if any(p["key"] == "sleep" for p in out):
        out = [p for p in out if p["key"] != "sleep_hours"]
    return out


def window(today: date, days: int = DEFAULT_DAYS) -> tuple[str, str]:
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def _num(v, unit: str) -> str:
    if v is None:
        return "not enough days"
    text = f"{v:,.2f}".rstrip("0").rstrip(".")
    if unit == "/10":
        return text + "/10"
    return f"{text} {unit}".strip()


def describe(p: dict) -> str:
    per = " a day" if p["agg"] in ("sum", "count") else ""
    days = "; ".join(f"{r['weekday'][:3]} {_num(r['mean'], p['unit'])} ({r['known']})"
                     for r in p["weekdays"])
    line = (f"{p['label']} by weekday, {p['start']} to {p['end']}, {p['known_days']} known "
            f"day(s){per and ', per-day mean'}: {days}")
    if p["low"] and p["high"] and p["low"]["weekday"] != p["high"]["weekday"]:
        line += (f"; lowest {p['low']['weekday']} {_num(p['low']['mean'], p['unit'])}, "
                 f"highest {p['high']['weekday']} {_num(p['high']['mean'], p['unit'])}")
    return line


def render_lines(items: list[dict]) -> list[str]:
    if not items:
        return ["No series knows enough days on at least two weekdays "
                f"({MIN_PER_WEEKDAY} each) to compare."]
    return ["Weekday averages (over known days only; (n) = known days):"] + \
        [f"  - {describe(p)}" for p in items]

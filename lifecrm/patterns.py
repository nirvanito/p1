"""Patterns: co-occurrence between daily series. EXPERIMENTAL.

`lifecrm patterns` tests every pair of daily series (lifecrm/series.py) for
an association the same day and the next day, and reports the pairs that
survive a false-discovery control. It is statistics over your own records,
no model; and it is experimental: off everywhere by default until its
validation suite (tests/test_patterns_validation.py, docs/patterns.md) has
shown the error control holds on autocorrelated, trending, tied and gappy
series like the real ones.

The method, and why each step is there:

- everything is done on CALENDAR days: a lag of one pairs day d with day
  d + 1 and exists only when both are known; the trend is fitted on the day
  index; the shift check moves one series by whole days across the window's
  grid. Positions in a compressed array would turn a gap of a week into a
  neighbour;
- each pair is reduced to the days both series know and linearly detrended
  on the day index, because a shared slow trend is not a day-to-day
  association; then Spearman's rho (Pearson on average ranks, ties shared),
  which does not care about scale or outliers;
- the p-value is Fisher's z on an EFFECTIVE sample size corrected for serial
  correlation (Bretherton et al. 1999: n_eff = n (1 - r1 r2) / (1 + r1 r2),
  r1 and r2 the lag-1 autocorrelations of the two residual series). Day
  series are autocorrelated, and treating 90 dependent days as 90
  independent ones is the classic way to see patterns that are not there;
- Benjamini-Hochberg over the WHOLE family — every pair and lag actually
  tested, reported as `family_size` — at q <= 0.10, plus an effect floor
  (|rho| >= 0.4) and a sample floor (n >= 30 paired days);
- for the pairs reported, a circular day-shift null (every shift of 7 or
  more days of one series against the other, which preserves each series'
  own autocorrelation) is computed and printed as `shift_p`. It is a check,
  not the inference: a shuffle-based p has a floor of 1/(shifts + 1) that
  no Benjamini-Hochberg threshold of a ~270-test family could rest on.

What a reported pair means: in this window, high days of one tended to be
high (or low) days of the other. Co-occurrence, never cause — a third thing
(a trip, a sick week) moves both. The wording says so every time, and a
health-tier series carries the not-medical line.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import date, timedelta

from . import series as series_mod
from .config import Config
from .domain.util import DomainError

STATUS = "experimental"
DEFAULT_DAYS = 90
MAX_DAYS = 365
MIN_DAYS = 30
MIN_EFFECT = 0.4
Q = 0.10
MIN_SHIFT = 7
LAGS = (0, 1)
NOT_MEDICAL = ("A health series here is a comparison with your own record, not medical "
               "advice; a pattern is co-occurrence, never cause.")


# ---------------------------------------------------------------- statistics

def ranks(values: list[float]) -> list[float]:
    """Average ranks, ties sharing their mean rank (1-based)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        mean_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            out[order[k]] = mean_rank
        i = j + 1
    return out


def pearson(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return 0.0
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb)


def detrend(points: list[tuple[int, float]]) -> list[float]:
    """Residuals of a least-squares line on the day index."""
    n = len(points)
    if n < 3:
        return [v for _, v in points]
    mx = sum(i for i, _ in points) / n
    my = sum(v for _, v in points) / n
    sxx = sum((i - mx) ** 2 for i, _ in points)
    if sxx == 0:
        return [v - my for _, v in points]
    slope = sum((i - mx) * (v - my) for i, v in points) / sxx
    return [v - (my + slope * (i - mx)) for i, v in points]


def lag1_autocorr(points: list[tuple[int, float]]) -> float:
    """Lag-1 autocorrelation over CONSECUTIVE calendar days only."""
    by_day = dict(points)
    pairs = [(by_day[i], by_day[i + 1]) for i in by_day if i + 1 in by_day]
    if len(pairs) < 3:
        return 0.0
    return pearson([a for a, _ in pairs], [b for _, b in pairs])


def fisher_p(rho: float, n_eff: float) -> float:
    """Two-sided p for Spearman's rho via Fisher's z, with the 1.06
    small-sample factor, on an effective sample size."""
    if n_eff <= 3:
        return 1.0
    r = max(-0.999999, min(0.999999, rho))
    z = math.atanh(r) * math.sqrt((n_eff - 3) / 1.06)
    return math.erfc(abs(z) / math.sqrt(2))


def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """Step-up adjusted values (q), in the input order."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = min(1.0, running)
    return q


def effective_n(n: int, r1: float, r2: float) -> float:
    """Bretherton's effective sample size, taken CONSERVATIVELY: each
    autocorrelation estimate is lifted by one standard error (1/sqrt(n)) and
    a negative one counts as none. A sample lag-1 autocorrelation of ninety
    days has a standard error near 0.1, and an estimate on the low side would
    otherwise hand a borderline pair a p-value it has not earned — the
    simulations in tests/test_patterns_validation.py showed exactly that."""
    se = 1.0 / math.sqrt(max(n, 1))
    a = min(0.99, max(0.0, r1) + se)
    b = min(0.99, max(0.0, r2) + se)
    prod = a * b
    return max(4.0, min(float(n), n * (1 - prod) / (1 + prod)))


# ---------------------------------------------------------------- pairs

def _grid(start: str, end: str) -> list[str]:
    return series_mod._days(start, end)


def _indexed(s: series_mod.Series, grid: list[str]) -> dict[int, float]:
    return {i: s.values[d] for i, d in enumerate(grid) if d in s.known}


def pair_stats(x: dict[int, float], y: dict[int, float], lag: int, *, min_days: int) -> dict:
    """x on day i against y on day i + lag, over the days both know."""
    idx = sorted(i for i in x if i + lag in y)
    n = len(idx)
    out = {"lag": lag, "n": n}
    if n < min_days:
        out["skipped"] = f"{n} paired day(s), fewer than {min_days}"
        return out
    xs = detrend([(i, x[i]) for i in idx])
    ys = detrend([(i, y[i + lag]) for i in idx])
    rho = pearson(ranks(xs), ranks(ys))
    r1 = lag1_autocorr(list(zip(idx, xs)))
    r2 = lag1_autocorr(list(zip(idx, ys)))
    n_eff = effective_n(n, r1, r2)
    out.update({"rho": round(rho, 4), "r1": round(r1, 3), "r2": round(r2, 3),
                "n_eff": round(n_eff, 1), "p": fisher_p(rho, n_eff)})
    # the contrast: raw y on the lowest and highest third of x days
    order = sorted(idx, key=lambda i: x[i])
    third = max(1, n // 3)
    low, high = order[:third], order[-third:]
    out["contrast"] = {"x_low_days": third, "y_on_low_x": round(sum(y[i + lag] for i in low) / third, 2),
                       "y_on_high_x": round(sum(y[i + lag] for i in high) / third, 2)}
    out["days"] = idx
    return out


def shift_p(x: dict[int, float], y: dict[int, float], lag: int, observed: float,
            grid_len: int, *, min_days: int) -> tuple[float | None, int]:
    """The circular day-shift check: y moved by every whole-day shift of
    MIN_SHIFT or more against x; how often the shifted |rho| reaches the
    observed one. Preserves each series' own autocorrelation."""
    count, shifts = 0, 0
    for s in range(MIN_SHIFT, grid_len - MIN_SHIFT + 1):
        shifted = {(i + s) % grid_len: v for i, v in y.items()}
        st = pair_stats(x, shifted, lag, min_days=min_days)
        if "rho" not in st:
            continue
        shifts += 1
        if abs(st["rho"]) >= abs(observed) - 1e-12:
            count += 1
    if shifts == 0:
        return None, 0
    return round((count + 1) / (shifts + 1), 4), shifts


# ---------------------------------------------------------------- the finder

def family(conn: sqlite3.Connection, cfg: Config, start: str, end: str, *,
           keys: list[str] | None = None, min_days: int = MIN_DAYS
           ) -> dict[str, series_mod.Series]:
    """The series in play: the registry's pattern defaults (or `keys`), each
    knowing at least `min_days` days in the window."""
    wanted = keys or [k for k, m in series_mod.KEYS.items() if m["pattern_default"]]
    out = {}
    for key in wanted:
        s = series_mod.daily(conn, cfg, key, start, end)
        if len(s.known) >= min_days:
            out[key] = s
    return out


def find(conn: sqlite3.Connection, cfg: Config, *, end: date | str, days: int = DEFAULT_DAYS,
         min_days: int = MIN_DAYS, min_effect: float = MIN_EFFECT, q: float = Q,
         keys: list[str] | None = None, check: bool = True) -> dict:
    """Every pair of series in the family at lags 0 and 1 (both directions),
    Fisher-z on an effective n, Benjamini-Hochberg over the whole family; the
    pairs that clear q, the effect floor and the sample floor are `items`,
    each with the day-shift check. `tested` lists everything, so a reader
    can see what was tried and why a pair was not reported."""
    if isinstance(end, str):
        end = date.fromisoformat(end)
    if not 14 <= days <= MAX_DAYS:
        raise DomainError(f"--days must be 14-{MAX_DAYS}, got {days}")
    start = end - timedelta(days=days - 1)
    grid = _grid(start.isoformat(), end.isoformat())
    fam = family(conn, cfg, start.isoformat(), end.isoformat(), keys=keys, min_days=min_days)
    indexed = {k: _indexed(s, grid) for k, s in fam.items()}
    names = sorted(fam)
    tested: list[dict] = []
    for a_i, a in enumerate(names):
        for b in names[a_i + 1:]:
            for lag in LAGS:
                pairs = [(a, b)] if lag == 0 else [(a, b), (b, a)]
                for x, y in pairs:
                    st = pair_stats(indexed[x], indexed[y], lag, min_days=min_days)
                    tested.append({"x": x, "y": y, **st})
    with_p = [t for t in tested if "p" in t]
    for t, qv in zip(with_p, benjamini_hochberg([t["p"] for t in with_p])):
        t["q"] = round(qv, 4)
        t["p"] = round(t["p"], 6)
    items = []
    for t in with_p:
        if abs(t["rho"]) < min_effect:
            t["reason"] = f"|rho| {abs(t['rho']):.2f} below the effect floor {min_effect}"
            continue
        if t["q"] > q:
            t["reason"] = f"q {t['q']:.3f} above {q}"
            continue
        t["reported"] = True
        item = dict(t)
        if check:
            sp, shifts = shift_p(indexed[t["x"]], indexed[t["y"]], t["lag"], t["rho"],
                                 len(grid), min_days=min_days)
            item["shift_p"], item["shifts"] = sp, shifts
        item["dates"] = [grid[i] for i in t["days"]]
        item["refs"] = {k: [r for i in t["days"] for r in fam[k].refs.get(grid[i], [])]
                        for k in (t["x"], t["y"])}
        item["wording"] = wording(fam[t["x"]], fam[t["y"]], item)
        item["tiers"] = {t["x"]: fam[t["x"]].tier, t["y"]: fam[t["y"]].tier}
        items.append(item)
    items.sort(key=lambda it: (it["q"], -abs(it["rho"]), it["x"], it["y"], it["lag"]))
    for t in tested:
        t.pop("days", None)
    notes = [f"{STATUS}: patterns are co-occurrence in this window, never cause; the "
             f"method is validated on simulated series only (docs/patterns.md)"]
    if any(fam[k].tier == "observed" and series_mod.KEYS.get(k, {}).get("lenses", set())
           & {"health"} for k in fam):
        notes.append(NOT_MEDICAL)
    return {"status": STATUS,
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
            "method": {"min_days": min_days, "min_effect": min_effect, "q": q,
                       "lags": list(LAGS), "detrend": "linear, on the day index",
                       "p": "Fisher z on an effective n (lag-1 autocorrelation)",
                       "fdr": "Benjamini-Hochberg over every pair tested"},
            "series": {k: {"label": s.label, "unit": s.unit, "tier": s.tier,
                           "known_days": len(s.known)} for k, s in fam.items()},
            "family_size": len(with_p), "tested": tested, "items": items, "notes": notes}


def wording(x: series_mod.Series, y: series_mod.Series, item: dict) -> str:
    when = "the next day" if item["lag"] else "the same day"
    direction = "higher" if item["rho"] > 0 else "lower"
    c = item["contrast"]
    return (f"{x.label} tends to go with {direction} {y.label} {when} in this window "
            f"(rho {item['rho']:+.2f} over {item['n']} paired days, q {item['q']:.3f}; "
            f"{y.label} averaged {c['y_on_low_x']:g} on the lowest-{x.label} days and "
            f"{c['y_on_high_x']:g} on the highest; co-occurrence, not cause)")


def render_lines(result: dict, *, show_all: bool = False) -> list[str]:
    w = result["window"]
    out = [f"Patterns ({STATUS.upper()}) · {w['start']} to {w['end']} · family of "
           f"{result['family_size']} tests over {len(result['series'])} series"]
    if not result["series"]:
        out.append(f"  no series knows {result['method']['min_days']} or more days in this "
                   f"window — nothing to test")
    for it in result["items"]:
        check = (f" · shift check p {it['shift_p']}" if it.get("shift_p") is not None
                 else "")
        out.append(f"  {it['wording']}{check}")
    if result["series"] and not result["items"]:
        out.append("  nothing clears the floors (effect, sample size, q) in this window — "
                   "no pattern is also a finding; --all lists every pair tested")
    if show_all:
        out.append("Tested:")
        for t in result["tested"]:
            why = ("reported" if t.get("reported") else
                   f"not reported: {t.get('reason') or t.get('skipped')}")
            stat = (f"rho {t['rho']:+.2f}, n {t['n']}, n_eff {t['n_eff']}, q {t.get('q', 1):.3f}"
                    if "rho" in t else f"n {t['n']}")
            out.append(f"  {t['x']} -> {t['y']} lag {t['lag']}: {stat} — {why}")
    for note in result["notes"]:
        out.append(note)
    return out


def seed_for(*parts) -> int:
    """A stable seed for anything random in a simulation: never hash()."""
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")

"""The validation gate for the pattern finder (docs/patterns.md).

What is measured: the false discovery rate is the expected false discovery
proportion over repetitions, where a repetition's proportion is false
discoveries / discoveries (0 when nothing is discovered); in an all-null
family it equals the probability of reporting anything. Each suite runs R
seeded repetitions of a 14-series, 90-day family and estimates the FDR as
the mean proportion with a Wilson 95 % interval.

The criterion: Benjamini-Hochberg controls the FDR AT OR BELOW q, and on an
all-null family it sits near q itself, so an estimate above q is expected
about half the time and proves nothing. The gate fails when the estimate is
SIGNIFICANTLY above q — the interval's lower bound over q — or grossly
above it (the point estimate over q + 0.05 with R = 200, q + 0.10 with the
regular run's R = 40).

Two strengths. The regular test run uses R = 40, so the suite stays fast;
LIFECRM_PATTERN_GATE=1 runs the full gate (R = 200, every suite, the
reserved seed set), which is what docs/patterns.md records.
Validation is separate from tuning: the development seeds ("dev") shaped the
method; the reserved seeds ("reserved") are only ever run after tuning
stops, and any later change to the family, floors or the null gets a fresh
reserved set.
"""

from __future__ import annotations

import math
import os
import random

import pytest

from lifecrm import patterns as mod

FULL = os.environ.get("LIFECRM_PATTERN_GATE", "").strip() not in ("", "0")
R = 200 if FULL else 40
SEEDS = "reserved" if FULL else "dev"
GROSS = 0.05 if FULL else 0.10           # point-estimate slack over q at this R
SERIES, DAYS = 14, 90


def ar1(rng, n, phi, *, missing=0.0, runs=False, ties=False, trend=0.0):
    out, v, skipping = {}, 0.0, 0
    for i in range(n):
        v = phi * v + rng.gauss(0, 1)
        if runs:
            if skipping:
                skipping -= 1
                continue
            if rng.random() < missing / 4:
                skipping = 3                       # a four-day hole
                continue
        elif rng.random() < missing:
            continue
        x = v + trend * i
        out[i] = round(x) if ties else x
    return out


def family(rng, *, phi, missing=0.0, runs=False, ties=False, trend=0.0, planted=None,
           days=DAYS):
    series = {f"s{k}": ar1(rng, days, phi, missing=missing, runs=runs, ties=ties, trend=trend)
              for k in range(SERIES)}
    if planted is not None:
        rho, lag = planted
        base, y = series["s0"], {}
        for i, v in base.items():
            if i + lag < days:
                y[i + lag] = rho * v + math.sqrt(1 - rho * rho) * rng.gauss(0, 1)
        series["s1"] = y
    return series


def run_family(series):
    names = sorted(series)
    tested = []
    for a_i, a in enumerate(names):
        for b in names[a_i + 1:]:
            for lag in mod.LAGS:
                for x, y in ([(a, b)] if lag == 0 else [(a, b), (b, a)]):
                    st = mod.pair_stats(series[x], series[y], lag, min_days=mod.MIN_DAYS)
                    tested.append((x, y, lag, st))
    with_p = [t for t in tested if "p" in t[3]]
    qs = mod.benjamini_hochberg([t[3]["p"] for t in with_p])
    return [(x, y, lag) for (x, y, lag, st), qv in zip(with_p, qs)
            if qv <= mod.Q and abs(st["rho"]) >= mod.MIN_EFFECT]


def wilson(p_hat, n, z=1.96):
    """(lower, upper) 95 % Wilson interval for a proportion."""
    denom = 1 + z * z / n
    centre = p_hat + z * z / (2 * n)
    spread = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))
    return (centre - spread) / denom, (centre + spread) / denom


def is_false(pair, planted) -> bool:
    """A discovery is false when it involves an INDEPENDENT series. With a
    planted pair, s0 and s1 are associated at every lag — a same-day
    association between autocorrelated series carries a real next-day echo
    (0.6 x 0.4 in population here) — so any pair between the two of them is
    a true, if dependent, association, not a false one."""
    x, y, _lag = pair
    return planted is None or {x, y} != {"s0", "s1"}


def estimate(name, **kw):
    """(FDR estimate, Wilson interval of P(any false), power) over R runs."""
    planted = kw.get("planted")
    truth = {("s0", "s1", planted[1])} if planted else set()
    fdp_sum, any_false, hits = 0.0, 0, 0
    for r in range(R):
        rng = random.Random(mod.seed_for(SEEDS, name, r))
        found = run_family(family(rng, **kw))
        false = [f for f in found if is_false(f, planted)]
        fdp_sum += (len(false) / len(found)) if found else 0.0
        any_false += bool(false)
        hits += any(f in truth for f in found)
    return fdp_sum / R, wilson(any_false / R, R), hits / R


NULL_SUITES = {
    "independent": dict(phi=0.0),
    "ar1 phi 0.4": dict(phi=0.4),
    "ar1 phi 0.8": dict(phi=0.8),
    "shared trend": dict(phi=0.4, trend=0.03),
    "heavy ties": dict(phi=0.4, ties=True),
    "missing 30% at random": dict(phi=0.4, missing=0.3),
    "missing in runs": dict(phi=0.4, missing=0.3, runs=True),
}


@pytest.mark.parametrize("name", list(NULL_SUITES) if FULL else
                         ["ar1 phi 0.4", "shared trend", "missing in runs"])
def test_false_discoveries_are_not_significantly_above_q_on_null_families(name):
    fdr, (lower, upper), _ = estimate(name, **NULL_SUITES[name])
    assert lower <= mod.Q, (f"{name}: P(any false) significantly above q — estimate "
                            f"{fdr:.3f}, interval {lower:.3f}..{upper:.3f}")
    assert fdr <= mod.Q + GROSS, f"{name}: FDR {fdr:.3f} grossly over q {mod.Q}"


def test_a_planted_association_is_usually_found():
    fdr, (lower, _), power = estimate("planted rho 0.5", phi=0.3, planted=(0.5, 0))
    assert power >= 0.80, f"power {power:.2f} for rho 0.5 over 90 days"
    assert lower <= mod.Q and fdr <= mod.Q + GROSS
    _, _, power1 = estimate("planted lag 1 rho 0.6", phi=0.3, planted=(0.6, 1))
    assert power1 >= 0.90


def test_the_discovery_set_is_stable_across_overlapping_windows():
    """Twelve 90-day windows sliding by a week over one simulated year of a
    family with one planted pair: the planted pair is found in most windows
    and no false pair (one involving an independent series) is found in more
    than one. The planted pair's own next-day echo may appear; it is counted
    separately and is not a false discovery."""
    rng = random.Random(mod.seed_for(SEEDS, "sliding"))
    year = family(rng, phi=0.4, planted=(0.6, 0), days=365)
    found_sets, false_counts, echo = [], {}, 0
    for w in range(12):
        lo = 7 * w
        window = {k: {i - lo: v for i, v in s.items() if lo <= i < lo + DAYS}
                  for k, s in year.items()}
        found = set(run_family(window))
        found_sets.append(found)
        for f in found - {("s0", "s1", 0)}:
            if is_false(f, (0.6, 0)):
                false_counts[f] = false_counts.get(f, 0) + 1
            else:
                echo += 1
    hits = sum(("s0", "s1", 0) in f for f in found_sets)
    assert hits >= 10, f"planted pair found in {hits} of 12 windows"
    assert not false_counts or max(false_counts.values()) <= 1, false_counts

"""The pattern finder (experimental): rank statistics on calendar days, a
p-value on an effective sample size, false-discovery control over the whole
family, and a planted association found while independent series are not."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import patterns as mod
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.domain import measurements
from lifecrm.domain.util import DomainError

runner = CliRunner()
END = date(2026, 10, 21)


# ---------------------------------------------------------------- statistics

def test_ranks_share_ties_and_pearson_is_a_correlation():
    assert mod.ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]
    assert mod.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert mod.pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1.0)
    assert mod.pearson([1, 1, 1], [1, 2, 3]) == 0.0


def test_detrend_removes_a_line_and_autocorr_uses_consecutive_days_only():
    line = [(i, 2.0 * i + 5) for i in range(10)]
    assert max(abs(v) for v in mod.detrend(line)) < 1e-9
    # alternating values on consecutive days: strongly negative lag-1
    alt = [(i, (-1) ** i) for i in range(20)]
    assert mod.lag1_autocorr(alt) < -0.9
    # the same values on every OTHER day have no consecutive pairs at all
    gappy = [(2 * i, (-1) ** i) for i in range(20)]
    assert mod.lag1_autocorr(gappy) == 0.0


def test_fisher_p_and_effective_n_behave():
    assert mod.fisher_p(0.0, 50) == pytest.approx(1.0)
    assert mod.fisher_p(0.5, 50) < mod.fisher_p(0.5, 20) < 0.05
    assert mod.fisher_p(0.9, 3) == 1.0
    # conservative: each autocorrelation is lifted by one standard error
    assert 85 < mod.effective_n(90, 0.0, 0.0) < 90
    assert mod.effective_n(90, 0.5, 0.5) < 54.0 and mod.effective_n(90, -0.5, -0.5) < 90
    assert mod.effective_n(90, 0.99, 0.99) == 4.0        # the floor
    assert mod.effective_n(90, 0.3, 0.3) > mod.effective_n(90, 0.6, 0.6)


def test_benjamini_hochberg_is_monotone_and_bounded():
    q = mod.benjamini_hochberg([0.001, 0.01, 0.04, 0.5])
    assert q == [pytest.approx(0.004), pytest.approx(0.02), pytest.approx(0.0533, abs=1e-3),
                 pytest.approx(0.5)]
    assert mod.benjamini_hochberg([]) == []
    assert all(0 <= v <= 1 for v in mod.benjamini_hochberg([0.9, 0.95, 1.0]))


def test_pair_stats_pairs_calendar_days_not_positions():
    x = {i: float(i % 7) for i in range(0, 60)}
    y = {i + 1: float(i % 7) for i in range(0, 60)}         # y is x moved one day later
    same = mod.pair_stats(x, y, 0, min_days=30)
    nxt = mod.pair_stats(x, y, 1, min_days=30)
    assert nxt["rho"] == pytest.approx(1.0) and abs(same["rho"]) < 0.9
    assert nxt["n"] == 60 and same["n"] == 59
    # a week-long gap leaves no lag-1 pairs across it
    x2 = {i: float(i) for i in list(range(0, 30)) + list(range(40, 70))}
    st = mod.pair_stats(x2, {i: v for i, v in x2.items()}, 1, min_days=30)
    assert st["n"] == 58                                   # 29 + 29 consecutive pairs
    few = mod.pair_stats({i: 1.0 for i in range(10)}, {i: 1.0 for i in range(10)}, 0,
                         min_days=30)
    assert "skipped" in few and "rho" not in few


def test_shift_check_is_high_for_an_aligned_pattern_and_low_for_a_fluke():
    rng = random.Random(mod.seed_for("shift"))
    x = {i: rng.gauss(0, 1) for i in range(90)}
    y = {i: 0.9 * x[i] + 0.3 * rng.gauss(0, 1) for i in range(90)}
    st = mod.pair_stats(x, y, 0, min_days=30)
    p, shifts = mod.shift_p(x, y, 0, st["rho"], 90, min_days=30)
    assert shifts > 50 and p is not None and p < 0.05


# ---------------------------------------------------------------- find

def _scale(z: float) -> float:
    """A standard-normal draw as a 1-10 journal score."""
    return round(min(10.0, max(1.0, 5.5 + 1.5 * z)), 2)


def _plant(conn, start: date, n: int, rho: float, seed: str, lag: int = 0):
    """mood drives energy on the next `lag` days; sleep_quality is noise."""
    rng = random.Random(mod.seed_for(seed))
    xs = {}
    for k in range(n):
        d = start + timedelta(days=k)
        x = rng.gauss(0, 1)
        xs[k] = x
        measurements.add(conn, journal_date=d.isoformat(), metric="mood",
                         numeric_value=_scale(x))
        measurements.add(conn, journal_date=d.isoformat(), metric="sleep_quality",
                         numeric_value=_scale(rng.gauss(0, 1) / 1.5))
        if k - lag >= 0:
            y = rho * xs[k - lag] + math.sqrt(1 - rho * rho) * rng.gauss(0, 1)
            measurements.add(conn, journal_date=d.isoformat(), metric="energy",
                             numeric_value=_scale(y))
    conn.commit()


def test_find_reports_the_planted_pair_and_not_the_noise(conn, cfg):
    _plant(conn, END - timedelta(days=89), 90, 0.7, "same-day")
    result = mod.find(conn, cfg, end=END, days=90)
    assert result["status"] == "experimental"
    assert set(result["series"]) == {"mood", "energy", "sleep_quality"}
    assert result["family_size"] == 9                        # 3 pairs x (1 + 2) lags
    reported = {(it["x"], it["y"], it["lag"]) for it in result["items"]}
    assert reported == {("energy", "mood", 0)}
    item = result["items"][0]
    assert item["rho"] > 0.5 and item["q"] <= 0.10 and item["n"] == 90
    assert item["shift_p"] is not None and item["shift_p"] < 0.1 and item["shifts"] > 50
    assert "co-occurrence, not cause" in item["wording"]
    assert "the same day" in item["wording"]
    assert len(item["dates"]) == 90 and set(item["refs"]) == {"energy", "mood"}
    assert all(t == "measurements" for t, _ in item["refs"]["mood"])
    reasons = {(t["x"], t["y"], t["lag"]): t.get("reason") for t in result["tested"]}
    assert all(r for k, r in reasons.items() if k != ("energy", "mood", 0))
    assert result["notes"][0].startswith("experimental:")


def test_find_sees_a_next_day_association_in_the_right_direction(conn, cfg):
    _plant(conn, END - timedelta(days=89), 90, 0.7, "next-day", lag=1)
    result = mod.find(conn, cfg, end=END, days=90)
    reported = {(it["x"], it["y"], it["lag"]) for it in result["items"]}
    assert ("mood", "energy", 1) in reported                 # mood today, energy tomorrow
    assert ("energy", "mood", 1) not in reported
    assert all("the next day" in it["wording"] for it in result["items"]
               if (it["x"], it["y"], it["lag"]) == ("mood", "energy", 1))


def test_find_is_deterministic_and_validates_its_window(conn, cfg):
    _plant(conn, END - timedelta(days=59), 60, 0.6, "det")
    a = mod.find(conn, cfg, end=END, days=60)
    b = mod.find(conn, cfg, end=END, days=60)
    assert a == b
    with pytest.raises(DomainError, match="--days must be"):
        mod.find(conn, cfg, end=END, days=7)
    thin = mod.find(conn, cfg, end=END, days=20)             # under the sample floor
    assert thin["series"] == {} and thin["items"] == [] and thin["family_size"] == 0


def test_an_empty_database_finds_nothing(conn, cfg):
    result = mod.find(conn, cfg, end=END)
    assert result["items"] == [] and result["tested"] == [] and result["series"] == {}
    assert any("experimental" in n for n in result["notes"])


# ---------------------------------------------------------------- CLI

def test_cli_prints_the_banner_and_json(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    _plant(conn, END - timedelta(days=89), 90, 0.7, "cli")
    conn.close()
    r = runner.invoke(app, ["patterns", "--today", END.isoformat()])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("Patterns (EXPERIMENTAL)")
    assert "energy tends to go with higher mood the same day" in r.output
    assert "family of 9" in r.output
    r = runner.invoke(app, ["patterns", "--today", END.isoformat(), "--all"])
    assert "not reported" in r.output
    r = runner.invoke(app, ["patterns", "--today", END.isoformat(), "--json", "--no-check"])
    assert r.exit_code == 0
    import json
    out = json.loads(r.output)
    assert out["status"] == "experimental" and "shift_p" not in out["items"][0]
    r = runner.invoke(app, ["patterns", "--days", "3"])
    assert r.exit_code == 1 and "--days must be" in r.output

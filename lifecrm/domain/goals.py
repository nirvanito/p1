"""Goals: a `plan` entry whose `data.goal` names a measurable target.

    {"metric": "workouts", "target": 3, "per": "week"}
    {"metric": "sleep", "target": 7, "per": "day"}
    {"metric": "spending", "target": 500, "per": "week", "op": "<="}
    {"metric": "contact", "entity": "Mom", "target": 1, "per": "week"}

The shape is validated here, in the domain layer, on every path that can
produce an entry — create, edit (including a kind-only edit that would turn
a goal-bearing plan into something else), and the draft contract — so no
write leaves a goal the read side (lifecrm/goals.py) cannot evaluate. Pure
rules, no database: the metric vocabulary is fixed here and mirrored by
lifecrm/series.py, which measures it.

How a goal is decided depends on how its metric adds up (docs/contract.md,
"Goals"): a cumulative count or sum can be reached early in a period and
projected; a level (sleep, mood) is reported with its coverage and decided
only when the period is over and enough days are known.
"""

from __future__ import annotations

import math

from .util import DomainError, is_iso_date

PERIODS = ("day", "week", "month")
OPS = (">=", "<=")
CUMULATIVE = ("workouts", "exercise_minutes", "spending", "texts", "contact", "steps")
AVERAGE = ("sleep", "sleep_hours", "mood", "energy")
METRICS = CUMULATIVE + AVERAGE
KEYS = frozenset({"metric", "target", "per", "op", "start", "entity", "currency", "min_known"})
MIN_KNOWN_DEFAULT = {"day": 1, "week": 5, "month": 20}


def aggregation(goal: dict) -> str:
    """How the metric adds up over a period: 'avg' (a level), 'sum' (a
    quantity) or 'count' (events). Steps are a sum within a day and a level
    across days ("8,000 a day" over a week is its daily mean)."""
    metric = goal["metric"]
    if metric in AVERAGE:
        return "avg"
    if metric == "steps":
        return "sum" if goal.get("per", "day") == "day" else "avg"
    if metric in ("workouts", "texts", "contact") or metric.startswith("cat:"):
        return "count"
    return "sum"


def validate_goal(goal) -> dict:
    """The goal, checked and normalized (defaults filled in); a single-line
    DomainError naming the field otherwise."""
    if not isinstance(goal, dict):
        raise DomainError("data.goal must be an object like "
                          '{"metric": "workouts", "target": 3, "per": "week"}')
    unknown = sorted(set(goal) - KEYS)
    if unknown:
        raise DomainError(f"data.goal has unknown field(s) {', '.join(unknown)} — known: "
                          f"{', '.join(sorted(KEYS))}")
    metric = goal.get("metric")
    if not isinstance(metric, str) or not metric.strip():
        raise DomainError(f"data.goal.metric must be one of {', '.join(METRICS)} or "
                          f"cat:<category>")
    metric = metric.strip()
    if metric not in METRICS and not (metric.startswith("cat:") and metric[4:].strip()):
        raise DomainError(f"data.goal.metric '{metric}' must be one of {', '.join(METRICS)} "
                          f"or cat:<category>")
    target = goal.get("target")
    if isinstance(target, bool) or not isinstance(target, (int, float)) \
            or not math.isfinite(target) or target < 0:
        raise DomainError(f"data.goal.target must be a number of 0 or more, got {target!r}")
    per = goal.get("per")
    if per not in PERIODS:
        raise DomainError(f"data.goal.per must be one of {', '.join(PERIODS)}, got {per!r}")
    op = goal.get("op", ">=")
    if op not in OPS:
        raise DomainError(f"data.goal.op must be '>=' or '<=', got {op!r}")
    start = goal.get("start")
    if start is not None and not is_iso_date(start):
        raise DomainError(f"data.goal.start must be a date YYYY-MM-DD, got {start!r}")
    entity = goal.get("entity")
    if metric == "contact":
        if not isinstance(entity, str) or not entity.strip():
            raise DomainError("data.goal.entity (the person's name) is required when "
                              "metric is 'contact'")
    elif entity is not None:
        raise DomainError("data.goal.entity only goes with metric 'contact'")
    currency = goal.get("currency")
    if currency is not None:
        if metric != "spending":
            raise DomainError("data.goal.currency only goes with metric 'spending'")
        if not isinstance(currency, str) or len(currency.strip()) != 3:
            raise DomainError(f"data.goal.currency must be a 3-letter code, got {currency!r}")
    min_known = goal.get("min_known", MIN_KNOWN_DEFAULT[per])
    if isinstance(min_known, bool) or not isinstance(min_known, int) or not 1 <= min_known <= 31:
        raise DomainError(f"data.goal.min_known must be a whole number of days, 1-31, "
                          f"got {min_known!r}")
    out = {"metric": metric, "target": float(target), "per": per, "op": op,
           "min_known": min_known}
    if start is not None:
        out["start"] = start
    if entity is not None:
        out["entity"] = entity.strip()
    if currency is not None:
        out["currency"] = currency.strip().upper()
    return out


def validate_entry_data(kind: str | None, data) -> None:
    """The rule every write path runs on the RESULTING entry: `data.goal`
    needs kind `plan` and a valid shape. An entry without a goal is left
    alone; `data` must already be an object or None."""
    if data is None:
        return
    if not isinstance(data, dict):
        raise DomainError("entry data must be an object or null")
    # The read-side features' shapes ride the same rule (lifecrm/features.py:
    # checked here whether or not the feature is shown).
    from . import experiments, gifts
    experiments.validate_entry_data(kind, data)
    gifts.validate_entry_data(kind, data)
    if "goal" not in data:
        return
    if kind != "plan":
        raise DomainError(f"data.goal needs kind 'plan' (this entry is {kind!r}) — make it "
                          f"a plan, or remove data.goal first")
    validate_goal(data["goal"])

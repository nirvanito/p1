"""Self-experiments: a `plan` entry whose `data.experiment` names a change and
the series it should move.

    {"change": "no caffeine after 2pm", "outcome": "sleep", "days": 14}
    {"change": "walk at lunch", "outcome": "mood", "start": "2026-11-02",
     "days": 21, "baseline_days": 28, "hope": "up"}

Bearable's experiments run a change for 7-30 days and show what moved; here
lifecrm/experiments.py compares the outcome over the days of the change with
the same number of days before it (baseline_days to override), over the
days the series KNOWS — a missing row is unknown, never zero. The shape is
checked here, on every write path (entries.create, corrections.edit_entry
on the resulting entry, the draft contract), whether or not the feature is
checked in [features]: a plan written today must still read correctly
later. Pure rules, no database; the outcome vocabulary mirrors
lifecrm/series.py.
"""

from __future__ import annotations

from .util import DomainError, is_iso_date

KEYS = frozenset({"change", "outcome", "start", "days", "baseline_days", "hope",
                  "min_known"})
OUTCOMES = ("mood", "energy", "sleep_quality", "sleep_hours", "sleep", "steps",
            "resting_heart_rate", "weight", "exercise_minutes", "workouts", "spending",
            "texts", "contacts")
FAMILIES = ("cat:", "nutrient:")
HOPES = ("up", "down")
MIN_DAYS, MAX_DAYS = 7, 90
DEFAULT_DAYS = 14


def default_min_known(days: int) -> int:
    """Half the days, and never fewer than 4 (or all of them, if fewer)."""
    return min(days, max(4, days // 2))


def validate_experiment(exp) -> dict:
    """The experiment, checked and normalized (defaults filled in); a
    single-line DomainError naming the field otherwise."""
    if not isinstance(exp, dict):
        raise DomainError('data.experiment must be an object like {"change": "no caffeine '
                          'after 2pm", "outcome": "sleep", "days": 14}')
    unknown = sorted(set(exp) - KEYS)
    if unknown:
        raise DomainError(f"data.experiment has unknown field(s) {', '.join(unknown)} — "
                          f"known: {', '.join(sorted(KEYS))}")
    change = exp.get("change")
    if not isinstance(change, str) or not change.strip() or len(change) > 200:
        raise DomainError("data.experiment.change must say what you change, in 200 "
                          "characters or fewer")
    outcome = exp.get("outcome")
    if not isinstance(outcome, str) or not (
            outcome.strip() in OUTCOMES
            or any(outcome.strip().startswith(f) and outcome.strip()[len(f):].strip()
                   for f in FAMILIES)):
        raise DomainError(f"data.experiment.outcome must be one of {', '.join(OUTCOMES)}, "
                          f"cat:<category> or nutrient:<key>, got {outcome!r}")
    days = exp.get("days", DEFAULT_DAYS)
    for name, value in (("days", days), ("baseline_days", exp.get("baseline_days", days))):
        if isinstance(value, bool) or not isinstance(value, int) \
                or not MIN_DAYS <= value <= MAX_DAYS:
            raise DomainError(f"data.experiment.{name} must be a whole number of days, "
                              f"{MIN_DAYS}-{MAX_DAYS}, got {value!r}")
    start = exp.get("start")
    if start is not None and not is_iso_date(start):
        raise DomainError(f"data.experiment.start must be a date YYYY-MM-DD, got {start!r}")
    hope = exp.get("hope")
    if hope is not None and hope not in HOPES:
        raise DomainError(f"data.experiment.hope must be 'up' or 'down', got {hope!r}")
    min_known = exp.get("min_known", default_min_known(days))
    if isinstance(min_known, bool) or not isinstance(min_known, int) \
            or not 1 <= min_known <= days:
        raise DomainError(f"data.experiment.min_known must be a whole number of days, "
                          f"1-{days}, got {min_known!r}")
    out = {"change": change.strip(), "outcome": outcome.strip(), "days": days,
           "baseline_days": exp.get("baseline_days", days), "min_known": min_known}
    if start is not None:
        out["start"] = start
    if hope is not None:
        out["hope"] = hope
    return out


def validate_entry_data(kind: str | None, data: dict) -> None:
    """`data.experiment` needs kind `plan` and a valid shape."""
    if "experiment" not in data:
        return
    if kind != "plan":
        raise DomainError(f"data.experiment needs kind 'plan' (this entry is {kind!r}) — "
                          f"make it a plan, or remove data.experiment first")
    validate_experiment(data["experiment"])

"""Structured measurements with unit normalization.

Known metrics get unit and range validation; open-ended personal metrics are
allowed through without normalization — validation must never prevent the user
from tracking something new.
"""

from __future__ import annotations

import sqlite3

from ..config import CONFIDENCE
from . import audit, days
from .util import DomainError, new_uuid, now_utc

# metric -> {units: {unit: factor_to_norm}, norm_unit, range (on normalized or raw value)}
KNOWN_METRICS: dict[str, dict] = {
    "mood":          {"range": (1, 10)},
    "energy":        {"range": (1, 10)},
    "rating":        {"range": (0, 10)},
    "sleep_hours":   {"units": {"h": 1, "hr": 1, "hours": 1}, "norm_unit": "h", "range": (0, 16)},
    "sleep_quality": {"range": (1, 10)},
    "duration":      {"units": {"s": 1 / 60, "sec": 1 / 60, "seconds": 1 / 60,
                                "min": 1, "mins": 1, "minutes": 1,
                                "h": 60, "hr": 60, "hours": 60},
                      "norm_unit": "min", "range": (0, 1440)},
    "distance":      {"units": {"m": 0.001, "km": 1, "mi": 1.60934, "miles": 1.60934,
                                "ft": 0.0003048, "yd": 0.0009144},
                      "norm_unit": "km", "range": (0, 1000)},
    "weight":        {"units": {"kg": 1, "lb": 0.453592, "lbs": 0.453592, "g": 0.001},
                      "norm_unit": "kg", "range": (0, 700)},
    "reps":          {"range": (0, 10000)},
    "sets":          {"range": (0, 200)},
    "calories":      {"units": {"kcal": 1, "cal": 1}, "norm_unit": "kcal", "range": (0, 20000)},
    "servings":      {"range": (0, 100)},
    "money":         {},   # unit is a currency code; no conversion
    "steps":         {"range": (0, 200000)},
    "water_ml":      {"units": {"ml": 1, "l": 1000, "oz": 29.5735}, "norm_unit": "ml",
                      "range": (0, 20000)},
}


def normalize(metric: str, numeric_value: float | None,
              unit: str | None) -> tuple[float | None, str | None, str | None]:
    """Return (normalized_value, normalized_unit, error). Open-ended metrics
    pass through with no normalization and no error."""
    spec = KNOWN_METRICS.get(metric)
    if spec is None or numeric_value is None:
        return None, None, None
    units = spec.get("units")
    norm_value, norm_unit = None, None
    if units:
        if unit is None:
            # Unitless value for a united metric: assume the normalized unit.
            norm_value, norm_unit = float(numeric_value), spec["norm_unit"]
        elif unit.lower() in units:
            norm_value = float(numeric_value) * units[unit.lower()]
            norm_unit = spec["norm_unit"]
        else:
            return None, None, (
                f"metric '{metric}' has unknown unit '{unit}' "
                f"(expected one of: {', '.join(sorted(units))})")
    else:
        if metric == "money":
            return None, None, None
        norm_value, norm_unit = float(numeric_value), unit
    rng = spec.get("range")
    if rng and norm_value is not None and not (rng[0] <= norm_value <= rng[1]):
        return None, None, (
            f"metric '{metric}' value {norm_value:g}"
            f"{' ' + norm_unit if norm_unit else ''} outside plausible range {rng[0]}..{rng[1]}")
    return norm_value, norm_unit, None


def validate_fields(metric: str, numeric_value, text_value, unit,
                    confidence: str = "high") -> str | None:
    """Single-line error or None."""
    if not isinstance(metric, str) or not metric.strip():
        return "measurement metric must be a non-empty string"
    if numeric_value is None and (text_value is None or str(text_value).strip() == ""):
        return f"measurement '{metric}' needs numeric_value or text_value"
    if numeric_value is not None and not isinstance(numeric_value, (int, float)):
        return f"measurement '{metric}' numeric_value must be a number"
    if confidence not in CONFIDENCE:
        return f"measurement '{metric}' confidence must be one of: {', '.join(CONFIDENCE)}"
    _, _, err = normalize(metric, numeric_value, unit)
    return err


def add(conn: sqlite3.Connection, *, entry_id: int | None = None,
        journal_date: str | None = None, metric: str,
        numeric_value: float | None = None, text_value: str | None = None,
        unit: str | None = None, confidence: str = "high",
        extraction_run_id: int | None = None, actor: str = "user") -> dict:
    if (entry_id is None) == (journal_date is None):
        raise DomainError("measurement needs exactly one owner: entry_id or journal_date")
    if journal_date is not None:
        days.ensure_day(conn, journal_date)   # validates format, creates day row
    err = validate_fields(metric, numeric_value, text_value, unit, confidence)
    if err:
        raise DomainError(err)
    norm_value, norm_unit, _ = normalize(metric, numeric_value, unit)
    uid = new_uuid()
    conn.execute(
        "INSERT INTO measurements (uuid, entry_id, journal_date, metric, numeric_value, "
        "text_value, unit, normalized_value, normalized_unit, confidence, "
        "extraction_run_id, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
        (uid, entry_id, journal_date, metric, numeric_value, text_value, unit,
         norm_value, norm_unit, confidence, extraction_run_id, now_utc()))
    audit.log(conn, actor, "create", "measurements", uid,
              {"metric": metric, "numeric_value": numeric_value,
               "text_value": text_value, "unit": unit,
               "entry_id": entry_id, "journal_date": journal_date})
    return {"uuid": uid, "normalized_value": norm_value, "normalized_unit": norm_unit}


def retract(conn: sqlite3.Connection, measurement_uuid: str, actor: str = "user") -> None:
    row = conn.execute("SELECT * FROM measurements WHERE uuid=?", (measurement_uuid,)).fetchone()
    if row is None:
        raise DomainError(f"measurement {measurement_uuid} does not exist")
    if row["status"] == "retracted":
        raise DomainError(f"measurement {measurement_uuid} is already retracted")
    conn.execute("UPDATE measurements SET status='retracted' WHERE uuid=?", (measurement_uuid,))
    audit.log(conn, actor, "retract", "measurements", measurement_uuid,
              {"metric": row["metric"]})

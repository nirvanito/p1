"""Daily series, one registry for drift, goals and (later) patterns.

A series is a value per calendar day, and — the part that matters — the set
of days it KNOWS. Missing data is unknown, never zero:

- a measured quantity (mood, sleep, steps, resting heart rate, weight, a
  nutrient) is known only on days with a record; a phone left at home is
  not a day of zero steps;
- a count over imported rows (workouts, spending, texts, who you were in
  touch with) is known-zero only on days an import DECLARED complete for
  that stream from a guarantee in its source (`import_coverage`, schema v7;
  lifecrm/domain/observations.coverage_days). Positives are always known;
- a count over the journal (`cat:<category>`) has positives only: a logged
  day does not establish that every category was recorded.

`contacts` and `contact:<id>` take their positives from every channel
(messages, calls, meetings, emails, and the people a confirmed entry names
as participants) but their zeros from complete TEXTS only — a zero means "no
recorded texts", which is how every reader words it.

Every value carries references to the rows behind it (table, uuid), so a
pack can allocate citation ids once from them (review._Ids); nothing here
knows what a pack id is. Reads only; no model.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .domain import observations as obs_mod
from .domain.util import DomainError
from .recall import (CALL_STREAMS, EMAIL_STREAMS, HEALTH_METRICS, JOURNAL_METRICS,
                     STREAM_GROUPS, TEXT_STREAMS, _metric_series)

TXN_STREAMS = STREAM_GROUPS["transaction"]
WORKOUT_STREAMS = STREAM_GROUPS["workout"]
CALENDAR_STREAMS = STREAM_GROUPS["calendar"]
CONTACT_STREAMS = tuple(dict.fromkeys(TEXT_STREAMS + CALL_STREAMS + CALENDAR_STREAMS
                                      + EMAIL_STREAMS))
SESSION_GAP = timedelta(minutes=30)        # two sessions this close are one
DEFAULT_SESSION = timedelta(minutes=60)    # a session with no end

# key -> how it reads. `agg` is how a period combines its days: avg (a level),
# sum (a quantity), count (events). `lenses`: the review lenses it belongs
# to. `pattern_default`: in the pattern finder's family (increment 3).
KEYS: dict[str, dict] = {
    "mood":               {"label": "mood", "unit": "/10", "tier": "confirmed", "agg": "avg",
                           "lenses": frozenset({"week", "health"}), "pattern_default": True},
    "energy":             {"label": "energy", "unit": "/10", "tier": "confirmed", "agg": "avg",
                           "lenses": frozenset({"week", "health"}), "pattern_default": True},
    "sleep_quality":      {"label": "sleep quality", "unit": "/10", "tier": "confirmed",
                           "agg": "avg", "lenses": frozenset({"week", "health"}),
                           "pattern_default": True},
    "sleep_hours":        {"label": "sleep (journal)", "unit": "h", "tier": "confirmed",
                           "agg": "avg", "lenses": frozenset({"week", "health"}),
                           "pattern_default": True},
    "sleep":              {"label": "sleep (sensor)", "unit": "h", "tier": "observed",
                           "agg": "avg", "lenses": frozenset({"week", "health"}),
                           "pattern_default": True},
    "steps":              {"label": "steps", "unit": "steps", "tier": "observed", "agg": "sum",
                           "lenses": frozenset({"week", "health"}), "pattern_default": True},
    "resting_heart_rate": {"label": "resting heart rate", "unit": "bpm", "tier": "observed",
                           "agg": "avg", "lenses": frozenset({"week", "health"}),
                           "pattern_default": True},
    "weight":             {"label": "weight", "unit": "kg", "tier": "observed", "agg": "avg",
                           "lenses": frozenset({"week", "health"}), "pattern_default": True},
    "exercise_minutes":   {"label": "exercise minutes (sensor)", "unit": "min",
                           "tier": "observed", "agg": "sum",
                           "lenses": frozenset({"week", "health"}), "pattern_default": True},
    "workouts":           {"label": "workouts", "unit": "sessions", "tier": "observed",
                           "agg": "count", "lenses": frozenset({"week", "health"}),
                           "pattern_default": True},
    "spending":           {"label": "spending", "unit": "", "tier": "observed", "agg": "sum",
                           "lenses": frozenset({"week", "finance"}), "pattern_default": True},
    "texts":              {"label": "texts", "unit": "messages", "tier": "observed",
                           "agg": "count", "lenses": frozenset({"week", "relationships"}),
                           "pattern_default": False},
    "contacts":           {"label": "people in touch", "unit": "people", "tier": "observed",
                           "agg": "count", "lenses": frozenset({"week", "relationships"}),
                           "pattern_default": True},
}
_JOURNAL = {"mood": "mood", "energy": "energy", "sleep_quality": "sleep_quality",
            "sleep_hours": "sleep_hours"}
_HEALTH = {"sleep": "sleep_session", "steps": "steps",
           "resting_heart_rate": "resting_heart_rate", "weight": "weight",
           "exercise_minutes": "exercise_session"}
FAMILIES = ("contact:", "cat:", "nutrient:")


@dataclass
class Series:
    key: str
    label: str
    unit: str
    tier: str
    agg: str
    values: dict[str, float] = field(default_factory=dict)
    known: set[str] = field(default_factory=set)
    refs: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    sources: dict = field(default_factory=dict)

    def period(self, start: str, end: str) -> dict:
        """What a period adds up to: `value` (sum/count over known days, or
        the mean of a level), `known` days of `days`, `complete` when every
        day is known. None when nothing is known."""
        days = _days(start, end)
        known = [d for d in days if d in self.known]
        if not known:
            return {"value": None, "known": 0, "days": len(days), "complete": False}
        vals = [self.values.get(d, 0.0) for d in known]
        value = sum(vals) / len(vals) if self.agg == "avg" else sum(vals)
        return {"value": round(value, 2), "known": len(known), "days": len(days),
                "complete": len(known) == len(days)}


def info(key: str) -> dict:
    """The registry row for a key, families resolved; DomainError otherwise."""
    if key in KEYS:
        return {"key": key, **KEYS[key]}
    if key.startswith("contact:") and key[len("contact:"):].strip():
        return {"key": key, "label": "days in touch", "unit": "days", "tier": "observed",
                "agg": "count", "lenses": frozenset({"relationships"}),
                "pattern_default": False}
    if key.startswith("cat:") and key[len("cat:"):].strip():
        return {"key": key, "label": f"{key[4:]} entries", "unit": "entries",
                "tier": "confirmed", "agg": "count", "lenses": frozenset({"week"}),
                "pattern_default": False}
    if key.startswith("nutrient:") and key[len("nutrient:"):].strip():
        from . import nutrients
        ref = nutrients.BY_KEY.get(key[len("nutrient:"):])
        if ref is None:
            raise DomainError(f"unknown nutrient '{key[9:]}' — known: "
                              f"{', '.join(sorted(nutrients.BY_KEY))}")
        return {"key": key, "label": ref.label, "unit": ref.unit, "tier": "observed",
                "agg": "avg", "lenses": frozenset({"health"}), "pattern_default": False}
    raise DomainError(f"unknown series '{key}' — known: {', '.join(sorted(KEYS))}, "
                      f"contact:<entity id>, cat:<category>, nutrient:<key>")


def _days(start: str, end: str) -> list[str]:
    d, last = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _owners(conn: sqlite3.Connection, cfg: Config) -> frozenset[int]:
    try:
        from . import owner as owner_mod
        return owner_mod.identity(conn, cfg).entities
    except Exception:          # a bad [owner] table: nobody is left out
        return frozenset()


def _dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- the series

def daily(conn: sqlite3.Connection, cfg: Config, key: str, start: str, end: str) -> Series:
    """The series `key` over [start, end], with what it knows and the rows
    behind each day. Always a Series (possibly knowing nothing)."""
    meta = info(key)
    s = Series(key=key, label=meta["label"], unit=meta["unit"], tier=meta["tier"],
               agg=meta["agg"])
    try:
        if key in _JOURNAL:
            _metric(conn, s, "measurements", _JOURNAL[key], JOURNAL_METRICS, start, end)
        elif key in _HEALTH:
            _metric(conn, s, "health_records", _HEALTH[key], HEALTH_METRICS, start, end)
        elif key == "workouts":
            _workouts(conn, cfg, s, start, end)
        elif key == "spending":
            _spending(conn, s, start, end)
        elif key == "texts":
            _texts(conn, s, start, end)
        elif key == "contacts":
            _contacts(conn, cfg, s, start, end, None)
        elif key.startswith("contact:"):
            try:
                entity_id = int(key[len("contact:"):])
            except ValueError:
                raise DomainError(f"{key}: contact series take an entity id") from None
            _contacts(conn, cfg, s, start, end, entity_id)
        elif key.startswith("cat:"):
            _category(conn, s, key[4:], start, end)
        elif key.startswith("nutrient:"):
            _nutrient(conn, s, key[len("nutrient:"):], start, end)
    except sqlite3.OperationalError:
        pass                   # a database before the observed tier: nothing known
    return s


def _metric(conn, s: Series, table: str, metric: str, specs: dict, start: str,
            end: str) -> None:
    out = _metric_series(conn, table=table, metric=metric, spec=specs[metric],
                         window={"start": start, "end": end}, refs=True)
    if out is None:
        return
    for p in out["series"]:
        s.values[p["date"]] = p["value"]
        s.known.add(p["date"])
        s.refs[p["date"]] = [(table, u) for u in p.get("uuids", [])]
    s.sources = {"metric": metric, "table": table,
                 "source_files": out.get("source_files", []),
                 "origins": out.get("origins", [])}


def _workouts(conn, cfg: Config, s: Series, start: str, end: str) -> None:
    """Sessions per day, merged across sources: a fitness-app row, a sensor
    exercise session and a journal exercise entry that overlap or start
    within 30 minutes are one session; a journal entry with no time attaches
    to a sourced session that day if there is one, else counts as one."""
    from .review import NOT_DERIVED
    sessions: dict[str, list] = {}
    marks = ",".join("?" for _ in WORKOUT_STREAMS)
    for r in conn.execute(
            f"SELECT uuid, journal_date, started_at, ended_at, value FROM observations "
            f"WHERE status = 'active' AND stream IN ({marks}) "
            f"AND journal_date BETWEEN ? AND ? ORDER BY started_at, id",
            (*WORKOUT_STREAMS, start, end)):
        sessions.setdefault(r["journal_date"], []).append(
            _session(r["started_at"], r["ended_at"], r["value"], "observations", r["uuid"]))
    for r in conn.execute(
            "SELECT uuid, journal_date, started_at, ended_at, value FROM health_records "
            "WHERE status = 'active' AND metric = 'exercise_session' "
            "AND journal_date BETWEEN ? AND ? ORDER BY started_at, id", (start, end)):
        sessions.setdefault(r["journal_date"], []).append(
            _session(r["started_at"], r["ended_at"], r["value"], "health_records", r["uuid"]))
    # An exercise EVENT is a session; a plan, task or idea about exercise
    # ("get back to three sessions a week") is not one.
    for r in conn.execute(
            "SELECT DISTINCT e.uuid, e.journal_date, e.occurred_at, e.ended_at FROM entries e "
            "JOIN entry_kinds k ON k.id = e.kind_id "
            "JOIN entry_categories ec ON ec.entry_id = e.id "
            "JOIN categories c ON c.id = ec.category_id "
            f"WHERE c.name = 'exercise' AND k.name = 'event' AND e.status = 'active' "
            f"AND e.journal_date BETWEEN ? AND ? AND {NOT_DERIVED} "
            "ORDER BY e.journal_date, e.id", (start, end)):
        sessions.setdefault(r["journal_date"], []).append(
            _session(r["occurred_at"], r["ended_at"], None, "entries", r["uuid"]))
    for day, items in sessions.items():
        timed = sorted((x for x in items if x["start"] is not None), key=lambda x: x["start"])
        clusters: list[dict] = []
        for x in timed:
            if clusters and x["start"] <= clusters[-1]["end"] + SESSION_GAP:
                clusters[-1]["end"] = max(clusters[-1]["end"], x["end"])
                clusters[-1]["refs"].append(x["ref"])
            else:
                clusters.append({"start": x["start"], "end": x["end"], "refs": [x["ref"]]})
        untimed = [x for x in items if x["start"] is None]
        if untimed and clusters:
            clusters[0]["refs"] += [x["ref"] for x in untimed]
        elif untimed:
            clusters.append({"start": None, "end": None, "refs": [x["ref"] for x in untimed]})
        s.values[day] = float(len(clusters))
        s.known.add(day)
        s.refs[day] = [ref for c in clusters for ref in c["refs"]]
    covered = obs_mod.coverage_days(conn, WORKOUT_STREAMS, start, end)
    for day in covered - s.known:
        s.values[day] = 0.0
        s.known.add(day)
    s.sources = {"streams": list(WORKOUT_STREAMS), "declared_days": len(covered),
                 "rule": "sessions from the fitness app, the sensor and the journal "
                         "that overlap or start within 30 minutes count once"}


def _session(started: str | None, ended: str | None, minutes, table: str, uuid: str) -> dict:
    start = _dt(started)
    end = _dt(ended)
    if start is not None and end is None:
        end = start + (timedelta(minutes=float(minutes)) if minutes else DEFAULT_SESSION)
    if start is not None and end is not None and end < start:
        end = start
    return {"start": start, "end": end, "ref": (table, uuid)}


def _spending(conn, s: Series, start: str, end: str) -> None:
    """Money out per day in the dominant currency, FINANCE_RULE applied
    (transfers and a bank line funding a payment app left out)."""
    from .review import FINANCE_RULE, _txn_totals, classify_txns
    marks = ",".join("?" for _ in TXN_STREAMS)
    rows = conn.execute(
        "SELECT o.id, o.uuid, o.stream, o.journal_date, o.started_at, o.ended_at, "
        "o.direction, o.title, o.body, o.value, o.unit, o.value_json, o.origin_app, "
        "i.uuid AS import_uuid FROM observations o JOIN imports i ON i.id = o.import_id "
        f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
        "AND o.journal_date BETWEEN ? AND ? ORDER BY o.started_at, o.id",
        (*TXN_STREAMS, start, end)).fetchall()
    counted = classify_txns(conn, rows)["counted"] if rows else []
    by_cur: dict[str, int] = {}
    for r in counted:
        by_cur[(r["unit"] or "").lower()] = by_cur.get((r["unit"] or "").lower(), 0) + 1
    currency = max(by_cur, key=lambda c: (by_cur[c], c)) if by_cur else ""
    by_day: dict[str, list] = {}
    for r in counted:
        if (r["unit"] or "").lower() == currency:
            by_day.setdefault(r["journal_date"], []).append(r)
    for day, items in by_day.items():
        totals = _txn_totals(items)
        spent = sum(t["spent"] for t in totals.values())
        s.values[day] = round(spent, 2)
        s.known.add(day)
        s.refs[day] = [("observations", r["uuid"]) for r in items]
    covered = obs_mod.coverage_days(conn, TXN_STREAMS, start, end)
    for day in covered - s.known:
        s.values[day] = 0.0
        s.known.add(day)
    s.unit = currency.upper()
    s.sources = {"currency": currency.upper() or None,
                 "other_currencies": sorted(c.upper() for c in by_cur if c != currency),
                 "declared_days": len(covered), "rule": FINANCE_RULE}


def _texts(conn, s: Series, start: str, end: str) -> None:
    marks = ",".join("?" for _ in TEXT_STREAMS)
    for r in conn.execute(
            f"SELECT journal_date, COUNT(*) AS n, GROUP_CONCAT(uuid) AS uuids "
            f"FROM observations WHERE status = 'active' AND stream IN ({marks}) "
            f"AND journal_date BETWEEN ? AND ? GROUP BY journal_date",
            (*TEXT_STREAMS, start, end)):
        s.values[r["journal_date"]] = float(r["n"])
        s.known.add(r["journal_date"])
        s.refs[r["journal_date"]] = [("observations", u) for u in r["uuids"].split(",")]
    covered = obs_mod.coverage_days(conn, TEXT_STREAMS, start, end)
    for day in covered - s.known:
        s.values[day] = 0.0
        s.known.add(day)
    s.sources = {"streams": list(TEXT_STREAMS), "declared_days": len(covered)}


def contact_days(conn: sqlite3.Connection, cfg: Config, start: str, end: str
                 ) -> dict[int, dict[str, list[tuple[str, str]]]]:
    """{person entity id: {day: rows}} for everyone in touch in the window:
    a message or email between you and them (review's between-you rule), a
    call or meeting naming them, or a confirmed entry naming them as a
    participant. Positives only; the owner is left out."""
    from .review import NON_PERSON_KINDS, NOT_DERIVED, _between_you_sql
    owners = _owners(conn, cfg)
    between, args = _between_you_sql()
    marks = ",".join("?" for _ in CONTACT_STREAMS)
    kinds = ",".join(f"'{k}'" for k in sorted(NON_PERSON_KINDS))
    people: dict[int, dict[str, list]] = {}
    for r in conn.execute(
            "SELECT o.uuid, o.journal_date, "
            "CASE WHEN e.status = 'merged' THEN e.merged_into ELSE e.id END AS eid, "
            "CASE WHEN e.status = 'merged' THEN m.type ELSE e.type END AS etype "
            "FROM observations o "
            "JOIN observation_participants p ON p.observation_id = o.id "
            "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            "JOIN entities e ON e.id = h.entity_id "
            "LEFT JOIN entities m ON m.id = e.merged_into "
            f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
            f"AND o.journal_date BETWEEN ? AND ? AND h.ignored = 0 "
            f"AND h.kind NOT IN ({kinds}) AND {between}",
            (*CONTACT_STREAMS, start, end, *args)):
        if r["etype"] != "person" or r["eid"] in owners:
            continue
        people.setdefault(r["eid"], {}).setdefault(r["journal_date"], []).append(
            ("observations", r["uuid"]))
    for r in conn.execute(
            "SELECT e.uuid, e.journal_date, "
            "CASE WHEN en.status = 'merged' THEN en.merged_into ELSE en.id END AS eid "
            "FROM entries e JOIN mentions mn ON mn.entry_id = e.id "
            "JOIN entities en ON en.id = mn.entity_id "
            f"WHERE e.status = 'active' AND mn.role = 'participant' AND en.type = 'person' "
            f"AND e.journal_date BETWEEN ? AND ? AND {NOT_DERIVED}", (start, end)):
        if r["eid"] in owners:
            continue
        people.setdefault(r["eid"], {}).setdefault(r["journal_date"], []).append(
            ("entries", r["uuid"]))
    return people


def _contacts(conn, cfg: Config, s: Series, start: str, end: str,
              entity_id: int | None) -> None:
    """People in touch per day (or whether one person was), positives from
    every channel and confirmed participant mentions, zeros from complete
    texts only."""
    per_day: dict[str, dict[int, list]] = {}
    for eid, days in contact_days(conn, cfg, start, end).items():
        for day, refs in days.items():
            per_day.setdefault(day, {}).setdefault(eid, []).extend(refs)
    for day, people in per_day.items():
        if entity_id is None:
            s.values[day] = float(len(people))
            s.refs[day] = [ref for refs in people.values() for ref in refs]
            s.known.add(day)
        elif entity_id in people:
            s.values[day] = 1.0
            s.refs[day] = list(people[entity_id])
            s.known.add(day)
    covered = obs_mod.coverage_days(conn, TEXT_STREAMS, start, end)
    for day in covered - s.known:
        s.values[day] = 0.0
        s.known.add(day)
    s.sources = {"streams": list(CONTACT_STREAMS), "declared_days": len(covered),
                 "zero_means": "no recorded texts that day (other channels unknown)"}


def _category(conn, s: Series, name: str, start: str, end: str) -> None:
    from .review import NOT_DERIVED
    for r in conn.execute(
            "SELECT e.journal_date, COUNT(DISTINCT e.id) AS n, "
            "GROUP_CONCAT(DISTINCT e.uuid) AS uuids FROM entries e "
            "JOIN entry_categories ec ON ec.entry_id = e.id "
            "JOIN categories c ON c.id = ec.category_id "
            f"WHERE c.name = ? COLLATE NOCASE AND e.status = 'active' "
            f"AND e.journal_date BETWEEN ? AND ? AND {NOT_DERIVED} GROUP BY e.journal_date",
            (name, start, end)):
        s.values[r["journal_date"]] = float(r["n"])
        s.known.add(r["journal_date"])
        s.refs[r["journal_date"]] = [("entries", u) for u in r["uuids"].split(",")]
    s.sources = {"category": name, "zero_means": "never: a logged day does not say "
                                                  "every category was recorded"}


def _nutrient(conn, s: Series, key: str, start: str, end: str) -> None:
    from . import nutrients
    for r in conn.execute(
            "SELECT uuid, journal_date, value_json FROM observations "
            "WHERE status = 'active' AND stream = 'nutrition_day' "
            "AND journal_date BETWEEN ? AND ? ORDER BY journal_date, started_at, id",
            (start, end)):
        values, _detail = nutrients._nutrients(r["value_json"])
        if key not in values:
            continue
        value, unit = values[key]
        s.values[r["journal_date"]] = value          # the newest row of a day wins
        s.known.add(r["journal_date"])
        s.refs[r["journal_date"]] = [("observations", r["uuid"])]
        if unit:
            s.unit = unit
    s.sources = {"stream": "nutrition_day", "nutrient": key}


def available(conn: sqlite3.Connection, cfg: Config, start: str, end: str, *,
              patterns_only: bool = False) -> list[str]:
    """The registry keys that know at least one day in the window."""
    out = []
    for key, meta in KEYS.items():
        if patterns_only and not meta["pattern_default"]:
            continue
        if daily(conn, cfg, key, start, end).known:
            out.append(key)
    return out

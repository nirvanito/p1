"""Fitness connector: workouts from Strong, Hevy and Strava exports.

The journal says "bench with Sam, three sets of five at one eighty-five"; the
training app beside it holds every set of every session for years. Strong
and Hevy (gym logs) and Strava (runs, rides, hikes) all hand that history
over as a CSV for free. This connector reads those files — never an API,
never a login — and turns each WORKOUT into one `workout` observation.

Accepted targets
----------------
- one export file, recognised by its header row, never by its name:
    Strong   strong.csv (app: Settings > Export Strong Data)
             Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,
             Distance,Seconds,Notes,Workout Notes,RPE — ','- or, in some
             locales, ';'-separated with decimal commas; variants with a
             Weight Unit / Distance Unit column, 'Workout Duration', or the
             unit in the header ('Weight (kg)') are read the same way
    Hevy     workouts.csv (app: Settings > Export & Import Data > Export
             Workouts)
             title,start_time,end_time,description,exercise_title,
             superset_id,exercise_notes,set_index,set_type,weight_lbs |
             weight_kg,reps,distance_miles | distance_km,duration_seconds,rpe
    Strava   activities.csv from the bulk export (strava.com > Settings > My
             Account > Download or Delete Your Account > Request Your Archive)
- Strava's archive as it arrives, export_<athlete id>.zip, or its folder.
  Only its CSVs are opened and only activities.csv is imported: the
  GPX/FIT/TCX files under activities/ are never read — each activity keeps
  its file's path, relative to the export, as media_path. An archive can
  run to gigabytes of tracks; reading one CSV out of it is what keeps an
  import fast.
- any folder (searched recursively) or .zip holding these; a CSV that is
  none of the three, and a zip inside a folder, are counted and left alone
- nothing at all: `path` in [connectors.fitness], else the one strong.csv,
  workouts.csv or export_*.zip in Downloads / OneDrive / iCloud Drive

One observation per workout, not per set
----------------------------------------
A set is not an event anyone asks about; a session is. Ten years of lifting
is ~1,500 workouts and ~40,000 sets: one row per set would bury the
sessions and the question "when did I last train legs" under the noise.
The sets are kept, structured, inside the workout's value_json.

  title        the workout's name ('Push Day'), Strava's activity name
               ('Zilker Park 5k'), else the activity type
  body         a readable summary: 'Bench Press (Barbell) 3×5 @ 185 lb;
               Overhead Press (Barbell) 3×8 @ 95 lb', or 'Run 5.00 km in
               27:00 (5:24 /km), +32 m, avg HR 158 / max 176, 412 kcal' —
               then the workout's own notes on the next line
  value/unit   duration in minutes, 'min' (Strava: moving time, its headline
               figure for a run; elapsed time when there is none)
  started_at   the start; ended_at = start + duration (Hevy: its end_time)
  value_json   app, exercises [{name, sets [{weight, unit, reps, distance,
               distance_unit, seconds, rpe, type}], notes, superset,
               top_set {weight, unit, reps, weight_kg}}], total_volume_kg,
               distance_km, elevation_m, avg_hr, max_hr, calories, notes,
               unit_source ('setting' when a unit was assumed, below) and
               Strava's activity_id, activity_type, moving_s, elapsed_s,
               gear, commute, private_note, relative_effort
  media_path   Strava only: 'activities/<id>.gpx' (or .fit.gz, .tcx.gz)

`top_set` is the heaviest working set of that exercise in that workout
(more reps breaks a tie; warm-ups never count) and `total_volume_kg` sums
weight × reps over working sets. Both are arithmetic on the record itself,
there so "what's my bench PR" is one query over value_json — never a
judgement about which sets "really" counted.

Time, and the zone nobody wrote down
------------------------------------
Strong and Hevy write the phone's wall-clock time with no zone
('2026-10-09 06:30:00', '9 Oct 2026, 06:30'): it is read in the configured
timezone, the same rule as every zone-less source. Strava's 'Activity Date'
is UTC ('Oct 10, 2026, 11:15:00 AM' is 06:15 in Austin) — which matters
abroad: a 9 am hike in Slovenia is 03:00 in Austin, and the journal day is
the home timezone's, so with a 05:00 rollover it lands on the day before.
None of the three files names the zone it happened in; that is a documented
limitation (base.journal_date_for_iso), not a bug.

Units the file does not state
-----------------------------
Strong writes its weights and distances in whatever unit the app is set to
and does not say which. `[connectors.fitness] weight_unit = "lb" | "kg"`
(default "lb", the US default) and `distance_unit = "mi" | "km"` (default
following weight_unit: mi with lb, km with kg) say it; a unit column or a
unit in the header always wins, and the report says whenever the setting
was used. Hevy names its unit in the column (weight_lbs / weight_kg).
Strava's activities.csv carries two 'Distance' columns: the first in the
athlete's display unit, the second in meters — the meters one is read. An
older export with only the first uses distance_unit, and says so.

Identity
--------
- Strong / Hevy: '<app>:<start as written>:<workout name, case-folded>'.
  The start is the file's own wall-clock text, not the UTC instant derived
  from it, so changing the configured timezone never re-identifies a
  workout. Renaming a workout in the app after it was imported gives it a
  new id — the old row stays (see "remember" below).
- Strava: 'strava:<Activity ID>', stable across renames.

Re-importing, and what wins
---------------------------
None of these files has a per-workout edit time, so every record's
last_modified_at is its export's "as of" — the latest moment that export
describes (its newest workout's end). A later export therefore wins: a set
corrected in the app, or a Strava activity renamed, updates the stored row,
audited. An older export overwrites nothing; a difference shows as a
conflict and the stored workout is kept. One edited without any newer
workout being logged is a conflict too — log the next session first.

Deletion semantics — remember, never mirror
-------------------------------------------
The apps let a workout be deleted, and a CSV can be partial (a Strava
archive requested mid-upload, a Hevy export of one routine, a Strong file
cut short by a share sheet). Telling a deleted workout from a missing one is
impossible from the file, and losing a year of training to a truncated
export is the unrecoverable direction. So the archive remembers: nothing is
ever removed because a later file does not hold it. `imports retract`
undoes a run that should not have happened.

Handles: none
-------------
No export names a training partner — not Strong's workout notes, not
Strava's activities.csv (its group activities are not in the file). Who you
trained with is the journal's to say ("bench with Sam"); a name inside a
workout note stays text in the body, searchable, never a participant.

Personal bests in the report
----------------------------
The dry run lists each exercise whose heaviest working set beat every
earlier workout IN THE SAME EXPORT ("new best: Bench Press (Barbell) 185 lb
× 5"). It is computed from the scan alone, against nothing in the archive,
and nothing about it is stored.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "fitness"
streams = ("workout",)
# No stale_after_days: these exports are pulled by hand, now and then, and a
# quiet month in the gym is not a broken pipeline.

_ISO = "%Y-%m-%dT%H:%M:%SZ"
LB_KG = 0.45359237
MI_KM = 1.609344

LABELS = {"strong": "Strong CSV", "hevy": "Hevy CSV", "strava": "Strava activities.csv"}
KNOWN = ("Strong's strong.csv, Hevy's workouts.csv, Strava's activities.csv "
         "or export_<id>.zip")

_WEIGHT_UNITS = {"lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
                 "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg"}
_DISTANCE_UNITS = {"km": "km", "kilometer": "km", "kilometers": "km",
                   "kilometre": "km", "kilometres": "km",
                   "mi": "mi", "mile": "mi", "miles": "mi",
                   "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m"}
_TO_KG = {"kg": 1.0, "lb": LB_KG}
_TO_KM = {"km": 1.0, "mi": MI_KM, "m": 0.001}
_TRACK_SUFFIXES = (".gpx", ".fit", ".tcx", ".gz")
_STRAVA_EXPORT = re.compile(r"^export_(\d+)(?:\.zip)?$", re.I)


# ---------------------------------------------------------------- settings

def _units(cfg: Config) -> dict:
    """The units Strong's file does not state, from [connectors.fitness]."""
    settings = cfg.connector_settings(name)

    def pick(key: str, table: dict, allowed: tuple[str, ...], default: str) -> str:
        raw = settings.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        unit = table.get(str(raw).strip().lower())
        if unit not in allowed:
            raise DomainError(f"[connectors.fitness] {key} must be "
                              f"{' or '.join(repr(a) for a in allowed)}, got {raw!r}")
        return unit

    weight = pick("weight_unit", _WEIGHT_UNITS, ("lb", "kg"), "lb")
    distance = pick("distance_unit", _DISTANCE_UNITS, ("mi", "km"),
                    "mi" if weight == "lb" else "km")
    return {"weight": weight, "distance": distance}


# ---------------------------------------------------------------- values

class _NoUnit(ValueError):
    """A bare number where a duration needs a unit."""


def _num(text, decimal_comma: bool = False) -> float | None:
    """A number as the apps write it, or None for an empty cell; ValueError
    for text that is not one. '82,5' is a decimal comma anywhere; in a
    ';'-separated file (a decimal-comma locale) so is '1,234'. '1,234.5'
    and '1.234,5' carry thousands separators."""
    s = str(text if text is not None else "").replace(" ", "").replace(" ", "").strip()
    if not s:
        return None
    if re.fullmatch(r"[-+]?\d{1,3}(\.\d{3})+,\d+", s):
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+\.\d+", s):
        s = s.replace(",", "")
    elif re.fullmatch(r"[-+]?\d+,\d{1,2}", s) or (decimal_comma and re.fullmatch(
            r"[-+]?\d+,\d+", s)):
        s = s.replace(",", ".")
    elif re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+", s):
        s = s.replace(",", "")
    if not re.fullmatch(r"[-+]?(\d+(\.\d*)?|\.\d+)", s):
        raise ValueError(f"not a number: {text!r}")
    value = float(s)
    if not math.isfinite(value):
        raise ValueError(f"not a number: {text!r}")
    return value


def _clean(x: float) -> int | float:
    """185.0 -> 185, 82.50 -> 82.5: one spelling per number, so a re-import
    compares equal and the JSON reads the way the app shows it."""
    x = round(float(x), 3)
    return int(x) if x == int(x) else x


def _fmt(x) -> str:
    return ("%.2f" % float(x)).rstrip("0").rstrip(".")


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _collapse(text) -> str:
    return " ".join(str(text or "").split())


def _unit(table: dict, text: str | None) -> str | None:
    return table.get(str(text or "").strip().lower().rstrip(".")) if text else None


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
    start=1)}
_CLOCK = r"(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*([AaPp]\.?[Mm]\.?)?"
_ISO_TIME = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T]" + _CLOCK
                       + r"\s*(Z|[+-]\d{2}:?\d{2})?")
_DAY_MONTH = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4}),?\s+" + _CLOCK)
_MONTH_DAY = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4}),?\s+" + _CLOCK)


def _month(text: str) -> int:
    month = _MONTHS.get(text[:3].lower())
    if month is None:
        raise ValueError(f"not a month: {text!r}")
    return month


def parse_time(text) -> datetime:
    """A wall-clock datetime from the forms these apps write: ISO
    ('2026-10-09 06:30:00', Strong), day first ('9 Oct 2026, 06:30', Hevy;
    Strava in en-GB) or month first ('Oct 9, 2026, 6:30:00 AM', Strava in
    en-US). Naive unless the text states its zone ('Z', '+02:00'). Month
    names are matched in English by hand, never through the machine's
    locale. ValueError for anything else — a slashed date is refused, not
    guessed day- or month-first."""
    s = " ".join(str(text or "").split())
    zone = None
    m = _ISO_TIME.fullmatch(s)
    if m:
        y, mo, d, hh, mm, ss, ampm, zone = m.groups()
        month = int(mo)
    elif (m := _DAY_MONTH.fullmatch(s)) is not None:
        d, mon, y, hh, mm, ss, ampm = m.groups()
        month = _month(mon)
    elif (m := _MONTH_DAY.fullmatch(s)) is not None:
        mon, d, y, hh, mm, ss, ampm = m.groups()
        month = _month(mon)
    else:
        raise ValueError(f"not a date and time: {text!r}")
    hour = int(hh)
    if ampm:
        if not 1 <= hour <= 12:
            raise ValueError(f"not a 12-hour time: {text!r}")
        hour = hour % 12 + (12 if ampm[0] in "pP" else 0)
    dt = datetime(int(y), month, int(d), hour, int(mm), int(ss or 0))
    if zone:
        if zone == "Z":
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            sign = -1 if zone[0] == "-" else 1
            digits = zone[1:].replace(":", "")
            dt = dt.replace(tzinfo=timezone(sign * timedelta(
                hours=int(digits[:2]), minutes=int(digits[2:]))))
    return dt


_DURATION_PART = re.compile(r"(\d+(?:[.,]\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|"
                            r"seconds?|secs?|s)(?![a-z])", re.I)
_HMS = re.compile(r"(\d+):(\d{2}):(\d{2})")


def parse_duration(text, bare: str | None = None) -> float | None:
    """Seconds from '1h 15m', '55m', '45m 30s' or '1:15:00'; None for an
    empty cell. A bare number has no unit unless the column states one
    (`bare` 's' or 'min', from 'Duration (sec)'): it raises _NoUnit rather
    than be guessed as seconds or minutes. ValueError for anything else."""
    s = str(text or "").strip().lower()
    if not s:
        return None
    m = _HMS.fullmatch(s)
    if m:
        h, mi, sec = (int(g) for g in m.groups())
        return float(h * 3600 + mi * 60 + sec)
    if re.fullmatch(r"\d+(?:[.,]\d+)?", s):
        value = float(s.replace(",", "."))
        if bare == "s":
            return value
        if bare == "min":
            return value * 60
        raise _NoUnit(f"no unit: {text!r}")
    parts = _DURATION_PART.findall(s)
    if not parts or _DURATION_PART.sub("", s).strip(" ,"):
        raise ValueError(f"not a duration: {text!r}")
    total = 0.0
    for amount, unit in parts:
        n = float(amount.replace(",", "."))
        total += n * (3600 if unit[0] == "h" else 60 if unit[0] == "m" else 1)
    return total


def _duration_unit(header_unit: str | None) -> str | None:
    u = (header_unit or "").lower()
    if u in ("s", "sec", "secs", "second", "seconds"):
        return "s"
    if u in ("min", "mins", "minute", "minutes"):
        return "min"
    return None


# ---------------------------------------------------------------- CSV

def _norm(cell: str) -> str:
    return " ".join(cell.replace("﻿", "").strip().strip('"').lower().split())


def _decode(raw: bytes, fname: str) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if b"\x00" in raw[:4096]:
        raise DomainError(f"{fname} is binary, not a CSV export ({KNOWN})")
    return raw.decode("cp1252", errors="replace")


def _rows(fname: str, raw: bytes) -> tuple[list[str], list[tuple[int, list[str]]], str]:
    """(normalised header, [(line number, cells)], delimiter). The delimiter
    is whichever of ',' ';' and tab the header line uses most; an Excel
    'sep=;' first line names it outright."""
    text = _decode(raw, fname)
    if not text.strip():
        raise DomainError(f"{fname} is empty")
    lines = text.splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    delimiter = None
    if first.strip().lower().startswith("sep=") and len(first.strip()) == 5:
        delimiter = first.strip()[4]
        text = text[text.index(first) + len(first):].lstrip("\r\n")
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if delimiter is None:
        counts = {d: first.count(d) for d in (",", ";", "\t")}
        delimiter = max(counts, key=lambda d: (counts[d], d == ","))
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    header: list[str] | None = None
    rows: list[tuple[int, list[str]]] = []
    try:
        for row in reader:
            if not any(c.strip() for c in row):
                continue
            if header is None:
                header = [_norm(c) for c in row]
                continue
            rows.append((reader.line_num, row))
    except csv.Error as e:
        raise DomainError(f"{fname}: not readable as CSV: {e}") from e
    return header or [], rows, delimiter


def _sniff(names: list[str]) -> str | None:
    have = set(names)
    if {"activity id", "activity date"} <= have:
        return "strava"
    if {"title", "start_time", "exercise_title"} <= have:
        return "hevy"
    if {"date", "workout name", "exercise name"} <= have:
        return "strong"
    return None


def _fields(names: list[str], spec: dict[str, str]
            ) -> tuple[dict[str, int], dict[str, str | None], list[str]]:
    """(field -> column, field -> unit named in its header, columns not
    read). Each field takes the first column its pattern fully matches."""
    pos: dict[str, int] = {}
    units: dict[str, str | None] = {}
    taken: set[int] = set()
    for field, pattern in spec.items():
        rx = re.compile(pattern)
        for j, n in enumerate(names):
            m = rx.fullmatch(n)
            if m and j not in taken:
                pos[field], units[field] = j, m.groupdict().get("unit")
                taken.add(j)
                break
    unused = [n for j, n in enumerate(names) if j not in taken and n]
    return pos, units, unused


def _cell(row: list[str], pos: dict[str, int], field: str) -> str:
    j = pos.get(field)
    return row[j].strip() if j is not None and j < len(row) else ""


# ---------------------------------------------------------------- reading

class _Acc:
    """What one scan has gathered so far, across every file it reads."""

    def __init__(self, units: dict):
        self.units = units
        self.skipped: Counter = Counter()
        self.unmapped: Counter = Counter()
        self.examples: dict[str, str] = {}

    def skip(self, why: str) -> None:
        self.skipped[why] += 1

    def odd(self, what: str, example: str | None = None) -> None:
        self.unmapped[what] += 1
        if example:
            self.examples.setdefault(what, example)


class _Log:
    """The workouts of one Strong or Hevy file, grouped as rows arrive."""

    def __init__(self, app: str, fname: str):
        self.app = app
        self.fname = fname
        self.workouts: dict[tuple[str, str], dict] = {}
        self.assumed: set[str] = set()       # 'weight' / 'distance' units from the setting

    def workout(self, when: datetime, title: str) -> dict:
        key = (_stamp(when), title.casefold())
        w = self.workouts.get(key)
        if w is None:
            w = self.workouts[key] = {
                "app": self.app, "file": self.fname, "when": when, "title": title,
                "key": key, "exercises": [], "duration_s": None, "end": None,
                "notes": None, "duration_seen": False, "assumed": False}
        return w

    @staticmethod
    def add_set(w: dict, exercise: str, s: dict, notes: str = "",
                superset: str = "") -> None:
        exs = w["exercises"]
        if not exs or exs[-1]["name"] != exercise or exs[-1]["superset"] != superset:
            exs.append({"name": exercise, "sets": [], "notes": [], "superset": superset})
        ex = exs[-1]
        ex["sets"].append(s)
        if notes and notes not in ex["notes"]:
            ex["notes"].append(notes)


def _stamp(when: datetime) -> str:
    """The start as the file wrote it, for identity: wall-clock, plus the
    offset only when the file itself stated one."""
    text = when.strftime("%Y-%m-%dT%H:%M:%S")
    if when.tzinfo is not None:
        off = int(when.utcoffset().total_seconds() // 60)
        sign = "+" if off > 0 else "-"
        text += "Z" if off == 0 else f"{sign}{abs(off) // 60:02d}:{abs(off) % 60:02d}"
    return text


def _make_set(acc: _Acc, where: str, kind: str, cells: dict[str, str],
              weight_unit: str | None, distance_unit: str | None,
              decimal_comma: bool, log: _Log, w: dict) -> dict | None:
    """One set's numbers, or None (counted) when it has none or one does not
    parse. Zero is how the apps write "not recorded" — Strong puts 0 in the
    Distance and Seconds of every lifting set — so it is dropped too."""
    values: dict[str, float | None] = {}
    for field in ("weight", "reps", "distance", "seconds", "rpe"):
        try:
            v = _num(cells.get(field), decimal_comma)
        except ValueError:
            acc.odd(f"set with a {field} that is not a number (set not imported)", where)
            return None
        values[field] = v if v is not None and v > 0 else None
    if all(values[f] is None for f in ("weight", "reps", "distance", "seconds")):
        acc.skip("set with no weight, reps, distance or time")
        return None
    out: dict = {"type": kind}
    if values["weight"] is not None:
        if weight_unit is None:
            weight_unit = acc.units["weight"]
            log.assumed.add("weight")
            w["assumed"] = True
        out["weight"], out["unit"] = _clean(values["weight"]), weight_unit
    if values["reps"] is not None:
        out["reps"] = _clean(values["reps"])
    if values["distance"] is not None:
        if distance_unit is None:
            distance_unit = acc.units["distance"]
            log.assumed.add("distance")
            w["assumed"] = True
        out["distance"], out["distance_unit"] = _clean(values["distance"]), distance_unit
    if values["seconds"] is not None:
        out["seconds"] = _clean(values["seconds"])
    if values["rpe"] is not None:
        out["rpe"] = _clean(values["rpe"])
    return out


_STRONG_FIELDS = {
    "date": r"date",
    "workout": r"workout name",
    "duration": r"(?:workout )?duration(?: \((?P<unit>[a-z]+)\))?",
    "exercise": r"exercise name",
    "set": r"set order",
    "weight": r"weight(?: \((?P<unit>[a-z]+)\))?",
    "weight_unit": r"weight unit",
    "reps": r"reps",
    "distance": r"distance(?: \((?P<unit>[a-z]+)\))?",
    "distance_unit": r"distance unit",
    "seconds": r"seconds",
    "notes": r"notes",
    "workout_notes": r"workout notes",
    "rpe": r"rpe",
}
_STRONG_SET_TYPES = {"w": "warmup", "d": "dropset", "f": "failure"}


def _read_strong(fname: str, names: list[str], rows, decimal_comma: bool,
                 acc: _Acc) -> tuple[_Log, list[str]]:
    pos, header_units, unused = _fields(names, _STRONG_FIELDS)
    weight_header = _unit(_WEIGHT_UNITS, header_units.get("weight"))
    distance_header = _unit(_DISTANCE_UNITS, header_units.get("distance"))
    bare = _duration_unit(header_units.get("duration"))
    log = _Log("strong", fname)
    for lineno, row in rows:
        where = f"{fname}:{lineno}"
        text = _cell(row, pos, "date")
        try:
            when = parse_time(text)
        except ValueError:
            acc.odd("row with a date that does not parse", f"{where} ({text!r})")
            continue
        w = log.workout(when, _collapse(_cell(row, pos, "workout")) or "Workout")
        _duration(acc, w, _cell(row, pos, "duration"), bare, where)
        w["notes"] = w["notes"] or _collapse(_cell(row, pos, "workout_notes")) or None
        order = _cell(row, pos, "set")
        low = order.lower()
        if low.startswith("rest"):
            acc.skip("rest timer row (time between sets, not a set)")
            continue
        if order == "" or order.isdigit():
            kind = "normal"
        elif low in _STRONG_SET_TYPES:
            kind = _STRONG_SET_TYPES[low]
        else:
            kind = low
            acc.odd(f"set order this reader does not know ({order!r}) — kept as the "
                    f"set's type", where)
        exercise = _collapse(_cell(row, pos, "exercise"))
        if not exercise:
            acc.skip("set with no exercise name")
            continue
        raw_unit = _cell(row, pos, "weight_unit")
        weight_unit = _unit(_WEIGHT_UNITS, raw_unit) or weight_header
        if raw_unit and _unit(_WEIGHT_UNITS, raw_unit) is None:
            acc.odd(f"weight unit this reader does not know ({raw_unit!r})", where)
        raw_dist = _cell(row, pos, "distance_unit")
        distance_unit = _unit(_DISTANCE_UNITS, raw_dist) or distance_header
        if raw_dist and _unit(_DISTANCE_UNITS, raw_dist) is None:
            acc.odd(f"distance unit this reader does not know ({raw_dist!r})", where)
        s = _make_set(acc, where, kind,
                      {f: _cell(row, pos, f) for f in ("weight", "reps", "distance",
                                                        "seconds", "rpe")},
                      weight_unit, distance_unit, decimal_comma, log, w)
        if s is not None:
            log.add_set(w, exercise, s, _collapse(_cell(row, pos, "notes")))
    return log, unused


def _duration(acc: _Acc, w: dict, text: str, bare: str | None, where: str) -> None:
    """The workout's duration, from the first row that states it."""
    if not text or w["duration_seen"]:
        return
    w["duration_seen"] = True
    try:
        seconds = parse_duration(text, bare)
    except _NoUnit:
        acc.odd("workout duration with no unit (left empty rather than guessed)",
                f"{where} ({text!r})")
        return
    except ValueError:
        acc.odd("workout duration that does not parse", f"{where} ({text!r})")
        return
    w["duration_s"] = seconds if seconds and seconds > 0 else None


_HEVY_FIELDS = {
    "title": r"title",
    "start": r"start_time",
    "end": r"end_time",
    "description": r"description",
    "exercise": r"exercise_title",
    "superset": r"superset_id",
    "exercise_notes": r"exercise_notes",
    "set": r"set_index",
    "set_type": r"set_type",
    "weight": r"weight_(?P<unit>lbs|lb|kg)|weight",
    "reps": r"reps",
    "distance": r"distance_(?P<unit>km|miles|mi|meters|metres|m)|distance",
    "seconds": r"duration_seconds",
    "rpe": r"rpe",
}
_HEVY_SET_TYPES = ("normal", "warmup", "dropset", "failure")


def _read_hevy(fname: str, names: list[str], rows, decimal_comma: bool,
               acc: _Acc) -> tuple[_Log, list[str]]:
    pos, header_units, unused = _fields(names, _HEVY_FIELDS)
    weight_unit = _unit(_WEIGHT_UNITS, header_units.get("weight"))
    distance_unit = _unit(_DISTANCE_UNITS, header_units.get("distance"))
    log = _Log("hevy", fname)
    for lineno, row in rows:
        where = f"{fname}:{lineno}"
        text = _cell(row, pos, "start")
        try:
            when = parse_time(text)
        except ValueError:
            acc.odd("row with a start_time that does not parse", f"{where} ({text!r})")
            continue
        w = log.workout(when, _collapse(_cell(row, pos, "title")) or "Workout")
        end_text = _cell(row, pos, "end")
        if end_text and w["end"] is None:
            try:
                w["end"] = parse_time(end_text)
            except ValueError:
                acc.odd("workout end_time that does not parse", f"{where} ({end_text!r})")
        w["notes"] = w["notes"] or _collapse(_cell(row, pos, "description")) or None
        raw_type = _cell(row, pos, "set_type").lower()
        kind = raw_type or "normal"
        if kind not in _HEVY_SET_TYPES:
            acc.odd(f"set_type this reader does not know ({raw_type!r}) — kept as the "
                    f"set's type", where)
        exercise = _collapse(_cell(row, pos, "exercise"))
        if not exercise:
            acc.skip("set with no exercise_title")
            continue
        s = _make_set(acc, where, kind,
                      {f: _cell(row, pos, f) for f in ("weight", "reps", "distance",
                                                        "seconds", "rpe")},
                      weight_unit, distance_unit, decimal_comma, log, w)
        if s is not None:
            log.add_set(w, exercise, s, _collapse(_cell(row, pos, "exercise_notes")),
                        _cell(row, pos, "superset"))
    return log, unused


# ---------------------------------------------------------------- strength records

def _aware(when: datetime, cfg: Config) -> datetime:
    return when if when.tzinfo is not None else when.replace(tzinfo=ZoneInfo(cfg.timezone))


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(_ISO)


def _kg(s: dict) -> float | None:
    if s.get("weight") is None:
        return None
    return s["weight"] * _TO_KG.get(s.get("unit"), 1.0)


def _top_set(sets: list[dict]) -> dict | None:
    """The heaviest working set (more reps breaks a tie, the first wins a
    full tie). Warm-ups and sets with no reps never count."""
    best = None
    for s in sets:
        if s.get("type") == "warmup" or s.get("reps") is None or _kg(s) is None:
            continue
        mark = (round(_kg(s), 3), s["reps"])
        if best is None or mark > best[0]:
            best = (mark, s)
    if best is None:
        return None
    s = best[1]
    return {"weight": s["weight"], "unit": s["unit"], "reps": s["reps"],
            "weight_kg": round(_kg(s), 1)}


def _exercise_text(ex: dict) -> str:
    """'Bench Press (Barbell) 3×5 @ 185 lb', '225×5, 230×5 lb', '3×8',
    '2.5 mi in 24:10' — what one exercise came to, the way a lifter says it."""
    work = [s for s in ex["sets"] if s.get("type") != "warmup"]
    warm = len(ex["sets"]) - len(work)
    tail = f" (+{warm} warm-up)" if warm and work else ""
    if not work:
        return f"{ex['name']} {warm} warm-up set(s)"
    units = {s.get("unit") for s in work}
    if all(s.get("weight") is not None and s.get("reps") is not None for s in work):
        pairs = [(s["weight"], s["reps"]) for s in work]
        if len(units) > 1:
            text = ", ".join(f"{_fmt(s['weight'])} {s['unit']}×{_fmt(s['reps'])}" for s in work)
        elif len(set(pairs)) == 1:
            text = f"{len(work)}×{_fmt(pairs[0][1])} @ {_fmt(pairs[0][0])} {work[0]['unit']}"
        else:
            text = ", ".join(f"{_fmt(wt)}×{_fmt(r)}" for wt, r in pairs) + f" {work[0]['unit']}"
    elif all(s.get("weight") is None and s.get("reps") is not None for s in work):
        reps = [s["reps"] for s in work]
        text = (f"{len(work)}×{_fmt(reps[0])}" if len(set(reps)) == 1
                else ", ".join(_fmt(r) for r in reps) + " reps")
    elif all(s.get("distance") is not None for s in work) and \
            len({s["distance_unit"] for s in work}) == 1:
        text = f"{_fmt(sum(s['distance'] for s in work))} {work[0]['distance_unit']}"
        if all(s.get("seconds") is not None for s in work):
            text += f" in {_clock(sum(s['seconds'] for s in work))}"
    elif all(s.get("seconds") is not None and s.get("weight") is None for s in work):
        secs = [s["seconds"] for s in work]
        text = (f"{len(work)}×{_clock(secs[0])}" if len(set(secs)) == 1
                else ", ".join(_clock(x) for x in secs))
    else:
        text = f"{len(work)} sets"
    return f"{ex['name']} {text}{tail}"


def _strength_records(log: _Log, cfg: Config, acc: _Acc) -> list[dict]:
    out = []
    for w in log.workouts.values():
        start = _aware(w["when"], cfg)
        end = None
        if w["end"] is not None:
            end = _aware(w["end"], cfg)
        elif w["duration_s"]:
            end = start + timedelta(seconds=w["duration_s"])
        if end is not None and end < start:
            acc.odd("workout that ends before it starts (end left empty)",
                    f"{log.fname}: {_stamp(w['when'])}")
            end = None
        exercises, volume, km = [], 0.0, 0.0
        for ex in w["exercises"]:
            entry: dict = {"name": ex["name"], "sets": ex["sets"]}
            if ex["notes"]:
                entry["notes"] = " / ".join(ex["notes"])
            if ex["superset"]:
                entry["superset"] = ex["superset"]
            top = _top_set(ex["sets"])
            if top:
                entry["top_set"] = top
            exercises.append(entry)
            for s in ex["sets"]:
                if s.get("type") != "warmup" and s.get("reps") is not None and _kg(s):
                    volume += _kg(s) * s["reps"]
                if s.get("distance") is not None:
                    km += s["distance"] * _TO_KM[s["distance_unit"]]
        minutes = round((end - start).total_seconds() / 60, 1) if end is not None else None
        summary = "; ".join(_exercise_text(ex) for ex in w["exercises"])
        body = "\n".join(p for p in (summary, w["notes"]) if p) or None
        detail = {"app": log.app, "exercises": exercises or None,
                  "total_volume_kg": round(volume, 1) if volume else None,
                  "distance_km": round(km, 2) if km else None,
                  "notes": w["notes"],
                  "unit_source": "setting" if w["assumed"] else None}
        started = _utc(start)
        out.append({
            "stream": "workout",
            "external_id": f"{log.app}:{w['key'][0]}:{w['key'][1]}",
            "journal_date": base.journal_date_for_iso(started, cfg),
            "started_at": started,
            "ended_at": _utc(end) if end is not None else None,
            "utc_offset_sec": None,
            "direction": None,
            "title": w["title"],
            "body": body,
            "value": minutes,
            "unit": "min" if minutes is not None else None,
            "value_json": canonical_json({k: v for k, v in detail.items()
                                          if v is not None}),
            "media_path": None,
            "origin_app": log.app,
            "participants": [],
        })
    return out


# ---------------------------------------------------------------- Strava

_STRAVA_FIRST = {"id": "activity id", "date": "activity date", "name": "activity name",
                 "type": "activity type", "description": "activity description",
                 "private_note": "activity private note", "gear": "activity gear",
                 "filename": "filename", "commute": "commute",
                 "relative_effort": "relative effort", "calories": "calories",
                 "avg_hr": "average heart rate"}
# Repeated in the export's second, raw block (seconds, meters): read from there.
_STRAVA_LAST = {"elapsed": "elapsed time", "moving": "moving time",
                "elevation": "elevation gain", "max_hr": "max heart rate"}
_FOOT = ("run", "walk", "hike")


def _read_strava(fname: str, names: list[str], rows, decimal_comma: bool,
                 acc: _Acc, cfg: Config) -> tuple[list[dict], dict]:
    positions: dict[str, list[int]] = {}
    for j, n in enumerate(names):
        positions.setdefault(n, []).append(j)

    def first(row, col):
        for j in positions.get(col, ()):
            if j < len(row) and row[j].strip():
                return row[j].strip()
        return ""

    def last(row, col):
        for j in reversed(positions.get(col, ())):
            if j < len(row) and row[j].strip():
                return row[j].strip()
        return ""

    distance_cols = positions.get("distance", [])
    meters = len(distance_cols) >= 2
    used_display = False
    records, tracks, seen = [], 0, set()
    for lineno, row in rows:
        where = f"{fname}:{lineno}"
        cell = {k: first(row, c) for k, c in _STRAVA_FIRST.items()}
        cell.update({k: last(row, c) for k, c in _STRAVA_LAST.items()})
        activity_id = cell["id"]
        if not activity_id:
            acc.odd("activity row with no Activity ID", where)
            continue
        if activity_id in seen:
            acc.skip("activity listed twice in activities.csv")
            continue
        try:
            when = parse_time(cell["date"])
        except ValueError:
            acc.odd("activity with an Activity Date that does not parse",
                    f"{where} ({cell['date']!r})")
            continue
        # Strava writes Activity Date in UTC and says so nowhere in the file.
        start = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
        seen.add(activity_id)
        # A cell that is not a number costs that one figure, not the activity.
        nums: dict[str, float | None] = {}
        columns = {**_STRAVA_FIRST, **_STRAVA_LAST}
        for k in ("elapsed", "moving", "elevation", "max_hr", "avg_hr", "calories",
                  "relative_effort"):
            try:
                nums[k] = _num(cell[k], decimal_comma)
            except ValueError:
                nums[k] = None
                acc.odd(f"activity '{columns[k]}' that is not a number (left empty)",
                        f"{where} ({cell[k]!r})")
        km = None
        raw_m = row[distance_cols[1]].strip() if meters and distance_cols[1] < len(row) else ""
        shown = row[distance_cols[0]].strip() \
            if distance_cols and distance_cols[0] < len(row) else ""
        try:
            if raw_m:
                km = _num(raw_m, decimal_comma) / 1000
            elif shown:
                km = _num(shown, decimal_comma) * _TO_KM[acc.units["distance"]]
                used_display = True
        except ValueError:
            acc.odd("activity 'distance' that is not a number (left empty)",
                    f"{where} ({raw_m or shown!r})")
        nums = {k: (v if v is not None and v > 0 else None) for k, v in nums.items()}
        km = km if km and km > 0 else None
        kind = _collapse(cell["type"]) or None
        title = _collapse(cell["name"]) or kind or "Strava activity"
        elapsed, moving = nums["elapsed"], nums["moving"]
        length = elapsed or moving
        end = start + timedelta(seconds=length) if length else None
        headline = moving or elapsed
        minutes = round(headline / 60, 1) if headline else None
        filename = cell["filename"].replace("\\", "/").strip("/") or None
        if filename:
            tracks += 1
        description = cell["description"].strip() or None
        detail = {
            "app": "strava", "activity_id": activity_id, "activity_type": kind,
            "distance_km": round(km, 2) if km else None,
            "elevation_m": _clean(nums["elevation"]) if nums["elevation"] else None,
            "avg_hr": _clean(nums["avg_hr"]) if nums["avg_hr"] else None,
            "max_hr": _clean(nums["max_hr"]) if nums["max_hr"] else None,
            "calories": _clean(nums["calories"]) if nums["calories"] else None,
            "moving_s": _clean(moving) if moving else None,
            "elapsed_s": _clean(elapsed) if elapsed else None,
            "relative_effort": _clean(nums["relative_effort"])
            if nums["relative_effort"] else None,
            "gear": _collapse(cell["gear"]) or None,
            "commute": True if cell["commute"].lower() in ("true", "1", "yes") else None,
            "notes": description,
            "private_note": cell["private_note"].strip() or None,
            "unit_source": "setting" if km and not raw_m else None,
        }
        summary = _strava_summary(kind or title, km, headline, nums)
        started = _utc(start)
        records.append({
            "stream": "workout",
            "external_id": f"strava:{activity_id}",
            "journal_date": base.journal_date_for_iso(started, cfg),
            "started_at": started,
            "ended_at": _utc(end) if end is not None else None,
            "utc_offset_sec": None,
            "direction": None,
            "title": title,
            "body": "\n".join(p for p in (summary, description) if p) or None,
            "value": minutes,
            "unit": "min" if minutes is not None else None,
            "value_json": canonical_json({k: v for k, v in detail.items()
                                          if v is not None}),
            "media_path": filename,
            "origin_app": "strava",
            "participants": [],
        })
    return records, {"tracks": tracks, "display_distance": used_display}


def _strava_summary(kind: str, km: float | None, seconds: float | None,
                    nums: dict) -> str:
    """'Run 5.00 km in 27:00 (5:24 /km), +32 m, avg HR 158 / max 176, 412 kcal'."""
    parts = [kind]
    if km:
        text = f"{km:.2f} km"
        if seconds:
            text += f" in {_clock(seconds)}"
            low = kind.lower()
            if any(w in low for w in _FOOT):
                text += f" ({_clock(seconds / km)} /km)"
            elif "ride" in low or "cycl" in low:
                text += f" ({km / (seconds / 3600):.1f} km/h)"
        parts.append(text)
    elif seconds:
        parts.append(_clock(seconds))
    head = " ".join(parts)
    extra = []
    if nums.get("elevation"):
        extra.append(f"+{_fmt(round(nums['elevation']))} m")
    if nums.get("avg_hr") and nums.get("max_hr"):
        extra.append(f"avg HR {_fmt(round(nums['avg_hr']))} / max {_fmt(round(nums['max_hr']))}")
    elif nums.get("avg_hr") or nums.get("max_hr"):
        which = "avg" if nums.get("avg_hr") else "max"
        extra.append(f"{which} HR {_fmt(round(nums.get('avg_hr') or nums['max_hr']))}")
    if nums.get("calories"):
        extra.append(f"{_fmt(round(nums['calories']))} kcal")
    return ", ".join([head, *extra])


# ---------------------------------------------------------------- targets

def _junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p.startswith(".") or p == "__MACOSX" for p in parts)


def _is_track(rel: str) -> bool:
    low = rel.lower()
    return low.endswith(_TRACK_SUFFIXES) or "/activities/" in f"/{low}"


def _sources(target: Path) -> tuple[str, list[tuple[str, bytes]], Counter, list[str]]:
    """(target kind, [(name, bytes)] of every CSV, Counter of files not read
    by why, examples). Only CSVs are ever opened: a Strava archive's GPS
    tracks are counted from the listing alone."""
    ignored: Counter = Counter()
    examples: list[str] = []

    def other(rel: str) -> None:
        if _is_track(rel):
            ignored["activity file (GPS track) — never parsed; each activity "
                    "keeps its path as media_path"] += 1
        elif rel.lower().endswith(".zip") and target.is_dir():
            ignored[f"zip in the folder, not opened — import it on its own "
                    f"({PurePosixPath(rel).name})"] += 1
        else:
            ignored["file that is not a CSV export"] += 1
            if len(examples) < 3:
                examples.append(rel)

    if target.is_dir():
        found = []
        for p in sorted(target.rglob("*")):
            rel = p.relative_to(target).as_posix()
            if not p.is_file() or _junk(rel):
                continue
            if p.suffix.lower() == ".csv":
                found.append((rel, p.read_bytes()))
            else:
                other(rel)
        return "folder", found, ignored, examples
    if zipfile.is_zipfile(target):
        found = []
        try:
            with zipfile.ZipFile(target) as zf:
                for info in sorted(zf.infolist(), key=lambda i: i.filename):
                    if info.is_dir() or _junk(info.filename):
                        continue
                    if info.filename.lower().endswith(".csv"):
                        found.append((info.filename, zf.read(info)))
                    else:
                        other(info.filename)
        except zipfile.BadZipFile as e:
            raise DomainError(f"{target.name} is not a readable zip: {e}") from e
        return "zip", found, ignored, examples
    return "file", [(target.name, target.read_bytes())], ignored, examples


def _content_hash(found: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel, raw in found:
        h.update(f"{rel}\0{hashlib.sha256(raw).hexdigest()}\n".encode("utf-8"))
    return h.hexdigest()


def _account(target: Path, files: list[dict]) -> str | None:
    """'strava:<athlete id>' from the archive's own name (export_<id>.zip,
    or the folder it unzips to). Informational only: nothing here mirrors."""
    for candidate in [target.name] + [PurePosixPath(f["file"]).parts[0]
                                      for f in files if f["app"] == "strava"]:
        m = _STRAVA_EXPORT.match(candidate)
        if m:
            return f"strava:{m.group(1)}"
    return None


def _is_file(p: Path) -> bool:
    """is_file() that answers False for a folder macOS will not let us look
    into (Downloads, iCloud Drive before permission is granted)."""
    try:
        return p.is_file()
    except OSError:
        return False


def default_target(cfg: Config) -> Path:
    """[connectors.fitness] path, else the one workout export sitting where
    downloads and synced folders land: strong.csv, workouts.csv (Hevy) or
    Strava's export_<id>.zip. Several are refused — pass the one to import."""
    settings = cfg.connector_settings(name)
    if settings.get("path"):
        p = cfg.resolve(settings["path"])
        if not p.exists():
            raise DomainError(f"[connectors.fitness] path does not exist: {p}")
        return p
    folders = [platforms.home() / "Downloads", *platforms.onedrive_candidates()]
    if platforms.is_mac():
        folders.append(platforms.icloud_drive())
    found, seen = [], set()
    for folder in folders:
        candidates = [folder / "strong.csv", folder / "workouts.csv"]
        try:
            candidates += sorted(folder.glob("export_*.zip"))
        except OSError:
            pass
        for p in candidates:
            if str(p).lower() not in seen and _is_file(p):
                seen.add(str(p).lower())
                found.append(p)
    if len(found) == 1:
        return found[0]
    if found:
        raise base.Ambiguous("found several workout exports — pass the one to import: "
                          + "; ".join(str(p) for p in found))
    raise DomainError(
        "no workout export found — looked for strong.csv, workouts.csv and "
        "export_<id>.zip in " + ", ".join(str(f) for f in folders)
        + "; export from the app (docs/fitness.md), pass the file, or set "
          "[connectors.fitness] path")


# ---------------------------------------------------------------- scan

def _read(fname: str, raw: bytes, cfg: Config, acc: _Acc) -> dict | None:
    """One CSV: its records and what the report says about it, or None when
    it is not a workout export at all."""
    names, rows, delimiter = _rows(fname, raw)
    app = _sniff(names)
    if app is None:
        return None
    decimal_comma = delimiter == ";"
    before = acc.unmapped + acc.skipped
    meta: dict = {}
    if app == "strava":
        records, meta = _read_strava(fname, names, rows, decimal_comma, acc, cfg)
        unused: list[str] = []
        assumed = {"distance"} if meta["display_distance"] else set()
    else:
        reader = _read_strong if app == "strong" else _read_hevy
        log, unused = reader(fname, names, rows, decimal_comma, acc)
        records = _strength_records(log, cfg, acc)
        assumed = log.assumed
    if not records:
        seen = (acc.unmapped + acc.skipped) - before
        why = (f"{len(rows)} row(s), none usable — "
               + "; ".join(f"{n} × {k}" for k, n in sorted(seen.items()))
               if rows else "a header and no rows")
        raise DomainError(f"{fname}: {LABELS[app]} with no workout in it ({why})")
    # No edit clock in any of these files: the export's own "as of" stands in.
    as_of = max(r["ended_at"] or r["started_at"] for r in records)
    for r in records:
        r["last_modified_at"] = as_of
    dates = [r["journal_date"] for r in records]
    return {"records": records,
            "file": {"file": fname, "app": app, "workouts": len(records),
                     "first": min(dates), "last": max(dates), "as_of": as_of,
                     "delimiter": delimiter, "assumed": sorted(assumed),
                     "columns_not_read": unused, **meta}}


def scan(target: Path, cfg: Config) -> base.Scan:
    units = _units(cfg)
    acc = _Acc(units)
    try:
        kind, found, ignored, examples = _sources(target)
        results, not_workouts, errors = [], [], []
        for fname, raw in found:
            try:
                result = _read(fname, raw, cfg, acc)
            except DomainError as e:
                if kind == "file":
                    raise
                errors.append(str(e))
                continue
            if result is None:
                if kind == "file":
                    names, _, _ = _rows(fname, raw)
                    seen = ", ".join(n for n in names if n)[:160] or "(no header)"
                    hint = (" — a single activity file is never read; pass Strava's "
                            "export zip" if target.suffix.lower() in _TRACK_SUFFIXES else "")
                    raise DomainError(f"{fname}: not a workout export this connector "
                                      f"knows — first row: {seen} (known: {KNOWN}){hint}")
                not_workouts.append(fname)
                continue
            results.append(result)
    except PermissionError as e:
        raise DomainError(
            f"cannot read {e.filename or target}: permission denied — on a Mac, "
            f"allow the app running this to use that folder (System Settings > "
            f"Privacy & Security > Files and Folders), then run it again") from e
    except OSError as e:
        raise DomainError(f"cannot read {target}: {e.strerror or e}") from e
    if not results:
        if errors:
            raise DomainError(errors[0] if len(errors) == 1 else
                              f"none of the {len(errors)} workout file(s) in "
                              f"{target.name} has a readable workout — first: {errors[0]}")
        csvs = ", ".join(f for f, _ in found[:6]) or "no CSV at all"
        raise DomainError(f"{target.name} holds no Strong, Hevy or Strava export "
                          f"(CSVs: {csvs}; known: {KNOWN})")

    # One workout in two exports (an old strong.csv beside a new one): the
    # copy from the export with the later "as of" is the one that counts.
    chosen: dict[tuple[str, str], dict] = {}
    twice = 0
    for result in results:
        for rec in result["records"]:
            key = (rec["stream"], rec["external_id"])
            prior = chosen.get(key)
            if prior is not None:
                twice += 1
                if prior["last_modified_at"] > rec["last_modified_at"]:
                    continue
            chosen[key] = rec
    records = sorted(chosen.values(),
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    files = [r["file"] for r in results]
    skipped = [f"{n} × {why}" for why, n in sorted(acc.skipped.items())]
    if twice:
        skipped.append(f"{twice} × workout in more than one export (the newest "
                       f"export's copy kept)")
    for why, n in sorted(ignored.items()):
        skipped.append(f"{n} × {why}" + (f" (e.g. {examples[0]})"
                                         if why.startswith("file that") and examples else ""))
    if not_workouts:
        skipped.append(f"{len(not_workouts)} × CSV that is not a workout export "
                       f"(e.g. {not_workouts[0]})")
    unmapped = [{"what": k, "count": v, "example": acc.examples.get(k)}
                for k, v in sorted(acc.unmapped.items())]
    unmapped += [{"what": e, "count": 1, "example": None} for e in errors]
    dates = [r["journal_date"] for r in records]
    as_of = max(f["as_of"] for f in files)
    # Strong, Hevy and Strava export every workout the app holds: between the
    # first one and the export's newest moment, a day without a row is a day
    # without a workout in that app (docs/assistant.md).
    coverage = []
    if dates:
        coverage.append({"stream": "workout", "account": _account(target, files),
                         "start": min(dates), "end": max(max(dates), as_of[:10]),
                         "basis": "full-history export (Strong, Hevy and Strava export "
                                  "every workout)"})
    return base.empty_scan(
        name,
        account=_account(target, files),
        source_file=target.name,
        source_hash=base.file_hash(target) if kind == "file" else _content_hash(found),
        records=records,
        handles=[],
        unmapped=unmapped,
        skipped=skipped,
        facts={"files": files, "weight_unit": units["weight"],
               "distance_unit": units["distance"],
               "as_of": as_of,
               "oldest_workout": min(dates) if dates else None,
               "newest_workout": max(dates) if dates else None},
        coverage=coverage,
        target_kind=kind,
        files=files,
        timezone=cfg.timezone)


# ---------------------------------------------------------------- report

def _local(rec: dict, tz: str) -> str:
    when = datetime.strptime(rec["started_at"], _ISO).replace(tzinfo=timezone.utc)
    return when.astimezone(ZoneInfo(tz)).strftime("%H:%M")


def _sample(rec: dict, tz: str) -> str:
    mins = f", {_fmt(rec['value'])} min" if rec.get("value") is not None else ""
    text = f"{rec['journal_date']} {_local(rec, tz)} {rec['title']}{mins}"
    first = (rec.get("body") or "").splitlines()[0] if rec.get("body") else ""
    if first:
        text += " — " + (first if len(first) <= 90 else first[:89] + "…")
    return text


def _units_note(f: dict, facts: dict) -> str | None:
    assumed = f.get("assumed") or []
    if not assumed:
        return None
    if f["app"] == "strava":
        return (f"distance read as {facts['distance_unit']} ([connectors.fitness] "
                f"distance_unit — this export has no meters column)")
    said = []
    if "weight" in assumed:
        said.append(f"weights as {facts['weight_unit']}")
    if "distance" in assumed:
        said.append(f"distances as {facts['distance_unit']}")
    keys = " / ".join(k for k, a in (("weight_unit", "weight"), ("distance_unit", "distance"))
                      if a in assumed)
    return (f"read {' and '.join(said)} ([connectors.fitness] {keys} — the file does not "
            f"say; check one set against the app)")


def personal_bests(records: list[dict], limit: int = 5) -> list[str]:
    """'new best: Bench Press (Barbell) 185 lb × 5 on 2026-10-09, up from
    180 lb × 5 on 2026-09-27' — per exercise, the latest workout whose
    heaviest working set beat every earlier workout in these records. From
    the records alone; nothing is stored."""
    best: dict[str, tuple] = {}
    latest: dict[str, tuple] = {}
    for rec in sorted(records, key=lambda r: (r["started_at"], r["external_id"])):
        if not rec.get("value_json"):
            continue
        for ex in json.loads(rec["value_json"]).get("exercises") or []:
            top = ex.get("top_set")
            if not top:
                continue
            key = " ".join(ex["name"].split()).casefold()
            mark = (top["weight_kg"], top.get("reps") or 0)
            prior = best.get(key)
            if prior is None or mark > prior[0]:
                if prior is not None:
                    latest[key] = (rec["started_at"], rec["journal_date"], ex["name"],
                                   top, prior[1], prior[2])
                best[key] = (mark, rec["journal_date"], top)

    def said(top: dict) -> str:
        reps = f" × {_fmt(top['reps'])}" if top.get("reps") is not None else ""
        return f"{_fmt(top['weight'])} {top['unit']}{reps}"

    events = sorted(latest.values(), key=lambda e: (e[0], e[2]), reverse=True)
    lines = [f"new best: {exname} {said(top)} on {day}, up from {said(was)} on {was_day}"
             for _, day, exname, top, was_day, was in events[:limit]]
    if len(events) > limit:
        lines.append(f"… and {len(events) - limit} more exercise(s) with a new best")
    return lines


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    files = scan.get("files") or []
    tz = scan.get("timezone") or "UTC"
    kinds = Counter(LABELS[f["app"]] for f in files)
    lines = [f"fitness: {scan['source_file']} ("
             + ", ".join(f"{k} ×{n}" for k, n in sorted(kinds.items())) + ")"]
    for f in files[:10]:
        line = (f"  file {f['file']}: {LABELS[f['app']]}, {f['workouts']} workout(s) "
                f"{f['first']}..{f['last']}")
        if f["app"] == "strava" and f.get("tracks"):
            line += f", {f['tracks']} activity file(s) referenced, never parsed"
        note = _units_note(f, facts)
        if note:
            line += f"; {note}"
        if f.get("columns_not_read"):
            line += f"; columns not read: {', '.join(f['columns_not_read'][:6])}"
        lines.append(line)
    if len(files) > 10:
        lines.append(f"  … and {len(files) - 10} more file(s)")

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    if new or upd:
        recs = new or upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  workout: {len(new)} new, {len(upd)} changed | {span} "
                     f"| e.g. {_sample(latest, tz)}")
        minutes = sum(r["value"] for r in recs if r.get("value") is not None)
        km = sum(json.loads(r["value_json"]).get("distance_km", 0)
                 for r in recs if r.get("value_json"))
        lines.append(f"  totals across these: {_fmt(round(minutes / 60, 1))} h"
                     + (f", {_fmt(round(km, 1))} km" if km else "")
                     + " — compare with the app's own history")
    else:
        lines.append("  nothing new — the export matches the database")
    lines += ["  " + b for b in personal_bests(scan["records"])]

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} unchanged workout(s) re-dated to this export")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} workout(s) listed twice")
    if tail:
        lines.append("  " + ", ".join(tail))
    if plan["conflicts"]:
        lines.append(f"  kept existing: {len(plan['conflicts'])} workout(s) differ from "
                     f"what a newer export already stored — an older export cannot "
                     f"overwrite them")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not understood: {u['count']} × {u['what']}"
                     + (f" (e.g. {u['example']})" if u.get("example") else ""))
    return lines

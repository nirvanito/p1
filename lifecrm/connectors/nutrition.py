"""Nutrition connector: what you ate, from a Cronometer export (or MyFitnessPal's).

"If you know every single thing that you eat, it tells you exactly what
you're deficient in" — the owner's goal, and the reason this reader exists.
A food diary already holds the record, food by food, with ~80 nutrients
each; this connector copies it in so `lifecrm nutrients` can set it beside
the reference intakes, offline and deterministically. It never logs in
anywhere and never calls an API.

Accepted targets
----------------
- one export file, recognised by its header row, not its name:
    servings.csv       Cronometer "Food & Recipe Entries":
                       Day,Time,Group,Food Name,Amount,Energy (kcal),
                       Alcohol (g),Caffeine (mg),... ~80 nutrient columns,
                       each naming its unit
    dailysummary.csv   Cronometer "Daily Nutrition": Date, the same nutrient
                       columns, Completed
    Nutrition-Summary-<from>-to-<to>.csv   MyFitnessPal (Premium only):
                       Date,Meal[,Time],Calories,Fat (g),...,Note — one row
                       per meal, not per food
- a folder or .zip holding them (searched recursively). Cronometer's
  exercises.csv, biometrics.csv and notes.csv and MyFitnessPal's exercise
  and measurement summaries are recognised, counted and skipped: not food.
  Two files of one kind in one target are refused — each is an export of
  its own date range, so they go in one at a time.
- nothing, when `path` is set in [connectors.nutrition]

What comes in
-------------
  food_log       one per serving: title the food name, body amount and meal
                 ("1.00 cup · Breakfast"), value its energy in kcal,
                 value_json {meal, amount, category, time, nutrients}.
                 A MyFitnessPal row is a meal total, not a food: title
                 "<Meal> (meal total)", value_json.aggregate = 'meal'.
  nutrition_day  one per day of Cronometer's daily summary: title
                 "Nutrition <date>", value the day's kcal, value_json
                 {nutrients, completed}.

`nutrients` maps a stable key to [value, unit]: {"magnesium": [312.4, "mg"],
"vitamin_d": [4.1, "µg"]}. The key table (NUTRIENTS below, and
docs/nutrition.md) gives every known column one key and one canonical unit,
so a nutrient reads the same whichever app or year it came from:

- masses convert exactly (Cronometer's fluoride in µg is stored in mg, the
  unit its reference intake uses); kJ convert to kcal;
- vitamin D in IU converts to µg (40 IU = 1 µg, exact by definition);
- vitamin A and vitamin E in IU do NOT convert — the factor depends on the
  form (retinol or carotene, natural or synthetic tocopherol) and the
  export does not say which — so they stay in IU under their own key
  (`vitamin_a_iu`), never summed with the µg figure;
- a column not in the table, or with a unit that does not convert, is kept
  as-is: its header is its key, with the unit it stated (None when it
  stated none — MyFitnessPal's Vitamin A / Vitamin C / Calcium / Iron, which
  the app has shown as % of daily value). Nothing is dropped; the dry run
  names every such column.

A blank cell is "not known for this food", not zero, and is left out. A
cell that is not a number is kept as text under value_json.other and
counted in the report.

Dates and times
---------------
journal_date is the diary day the app filed the food under — the day the
owner chose — and the rollover hour is NOT applied again: a 00:40 snack
Cronometer files under Tuesday stays Tuesday, so a day's servings always
add up to that day's summary. started_at is Day + Time in the configured
timezone (the export states no zone), and local noon when the export has
no time (Cronometer's Time column is empty unless timestamps are on);
value_json.time says which. A daily summary sits at local noon too: a
placeholder time, never an instant.

Identity
--------
- serving: 'cronometer:serving:' + sha1(day, time, group, food, amount,
  ordinal), the ordinal counting identical rows of a day so two eggs logged
  twice stay two servings. Overlapping exports give the same ids for the
  same rows. A serving edited in Cronometer after it was imported (another
  amount, another time) is a new serving; the old one stays until its
  import is retracted — the daily summary, which the report prefers, is
  updated in place either way.
- daily summary: 'cronometer:day:<date>'.
- MyFitnessPal meal: 'myfitnesspal:meal:<date>:<meal>:<ordinal>'.

Re-importing, and what wins
---------------------------
The exports carry no edit times, so every record's last_modified_at is the
export's own "as of": the latest instant it describes. A later export wins —
today's half-logged summary is replaced by tomorrow's export of the full
day, audited — and an older export overwrites nothing (a difference is
reported as kept-existing). Two exports taken on the same day without
times describe the same instant; the second cannot update the first, and
the next day's export does.

Deletion semantics — remember, never mirror
-------------------------------------------
An export is a date range the owner picks, not the diary's whole state: a
day absent from this month's file is in last month's. Nothing is removed
because a later export does not contain it; `imports retract` undoes a run.

Not here
--------
health_records: that tier belongs to Health Connect and Apple Health (F8),
whose nutrition rows carry energy and macros only. Cronometer can also
write to them — import one or the other for intake, or the report and the
health metrics count one meal from two places. Cronometer's biometrics
(weight, blood pressure) are skipped: the health importers own those
metrics. No people are in a food diary, so there are no handles.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "nutrition"
streams = ("food_log", "nutrition_day")
# Exports are taken by hand; nothing newer in six weeks means the habit (or
# the exporting) stopped, and the nutrient report is describing the past.
stale_after_days = 45

MICRO = "µg"                       # µg, the micro sign Cronometer writes
_ISO = "%Y-%m-%dT%H:%M:%SZ"


# ---------------------------------------------------------------- nutrient keys

# (key, canonical unit, the names a header goes by — lower-case, unit removed)
NUTRIENTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("energy", "kcal", ("energy", "calories")),
    ("protein", "g", ("protein",)),
    ("carbohydrate", "g", ("carbs", "carbohydrates", "carbohydrate", "total carbs",
                           "total carbohydrate")),
    ("net_carbs", "g", ("net carbs",)),
    ("fiber", "g", ("fiber", "fibre", "dietary fiber")),
    ("soluble_fiber", "g", ("soluble fiber",)),
    ("insoluble_fiber", "g", ("insoluble fiber",)),
    ("starch", "g", ("starch",)),
    ("sugars", "g", ("sugars", "sugar", "total sugars")),
    ("added_sugars", "g", ("added sugars", "added sugar")),
    ("sugar_alcohol", "g", ("sugar alcohol", "sugar alcohols")),
    ("fructose", "g", ("fructose",)),
    ("galactose", "g", ("galactose",)),
    ("glucose", "g", ("glucose",)),
    ("lactose", "g", ("lactose",)),
    ("maltose", "g", ("maltose",)),
    ("sucrose", "g", ("sucrose",)),
    ("fat", "g", ("fat", "total fat")),
    ("saturated_fat", "g", ("saturated", "saturated fat")),
    ("monounsaturated_fat", "g", ("monounsaturated", "monounsaturated fat")),
    ("polyunsaturated_fat", "g", ("polyunsaturated", "polyunsaturated fat")),
    ("trans_fat", "g", ("trans-fats", "trans fats", "trans-fat", "trans fat")),
    ("cholesterol", "mg", ("cholesterol",)),
    ("omega_3", "g", ("omega-3", "omega 3")),
    ("omega_6", "g", ("omega-6", "omega 6")),
    ("ala", "g", ("ala", "alpha-linolenic acid")),
    ("epa", "g", ("epa",)),
    ("dha", "g", ("dha",)),
    ("alcohol", "g", ("alcohol",)),
    ("caffeine", "mg", ("caffeine",)),
    ("water", "g", ("water",)),
    ("thiamin", "mg", ("b1 (thiamine)", "thiamine", "thiamin", "vitamin b1")),
    ("riboflavin", "mg", ("b2 (riboflavin)", "riboflavin", "vitamin b2")),
    ("niacin", "mg", ("b3 (niacin)", "niacin", "vitamin b3")),
    ("pantothenic_acid", "mg", ("b5 (pantothenic acid)", "pantothenic acid",
                                "vitamin b5")),
    ("vitamin_b6", "mg", ("b6 (pyridoxine)", "vitamin b6", "pyridoxine")),
    ("biotin", MICRO, ("biotin", "b7 (biotin)", "vitamin b7")),
    ("folate", MICRO, ("folate", "folate (dfe)", "folate, dfe", "b9 (folate)")),
    ("folic_acid", MICRO, ("folic acid",)),
    ("vitamin_b12", MICRO, ("b12 (cobalamin)", "vitamin b12", "cobalamin")),
    ("choline", "mg", ("choline",)),
    ("vitamin_a", MICRO, ("vitamin a", "vitamin a (rae)", "vitamin a, rae")),
    ("retinol", MICRO, ("retinol",)),
    ("alpha_carotene", MICRO, ("alpha-carotene", "alpha carotene")),
    ("beta_carotene", MICRO, ("beta-carotene", "beta carotene")),
    ("beta_cryptoxanthin", MICRO, ("beta-cryptoxanthin", "beta cryptoxanthin")),
    ("lutein_zeaxanthin", MICRO, ("lutein+zeaxanthin", "lutein + zeaxanthin")),
    ("lycopene", MICRO, ("lycopene",)),
    ("vitamin_c", "mg", ("vitamin c",)),
    ("vitamin_d", MICRO, ("vitamin d",)),
    ("vitamin_e", "mg", ("vitamin e",)),
    ("vitamin_k", MICRO, ("vitamin k",)),
    ("calcium", "mg", ("calcium",)),
    ("chromium", MICRO, ("chromium",)),
    ("copper", "mg", ("copper",)),
    ("fluoride", "mg", ("fluoride",)),
    ("iodine", MICRO, ("iodine",)),
    ("iron", "mg", ("iron",)),
    ("magnesium", "mg", ("magnesium",)),
    ("manganese", "mg", ("manganese",)),
    ("molybdenum", MICRO, ("molybdenum",)),
    ("phosphorus", "mg", ("phosphorus",)),
    ("potassium", "mg", ("potassium",)),
    ("selenium", MICRO, ("selenium",)),
    ("sodium", "mg", ("sodium",)),
    ("zinc", "mg", ("zinc",)),
    ("cystine", "g", ("cystine",)),
    ("histidine", "g", ("histidine",)),
    ("isoleucine", "g", ("isoleucine",)),
    ("leucine", "g", ("leucine",)),
    ("lysine", "g", ("lysine",)),
    ("methionine", "g", ("methionine",)),
    ("phenylalanine", "g", ("phenylalanine",)),
    ("threonine", "g", ("threonine",)),
    ("tryptophan", "g", ("tryptophan",)),
    ("tyrosine", "g", ("tyrosine",)),
    ("valine", "g", ("valine",)),
)
_ALIASES: dict[str, tuple[str, str]] = {
    alias: (key, unit) for key, unit, aliases in NUTRIENTS for alias in aliases}

_UNIT_WORDS = {"g": "g", "mg": "mg", "µg": MICRO, "μg": MICRO, "mcg": MICRO,
               "ug": MICRO, "kcal": "kcal", "kj": "kJ", "iu": "IU", "%": "%DV",
               "% dv": "%DV", "%dv": "%DV", "dv": "%DV"}
_FACTORS = {("mg", MICRO): 1000.0, (MICRO, "mg"): 0.001, ("g", "mg"): 1000.0,
            ("mg", "g"): 0.001, ("g", MICRO): 1_000_000.0, (MICRO, "g"): 0.000001,
            ("kJ", "kcal"): 1 / 4.184}
# µg per IU where one factor holds for every form: vitamin D only (40 IU = 1 µg).
_IU_FACTORS = {"vitamin_d": 0.025}

# MyFitnessPal writes most units only on some columns; these are the ones its
# app shows for the rest. Vitamin A/C, Calcium and Iron are deliberately
# absent: the app has shown them as % of daily value, so they stay as-is.
MFP_UNITS = {"calories": "kcal", "fat": "g", "saturated fat": "g",
             "polyunsaturated fat": "g", "monounsaturated fat": "g",
             "trans fat": "g", "cholesterol": "mg", "sodium": "mg",
             "potassium": "mg", "carbohydrates": "g", "fiber": "g", "sugar": "g",
             "protein": "g"}

_UNIT_TAIL = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


@dataclass(frozen=True)
class Column:
    header: str                 # as the file wrote it (whitespace collapsed)
    key: str                    # the stable key, or the header when not in the table
    unit: str | None            # the unit stored
    factor: float = 1.0         # source value x factor = stored value
    known: bool = True          # False: kept as-is
    source_unit: str | None = None


def _split_unit(label: str) -> tuple[str, str | None]:
    """('Vitamin D', 'IU') from 'Vitamin D (IU)'; ('B1 (Thiamine)', 'mg') from
    'B1 (Thiamine) (mg)'. Parentheses that hold no unit stay in the name."""
    m = _UNIT_TAIL.match(label)
    if m:
        unit = _UNIT_WORDS.get(" ".join(m.group(2).lower().split()))
        if unit is not None and m.group(1).strip():
            return m.group(1).strip(), unit
    return label, None


def nutrient_column(header: str, defaults: dict | None = None) -> Column:
    """The stable key, stored unit and conversion for one nutrient column."""
    label = " ".join(str(header).replace("﻿", "").split())
    base_name, unit = _split_unit(label)
    low = base_name.lower()
    hit = _ALIASES.get(low)
    if unit is None and defaults:
        unit = defaults.get(low)
    if hit is None or unit is None:
        return Column(label, label, unit, known=False, source_unit=unit)
    key, canonical = hit
    if unit == canonical:
        return Column(label, key, canonical, source_unit=unit)
    if unit == "IU":
        if key in _IU_FACTORS:
            return Column(label, key, canonical, _IU_FACTORS[key], source_unit="IU")
        return Column(label, f"{key}_iu", "IU", source_unit="IU")
    factor = _FACTORS.get((unit, canonical))
    if factor is None:
        return Column(label, label, unit, known=False, source_unit=unit)
    return Column(label, key, canonical, factor, source_unit=unit)


# ---------------------------------------------------------------- cells

def parse_number(text) -> float | None:
    """A nutrient cell: None when blank, else a float; ValueError otherwise.
    Thousands commas ('1,234.5') are accepted."""
    s = str(text if text is not None else "").replace(" ", " ").strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    if not re.fullmatch(r"-?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?", s):
        raise ValueError(f"not a number: {text!r}")
    return float(s)


_DAY_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%m/%d/%y", "%b %d, %Y",
                "%B %d, %Y", "%d %b %Y")


def parse_day(text) -> date | None:
    """A diary day: ISO first (both apps write it), then US month-first."""
    s = " ".join(str(text if text is not None else "").split())
    if not s:
        return None
    s = re.sub(r"[ T]\d{1,2}:\d{2}(:\d{2})?$", "", s)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            raise ValueError(f"not a date: {text!r}") from None
    for fmt in _DAY_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"not a date: {text!r}")


_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp])?\.?\s*(?:[Mm]\.?)?$")


def parse_time(text) -> time | None:
    """'08:05', '8:05 AM', '20:05:30', '8:05 p.m.'; None when blank."""
    s = " ".join(str(text if text is not None else "").split())
    if not s:
        return None
    m = _TIME_RE.match(s)
    if not m:
        raise ValueError(f"not a time: {text!r}")
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    half = (m.group(4) or "").lower()
    if half:
        if not 1 <= hh <= 12:
            raise ValueError(f"not a time: {text!r}")
        hh = hh % 12 + (12 if half == "p" else 0)
    if hh > 23 or mm > 59 or ss > 59:
        raise ValueError(f"not a time: {text!r}")
    return time(hh, mm, ss)


def _completed(text) -> bool | None:
    s = str(text if text is not None else "").strip().lower()
    if s in ("true", "yes", "y", "1", "x", "completed"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


def _collapse(text) -> str:
    return " ".join(str(text if text is not None else "").split())


def _norm(cell: str) -> str:
    return " ".join(str(cell).replace("﻿", "").strip().strip('"').lower().split())


# ---------------------------------------------------------------- file kinds

FOOD_KINDS = {"servings": "Cronometer servings",
              "daily": "Cronometer daily summary",
              "mfp": "MyFitnessPal nutrition summary"}
SKIP_KINDS = {"cronometer_exercises": "Cronometer exercise log",
              "cronometer_biometrics": "Cronometer biometrics",
              "cronometer_notes": "Cronometer notes",
              "mfp_exercise": "MyFitnessPal exercise summary",
              "mfp_measurement": "MyFitnessPal measurement summary"}
# Columns that are not nutrients, per kind (normalised names).
_TEXT_COLUMNS = {"servings": ("day", "time", "group", "food name", "amount", "category"),
                 "daily": ("date", "day", "completed"),
                 "mfp": ("date", "meal", "time", "note")}


def _has_energy(names: list[str]) -> bool:
    return any(nutrient_column(n, MFP_UNITS).key == "energy" for n in names)


def file_kind(names: list[str]) -> str | None:
    """Which export a header row belongs to, or None."""
    have = set(names)
    if "food name" in have and ("day" in have or "date" in have):
        return "servings"
    if {"date", "meal"} <= have and _has_energy(names):
        return "mfp"
    if "exercise" in have:
        return "cronometer_exercises" if "day" in have else "mfp_exercise"
    if "metric" in have and "amount" in have:
        return "cronometer_biometrics"
    if "note" in have and "day" in have:
        return "cronometer_notes"
    if ("date" in have or "day" in have) and _has_energy(names):
        return "daily"
    if "date" in have and ("weight" in have or any(n.startswith("weight") for n in have)):
        return "mfp_measurement"
    return None


def _decode(raw: bytes) -> tuple[str, str]:
    try:
        return raw.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace"), "windows-1252"


# ---------------------------------------------------------------- reading

class _Reader:
    """What one scan has gathered so far, across every file it reads."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.zone = ZoneInfo(cfg.timezone)
        self.servings: list[dict] = []
        self.days: list[dict] = []
        self.meals: list[dict] = []
        self.files: list[dict] = []
        self.skipped: Counter = Counter()
        self.unmapped: Counter = Counter()
        self.examples: dict[str, str] = {}
        self.columns: dict[str, Column] = {}         # header -> column, every file
        self.others: list[str] = []                  # files read that hold no food

    def miss(self, what: str, where: str, n: int = 1) -> None:
        self.unmapped[what] += n
        self.examples.setdefault(what, where)

    def read(self, fname: str, raw: bytes, single: bool) -> str | None:
        """Read one file; returns its kind, or None when it is not an export
        this connector knows (raised instead when it is the only file)."""
        text, encoding = _decode(raw)
        try:
            rows = list(csv.reader(io.StringIO(text, newline="")))
        except csv.Error as e:
            raise DomainError(f"{fname}: not readable as CSV: {e}") from e
        start = next((i for i, r in enumerate(rows) if any(c.strip() for c in r)), None)
        if start is None:
            if single:
                raise DomainError(f"{fname} is empty")
            self.miss("empty file", fname)
            self.others.append(f"{PurePosixPath(fname).name} (empty)")
            return None
        header = rows[start]
        names = [_norm(c) for c in header]
        kind = file_kind([n for n in names if n])
        data = [r for r in rows[start + 1:] if any(c.strip() for c in r)]
        if kind is None:
            seen = ", ".join(c.strip() for c in header if c.strip())[:160]
            msg = (f"{fname}: not a Cronometer or MyFitnessPal export — first row: "
                   f"{seen} (expected servings.csv, dailysummary.csv or a "
                   f"MyFitnessPal Nutrition Summary)")
            if single:
                raise DomainError(msg)
            self.miss(f"file that is not a nutrition export ({PurePosixPath(fname).name})",
                      seen[:80])
            self.others.append(f"{PurePosixPath(fname).name} (not a nutrition export)")
            return None
        if kind in SKIP_KINDS:
            if single:
                raise DomainError(
                    f"{fname} is a {SKIP_KINDS[kind]}, not food — import servings.csv "
                    f"and dailysummary.csv (Cronometer) or the Nutrition Summary "
                    f"(MyFitnessPal)")
            self.skipped[f"{SKIP_KINDS[kind]} row ({PurePosixPath(fname).name}) — "
                         f"not food, not imported"] += len(data)
            self.others.append(f"{PurePosixPath(fname).name} ({SKIP_KINDS[kind]})")
            return kind
        made, reader = {"servings": (self.servings, self._serving),
                        "daily": (self.days, self._day),
                        "mfp": (self.meals, self._meal)}[kind]
        before = len(made)
        cols = self._columns(kind, header)
        index = {n: j for j, n in reversed(list(enumerate(names))) if n}
        for lineno, row in enumerate(rows[start + 1:], start=start + 2):
            if any(c.strip() for c in row):
                reader(fname, lineno, row, index, cols)
        mine = made[before:]
        dates = [m["day"] for m in mine]
        self.files.append({"file": fname, "kind": kind, "format": FOOD_KINDS[kind],
                           "encoding": encoding, "rows": len(mine),
                           "first": min(dates).isoformat() if dates else None,
                           "last": max(dates).isoformat() if dates else None})
        return kind

    def _columns(self, kind: str, header: list[str]) -> list[tuple[int, Column]]:
        """(position, column) for every nutrient column — every column the kind
        does not name as text. A key two columns would share goes to the
        first; the second is kept as-is under its own header."""
        defaults = MFP_UNITS if kind == "mfp" else None
        out, taken = [], set()
        for j, cell in enumerate(header):
            n = _norm(cell)
            if not n or n in _TEXT_COLUMNS[kind]:
                continue
            col = nutrient_column(cell, defaults)
            if col.key in taken:
                col = Column(col.header, col.header, col.source_unit, known=False,
                             source_unit=col.source_unit)
            taken.add(col.key)
            self.columns.setdefault(col.header, col)
            out.append((j, col))
        return out

    def _values(self, fname: str, lineno: int, row: list[str],
                cols: list[tuple[int, Column]]) -> tuple[dict, dict]:
        nutrients, other = {}, {}
        for j, col in cols:
            cell = row[j].strip() if j < len(row) else ""
            try:
                v = parse_number(cell)
            except ValueError:
                other[col.header] = cell
                self.miss("cell that is not a number (kept as text in value_json.other)",
                          f"{fname}:{lineno} {col.header}={cell[:20]}")
                continue
            if v is None:
                continue
            if col.factor != 1.0:
                v = round(v * col.factor, 6)
            nutrients[col.key] = [v, col.unit]
        return nutrients, other

    @staticmethod
    def _cell(row: list[str], index: dict, *names: str) -> str:
        for n in names:
            j = index.get(n)
            if j is not None and j < len(row):
                return row[j].strip()
        return ""

    def _serving(self, fname, lineno, row, index, cols) -> None:
        try:
            day = parse_day(self._cell(row, index, "day", "date"))
            if day is None:
                raise ValueError("no day")
        except ValueError:
            self.miss("serving with no readable day", f"{fname}:{lineno}")
            return
        food = _collapse(self._cell(row, index, "food name"))
        if not food:
            self.miss("serving with no food name", f"{fname}:{lineno}")
            return
        raw_time = self._cell(row, index, "time")
        try:
            when = parse_time(raw_time)
        except ValueError:
            when = None
            self.miss("serving with a time that does not parse (placed at noon)",
                      f"{fname}:{lineno} {raw_time[:20]}")
        nutrients, other = self._values(fname, lineno, row, cols)
        self.servings.append({
            "file": fname, "day": day, "time": when, "raw_time": raw_time,
            "group": _collapse(self._cell(row, index, "group")),
            "food": food, "amount": _collapse(self._cell(row, index, "amount")),
            "category": _collapse(self._cell(row, index, "category")),
            "nutrients": nutrients, "other": other,
            "row": tuple(c.strip() for c in row)})

    def _day(self, fname, lineno, row, index, cols) -> None:
        try:
            day = parse_day(self._cell(row, index, "date", "day"))
            if day is None:
                raise ValueError("no date")
        except ValueError:
            self.miss("daily summary row with no readable date", f"{fname}:{lineno}")
            return
        nutrients, other = self._values(fname, lineno, row, cols)
        if not other and not any(v for v, _ in nutrients.values()):
            self.skipped["daily summary of a day with nothing logged"] += 1
            return
        self.days.append({"file": fname, "day": day, "nutrients": nutrients,
                          "other": other,
                          "completed": _completed(self._cell(row, index, "completed"))})

    def _meal(self, fname, lineno, row, index, cols) -> None:
        try:
            day = parse_day(self._cell(row, index, "date"))
            if day is None:
                raise ValueError("no date")
        except ValueError:
            self.miss("meal row with no readable date", f"{fname}:{lineno}")
            return
        raw_time = self._cell(row, index, "time")
        try:
            when = parse_time(raw_time)
        except ValueError:
            when = None
            self.miss("meal with a time that does not parse (placed at noon)",
                      f"{fname}:{lineno} {raw_time[:20]}")
        nutrients, other = self._values(fname, lineno, row, cols)
        self.meals.append({
            "file": fname, "day": day, "time": when,
            "meal": _collapse(self._cell(row, index, "meal")) or "Meal",
            "note": _collapse(self._cell(row, index, "note")),
            "nutrients": nutrients, "other": other,
            "row": tuple(c.strip() for c in row)})


# ---------------------------------------------------------------- records

def _instant(day: date, when: time | None, zone: ZoneInfo) -> str:
    local = datetime.combine(day, when or time(12), zone)
    return local.astimezone(timezone.utc).strftime(_ISO)


def _kcal(nutrients: dict) -> float | None:
    hit = nutrients.get("energy")
    return hit[0] if hit and hit[1] == "kcal" else None


def _serving_ids(servings: list[dict]) -> None:
    """sha1(day, time, group, food, amount, ordinal) — see the module
    docstring. The ordinal counts identical rows of a day, ordered by their
    full content, so a file sorted the other way round yields the same ids."""
    groups: dict[tuple, list[dict]] = {}
    for s in servings:
        t = s["time"].isoformat() if s["time"] else s["raw_time"]
        key = (s["day"].isoformat(), t, s["group"], s["food"], s["amount"])
        groups.setdefault(key, []).append(s)
    for key, members in groups.items():
        for ordinal, s in enumerate(sorted(members, key=lambda x: x["row"])):
            digest = hashlib.sha1("|".join((*key, str(ordinal))).encode("utf-8"))
            s["external_id"] = f"cronometer:serving:{digest.hexdigest()[:24]}"


def _meal_ids(meals: list[dict]) -> None:
    seen: Counter = Counter()
    for m in meals:
        slug = re.sub(r"[^a-z0-9]+", "_", m["meal"].lower()).strip("_") or "meal"
        key = (m["day"].isoformat(), slug)
        m["external_id"] = f"myfitnesspal:meal:{key[0]}:{slug}:{seen[key]}"
        seen[key] += 1


def _record(stream: str, external_id: str, day: date, started: str, *, title, body,
            nutrients: dict, detail: dict, origin: str) -> dict:
    kcal = _kcal(nutrients)
    value = {k: v for k, v in detail.items()
             if k == "completed" or v not in (None, "", {})}
    value["nutrients"] = nutrients
    return {
        "stream": stream,
        "external_id": external_id,
        "journal_date": day.isoformat(),
        "started_at": started,
        "ended_at": None,
        "utc_offset_sec": None,
        "direction": None,
        "title": title,
        "body": body,
        "value": kcal,
        "unit": "kcal" if kcal is not None else None,
        "value_json": canonical_json(value),
        "media_path": None,
        "origin_app": origin,
        "participants": [],
    }


def _records(rd: _Reader) -> list[dict]:
    _serving_ids(rd.servings)
    _meal_ids(rd.meals)
    out = []
    for s in rd.servings:
        body = " · ".join(p for p in (s["amount"], s["group"]) if p) or None
        out.append(_record(
            "food_log", s["external_id"], s["day"], _instant(s["day"], s["time"], rd.zone),
            title=s["food"], body=body, nutrients=s["nutrients"], origin="cronometer",
            detail={"meal": s["group"], "amount": s["amount"], "category": s["category"],
                    "time": s["time"].strftime("%H:%M") if s["time"] else None,
                    "other": s["other"]}))
    for m in rd.meals:
        body = "MyFitnessPal meal total" + (f" · {m['note']}" if m["note"] else "")
        out.append(_record(
            "food_log", m["external_id"], m["day"], _instant(m["day"], m["time"], rd.zone),
            title=f"{m['meal']} (meal total)", body=body, nutrients=m["nutrients"],
            origin="myfitnesspal",
            detail={"meal": m["meal"], "aggregate": "meal", "note": m["note"],
                    "time": m["time"].strftime("%H:%M") if m["time"] else None,
                    "other": m["other"]}))
    for d in rd.days:
        out.append(_record(
            "nutrition_day", f"cronometer:day:{d['day'].isoformat()}", d["day"],
            _instant(d["day"], None, rd.zone), title=f"Nutrition {d['day'].isoformat()}",
            body=None, nutrients=d["nutrients"], origin="cronometer",
            detail={"completed": d["completed"], "other": d["other"]}))
    return out


def _loads(text: str | None) -> dict:
    return json.loads(text) if text else {}


# ---------------------------------------------------------------- targets

def _junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p.startswith(".") or p == "__MACOSX" for p in parts) or \
        PurePosixPath(rel).name.startswith("~$")


def _export_files(target: Path) -> tuple[list[tuple[str, bytes]], list[str], str]:
    """([(name, bytes)], [ignored names], 'folder' | 'zip' | 'file')."""
    try:
        if target.is_dir():
            found, ignored = [], []
            for p in sorted(target.rglob("*")):
                rel = p.relative_to(target).as_posix()
                if not p.is_file() or _junk(rel):
                    continue
                if p.suffix.lower() == ".csv":
                    found.append((rel, p.read_bytes()))
                else:
                    ignored.append(rel)
            return found, ignored, "folder"
        if zipfile.is_zipfile(target):
            found, ignored = [], []
            with zipfile.ZipFile(target) as zf:
                for info in sorted(zf.infolist(), key=lambda i: i.filename):
                    if info.is_dir() or _junk(info.filename):
                        continue
                    if PurePosixPath(info.filename).suffix.lower() == ".csv":
                        found.append((info.filename, zf.read(info)))
                    else:
                        ignored.append(info.filename)
            return found, ignored, "zip"
        return [(target.name, target.read_bytes())], [], "file"
    except (OSError, zipfile.BadZipFile) as e:
        why = getattr(e, "strerror", None) or e
        raise DomainError(f"cannot read {target}: {why}") from e


def _content_hash(found: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel, raw in found:
        h.update(f"{rel}\0{hashlib.sha256(raw).hexdigest()}\n".encode("utf-8"))
    return h.hexdigest()


def default_target(cfg: Config) -> Path:
    """The `path` in [connectors.nutrition]: where the exports are saved."""
    value = cfg.connector_settings(name).get("path")
    if not value:
        raise DomainError("nutrition needs a path to read — Cronometer's servings.csv "
                          "or dailysummary.csv, MyFitnessPal's Nutrition Summary, a "
                          "folder or .zip holding them (or set path in "
                          "[connectors.nutrition])")
    p = cfg.resolve(value)
    if not p.exists():
        raise DomainError(f"[connectors.nutrition] path does not exist: {p}")
    return p


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    found, ignored, kind = _export_files(target)
    if not found:
        listing = ", ".join(ignored[:8]) or "(empty)"
        raise DomainError(f"{target.name} holds no .csv export (servings.csv, "
                          f"dailysummary.csv, a MyFitnessPal Nutrition Summary) — "
                          f"has: {listing}")
    rd = _Reader(cfg)
    kinds: dict[str, list[str]] = {}
    for fname, raw in found:
        k = rd.read(fname, raw, single=kind == "file")
        if k in FOOD_KINDS:
            kinds.setdefault(k, []).append(fname)
    for k, names in sorted(kinds.items()):
        if len(names) > 1:
            raise DomainError(
                f"{target.name} holds {len(names)} {FOOD_KINDS[k]} files "
                f"({', '.join(names[:4])}) — each export covers its own date range; "
                f"import them one at a time, oldest first")
    if not rd.files:
        raise DomainError(
            f"{target.name} holds no food export — has: "
            + ", ".join(rd.others[:6])
            + " (expected servings.csv, dailysummary.csv or a MyFitnessPal "
              "Nutrition Summary)")

    records = _records(rd)
    # The export's "as of": the latest instant it describes. A later export
    # wins on re-import; an older one overwrites nothing.
    as_of = max((r["started_at"] for r in records), default=None)
    for rec in records:
        rec["last_modified_at"] = as_of
    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))

    cols = list(rd.columns.values())
    skipped_lines = [f"{n} × {why}" for why, n in sorted(rd.skipped.items())]
    if ignored:
        skipped_lines.append(f"{len(ignored)} × file that is not a .csv export "
                             f"(e.g. {ignored[0]})")
    dates = [r["journal_date"] for r in records]
    return base.empty_scan(
        name,
        source_file=target.name,
        source_hash=base.file_hash(target) if kind != "folder" else _content_hash(found),
        records=records,
        handles=[],
        unmapped=[{"what": k, "count": v, "example": rd.examples.get(k)}
                  for k, v in sorted(rd.unmapped.items())],
        skipped=skipped_lines,
        facts={"files": rd.files, "as_of": as_of,
               "first_day": min(dates) if dates else None,
               "last_day": max(dates) if dates else None,
               "known_columns": sum(1 for c in cols if c.known),
               "kept_as_is": sorted(c.header for c in cols if not c.known),
               "converted": sorted(f"{c.header} -> {c.key} in {c.unit}"
                                   for c in cols if c.known and c.factor != 1.0),
               "in_iu": sorted(f"{c.header} -> {c.key}" for c in cols
                               if c.known and c.unit == "IU")},
        target_kind=kind,
        files=rd.files)


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    detail = _loads(rec.get("value_json"))
    kcal = f", {rec['value']:,.0f} kcal" if rec.get("value") is not None else ""
    if rec["stream"] == "nutrition_day":
        flag = {True: ", marked completed",
                False: ", not marked completed"}.get(detail.get("completed"), "")
        return f"{rec['journal_date']}{kcal}{flag}"
    body = f", {rec['body']}" if rec.get("body") else ""
    return (f"{rec['journal_date']} {detail.get('time') or 'no time'} "
            f"{rec['title']}{body}{kcal}")


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    files, facts = scan["files"], scan.get("facts") or {}
    kinds = Counter(f["format"] for f in files)
    lines = [f"nutrition: {scan['source_file']} ("
             + ", ".join(f"{k} ×{n}" for k, n in sorted(kinds.items())) + ")"]
    for f in files[:10]:
        span = f" {f['first']}..{f['last']}" if f["rows"] else ""
        lines.append(f"  file {f['file']}: {f['format']}, {f['rows']} row(s){span}")

    changed = [rec for _, rec, _ in plan["updates"]]
    shown = False
    for stream in streams:
        new = [r for r in plan["new"] if r["stream"] == stream]
        upd = [r for r in changed if r["stream"] == stream]
        recs = new or upd
        if not recs:
            continue
        shown = True
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda r: (r["started_at"], r["external_id"]))
        lines.append(f"  {stream}: {len(new)} new, {len(upd)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
    if not shown:
        lines.append("  nothing new — the export matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} unchanged record(s) re-dated to this export")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} record(s) listed twice")
    if tail:
        lines.append("  " + ", ".join(tail))

    if facts.get("known_columns"):
        lines.append(f"  nutrients: {facts['known_columns']} column(s) read as stable "
                     f"keys (docs/nutrition.md)")
    for c in facts.get("converted") or []:
        lines.append(f"  converted: {c}")
    if facts.get("in_iu"):
        lines.append("  kept in IU (no single conversion to µg/mg): "
                     + ", ".join(facts["in_iu"]))
    kept = facts.get("kept_as_is") or []
    if kept:
        lines.append(f"  kept as-is: {len(kept)} column(s) not in the nutrient table, "
                     f"under their own header — " + ", ".join(kept[:8])
                     + (f" (+{len(kept) - 8} more)" if len(kept) > 8 else ""))
    if any(f["kind"] == "mfp" for f in files):
        lines.append("  MyFitnessPal rows are meal totals, not foods: the nutrient "
                     "report can sum a day from them but name no food sources")
    if plan["conflicts"]:
        lines.append(f"  kept existing: {len(plan['conflicts'])} record(s) differ from "
                     f"what an export as recent or newer already stored — an older "
                     f"export cannot overwrite them")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not understood: {u['count']} × {u['what']}"
                     + (f" (e.g. {u['example']})" if u.get("example") else ""))
    if shown:
        lines.append("  then: lifecrm nutrients — average intake set beside the "
                     "reference intakes")
    return lines

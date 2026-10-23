"""`lifecrm nutrients`: logged intake set beside the reference intakes.

The owner's question — "what am I low on?" — answered the only way this
system answers anything: deterministically, from cited rows, with no model.
Over a window (the last 30 days by default) it averages each nutrient per
day from the `nutrition` connector's daily summaries (`nutrition_day`),
summing that day's `food_log` servings where a day has no summary, and puts
each average beside the adult Dietary Reference Intake for the configured
sex and age:

    low          below 70% of the RDA or AI
    borderline   70% to 100%
    meets        100% or more
    high         average above the upper level (UL; for sodium the CDRR)

plus the days a nutrient went over its upper level, and the foods that
supplied most of each flagged nutrient ("most of your magnesium came from
black beans"). Every line cites the observation rows behind it.

What this is not
----------------
A comparison with reference values, never medical advice or a diagnosis.
An RDA is set to cover 97-98% of healthy people, so intake below it is not
a deficiency; an AI is set where evidence was too thin for an RDA, so intake
below an AI says nothing about adequacy. The report says so at the top, and
it recommends nothing: no supplements, no doses, no foods to eat. The
/advise health skill may turn a flagged nutrient into a question to ask a
clinician — never into a prescription.

Two tiers, as everywhere: these rows are observed (bulk-imported, never
reviewed), so every number carries the ids it came from.

The reference table
-------------------
REFERENCES below embeds the adult values from the National Academies' DRI
reports as summarised by the NIH Office of Dietary Supplements, with the
report each comes from. Selection follows `[nutrition] sex` ("female" |
"male") and `age` (19 and over) in config.toml. Without them each value is
the higher of the female and male values (for ages 19-50 when no age is
set) — the cautious default, and the report says which values it used. An
upper level is compared only where it applies to intake from food: the ULs
for magnesium, niacin, folate, vitamin E and vitamin A are set for
supplements, fortificants or preformed vitamin A only, so food-diary totals
are never held against them.

This module imports nothing from recall or review, so either can call
`build_report` (see docs/nutrition.md, "For ask and the weekly review").
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .config import Config
from .domain import days as days_mod
from .domain.util import DomainError

REPORT_VERSION = 1
DEFAULT_DAYS = 30
MAX_DAYS = 3660
LOW_BELOW = 70.0               # percent of the reference
MEETS_AT = 100.0
FEW_DAYS = 7
TOP_SOURCES = 3
SEXES = ("female", "male")
STREAMS = ("nutrition_day", "food_log")

DISCLAIMER = ("This compares logged intake with the National Academies' Dietary "
              "Reference Intakes for healthy adults; it is not medical advice or a "
              "diagnosis, and it recommends nothing.")

MICRO = "µg"


# ---------------------------------------------------------------- references

SRC_1997 = "IOM 1997, DRIs for Calcium, Phosphorus, Magnesium, Vitamin D, and Fluoride"
SRC_1998 = ("IOM 1998, DRIs for Thiamin, Riboflavin, Niacin, Vitamin B6, Folate, "
            "Vitamin B12, Pantothenic Acid, Biotin, and Choline")
SRC_2000 = "IOM 2000, DRIs for Vitamin C, Vitamin E, Selenium, and Carotenoids"
SRC_2001 = ("IOM 2001, DRIs for Vitamin A, Vitamin K, Arsenic, Boron, Chromium, "
            "Copper, Iodine, Iron, Manganese, Molybdenum, Nickel, Silicon, Vanadium, "
            "and Zinc")
SRC_2005 = ("IOM 2005, DRIs for Energy, Carbohydrate, Fiber, Fat, Fatty Acids, "
            "Cholesterol, Protein, and Amino Acids")
SRC_2011 = "IOM 2011, DRIs for Calcium and Vitamin D"
SRC_2019 = "NASEM 2019, DRIs for Sodium and Potassium"


def _ods(sheet: str) -> str:
    return f"NIH ODS {sheet} fact sheet"


@dataclass(frozen=True)
class Reference:
    """One nutrient's adult reference intake.

    `bands` are (first age, male, female), ascending: a band holds from its
    first age to the next band's. `upper` likewise, (first age, value).
    `upper_food` False means the UL is set for supplements or fortificants
    only and is never compared with a food diary."""
    key: str
    label: str
    unit: str
    kind: str                                        # 'RDA' | 'AI'
    bands: tuple[tuple[int, float, float], ...]
    upper: tuple[tuple[int, float], ...] = ()
    upper_kind: str = "UL"                           # 'UL' | 'CDRR'
    upper_food: bool = True
    upper_note: str | None = None
    source: str = ""


REFERENCES: tuple[Reference, ...] = (
    # ---- macronutrients
    Reference("protein", "Protein", "g", "RDA", ((19, 56, 46),),
              source=f"{SRC_2005}; 0.8 g/kg of a reference body weight"),
    Reference("carbohydrate", "Carbohydrate", "g", "RDA", ((19, 130, 130),),
              source=SRC_2005),
    Reference("fiber", "Fiber", "g", "AI", ((19, 38, 25), (51, 30, 21)),
              source=f"{SRC_2005}; 14 g per 1,000 kcal"),
    # ---- vitamins
    Reference("vitamin_a", "Vitamin A", MICRO, "RDA", ((19, 900, 700),),
              upper=((19, 3000),), upper_food=False,
              upper_note="the UL is for preformed vitamin A (retinol) only; a diary's "
                         "total includes carotenoids, so it is not compared",
              source=f"{SRC_2001}; {_ods('Vitamin A')}; µg RAE"),
    Reference("vitamin_c", "Vitamin C", "mg", "RDA", ((19, 90, 75),),
              upper=((19, 2000),), source=f"{SRC_2000}; {_ods('Vitamin C')}"),
    Reference("vitamin_d", "Vitamin D", MICRO, "RDA", ((19, 15, 15), (71, 20, 20)),
              upper=((19, 100),),
              source=f"{SRC_2011}; {_ods('Vitamin D')}; 15 µg = 600 IU"),
    Reference("vitamin_e", "Vitamin E", "mg", "RDA", ((19, 15, 15),),
              upper=((19, 1000),), upper_food=False,
              upper_note="the UL is for supplemental alpha-tocopherol only",
              source=f"{SRC_2000}; {_ods('Vitamin E')}; mg alpha-tocopherol"),
    Reference("vitamin_k", "Vitamin K", MICRO, "AI", ((19, 120, 90),),
              source=f"{SRC_2001}; {_ods('Vitamin K')}"),
    Reference("thiamin", "Thiamin (B1)", "mg", "RDA", ((19, 1.2, 1.1),),
              source=f"{SRC_1998}; {_ods('Thiamin')}"),
    Reference("riboflavin", "Riboflavin (B2)", "mg", "RDA", ((19, 1.3, 1.1),),
              source=f"{SRC_1998}; {_ods('Riboflavin')}"),
    Reference("niacin", "Niacin (B3)", "mg", "RDA", ((19, 16, 14),),
              upper=((19, 35),), upper_food=False,
              upper_note="the UL is for niacin from supplements and fortified "
                         "foods only",
              source=f"{SRC_1998}; {_ods('Niacin')}; mg niacin equivalents"),
    Reference("pantothenic_acid", "Pantothenic acid (B5)", "mg", "AI", ((19, 5, 5),),
              source=f"{SRC_1998}; {_ods('Pantothenic Acid')}"),
    Reference("vitamin_b6", "Vitamin B6", "mg", "RDA",
              ((19, 1.3, 1.3), (51, 1.7, 1.5)), upper=((19, 100),),
              source=f"{SRC_1998}; {_ods('Vitamin B6')}"),
    Reference("biotin", "Biotin (B7)", MICRO, "AI", ((19, 30, 30),),
              source=f"{SRC_1998}; {_ods('Biotin')}"),
    Reference("folate", "Folate", MICRO, "RDA", ((19, 400, 400),),
              upper=((19, 1000),), upper_food=False,
              upper_note="the UL is for synthetic folic acid only",
              source=f"{SRC_1998}; {_ods('Folate')}; µg DFE"),
    Reference("vitamin_b12", "Vitamin B12", MICRO, "RDA", ((19, 2.4, 2.4),),
              source=f"{SRC_1998}; {_ods('Vitamin B12')}"),
    Reference("choline", "Choline", "mg", "AI", ((19, 550, 425),),
              upper=((19, 3500),), source=f"{SRC_1998}; {_ods('Choline')}"),
    # ---- minerals
    Reference("calcium", "Calcium", "mg", "RDA",
              ((19, 1000, 1000), (51, 1000, 1200), (71, 1200, 1200)),
              upper=((19, 2500), (51, 2000)), source=f"{SRC_2011}; {_ods('Calcium')}"),
    Reference("chromium", "Chromium", MICRO, "AI", ((19, 35, 25), (51, 30, 20)),
              source=f"{SRC_2001}; {_ods('Chromium')}"),
    Reference("copper", "Copper", "mg", "RDA", ((19, 0.9, 0.9),),
              upper=((19, 10),), source=f"{SRC_2001}; {_ods('Copper')}; 900 µg"),
    Reference("fluoride", "Fluoride", "mg", "AI", ((19, 4, 3),),
              upper=((19, 10),), source=f"{SRC_1997}; {_ods('Fluoride')}"),
    Reference("iodine", "Iodine", MICRO, "RDA", ((19, 150, 150),),
              upper=((19, 1100),), source=f"{SRC_2001}; {_ods('Iodine')}"),
    Reference("iron", "Iron", "mg", "RDA", ((19, 8, 18), (51, 8, 8)),
              upper=((19, 45),), source=f"{SRC_2001}; {_ods('Iron')}"),
    Reference("magnesium", "Magnesium", "mg", "RDA",
              ((19, 400, 310), (31, 420, 320)), upper=((19, 350),), upper_food=False,
              upper_note="the UL is for magnesium from supplements and medicines "
                         "only, not food",
              source=f"{SRC_1997}; {_ods('Magnesium')}"),
    Reference("manganese", "Manganese", "mg", "AI", ((19, 2.3, 1.8),),
              upper=((19, 11),), source=f"{SRC_2001}; {_ods('Manganese')}"),
    Reference("molybdenum", "Molybdenum", MICRO, "RDA", ((19, 45, 45),),
              upper=((19, 2000),), source=f"{SRC_2001}; {_ods('Molybdenum')}"),
    Reference("phosphorus", "Phosphorus", "mg", "RDA", ((19, 700, 700),),
              upper=((19, 4000), (71, 3000)), source=f"{SRC_1997}; {_ods('Phosphorus')}"),
    Reference("potassium", "Potassium", "mg", "AI", ((19, 3400, 2600),),
              source=f"{SRC_2019}; {_ods('Potassium')}"),
    Reference("selenium", "Selenium", MICRO, "RDA", ((19, 55, 55),),
              upper=((19, 400),), source=f"{SRC_2000}; {_ods('Selenium')}"),
    Reference("sodium", "Sodium", "mg", "AI", ((19, 1500, 1500),),
              upper=((19, 2300),), upper_kind="CDRR",
              upper_note="2019 replaced sodium's UL with a Chronic Disease Risk "
                         "Reduction intake: intake above 2,300 mg",
              source=SRC_2019),
    Reference("zinc", "Zinc", "mg", "RDA", ((19, 11, 8),),
              upper=((19, 40),), source=f"{SRC_2001}; {_ods('Zinc')}"),
)
BY_KEY = {r.key: r for r in REFERENCES}
# Nutrients a diary reports that have a DRI this report does not compare,
# and why — so they are listed with a reason, not silently dropped.
NOT_COMPARED = {
    "water": "the AI for water counts every drink and the water in food; diaries "
             "rarely log every glass",
    "omega_3": "the AI is for alpha-linolenic acid alone; the export's omega-3 "
               "adds EPA and DHA",
    "omega_6": "the AI is for linoleic acid alone",
}


# ---------------------------------------------------------------- profile

def profile(cfg: Config, sex: str | None = None, age: int | None = None) -> dict:
    """Whose reference values: flags first, then [nutrition] in config.toml.
    One-line DomainError for a value the table cannot use."""
    section = cfg.extra.get("nutrition") if isinstance(cfg.extra, dict) else None
    section = section if isinstance(section, dict) else {}
    where_sex = "--sex" if sex is not None else "[nutrition] sex"
    where_age = "--age" if age is not None else "[nutrition] age"
    raw_sex = sex if sex is not None else section.get("sex")
    raw_age = age if age is not None else section.get("age")
    if raw_sex in (None, ""):
        s = None
    elif isinstance(raw_sex, str) and raw_sex.strip().lower() in SEXES:
        s = raw_sex.strip().lower()
    else:
        raise DomainError(f'{where_sex} must be "female" or "male", got {raw_sex!r}')
    if raw_age in (None, ""):
        a = None
    else:
        if isinstance(raw_age, bool) or not isinstance(raw_age, int):
            try:
                raw_age = int(str(raw_age).strip())
            except ValueError:
                raise DomainError(f"{where_age} must be a whole number of years, "
                                  f"got {raw_age!r}") from None
        if not 19 <= raw_age <= 120:
            raise DomainError(f"{where_age} is {raw_age}: the reference table covers "
                              f"adults, 19 and over")
        a = raw_age
    notes = []
    if s is None:
        notes.append("sex not set: each reference is the higher of the female and "
                     "male values (iron's 18 mg is the female 19-50 value) — set "
                     "[nutrition] sex in config.toml")
    if a is None:
        notes.append("age not set: the values for ages 19-50 are used — set "
                     "[nutrition] age in config.toml")
    return {"sex": s, "age": a, "label": _profile_label(s, a), "notes": notes}


def _band_label(age: int | None) -> str:
    if age is None:
        return "19-50"
    starts = (19, 31, 51, 71)
    first = max(b for b in starts if b <= age)
    nxt = next((b for b in starts if b > first), None)
    return f"{first}-{nxt - 1}" if nxt else f"{first}+"


def _profile_label(sex: str | None, age: int | None) -> str:
    who = sex or "adult (higher of female and male)"
    return f"{who}, ages {_band_label(age)}" + (f" (age {age})" if age else "")


def _at(rows: tuple, age: int):
    hit = None
    for row in rows:
        if row[0] <= age:
            hit = row
    return hit


def reference_for(key: str, prof: dict) -> dict | None:
    """The reference intake and upper level for one nutrient and profile."""
    ref = BY_KEY.get(key)
    if ref is None:
        return None
    ages = (prof["age"],) if prof.get("age") is not None else (19, 31)
    sexes = (prof["sex"],) if prof.get("sex") else SEXES
    values = []
    for a in ages:
        _, male, female = _at(ref.bands, a)
        values += [male if s == "male" else female for s in sexes]
    uppers = [u[1] for u in (_at(ref.upper, a) for a in ages) if u] if ref.upper else []
    return {"key": key, "label": ref.label, "unit": ref.unit, "kind": ref.kind,
            "value": max(values), "upper": min(uppers) if uppers else None,
            "upper_kind": ref.upper_kind if uppers else None,
            "upper_compared": bool(uppers) and ref.upper_food,
            "upper_note": ref.upper_note, "source": ref.source}


# ---------------------------------------------------------------- window

def local_today(cfg: Config) -> date:
    """Today's journal date (timezone and rollover hour honoured)."""
    return date.fromisoformat(days_mod.journal_date_for(
        datetime.now(timezone.utc), cfg.timezone, cfg.day_rollover_hour))


def _day(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise DomainError(f"{flag} '{value}' is not a date (expected YYYY-MM-DD)") \
            from None


def resolve_window(cfg: Config, *, days: int | None = None, start: str | None = None,
                   end: str | None = None, today: date | None = None) -> dict:
    """The days to average over: --days N (ending today), --start [--end],
    or the last DEFAULT_DAYS days."""
    today = today or local_today(cfg)
    if days is not None and (start or end):
        raise DomainError("give --days or --start/--end, not both")
    if start or end:
        if not start:
            raise DomainError("--end needs --start")
        s = _day(start, "--start")
        e = _day(end, "--end") if end else today
        if e < s:
            raise DomainError(f"--end {e} is before --start {s}")
        label = f"{s.isoformat()} to {e.isoformat()}"
    else:
        n = DEFAULT_DAYS if days is None else days
        if not 1 <= n <= MAX_DAYS:
            raise DomainError(f"--days must be 1-{MAX_DAYS}, got {n}")
        s, e = today - timedelta(days=n - 1), today
        label = f"last {n} day{'s' if n != 1 else ''}"
    n_days = (e - s).days + 1
    if n_days > MAX_DAYS:
        raise DomainError(f"{s} to {e} is {n_days} days — at most {MAX_DAYS}")
    return {"start": s.isoformat(), "end": e.isoformat(), "days": n_days,
            "label": label, "today": today.isoformat()}


# ---------------------------------------------------------------- reading

def _nutrients(value_json: str | None) -> tuple[dict, dict]:
    try:
        detail = json.loads(value_json) if value_json else {}
    except ValueError:
        return {}, {}
    if not isinstance(detail, dict):
        return {}, {}
    out = {}
    for key, pair in (detail.get("nutrients") or {}).items():
        if isinstance(pair, list) and len(pair) == 2 \
                and isinstance(pair[0], (int, float)) and not isinstance(pair[0], bool):
            out[key] = (float(pair[0]), pair[1])
    return out, detail


def _rows(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, uuid, stream, journal_date, title, value_json, origin_app "
        "FROM observations WHERE status='active' AND stream IN (?, ?) "
        "AND journal_date BETWEEN ? AND ? ORDER BY journal_date, stream, id",
        (*STREAMS, start, end)).fetchall()


def _food_key(title: str | None) -> str:
    return " ".join((title or "(unnamed food)").split()).casefold()


def ranges(ids: list[int]) -> str:
    """'#12-15, #19' — observation ids, compactly."""
    out, run = [], []
    for i in sorted(set(ids)):
        if run and i == run[-1] + 1:
            run.append(i)
            continue
        if run:
            out.append(f"#{run[0]}" if len(run) == 1 else f"#{run[0]}-{run[-1]}")
        run = [i]
    if run:
        out.append(f"#{run[0]}" if len(run) == 1 else f"#{run[0]}-{run[-1]}")
    return ", ".join(out)


# ---------------------------------------------------------------- report

def band(percent: float) -> str:
    """'low' below 70% of the reference, 'borderline' below 100%, else
    'meets' — on the percentage rounded to a tenth, as printed, so 69.96%
    reads and counts as 70.0%."""
    p = round(percent, 1)            # the figure the report prints
    return "low" if p < LOW_BELOW else "borderline" if p < MEETS_AT else "meets"


def build_report(conn: sqlite3.Connection, cfg: Config, window: dict, *,
                 prof: dict | None = None) -> dict:
    """The nutrient report for `window` (resolve_window's dict). Reads only."""
    prof = prof or profile(cfg)
    today = window.get("today") or local_today(cfg).isoformat()
    per_date: dict[str, dict] = {}
    for r in _rows(conn, window["start"], window["end"]):
        slot = per_date.setdefault(r["journal_date"], {"day": None, "foods": []})
        values, detail = _nutrients(r["value_json"])
        item = {"id": r["id"], "uuid": r["uuid"], "title": r["title"],
                "values": values, "detail": detail, "origin": r["origin_app"]}
        if r["stream"] == "nutrition_day":
            slot["day"] = item                           # the newest row wins
        else:
            slot["foods"].append(item)

    completed_in_use = any(s["day"] and s["day"]["detail"].get("completed") is True
                           for s in per_date.values())
    counted, skipped = [], []
    for d in sorted(per_date):
        slot = per_date[d]
        done = slot["day"]["detail"].get("completed") if slot["day"] else None
        if d == today and done is not True:
            skipped.append({"date": d, "why": "today — still being logged"})
            continue
        if completed_in_use and done is False:
            skipped.append({"date": d, "why": "not marked completed in Cronometer"})
            continue
        if slot["day"] is not None:
            counted.append({"date": d, "basis": "nutrition_day",
                            "values": slot["day"]["values"],
                            "ids": [slot["day"]["id"]], "foods": slot["foods"]})
            continue
        if not slot["foods"]:
            continue
        total: dict[tuple[str, str], float] = {}
        for f in slot["foods"]:
            for key, (v, unit) in f["values"].items():
                total[(key, unit)] = total.get((key, unit), 0.0) + v
        units = {}
        for (key, unit), v in total.items():
            units.setdefault(key, (round(v, 6), unit))
        counted.append({"date": d, "basis": "food_log", "values": units,
                        "ids": [f["id"] for f in slot["foods"]], "foods": slot["foods"]})

    n = len(counted)
    by_key: dict[tuple[str, str], list[tuple[dict, float]]] = {}
    for day in counted:
        for key, (v, unit) in day["values"].items():
            by_key.setdefault((key, unit), []).append((day, v))

    nutrients, no_reference, not_compared = [], [], []
    for (key, unit) in sorted(by_key):
        hits = by_key[(key, unit)]
        avg = sum(v for _, v in hits) / n
        ref = reference_for(key, prof)
        base_item = {"key": key, "label": ref["label"] if ref else key, "unit": unit,
                     "average": _round(avg), "days_with_value": len(hits)}
        if ref is None:
            if key in NOT_COMPARED:
                not_compared.append({**base_item, "why": NOT_COMPARED[key]})
            else:
                no_reference.append(base_item)
            continue
        if unit != ref["unit"]:
            not_compared.append({**base_item, "why": f"stored in {unit}; the "
                                 f"reference is in {ref['unit']}"})
            continue
        pct = avg / ref["value"] * 100
        status = band(pct)
        over = []
        if ref["upper_compared"]:
            over = [{"date": day["date"], "value": _round(v), "ids": day["ids"]}
                    for day, v in hits if v > ref["upper"]]
            if avg > ref["upper"]:
                status = "high"
        nutrients.append({
            **base_item, "percent": round(pct, 1), "status": status,
            "reference": ref,
            "cites": {"nutrition_day": sorted(i for day, _ in hits
                                              if day["basis"] == "nutrition_day"
                                              for i in day["ids"]),
                      "food_log": sorted(i for day, _ in hits
                                         if day["basis"] == "food_log"
                                         for i in day["ids"]),
                      "dates": [day["date"] for day, _ in hits]},
            "days_over": over})

    for item in nutrients:
        if item["status"] in ("low", "borderline", "high"):
            item["sources"] = _sources(counted, item["key"], item["unit"])
        if item["days_over"]:
            over_days = {o["date"] for o in item["days_over"]}
            item["sources_on_days_over"] = _sources(
                [d for d in counted if d["date"] in over_days], item["key"], item["unit"])

    missing = sorted(r.key for r in REFERENCES
                     if not any(k == r.key for k, _ in by_key))
    notes = list(prof["notes"])
    flagged = [x for x in nutrients if x["status"] in ("low", "borderline")]
    if any(x["reference"]["kind"] == "AI" for x in flagged):
        notes.append("an AI (adequate intake) is set where evidence was too thin for "
                     "an RDA: intake below an AI says nothing about adequacy")
    if flagged:
        notes.append("an RDA covers 97-98% of healthy people: intake below it is not "
                     "a deficiency")
    if 0 < n < FEW_DAYS:
        notes.append(f"only {n} day(s) counted — an average over so few days says "
                     f"little")
    if per_date and not completed_in_use and any(s["day"] for s in per_date.values()):
        notes.append("no day in this window is marked completed in Cronometer, so "
                     "every logged day counts — a half-logged day pulls the "
                     "averages down")
    if any(f["detail"].get("aggregate") == "meal" for d in counted for f in d["foods"]):
        notes.append("MyFitnessPal's export holds meal totals, not foods — no food "
                     "sources can be named for those days")
    partial = [x for x in nutrients if x["days_with_value"] < n]
    if partial:
        notes.append("a nutrient missing from a day's export counts as zero that day "
                     "(the report names each one: 'on N of M days')")
    if not counted:
        notes.append("no nutrition data in this window — import a Cronometer export: "
                     "lifecrm observe nutrition <servings.csv | dailysummary.csv> --apply")

    by_status = {s: [x["key"] for x in sorted(
        (x for x in nutrients if x["status"] == s),
        key=lambda x: (x["percent"], x["key"]))] for s in ("low", "borderline", "meets")}
    by_status["high"] = [x["key"] for x in sorted(
        (x for x in nutrients if x["status"] == "high"),
        key=lambda x: (-x["average"] / x["reference"]["upper"], x["key"]))]
    return {
        "version": REPORT_VERSION,
        "disclaimer": DISCLAIMER,
        "window": window,
        "profile": prof,
        "thresholds": {"low_below_percent": LOW_BELOW, "meets_at_percent": MEETS_AT},
        "coverage": {
            "days_in_window": window["days"],
            "days_counted": n,
            "from_daily_summary": sum(1 for d in counted if d["basis"] == "nutrition_day"),
            "from_food_log": sum(1 for d in counted if d["basis"] == "food_log"),
            "days_without_food": window["days"] - n - len(skipped),
            "skipped": skipped,
            "dates": [d["date"] for d in counted]},
        "nutrients": nutrients,
        **by_status,
        "over_on_some_days": sorted(x["key"] for x in nutrients if x["days_over"]),
        "no_reference": no_reference,
        "not_compared": not_compared,
        "not_in_data": missing,
        "notes": notes,
    }


def _sources(days: list[dict], key: str, unit: str) -> list[dict]:
    """The foods that supplied most of `key` on these days, from food_log."""
    per: dict[str, dict] = {}
    total = 0.0
    for day in days:
        for f in day["foods"]:
            if f["detail"].get("aggregate"):
                continue
            hit = f["values"].get(key)
            if not hit or hit[1] != unit or hit[0] <= 0:
                continue
            total += hit[0]
            slot = per.setdefault(_food_key(f["title"]),
                                  {"food": " ".join((f["title"] or "").split())
                                   or "(unnamed food)", "amount": 0.0, "ids": []})
            slot["amount"] += hit[0]
            slot["ids"].append(f["id"])
    if total <= 0:
        return []
    top = sorted(per.values(), key=lambda s: (-s["amount"], s["food"]))[:TOP_SOURCES]
    return [{"food": s["food"], "amount": _round(s["amount"]), "unit": unit,
             "share_percent": round(s["amount"] / total * 100),
             "servings": len(s["ids"]), "food_log": sorted(s["ids"])} for s in top]


def _round(v: float) -> float:
    return round(v, 2) if abs(v) < 100 else round(v, 1)


# ---------------------------------------------------------------- text

def fmt(v: float) -> str:
    if abs(v) >= 100:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def _cites(item: dict) -> str:
    c = item["cites"]
    parts = []
    if c["nutrition_day"]:
        parts.append(f"nutrition_day {ranges(c['nutrition_day'])}")
    if c["food_log"]:
        parts.append(f"food_log sums {ranges(c['food_log'])}")
    return "[" + "; ".join(parts) + "]"


def _source_text(sources: list[dict]) -> str:
    return ", ".join(f"{s['food']} {s['share_percent']}%" for s in sources)


def _line(item: dict, n: int) -> str:
    ref = item["reference"]
    text = (f"  {ref['label']}: {fmt(item['average'])} {item['unit']}/day — "
            f"{item['percent']:.1f}% of the {ref['kind']} {fmt(ref['value'])} "
            f"{item['unit']}")
    if item["days_with_value"] < n:
        text += f" (on {item['days_with_value']} of {n} days)"
    if item["status"] == "high":
        text = (f"  {ref['label']}: {fmt(item['average'])} {item['unit']}/day — above "
                f"the {ref['upper_kind']} {fmt(ref['upper'])} {item['unit']}")
    if item.get("sources"):
        text += f" | most from: {_source_text(item['sources'])}"
    return text + " " + _cites(item)


def render_lines(report: dict) -> list[str]:
    w, cov, prof = report["window"], report["coverage"], report["profile"]
    n = cov["days_counted"]
    lines = [report["disclaimer"],
             f"nutrients: {w['start']} to {w['end']} ({w['label']}) — {n} of "
             f"{w['days']} day(s) counted: {cov['from_daily_summary']} from Cronometer "
             f"daily summaries, {cov['from_food_log']} summed from food_log",
             f"  reference intakes for: {prof['label']}"]
    for s in cov["skipped"]:
        lines.append(f"  skipped {s['date']}: {s['why']}")
    if not n:
        lines += [f"  note: {x}" for x in report["notes"]]
        return lines
    items = {x["key"]: x for x in report["nutrients"]}
    sections = (("low", f"low — below {LOW_BELOW:.0f}% of the reference intake"),
                ("borderline", f"borderline — {LOW_BELOW:.0f}-{MEETS_AT:.0f}%"),
                ("high", "high — average above the upper level"))
    for status, title in sections:
        keys = report[status]
        lines.append(f"{title}:" if keys else f"{title}: none")
        lines += [_line(items[k], n) for k in keys]
    over = [items[k] for k in report["over_on_some_days"]]
    if over:
        lines.append("above the upper level on some days:")
        for item in over:
            ref = item["reference"]
            days = item["days_over"]
            shown = ", ".join(f"{o['date']} {fmt(o['value'])} {item['unit']}"
                              for o in days[:6])
            more = f" (+{len(days) - 6} more)" if len(days) > 6 else ""
            src = item.get("sources_on_days_over") or []
            ids = ranges([i for o in days for i in o["ids"]])
            lines.append(f"  {ref['label']}: above the {ref['upper_kind']} "
                         f"{fmt(ref['upper'])} {item['unit']} on {len(days)} of {n} "
                         f"days: {shown}{more}"
                         + (f" | most from, those days: {_source_text(src)}" if src else "")
                         + f" [{ids}]")
    meets = [items[k] for k in report["meets"]]
    if meets:
        lines.append(f"meets the reference ({len(meets)}): "
                     + ", ".join(f"{x['reference']['label']} {x['percent']:.0f}%"
                                 for x in meets))
    if report["no_reference"]:
        lines.append("no reference intake: "
                     + ", ".join(x["key"] for x in report["no_reference"]))
    for x in report["not_compared"]:
        lines.append(f"not compared: {x['key']} — {x['why']}")
    if report["not_in_data"]:
        lines.append("not in the export: " + ", ".join(report["not_in_data"]))
    lines += [f"  note: {x}" for x in report["notes"]]
    lines.append("sources: references from the National Academies' DRI reports via "
                 "the NIH Office of Dietary Supplements — `--json` names each one")
    return lines

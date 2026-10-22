"""Deterministic retrieval: turn a question into cited evidence.

This is the answer layer's foundation, and it deliberately contains no model.
A question is parsed by plain rules — a time window, metrics, the people and
places it names, the kind of record it asks about — then answered from the
database. Everything comes back with a citable id and its provenance:

  M#  a statistic: sensor import, journal measurements, or a transaction sum
  E#  a journal entry the owner confirmed
  P#  a passage of the owner's own transcribed words
  O#  an OBSERVED record — a text, call, calendar event, AI chat turn,
      transaction, contact card — bulk-imported and never reviewed. It is
      evidence of what a record says, never confirmed fact.

`no_data` names what was asked for but does not exist.

The split matters: retrieval is deterministic, testable, and reproducible;
a local LLM (lifecrm/llm.py) is only ever allowed to PHRASE this evidence,
never to supply facts — and what it phrases is checked before anyone sees
it: every id it cites must exist, and every number or date it states must
appear in the evidence it cited. The same bundle serves the CLI, `--json` for
an external tool caller, the web UI and the voice loop.

Reads only — callers pass a read-only connection.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .domain import days as days_mod

# ---------------------------------------------------------------- metrics

# agg: how several records on one day combine into that day's value.
HEALTH_METRICS: dict[str, dict] = {
    "sleep_session":      {"agg": "sum", "factor": 1 / 60, "unit": "h",
                           "label": "sleep"},
    "steps":              {"agg": "sum", "factor": 1, "unit": "steps",
                           "label": "steps"},
    "weight":             {"agg": "avg", "factor": 1, "unit": "kg",
                           "label": "weight"},
    "heart_rate":         {"agg": "avg", "factor": 1, "unit": "bpm",
                           "label": "heart rate"},
    "resting_heart_rate": {"agg": "avg", "factor": 1, "unit": "bpm",
                           "label": "resting heart rate"},
    "exercise_session":   {"agg": "sum", "factor": 1, "unit": "min",
                           "label": "workouts"},
    "distance":           {"agg": "sum", "factor": 1, "unit": "km",
                           "label": "distance"},
    "total_calories":     {"agg": "sum", "factor": 1, "unit": "kcal",
                           "label": "calories burned"},
    "nutrition":          {"agg": "sum", "factor": 1, "unit": "kcal",
                           "label": "food energy"},
    "body_fat":           {"agg": "avg", "factor": 1, "unit": "%",
                           "label": "body fat"},
    "hydration":          {"agg": "sum", "factor": 1, "unit": "ml",
                           "label": "hydration"},
}

# twin: the sensor metric measuring the same thing. When the sensor has data,
# the journal not also stating it is not a gap worth reporting.
JOURNAL_METRICS: dict[str, dict] = {
    "mood":          {"agg": "avg", "unit": "/10", "label": "mood"},
    "energy":        {"agg": "avg", "unit": "/10", "label": "energy"},
    "sleep_hours":   {"agg": "avg", "unit": "h", "label": "sleep (journal)",
                      "twin": "sleep_session"},
    "sleep_quality": {"agg": "avg", "unit": "/10", "label": "sleep quality"},
    "rating":        {"agg": "avg", "unit": "/10", "label": "rating"},
    # On a workout entry 'weight' is the load lifted (bench, 185 lb), not
    # the owner's body weight; averaging the two would be nonsense.
    "weight":        {"agg": "avg", "unit": "kg", "label": "weight (journal)",
                      "skip_categories": ("exercise",), "twin": "weight"},
    "steps":         {"agg": "sum", "unit": "steps", "label": "steps (journal)",
                      "twin": "steps"},
    "calories":      {"agg": "sum", "unit": "kcal", "label": "calories (journal)"},
    "water_ml":      {"agg": "sum", "unit": "ml", "label": "water"},
    "reps":          {"agg": "sum", "unit": "reps", "label": "reps"},
    "sets":          {"agg": "sum", "unit": "sets", "label": "sets"},
    "duration":      {"agg": "sum", "unit": "min", "label": "duration"},
    "distance":      {"agg": "sum", "unit": "km", "label": "distance (journal)",
                      "twin": "distance"},
    "servings":      {"agg": "sum", "unit": "servings", "label": "servings"},
    "money":         {"agg": "sum", "unit": "", "label": "money (journal)"},
}

MONEY_WORDS = ("spent", "spend", "spending", "cost", "costs", "paid", "pay",
               "money", "bought", "buy", "purchase", "purchased", "purchases")

# (question words) -> (health metrics, journal metrics). Matched on whole
# tokens, so 'resting' never fires 'rest'.
KEYWORDS: list[tuple[tuple[str, ...], list[str], list[str]]] = [
    (("sleep", "slept", "sleeping", "asleep", "bed", "bedtime", "insomnia",
      "rested", "nap"),
     ["sleep_session"], ["sleep_hours", "sleep_quality"]),
    (("weight", "weigh", "weighed", "weighing", "kg", "lbs", "pounds",
      "scale", "heavier", "lighter"),
     ["weight"], ["weight"]),
    (("steps", "step", "walk", "walked", "walking", "walks"),
     ["steps"], ["steps"]),
    (("workout", "workouts", "exercise", "exercised", "exercising", "gym",
      "training", "trained", "train", "lift", "lifted", "lifting", "run",
      "ran", "running", "ride", "rode", "biked", "biking", "cycling",
      "swim", "swam", "cardio"),
     ["exercise_session", "distance"], ["reps", "sets", "duration", "distance"]),
    (("heart", "bpm", "pulse", "resting", "hr"),
     ["heart_rate", "resting_heart_rate"], []),
    (("calories", "calorie", "kcal", "ate", "eat", "eating", "food", "diet",
      "nutrition", "meal", "meals", "macros", "protein", "carbs"),
     ["nutrition", "total_calories"], ["calories", "servings"]),
    (("mood", "feel", "feeling", "felt", "happy", "sad", "anxious", "stressed",
      "stress", "down", "great"),
     [], ["mood"]),
    (("energy", "energetic", "tired", "exhausted", "fatigue", "fatigued",
      "drained", "productive", "productivity"),
     [], ["energy"]),
    (("water", "hydration", "hydrated", "drank", "drink"),
     ["hydration"], ["water_ml"]),
    (("bodyfat",), ["body_fat"], []),
    (MONEY_WORDS, [], ["money"]),
]

# ---------------------------------------------------------------- windows

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
MONTH_ABBR = {m[:3]: i for m, i in MONTHS.items()} | {"sept": 9}
MONTH_NAMES = {i: m.capitalize() for m, i in MONTHS.items()}
WEEKDAYS = {d: i for i, d in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
     "sunday"])}
WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12}
UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
# 'last 100000 years' is a typo, not a window — and would overflow date.
MAX_SPAN_DAYS = 36500

# A bare 'may' or 'march' is usually a verb, and 'jan' or 'jun' a name. These
# count as months only beside something that makes them one: 'in may',
# 'march 3', 'may 2025', 'may last year'.
_AMBIGUOUS_MONTHS = frozenset({"may", "march"}) | frozenset(MONTH_ABBR)
_TIME_LEAD = frozenset({"in", "during", "of", "since", "until", "till",
                        "before", "after", "from", "through", "last", "this",
                        "next", "early", "late", "mid", "by", "on"})

_NUM = r"(\d+|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_ISO_DATE = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?!\d)")
_ISO_MONTH = re.compile(r"(?<![\d-])(\d{4})-(\d{2})(?![\d-])")
_LAST_N = re.compile(rf"\b(?:last|past|previous)\s+{_NUM}\s+(day|week|month|year)s?\b")
_AGO = re.compile(rf"\b{_NUM}\s+(day|week|month|year)s?\s+ago\b")
_YEAR = re.compile(r"\b(?:in|during|of|for|since|from|year)\s+((?:19|20)\d{2})\b(?![-\d])")
_MONTH_WORDS = sorted(set(MONTHS) | set(MONTH_ABBR), key=len, reverse=True)
_MONTH_RE = re.compile(r"\b(" + "|".join(_MONTH_WORDS) + r")\b")
_WEEKDAY_RE = re.compile(r"\b(?:(last|next|this|on)\s+)?(" + "|".join(WEEKDAYS)
                         + r")s?\b")


def ask_setting(cfg: Config, key: str, default: int) -> int:
    """A positive whole number from [ask]; a typo there falls back to the
    default rather than reading as a bad question."""
    try:
        n = int((cfg.ask or {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return n if n >= 1 else default


def _calendar_horizon(cfg: Config) -> int:
    """How far ahead a forward-looking calendar question reaches: as far as
    the calendar import expands repeating events ([connectors.calendar]
    horizon_days, 90 by default)."""
    try:
        n = int(cfg.connector_settings("calendar").get("horizon_days", 90))
    except (TypeError, ValueError, AttributeError):
        return 90
    return n if 1 <= n <= MAX_SPAN_DAYS else 90


def local_today(cfg: Config) -> date:
    """Today in the configured timezone, honoring the rollover hour — before
    05:00 local, 'today' is still yesterday's journal date."""
    return date.fromisoformat(days_mod.journal_date_for(
        datetime.now(timezone.utc), cfg.timezone, cfg.day_rollover_hour))


def _month_span(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return start, end


def _month_window(year: int, month: int) -> tuple[str, str]:
    start, end = _month_span(year, month)
    return start.isoformat(), end.isoformat()


def _number(text: str) -> int:
    return int(text) if text.isdigit() else WORD_NUMBERS[text]


def _months_back(today: date, n: int) -> date:
    """The day n calendar months before today, clamped to that month's end
    (March 31 less one month is February 28)."""
    y, m = divmod(today.year * 12 + today.month - 1 - n, 12)
    return date(y, m + 1, min(today.day, _month_span(y, m + 1)[1].day))


def _rolling_start(n: int, unit: str, today: date) -> date:
    """The first day of a rolling span of n units ending today. Months and
    years count calendar months — 'past 12 months' is 'past year', both
    starting the day after this date a year ago — and days and weeks count
    days."""
    if unit in ("month", "year"):
        return _months_back(today, n * (12 if unit == "year" else 1)) + timedelta(days=1)
    return today - timedelta(days=n * UNIT_DAYS[unit] - 1)


def _period(which: str, unit: str, today: date) -> tuple[date, date, str] | None:
    """this/last/next week|month|year. 'last week' is the 7 days before
    today (rolling); months and years are calendar ones. 'past week|month|
    year' is a rolling span ending today, like 'past 1 year' — 'how much
    did I spend over the past year' means the last twelve months, never
    only the previous calendar year."""
    if which == "past":
        return _rolling_start(1, unit, today), today, f"past {unit}"
    which = "last" if which == "previous" else which
    if unit == "week":
        monday = today - timedelta(days=today.weekday())
        if which == "this":
            return monday, today, "this week"
        if which == "last":
            return today - timedelta(days=7), today - timedelta(days=1), "last week"
        return monday + timedelta(days=7), monday + timedelta(days=13), "next week"
    if unit == "month":
        if which == "this":
            return today.replace(day=1), today, "this month"
        if which == "last":
            prev = today.replace(day=1) - timedelta(days=1)
            return *_month_span(prev.year, prev.month), "last month"
        nxt = _month_span(today.year, today.month)[1] + timedelta(days=1)
        return *_month_span(nxt.year, nxt.month), "next month"
    if which == "this":
        return today.replace(month=1, day=1), today, "this year"
    year = today.year - 1 if which == "last" else today.year + 1
    return date(year, 1, 1), date(year, 12, 31), f"{which} year"


def _period_end(unit: str, today: date) -> date:
    """The last day of this week|month|year — where a forward-looking
    question ('what's on my calendar this week') stops."""
    if unit == "week":
        return today + timedelta(days=6 - today.weekday())
    if unit == "month":
        return _month_span(today.year, today.month)[1]
    return date(today.year, 12, 31)


# A numeric date is US order, month first: 10/20 is October 20, 10/20/2025
# and 10/20/25 carry their year. Without a year it is the most recent one,
# like 'October 20' — and it needs a word that makes it a time ('on 10/20',
# 'since 9/1'), because '1/2 cup' is not January 2.
_NUM_DATE = re.compile(
    r"(?<![\w/.:-])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\w/])"
    r"(?!\s*(?:cups?|tbsps?|tsps?|tablespoons?|teaspoons?|oz|ounces?|lbs?|pounds?|g|kg|"
    r"grams?|miles?|mi|km|hours?|hrs?|h|mins?|minutes?|liters?|litres?|l|ml|servings?|"
    r"portions?|slices?|pieces?|of)\b)")
_NUM_DATE_LEAD = frozenset({"on", "since", "from", "until", "till", "before", "after",
                            "through", "thru", "by", "between", "to", "and", "the", "for",
                            "of", "was", "is", "about", "around"})
_ORDINAL = r"(\d{1,2})(?:st|nd|rd|th)"
# 'the 20th' is a day of the month — not 'the 20th century' or 'the 3rd floor'.
_NOT_A_DAY = (r"\s+(?:centur(?:y|ies)|floor|time|times|place|anniversary|birthday|"
              r"grade|row|street|avenue|percentile|day\s+of|week|month|year|lap|rep|set)\b")


def _num_date(m: re.Match, today: date, ignored: list[str]) -> date | None:
    mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
    year = (2000 + int(y) if len(y) == 2 else int(y)) if y else None
    try:
        out = date(year or today.year, mo, d)
        if year is None and out > today:
            out = date(today.year - 1, mo, d)
    except ValueError:
        ignored.append(f"{m.group(0)} is not a real date")
        return None
    return out


def _day_of_month(n: int, today: date) -> date | None:
    """'the 20th': the most recent day numbered n, this month or before
    (the 31st in October 2026 is August 31 when asked on the 22nd)."""
    if not 1 <= n <= 31:
        return None
    y, mo = today.year, today.month
    for _ in range(13):
        if n <= _month_span(y, mo)[1].day:
            d = date(y, mo, n)
            if d <= today:
                return d
        y, mo = (y - 1, 12) if mo == 1 else (y, mo - 1)
    return None


def _upcoming_weekend(today: date) -> tuple[date, date]:
    """This week's Saturday and Sunday — still this weekend on the Saturday
    and Sunday themselves."""
    saturday = today - timedelta(days=today.weekday()) + timedelta(days=5)
    return saturday, saturday + timedelta(days=1)


# Which weekend a bare 'the weekend' is: a journal is asked about the past
# ('what did I do over the weekend'), a calendar about what is coming.
_PAST_TENSE = re.compile(r"\b(?:did|was|were|had|went|happened|spent|got|saw|met|ate|"
                         r"ran|been|wrote|logged)\b")
_FUTURE_TENSE = re.compile(r"\b(?:will|going to|gonna|am i|are we|planned|planning|plans?|"
                           r"upcoming|coming|scheduled|schedule|calendar|agenda|need to)\b"
                           r"|\bwhat(?:'s| is) on\b")

# 'the first week of September', 'the end of last month': part of a month.
_PART_WEEKS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
               "fourth": 4, "4th": 4, "last": 0, "final": 0}
_PART_THIRDS = {"start": (1, 10), "beginning": (1, 10), "middle": (11, 20), "end": (21, 31)}


def _month_part(m: re.Match, q: str, today: date, year_hint: int | None,
                names: frozenset[str]) -> tuple[date, date, str, int] | None:
    """A _PART_RE match as (start, end, label, end of span)."""
    week, third = m.group("week"), m.group("third")
    which, token = m.group("which") or "this", m.group("month")
    used = m.end()
    if token:
        if token in names:
            return None
        month = MONTHS.get(token) or MONTH_ABBR[token]
        ym = re.match(r"\.?,?\s+(\d{4})\b", q[m.end():])
        year = int(ym.group(1)) if ym else year_hint
        if year is None:
            year = today.year if month <= today.month else today.year - 1
        used += ym.end() if ym else 0
    else:
        anchor = today if which == "this" else (
            today.replace(day=1) - timedelta(days=1) if which in ("last", "previous")
            else _month_span(today.year, today.month)[1] + timedelta(days=1))
        year, month = anchor.year, anchor.month
    first, last = _month_span(year, month)
    if week:
        n = _PART_WEEKS[week]
        start = last - timedelta(days=6) if n == 0 else first + timedelta(days=7 * (n - 1))
        end = last if n == 0 else start + timedelta(days=6)
        part = f"{'last' if n == 0 else week} week"
    else:
        lo, hi = _PART_THIRDS[third]
        start, end = first.replace(day=lo), first.replace(day=min(hi, last.day))
        part = third
    return start, end, f"{part} of {MONTH_NAMES[month]} {year}", used


_PART_RE = re.compile(
    r"\b(?:the\s+)?(?:(?P<week>" + "|".join(_PART_WEEKS) + r")\s+week|"
    r"(?P<third>start|beginning|middle|end))\s+of\s+(?:the\s+)?"
    r"(?:(?:(?P<which>this|last|next|previous)\s+)?month|(?P<month>"
    + "|".join(_MONTH_WORDS) + r"))\b")


def _month_point(token: str, after: str, today: date,
                 year_hint: int | None, ignored: list[str]
                 ) -> tuple[date, date, str, int] | None:
    """A month name, optionally with a day and/or a year after it. The
    fourth value is how much of `after` the day and year used."""
    month = MONTHS.get(token) or MONTH_ABBR[token]
    dm = re.match(r"\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4})\b)?", after)
    ym = re.match(r"\.?,?\s+(\d{4})\b", after)
    year = (int(dm.group(2)) if dm and dm.group(2)
            else int(ym.group(1)) if ym else year_hint)
    used = dm.end() if dm else ym.end() if ym else 0
    if dm:
        n = int(dm.group(1))
        y = year or today.year
        try:
            d = date(y, month, n)
            if year is None and d > today:
                d = date(y - 1, month, n)
        except ValueError:
            ignored.append(f"{MONTH_NAMES[month]} {n} is not a real date")
            return None
        return d, d, f"{MONTH_NAMES[month]} {d.day}, {d.year}", used
    if year is None:
        # In July, 'December' means last December — not one five months away.
        year = today.year if month <= today.month else today.year - 1
    start, end = _month_span(year, month)
    return start, end, f"{MONTH_NAMES[month]} {year}", used


def _point(text: str, today: date) -> tuple[date, date, str, int] | None:
    """One time expression at the very start of `text` — the X in 'since X'
    or 'between X and Y'. Anchored, so 'from Jake in July' is not 'since
    July'. The fourth value is how many characters of `text` it used."""
    stripped = text.lstrip()
    lead = re.match(r"the\s+", stripped)
    skip = len(text) - len(stripped) + (lead.end() if lead else 0)
    t = text[skip:]
    m = _ISO_DATE.match(t)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        return d, d, d.isoformat(), skip + m.end()
    m = _NUM_DATE.match(t)
    if m:
        d = _num_date(m, today, [])
        return (d, d, d.isoformat(), skip + m.end()) if d else None
    m = re.match(r"(today|yesterday|tomorrow|now)\b", t)
    if m:
        d = today + timedelta(days={"today": 0, "yesterday": -1, "now": 0,
                                    "tomorrow": 1}[m.group(1)])
        return d, d, m.group(1), skip + m.end()
    m = re.match(r"(last|this|next|past|previous)\s+(week|month|year)\b", t)
    if m:
        return *_period(m.group(1), m.group(2), today), skip + m.end()
    m = _MONTH_RE.match(t)
    if m:
        p = _month_point(m.group(1), t[m.end():], today, None, [])
        return (*p[:3], skip + m.end() + p[3]) if p else None
    m = re.match(r"((?:19|20)\d{2})\b(?![-\d])", t)
    if m:
        y = int(m.group(1))
        return date(y, 1, 1), date(y, 12, 31), str(y), skip + m.end()
    return None


def parse_window(question: str, cfg: Config, today: date | None = None,
                 default_days: int = 30,
                 names: frozenset[str] = frozenset()) -> dict:
    """Resolve the question's time expression.

    Rolling definitions, stated plainly because the answer always shows the
    resolved window: 'last week' is the 7 days before today, 'this week'
    starts Monday, 'last year' is the previous calendar year, 'past N
    months' counts calendar months (so 'past 12 months' is 'past year'). A
    numeric date is month first, US order: '10/20' is October 20. 'The
    20th' is the most recent 20th; a bare 'the weekend' is the one you are
    in, else the last one — or the coming one when the question looks
    forward ('what's on my calendar for the weekend'). 'September and
    October' is one window across both, and 'the rest of the week' runs
    from today to Sunday. `explicit` is False when nothing in the question
    named a time — callers widen text search in that case rather than
    silently hiding older matches.

    `names` are words the question uses as names (resolved entities): a
    person called June is never read as the month. Something that looks like
    a date but is not one ('2026-13', 'Feb 30') is ignored, never a crash,
    and listed under `ignored` so the answer can say so.

    An explicit window lists the character `spans` of the question it was
    read from. Those words are the window, not the topic: 'how much did I
    spend in September' searches every transaction in September, never ones
    whose text says "September" (`without_window_words`). A 'this week|
    month|year' window also carries `through`, the period's last day, for
    questions that look forward (`build_evidence`).
    """
    q = question.lower().replace("’", "'")
    today = today or local_today(cfg)
    ignored: list[str] = []

    def win(start: date | str, end: date | str, label: str, *spans,
            through: date | None = None) -> dict:
        w = {"start": start if isinstance(start, str) else start.isoformat(),
             "end": end if isinstance(end, str) else end.isoformat(),
             "label": label, "explicit": True,
             "spans": [list(_lead_in(q, s)) for s in spans]}
        if through is not None and through.isoformat() > w["end"]:
            w["through"] = through.isoformat()
        if ignored:
            w["ignored"] = list(ignored)
        return w

    def since_start(pos: int) -> int | None:
        found = re.search(r"\b(?:since|after|from)\s+$", q[:pos])
        return found.start() if found else None

    # ---- ISO dates and months, validated
    dates: list[tuple[date, tuple[int, int]]] = []
    for m in _ISO_DATE.finditer(q):
        try:
            dates.append((date(int(m.group(1)), int(m.group(2)),
                               int(m.group(3))), m.span()))
        except ValueError:
            ignored.append(f"{m.group(0)} is not a real date")
    for m in _NUM_DATE.finditer(q):
        before = q[:m.start()].split()
        if m.group(3) or (before and before[-1] in _NUM_DATE_LEAD):
            d = _num_date(m, today, ignored)
            if d:
                dates.append((d, m.span()))
    dates.sort(key=lambda x: x[1])
    if len(dates) >= 2:
        lo, hi = sorted(d for d, _ in dates[:2])
        return win(lo, hi, f"{lo} to {hi}", *(s for _, s in dates[:2]))
    if dates:
        d, (pos, end) = dates[0]
        since = since_start(pos)
        if since is not None:
            return win(d, max(d, today), f"since {d}", (since, end))
        return win(d, d, d.isoformat(), (pos, end))
    for m in _ISO_MONTH.finditer(q):
        try:
            start, end = _month_span(int(m.group(1)), int(m.group(2)))
        except ValueError:
            ignored.append(f"{m.group(0)} is not a real month")
            continue
        since = since_start(m.start())
        if since is not None:
            return win(start, max(start, today), f"since {m.group(0)}",
                       (since, m.end()))
        return win(start, end, m.group(0), m.span())

    # ---- ranges in words: between X and Y, since X
    m = re.search(r"\bbetween\s+(.+?)\s+and\s+(.+)$", q)
    if m:
        a, b = _point(m.group(1), today), _point(m.group(2), today)
        if a and b:
            return win(min(a[0], b[0]), max(a[1], b[1]), f"{a[2]} to {b[2]}",
                       (m.start(), m.start(1) + a[3]),
                       (m.end(1), m.start(2) + b[3]))
    # 'September and October', 'from September to October', 'Oct 1 - 15':
    # one window across both, never the first month plus a word to find.
    for pos in sorted({m.start() for m in _MONTH_RE.finditer(q) if m.group(1) not in names}):
        a = _point(q[pos:], today)
        if not a:
            continue
        rest = q[pos + a[3]:]
        joint = re.match(r"\s*[-–—]\s*|\s+(?:and|to|through|thru|until|till)\s+(?:the\s+)?",
                         rest)
        if not joint:
            continue
        tail = rest[joint.end():]
        b = _point(tail, today)
        day = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\b(?![/:])", tail)
        if not b and day and a[0] == a[1]:
            try:
                d = a[0].replace(day=int(day.group(1)))
            except ValueError:
                d = None
            if d:
                b = (d, d, d.isoformat(), day.end())
        if not b:
            continue
        lead = re.search(r"\b(?:from|between)\s+$", q[:pos])
        return win(min(a[0], b[0]), max(a[1], b[1]), f"{a[2]} to {b[2]}",
                   (lead.start() if lead else pos, pos + a[3] + joint.end() + b[3]))
    for m in re.finditer(r"\b(?:since|after|from)\s+(.+)$", q):
        p = _point(m.group(1), today)
        if p:
            return win(p[0], max(p[0], today), f"since {p[2]}",
                       (m.start(), m.start(1) + p[3]))

    # ---- days
    m = re.search(r"\b(?:the\s+)?day\s+(before\s+yesterday|after\s+tomorrow)\b", q)
    if m:
        d = today + timedelta(days=-2 if m.group(1).startswith("before") else 2)
        return win(d, d, f"the day {' '.join(m.group(1).split())} ({d})", m.span())
    # 'the rest of the week': today to the period's end.
    m = re.search(r"\b(?:the\s+)?rest\s+of\s+(?:the\s+|this\s+)?(day|today|week|month|year)\b",
                  q)
    if m:
        unit = m.group(1)
        if unit in ("day", "today"):
            return win(today, today, "today", m.span())
        return win(today, _period_end(unit, today), f"rest of this {unit}", m.span())
    m = re.search(r"\b(?:today|tonight|this (?:morning|afternoon|evening))\b", q)
    if m:
        return win(today, today, "today", m.span())
    m = re.search(r"\byesterday\b", q)
    if m:
        y = today - timedelta(days=1)
        return win(y, y, "yesterday", m.span())
    m = re.search(r"\btomorrow\b", q)
    if m:
        t = today + timedelta(days=1)
        return win(t, t, "tomorrow", m.span())
    m = re.search(r"\blast night\b", q)
    if m:
        # Sleep is filed under the day you woke up, so last night == today.
        return win(today, today, "last night", m.span())

    # ---- N units ago / last N units
    m = _AGO.search(q)
    if m:
        n, unit = _number(m.group(1)), m.group(2)
        label = f"{n} {unit}{'s' if n != 1 else ''} ago"
        if n >= 1 and n * UNIT_DAYS[unit] <= MAX_SPAN_DAYS:
            if unit == "day":
                d = today - timedelta(days=n)
                return win(d, d, label, m.span())
            if unit == "week":
                d = today - timedelta(days=7 * n)
                monday = d - timedelta(days=d.weekday())
                return win(monday, monday + timedelta(days=6), label, m.span())
            if unit == "month":
                y, mo = divmod(today.year * 12 + today.month - 1 - n, 12)
                return win(*_month_span(y, mo + 1), label, m.span())
            # 'four years ago' is fuzzy: the year centred on that date.
            try:
                d = today.replace(year=today.year - n)
            except ValueError:                  # Feb 29
                d = today.replace(year=today.year - n, day=28)
            return win(d - timedelta(days=182), d + timedelta(days=182),
                       f"around {label}", m.span())
        ignored.append(f"'{m.group(0)}' is not a usable span")
    m = _LAST_N.search(q)
    if m:
        n, unit = _number(m.group(1)), m.group(2)
        if n < 1:
            ignored.append(f"'{m.group(0)}' is an empty span")
        else:
            label = f"last {n} {unit}{'s' if n != 1 else ''}"
            if n * UNIT_DAYS[unit] > MAX_SPAN_DAYS:
                ignored.append(f"'{m.group(0)}' capped at {MAX_SPAN_DAYS} days")
                return win(today - timedelta(days=MAX_SPAN_DAYS - 1), today, label,
                           m.span())
            # Months and years by the calendar: 'past 12 months' is 'past year'.
            return win(_rolling_start(n, unit, today), today, label, m.span())
    m = re.search(r"\b(?:last|past|previous)\s+fortnight\b", q)
    if m:
        return win(today - timedelta(days=13), today, "past fortnight", m.span())
    m = re.search(r"\b(?:a|one)\s+fortnight\s+ago\b", q)
    if m:
        d = today - timedelta(days=14)
        monday = d - timedelta(days=d.weekday())
        return win(monday, monday + timedelta(days=6), "a fortnight ago", m.span())

    # ---- part of a month: 'the first week of October', 'the end of September'
    year_hint = (today.year - 1 if re.search(r"\b(?:last|previous) year\b", q)
                 else today.year if re.search(r"\bthis year\b", q) else None)
    for m in _PART_RE.finditer(q):
        p = _month_part(m, q, today, year_hint, names)
        if p:
            return win(*p[:3], (m.start(), p[3]))

    # ---- weekends and weeks
    monday = today - timedelta(days=today.weekday())
    last_weekend = (monday - timedelta(days=2), monday - timedelta(days=1), "last weekend")
    m = re.search(r"\b(?:the\s+)?weekend\s+before\s+last\b", q)
    if m:
        return win(monday - timedelta(days=9), monday - timedelta(days=8),
                   "the weekend before last", m.span())
    m = re.search(r"\b(?:last|(?:this\s+)?past|previous)\s+weekend\b", q)
    if m:
        return win(*last_weekend, m.span())
    m = re.search(r"\bthis weekend\b", q)
    if m:
        return win(monday + timedelta(days=5), monday + timedelta(days=6),
                   "this weekend", m.span())
    m = re.search(r"\bnext weekend\b", q)
    if m:
        return win(monday + timedelta(days=12), monday + timedelta(days=13),
                   "next weekend", m.span())
    m = re.search(r"\b(?:this\s+|the\s+)?coming\s+weekend\b", q)
    if m:
        sat = _upcoming_weekend(today)[0] + timedelta(days=7 if today.weekday() == 6 else 0)
        return win(sat, sat + timedelta(days=1), "the coming weekend", m.span())
    m = re.search(r"\b(?:this\s+|the\s+)?coming\s+week\b", q)
    if m:
        return win(today, today + timedelta(days=6), "the coming week", m.span())
    # A bare 'the weekend': the one you are in, else the one a past-tense
    # question looks back on ('what did I do over the weekend') or a
    # calendar question looks forward to.
    m = re.search(r"\b(?:(?:over|on|at|during|for)\s+)?(?:the\s+)?weekend\b(?!s)", q)
    if m:
        if today.weekday() >= 5:
            return win(*_upcoming_weekend(today), "this weekend", m.span())
        if _FUTURE_TENSE.search(q) and not _PAST_TENSE.search(q):
            return win(*_upcoming_weekend(today), "this weekend", m.span())
        return win(*last_weekend, m.span())
    m = re.search(r"\b(this|last|past|previous|next) week\b", q)
    if m:
        return win(*_period(m.group(1), "week", today), m.span(),
                   through=_period_end("week", today) if m.group(1) == "this" else None)

    # ---- month names (before 'last year', so 'July last year' is July)
    for m in _MONTH_RE.finditer(q):
        token = m.group(1)
        if token in names:
            continue
        after = q[m.end():]
        # 'the 20th of October', '20 October': the day comes first.
        day_first = None
        if not re.match(r"\.?\s+\d{1,2}(?:st|nd|rd|th)?\b", after):
            day_first = re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?$",
                                  q[:m.start()])
        if token in _AMBIGUOUS_MONTHS and not day_first:
            before = q[:m.start()].split()
            lead = before[-1] if before else ""
            if not (lead in _TIME_LEAD
                    or re.match(r"\.?,?\s+\d{1,4}\b", after)
                    or re.match(r"\s+(?:last|this|next) year\b", after)):
                continue
        if day_first:
            ym = re.match(r"\.?,?\s+(\d{4})\b", after)
            p = _month_point(token, f" {day_first.group(1)}"
                             + (f" {ym.group(1)}" if ym else ""), today, year_hint, ignored)
            if p:
                return win(*p[:3], (day_first.start(), m.end() + (ym.end() if ym else 0)))
            continue
        p = _month_point(token, after, today, year_hint, ignored)
        if p:
            return win(*p[:3], (m.start(), m.end() + p[3]))

    m = re.search(r"\b(this|last|past|previous|next) month\b", q)
    if m:
        return win(*_period(m.group(1), "month", today), m.span(),
                   through=_period_end("month", today) if m.group(1) == "this" else None)
    m = re.search(r"\b(this|last|past|previous|next) year\b", q)
    if m:
        return win(*_period(m.group(1), "year", today), m.span(),
                   through=_period_end("year", today) if m.group(1) == "this" else None)
    m = _YEAR.search(q)
    if m:
        y = int(m.group(1))
        return win(date(y, 1, 1), min(date(y, 12, 31), max(today, date(y, 1, 1))),
                   str(y), m.span())

    m = re.search(rf"\bthe\s+{_ORDINAL}\b(?!{_NOT_A_DAY})", q)
    if m:
        d = _day_of_month(int(m.group(1)), today)
        if d:
            return win(d, d, f"the {m.group(0).split()[-1]} ({d})", m.span())
        ignored.append(f"'{m.group(0)}' is not a day of the month")

    # A place called Sunday Market is a name, not a day.
    m = next((m for m in _WEEKDAY_RE.finditer(q) if m.group(2) not in names), None)
    if m:
        which, wd = m.group(1), WEEKDAYS[m.group(2)]
        if which == "next":
            d = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
        else:
            delta = (today.weekday() - wd) % 7
            d = today - timedelta(days=delta or (7 if which == "last" else 0))
        label = f"{which + ' ' if which in ('last', 'next') else ''}{m.group(2).capitalize()}"
        return win(d, d, f"{label} ({d})", m.span())

    start = today - timedelta(days=default_days - 1)
    w = {"start": start.isoformat(), "end": today.isoformat(),
         "label": f"last {default_days} days", "explicit": False}
    if ignored:
        w["ignored"] = ignored
    return w


# A word that only qualifies the time after it ('during September', 'early
# October', 'until Friday') goes with it: once that time is the window, the
# word is no topic either.
_LEAD_IN = re.compile(r"\b(?:during|until|till|through|throughout|before|after|"
                      r"around|early|late|mid|since)[\s-]+$")


def _lead_in(q: str, span: tuple[int, int]) -> tuple[int, int]:
    m = _LEAD_IN.search(q[:span[0]])
    return (m.start(), span[1]) if m else (span[0], span[1])


def without_window_words(question: str, window: dict) -> str:
    """The question with the words its window was read from blanked out.

    They said WHEN, so they are never also WHAT to search for: left in,
    'September', 'weekend', 'next' or the 'two' of 'past two weeks' became
    required search terms, and a question whose window was right still
    found nothing. Only the words the window actually used go — a person or
    a topic called June stays a word when June was not read as the month.
    """
    spans = window.get("spans") or ()
    if not spans:
        return question
    # The spans index the lowercased question; when lowercasing changes a
    # length (rare non-ASCII letters), blank that form instead.
    chars = list(question if len(question.lower()) == len(question)
                 else question.lower())
    for start, end in spans:
        for i in range(max(start, 0), min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


# ---------------------------------------------------------------- words

STOPWORDS = {
    "a", "about", "all", "also", "am", "an", "and", "any", "anything", "are",
    "as", "at", "average", "back", "be", "been", "being", "between", "but",
    "by", "can", "could", "day", "days", "did", "do", "does", "doing", "done",
    "each", "ever", "for", "from", "get", "go", "had", "happen", "happened",
    "happening", "happens", "has", "have", "he",
    "her", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "just", "last", "like", "long", "many", "me", "month", "months", "more",
    "most", "much", "my", "night", "of", "often", "on", "or", "our", "over",
    "past", "really", "recap", "recent", "recently", "she", "should", "show",
    "so", "some", "something", "still", "summarise", "summarize", "summary",
    "than", "that", "the", "their", "them",
    "then", "there", "these", "they", "this", "those", "time", "times", "to",
    "today", "up", "very", "was", "we", "week", "weeks", "were", "what",
    "when", "where", "which", "who", "why", "will", "with", "would", "year",
    "years", "yesterday", "you", "your",
}

# Verbs that say HOW something was communicated, not WHAT about. "what did I
# text Jake about the lake" is about the lake; ranking on "text" would favour
# every message that happens to contain the word.
INTENT_WORDS = frozenset({
    "ask", "asked", "asking", "say", "said", "says", "tell", "told", "tells",
    "talk", "talked", "talking", "speak", "spoke", "spoken", "send", "sent",
    "hear", "heard", "message", "messaged", "messages", "text", "texted",
    "texts", "texting", "chat", "chatted", "chats", "call", "called", "calls",
    "calling", "write", "wrote", "written", "reply", "replied", "mention",
    "mentioned", "mentions", "see", "saw", "seen", "meet", "met", "went",
    "goes", "going", "gone", "give", "gave", "given", "got", "know", "knew",
    "remember", "discuss", "discussed", "email", "emailed", "e-mail", "e-mailed",
})

# Words that say WHEN, never WHAT: left over from a time phrase the window
# did not read ('the weekend', 'next on my calendar', 'yesterday morning',
# the second month of 'September and October'), they were required search
# terms and a question whose window was right still found nothing. 'May'
# and 'March' are verbs as often as months: they go only when the window
# read them. A word a resolved entity is named by stays (`parse_intent`).
TIME_WORDS = (frozenset(MONTHS) - {"may", "march"}) | frozenset(WEEKDAYS) \
    | frozenset(d + "s" for d in WEEKDAYS) | frozenset({
        "weekend", "weekends", "fortnight", "fortnights", "next", "upcoming", "coming",
        "earlier", "later", "lately", "morning", "mornings", "afternoon", "afternoons",
        "evening", "evenings", "night", "nights", "tonight", "tomorrow", "rest",
        "start", "end", "beginning", "middle", "ago", "previous", "ahead", "soon"})
# Words that only mean 'what my journal holds': 'what did I log yesterday'
# is a recap of yesterday, not a search for the word "log".
RECAP_WORDS = frozenset({"log", "logs", "logged", "logging", "journal", "journals",
                         "journaled", "journalled", "journaling", "journalling",
                         "entry", "entries", "record", "records", "recorded",
                         "diary", "wrote", "write", "written"})

# A hyphenated word is one word: 'H-E-B' split into h, e and b left nothing
# long enough to search for. Quoted, FTS5 reads "h-e-b" as the phrase h e b,
# which is exactly how it indexed "H-E-B #742 AUSTIN TX". An ampersand joins
# too: 'AT&T' is the phrase "at t", never the stopword 'at' and a letter.
_WORD_RE = re.compile(r"\w+(?:['’&-]\w+)*")
_HONORIFICS = frozenset({"dr", "mr", "mrs", "ms", "mx", "prof", "sir"})


def _fold(text: str) -> str:
    """Case- and accent-insensitive, the same rule entities.match uses."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _key(text: str) -> str:
    """A name as comparable words: 'Dr. Ellis' -> 'dr ellis', "O'Brien" ->
    'obrien'."""
    return " ".join(re.findall(r"\w+", _fold(text).replace("'", "")
                               .replace("’", "")))


def _words(question: str) -> list[dict]:
    """The question's words: as typed, as a comparable key, as an FTS term.
    A possessive is not part of the name — "Jake's" is Jake."""
    out = []
    for m in _WORD_RE.finditer(question):
        raw = m.group(0).replace("’", "'")
        base = re.sub(r"'s$", "", raw, flags=re.IGNORECASE)
        out.append({"raw": raw, "key": _key(base), "term": base.lower()})
    return out


# Two-letter words that are never a merchant.
_SHORT_NOISE = frozenset({"us", "no", "ok", "oh", "um", "uh", "ah", "eh", "hi", "ya"})


def _terms(words: list[dict], drop: frozenset[str] = frozenset(),
           keep_short: bool = False) -> list[str]:
    """Search terms: no stopwords, numbers or very short words. With
    `keep_short`, a two-letter word stays — in a money question it is a
    merchant ('BP', 'TJ'), and dropping it would answer with every
    transaction instead of that merchant's."""
    seen, keep = set(), []
    for w in words:
        t = w["term"].strip("'")
        short = len(t) <= 2 and not (keep_short and len(t) == 2 and t.isalpha()
                                     and t not in _SHORT_NOISE)
        if (short or t in STOPWORDS or t.replace("-", "").isdigit() or t in drop
                or w["key"] in drop or re.fullmatch(r"\d+(?:st|nd|rd|th)", t)):
            continue
        if t not in seen:
            seen.add(t)
            keep.append(t)
    return keep[:12]


def _fts_expr(terms: list[str], op: str = " OR ", prefix: bool = False) -> str | None:
    """Every term re-quoted as a phrase: FTS5 operators, quotes and
    punctuation in a question can never reach the parser as syntax. A
    hyphenated or ampersanded term is a phrase and is matched whole ('h-e-b'
    never as a prefix of 'h e bakery', 'at&t' never of 'at tiffany's')."""
    if not terms:
        return None
    return op.join(f'"{t}"' + ("*" if prefix and len(t) >= 4 and "-" not in t
                               and "&" not in t else "")
                   for t in terms)


def _phrase(label: str) -> str:
    """A name as one FTS5 phrase body: its words, lowercased, no syntax."""
    return " ".join(w["term"].strip("'") for w in _words(label))


def _match_expr(terms: list[str], groups: list[list[str]] = (),
                prefix: bool = True) -> str | None:
    """Terms AND-ed, each group of alternatives (a merchant's name and
    aliases) OR-ed inside and AND-ed with the rest."""
    parts = [_fts_expr(terms, " ", prefix=prefix)] if terms else []
    parts += [f"({_fts_expr(g)})" for g in groups if g]
    return " AND ".join(parts) or None


def fts_query(question: str, drop: set[str] | frozenset[str] | None = None) -> str | None:
    """Build a safe FTS5 MATCH expression from free text.

    Every token is extracted as word characters only and re-quoted, so FTS5
    operators, quotes, and punctuation in the question can never reach the
    parser as syntax (a raw question like `what's up? OR "` is a MATCH error,
    not a search). Tokens are OR-ed and results ranked by bm25.
    """
    return _fts_expr(_terms(_words(question), frozenset(drop or ())))


def _clean(text, cap: int = 280) -> str:
    """Evidence text as it may be shown to a model: one line, capped, and
    with no square brackets — a quoted message containing '[M1]' must never
    look like a citation."""
    t = " ".join(str(text or "").split()).replace("[", "(").replace("]", ")")
    if len(t) > cap:
        t = t[:cap - 1].rstrip() + "…"
    return t


def _plain(text, cap: int = 160) -> str:
    """An excerpt for prose: highlight markers removed."""
    return _clean((text or "").replace("«", "").replace("»", ""), cap)


# ---------------------------------------------------------------- evidence: metrics

def _round(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    if unit in ("steps", "ml", "kcal"):
        return round(value)
    return round(value, 2)


def _trend(series: list[dict]) -> int | None:
    """Percent change, second half of the window against the first; None
    when it is too short to say or the change is under 5%."""
    if len(series) < 4:
        return None
    half = len(series) // 2
    first = sum(s["value"] for s in series[:half]) / half
    second = sum(s["value"] for s in series[half:]) / (len(series) - half)
    if not first:
        return None
    change = (second - first) / abs(first) * 100
    return round(change) if abs(change) >= 5 else None


def _health_days(conn: sqlite3.Connection, metric: str, spec: dict,
                 window: dict) -> tuple[list[tuple], list[str], int, list[str]]:
    """(day, value, records, origin-or-None) per day, the origins seen, how
    many days had more than one, and the export files behind them.

    Samsung Health and Google Fit can both write the same walk; summing every
    record would count it twice. For a summed metric each day therefore takes
    the single app with the larger total, never both. Averages are safe to
    pool across apps."""
    rows = conn.execute(
        "SELECT journal_date AS day, COALESCE(origin_app, '') AS origin, "
        "SUM(value) AS total, COUNT(*) AS n, "
        "GROUP_CONCAT(DISTINCT source_file) AS sources "
        "FROM health_records WHERE metric=? AND status='active' "
        "AND value IS NOT NULL AND journal_date BETWEEN ? AND ? "
        "GROUP BY day, origin ORDER BY day, origin",
        (metric, window["start"], window["end"])).fetchall()
    per_day: dict[str, list] = {}
    sources: set[str] = set()
    for r in rows:
        per_day.setdefault(r["day"], []).append(r)
        if r["sources"]:
            sources.update(r["sources"].split(","))
    out, multi = [], 0
    for day, group in per_day.items():
        if len(group) > 1:
            multi += 1
        if spec["agg"] == "sum":
            best = max(group, key=lambda r: (r["total"], r["origin"]))
            out.append((day, best["total"], best["n"],
                        best["origin"] if len(group) > 1 else None))
        else:
            n = sum(r["n"] for r in group)
            out.append((day, sum(r["total"] for r in group) / n, n, None))
    origins = sorted({r["origin"] for r in rows if r["origin"]})
    return out, origins, multi, sorted(sources)


def _metric_series(conn: sqlite3.Connection, *, table: str, metric: str,
                   spec: dict, window: dict, entry_match: str | None = None,
                   span: dict | None = None) -> dict | None:
    """A metric over the window, with its per-day series and provenance.

    Journal measurements belong either to a day or to an entry (workout
    distance, money spent on a dinner); both count, the entry's journal date
    standing in for the measurement's own. `entry_match` (an FTS expression)
    keeps only measurements of entries that match it — 'how much did I spend
    on groceries' sums the grocery entries, not the whole month.
    """
    factor = spec.get("factor", 1)
    window = span or window
    origins: list[str] = []
    multi = 0
    entry_uuids: list[str] = []
    units: list[str] = []
    if table == "health_records":
        days_out, origins, multi, sources = _health_days(conn, metric, spec, window)
    else:
        agg = "SUM" if spec["agg"] == "sum" else "AVG"
        where = ["m.metric=?", "m.status='active'",
                 "(m.entry_id IS NULL OR e.status='active')",
                 "COALESCE(m.normalized_value, m.numeric_value) IS NOT NULL",
                 "COALESCE(m.journal_date, e.journal_date) BETWEEN ? AND ?"]
        args: list = [metric, window["start"], window["end"]]
        skip = spec.get("skip_categories")
        if skip:
            marks = ",".join("?" for _ in skip)
            where.append(
                "(m.entry_id IS NULL OR NOT EXISTS (SELECT 1 FROM entry_categories ec "
                "JOIN categories c ON c.id = ec.category_id "
                f"WHERE ec.entry_id = m.entry_id AND ec.is_primary = 1 AND c.name IN ({marks})))")
            args += list(skip)
        if entry_match:
            where.append("m.entry_id IN (SELECT rowid FROM entries_fts "
                         "WHERE entries_fts MATCH ?)")
            args.append(entry_match)
        rows = conn.execute(
            f"SELECT COALESCE(m.journal_date, e.journal_date) AS day, "
            f"{agg}(COALESCE(m.normalized_value, m.numeric_value)) AS value, "
            f"COUNT(*) AS n, GROUP_CONCAT(DISTINCT e.uuid) AS entries, "
            f"GROUP_CONCAT(DISTINCT m.unit) AS units "
            f"FROM measurements m LEFT JOIN entries e ON e.id = m.entry_id "
            f"WHERE {' AND '.join(where)} GROUP BY day ORDER BY day",
            args).fetchall()
        days_out = [(r["day"], r["value"], r["n"], None) for r in rows]
        sources = []
        entry_uuids = sorted({u for r in rows if r["entries"]
                              for u in r["entries"].split(",")})
        units = sorted({u for r in rows if r["units"] for u in r["units"].split(",")})
    if not days_out:
        return None
    unit = spec["unit"]
    if metric == "money" and len(units) == 1:
        unit = units[0].upper()
    series = []
    for day, value, n, origin in days_out:
        point = {"date": day, "value": _round(value * factor, unit), "records": n}
        if origin is not None:
            point["origin"] = origin
        series.append(point)
    values = [s["value"] for s in series]
    lo = min(series, key=lambda s: s["value"])
    hi = max(series, key=lambda s: s["value"])
    out = {
        "metric": metric,
        "label": spec["label"],
        "unit": unit,
        "origin": "sensor import" if table == "health_records" else "journal",
        "tier": "observed" if table == "health_records" else "confirmed",
        "table": table,
        "days": len(series),
        "total": _round(sum(values), unit),
        "mean": _round(sum(values) / len(values), unit),
        "min": lo["value"], "min_date": lo["date"],
        "max": hi["value"], "max_date": hi["date"],
        "latest": series[-1]["value"], "latest_date": series[-1]["date"],
        "per_day_agg": spec["agg"],
        "series": series,
        "source_files": sources,
        "trend_pct": _trend(series),
    }
    if origins:
        out["origins"] = origins
        out["multi_origin_days"] = multi
    if entry_uuids:
        out["entry_uuids"] = entry_uuids
    if span is not None:
        out["span"] = span
    return out


def detect_metrics(question: str) -> tuple[list[str], list[str]]:
    q = question.lower()
    tokens = set(re.findall(r"[\w]+", q))
    if re.search(r"\bbody\s*fat\b", q):
        tokens.add("bodyfat")
    health: list[str] = []
    journal: list[str] = []
    for words, h, j in KEYWORDS:
        if tokens & set(words):
            health += [m for m in h if m not in health]
            journal += [m for m in j if m not in journal]
    return health, journal


# ---------------------------------------------------------------- evidence: confirmed tier

def _entry_items(conn: sqlite3.Connection, rows: list, via: str) -> list[dict]:
    """Entry rows as evidence items, with categories in one query."""
    if not rows:
        return []
    marks = ",".join("?" for _ in rows)
    cats: dict[str, list[str]] = {}
    for c in conn.execute(
            "SELECT e.uuid, c.name FROM entry_categories ec "
            "JOIN categories c ON c.id = ec.category_id "
            "JOIN entries e ON e.id = ec.entry_id "
            f"WHERE e.uuid IN ({marks}) ORDER BY e.uuid, ec.is_primary DESC, c.name",
            [r["uuid"] for r in rows]):
        cats.setdefault(c["uuid"], []).append(c["name"])
    out = []
    for r in rows:
        keys = r.keys()
        snippet = r["snippet"] if "snippet" in keys else None
        if not snippet and "body" in keys and r["body"]:
            snippet = _clean(r["body"], 140)
        out.append({"uuid": r["uuid"], "date": r["journal_date"], "kind": r["kind"],
                    "title": r["title"], "snippet": snippet,
                    "categories": cats.get(r["uuid"], []), "tier": "confirmed",
                    "via": via,
                    "occurred_at": r["occurred_at"] if "occurred_at" in keys else None,
                    "due_at": r["due_at"] if "due_at" in keys else None,
                    "closed_at": r["closed_at"] if "closed_at" in keys else None,
                    "closure": r["closure"] if "closure" in keys else None})
    return out


_ENTRY_COLS = ("e.uuid, e.journal_date, e.title, e.body, e.occurred_at, e.due_at, "
               "e.closed_at, e.closure, k.name AS kind")


def search_entries(conn: sqlite3.Connection, match: str, window: dict,
                   limit: int) -> list[dict]:
    clause = ("AND e.journal_date BETWEEN ? AND ? " if window["explicit"] else "")
    args: tuple = ((match, window["start"], window["end"], limit)
                   if window["explicit"] else (match, limit))
    rows = conn.execute(
        f"SELECT {_ENTRY_COLS}, "
        "snippet(entries_fts, -1, '«', '»', '…', 14) AS snippet "
        "FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
        "JOIN entry_kinds k ON k.id = e.kind_id "
        f"WHERE entries_fts MATCH ? AND e.status='active' {clause}"
        "ORDER BY rank, e.id LIMIT ?", args).fetchall()
    return _entry_items(conn, rows, "search")


def search_passages(conn: sqlite3.Connection, match: str, window: dict,
                    limit: int) -> list[dict]:
    clause = ("AND s.journal_date BETWEEN ? AND ? " if window["explicit"] else "")
    args: tuple = ((match, window["start"], window["end"], limit)
                   if window["explicit"] else (match, limit))
    rows = conn.execute(
        "SELECT t.source_id, s.uuid AS source_uuid, s.journal_date, s.type, "
        "snippet(transcripts_fts, -1, '«', '»', '…', 24) AS snippet "
        "FROM transcripts_fts JOIN transcripts t ON t.id = transcripts_fts.rowid "
        "JOIN sources s ON s.id = t.source_id "
        f"WHERE transcripts_fts MATCH ? {clause}"
        "ORDER BY rank, t.id LIMIT ?", args).fetchall()
    return [{"source_id": r["source_id"], "source_uuid": r["source_uuid"],
             "date": r["journal_date"], "source_type": r["type"],
             "snippet": r["snippet"], "tier": "confirmed"} for r in rows]


def open_loops(conn: sqlite3.Connection, window: dict, *,
               entity_ids: list[int] | None = None, limit: int = 10,
               due_only: bool = False) -> tuple[list[dict], int, int]:
    """Open tasks and plans — closed_at NULL — soonest due first, then the
    most recently recorded. Returns (items, how many match, how many are
    open in all).

    A named window keeps what is still open then, not what was RECORDED
    then: a loop due by the window's end (overdue ones included), or one
    with no due date that already existed. Filtering on the day a task was
    noted made 'what do I need to do this week' forget every task from last
    week, and a future 'this weekend' empty by construction. `due_only`
    ('what's due this week') keeps only loops that have a due date."""
    base = ["e.status='active'", "e.closed_at IS NULL", "k.name IN ('task', 'plan')"]
    where, args = list(base), []
    if due_only:
        where.append("e.due_at IS NOT NULL")
    if window["explicit"]:
        where.append("(CASE WHEN e.due_at IS NOT NULL THEN substr(e.due_at, 1, 10) <= ? "
                     "ELSE e.journal_date <= ? END)")
        args += [window["end"], window["end"]]
    if entity_ids:
        marks = ",".join("?" for _ in entity_ids)
        where.append(f"e.id IN (SELECT entry_id FROM mentions WHERE entity_id IN ({marks}))")
        args += entity_ids
    w = " AND ".join(where)
    count = ("SELECT COUNT(*) AS c FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
             "WHERE {}")
    total = conn.execute(count.format(w), args).fetchone()["c"]
    open_all = conn.execute(count.format(" AND ".join(base))).fetchone()["c"]
    rows = conn.execute(
        f"SELECT {_ENTRY_COLS} FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        f"WHERE {w} ORDER BY e.due_at IS NULL, e.due_at, e.journal_date DESC, e.id DESC "
        f"LIMIT ?", [*args, limit]).fetchall()
    return _entry_items(conn, rows, "loop"), total, open_all


# ---------------------------------------------------------------- entities

def _resolve_entities(conn: sqlite3.Connection, question: str,
                      words: list[dict]) -> tuple[list[dict], list[dict]]:
    """Names in the question that are known entities: (resolved, ambiguous).

    Exact names and aliases only, never fuzzy — case- and accent-insensitive,
    over every run of up to four words, so voice input ('jake'), accents
    ('José') and multi-word names ('Maya Chen', 'Dr. Ellis') all resolve.
    One more rule, because people are named by first name: a word that is
    the first name of exactly ONE known person resolves to them. A name that
    fits several entities is ambiguous: listed, never used as a filter —
    retrieval does not guess.
    """
    ents = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, name, type FROM entities WHERE status='active'")}
    labels: dict[str, dict[int, str]] = {}
    raw_labels: list[tuple[str, int]] = []
    for e in ents.values():
        labels.setdefault(_key(e["name"]), {})[e["id"]] = "name"
        raw_labels.append((e["name"], e["id"]))
    for r in conn.execute(
            "SELECT a.alias, a.entity_id FROM entity_aliases a "
            "JOIN entities e ON e.id = a.entity_id WHERE e.status='active'"):
        labels.setdefault(_key(r["alias"]), {}).setdefault(r["entity_id"], "alias")
        raw_labels.append((r["alias"], r["entity_id"]))
    first_names: dict[str, set[int]] = {}
    for label, eid in raw_labels:
        # Word by word as the question is read: a hyphenated first name
        # ('Jean-Luc') is one word there too.
        parts = [w["key"] for w in _words(label) if w["key"] not in _HONORIFICS]
        if parts and ents[eid]["type"] == "person":
            first_names.setdefault(parts[0], set()).add(eid)

    keys = [w["key"] for w in words]
    consumed = [False] * len(words)
    has_upper = any(c.isupper() for c in question)
    found: dict[int, dict] = {}
    ambiguous: list[dict] = []

    def month_word(i: int) -> bool:
        # 'in June' is the month even when someone is called June.
        return (keys[i] in MONTHS or keys[i] in MONTH_ABBR) and i > 0 \
            and keys[i - 1] in _TIME_LEAD

    def add(ids, span: list[int], via: str) -> None:
        for j in span:
            consumed[j] = True
        matched = " ".join(words[j]["raw"] for j in span)
        if len(ids) > 1:
            ambiguous.append({"matched": matched, "candidates": sorted(
                ({"entity_id": i, "name": ents[i]["name"], "type": ents[i]["type"]}
                 for i in ids), key=lambda c: (c["name"], c["entity_id"]))})
            return
        eid = next(iter(ids))
        ent = found.setdefault(eid, {
            "name": ents[eid]["name"], "type": ents[eid]["type"],
            "matched": matched, "via": via, "id": eid, "entity_id": eid,
            "tokens": [], "positions": []})
        ent["tokens"] += [keys[j] for j in span]
        ent["positions"] += span

    for n in (4, 3, 2, 1):
        for i in range(len(words) - n + 1):
            if any(consumed[i:i + n]):
                continue
            key = " ".join(keys[i:i + n])
            if n == 1 and (key in STOPWORDS or len(key) < 2 or month_word(i)):
                continue
            hits = labels.get(key)
            if hits:
                add(hits, list(range(i, i + n)),
                    "name" if "name" in hits.values() else "alias")
    for i, w in enumerate(words):
        key = w["key"]
        if (consumed[i] or key in STOPWORDS or len(key) < 3
                or key in _HONORIFICS or month_word(i)):
            continue
        # In a typed, capitalised question a lowercase 'park' is a word, not
        # Grace Park; voice input is all lowercase and gets the benefit.
        if has_upper and not w["raw"][:1].isupper():
            continue
        if key in first_names:
            add(first_names[key], [i], "first name")
    return list(found.values()), ambiguous


def _entity_entries(conn: sqlite3.Connection, entity_id: int, window: dict,
                    limit: int = 5) -> list[dict]:
    """Entries that mention the entity — stated facts first, then newest."""
    clause = "AND e.journal_date BETWEEN ? AND ? " if window["explicit"] else ""
    args = [entity_id, *([window["start"], window["end"]] if window["explicit"] else []),
            limit]
    rows = conn.execute(
        f"SELECT {_ENTRY_COLS} FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        f"WHERE e.status='active' AND e.id IN "
        f"(SELECT entry_id FROM mentions WHERE entity_id = ?) {clause}"
        f"ORDER BY (k.name = 'fact') DESC, e.journal_date DESC, e.id DESC LIMIT ?",
        args).fetchall()
    return _entry_items(conn, rows, "entity")


def _relationships(conn: sqlite3.Connection, entity_id: int) -> list[dict]:
    """Confirmed relationships touching the entity ("Jake Morales from El
    Paso"), each with the entry that established it, so the sentence can be
    cited like any other confirmed fact. A relationship added by hand with no
    source entry has nothing to cite and is left out of the answer (it is
    still on `entities show`). Pre-v5 databases have no relationships."""
    from .domain import entity_links
    try:
        links = entity_links.for_entity(conn, entity_id)
    except sqlite3.OperationalError:
        return []
    out = []
    for link in links:
        entry_uuid = link["provenance"].get("entry_uuid")
        if not entry_uuid:
            continue
        rows = conn.execute(
            f"SELECT {_ENTRY_COLS} FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
            f"WHERE e.uuid = ? AND e.status = 'active'", (entry_uuid,)).fetchall()
        items = _entry_items(conn, rows, "relationship")
        if items:
            out.append({"text": link["text"], "type": link["type"],
                        "other": link["other"]["name"], "entry": items[0]})
    return out


def _last_mention(conn: sqlite3.Connection, entity_id: int, window: dict,
                  today: date, participant_only: bool,
                  match: str | None = None) -> list[dict]:
    """The newest entry naming the entity — for a person, only one they took
    part in (a fact about them is not contact): the confirmed side of 'when
    did I last talk to X'. `match` (an FTS expression) keeps only entries
    that say what kind of contact it was, for 'when did I last call X'."""
    clause = "AND e.journal_date BETWEEN ? AND ? " if window["explicit"] else ""
    role = " AND role = 'participant'" if participant_only else ""
    fts = ("AND e.id IN (SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?) "
           if match else "")
    rows = conn.execute(
        f"SELECT {_ENTRY_COLS} FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
        f"WHERE e.status='active' AND e.journal_date <= ? AND e.id IN "
        f"(SELECT entry_id FROM mentions WHERE entity_id = ?{role}) "
        f"{clause}{fts}ORDER BY e.journal_date DESC, COALESCE(e.occurred_at, '') DESC, "
        f"e.id DESC LIMIT 1",
        [today.isoformat(), entity_id,
         *([window["start"], window["end"]] if window["explicit"] else []),
         *([match] if match else [])]).fetchall()
    return _entry_items(conn, rows, "contact")


def _labels(conn: sqlite3.Connection, ent: dict) -> list[str]:
    """An entity's name and aliases — every way a record may spell it."""
    return [ent["name"]] + [r["alias"] for r in conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
        (ent["id"],))]


def find_entities(conn: sqlite3.Connection, question: str) -> list[dict]:
    """Known entities the question names, each with its recent entries."""
    resolved, _ = _resolve_entities(conn, question, _words(question))
    everything = {"explicit": False}
    for ent in resolved:
        ent["entries"] = [{"uuid": e["uuid"], "date": e["date"], "title": e["title"],
                           "kind": e["kind"]}
                          for e in _entity_entries(conn, ent["id"], everything)]
    return resolved


# ---------------------------------------------------------------- observed tier

TEXT_STREAMS = ("imessage", "sms", "rcs", "android_sms", "android_mms", "whatsapp")
CALL_STREAMS = ("ios_call", "android_call")
AI_STREAMS = ("claude_chat", "chatgpt_chat")
# Reactions and thread events carry no prose of their own: searching them
# only ever matches the group name they inherit.
QUIET_STREAMS = ("text_reaction", "text_event")

STREAM_GROUPS: dict[str, tuple[str, ...]] = {
    "text": TEXT_STREAMS,
    "call": CALL_STREAMS,
    "calendar": ("calendar_event",),
    "contact card": ("contact_card",),
    "note": ("note",),
    "transaction": ("txn",),
    "photo": ("photo",),
    "place visit": ("place_visit",),
    "web visit": ("browser_visit",),
    "AI chat": AI_STREAMS,
}
STREAM_LABELS = {s: label for label, streams in STREAM_GROUPS.items()
                 for s in streams}
AI_NAMES = {"claude_chat": "Claude", "chatgpt_chat": "ChatGPT"}
# What 'when did I last talk to X' counts as contact.
CONTACT_GROUPS = ("text", "call", "calendar")
# Where a place's visits are: a Timeline visit there, an event held there.
_VISIT_STREAMS = ("place_visit", "calendar_event")
# What a recap of a day lists beside its entries: where you were and what
# you did there, not every message and web page.
_RECAP_STREAMS = ("calendar_event", "place_visit", "photo", "txn") + CALL_STREAMS
# What a journal entry says when it was a call or a text — the confirmed
# side of 'when did I last CALL Mom'.
_CHANNEL_WORDS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (CALL_STREAMS, ("call", "called", "calls", "calling", "phone", "phoned",
                    "ring", "rang", "facetime", "facetimed")),
    (TEXT_STREAMS, ("text", "texted", "texts", "texting", "message", "messaged",
                    "messages", "whatsapp", "imessage", "sms", "dm", "dms")),
)
# The span of a search that was not clipped to the window.
ALL_RECORDS = "all records"
_UNTIMED = ("contact card", "note", "transaction")

# Question words that narrow the observed search to some streams.
STREAM_WORDS: list[tuple[frozenset[str], tuple[str, ...]]] = [
    (frozenset({"claude"}), ("claude_chat",)),
    (frozenset({"chatgpt", "gpt", "openai"}), ("chatgpt_chat",)),
    (frozenset({"ai", "chatbot", "chatbots"}), AI_STREAMS),
    (frozenset({"text", "texts", "texted", "texting", "message", "messages",
                "messaged", "imessage", "imessages", "sms", "rcs", "dm", "dms"}),
     TEXT_STREAMS),
    (frozenset({"whatsapp"}), ("whatsapp",)),
    # 'phone' and 'ring' are calls only as verbs ('phone me', not 'phone
    # number'; 'Jake rang', not 'the ring'): parse_intent decides.
    (frozenset({"call", "calls", "called", "calling", "phoned", "phoning", "rang",
                "facetime", "facetimed", "facetiming"}), CALL_STREAMS),
    # What is on the calendar, and what is coming: 'what's on my schedule',
    # 'what do I have scheduled', 'any events this week', 'what's upcoming'.
    (frozenset({"calendar", "meeting", "meetings", "appointment",
                "appointments", "invite", "invites", "schedule", "schedules",
                "scheduled", "event", "events", "agenda", "upcoming"}),
     ("calendar_event",)),
    (frozenset({"note", "notes"}), ("note",)),
    (frozenset({"photo", "photos", "picture", "pictures", "pic", "pics",
                "selfie", "selfies"}), ("photo",)),
    (frozenset({"transaction", "transactions", "charge", "charges", "charged",
                "bank"}), ("txn",)),
    (frozenset({"website", "websites", "browsed", "browsing", "webpage"}),
     ("browser_visit",)),
]

_OUT_VERBS = frozenset({"ask", "asked", "tell", "told", "say", "said", "text",
                        "texted", "send", "sent", "message", "messaged",
                        "write", "wrote", "call", "called", "email", "emailed",
                        "phoned", "rang", "facetime", "facetimed"})
# Calls only when used as a verb (`_CALL_VERB_RE`); otherwise a phone is a
# number on a card and a ring is jewellery.
_CALL_NOUNISH = frozenset({"phone", "phones", "ring", "rings"})
_CALL_VERB_RE = re.compile(
    r"\b(?:phone|ring)s?\s+(?:me|you|him|her|them|us)\b|\bon the phone\b"
    r"|\b(?:i|we|you|he|she|they)\s+(?:(?:last|ever|just|recently|first)\s+)?(?:phone|ring)\b")
# A question that asks WHEN or WHETHER ('did Jake call me this week', 'when
# did Jake call'), not what was said: the contact is the answer.
_ASKS_WHEN = re.compile(r"^\W*(?:(?:hey|so|ok|okay|and|but|um)\W+)*"
                        r"(?:when\b|did\b|has\b|have\b|had\b|how long\b|how recently\b"
                        r"|is there\b|was there\b|were there\b|any\b)")
_IN_VERBS = _OUT_VERBS | {"says", "tells", "texts", "suggest", "suggested",
                          "recommend", "recommended", "reply", "replied",
                          "answer", "answered"}
# May stand between who and the verb: 'when did Maya last text me'.
_VERB_ADVERBS = frozenset({"last", "ever", "recently", "first", "most", "just",
                           "also", "finally", "actually", "really"})
_CONTACT_VERBS = frozenset({"talk", "talked", "spoke", "speak", "spoken",
                            "text", "texted", "message", "messaged", "call",
                            "called", "see", "saw", "seen", "hear", "heard",
                            "meet", "met", "chat", "chatted", "email",
                            "emailed", "visit", "visited", "go", "went", "been",
                            "contact", "contacted", "phoned", "rang",
                            "facetime", "facetimed"})
_RECENT_RE = re.compile(r"\b(?:last|latest|most recent(?:ly)?|recently)\b")
# A phone is profile only as a thing someone has ('phone number', "Maya's
# phone"); 'when did Jake last phone me' is a call.
_PROFILE_RE = re.compile(
    r"\b(?:favou?rites?|birthday|born|hometown|number|email|e-mail|address|"
    r"lives?|living|grew|grow|raised|from)\b|\bphone\s*(?:#|no\b)|'s\s+(?:phone|cell|mobile)\b"
    r"|\bwho(?:'s| is)\b|\btell me about\b")
# A question after someone's numbers: their card's phones and emails lead.
_NUMBERS_RE = re.compile(r"\b(?:phone|number|numbers|cell|mobile|e-?mail|emails|"
                         r"contact (?:info|details))\b", re.IGNORECASE)
_LOOPS_RE = re.compile(
    r"\bopen (?:tasks?|loops?|items?|plans?|to-?dos?)\b|\bto-?dos?\b"
    r"|\bon my (?:list|plate)\b|\b(?:need|have) to do\b|\bunfinished\b"
    r"|\boutstanding (?:tasks?|items?)\b|\bpending (?:tasks?|items?)\b"
    r"|\bmy (?:tasks|plans)\b"
    # 'what tasks are open', 'any tasks this week', 'show me my tasks'
    r"|\b(?:what|which|any|list|show(?:\s+me)?|all)\s+(?:(?:the|my|open|pending|"
    r"outstanding|remaining|overdue)\s+)*(?:tasks?|loops?)\b"
    # 'what tasks are due this week', "what's due", 'anything overdue'
    r"|\b(?:tasks?|to-?dos?|plans?|items?)\b(?:\s+[\w']+){0,4}?\s+(?:over)?due\b"
    r"|\b(?:what(?:'s|\s+is|\s+are)?|anything|is\s+(?:there\s+)?anything)\s+"
    r"(?:[\w']+\s+){0,3}?(?:over)?due\b|\boverdue\b")
# 'due' asks for what has a due date by then, not every open loop.
_DUE_RE = re.compile(r"\b(?:over)?due\b")
LOOP_WORDS = frozenset({"open", "task", "tasks", "todo", "todos", "to-do",
                        "to-dos", "list", "plate", "loop", "loops", "outstanding",
                        "unfinished", "pending", "plans", "items", "need", "due",
                        "overdue", "remaining"})


def has_observed_tier(conn: sqlite3.Connection) -> bool:
    """A pre-v4 database has no observed tier; retrieval skips it rather
    than failing — `ask` must work on an un-migrated read-only database."""
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE name IN "
            "('observations', 'observations_fts', 'observation_participants', "
            "'entity_handles', 'imports')")}
    except sqlite3.Error:
        return False
    return len(names) == 5


def stream_label(stream: str) -> str:
    return STREAM_LABELS.get(stream, stream.replace("_", " "))


def parse_intent(question: str, words: list[dict], entities: list[dict],
                 health: list[str], journal: list[str],
                 topic_words: list[dict] | None = None) -> dict:
    """What kind of record the question is after, by rule. `topic_words`
    are the words the topic is taken from — the question minus its time
    words (`without_window_words`); `words` keep every position, because
    that is where the names were found."""
    q = question.lower().replace("’", "'")
    keys = [w["key"] for w in words]
    persons = [e for e in entities if e["type"] == "person"]
    person_pos = {p for e in persons for p in e["positions"]}
    streams: list[str] = []
    stream_keys: set[str] = set()
    for i, k in enumerate(keys):
        if i in person_pos:            # a person called Claude is a person
            continue
        for vocab, group in STREAM_WORDS:
            if k in vocab:
                stream_keys.add(k)
                streams += [s for s in group if s not in streams]

    def verb_after(pos: int, verbs: frozenset[str]) -> int | None:
        # 'Maya texted me', and with an adverb between: 'when did Maya last
        # text me', 'did I ever tell Jake'.
        nxt = pos + 1
        while nxt < len(keys) and nxt - pos <= 2 and keys[nxt] in _VERB_ADVERBS:
            nxt += 1
        return nxt if nxt < len(keys) and keys[nxt] in verbs else None

    def followed_by(pos: int, verbs: frozenset[str]) -> bool:
        return verb_after(pos, verbs) is not None

    def possessive(pos: int) -> bool:
        return bool(re.search(r"'s$", words[pos]["raw"], re.IGNORECASE))

    # 'phone me', 'did I ring Jake', 'when did Jake last phone': a call. 'Jake's
    # phone number' and 'the ring' are not.
    call_verb = bool(_CALL_VERB_RE.search(q))
    for e in persons:
        for p in e["positions"]:
            j = verb_after(p, _CALL_NOUNISH)
            if j is not None and not possessive(p) and not (
                    j + 1 < len(keys) and keys[j + 1] in ("number", "numbers", "no")):
                call_verb = True
    if call_verb:
        stream_keys |= set(keys) & _CALL_NOUNISH
        streams += [s for s in CALL_STREAMS if s not in streams]
    if re.search(r"\bcoming up\b", q):
        streams += [s for s in ("calendar_event",) if s not in streams]
    out_verbs = _OUT_VERBS | (_CALL_NOUNISH if call_verb else frozenset())
    in_verbs = _IN_VERBS | out_verbs

    out = any(k == "i" and followed_by(i, out_verbs) for i, k in enumerate(keys))
    # "Jake's email" is a thing Jake has, not something Jake sent.
    senders = [e["id"] for e in persons
               if any(followed_by(p, in_verbs) and not possessive(p)
                      for p in e["positions"])]
    ai_in = any(k in ("claude", "chatgpt", "gpt") and i not in person_pos
                and followed_by(i, in_verbs) for i, k in enumerate(keys))
    direction = None
    if out and not (senders or ai_in):
        direction = "out"
    elif (senders or ai_in) and not out:
        direction = "in"

    loops = bool(_LOOPS_RE.search(q))
    money = "money" in journal or "txn" in streams
    drop = set(INTENT_WORDS) | stream_keys
    drop |= {t for e in persons for t in e["tokens"]}
    # Time words are never the topic — unless a place or a thing is called
    # that ('Sunday Market'), when they are its name.
    drop |= TIME_WORDS - {t for e in entities for t in e["tokens"]}
    if loops:
        drop |= LOOP_WORDS
    if money:
        drop |= set(MONEY_WORDS)
    else:
        drop |= RECAP_WORDS
    topic_words = words if topic_words is None else topic_words
    topic = _terms(topic_words, frozenset(drop), keep_short=money)
    display = {w["term"].strip("'"): w["raw"] for w in topic_words}
    merchant = None
    if money:
        slot = _merchant_slot(topic_words, {t for e in persons for t in e["tokens"]})
        if slot:
            merchant = " ".join(w["raw"] for w in slot)
            topic = list(dict.fromkeys([w["term"].strip("'") for w in slot] + topic))[:12]
    channel = set(streams) & set(TEXT_STREAMS + CALL_STREAMS)
    return {
        "streams": streams,
        "direction": direction,
        "senders": senders if direction == "in" else [],
        "recency": bool(_RECENT_RE.search(q) and (set(keys) & _CONTACT_VERBS or call_verb))
        or "last time" in q
        or bool(re.search(r"\b(?:was|were) (?:i|we) (?:last|most recently)\b", q)),
        # 'Did Jake call me this week', 'when did Jake call': a channel named
        # for a person and nothing to find in what was said — the contact
        # (or that there was none) is the answer, not their card.
        "contact": bool(channel and persons and not topic and _ASKS_WHEN.search(q)),
        "profile": bool(_PROFILE_RE.search(q)),
        "loops": loops,
        # 'what's due this week': open loops with a due date by then.
        "due": loops and bool(_DUE_RE.search(q)),
        # 'what's coming up', 'what's next on my calendar': from today on.
        "forward": bool(re.search(r"\b(?:upcoming|coming|ahead|scheduled|planned|next|soon|"
                                  r"later)\b|\brest of\b", q)),
        "money": money,
        # The merchant a money question named ('spend at AT&T'), kept even
        # when it is short, a number or a stopword ('76', 'H&M').
        "merchant": merchant,
        "topic_terms": topic,
        "topic_display": " ".join(display.get(t, t) for t in topic),
        # A pure metric question ('how did I sleep') is answered by the
        # metrics; searching texts for 'sleep' would only add noise.
        "metric_only": bool(health or journal) and not money and not streams
        and not entities and not loops,
    }


# The merchant slot of a money question: the words after 'at' or 'from'
# ('how much did I spend at AT&T', 'what did I buy from H&M'), up to a word
# that ends it ('at Uchi in October' -> Uchi).
_SLOT_LEAD = frozenset({"at", "from"})
_SLOT_TRIGGER = frozenset(MONEY_WORDS) | {"transaction", "transactions", "charge",
                                          "charges", "charged"}
_SLOT_STOP = TIME_WORDS | frozenset({
    "in", "on", "during", "over", "for", "since", "between", "before", "after", "until",
    "till", "by", "last", "this", "next", "past", "and", "or", "with", "when", "while",
    "so", "than", "vs", "versus", "compared", "today", "yesterday", "around", "about",
    "per", "each", "every", "total", "altogether", "overall"})
_SLOT_FILLER = frozenset({"the", "a", "an", "my", "our"})
# 'spend at all', 'at least', 'at night', 'at once': no merchant.
_NOT_MERCHANT = frozenset({"all", "least", "most", "once", "first", "it", "that", "this",
                           "them", "there", "times", "any", "some"})


def _merchant_slot(words: list[dict], person_keys: set[str]) -> list[dict]:
    """The words of a money question's merchant slot, or [] when it names
    none."""
    money_at = next((i for i, w in enumerate(words) if w["term"] in _SLOT_TRIGGER), None)
    if money_at is None:
        return []
    lead = next((i for i in range(money_at + 1, len(words))
                 if words[i]["term"] in _SLOT_LEAD), None)
    if lead is None:
        return []
    slot: list[dict] = []
    for w in words[lead + 1:]:
        t = w["term"].strip("'")
        if t in _SLOT_STOP:
            break
        if (not slot and t in _SLOT_FILLER) or w["key"] in person_keys:
            continue
        slot.append(w)
    if slot and slot[0]["term"].strip("'") in _NOT_MERCHANT:
        return []
    return slot


_OBS_COLS = ("o.id, o.uuid, o.stream, o.journal_date, o.started_at, o.ended_at, "
             "o.utc_offset_sec, o.direction, o.title, substr(o.body, 1, 1500) AS body, "
             "o.value, o.unit, o.value_json, o.origin_app, i.uuid AS import_uuid, "
             "i.connector, i.account, i.source_file")
# Abandoned ChatGPT branches stay in the archive but rank after the answer
# that was kept.
_OFF_PATH = ("CASE WHEN o.stream = 'chatgpt_chat' AND (CASE WHEN json_valid(o.value_json) "
             "THEN json_extract(o.value_json, '$.on_current_path') END) = 0 "
             "THEN 1 ELSE 0 END")
_RECENT_FIRST = "o.journal_date DESC, o.started_at DESC, o.id DESC"


def _party_clause(family: list[int], sender_only: bool = False) -> tuple[str, list]:
    """Observations with a participant whose handle the owner linked to
    this entity (or to one merged into it)."""
    marks = ",".join("?" for _ in family)
    return ("EXISTS (SELECT 1 FROM observation_participants p "
            "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
            f"WHERE p.observation_id = o.id AND h.entity_id IN ({marks}) "
            "AND h.confirmed_at IS NOT NULL AND h.ignored = 0"
            + (" AND p.role = 'sender'" if sender_only else "") + ")"), list(family)


def _entity_family(conn: sqlite3.Connection, entity_id: int) -> list[int]:
    """The entity plus everything merged into it: merge moves mentions and
    aliases, but handles stay on the merged row."""
    return [r[0] for r in conn.execute(
        "WITH RECURSIVE fam(id) AS (SELECT ? UNION "
        "SELECT e.id FROM entities e JOIN fam ON e.merged_into = fam.id) "
        "SELECT id FROM fam ORDER BY id", (entity_id,))]


def _linked_handles(conn: sqlite3.Connection, family: list[int]) -> list[str]:
    marks = ",".join("?" for _ in family)
    return [f"{r['kind']}:{r['value']}" for r in conn.execute(
        f"SELECT kind, value FROM entity_handles WHERE entity_id IN ({marks}) "
        f"AND confirmed_at IS NOT NULL AND ignored = 0 ORDER BY kind, value", family)]


class _LiveNames:
    """Entity id -> (live id, name), following merges; cached per question."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn, self.cache = conn, {}

    def get(self, entity_id: int) -> tuple[int, str] | None:
        if entity_id not in self.cache:
            row, cur, seen = None, entity_id, set()
            while cur is not None and cur not in seen:
                seen.add(cur)
                row = self.conn.execute(
                    "SELECT id, name, status, merged_into FROM entities WHERE id=?",
                    (cur,)).fetchone()
                if row is None or row["status"] != "merged":
                    break
                cur = row["merged_into"]
            self.cache[entity_id] = (row["id"], row["name"]) if row else None
        return self.cache[entity_id]


def _detail(value_json: str | None) -> dict:
    """The structured fields worth showing: scalars only, capped, cleaned."""
    if not value_json:
        return {}
    try:
        data = json.loads(value_json)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k in sorted(data):
        v = data[k]
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str) and v.strip():
            out[k] = _clean(v, 200)
        if len(out) >= 12:
            break
    return out


def _local_hhmm(started_at: str, offset: int | None, cfg: Config) -> str | None:
    try:
        dt = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    if offset is not None:
        return (dt + timedelta(seconds=offset)).strftime("%H:%M")
    return dt.astimezone(ZoneInfo(cfg.timezone)).strftime("%H:%M")


def _names(ps: list[dict]) -> str:
    seen: list[str] = []
    for p in ps:
        if p["name"] not in seen:
            seen.append(p["name"])
    return ", ".join(seen[:4]) + (f" and {len(seen) - 4} more" if len(seen) > 4 else "")


def _people(o: dict) -> list[dict]:
    """Who took part besides the owner. A 'location' participant is the
    place an event or a visit was at — said with 'at', never 'with'."""
    return [p for p in o["participants"] if not p["is_owner"] and p["role"] != "location"]


def _places(o: dict) -> list[dict]:
    return [p for p in o["participants"] if p["role"] == "location" and p["entity"]]


def _who(o: dict) -> str | None:
    if o["stream"] in AI_STREAMS:
        bot = AI_NAMES.get(o["stream"], "the assistant")
        return f"you → {bot}" if o["direction"] == "out" else f"{bot} → you"
    if o["kind"] in ("contact card", "note", "photo", "transaction", "web visit"):
        return None
    if o["kind"] == "place visit":
        places = _places(o)
        return f"at {_names(places)}" if places else None
    others = _people(o)
    senders = [p for p in others if p["role"] in ("sender", "organizer")]
    rest = [p for p in others if p not in senders]
    if o["direction"] == "out":
        return "you → " + (_names(rest or others) or "?")
    if o["direction"] == "in":
        return (_names(senders or others) or "?") + " → you"
    return ("with " + _names(others)) if others else None


_owner_cache: list = [None, None, None]        # [conn, version, identity]


def _owner_identity(conn: sqlite3.Connection, cfg: Config
                    ) -> tuple[frozenset[str], frozenset[tuple[str, str]], frozenset[int]]:
    """Who 'you' are across every import: (accounts, handles, entity ids).

    An import's own account is not enough. The iPhone calendar export's
    account is 'calendar:Home', so the owner's address on its events read as
    a third party ('with Alex Rivera, Dr. Rebecca Ellis'). The rule is
    lifecrm/owner.py's, shared with the web UI and the review pack: the
    [owner] handles in config.toml, any import's account, any handle that
    sent an outgoing record, and the entities linked to them. Cached per
    connection until the data or the configured handles change, because
    every evidence list asks."""
    from . import owner as owner_mod
    version = (conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0],
               owner_mod.configured(cfg))
    if _owner_cache[0] is conn and _owner_cache[1] == version:
        return _owner_cache[2]
    ident = owner_mod.identity(conn, cfg)
    identity = (ident.accounts, ident.handles, ident.entities)
    _owner_cache[:] = [conn, version, identity]
    return identity


def _card_details(r, participants: list[dict]) -> dict:
    """A contact card's phone numbers and email addresses — as written on
    the card when the export kept that, else the handles themselves. They
    are evidence like any other field: 'what's Maya's phone number' must be
    answerable, and a model quoting the number must find it to cite."""
    try:
        data = json.loads(r["value_json"]) if r["value_json"] else {}
    except (TypeError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}

    def listed(key: str, field: str) -> list[str]:
        out = []
        for x in data.get(key) or ():
            value = x.get(field) if isinstance(x, dict) else x
            if isinstance(value, str) and value.strip():
                label = x.get("label") if isinstance(x, dict) else None
                text = _clean(value, 80)
                out.append(f"{text} ({_clean(label, 30)})" if label else text)
        return out

    phones, emails = listed("phones", "number"), listed("emails", "address")
    if not phones:
        phones = [p["handle"].split(":", 1)[1] for p in participants
                  if p["handle"].startswith("phone:")]
    if not emails:
        emails = [p["handle"].split(":", 1)[1] for p in participants
                  if p["handle"].startswith("email:")]
    return {"phones": phones[:4], "emails": emails[:4]}


def _obs_items(conn: sqlite3.Connection, cfg: Config, rows: list, *,
               via: str, cap: int = 280) -> list[dict]:
    """Observation rows as evidence items: participants resolved through
    entity_handles to entity names (or kind:value while unresolved), the
    owner shown as 'you', provenance down to the import and file."""
    if not rows:
        return []
    owner_accounts, owner_handles, owner_entities = _owner_identity(conn, cfg)
    marks = ",".join("?" for _ in rows)
    parts: dict[int, list] = {}
    for p in conn.execute(
            "SELECT p.observation_id, p.role, p.handle_kind, p.handle_value, "
            "h.display_name, h.entity_id, h.confirmed_at, h.ignored "
            "FROM observation_participants p "
            # LEFT: a participant whose handle row is missing is still a
            # participant — shown as kind:value, never dropped.
            "LEFT JOIN entity_handles h ON h.kind = p.handle_kind "
            "AND h.value = p.handle_value "
            f"WHERE p.observation_id IN ({marks}) ORDER BY p.observation_id, "
            "CASE p.role WHEN 'sender' THEN 0 WHEN 'organizer' THEN 1 "
            "WHEN 'recipient' THEN 2 ELSE 3 END, p.handle_kind, p.handle_value",
            [r["id"] for r in rows]):
        parts.setdefault(p["observation_id"], []).append(p)
    live = _LiveNames(conn)
    items = []
    for r in rows:
        participants = []
        for p in parts.get(r["id"], []):
            value = p["handle_value"]
            low = value.lower()
            ent = (live.get(p["entity_id"]) if p["entity_id"] is not None
                   and p["confirmed_at"] and not p["ignored"] else None)
            owner = (low in owner_accounts
                     or any(low.endswith(":" + a) for a in owner_accounts)
                     or (p["handle_kind"], low) in owner_handles
                     or bool(ent and ent[0] in owner_entities)
                     or (p["display_name"] or "").lower().startswith("you (")
                     or (r["direction"] == "out" and p["role"] == "sender"))
            participants.append({
                "role": p["role"], "handle": f"{p['handle_kind']}:{value}",
                "display_name": _clean(p["display_name"], 80) or None,
                "entity_id": ent[0] if ent else None,
                "entity": ent[1] if ent else None, "is_owner": owner,
                "name": "you" if owner else (ent[1] if ent else
                                             f"{p['handle_kind']}:{value}")})
        keys = r.keys()
        snippet = r["snippet"] if "snippet" in keys and r["snippet"] else None
        kind = stream_label(r["stream"])
        item = {"id": None, "tier": "observed", "uuid": r["uuid"],
                "stream": r["stream"], "kind": kind, "date": r["journal_date"],
                "started_at": r["started_at"],
                "local_time": _local_hhmm(r["started_at"], r["utc_offset_sec"], cfg),
                "direction": r["direction"],
                "title": _clean(r["title"], 120) or None,
                "snippet": _clean(snippet or r["body"], cap) or None,
                "participants": participants, "who": None,
                "connector": r["connector"], "import_uuid": r["import_uuid"],
                "account": r["account"], "source_file": r["source_file"],
                "value": r["value"], "unit": r["unit"],
                "detail": _detail(r["value_json"]), "origin_app": r["origin_app"],
                "via": via}
        if r["stream"] == "contact_card":
            item["card"] = _card_details(r, participants)
        item["who"] = _who(item)
        items.append(item)
    return items


def search_observations(conn: sqlite3.Connection, cfg: Config, *,
                        terms: list[str] | tuple = (), window: dict | None = None,
                        streams: list[str] | tuple | None = None,
                        direction: str | None = None,
                        family: list[int] | None = None, sender_only: bool = False,
                        limit: int = 6, recency: bool = False,
                        via: str = "search", around: str | None = None) -> list[dict]:
    """Full-text search over the observed tier (status active only).

    Terms are AND-ed first and the result topped up with an OR search, so a
    message naming both 'lake' and 'Slovenia' outranks one naming either.
    Terms of four letters or more match as prefixes ('lake' finds
    'lakeside'). A conversation title is weighted below the message body:
    every turn of a chat called "Lake crew" carries that title, and must not
    outrank the one message that is actually about the lake. `family`
    restricts to observations an entity took part in; `sender_only` to
    ones it sent. The window clips only when the question named a time —
    the same rule as entries. With no terms, `around` (today) lists what is
    coming first, soonest first, then what has passed, newest first: a
    window reaching into the future (a calendar's week) would otherwise
    lead with its furthest day.
    """
    where = ["o.status = 'active'"]
    args: list = []
    if streams:
        where.append(f"o.stream IN ({','.join('?' for _ in streams)})")
        args += list(streams)
    else:
        where.append(f"o.stream NOT IN ({','.join('?' for _ in QUIET_STREAMS)})")
        args += list(QUIET_STREAMS)
    if direction:
        where.append("o.direction = ?")
        args.append(direction)
    if window and window.get("explicit"):
        where.append("o.journal_date BETWEEN ? AND ?")
        args += [window["start"], window["end"]]
    if family:
        clause, fargs = _party_clause(family, sender_only)
        where.append(clause)
        args += fargs
    w = " AND ".join(where)
    rows: list = []
    if not terms:
        order, oargs = _RECENT_FIRST, []
        if around:
            order = ("o.journal_date < ?, CASE WHEN o.journal_date >= ? THEN "
                     "o.journal_date END, o.journal_date DESC, o.started_at, o.id")
            oargs = [around, around]
        rows = conn.execute(
            f"SELECT {_OBS_COLS}, NULL AS snippet FROM observations o "
            f"JOIN imports i ON i.id = o.import_id WHERE {w} "
            f"AND (o.body IS NOT NULL OR o.title IS NOT NULL OR o.value IS NOT NULL) "
            f"ORDER BY {_OFF_PATH}, {order} LIMIT ?", [*args, *oargs, limit]).fetchall()
        return _obs_items(conn, cfg, rows, via=via)
    order = _RECENT_FIRST if recency else "bm25(observations_fts, 0.5, 1.0), o.id"
    exprs = [_fts_expr(list(terms), " ", prefix=True)]
    if len(terms) > 1:
        exprs.append(_fts_expr(list(terms), " OR ", prefix=True))
    # A text's title is its group's name, inherited by every message in the
    # thread: "Bled crew" would make each one a hit for 'Bled'. Texts match
    # on what was said; an AI chat's title is the owner's own topic and counts.
    text_marks = ",".join("?" for _ in TEXT_STREAMS + QUIET_STREAMS)
    body_only = (f"(o.stream NOT IN ({text_marks}) OR o.id IN (SELECT rowid "
                 f"FROM observations_fts WHERE observations_fts MATCH ?))")
    seen: set[int] = set()
    for expr in exprs:
        if len(rows) >= limit:
            break
        for r in conn.execute(
                # The body's snippet, even when the hit was the title: the
                # title is shown anyway, the body is what was said.
                f"SELECT {_OBS_COLS}, snippet(observations_fts, 1, '«', "
                f"'»', '…', 20) AS snippet FROM observations_fts "
                f"JOIN observations o ON o.id = observations_fts.rowid "
                f"JOIN imports i ON i.id = o.import_id "
                f"WHERE observations_fts MATCH ? AND {w} AND {body_only} "
                f"ORDER BY {_OFF_PATH}, {order} LIMIT ?",
                [expr, *args, *TEXT_STREAMS, *QUIET_STREAMS,
                 f"body : ({expr})", limit]).fetchall():
            if r["id"] not in seen and len(rows) < limit:
                seen.add(r["id"])
                rows.append(r)
    return _obs_items(conn, cfg, rows, via=via)


def last_contact(conn: sqlite3.Connection, cfg: Config, family: list[int],
                 window: dict, today: date, *, streams: list[str] | None = None,
                 direction: str | None = None, sender_only: bool = False
                 ) -> list[dict]:
    """The newest text, call and calendar event the entity took part in —
    one of each at most, nothing dated after today. A missed (zero-second)
    call is an attempt, not contact. `streams` narrows it ('when did I last
    CALL Mom'), and so does `direction` ('when did I last text Jake').

    A text counts as contact with someone only when it passed between you
    and them: one they sent you, or one you sent them. A group message
    Jake sent, which Maya merely received, is not Maya texting you —
    `sender_only` ('when did Maya last text me') keeps only what they sent.
    Calls and events are left alone: an iPhone call names the other party
    as a plain participant."""
    items: list[dict] = []
    groups = [g for g in CONTACT_GROUPS
              if streams and set(STREAM_GROUPS[g]) & set(streams)] or CONTACT_GROUPS
    for group in groups:
        group_streams = STREAM_GROUPS[group]
        clause, fargs = _party_clause(family)
        if group == "text":
            sent, sargs = _party_clause(family, sender_only=True)
            if sender_only:
                clause, fargs = sent, sargs
            else:
                clause = f"((o.direction = 'out' AND {clause}) OR {sent})"
                fargs = [*fargs, *sargs]
        where = [f"o.stream IN ({','.join('?' for _ in group_streams)})",
                 "o.status = 'active'", "o.journal_date <= ?", clause]
        args = [*group_streams, today.isoformat(), *fargs]
        if group == "call":
            where.append("(o.value IS NULL OR o.value > 0)")
        if window.get("explicit"):
            where.append("o.journal_date BETWEEN ? AND ?")
            args += [window["start"], window["end"]]
        query = (f"SELECT {_OBS_COLS}, NULL AS snippet FROM observations o "
                 f"JOIN imports i ON i.id = o.import_id WHERE {{}} "
                 f"ORDER BY {_RECENT_FIRST} LIMIT 1")
        rows = []
        if direction and group != "calendar":
            rows = conn.execute(query.format(" AND ".join(where + ["o.direction = ?"])),
                                [*args, direction]).fetchall()
        # 'when did I last call Jake' when only Jake ever called: the call
        # is still the answer, and it says who called whom.
        if not rows and (not direction or group == "call"):
            rows = conn.execute(query.format(" AND ".join(where)), args).fetchall()
        items += _obs_items(conn, cfg, rows, via="contact")
    return items


def last_visit(conn: sqlite3.Connection, cfg: Config, family: list[int],
               window: dict, today: date) -> list[dict]:
    """The newest place visit and calendar event AT a place — through the
    place handles linked to it ('location' participants), one of each at
    most, nothing dated after today. A place's phone number (a restaurant's)
    is not a visit: calls and texts are never counted here."""
    items: list[dict] = []
    marks = ",".join("?" for _ in family)
    at = ("EXISTS (SELECT 1 FROM observation_participants p "
          "JOIN entity_handles h ON h.kind = p.handle_kind AND h.value = p.handle_value "
          f"WHERE p.observation_id = o.id AND p.role = 'location' AND h.entity_id IN ({marks}) "
          "AND h.confirmed_at IS NOT NULL AND h.ignored = 0)")
    for stream in ("place_visit", "calendar_event"):
        where = ["o.stream = ?", "o.status = 'active'", "o.journal_date <= ?", at]
        args = [stream, today.isoformat(), *family]
        if window.get("explicit"):
            where.append("o.journal_date BETWEEN ? AND ?")
            args += [window["start"], window["end"]]
        rows = conn.execute(
            f"SELECT {_OBS_COLS}, NULL AS snippet FROM observations o "
            f"JOIN imports i ON i.id = o.import_id WHERE {' AND '.join(where)} "
            f"ORDER BY {_RECENT_FIRST} LIMIT 1", args).fetchall()
        items += _obs_items(conn, cfg, rows, via="visit")
    return items


def contact_cards(conn: sqlite3.Connection, cfg: Config, family: list[int],
                  labels: list[str], limit: int = 2) -> list[dict]:
    """The entity's contact cards: linked through a handle on the card, or
    titled with the entity's exact name or alias."""
    where, args = [], []
    if family:
        clause, fargs = _party_clause(family)
        where.append(clause)
        args += fargs
    if labels:
        where.append(f"o.title COLLATE NOCASE IN ({','.join('?' for _ in labels)})")
        args += labels
    if not where:
        return []
    rows = conn.execute(
        f"SELECT {_OBS_COLS}, NULL AS snippet FROM observations o "
        f"JOIN imports i ON i.id = o.import_id WHERE o.stream = 'contact_card' "
        f"AND o.status = 'active' AND ({' OR '.join(where)}) "
        f"ORDER BY o.started_at DESC, o.id DESC LIMIT ?", [*args, limit]).fetchall()
    return _obs_items(conn, cfg, rows, via="card", cap=500)


def spending(conn: sqlite3.Connection, cfg: Config, *, terms: list[str],
             display: str, window: dict, limit: int = 10,
             groups: list[list[str]] = ()) -> tuple[list[dict], list[dict]]:
    """Transaction totals (stream txn: value = amount, negative = money out,
    unit = currency) — one M-item per currency, plus the transactions
    themselves as O-items.

    Named merchant terms search every record unless the question named a
    time (the text-search rule: 'how much did I spend at Veracruz' means all
    of it); a general 'how much did I spend' uses the window. `groups` are
    a named place's or organisation's name and aliases ('H-E-B Mueller',
    'H-E-B', 'HEB'): any one of them is that merchant. A question that named
    a merchant is never answered with everyone's total — no match is no
    data."""
    where = ["o.stream = 'txn'", "o.status = 'active'", "o.value IS NOT NULL"]
    args: list = []
    named = bool(terms or any(groups))
    all_records = named and not window["explicit"]
    if not all_records:
        where.append("o.journal_date BETWEEN ? AND ?")
        args += [window["start"], window["end"]]
    if named:
        source = ("FROM observations_fts JOIN observations o ON o.id = observations_fts.rowid "
                  "JOIN imports i ON i.id = o.import_id")
        where.insert(0, "observations_fts MATCH ?")
        args.insert(0, _match_expr(terms, groups))
    else:
        source = "FROM observations o JOIN imports i ON i.id = o.import_id"
    w = " AND ".join(where)
    totals = conn.execute(
        f"SELECT UPPER(COALESCE(o.unit, '')) AS cur, COUNT(*) AS n, "
        f"SUM(CASE WHEN o.value < 0 THEN 1 ELSE 0 END) AS n_spent, "
        f"SUM(CASE WHEN o.value > 0 THEN 1 ELSE 0 END) AS n_received, "
        f"SUM(CASE WHEN o.value < 0 THEN -o.value ELSE 0 END) AS spent, "
        f"SUM(CASE WHEN o.value > 0 THEN o.value ELSE 0 END) AS received, "
        f"MIN(CASE WHEN o.value < 0 THEN o.value END) AS largest, "
        f"MIN(o.journal_date) AS first, MAX(o.journal_date) AS last, "
        f"GROUP_CONCAT(DISTINCT i.source_file) AS sources "
        f"{source} WHERE {w} GROUP BY cur ORDER BY n DESC, cur", args).fetchall()
    if not totals:
        return [], []
    days: dict[str, list] = {}
    for r in conn.execute(
            f"SELECT UPPER(COALESCE(o.unit, '')) AS cur, o.journal_date AS day, "
            f"SUM(CASE WHEN o.value < 0 THEN -o.value ELSE 0 END) AS spent, "
            f"COUNT(*) AS n {source} WHERE {w} GROUP BY cur, day ORDER BY cur, day",
            args):
        days.setdefault(r["cur"], []).append(r)
    rows = conn.execute(
        f"SELECT {_OBS_COLS}, NULL AS snippet {source} WHERE {w} "
        f"ORDER BY {_RECENT_FIRST} LIMIT ?", [*args, limit]).fetchall()
    label = f'spending matching "{display}"' if named else "spending"
    metrics = []
    for t in totals:
        series = [{"date": d["day"], "value": round(d["spent"], 2), "records": d["n"]}
                  for d in days.get(t["cur"], [])]
        spent_days = [s for s in series if s["value"] > 0] or series
        hi = max(spent_days, key=lambda s: s["value"])
        lo = min(spent_days, key=lambda s: s["value"])
        span = ({"start": t["first"], "end": t["last"], "label": ALL_RECORDS}
                if all_records else {"start": window["start"], "end": window["end"],
                                     "label": window["label"]})
        metrics.append({
            "metric": "txn_spend", "label": label, "unit": t["cur"],
            "origin": "transactions, observed", "tier": "observed",
            "table": "observations", "count": t["n"],
            # 'You spent $X across N transaction(s)': N counts money out
            # only; a paycheck in the window is not a purchase.
            "spent_count": t["n_spent"], "received_count": t["n_received"],
            "days": len(spent_days), "total": round(t["spent"], 2),
            "received": round(t["received"], 2),
            "largest": round(-t["largest"], 2) if t["largest"] is not None else None,
            "mean": round(t["spent"] / len(spent_days), 2) if spent_days else 0,
            "min": lo["value"], "min_date": lo["date"],
            "max": hi["value"], "max_date": hi["date"],
            "latest": series[-1]["value"], "latest_date": series[-1]["date"],
            "per_day_agg": "sum", "series": series,
            "source_files": sorted(set((t["sources"] or "").split(",")) - {""}),
            "span": span, "terms": list(terms) + [g[0] for g in groups if g],
            "terms_display": display,
            "transactions": [],
            "trend_pct": None})
    return metrics, _obs_items(conn, cfg, rows, via="spending")


def _stream_counts(conn: sqlite3.Connection, streams: list[str]) -> dict[str, int]:
    marks = ",".join("?" for _ in streams)
    return {r["stream"]: r["n"] for r in conn.execute(
        f"SELECT stream, COUNT(*) AS n FROM observations WHERE status='active' "
        f"AND stream IN ({marks}) GROUP BY stream", streams)}


# ---------------------------------------------------------------- the bundle

class _Ids:
    """Citable items in first-seen order; the same record found two ways
    keeps one id."""

    def __init__(self, prefix: str):
        self.prefix, self.items, self.by_uuid = prefix, [], {}

    def add(self, item: dict) -> str:
        existing = self.by_uuid.get(item["uuid"])
        if existing is not None:
            # A search snippet shows WHY it matched; prefer it to an excerpt.
            if "«" in (item.get("snippet") or "") \
                    and "«" not in (existing.get("snippet") or ""):
                existing["snippet"] = item["snippet"]
            return existing["id"]
        item["id"] = f"{self.prefix}{len(self.items) + 1}"
        self.items.append(item)
        self.by_uuid[item["uuid"]] = item
        return item["id"]


def build_evidence(conn: sqlite3.Connection, cfg: Config, question: str, *,
                   today: date | None = None, days: int | None = None,
                   max_entries: int = 8, max_passages: int = 4,
                   max_observations: int | None = None) -> dict:
    """Everything the database can offer about this question, each item
    carrying a citable id and its provenance."""
    if days is not None and days < 1:
        raise ValueError(f"days must be at least 1 (got {days})")
    max_obs = (int(max_observations) if max_observations is not None
               else ask_setting(cfg, "max_observations", 6))
    today = today or local_today(cfg)
    observed = has_observed_tier(conn)

    words = _words(question)
    entities, ambiguous = _resolve_entities(conn, question, words)
    window = parse_window(
        question, cfg, today=today,
        default_days=days or ask_setting(cfg, "default_days", 30),
        names=frozenset(t for e in entities for t in e["tokens"]))
    health, journal = detect_metrics(question)
    # The window's own words said WHEN; the topic is what is left.
    topic_question = without_window_words(question, window)
    topic_words = words if topic_question == question else _words(topic_question)
    intent = parse_intent(question, words, entities, health, journal,
                          topic_words=topic_words)
    terms = intent["topic_terms"]
    # 'What's on my calendar this week', 'what do I need to do this month':
    # the rest of the period counts. Nothing else is dated in the future, so
    # metrics and entries are unchanged by it.
    if window.get("through") and ("calendar_event" in intent["streams"]
                                  or intent["loops"]):
        window = {**window, "end": window["through"]}
    # 'What's upcoming', 'what's next on my calendar', 'when is my next
    # appointment': from today as far ahead as the calendar import expands
    # repeating events, soonest first — never the last 30 days.
    if not window["explicit"] and intent["forward"] \
            and "calendar_event" in intent["streams"]:
        ahead = _calendar_horizon(cfg)
        window = {"start": today.isoformat(),
                  "end": (today + timedelta(days=ahead - 1)).isoformat(),
                  "label": f"the next {ahead} days", "explicit": True,
                  **({"ignored": window["ignored"]} if window.get("ignored") else {})}

    E, O = _Ids("E"), _Ids("O")
    metrics: list[dict] = []
    no_data: list[str] = []

    def missing_label(label: str, origin: str) -> str:
        # Several labels already carry their origin ("sleep (journal)") —
        # don't produce "sleep (journal) (journal)".
        return label if "(" in label else f"{label} ({origin})"

    # A named place or organisation in a money question is the merchant:
    # any of its names matches ('HEB' finds "H-E-B #742"), and its words
    # are not also required as plain terms.
    merchants = ([e for e in entities if e["type"] in ("place", "organization")]
                 if intent["money"] else [])
    merchant_keys = {t for e in merchants for t in e["tokens"]}
    groups = [sorted({_phrase(label) for label in _labels(conn, e)} - {""})
              for e in merchants]
    money_terms = [t for t in terms if _key(t) not in merchant_keys]
    money_display = (intent["topic_display"] or intent.get("merchant")
                     or ", ".join(e["matched"] for e in merchants))
    # A question that named a merchant is never answered with everyone's
    # total: when nothing of the name is left to search for, it is no data.
    searchable = bool(money_terms or any(groups))
    named_merchant = searchable or bool(intent.get("merchant"))

    # ---- money: transactions first, then what the journal says was spent
    if intent["money"]:
        if observed:
            found, txns = (spending(conn, cfg, terms=money_terms, groups=groups,
                                    display=money_display, window=window,
                                    limit=ask_setting(cfg, "max_transactions", 10))
                           if searchable or not named_merchant else ([], []))
            ids = [O.add(t) for t in txns]
            for m in found:
                m["transactions"] = [i for i, t in zip(ids, txns)
                                     if (t["unit"] or "").upper() == m["unit"]]
                metrics.append(m)
            if not found:
                what = (f'transactions matching "{money_display}"'
                        if named_merchant else "transactions")
                no_data.append(f"{what} ({ALL_RECORDS})"
                               if named_merchant and not window["explicit"] else what)
        else:
            no_data.append("transactions (no observed tier in this database)")

    for name in health:
        found = _metric_series(conn, table="health_records", metric=name,
                               spec=HEALTH_METRICS[name], window=window)
        (metrics.append(found) if found else no_data.append(
            missing_label(HEALTH_METRICS[name]["label"], "sensor import")))
    for name in journal:
        spec, match, span = JOURNAL_METRICS[name], None, None
        if name == "money" and named_merchant:
            spec = {**spec, "label": f'money (journal) matching "{money_display}"'}
            match = _match_expr(money_terms, groups)
            if not window["explicit"]:
                span = {"start": "0001-01-01", "end": "9999-12-31",
                        "label": ALL_RECORDS}
        found = (_metric_series(conn, table="measurements", metric=name, spec=spec,
                                window=window, entry_match=match, span=span)
                 if match or not (name == "money" and named_merchant) else None)
        answered_by = {m["metric"] for m in metrics if m["table"] != "measurements"}
        if found:
            metrics.append(found)
        elif not (spec.get("twin") in answered_by
                  or name == "money" and "txn_spend" in answered_by):
            # Spending answered from transactions, or steps from the sensor:
            # the journal not also stating it is not a gap worth reporting.
            no_data.append(missing_label(spec["label"], "journal")
                           + (f" ({ALL_RECORDS})" if span else ""))
    for i, m in enumerate(metrics, start=1):
        m["id"] = f"M{i}"

    # ---- open loops
    loops, loops_total, loops_open = [], 0, 0
    if intent["loops"]:
        named = [e["id"] for e in entities]
        rows, loops_total, loops_open = open_loops(
            conn, window, entity_ids=named or None,
            limit=ask_setting(cfg, "max_loops", 10), due_only=intent["due"])
        loops = [E.add(r) for r in rows]

    # ---- the people and places it names
    # A question narrowed to calls or texts is answered by calls or texts:
    # 'Tacos at Veracruz with Jake' is not when Jake last called. A journal
    # entry still counts when it says it was one ('Called Mom on the way').
    channel = set(intent["streams"]) & set(TEXT_STREAMS + CALL_STREAMS)
    channel_words = ([w for streams, ws in _CHANNEL_WORDS if channel & set(streams)
                      for w in ws] if channel else [])
    # A place's visits answer a question about the place, unless it asked
    # for another kind of record ('what did I ask Claude about Bled').
    visits_wanted = (tuple(s for s in _VISIT_STREAMS if s in intent["streams"])
                     if intent["streams"] else _VISIT_STREAMS)
    for ent in entities:
        ent_entries = _entity_entries(conn, ent["id"], window)
        ent["entries"] = [{"uuid": e["uuid"], "date": e["date"], "title": e["title"],
                           "kind": e["kind"]} for e in ent_entries]
        ent["facts"] = [E.add(e) for e in ent_entries]
        ent["relationships"] = [{"text": r["text"], "type": r["type"],
                                 "other": r["other"], "id": E.add(r["entry"])}
                                for r in _relationships(conn, ent["id"])]
        ent["handles_linked"], ent["cards"], ent["hits"] = 0, [], []
        if ent["type"] != "person":
            ent["visits"] = []
        contact = [E.add(e) for e in _last_mention(
            conn, ent["id"], window, today, participant_only=ent["type"] == "person",
            match=_fts_expr(channel_words) if channel else None)]
        if observed:
            family = _entity_family(conn, ent["id"])
            handles = _linked_handles(conn, family)
            ent["handles_linked"] = len(handles)
        if observed and ent["type"] == "person":
            ent["cards"] = [O.add(c) for c in contact_cards(
                conn, cfg, family, _labels(conn, ent))]
            if handles:
                contact += [O.add(o) for o in last_contact(
                    conn, cfg, family, window, today,
                    streams=intent["streams"] or None, direction=intent["direction"],
                    sender_only=ent["id"] in intent["senders"])]
                if (terms or intent["streams"]) and not intent["metric_only"] \
                        and not intent["money"]:
                    ent["hits"] = [O.add(o) for o in search_observations(
                        conn, cfg, terms=terms, window=window,
                        streams=intent["streams"] or None,
                        direction=intent["direction"], family=family,
                        sender_only=ent["id"] in intent["senders"],
                        limit=max_obs, recency=intent["recency"])]
        elif observed and handles and visits_wanted:
            # A place's linked handles are its visits: every Timeline visit
            # and calendar event held there, past and future, is attributed
            # once the place_id is linked. Only those two streams — a
            # restaurant's linked phone number makes no call a visit.
            contact += [O.add(o) for o in last_visit(conn, cfg, family, window, today)]
            if not intent["metric_only"] and not intent["money"]:
                ent["visits"] = [O.add(o) for o in search_observations(
                    conn, cfg, window=window, streams=visits_wanted, family=family,
                    limit=max_obs, recency=True)]
        ent["last_contact"] = _latest_contact(contact, E, O, cfg)

    # ---- full-text: entries, transcripts, and the observed tier
    entry_drop = set(INTENT_WORDS) | (LOOP_WORDS if intent["loops"] else set()) \
        | (set(MONEY_WORDS) if intent["money"] else RECAP_WORDS) \
        | (TIME_WORDS - {t for e in entities for t in e["tokens"]})
    match = fts_query(topic_question, drop=entry_drop)
    entry_hits = ([E.add(e) for e in search_entries(conn, match, window, max_entries)]
                  if match else [])
    passages = search_passages(conn, match, window, max_passages) if match else []
    for i, p in enumerate(passages, start=1):
        p["id"] = f"P{i}"

    # A named person with linked handles was searched through them above;
    # money questions are answered by the transactions themselves.
    obs_hits: list[str] = []
    person_filtered = any(e["handles_linked"] for e in entities
                          if e["type"] == "person")
    # A window reaching past today ('next week', the rest of 'this week')
    # lists what is nearest today first, not what is furthest away.
    around = today.isoformat() if window["end"] > today.isoformat() else None
    if observed and not intent["metric_only"] and not intent["money"] \
            and not person_filtered and (terms or intent["streams"]):
        obs_hits = [O.add(o) for o in search_observations(
            conn, cfg, terms=terms, window=window,
            streams=intent["streams"] or None, direction=intent["direction"],
            limit=max_obs, recency=intent["recency"], around=around)]
    elif observed and intent["loops"] and window["explicit"] and not terms \
            and not intent["due"]:
        # 'What are my plans this weekend': what the calendar holds then is
        # part of the answer, next to the open tasks.
        obs_hits = [O.add(o) for o in search_observations(
            conn, cfg, window=window, streams=("calendar_event",),
            limit=max_obs, around=around)]

    # ---- a recap: 'what did I do yesterday', 'how was last weekend'
    # A named time and nothing else to narrow it — no topic, person, metric,
    # record kind, loop or money — asks what that time holds. Its entries
    # (and a few of its records) are the answer, cited; without this the
    # question found nothing although the day was logged.
    recap = bool(window["explicit"] and not match and not terms and not entities
                 and not ambiguous and not health and not journal
                 and not intent["streams"] and not intent["loops"]
                 and not intent["money"])
    if recap:
        rows = conn.execute(
            f"SELECT {_ENTRY_COLS} FROM entries e JOIN entry_kinds k ON k.id = e.kind_id "
            f"WHERE e.status='active' AND e.journal_date BETWEEN ? AND ? "
            f"ORDER BY e.journal_date DESC, COALESCE(e.occurred_at, '') DESC, e.id DESC "
            f"LIMIT ?", (window["start"], window["end"], max_entries)).fetchall()
        entry_hits = [E.add(e) for e in _entry_items(conn, rows[::-1], "window")]
        if observed:
            obs_hits = [O.add(o) for o in search_observations(
                conn, cfg, window=window, streams=_RECAP_STREAMS, limit=max_obs,
                around=around)]
    if observed and intent["streams"]:
        counts = _stream_counts(conn, intent["streams"])
        for label, streams in STREAM_GROUPS.items():
            wanted = [s for s in streams if s in intent["streams"]]
            if wanted and not any(counts.get(s) for s in wanted):
                no_data.append(f"{label} records ({', '.join(wanted)}): none imported")

    covered = conn.execute(
        "SELECT COUNT(DISTINCT journal_date) AS c FROM entries "
        "WHERE status='active' AND journal_date BETWEEN ? AND ?",
        (window["start"], window["end"])).fetchone()["c"]

    has_evidence = bool(metrics or E.items or passages or O.items)
    asked_for_numbers = bool(health or journal or intent["money"])
    return {"question": question, "window": window, "metrics": metrics,
            "entries": E.items, "passages": passages, "observations": O.items,
            "entities": entities, "ambiguous": ambiguous, "no_data": no_data,
            "match": match, "intent": intent, "loops": loops,
            "loops_total": loops_total, "loops_open": loops_open,
            "obs_hits": obs_hits, "entry_hits": entry_hits, "recap": recap,
            "observed_tier": observed,
            "days_with_entries_in_window": covered,
            "has_evidence": has_evidence,
            # 'answered' feeds recall_log, the build-this-next list: a
            # question that asked for numbers and got only loose text
            # matches was not answered.
            "answered": has_evidence and (not asked_for_numbers or bool(metrics))}


def _sort_key(item: dict, cfg: Config) -> tuple:
    if item["tier"] == "observed":
        return (item["date"], item.get("local_time") or "", 1)
    t = ""
    if item.get("occurred_at") and "T" in item["occurred_at"]:
        try:
            dt = datetime.fromisoformat(item["occurred_at"].replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(ZoneInfo(cfg.timezone))
            t = dt.strftime("%H:%M")
        except ValueError:
            t = ""
    return (item["date"], t, 0)


def _latest_contact(ids: list[str], E: _Ids, O: _Ids, cfg: Config) -> dict | None:
    """Of the newest text, call, calendar event and journal mention, which
    came last. On the same minute a record with an exact timestamp wins."""
    if not ids:
        return None
    pool = {i: (O.items if i.startswith("O") else E.items)[int(i[1:]) - 1] for i in ids}
    ordered = sorted(ids, key=lambda i: _sort_key(pool[i], cfg), reverse=True)
    return {"latest": ordered[0], "items": ordered,
            "date": pool[ordered[0]]["date"]}


# ---------------------------------------------------------------- rendering

def fmt_value(value, unit: str) -> str:
    """Number plus unit, readable: counts get thousands separators, ratings
    read as 7/10, everything else keeps significant digits."""
    if value is None:
        return "-"
    text = f"{value:,.0f}" if unit in ("steps", "ml", "kcal") else f"{value:g}"
    return f"{text} {unit}".strip() if unit and not unit.startswith("/") \
        else f"{text}{unit}"


_CURRENCY = {"USD": "$", "EUR": "€", "GBP": "£"}


def fmt_money(value, unit: str | None, signed: bool = False) -> str:
    if value is None:
        return "-"
    cur = (unit or "").upper()
    sign = "-" if signed and value < 0 else ""
    amount = f"{abs(value):,.2f}"
    return f"{sign}{_CURRENCY[cur]}{amount}" if cur in _CURRENCY \
        else f"{sign}{amount} {cur}".strip()


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} s"
    minutes = int(seconds / 60 + 0.5)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _obs_value(o: dict) -> str | None:
    """The record's own quantity, rendered the way an answer would say it."""
    d, v = o["detail"], o["value"]
    if o["kind"] == "call":
        if v is not None and v <= 0 or d.get("missed") or d.get("type") == "missed":
            return "missed"
        return _duration(v) if v is not None else None
    if o["kind"] == "transaction":
        return fmt_money(v, o["unit"], signed=True) if v is not None else None
    if o["kind"] in ("photo", "place visit"):
        lat = d.get("lat", d.get("latitude"))
        lon = d.get("lon", d.get("lng", d.get("longitude")))
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return f"at {lat:.4f}, {lon:.4f}"
        return None
    if o["kind"] == "calendar":
        loc = d.get("location")
        return f"at {loc}" if loc else None
    if v is not None and o["kind"] not in ("text", "AI chat", "contact card", "note"):
        return f"{v:g} {o['unit'] or ''}".strip()
    return None


def _obs_line(o: dict) -> str:
    head = f"[{o['id']}] {o['date']}"
    if o["local_time"] and o["kind"] not in _UNTIMED:
        head += f" {o['local_time']}"
    bits = [head, o["kind"] if o["kind"] == o["stream"] else f"{o['kind']} ({o['stream']})"]
    if o["who"]:
        bits.append(o["who"])
    if o["title"]:
        bits.append(f'in "{o["title"]}"' if o["kind"] in ("text", "AI chat")
                    else f'"{o["title"]}"')
    extra = _obs_value(o)
    if extra:
        bits.append(extra)
    card = _card_text(o)
    if card:
        bits.append(card)
    line = " ".join(bits)
    if o["snippet"] and o["kind"] != "call":
        line += f": {o['snippet']}"
    return line + " (observed, not confirmed)"


def _card_text(o: dict) -> str | None:
    """A contact card's numbers and addresses, as one clause."""
    card = o.get("card") or {}
    bits = [f"phone {p}" for p in card.get("phones", ())]
    bits += [f"email {e}" for e in card.get("emails", ())]
    return "; ".join(bits) or None


def _metric_line(m: dict) -> str:
    if m["metric"] == "txn_spend":
        span = m["span"]
        line = (f"[{m['id']}] {m['label']} — {m['count']} transaction(s) "
                f"({m['spent_count']} out, {m['received_count']} in), "
                f"{span['label']} ({span['start']} to {span['end']}): "
                f"total spent {fmt_money(m['total'], m['unit'])}")
        if m["received"]:
            line += f", received {fmt_money(m['received'], m['unit'])}"
        if m["largest"]:
            line += f"; largest {fmt_money(m['largest'], m['unit'])}"
        line += f" (source: {m['origin']})"
        if m["transactions"]:
            line += f"; listed: {', '.join(m['transactions'])}"
        return line
    agg = "per-day total" if m["per_day_agg"] == "sum" else "per-day average"
    line = (f"[{m['id']}] {m['label']} — {m['days']} day(s), {agg}: "
            f"mean {fmt_value(m['mean'], m['unit'])}, "
            f"min {fmt_value(m['min'], m['unit'])} on {m['min_date']}, "
            f"max {fmt_value(m['max'], m['unit'])} on {m['max_date']}, "
            f"latest {fmt_value(m['latest'], m['unit'])} on {m['latest_date']}")
    if m["per_day_agg"] == "sum":
        line += f", total {fmt_value(m['total'], m['unit'])}"
    if m.get("trend_pct"):
        line += (f", trend {'up' if m['trend_pct'] > 0 else 'down'} "
                 f"{abs(m['trend_pct'])}% (second half vs first)")
    if m.get("span"):
        line += f", over {m['span']['label']}"
    line += f" (source: {m['origin']})"
    if m.get("multi_origin_days"):
        line += (f"; {len(m['origins'])} apps recorded it on {m['multi_origin_days']} "
                 f"day(s), each day counts one app, never both")
    return line


def _entry_line(e: dict) -> str:
    cats = f" ({', '.join(e['categories'])})" if e["categories"] else ""
    line = f"[{e['id']}] {e['date']} {e['kind']}{cats}: {_clean(e['title'], 160)}"
    if e.get("snippet"):
        line += f" — {_clean(e['snippet'], 200)}"
    if e["kind"] in ("task", "plan"):
        if e.get("closure"):
            line += f" (closed {e['closure']} {str(e['closed_at'])[:10]})"
        else:
            line += " (open" + (f", due {str(e['due_at'])[:10]}" if e.get("due_at") else "") + ")"
    return line


def _window_line(bundle: dict) -> str:
    w = bundle["window"]
    line = (f"WINDOW: {w['label']} ({w['start']} to {w['end']}); "
            f"{bundle['days_with_entries_in_window']} day(s) with entries")
    if w.get("ignored"):
        line += "; ignored: " + "; ".join(w["ignored"])
    return line


def _evidence_lines(bundle: dict) -> tuple[list[str], dict[str, str]]:
    """(context lines, {id: line}). Context lines carry no id: the window,
    entity summaries, gaps."""
    context = [_window_line(bundle)]
    by_id: dict[str, str] = {}
    for m in bundle["metrics"]:
        by_id[m["id"]] = _metric_line(m)
    for e in bundle["entries"]:
        by_id[e["id"]] = _entry_line(e)
    for p in bundle["passages"]:
        by_id[p["id"]] = (f"[{p['id']}] {p['date']} transcript (source "
                          f"{p['source_id']}): {_clean(p['snippet'], 300)}")
    for o in bundle.get("observations", []):
        by_id[o["id"]] = _obs_line(o)
    if bundle.get("loops") is not None and bundle.get("intent", {}).get("loops"):
        # 'In total' must be the real total: a person or a window that
        # filters every loop out does not mean nothing is open.
        open_all = bundle.get("loops_open", bundle["loops_total"])
        line = (f"OPEN LOOPS: {bundle['loops_total']} open task(s)/plan(s) in total"
                if open_all == bundle["loops_total"] else
                f"OPEN LOOPS: {bundle['loops_total']} open task(s)/plan(s) match the "
                f"question, {open_all} open in total")
        context.append(f"{line}; listed: {', '.join(bundle['loops']) or 'none'}")
    for ent in bundle["entities"]:
        bits = [f"ENTITY: {ent['name']} ({ent['type']}), named as \"{ent['matched']}\" "
                f"by {ent['via']}"]
        if ent.get("handles_linked"):
            bits.append(f"{ent['handles_linked']} linked handle(s)")
        lc = ent.get("last_contact")
        if lc:
            what = ("last contact" if ent["type"] == "person"
                    else "last visit" if lc["latest"].startswith("O") else "last mentioned")
            bits.append(f"{what} {lc['latest']} ({lc['date']})")
        if ent.get("cards"):
            bits.append(f"contact card {', '.join(ent['cards'])}")
        if ent.get("facts"):
            bits.append(f"entries {', '.join(ent['facts'])}")
        context.append("; ".join(bits))
    for amb in bundle.get("ambiguous", []):
        context.append(f"AMBIGUOUS: \"{amb['matched']}\" fits "
                       + ", ".join(c["name"] for c in amb["candidates"])
                       + " — not used to narrow the search")
    for missing in bundle["no_data"]:
        if "none imported" in missing:
            context.append(f"NO DATA: {missing}")
        elif missing.endswith(f"({ALL_RECORDS})"):
            context.append(f"NO DATA: {missing}: nothing recorded at any date")
        else:
            context.append(f"NO DATA: {missing}: nothing recorded in this window")
    if not bundle["has_evidence"]:
        context.append("NO DATA: nothing in the database matches this question")
    return context, by_id


def render_evidence(bundle: dict, max_series: int = 14) -> str:
    """The evidence as compact text: what a model is allowed to use, and what
    a person sees when there is no model."""
    context, by_id = _evidence_lines(bundle)
    lines = [context[0]]
    for m in bundle["metrics"]:
        lines.append(by_id[m["id"]])
        series = m["series"][-max_series:]
        fmt = (lambda v: fmt_money(v, m["unit"])) if m["metric"] == "txn_spend" \
            else (lambda v: fmt_value(v, m["unit"]))
        lines.append("     " + "; ".join(f"{s['date']} {fmt(s['value'])}" for s in series))
    for key in ("entries", "passages", "observations"):
        for item in bundle.get(key, []):
            lines.append(by_id[item["id"]])
    lines += context[1:]
    return "\n".join(lines)


def _obs_phrase(o: dict) -> str:
    """One observed record as an answer would say it — what the record says,
    attributed, never restated as fact."""
    kind, ex = o["kind"], _plain(o["snippet"])
    others = _names(_people(o))
    senders = _names([p for p in _people(o) if p["role"] in ("sender", "organizer")])
    if kind == "AI chat":
        bot = AI_NAMES.get(o["stream"], "the assistant")
        verb = f"you asked {bot}" if o["direction"] == "out" else f"{bot} answered"
        title = f' (in "{o["title"]}")' if o["title"] else ""
        return f'{verb}{title}: "{ex}"'
    if kind == "text":
        who = (f"you texted {others or 'someone'}" if o["direction"] == "out"
               else f"{senders or others or 'someone'} texted you")
        group = f' in "{o["title"]}"' if o["title"] else ""
        return f'{who}{group}: "{ex}"'
    if kind == "call":
        dur = _obs_value(o)
        if dur == "missed":
            return (f"missed call from {others or 'someone'}" if o["direction"] != "out"
                    else f"unanswered call to {others or 'someone'}")
        verb = (f"you called {others or 'someone'}" if o["direction"] == "out"
                else f"{others or 'someone'} called you")
        return verb + (f" ({dur})" if dur else "")
    if kind == "calendar":
        loc = _obs_value(o)
        return (f'calendar "{o["title"] or "event"}"'
                + (f" with {others}" if others else "") + (f" {loc}" if loc else ""))
    if kind == "transaction":
        return f'transaction "{o["title"] or "?"}" {_obs_value(o) or ""}'.strip()
    if kind == "place visit":
        # The linked place's name; else a Timeline label ('Home'), never
        # its "Place at <lat>, <lon>" placeholder next to the same numbers.
        title = o["title"] if o["title"] and not o["title"].startswith("Place at ") else None
        where = _names(_places(o)) or (f'"{title}"' if title else "")
        coords = (_obs_value(o) or "").removeprefix("at ")
        return ("place visit" + (f" at {where}" if where else "")
                + (f" ({coords})" if coords else ""))
    extra = _obs_value(o)
    text = f'{kind} "{o["title"]}"' if o["title"] else kind
    if extra:
        text += f" {extra}"
    return text + (f': "{ex}"' if ex else "")


def _entry_phrase(e: dict) -> str:
    return f'journal entry "{e["title"]}"'


def _contact_kind(intent: dict) -> str:
    """What 'nothing recorded shows ...' was looking for: a call, a text,
    or any contact."""
    streams = set(intent.get("streams") or ())
    calls, texts = streams & set(CALL_STREAMS), streams & set(TEXT_STREAMS)
    if calls and not texts:
        return "a call with"
    if texts and not calls:
        return "a text with"
    return "contact with"


def deterministic_answer(bundle: dict) -> str:
    """A useful answer with no model involved: the statistics and records,
    stated plainly, each pointing at its evidence id. Everything numeric in
    it appears in the evidence it cites — it passes its own citation check."""
    w = bundle["window"]
    intent = bundle.get("intent") or {}
    items = {x["id"]: x for key in ("metrics", "entries", "passages", "observations")
             for x in bundle.get(key, [])}
    used: set[str] = set()
    out: list[str] = []

    def phrase(i: str) -> str:
        used.add(i)
        x = items[i]
        return _obs_phrase(x) if i.startswith("O") else _entry_phrase(x)

    for m in bundle["metrics"]:
        used.add(m["id"])
        if m["metric"] == "txn_spend":
            span = m["span"]
            spent_n = m.get("spent_count", m["count"])
            text = (f"You spent {fmt_money(m['total'], m['unit'])} across "
                    f"{spent_n} transaction(s)"
                    + (f' matching "{m["terms_display"]}"' if m["terms"] else "")
                    + f" ({span['label']}, {span['start']} to {span['end']})")
            if m["received"]:
                text += (f" and received {fmt_money(m['received'], m['unit'])}"
                         f" in {m.get('received_count', 0)} transaction(s)")
            text += f" [{m['id']}]"
            listed = m["transactions"][:5]
            if listed:
                text += ": " + "; ".join(
                    f"{items[i]['date']} {items[i]['title'] or ''} "
                    f"{fmt_money(items[i]['value'], items[i]['unit'], signed=True)} [{i}]"
                    for i in listed)
                used.update(listed)
            out.append(text + ".")
            continue
        trend = ""
        if m.get("trend_pct"):
            trend = (f", trending {'up' if m['trend_pct'] > 0 else 'down'} "
                     f"{abs(m['trend_pct'])}% across the window")
        word = "totalled" if m["per_day_agg"] == "sum" else "averaged"
        # Only the first letter: 'matching "H-E-B"' keeps the merchant's case.
        out.append(f"{m['label'][:1].upper()}{m['label'][1:]} {word} "
                   f"{fmt_value(m['mean'], m['unit'])} per day over {m['days']} "
                   f"day(s) (min {fmt_value(m['min'], m['unit'])} {m['min_date']}, "
                   f"max {fmt_value(m['max'], m['unit'])} {m['max_date']}){trend} "
                   f"[{m['id']}].")

    if intent.get("loops"):
        listed = [i for i in bundle.get("loops", [])]
        open_all = bundle.get("loops_open", bundle.get("loops_total", 0))
        due = intent.get("due")
        if listed:
            used.update(listed)
            head = (f"Open tasks and plans due by {w['end']}: " if due and w.get("explicit")
                    else "Open tasks and plans with a due date: " if due
                    else "Open tasks and plans: ")
            out.append(head + "; ".join(
                f"{items[i]['date']} {items[i]['title']} ({items[i]['kind']}"
                + (f", due {str(items[i]['due_at'])[:10]}" if items[i].get("due_at") else "")
                + f") [{i}]" for i in listed) + ".")
        elif open_all:
            # Something is open; the filter is what left nothing — say which.
            named = [e["name"] for e in bundle["entities"]]
            reason = " and ".join(([f"mention {' or '.join(named)}"] if named else [])
                                  + (["are due"] if due else []))
            why = (f" {reason}" if reason else "") \
                + (f" for {w['label']}" if w.get("explicit") else "")
            out.append(f"No open tasks or plans{why} ({open_all} open in total).")
        else:
            out.append("You have no open tasks or plans.")

    later: list[str] = []
    wants_numbers = bool(_NUMBERS_RE.search(bundle.get("question") or ""))
    for ent in bundle["entities"]:
        name, lc = ent["name"], ent.get("last_contact")
        if ent["type"] != "person":
            if intent.get("recency") and lc:
                latest = lc["latest"]
                lead = (f"Last visit to {name} on record: {lc['date']}, "
                        if latest.startswith("O") else
                        f"You last mentioned {name} on {lc['date']}: ")
                out.append(f"{lead}{phrase(latest)} [{latest}].")
                before = [i for i in lc["items"] if i != latest]
                if before:
                    out.append("Before that: " + "; ".join(
                        f"{items[i]['date']} {phrase(i)} [{i}]" for i in before) + ".")
            continue
        no_hits = not ent.get("hits")
        # 'When did Jake call', 'did Jake call me this week', and 'what did
        # Jake text me over the weekend' when he texted nothing then: the
        # contact leads, or that there was none — never their card.
        channel_asked = bool(set(intent.get("streams") or ())
                             & set(TEXT_STREAMS + CALL_STREAMS))
        want_history = intent.get("recency") or intent.get("contact")
        want_contact = want_history \
            or (channel_asked and no_hits and not intent.get("profile"))
        want_profile = intent.get("profile")
        fallback = no_hits and not want_contact and not want_profile
        sentences = []
        if want_contact or fallback:
            if lc:
                sentences.append(f"Last contact with {name}: {lc['date']}, "
                                 f"{phrase(lc['latest'])} [{lc['latest']}].")
                before = [i for i in lc["items"] if i != lc["latest"]]
                if before and want_history:
                    sentences.append("Before that: " + "; ".join(
                        f"{items[i]['date']} {phrase(i)} [{i}]" for i in before) + ".")
            elif want_contact:
                sentences.append(f"Nothing recorded shows {_contact_kind(intent)} {name}"
                                 + (f" for {w['label']}" if w.get("explicit") else "")
                                 + ".")
        if want_profile or fallback:
            # A stated relationship answers "where is X from / where does X
            # live" directly, so it leads there; otherwise it follows the card.
            rels = ent.get("relationships", [])
            asks_origin = re.search(r"\b(from|grew up|live[sd]?|hometown|works?)\b",
                                    bundle.get("question") or "", re.I)
            rel_line = ("Relationships: " + "; ".join(
                f"{r['text']} [{r['id']}]" for r in rels) + ".") if rels else None
            used.update(r["id"] for r in rels)
            if rel_line and asks_origin:
                sentences.append(rel_line)
            for c in ent.get("cards", []):
                used.add(c)
                numbers = _card_text(items[c]) if wants_numbers else None
                sentences.append(f"{name}'s contact card: "
                                 + (f"{numbers}; " if numbers else "")
                                 + f'"{_plain(items[c]["snippet"], 300)}" [{c}].')
            if rel_line and not asks_origin:
                sentences.append(rel_line)
            facts = [i for i in ent.get("facts", []) if i not in used][:4]
            if facts:
                used.update(facts)
                sentences.append(f"Recorded about {name}: " + "; ".join(
                    f"{items[i]['date']} {items[i]['title']} [{i}]" for i in facts) + ".")
        (later if fallback else out).extend(sentences)

    # What a named person sent or received first, then a named place's
    # visits, then whatever else the search found.
    hits = [i for e in bundle["entities"] for i in e.get("hits", [])] \
        + [i for e in bundle["entities"] for i in e.get("visits", [])] \
        + bundle.get("obs_hits", [])
    hits = [i for i in dict.fromkeys(hits) if i not in used][:4]
    observed = []
    if hits:
        observed.append("Imported records (observed, not confirmed): " + "; ".join(
            f"{items[i]['date']} {phrase(i)} [{i}]" for i in hits) + ".")

    # Text-search hits in rank order first, then whatever else was gathered.
    order = list(dict.fromkeys(bundle.get("entry_hits", []) + list(items)))
    recap = bundle.get("recap")
    related = [items[i] for i in order if i.startswith("E") and i not in used][
        :8 if recap else 5]
    confirmed = []
    if related:
        used.update(e["id"] for e in related)
        confirmed.append((f"Journal entries for {w['label']}: " if recap
                          else "Related entries: ") + "; ".join(
            f"{e['date']} {e['title']} [{e['id']}]" for e in related) + ".")
    # What the owner confirmed leads — unless the question asked about the
    # records themselves ('what did I ask Claude', 'what did Jake text me').
    asked_for_records = bool(intent.get("streams") or intent.get("direction"))
    out.extend(observed + confirmed if asked_for_records else confirmed + observed)
    out.extend(later)
    if bundle["passages"] and not bundle["entries"]:
        out.append("Mentioned in transcripts: " + "; ".join(
            f"{p['date']} [{p['id']}]" for p in bundle["passages"]) + ".")
    for ent in bundle["entities"]:
        if not (ent.get("facts") or ent.get("cards") or ent.get("last_contact")
                or ent.get("hits") or ent.get("visits")):
            out.append(f"{ent['name']} is known, but nothing recorded mentions them.")
    for amb in bundle.get("ambiguous", []):
        out.append(f"\"{amb['matched']}\" could be "
                   + " or ".join(c["name"] for c in amb["candidates"])
                   + " — name them in full to narrow it down.")
    if bundle["no_data"]:
        # One keyword can fire many metrics; naming every empty one buries the
        # answer. The full list stays in the evidence and in --json.
        anytime = [x for x in bundle["no_data"] if x.endswith(f"({ALL_RECORDS})")]
        windowed = [x for x in bundle["no_data"] if x not in anytime]
        parts = []
        if windowed:
            shown = windowed[:3]
            parts.append(", ".join(shown)
                         + (", and more (see the evidence)" if len(windowed) > 3 else "")
                         + f" in {w['label']}")
        parts += anytime
        out.append("No data for: " + "; ".join(parts) + ".")
    if w.get("ignored"):
        out.append("Ignored: " + "; ".join(w["ignored"]) + ".")
    if not out or (not bundle["has_evidence"] and not bundle["no_data"]
                   and not intent.get("loops")):
        out.append(f"Nothing in the database matches that question for "
                   f"{w['label']} ({w['start']} to {w['end']}). "
                   f"{bundle['days_with_entries_in_window']} day(s) in that "
                   f"window have entries at all.")
    return " ".join(out)


# ---------------------------------------------------------------- citations

CITE_RE = re.compile(r"\[([MEPO]\d+)\]")
# Anything bracketed that is shaped like an id counts — [m1], [M1, E2] and
# an invented [X2] included — so a citation can never slip past the check
# by being spelled differently.
_BRACKET_RE = re.compile(r"\[([^\[\]\n]{1,60})\]")
_ID_SHAPE = re.compile(r"[A-Za-z]{1,3}\d{1,5}")
_ID_REF = re.compile(r"\b[MEPO]\d+\b")

_F_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTH_ALT = "|".join(_MONTH_WORDS)
_F_MD = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}})\b)?",
                   re.IGNORECASE)
_F_DM = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_ALT})\b\.?(?:,?\s+(\d{{4}})\b)?",
                   re.IGNORECASE)
_F_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?::\d{2})?(?![\d:])(?:\s*([ap])\.?m\b\.?)?",
                     re.IGNORECASE)
_F_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)")
_F_WORDNUM = re.compile(r"\b(" + "|".join(
    ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
     "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
     "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "hundred"])
    + r")\b", re.IGNORECASE)
_WORDNUM_VALUES = {w: v for v, w in enumerate(
    ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
     "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
     "seventeen", "eighteen", "nineteen"])} | {"twenty": 20, "thirty": 30,
                                               "forty": 40, "fifty": 50,
                                               "hundred": 100}


def _cited(text: str) -> list[str]:
    out = []
    for m in _BRACKET_RE.finditer(text):
        for part in re.split(r"[,;/\s]+", m.group(1).strip()):
            if _ID_SHAPE.fullmatch(part):
                out.append(part.upper())
    return out


def _month_number(token: str) -> int:
    t = token.lower()
    return MONTHS.get(t) or MONTH_ABBR[t]


def _facts(text: str, *, evidence: bool = False) -> dict:
    """The checkable content of a text: full dates, month-days, clock times
    and plain numbers (dates and times are taken out first, so a day-of-month
    never passes for a quantity)."""
    dates, md, times, nums = set(), set(), set(), []
    # A search snippet's highlight ('«March» 14') is no part of the date.
    t = text.replace("«", "").replace("»", "")
    for m in _F_ISO.finditer(t):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        dates.add((y, mo, d))
        md.add((mo, d))
        nums.append((float(y), 0))
    t = _F_ISO.sub(" ", t)
    for rx, mi, di in ((_F_MD, 1, 2), (_F_DM, 2, 1)):
        for m in rx.finditer(t):
            md.add((_month_number(m.group(mi)), int(m.group(di))))
            if m.group(3):
                nums.append((float(m.group(3)), 0))
        t = rx.sub(" ", t)
    for m in _F_TIME.finditer(t):
        times.add((int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()))
    t = _F_TIME.sub(" ", t)
    for m in _F_NUM.finditer(t):
        s = m.group(1).replace(",", "")
        if s.endswith("."):
            s = s[:-1]
        try:
            nums.append((float(s), len(s.split(".")[1]) if "." in s else 0))
        except ValueError:
            continue
    if evidence:
        for m in _F_WORDNUM.finditer(t):
            nums.append((float(_WORDNUM_VALUES[m.group(1).lower()]), 0))
    return {"dates": dates, "md": md, "times": times, "nums": nums}


def _merge(facts: list[dict]) -> dict:
    out = {"dates": set(), "md": set(), "times": set(), "nums": set()}
    for f in facts:
        out["dates"] |= f["dates"]
        out["md"] |= f["md"]
        out["times"] |= {(h, m) for h, m, _ in f["times"]}
        out["nums"] |= {abs(v) for v, _ in f["nums"]}
    return out


def _num_ok(value: float, decimals: int, pool: set[float]) -> bool:
    scale = 10 ** decimals
    for e in pool:
        if abs(e - value) < 1e-9:
            return True
        # 7.19 may be said as 7.2 or 7 — rounded or cut to the stated
        # precision — but never as 9.
        if abs(math.floor(e * scale + 0.5) / scale - value) < 1e-9 \
                or abs(math.floor(e * scale) / scale - value) < 1e-9:
            return True
    return False


def _supported(f: dict, pool: dict) -> bool:
    if any(d not in pool["dates"] for d in f["dates"]):
        return False
    if any(md not in pool["md"] for md in f["md"]):
        return False
    for h, mi, ampm in f["times"]:
        cands = {(h, mi)}
        if ampm == "p" and h < 12:
            cands = {(h + 12, mi)}
        elif ampm == "a" and h == 12:
            cands = {(0, mi)}
        elif not ampm and h < 12:
            cands.add((h + 12, mi))
        if not cands & pool["times"]:
            return False
    return all(_num_ok(abs(v), dec, pool["nums"]) for v, dec in f["nums"])


# A period after these ends a word, not a sentence: 'you saw Dr. Ellis on
# 2026-10-10' split at 'Dr.' left the date in a fragment with no citation.
# Not 'etc', which usually does end one. Nor does an initial ('Laura J.
# Kim', 'Dr. R. Ellis') or a dotted abbreviation ('D.D.S.', 'Ph.D.',
# 'U.S.'): names reach answers verbatim from contact cards and `handles link
# --new`, and the answer about Laura J. Kim failed its own check.
_ABBREVIATIONS = _HONORIFICS | {"st", "jr", "sr", "vs", "e.g", "i.e", "a.m", "p.m"}
_ABBREV_BEFORE = re.compile(r"(?<![A-Za-z])([A-Za-z][A-Za-z.]*)$")
_DOTTED = re.compile(r"(?:[A-Za-z]{1,3}\.)+[A-Za-z]{1,3}")


def _abbreviation_before(text: str, i: int) -> bool:
    m = _ABBREV_BEFORE.search(text[:i])
    if not m:
        return False
    token = m.group(1)
    return (token.lower().rstrip(".") in _ABBREVIATIONS
            or (len(token) == 1 and token.isupper())
            or bool(_DOTTED.fullmatch(token)))


def _sentences(answer: str) -> list[str]:
    """Split on sentence ends outside quotes: a quoted message ("Veracruz
    tonight? you know it's my favorite") is part of the sentence quoting it,
    and so is the period of an abbreviation ('Dr. Ellis')."""
    pieces, buf, quoted = [], [], False
    text = answer.strip()
    for i, ch in enumerate(text):
        buf.append(ch)
        if ch == '"':
            quoted = not quoted
        elif ch == "“":
            quoted = True
        elif ch == "”":
            quoted = False
        elif ch == "\n" or (ch in ".!?" and not quoted
                            and (i + 1 == len(text) or text[i + 1].isspace())
                            and not (ch == "." and _abbreviation_before(text, i))):
            pieces.append("".join(buf))
            buf = []
    pieces.append("".join(buf))
    out: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        # "...7 hours. [M1]" — a trailing citation belongs to its sentence.
        if out and _BRACKET_RE.sub("", piece).strip(" .,;") == "":
            out[-1] = f"{out[-1]} {piece}"
        else:
            out.append(piece)
    return out


def evidence_ids(bundle: dict) -> set[str]:
    return {x["id"] for key in ("metrics", "entries", "passages", "observations")
            for x in bundle.get(key, [])}


def check_citations(answer: str, bundle: dict) -> dict:
    """Which evidence the answer cites, any id it invented, and any sentence
    whose numbers or dates the evidence does not contain.

    A sentence that states a number or a date must get it from the evidence
    it cites (or from the window and no-data lines, which every sentence may
    use): one valid citation elsewhere in the answer no longer vouches for
    the rest. A context line that points at ids ("last contact O3
    (2026-10-12)") lends its facts only to sentences citing one of them —
    "you last talked to Mom on 2026-10-12" with no citation is unsupported.
    The caller surfaces failures rather than printing them as fact.
    """
    cited = set(_cited(answer))
    known = evidence_ids(bundle)
    context, by_id = _evidence_lines(bundle)
    shared, attached = [], {}
    for line in context:
        refs = [r for r in _ID_REF.findall(line) if r in known]
        for r in refs:
            attached.setdefault(r, []).append(line)
        if not refs:
            shared.append(line)
    base = _facts("\n".join(shared), evidence=True)
    per_id: dict[str, dict] = {}
    unsupported = []
    for sentence in _sentences(answer):
        ids = [i for i in _cited(sentence) if i in known]
        f = _facts(_BRACKET_RE.sub(" ", sentence))
        if not (f["dates"] or f["md"] or f["times"] or f["nums"]):
            continue
        for i in ids:
            if i not in per_id:
                per_id[i] = _facts("\n".join([by_id[i], *attached.get(i, [])]),
                                   evidence=True)
        pool = _merge([base, *(per_id[i] for i in ids)])
        if not _supported(f, pool):
            unsupported.append(sentence[:300])
    return {"cited": sorted(cited & known), "unknown": sorted(cited - known),
            "uncited": bool(known) and not (cited & known),
            "unsupported": unsupported}


# ---------------------------------------------------------------- the whole pipeline

def evidence_index(bundle: dict) -> dict:
    """id -> one-line description, for printing what an answer relied on.

    Covers every evidence list a bundle may carry; a list a given build does
    not produce is simply absent."""
    index = {}
    for m in bundle.get("metrics", []):
        if m.get("metric") == "txn_spend":
            index[m["id"]] = (f"{m['label']}: {fmt_money(m['total'], m['unit'])} over "
                              f"{m.get('spent_count', m['count'])} transaction(s), "
                              f"{m['origin']}")
        else:
            index[m["id"]] = (f"{m['label']}: mean {fmt_value(m['mean'], m['unit'])} "
                              f"over {m['days']} day(s), {m['origin']}")
    for e in bundle.get("entries", []):
        index[e["id"]] = f"{e['date']} entry: {e['title']}"
    for p in bundle.get("passages", []):
        index[p["id"]] = f"{p['date']} transcript (source {p['source_id']})"
    for o in bundle.get("observations", []):
        what = o.get("kind") or o.get("stream")
        who = f" {o['who']}" if o.get("who") else ""
        title = f' "{o["title"]}"' if o.get("title") else ""
        index[o["id"]] = (f"{o['date']} {what}{who}{title} (observed, import "
                          f"{str(o.get('import_uuid') or '')[:8]}"
                          + (f", {o['source_file']}" if o.get("source_file") else "")
                          + ")")
    return index


def answer(conn: sqlite3.Connection, cfg: Config, question: str, *,
           days: int | None = None, use_llm: bool = True,
           model: str | None = None, warn=None, today: date | None = None) -> dict:
    """Everything `lifecrm ask` does short of logging the question.

    Returns exactly the `ask --json` shape: {question, answer, answer_source,
    citations, evidence, evidence_text, model, model_rejected}. `conn` should
    be read-only; sqlite3 errors from retrieval propagate to the caller, as
    does ValueError for an unusable `days`. `warn(msg)` hears about anything
    the person should know that is not the answer: a local model that failed,
    a model answer that was set aside, an endpoint that is not this machine.
    The CLI, the web UI and the voice loop all call this, so they can never
    disagree about what the data says.

    A model's answer is used only if every id it cites exists and every
    number and date it states is in the evidence it cited; otherwise the
    deterministic answer stands and `model_rejected` says why.
    """
    from . import llm as llm_mod

    bundle = build_evidence(
        conn, cfg, question, days=days, today=today,
        max_entries=ask_setting(cfg, "max_entries", 8),
        max_passages=ask_setting(cfg, "max_passages", 4),
        max_observations=ask_setting(cfg, "max_observations", 6))
    evidence_text = render_evidence(bundle)
    answer_text = deterministic_answer(bundle)
    source = "deterministic"
    rejected = None
    if not use_llm:
        llm_state = {"available": False, "reason": "--no-llm"}
    else:
        llm_state = llm_mod.status(cfg) if model is None \
            else llm_mod.status(cfg, model=model)
        if llm_state.get("warning") and warn:
            warn(llm_state["warning"])

    if use_llm and llm_state["available"] and evidence_ids(bundle):
        name = model or llm_state["model"]
        try:
            phrased = llm_mod.answer(cfg, evidence_text, question, model=name)
        except Exception as e:                      # transport, timeout, 404
            if warn:
                warn(f"local model unavailable ({e}) — deterministic answer")
            phrased = ""
        if phrased:
            check = check_citations(phrased, bundle)
            if check["unknown"] or check["unsupported"]:
                rejected = {"model": name, "answer": phrased,
                            "unknown": check["unknown"],
                            "unsupported": check["unsupported"]}
                if warn:
                    why = []
                    if check["unknown"]:
                        why.append(f"the model cited evidence that does not exist "
                                   f"({', '.join(check['unknown'])})")
                    if check["unsupported"]:
                        why.append(f"{len(check['unsupported'])} sentence(s) state "
                                   f"numbers or dates its citations do not contain")
                    warn("; ".join(why) + " — its answer was set aside for one "
                         "built from the evidence alone")
            else:
                answer_text, source = phrased, f"model:{name}"
                if check["uncited"] and warn:
                    warn("the model cited no evidence — verify it against --evidence")

    # Both paths cite evidence ids, so both get checked the same way — the
    # printed sources are what the answer actually used, never everything
    # that was retrieved.
    citations = check_citations(answer_text, bundle)
    return {"question": question, "answer": answer_text,
            "answer_source": source, "citations": citations,
            "evidence": bundle, "evidence_text": evidence_text,
            "model": llm_state, "model_rejected": rejected}

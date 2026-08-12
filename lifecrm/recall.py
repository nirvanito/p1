"""Deterministic retrieval: turn a question into cited evidence.

This is the answer layer's foundation, and it deliberately contains no model.
A question is parsed into a time window and a set of metrics by plain rules,
then answered from the database — every number, entry, and passage comes back
with an id (M1, E2, P3) and its provenance, and `no_data` names what was
asked for but does not exist.

The split matters: retrieval is deterministic, testable, and reproducible;
a local LLM (lifecrm/llm.py) is only ever allowed to PHRASE this evidence,
never to supply facts. The same evidence bundle serves the CLI, `--json` for
an external tool caller, and eventually the voice loop.

Reads only — callers pass a read-only connection.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

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

JOURNAL_METRICS: dict[str, dict] = {
    "mood":          {"agg": "avg", "unit": "/10", "label": "mood"},
    "energy":        {"agg": "avg", "unit": "/10", "label": "energy"},
    "sleep_hours":   {"agg": "avg", "unit": "h", "label": "sleep (journal)"},
    "sleep_quality": {"agg": "avg", "unit": "/10", "label": "sleep quality"},
    "rating":        {"agg": "avg", "unit": "/10", "label": "rating"},
    "weight":        {"agg": "avg", "unit": "kg", "label": "weight (journal)"},
    "steps":         {"agg": "sum", "unit": "steps", "label": "steps (journal)"},
    "calories":      {"agg": "sum", "unit": "kcal", "label": "calories (journal)"},
    "water_ml":      {"agg": "sum", "unit": "ml", "label": "water"},
    "reps":          {"agg": "sum", "unit": "reps", "label": "reps"},
    "sets":          {"agg": "sum", "unit": "sets", "label": "sets"},
    "duration":      {"agg": "sum", "unit": "min", "label": "duration"},
    "distance":      {"agg": "sum", "unit": "km", "label": "distance (journal)"},
    "servings":      {"agg": "sum", "unit": "servings", "label": "servings"},
    "money":         {"agg": "sum", "unit": "", "label": "money"},
}

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
    (("bodyfat", "bodyfat%"), ["body_fat"], []),
    (("spent", "spend", "spending", "cost", "paid", "money", "bought"),
     [], ["money"]),
]

# ---------------------------------------------------------------- windows

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
MONTH_ABBR = {m[:3]: i for m, i in MONTHS.items()}

_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_ISO_MONTH = re.compile(r"\b(\d{4})-(\d{2})\b")
_LAST_N = re.compile(r"\b(?:last|past|previous)\s+(\d+)\s+(day|week|month|year)s?\b")


def local_today(cfg: Config) -> date:
    """Today in the configured timezone, honoring the rollover hour — before
    05:00 local, 'today' is still yesterday's journal date."""
    return date.fromisoformat(days_mod.journal_date_for(
        datetime.now(timezone.utc), cfg.timezone, cfg.day_rollover_hour))


def _month_window(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1))
    return start.isoformat(), end.isoformat()


def parse_window(question: str, cfg: Config, today: date | None = None,
                 default_days: int = 30) -> dict:
    """Resolve the question's time expression.

    Rolling definitions, stated plainly because the answer always shows the
    resolved window: 'last week' is the 7 days before today, 'this week'
    starts Monday. `explicit` is False when nothing in the question named a
    time — callers widen text search in that case rather than silently
    hiding older matches.
    """
    q = question.lower()
    today = today or local_today(cfg)

    def win(start: date | str, end: date | str, label: str) -> dict:
        return {"start": start if isinstance(start, str) else start.isoformat(),
                "end": end if isinstance(end, str) else end.isoformat(),
                "label": label, "explicit": True}

    dates = _ISO_DATE.findall(q)
    if len(dates) >= 2:
        lo, hi = sorted(dates[:2])
        return win(lo, hi, f"{lo} to {hi}")
    if len(dates) == 1:
        return win(dates[0], dates[0], dates[0])

    m = _ISO_MONTH.search(q)
    if m:
        start, end = _month_window(int(m.group(1)), int(m.group(2)))
        return win(start, end, m.group(0))

    if re.search(r"\b(today)\b", q):
        return win(today, today, "today")
    if re.search(r"\b(yesterday)\b", q):
        y = today - timedelta(days=1)
        return win(y, y, "yesterday")
    if re.search(r"\blast night\b", q):
        # Sleep is filed under the day you woke up, so last night == today.
        return win(today, today, "last night")

    m = _LAST_N.search(q)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        span = {"day": 1, "week": 7, "month": 30, "year": 365}[unit] * n
        return win(today - timedelta(days=span - 1), today,
                   f"last {n} {unit}{'s' if n != 1 else ''}")

    if re.search(r"\bthis week\b", q):
        return win(today - timedelta(days=today.weekday()), today, "this week")
    if re.search(r"\b(last|past) week\b", q):
        return win(today - timedelta(days=7), today - timedelta(days=1),
                   "last week")
    if re.search(r"\bthis month\b", q):
        return win(today.replace(day=1), today, "this month")
    if re.search(r"\b(last|past) month\b", q):
        first = today.replace(day=1)
        prev = first - timedelta(days=1)
        start, end = _month_window(prev.year, prev.month)
        return win(start, end, "last month")
    if re.search(r"\bthis year\b", q):
        return win(today.replace(month=1, day=1), today, "this year")

    for token in re.findall(r"[a-z]+", q):
        month = MONTHS.get(token) or MONTH_ABBR.get(token)
        if not month:
            continue
        ymatch = re.search(rf"{token}\s+(\d{{4}})", q)
        year = int(ymatch.group(1)) if ymatch else (
            today.year if month <= today.month else today.year - 1)
        start, end = _month_window(year, month)
        return win(start, end, f"{token.capitalize()} {year}")

    start = today - timedelta(days=default_days - 1)
    return {"start": start.isoformat(), "end": today.isoformat(),
            "label": f"last {default_days} days", "explicit": False}


# ---------------------------------------------------------------- text search

STOPWORDS = {
    "a", "about", "all", "am", "an", "and", "any", "are", "as", "at", "average",
    "back", "be", "been", "being", "between", "but", "by", "can", "day", "days",
    "did", "do", "does", "doing", "done", "each", "for", "from", "get", "go",
    "had", "has", "have", "how", "i", "if", "in", "is", "it", "its", "just",
    "last", "like", "long", "many", "me", "month", "months", "more", "most",
    "much", "my", "night", "of", "often", "on", "or", "over", "past", "recent",
    "recently", "show", "so", "some", "that", "the", "their", "them", "then",
    "there", "these", "this", "those", "time", "times", "to", "today", "up",
    "was", "we", "week", "weeks", "were", "what", "when", "where", "which",
    "who", "why", "will", "with", "year", "years", "yesterday", "you", "your",
}


def fts_query(question: str) -> str | None:
    """Build a safe FTS5 MATCH expression from free text.

    Every token is extracted as word characters only and re-quoted, so FTS5
    operators, quotes, and punctuation in the question can never reach the
    parser as syntax (a raw question like `what's up? OR "` is a MATCH error,
    not a search). Tokens are OR-ed and results ranked by bm25.
    """
    tokens = [t for t in re.findall(r"[\w']+", question.lower())
              if len(t) > 2 and t not in STOPWORDS and not t.isdigit()]
    seen, keep = set(), []
    for t in tokens:
        t = t.strip("'")
        if t and t not in seen:
            seen.add(t)
            keep.append(t)
    if not keep:
        return None
    return " OR ".join(f'"{t}"' for t in keep[:12])


# ---------------------------------------------------------------- evidence

def _round(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    if unit in ("steps", "ml", "kcal"):
        return round(value)
    return round(value, 2)


def _metric_series(conn: sqlite3.Connection, *, table: str, metric: str,
                   spec: dict, window: dict) -> dict | None:
    factor = spec.get("factor", 1)
    agg = "SUM" if spec["agg"] == "sum" else "AVG"
    if table == "health_records":
        sql = (f"SELECT journal_date AS day, {agg}(value) AS value, "
               f"COUNT(*) AS n, GROUP_CONCAT(DISTINCT source_file) AS sources "
               f"FROM health_records WHERE metric=? AND status='active' "
               f"AND value IS NOT NULL AND journal_date BETWEEN ? AND ? "
               f"GROUP BY day ORDER BY day")
    else:
        sql = (f"SELECT journal_date AS day, "
               f"{agg}(COALESCE(normalized_value, numeric_value)) AS value, "
               f"COUNT(*) AS n, NULL AS sources "
               f"FROM measurements WHERE metric=? AND status='active' "
               f"AND journal_date IS NOT NULL "
               f"AND COALESCE(normalized_value, numeric_value) IS NOT NULL "
               f"AND journal_date BETWEEN ? AND ? GROUP BY day ORDER BY day")
    rows = conn.execute(sql, (metric, window["start"], window["end"])).fetchall()
    if not rows:
        return None
    unit = spec["unit"]
    series = [{"date": r["day"], "value": _round(r["value"] * factor, unit),
               "records": r["n"]} for r in rows]
    values = [s["value"] for s in series]
    sources = sorted({s for r in rows if r["sources"]
                      for s in r["sources"].split(",")})
    lo = min(series, key=lambda s: s["value"])
    hi = max(series, key=lambda s: s["value"])
    return {
        "metric": metric,
        "label": spec["label"],
        "unit": unit,
        "origin": "sensor import" if table == "health_records" else "journal",
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
    }


def detect_metrics(question: str) -> tuple[list[str], list[str]]:
    tokens = set(re.findall(r"[\w]+", question.lower()))
    health: list[str] = []
    journal: list[str] = []
    for words, h, j in KEYWORDS:
        if tokens & set(words):
            health += [m for m in h if m not in health]
            journal += [m for m in j if m not in journal]
    return health, journal


def search_entries(conn: sqlite3.Connection, match: str, window: dict,
                   limit: int) -> list[dict]:
    clause = ("AND e.journal_date BETWEEN ? AND ? " if window["explicit"] else "")
    args: tuple = ((match, window["start"], window["end"], limit)
                   if window["explicit"] else (match, limit))
    rows = conn.execute(
        "SELECT e.uuid, e.journal_date, e.title, e.body, k.name AS kind, "
        "snippet(entries_fts, -1, '«', '»', '…', 14) AS snippet "
        "FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid "
        "JOIN entry_kinds k ON k.id = e.kind_id "
        f"WHERE entries_fts MATCH ? AND e.status='active' {clause}"
        "ORDER BY rank LIMIT ?", args).fetchall()
    out = []
    for r in rows:
        cats = [c["name"] for c in conn.execute(
            "SELECT c.name FROM entry_categories ec "
            "JOIN categories c ON c.id=ec.category_id "
            "WHERE ec.entry_id=(SELECT id FROM entries WHERE uuid=?) "
            "ORDER BY ec.is_primary DESC", (r["uuid"],))]
        out.append({"uuid": r["uuid"], "date": r["journal_date"],
                    "kind": r["kind"], "title": r["title"],
                    "snippet": r["snippet"], "categories": cats})
    return out


def search_passages(conn: sqlite3.Connection, match: str, window: dict,
                    limit: int) -> list[dict]:
    clause = ("AND s.journal_date BETWEEN ? AND ? " if window["explicit"] else "")
    args: tuple = ((match, window["start"], window["end"], limit)
                   if window["explicit"] else (match, limit))
    rows = conn.execute(
        "SELECT t.source_id, s.journal_date, s.type, "
        "snippet(transcripts_fts, -1, '«', '»', '…', 24) AS snippet "
        "FROM transcripts_fts JOIN transcripts t ON t.id = transcripts_fts.rowid "
        "JOIN sources s ON s.id = t.source_id "
        f"WHERE transcripts_fts MATCH ? {clause}"
        "ORDER BY rank LIMIT ?", args).fetchall()
    return [{"source_id": r["source_id"], "date": r["journal_date"],
             "source_type": r["type"], "snippet": r["snippet"]} for r in rows]


def find_entities(conn: sqlite3.Connection, question: str) -> list[dict]:
    """Capitalized words in the question that resolve to known entities."""
    from .domain import entities as entities_mod
    names = re.findall(r"\b[A-Z][a-z]{2,}\b", question)
    found: dict[int, dict] = {}
    for name in names[:6]:
        result = entities_mod.match(conn, name)
        if result["status"] in ("exact", "alias"):
            for m in result["matches"]:
                found[m["id"]] = {"name": m["name"], "type": m["type"],
                                  "matched": name, "id": m["id"]}
    for ent in found.values():
        ent["entries"] = [dict(r) for r in conn.execute(
            "SELECT e.uuid, e.journal_date AS date, e.title FROM mentions m "
            "JOIN entries e ON e.id=m.entry_id WHERE m.entity_id=? "
            "AND e.status='active' ORDER BY e.journal_date DESC LIMIT 5",
            (ent["id"],))]
    return list(found.values())


def build_evidence(conn: sqlite3.Connection, cfg: Config, question: str, *,
                   today: date | None = None, days: int | None = None,
                   max_entries: int = 8, max_passages: int = 4) -> dict:
    """Everything the database can offer about this question, each item
    carrying a citable id and its provenance."""
    ask_cfg = cfg.ask
    window = parse_window(question, cfg, today=today,
                          default_days=days or int(ask_cfg.get("default_days", 30)))
    health, journal = detect_metrics(question)

    def missing_label(label: str, origin: str) -> str:
        # Several labels already carry their origin ("sleep (journal)") —
        # don't produce "sleep (journal) (journal)".
        return label if "(" in label else f"{label} ({origin})"

    metrics: list[dict] = []
    no_data: list[str] = []
    for name in health:
        found = _metric_series(conn, table="health_records", metric=name,
                               spec=HEALTH_METRICS[name], window=window)
        (metrics.append(found) if found else no_data.append(
            missing_label(HEALTH_METRICS[name]["label"], "sensor import")))
    for name in journal:
        found = _metric_series(conn, table="measurements", metric=name,
                               spec=JOURNAL_METRICS[name], window=window)
        (metrics.append(found) if found else no_data.append(
            missing_label(JOURNAL_METRICS[name]["label"], "journal")))
    for i, m in enumerate(metrics, start=1):
        m["id"] = f"M{i}"

    match = fts_query(question)
    entries = search_entries(conn, match, window, max_entries) if match else []
    passages = search_passages(conn, match, window, max_passages) if match else []
    for i, e in enumerate(entries, start=1):
        e["id"] = f"E{i}"
    for i, p in enumerate(passages, start=1):
        p["id"] = f"P{i}"

    entities = find_entities(conn, question)
    covered = conn.execute(
        "SELECT COUNT(DISTINCT journal_date) AS c FROM entries "
        "WHERE status='active' AND journal_date BETWEEN ? AND ?",
        (window["start"], window["end"])).fetchone()["c"]

    return {"question": question, "window": window, "metrics": metrics,
            "entries": entries, "passages": passages, "entities": entities,
            "no_data": no_data, "match": match,
            "days_with_entries_in_window": covered,
            "has_evidence": bool(metrics or entries or passages or entities)}


# ---------------------------------------------------------------- rendering

def fmt_value(value, unit: str) -> str:
    """Number plus unit, readable: counts get thousands separators, ratings
    read as 7/10, everything else keeps significant digits."""
    if value is None:
        return "-"
    text = f"{value:,.0f}" if unit in ("steps", "ml", "kcal") else f"{value:g}"
    return f"{text} {unit}".strip() if unit and not unit.startswith("/") \
        else f"{text}{unit}"


def render_evidence(bundle: dict, max_series: int = 14) -> str:
    """The evidence as compact text: what a model is allowed to use, and what
    a person sees when there is no model."""
    w = bundle["window"]
    lines = [f"WINDOW: {w['label']} ({w['start']} to {w['end']})"]
    for m in bundle["metrics"]:
        agg = "per-day total" if m["per_day_agg"] == "sum" else "per-day average"
        head = (f"[{m['id']}] {m['label']} — {m['days']} day(s), {agg}: "
                f"mean {fmt_value(m['mean'], m['unit'])}, "
                f"min {fmt_value(m['min'], m['unit'])} on {m['min_date']}, "
                f"max {fmt_value(m['max'], m['unit'])} on {m['max_date']}, "
                f"latest {fmt_value(m['latest'], m['unit'])} on {m['latest_date']}"
                f" (source: {m['origin']})")
        lines.append(head)
        series = m["series"][-max_series:]
        lines.append("     " + "; ".join(
            f"{s['date']} {fmt_value(s['value'], m['unit'])}" for s in series))
    for e in bundle["entries"]:
        cats = f" [{', '.join(e['categories'])}]" if e["categories"] else ""
        lines.append(f"[{e['id']}] {e['date']} ({e['kind']}){cats} {e['title']}"
                     + (f" — {e['snippet']}" if e["snippet"] else ""))
    for p in bundle["passages"]:
        lines.append(f"[{p['id']}] {p['date']} transcript (source "
                     f"{p['source_id']}): {p['snippet']}")
    for ent in bundle["entities"]:
        recent = "; ".join(f"{x['date']} {x['title']}" for x in ent["entries"])
        lines.append(f"[ENTITY] {ent['name']} ({ent['type']}) — recent: "
                     f"{recent or 'no entries'}")
    for missing in bundle["no_data"]:
        lines.append(f"[NO DATA] {missing}: nothing recorded in this window")
    if not bundle["has_evidence"]:
        lines.append("[NO DATA] nothing in the database matches this question")
    return "\n".join(lines)


def deterministic_answer(bundle: dict) -> str:
    """A useful answer with no model involved: the statistics, stated plainly,
    each pointing at its evidence id."""
    w = bundle["window"]
    out: list[str] = []
    for m in bundle["metrics"]:
        trend = ""
        if len(m["series"]) >= 4:
            half = len(m["series"]) // 2
            first = sum(s["value"] for s in m["series"][:half]) / half
            second = (sum(s["value"] for s in m["series"][half:])
                      / (len(m["series"]) - half))
            if first:
                change = (second - first) / abs(first) * 100
                if abs(change) >= 5:
                    trend = (f", trending {'up' if change > 0 else 'down'} "
                             f"{abs(change):.0f}% across the window")
        word = "totalled" if m["per_day_agg"] == "sum" else "averaged"
        out.append(f"{m['label'].capitalize()} {word} "
                   f"{fmt_value(m['mean'], m['unit'])} per day over {m['days']} "
                   f"day(s) (min {fmt_value(m['min'], m['unit'])} {m['min_date']}, "
                   f"max {fmt_value(m['max'], m['unit'])} {m['max_date']}){trend} "
                   f"[{m['id']}].")
    if bundle["entries"]:
        out.append("Related entries: " + "; ".join(
            f"{e['date']} {e['title']} [{e['id']}]" for e in bundle["entries"][:5])
            + ".")
    if bundle["passages"] and not bundle["entries"]:
        out.append("Mentioned in transcripts: " + "; ".join(
            f"{p['date']} [{p['id']}]" for p in bundle["passages"]) + ".")
    for ent in bundle["entities"]:
        out.append(f"{ent['name']} appears in "
                   f"{len(ent['entries'])} recent entr(y/ies).")
    if bundle["no_data"]:
        # One keyword can fire many metrics; naming every empty one buries the
        # answer. The full list stays in the evidence and in --json.
        shown = bundle["no_data"][:3]
        rest = len(bundle["no_data"]) - len(shown)
        out.append("No data for: " + ", ".join(shown)
                   + (f" (and {rest} more)" if rest > 0 else "")
                   + f" in {w['label']}.")
    if not out:
        out.append(f"Nothing in the database matches that question for "
                   f"{w['label']} ({w['start']} to {w['end']}). "
                   f"{bundle['days_with_entries_in_window']} day(s) in that "
                   f"window have entries at all.")
    return " ".join(out)


# ---------------------------------------------------------------- citations

CITE_RE = re.compile(r"\[([MEP]\d+)\]")


def evidence_ids(bundle: dict) -> set[str]:
    return ({m["id"] for m in bundle["metrics"]}
            | {e["id"] for e in bundle["entries"]}
            | {p["id"] for p in bundle["passages"]})


def check_citations(answer: str, bundle: dict) -> dict:
    """Which evidence the answer actually cites, and any id it invented.

    An unknown citation means the model referenced evidence that does not
    exist — the caller surfaces that rather than printing it as fact.
    """
    cited = set(CITE_RE.findall(answer))
    known = evidence_ids(bundle)
    return {"cited": sorted(cited & known), "unknown": sorted(cited - known),
            "uncited": bool(known) and not (cited & known)}

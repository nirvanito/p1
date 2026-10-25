"""`ask` for the read-side features: places, subscriptions, weekdays, links.

Each feature that answers a question shape of its own lives here, outside
recall.py's rules: `evidence()` recognises the shape, builds metrics in the
bundle's own format — an evidence line and answer sentences carrying every
number they state — and cites the records behind them through recall's own
observation items, so the answer, the citation check, the web page and the
local model all treat them like any other metric. A question none of them
recognises comes back with nothing added, and a feature that is unchecked in
[features] answers nothing.

    places         'which countries have I been to', 'what trips did I take
                   in 2024', 'where was I last weekend', 'when was I in
                   Slovenia', 'have I been to Bled'
    subscriptions  'what subscriptions do I pay for', 'recurring charges'
    weekdays       'which day of the week do I sleep the least'
    links          'who sent me nytimes.com', 'where did I see <a link>'
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from .config import Config

KINDS = ("places", "subscriptions", "weekday", "links")

_COUNTRIES_RE = re.compile(
    r"\b(?:which|what|how\s+many|list(?:\s+(?:the|my|all))?)\s+(?:of\s+the\s+)?countries\b"
    r"|\bcountries\s+(?:have\s+|did\s+)?(?:i|we)(?:'ve)?\s+(?:been|visited|visit|traveled|"
    r"travelled|gone|go)\b")
# 'what trips did I take', 'my last trip', 'where did I travel in 2024' — the
# owner's own trips, never 'what did Jake say about the lake trip'.
_TRIPS_RE = re.compile(
    r"\b(?:what|which|list|how\s+many|my|all)\s+(?:of\s+my\s+)?(?:\w+\s+)?(?:trips|vacations|"
    r"holidays)\b"
    r"|\b(?:trips?|vacations?)\s+(?:did|have|had)\s+(?:i|we)\b"
    r"|\b(?:where|when)\s+(?:did|have)\s+(?:i|we)\s+(?:travell?ed|travel|go\s+away)\b"
    r"|\b(?:my|last|latest|most\s+recent|recent)\s+(?:trip|vacation|holiday)\b")
_WHERE_RE = re.compile(r"\bwhere\s+(?:was|were|am)\s+(?:i|we)\b")
_VISIT_RE = re.compile(
    r"(?:\bwhen\s+(?:was|were|did)\s+(?:i|we)\s+(?:last\s+|first\s+|ever\s+)?(?:in|at|"
    r"go(?:ing)?\s+to|visit(?:ing)?|travel(?:l?ing)?\s+to|fly\s+to|get\s+to|been\s+to)"
    r"|\b(?:have|had)\s+(?:i|we)\s+(?:ever\s+)?been\s+(?:to|in)"
    r"|\bdid\s+(?:i|we)\s+(?:ever\s+)?(?:go|travel|fly|get)\s+to"
    r"|\bhow\s+(?:many\s+times|often)\s+(?:have\s+|did\s+)?(?:i|we)\s+(?:been|gone|go|"
    r"visited|visit)\s+(?:to\s+|in\s+)?"
    r"|\bwhich\s+(?:towns|cities|places)\s+(?:did|have)\s+(?:i|we)\s+(?:visit(?:ed)?|"
    r"see|seen|go\s+to|been\s+to)\s+in)\s+(?P<place>.+)$")
_SUBS_RE = re.compile(
    r"\b(?:subscriptions?|recurring\s+(?:charges?|payments?|bills?)|what\s+(?:do|am)\s+i\s+"
    r"(?:pay(?:ing)?|paying\s+for)\s+(?:every|each|per|a)\s+(?:month|year|week)|monthly\s+"
    r"(?:charges?|bills?|payments?))\b")
_WEEKDAY_RE = re.compile(r"\b(?:day\s+of\s+(?:the\s+)?week|weekday|weekdays|which\s+day\s+do\s+i"
                         r"|what\s+day\s+do\s+i|on\s+which\s+day)\b")
# A link or a site in the question — never the domain of an email address.
_LINK_RE = re.compile(r"(?i)(?<![@\w.-])(?:https?://\S+|www\.\S+|[a-z0-9-]+(?:\.[a-z0-9-]+)*\."
                      r"(?:com|org|net|io|co|uk|de|fr|edu|gov|app|dev|news|tv|me|us|ca|ai)"
                      r"(?:/\S*)?)(?![\w@])")
_LINK_ASK_RE = re.compile(r"\b(?:sent|send|shared|share|link|links|linked|url|see|saw|seen|"
                          r"mention|mentioned|where)\b")
_TIME_TAIL = re.compile(
    r"\b(?:last|this|next|in|during|since|before|after|ago|on|from|until|for|the|past)\b.*$")


def _metric(kind: str, label: str, line: str, sentences: list[str], *, origin: str,
            cites: list[str], summary: str, **extra) -> dict:
    return {"metric": kind, "label": label, "origin": origin, "table": "observations",
            "unit": "", "series": [], "line": line, "sentences": sentences,
            "summary": summary, "transactions": cites, **extra}


def _obs(conn: sqlite3.Connection, cfg: Config, add, ids: list[int], via: str
         ) -> dict[int, str]:
    """recall's own evidence items for these observation rows, added to the
    bundle; observation id -> O#."""
    from . import recall
    ids = [i for i in dict.fromkeys(ids) if i is not None]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT {recall._OBS_COLS} FROM observations o JOIN imports i "
                        f"ON i.id = o.import_id WHERE o.id IN ({marks}) ORDER BY o.id",
                        ids).fetchall()
    out = {}
    for row, item in zip(rows, recall._obs_items(conn, cfg, rows, via=via)):
        out[row["id"]] = add(item)
    return out


def evidence(conn: sqlite3.Connection, cfg: Config, question: str, *, window: dict,
             today: date, add, no_data: list[str]) -> list[dict]:
    """The features' metrics for this question (often none)."""
    from . import features
    q = question.lower().replace("’", "'").strip()
    try:
        on = features.settings(cfg)
    except Exception as e:                     # a bad [features] table
        no_data.append(f"features ({e})")
        return []
    out: list[dict] = []
    if on["places"]:
        out += _places(conn, cfg, q, window, today, add, no_data)
    if on["subscriptions"] and _SUBS_RE.search(q):
        out += _subscriptions(conn, cfg, today, add, no_data)
    if on["weekday_profile"] and _WEEKDAY_RE.search(q):
        out += _weekdays(conn, cfg, q, window, today, add, no_data)
    if on["link_memory"]:
        out += _links(conn, cfg, question, q, window, add)
    return out


# ---------------------------------------------------------------- places

def _span(window: dict) -> tuple[str | None, str | None, str]:
    if window.get("explicit"):
        return window["start"], window["end"], f"{window['label']} ({window['start']} to " \
                                               f"{window['end']})"
    return None, None, "all records"


def _places(conn, cfg, q, window, today, add, no_data) -> list[dict]:
    from . import places as pl
    wants_countries = bool(_COUNTRIES_RE.search(q))
    visit = _VISIT_RE.search(q)
    wants_trips = bool(_TRIPS_RE.search(q))
    wants_where = bool(_WHERE_RE.search(q)) and window.get("explicit")
    if not (wants_countries or visit or wants_trips or wants_where):
        return []
    name = cc = None
    if visit:
        name = _TIME_TAIL.sub("", visit.group("place")).strip(" ?.!'\"")
        name = re.sub(r"^(?:the\s+)", "", name)
        cc = pl.country_code(name) if name else None
    tl = pl.timeline(conn, cfg)
    if not tl["ok"]:
        # Said only for a question that is the feature's own; 'when was I last
        # at Zilker Park' is a linked place's question and gets nothing here.
        if wants_countries or wants_trips or wants_where or cc:
            no_data.append("place names (no gazetteer on this machine — run: python -m "
                           "lifecrm places fetch)")
        return []
    start, end, span = _span(window)
    out = []
    if wants_countries:
        cs = [c for c in tl["countries"]
              if (start is None or c["last_date"] >= start) and (end is None or c["first_date"] <= end)]
        if not cs:
            no_data.append(f"located records in any country ({span})")
        else:
            ids = _obs(conn, cfg, add, [c["first_obs_id"] for c in cs], "places")
            parts = [f"{c['name']} ({c['cc']}): first {c['first_date']}, last {c['last_date']}, "
                     f"{c['days']} day(s), towns {', '.join(c['towns'][:5])}"
                     + f" [{ids[c['first_obs_id']]}]" for c in cs]
            line = (f"countries with located records (place visits and photos, nearest "
                    f"GeoNames town; observed), {span}: {len(cs)} — " + "; ".join(parts))
            sentences = [f"Your located records fall in {len(cs)} countr"
                         f"{'y' if len(cs) == 1 else 'ies'} [{{mid}}]: " + "; ".join(
                             f"{c['name']}, {c['first_date']}"
                             + (f" to {c['last_date']}" if c["last_date"] != c["first_date"] else "")
                             + f" [{ids[c['first_obs_id']]}]" for c in cs) + "."]
            out.append(_metric("places", "countries visited", line, sentences,
                               origin="places from GeoNames, observed",
                               cites=list(ids.values()), summary=f"{len(cs)} countries"))
    if visit:
        town = None if cc else (pl.known_town(tl, name) if name else None)
        if name and (cc or town):
            v = pl.visits_to(tl, cc=cc, town=town)
            days = [d for d in v["days"] if (start is None or d >= start)
                    and (end is None or d <= end)]
            where = pl.country_name(cc) if cc else town
            if not days:
                no_data.append(f"located records in {where} ({span})")
            else:
                first_id = v["first"].obs_id if v["first"] else None
                last_id = v["last"].obs_id if v["last"] else None
                ids = _obs(conn, cfg, add, [first_id, last_id], "places")
                trips = [t for t in v["trips"] if (start is None or t["end"] >= start)
                         and (end is None or t["start"] <= end)]
                trip_text = "; ".join(pl.describe_trip(t) for t in trips[-5:])
                line = (f"located records in {where} (place visits and photos, nearest GeoNames "
                        f"town; observed), {span}: {len(days)} day(s), first {days[0]}, last "
                        f"{days[-1]}; towns {', '.join(v['towns'][:8])}"
                        + (f"; trips: {trip_text}" if trip_text else ""))
                cites = [ids[i] for i in (first_id, last_id) if i in ids]
                last_cite = f" [{ids[last_id]}]" if last_id in ids else ""
                first_cite = f" [{ids[first_id]}]" if first_id in ids else ""
                sent = (f"Your records place you in {where} on {len(days)} day"
                        f"{'' if len(days) == 1 else 's'} [{{mid}}], most recently {days[-1]}"
                        f"{last_cite}" + (f", first {days[0]}{first_cite}" if days[0] != days[-1]
                                          else "") + ".")
                sentences = [sent]
                if v["towns"] and cc:
                    sentences.append(f"Towns: {', '.join(v['towns'][:8])} [{{mid}}].")
                if trips:
                    sentences.append(f"Trips: {trip_text} [{{mid}}].")
                out.append(_metric("places", f"visits to {where}", line, sentences,
                                   origin="places from GeoNames, observed", cites=cites,
                                   summary=f"{len(days)} day(s) in {where}"))
    if wants_trips and not visit:
        trips = pl.trips_between(tl, start or "0001-01-01", end or "9999-12-31")
        if not trips:
            no_data.append(f"trips more than {tl['trip_km']:g} km from home ({span})")
        else:
            shown = trips[-10:]
            ids = _obs(conn, cfg, add, [t["first_obs_id"] for t in shown], "places")
            parts = [pl.describe_trip(t) + f" [{ids[t['first_obs_id']]}]" for t in shown]
            line = (f"trips (days with a located record more than {tl['trip_km']:g} km from home; "
                    f"observed), {span}: {len(trips)} trip(s)"
                    + (f", the latest {len(shown)} listed" if len(trips) > len(shown) else "")
                    + ": " + "; ".join(parts))
            sentences = [f"Your located records show {len(trips)} trip"
                         f"{'' if len(trips) == 1 else 's'} away from home [{{mid}}]: "
                         + "; ".join(parts[::-1]) + "."]
            out.append(_metric("places", "trips", line, sentences,
                               origin="places from GeoNames, observed",
                               cites=list(ids.values()), summary=f"{len(trips)} trip(s)"))
    if wants_where and not visit and not wants_countries:
        days = [d for d in sorted(tl["days"]) if start <= d <= end]
        if not days:
            no_data.append(f"located records ({span})")
        else:
            listed = days[:31]
            rows = []
            first_ids = []
            for d in listed:
                info = tl["days"][d]
                towns = ", ".join(t.label for t in info["towns"][:4]) or "no town within range"
                rows.append((d, towns, info["points"][0].obs_id))
                first_ids.append(info["points"][0].obs_id)
            ids = _obs(conn, cfg, add, first_ids, "places")
            parts = [f"{d}: {towns} [{ids[i]}]" for d, towns, i in rows]
            line = (f"where the located records put you (nearest GeoNames town; observed), "
                    f"{span}: {len(days)} day(s) with a position"
                    + (f", the first {len(listed)} listed" if len(days) > len(listed) else "")
                    + ": " + "; ".join(parts))
            sentences = [f"Your located records for {span}: " + "; ".join(parts)
                         + f" [{{mid}}]."]
            out.append(_metric("places", "where you were", line, sentences,
                               origin="places from GeoNames, observed",
                               cites=list(ids.values()), summary=f"{len(days)} located day(s)"))
    return out


# ---------------------------------------------------------------- subscriptions

def _subscriptions(conn, cfg, today, add, no_data) -> list[dict]:
    from . import subscriptions as subs
    data = subs.detect(conn, cfg, today)
    if not data["items"]:
        no_data.append("recurring charges (no transaction series repeats at a steady rhythm)")
        return []
    uuids = [x["latest"]["uuid"] for x in data["items"]]
    rows = {r["uuid"]: r["id"] for r in conn.execute(
        f"SELECT id, uuid FROM observations WHERE uuid IN ({','.join('?' * len(uuids))})",
        uuids)}
    ids = _obs(conn, cfg, add, [rows.get(u) for u in uuids], "subscriptions")
    by_uuid = {u: ids.get(rows.get(u)) for u in uuids}
    parts = [subs.describe(x) + (f" [{by_uuid[x['latest']['uuid']]}]"
                                 if by_uuid.get(x["latest"]["uuid"]) else "")
             for x in data["items"]]
    monthly = "; ".join(f"about {subs.money(v, k or None)} a month" for k, v in
                        sorted(data["monthly"].items()))
    active = [p for p, x in zip(parts, data["items"]) if x["status"] == "active"]
    other = [p for p, x in zip(parts, data["items"]) if x["status"] != "active"]
    line = (f"recurring charges as of {data['as_of']} (the bank's own descriptions; observed): "
            f"{len(data['items'])} series, {len(active)} active"
            + (f", {monthly} in active ones" if monthly else "")
            + ": " + "; ".join(parts) + f". Rule: {data['rule']}")
    sentences = []
    if active:
        sentences.append(f"{len(active)} charge(s) repeat at a steady rhythm"
                         + (f", {monthly} in all" if monthly else "") + " [{mid}]: "
                         + "; ".join(active) + ".")
    if other:
        sentences.append("Series with no recent charge: " + "; ".join(other) + " [{mid}].")
    return [_metric("subscriptions", "recurring charges", line, sentences,
                    origin="transactions, observed", cites=[i for i in by_uuid.values() if i],
                    summary=f"{len(data['items'])} recurring series")]


# ---------------------------------------------------------------- weekdays

_WEEKDAY_SERIES = (("sleep", ("sleep", "slept", "sleeping")),
                   ("steps", ("steps", "walk", "walked", "walking")),
                   ("mood", ("mood", "happy", "happiest", "saddest", "feel", "felt")),
                   ("energy", ("energy", "energetic", "tired", "tiredest")),
                   ("workouts", ("workout", "workouts", "gym", "exercise", "train", "trained")),
                   ("spending", ("spend", "spent", "spending", "money")),
                   ("texts", ("text", "texts", "texting", "message", "messages")),
                   ("contacts", ("people", "social", "see people", "talk")))


def _weekdays(conn, cfg, q, window, today, add, no_data) -> list[dict]:
    from . import weekday as wk
    words = set(re.findall(r"[a-z]+", q))
    keys = [k for k, vocab in _WEEKDAY_SERIES if words & set(vocab)]
    if not keys:
        return []
    if window.get("explicit"):
        start, end = window["start"], window["end"]
    else:
        start, end = wk.window(today)
    out = []
    for key in keys:
        p = wk.profile(conn, cfg, key, start, end)
        if key == "sleep" and p["low"] is None:
            p = wk.profile(conn, cfg, "sleep_hours", start, end)
        if p["low"] is None:
            no_data.append(f"{p['label']} on at least two weekdays ({wk.MIN_PER_WEEKDAY} known "
                           f"days each, {start} to {end})")
            continue
        line = wk.describe(p)
        lo, hi = p["low"], p["high"]
        per = " a day" if p["agg"] in ("sum", "count") else ""
        sentences = [f"Over {start} to {end}, {p['label']} averaged lowest on "
                     f"{lo['weekday']}s ({wk._num(lo['mean'], p['unit'])}{per}, {lo['known']} "
                     f"known days) and highest on {hi['weekday']}s "
                     f"({wk._num(hi['mean'], p['unit'])}{per}, {hi['known']} known days) [{{mid}}]."]
        out.append(_metric("weekday", f"{p['label']} by weekday", line, sentences,
                           origin=f"{p['tier']} series", cites=[],
                           summary=f"lowest {lo['weekday']}, highest {hi['weekday']}"))
    return out


# ---------------------------------------------------------------- links

def _links(conn, cfg, question, q, window, add) -> list[dict]:
    from . import links as lk
    m = _LINK_RE.search(question)
    if not m or not _LINK_ASK_RE.search(q):
        return []
    data = lk.sightings(conn, cfg, m.group(0),
                        start=window["start"] if window.get("explicit") else None,
                        end=window["end"] if window.get("explicit") else None)
    t = data["target"]
    if t is None or not data["sightings"]:
        return []
    obs = [s for s in data["sightings"] if s["type"] == "observation"]
    rows = {r["uuid"]: r["id"] for r in conn.execute(
        f"SELECT id, uuid FROM observations WHERE uuid IN ({','.join('?' * len(obs))})",
        [s["uuid"] for s in obs])} if obs else {}
    shown = data["sightings"][:12]
    ids = _obs(conn, cfg, add, [rows.get(s["uuid"]) for s in shown if s["type"] == "observation"],
               "links")
    what = f"the site {t['host']}" if t["site"] else t["norm"]
    parts = []
    for s in shown:
        cite = ids.get(rows.get(s["uuid"])) if s["type"] == "observation" else None
        who = f" from {s['from']}" if s.get("from") else ""
        kind = s.get("stream") or "journal"
        parts.append(f"{s['date']} {kind}{who}" + (f" [{cite}]" if cite else ""))
    line = (f"records linking to {what} (texts, email, notes, chats, browser history, the "
            f"journal): {data['total']} — " + "; ".join(parts))
    first = parts[0]
    sentences = [f"{data['total']} record(s) link to {what} [{{mid}}]; the first: {first}"
                 + (f"; the latest: {parts[-1]}" if len(parts) > 1 else "") + "."]
    return [_metric("links", f"links to {what}", line, sentences,
                    origin="imports and journal", cites=list(ids.values()),
                    summary=f"{data['total']} record(s)")]

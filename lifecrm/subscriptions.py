"""Subscriptions: charges that repeat, new ones, and price rises.

Rocket Money and its kind scan a bank feed for charges that come back at a
steady rhythm and alert when one costs more than last time. Here the same
detection runs over the `txn` rows your statements imported: charges are
grouped by the merchant's own description (digits and reference codes
dropped, nothing categorised), and a group is a subscription when its
charges come back weekly, monthly, quarterly or yearly at a steady amount.

    price rise   the latest charge is more than max($1.99, 5 %) above the one
                 before (Rocket Money's own threshold)
    new          the series became visible recently: its first charge within
                 a few periods and its newest within the last 45 days
    stopped      the next charge is overdue — said only when the statements
                 declared those days complete (`import_coverage`): a missing
                 row is unknown, never "cancelled"

Payment-app transfers between your own accounts and bank lines that fund a
payment app are left out the way review.classify_txns leaves them out, and
so are payments to people. Reads only; every series cites its charges.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
import unicodedata
from datetime import date, timedelta

from .config import Config
from .recall import STREAM_GROUPS

TXN_STREAMS = STREAM_GROUPS["transaction"]
# cadence -> (median interval range, every interval's tolerance band, min charges)
CADENCES = (
    ("weekly", (6, 8), (5, 9), 4),
    ("monthly", (27, 34), (24, 38), 3),
    ("quarterly", (85, 97), (78, 104), 3),
    ("yearly", (350, 380), (335, 395), 2),
)
REGULAR_SHARE = 0.75
AMOUNT_SPREAD = 0.25
PRICE_RISE_ABS = 1.99
PRICE_RISE_PCT = 0.05
RECENT_DAYS = 45
NOISE = frozenset({"pos", "debit", "purchase", "card", "recurring", "payment", "ach",
                   "www", "com", "inc", "llc", "ltd", "co", "online", "autopay", "pmt",
                   "bill", "ref", "id", "the", "visa", "mastercard", "dd", "sq", "tst"})
# What one charge comes to per month, by cadence.
PER_MONTH = {"weekly": 52 / 12, "monthly": 1.0, "quarterly": 1 / 3, "yearly": 1 / 12}


def merchant_key(text: str | None) -> str:
    """The merchant a description names, as a grouping key: lowercase, accents
    and punctuation off, any word with a digit dropped (store numbers, dates,
    reference codes), card and bank noise words dropped, the first three
    words kept. 'NETFLIX.COM 866-579-7172 CA' -> 'netflix ca'."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    words = [w for w in re.sub(r"[^\w]+", " ", t).split()
             if not any(ch.isdigit() for ch in w) and len(w) > 1 and w not in NOISE]
    return " ".join(words[:3])


def slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "merchant"


def _spent_rows(conn: sqlite3.Connection, end: str) -> list:
    from .review import _money_flow, classify_txns
    marks = ",".join("?" * len(TXN_STREAMS))
    try:
        rows = conn.execute(
            "SELECT o.id, o.uuid, o.stream, o.journal_date, o.started_at, o.ended_at, "
            "o.direction, o.title, o.body, o.value, o.unit, o.value_json, o.origin_app, "
            "i.uuid AS import_uuid FROM observations o JOIN imports i ON i.id = o.import_id "
            f"WHERE o.status = 'active' AND o.stream IN ({marks}) AND o.journal_date <= ? "
            "ORDER BY o.journal_date, o.started_at, o.id", (*TXN_STREAMS, end)).fetchall()
    except sqlite3.OperationalError:
        return []
    merchants = classify_txns(conn, rows)["merchant_rows"]
    out = []
    for r in merchants:
        flow, amount = _money_flow(r)
        if flow == "spent" and amount > 0:
            out.append((r, round(amount, 2)))
    return out


def _next_due(last: date, cadence: str, median_gap: float) -> date:
    """The next charge: the same day of the next month (quarter, year) for a
    calendar cadence, clamped to the month's last day; a week later for a
    weekly one."""
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}.get(cadence)
    if months is None:
        return last + timedelta(days=round(median_gap))
    y, m = divmod(last.month - 1 + months, 12)
    year, month = last.year + y, m + 1
    for day in (last.day, 30, 29, 28):
        try:
            return date(year, month, min(last.day, day))
        except ValueError:
            continue
    return date(year, month, 28)


def _cadence(intervals: list[int]) -> tuple[str, float] | None:
    if not intervals:
        return None
    med = statistics.median(intervals)
    for name, (lo, hi), (blo, bhi), need in CADENCES:
        if not lo <= med <= hi or len(intervals) + 1 < need:
            continue
        regular = sum(1 for i in intervals if blo <= i <= bhi) / len(intervals)
        if regular >= REGULAR_SHARE:
            return name, med
    return None


def detect(conn: sqlite3.Connection, cfg: Config, today: date) -> dict:
    """Every recurring series up to `today`: {'items': [...], 'monthly':
    {currency: total}, 'rule'}. Each item names its charges (uuids, the
    latest and the one before) so a caller can cite them."""
    from .domain import observations as obs_mod
    rows = _spent_rows(conn, today.isoformat())
    groups: dict[tuple[str, str], list] = {}
    for r, amount in rows:
        key = merchant_key(r["title"] or r["body"])
        if not key:
            continue
        groups.setdefault((key, (r["unit"] or "").upper()), []).append((r, amount))
    items = []
    for (key, cur), charges in sorted(groups.items()):
        # one charge per day: a refund-and-recharge or a split posting is one
        per_day: dict[str, tuple] = {}
        for r, amount in charges:
            d = r["journal_date"]
            if d not in per_day or amount > per_day[d][1]:
                per_day[d] = (r, amount)
        series = [per_day[d] for d in sorted(per_day)]
        if len(series) < 2:
            continue
        dates = [date.fromisoformat(r["journal_date"]) for r, _a in series]
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        found = _cadence(intervals)
        if found is None:
            continue
        cadence, median_gap = found
        amounts = [a for _r, a in series]
        recent = amounts[-3:]
        mid = statistics.median(recent)
        if mid <= 0 or any(abs(a - mid) > AMOUNT_SPREAD * mid for a in recent[:-1]):
            continue
        latest_r, latest = series[-1]
        prev_r, prev = series[-2]
        rise = latest - prev
        price_rise = rise > max(PRICE_RISE_ABS, PRICE_RISE_PCT * prev)
        next_due = _next_due(dates[-1], cadence, median_gap)
        grace = timedelta(days=round(0.1 * median_gap) + 5)
        status, judged = "active", True
        if today > next_due + grace:
            covered = obs_mod.coverage_days(conn, TXN_STREAMS, next_due.isoformat(),
                                            (next_due + grace).isoformat())
            span = (grace.days + 1)
            if len(covered) >= span:
                status = "stopped"
            else:
                status, judged = "overdue", False
        first = dates[0]
        new = ((today - first).days <= max(4 * median_gap, 60)
               and (today - dates[-1]).days <= RECENT_DAYS
               and len(series) <= {"weekly": 6, "monthly": 4}.get(cadence, 3))
        per_month = round(latest * PER_MONTH[cadence], 2) if status == "active" else 0.0
        items.append({
            "key": key, "slug": slug(key), "merchant": (latest_r["title"] or key).strip(),
            "currency": cur or None, "cadence": cadence, "interval_days": median_gap,
            "amount": latest, "previous_amount": prev, "count": len(series),
            "first_date": series[0][0]["journal_date"],
            "last_date": latest_r["journal_date"], "previous_date": prev_r["journal_date"],
            "next_due": next_due.isoformat(), "status": status, "judged": judged,
            "price_rise": price_rise, "rise": round(rise, 2) if price_rise else 0.0,
            "new": new, "per_month": per_month,
            "uuids": [r["uuid"] for r, _a in series][-20:],
            "imports": sorted({r["import_uuid"] for r, _a in series}),
            "latest": _row(latest_r), "previous": _row(prev_r)})
    items.sort(key=lambda x: (x["status"] != "active", -x["per_month"], x["merchant"].lower()))
    monthly: dict[str, float] = {}
    for x in items:
        if x["status"] == "active":
            k = x["currency"] or ""
            monthly[k] = round(monthly.get(k, 0.0) + x["per_month"], 2)
    return {"as_of": today.isoformat(), "items": items, "monthly": monthly,
            "rule": ("charges grouped by the merchant's own description; a series repeats "
                     "weekly, monthly, quarterly or yearly at a steady amount; a price rise "
                     f"is more than {PRICE_RISE_ABS} or {int(PRICE_RISE_PCT * 100)}% over the "
                     "charge before; 'stopped' only where the statements declared the days "
                     "complete")}


def _row(r) -> dict:
    return {"type": "observation", "tier": "observed", "uuid": r["uuid"],
            "stream": r["stream"], "date": r["journal_date"],
            "occurred_at": r["started_at"], "direction": r["direction"],
            "title": r["title"], "import_uuid": r["import_uuid"]}


def count_item(x: dict) -> dict:
    """The O# item a series is cited as: its charges."""
    return {"type": "observation_count", "tier": "observed",
            "what": f"recurring charges: {x['merchant']}", "count": x["count"],
            "first_date": x["first_date"], "last_date": x["last_date"],
            "uuids": x["uuids"], "imports": x["imports"], "stream": "txn",
            "cadence": x["cadence"], "amount": x["amount"], "currency": x["currency"]}


def money(value: float, cur: str | None) -> str:
    return f"{value:,.2f}" + (f" {cur}" if cur else "")


def describe(x: dict) -> str:
    """One line per series, every number in it."""
    line = (f"{x['merchant']}: {money(x['amount'], x['currency'])} {x['cadence']}, "
            f"{x['count']} charges {x['first_date']} to {x['last_date']}")
    if x["status"] == "active":
        line += f", next about {x['next_due']} (about {money(x['per_month'], x['currency'])} a month)"
    elif x["status"] == "stopped":
        line += f", no charge since — due about {x['next_due']}, statements complete"
    else:
        line += f", due about {x['next_due']} — not judged: statements do not cover it"
    if x["price_rise"]:
        line += (f"; up from {money(x['previous_amount'], x['currency'])} on "
                 f"{x['previous_date']}")
    return line


def render_lines(data: dict) -> list[str]:
    out = [f"Recurring charges as of {data['as_of']} (the bank's own descriptions, "
           f"observed; no categories):"]
    if not data["items"]:
        out.append("  none found")
    for x in data["items"]:
        flags = " [NEW]" if x["new"] else ""
        flags += " [PRICE RISE]" if x["price_rise"] else ""
        out.append(f"  - {describe(x)}{flags}")
    for cur, total in sorted(data["monthly"].items()):
        out.append(f"  about {money(total, cur or None)} a month in active subscriptions")
    out.append(f"  rule: {data['rule']}")
    return out

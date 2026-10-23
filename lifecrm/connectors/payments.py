"""Payments connector: Venmo statements and PayPal activity downloads —
money between people, with the person on the other side named.

A bank statement says "VENMO PAYMENT 1023849" and stops there. Venmo and
PayPal know who the money went to and why ("Jake Morales: Veracruz tacos"),
and both hand that over as a CSV from their website. This connector reads
those files. It never logs in, never calls an API, and never sends anything.

Accepted targets
----------------
- one statement file (.csv), recognised by its header, not its name
- a folder of them, searched recursively (statements saved month by month;
  anything that is neither a Venmo nor a PayPal download is counted and
  left alone, so a folder shared with bank statements is fine)
- a .zip of them
- nothing at all, when `path` is set in [connectors.payments]

The two formats
---------------
Venmo (venmo.com → Statements → a month or range → Download CSV): a few
preamble rows ('Account Statement - (@username)', 'Account Activity'), then

  ,ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (tip),
  Amount (tax),Amount (fee),Tax Rate,Tax Exempt,Funding Source,Destination,
  Beginning Balance,Ending Balance,Statement Period Venmo Fees,
  Terminal Location,Year to Date Venmo Fees,Disclaimer

— the first column has no name and no values. The first and last data rows
carry only balances, fees and the disclaimer; they are statement summary
rows, counted and skipped. Amounts read '+ $25.00' / '- $1,234.56', from the
account holder's side. Older statements name the first column 'Username';
that works too.

PayPal (paypal.com → Activity → Statements → Activity report / Download →
CSV): one header row

  Date,Time,TimeZone,Name,Type,Status,Currency,Gross,Fee,Net,
  From Email Address,To Email Address,Transaction ID,…,Subject,Note,…,
  Balance Impact

Dates are in the account's locale (month-first in the US, day-first in
most of the world: decided from the file's own dates, else
[connectors.payments] paypal_date_order, else month-first when every row's
zone is a US one, else month-first with a warning that it had to guess).
TimeZone is an abbreviation (PDT, CET, …) or an offset. Gross is signed
from the account's side.

What comes in
-------------
One observation per completed transaction, stream `txn` — the stream, and
the semantics, of the finance connector, so `ask`'s money answers read both:

  value       the signed amount (Venmo's Amount (total), PayPal's Gross) —
              negative is money out
  unit        lowercase ISO currency ('usd'; Venmo is always USD)
  direction   'out' / 'in'
  title       the counterparty and the note: "Jake Morales: Veracruz tacos"
              (a transfer: "Standard Transfer to Chase Bank")
  body        the note
  value_json  {app, note, type, status, funding_source, destination, and
              when non-zero tip, tax, fee (Venmo) / fee, net, subject, item,
              reference_txn_id (PayPal); transfer: true for money moved
              between your own accounts}

`transfer` marks cash-outs, bank top-ups, card funding and currency
conversions: real movements of the account, but not money paid to anyone.
A spending total should leave them out — the bank statement shows the
same money arriving or leaving.

Pending, failed, declined, denied, canceled and other unfinished rows are
skipped and counted: such a payment never happened, or has not yet. A
status this reader does not know is reported, not imported. PayPal's
'Memo' lines (authorizations and holds, no balance impact) are skipped the
same way.

Counterparties are handles, never names
---------------------------------------
- PayPal names the other side by email: an email handle, role
  'counterparty', with the Name column as its display_name. It is the same
  handle a contact card, a calendar invite or a LinkedIn connection
  carries, so linking it once attributes every payment.
- Venmo's statement has no ids for people — only the name as their
  profile shows it. The counterparty becomes app_account
  'venmo:<name as shown>' (display_name the name): the export's only
  identifier for that person, and a profile renamed later is a new handle.
- Your own side is never a counterparty. It is the account's handle,
  participant 'sender' of money out and 'recipient' of money in — finance's
  card rule — so linking it marks every row as yours.

Which side is you: Venmo's From/To that is [connectors.payments] me (a
name, or a list of names and emails) or [owner] name; else the name on
every row of the statement; else the one matching the statement's
@username. PayPal's email that is `me` or one of [owner] handles; else the
payer of money out and the payee of money in (Gross is signed from your
side, so the file says it outright). When Venmo's owner cannot be told,
the counterparty is read from the amount's sign and the type, and the
report warns.

Identity
--------
'venmo:<ID>' / 'paypal:<Transaction ID>' — each service's own id, so
overlapping statements (October, then mid-October to November) import the
overlap once. A Venmo ID that a spreadsheet re-saved as '3.91E+18' has lost
its digits; such rows are reported, not imported — re-download the file.

Times: PayPal's are local to the zone each row names. Venmo writes no zone
at all; its times are read as UTC — set [connectors.payments]
venmo_timezone if your statements turn out to be local (docs/payments.md:
unverified without a real statement).

Deletion semantics — remember, never mirror
-------------------------------------------
A statement is a window (one month, a chosen range), not the account's
whole history. Nothing is removed because a later file does not hold it;
`imports retract` undoes a run.

Known limitations, by design
----------------------------
- A payment funded from a bank or card shows on that statement too ('VENMO
  PAYMENT'); summing both counts it twice. This connector cannot see the
  bank side — a money total over every `txn` must decide which to trust.
- Venmo's merchant payments name the business as shown, the same way as a
  person: it is a handle to link or ignore.
- No auto-categorisation, no merging of counterparties: as in finance, that
  is a derived layer, never the record.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import owner as owner_mod
from ..config import Config, ConfigError
from ..domain.util import DomainError, canonical_json
from . import _ofx, base
from .finance import parse_amount

name = "payments"
streams = ("txn",)
# No stale_after_days: statements are downloaded when wanted, and doctor's
# age check is per stream — `txn` is finance's too, and would measure both.

_HEADER_SCAN = 15
_VENMO_HEADER = ("id", "datetime", "type", "status", "from", "to", "amount (total)")
_PAYPAL_HEADER = ("date", "time", "name", "type", "status", "currency", "transaction id")

# Done: imported. Not done: skipped and counted. Anything else is reported.
_DONE = {"complete", "completed", "issued", "cleared", "refunded",
         "partially refunded", "paid"}
_NOT_DONE = {"pending", "failed", "declined", "denied", "canceled", "cancelled",
             "voided", "expired", "unclaimed", "held", "on hold", "placed",
             "removed", "uncleared"}
_TRANSFER_WORDS = ("transfer", "deposit", "withdrawal", "add funds", "currency conversion",
                   "card funding", "cash out", "cashout")
_DATE_ORDERS = {"mdy": "month-first", "dmy": "day-first", "ymd": "year-first"}
_ORDER_SETTINGS = {"mdy": "mdy", "month-first": "mdy", "dmy": "dmy",
                   "day-first": "dmy", "ymd": "ymd", "year-first": "ymd"}

# Zone abbreviations PayPal writes, as hours from UTC. 'IST' (India or
# Ireland) and other ambiguous ones are left out: an unknown zone is read in
# the home timezone and reported, never guessed.
_ZONE_HOURS = {
    "UTC": 0, "GMT": 0, "Z": 0, "WET": 0, "WEST": 1, "BST": 1, "CET": 1, "CEST": 2,
    "MET": 1, "MEST": 2, "EET": 2, "EEST": 3, "MSK": 3, "GST": 4, "PKT": 5,
    "SGT": 8, "HKT": 8, "AWST": 8, "JST": 9, "KST": 9, "ACST": 9.5, "ACDT": 10.5,
    "AEST": 10, "AEDT": 11, "NZST": 12, "NZDT": 13,
    "HST": -10, "AKST": -9, "AKDT": -8, "PST": -8, "PDT": -7, "MST": -7, "MDT": -6,
    "CST": -6, "CDT": -5, "EST": -5, "EDT": -4, "AST": -4, "ADT": -3,
    "NST": -3.5, "NDT": -2.5,
}


# ---------------------------------------------------------------- small readers

def _norm(cell: str) -> str:
    return " ".join(str(cell).replace("﻿", "").strip().strip('"').lower().split())


def _collapse(text) -> str:
    return " ".join(str(text or "").replace("\xa0", " ").split())


def _key(name_text: str) -> str:
    return _collapse(name_text).casefold()


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", re.sub(r"[-_\d]+$", "", text.casefold()))


def _header(rows: list[list[str]], required: tuple[str, ...]):
    for i, row in enumerate(rows[:_HEADER_SCAN]):
        names = [_norm(c) for c in row]
        if set(required) <= set(names):
            index: dict[str, int] = {}
            for j, n in enumerate(names):
                if n:
                    index.setdefault(n, j)
            return i, index
    return None


def _kind(rows: list[list[str]]) -> tuple[str, int, dict] | None:
    """('venmo' | 'paypal', header row, column index), or None."""
    v = _header(rows, _VENMO_HEADER)
    if v:
        return "venmo", *v
    p = _header(rows, _PAYPAL_HEADER)
    if p:
        i, index = p
        if ("gross" in index or "amount" in index) and \
                ("timezone" in index or "time zone" in index):
            return "paypal", i, index
    return None


def _money(text: str):
    """(Decimal or None); ValueError for text that is not an amount."""
    return parse_amount(text)[0]


def _is_transfer(kind: str) -> bool:
    low = kind.casefold()
    return any(w in low for w in _TRANSFER_WORDS)


def _status_skip(status: str) -> str:
    return f"{status or 'blank-status'} transaction (only completed ones are imported)"


# ---------------------------------------------------------------- settings

def _settings(cfg: Config) -> dict:
    s = cfg.connector_settings(name)
    me = s.get("me")
    if me is None or me == "":
        mine: list[str] = []
    elif isinstance(me, str):
        mine = [me]
    elif isinstance(me, list) and all(isinstance(x, str) for x in me):
        mine = [x for x in me if x.strip()]
    else:
        raise DomainError(f"[connectors.payments] me must be your name as Venmo shows "
                          f"it (or your PayPal email), or a list of them — got {me!r}")
    try:
        owner_name, owner_handles = owner_mod.configured(cfg)
    except ConfigError as e:
        raise DomainError(str(e)) from e
    names = [_collapse(x) for x in mine if "@" not in x]
    if owner_name:
        names.append(_collapse(owner_name))
    from_me = {_key(x) for x in mine if "@" not in x}
    emails = {x.strip().lower() for x in mine if "@" in x}
    emails |= {v for k, v in owner_handles if k == "email"}
    zone = s.get("venmo_timezone") or "UTC"
    try:
        ZoneInfo(str(zone))
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise DomainError(f"[connectors.payments] venmo_timezone '{zone}' is not a "
                          f"known IANA zone (e.g. UTC, America/Chicago)") from e
    order = s.get("paypal_date_order")
    if order is not None:
        if str(order).lower() not in _ORDER_SETTINGS:
            raise DomainError(f"[connectors.payments] paypal_date_order must be \"mdy\" "
                              f"(month-first) or \"dmy\" (day-first) — got {order!r}")
        order = _ORDER_SETTINGS[str(order).lower()]
    return {"names": names, "from_me": from_me, "owner_name": owner_name,
            "emails": emails, "venmo_zone": str(zone), "paypal_order": order}


# ---------------------------------------------------------------- Venmo

_USERNAME = re.compile(r"\(@([^)\s]+)\s*\)")
_SCIENTIFIC = re.compile(r"\d+(\.\d+)?E\+?\d+", re.I)


def _read_venmo(fname: str, rows: list[list[str]], start: int, index: dict,
                settings: dict, cfg: Config) -> dict:
    username = None
    for row in rows[:start]:
        m = _USERNAME.search(" ".join(row))
        if m:
            username = m.group(1)
            break
    zone = ZoneInfo(settings["venmo_zone"])
    skipped, unmapped, examples = Counter(), Counter(), {}
    txns = []

    for lineno, row in enumerate(rows[start + 1:], start=start + 2):
        if not any(c.strip() for c in row):
            continue

        def cell(col: str) -> str:
            j = index.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        if username is None and cell("username").startswith("@"):
            username = cell("username")[1:]
        tid = cell("id")
        if not tid:
            skipped["statement summary row (balances, fees, disclaimer)"] += 1
            continue
        if _SCIENTIFIC.fullmatch(tid):
            what = ("row whose ID a spreadsheet rounded (e.g. 3.91E+18) — re-download "
                    "the statement and import it unopened")
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        status = cell("status").lower()
        if status in _NOT_DONE:
            skipped[_status_skip(status)] += 1
            continue
        if status not in _DONE:
            what = f"row with a status this reader does not know ('{cell('status')}')"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        try:
            # A zone the file does state (a trailing Z, an offset) is kept;
            # only a bare time takes venmo_timezone.
            naive = datetime.fromisoformat(cell("datetime").replace("Z", "+00:00"))
        except ValueError:
            what = "row whose Datetime does not read"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno} ({cell('datetime')!r})")
            continue
        if naive.tzinfo is None:
            naive = naive.replace(tzinfo=zone)
        when = naive.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            value = _money(cell("amount (total)"))
            extras = {k: _money(cell(f"amount ({k})")) for k in ("tip", "tax", "fee")}
        except ValueError:
            what = "row with an amount that does not parse"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        if value is None:
            what = "row with no Amount (total)"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        txns.append({
            "file": fname, "app": "venmo", "external_id": f"venmo:{tid}", "when": when,
            "value": value, "currency": "usd", "type": cell("type"),
            "status": cell("status"), "note": _collapse(cell("note")) or None,
            "from": _collapse(cell("from")), "to": _collapse(cell("to")),
            "funding": cell("funding source"), "destination": cell("destination"),
            "extras": {k: v for k, v in extras.items() if v not in (None, Decimal(0))}})
    return {"txns": txns, "skipped": skipped, "unmapped": unmapped,
            "examples": examples, "username": username}


def _venmo_owner(txns: list[dict], usernames: list[str], settings: dict
                 ) -> tuple[set[str], str | None, str | None]:
    """(the keys that are you, your name as the statements show it, how
    that was decided)."""
    shown: dict[str, str] = {}
    for t in txns:
        for side in (t["from"], t["to"]):
            if side:
                shown.setdefault(_key(side), side)
    wanted = {_key(n) for n in settings["names"]}
    hits = [k for k in sorted(wanted) if k in shown]
    if hits:
        how = ("[connectors.payments] me" if set(hits) & settings["from_me"]
               else "[owner] name")
        return set(hits), shown[hits[0]], how
    people = [t for t in txns if not _is_transfer(t["type"])]
    if people:
        common = set.intersection(*({_key(s) for s in (t["from"], t["to"]) if s}
                                    for t in people))
        if len(common) == 1:
            k = next(iter(common))
            return {k}, shown[k], "the name on every row of the statement"
        handles = {_letters(u) for u in usernames if u}
        by_user = [k for k in sorted(common or shown) if _letters(k) in handles]
        if len(by_user) == 1:
            k = by_user[0]
            return {k}, shown[k], "the name matching the statement's @username"
    return set(), None, None


def _venmo_sides(t: dict, me: set[str]) -> tuple[str | None, str | None]:
    """(counterparty as shown, your name as shown) for one Venmo row."""
    if _is_transfer(t["type"]):
        mine = t["from"] if _key(t["from"]) in me else t["to"] if _key(t["to"]) in me \
            else None
        return None, mine
    f_me, t_me = _key(t["from"]) in me, _key(t["to"]) in me
    if me and f_me != t_me:
        return (t["to"] if f_me else t["from"]) or None, t["from"] if f_me else t["to"]
    if me and f_me and t_me:
        return None, t["from"]
    # Who you are is not known (or you are on neither side): the sign says
    # which way the money went, and the type which column is the payer. A
    # Payment's From paid its To; a Charge's From asked its To for money.
    out = t["value"] < 0
    charge = t["type"].casefold() == "charge"
    other = (t["from"] if out else t["to"]) if charge else (t["to"] if out else t["from"])
    return other or None, None


# ---------------------------------------------------------------- PayPal

_PP_ZONE = re.compile(r"(?:UTC|GMT)?\s*(?P<sign>[+-])(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?")


def _zone_offset(text: str, home: str) -> tuple[timedelta | ZoneInfo, bool]:
    """(an offset or a zone, recognised?) for PayPal's TimeZone column."""
    s = (text or "").strip()
    up = s.upper()
    if up in _ZONE_HOURS:
        return timedelta(hours=_ZONE_HOURS[up]), True
    m = _PP_ZONE.fullmatch(up)
    if m:
        delta = timedelta(hours=int(m["h"]), minutes=int(m["m"] or 0))
        return (-delta if m["sign"] == "-" else delta), True
    if "/" in s:
        try:
            return ZoneInfo(s), True
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return ZoneInfo(home), False


_US_ZONES = {"HST", "AKST", "AKDT", "PST", "PDT", "MST", "MDT", "CST", "CDT", "EST",
             "EDT"}


def _paypal_order(rows: list[list[str]], j: int, zone_j: int, setting: str | None
                  ) -> tuple[str, str, bool]:
    """(order, how it was decided, guessed?) from every date in the file: a
    day over 12 settles it; else the setting; else US time zones on every
    row (PayPal's US site writes the month first); else month-first, and
    the report warns."""
    parts, zones = [], set()
    for row in rows:
        if j < len(row) and re.fullmatch(r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}", row[j].strip()):
            parts.append([int(x) for x in re.split(r"[./-]", row[j].strip())])
            zones.add(row[zone_j].strip().upper() if zone_j < len(row) else "")
    if any(p[0] > 999 for p in parts):
        return "ymd", "the year comes first", False
    if any(p[0] > 12 for p in parts):
        return "dmy", "a day over 12 comes first", False
    if any(p[1] > 12 for p in parts):
        return "mdy", "a day over 12 comes second", False
    if setting:
        return setting, "[connectors.payments] paypal_date_order", False
    if zones and zones <= _US_ZONES:
        return "mdy", "US time zones: PayPal's US site writes the month first", False
    return "mdy", "guessed — every date fits both", bool(parts)


def _paypal_date(text: str, order: str):
    a, b, c = (int(x) for x in re.split(r"[./-]", text.strip()))
    y, m, d = {"mdy": (c, a, b), "dmy": (c, b, a), "ymd": (a, b, c)}[order]
    return datetime(y + 2000 if y < 100 else y, m, d)


def _read_paypal(fname: str, rows: list[list[str]], start: int, index: dict,
                 settings: dict, cfg: Config) -> dict:
    skipped, unmapped, examples = Counter(), Counter(), {}
    gross_col = "gross" if "gross" in index else "amount"
    zone_col = "timezone" if "timezone" in index else "time zone"
    order, order_how, guessed = _paypal_order(rows[start + 1:], index["date"],
                                              index[zone_col], settings["paypal_order"])
    txns = []
    for lineno, row in enumerate(rows[start + 1:], start=start + 2):
        if not any(c.strip() for c in row):
            continue

        def cell(col: str) -> str:
            j = index.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        tid = cell("transaction id")
        if not tid:
            what = "row with no Transaction ID"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        status = cell("status").lower()
        if status in _NOT_DONE:
            skipped[_status_skip(status)] += 1
            continue
        if status not in _DONE:
            what = f"row with a status this reader does not know ('{cell('status')}')"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        if cell("balance impact").lower() == "memo":
            skipped["memo line with no balance impact (an authorization or hold)"] += 1
            continue
        try:
            day = _paypal_date(cell("date"), order)
            clock = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", cell("time"))
            if clock is None:
                raise ValueError("no time")
            local = day.replace(hour=int(clock[1]), minute=int(clock[2]),
                                second=int(clock[3] or 0))
        except (ValueError, TypeError):
            what = "row whose Date/Time does not read"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno} ({cell('date')} {cell('time')})")
            continue
        zone, known = _zone_offset(cell(zone_col), cfg.timezone)
        if not known:
            what = (f"time zone '{cell(zone_col)}' not recognised (read in "
                    f"{cfg.timezone})")
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
        aware = local.replace(tzinfo=zone if isinstance(zone, ZoneInfo)
                              else timezone(zone))
        when = aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            value = _money(cell(gross_col))
            fee, net = _money(cell("fee")), _money(cell("net"))
        except ValueError:
            what = "row with an amount that does not parse"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        if value is None:
            what = f"row with no {gross_col.title()}"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        currency = cell("currency").lower() or "usd"
        if not re.fullmatch(r"[a-z]{3}", currency):
            what = f"row whose Currency is not an ISO code ('{cell('currency')}')"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        extras = {"fee": fee if fee not in (None, Decimal(0)) else None,
                  "net": net if net is not None and fee not in (None, Decimal(0)) else None}
        txns.append({
            "file": fname, "app": "paypal", "external_id": f"paypal:{tid}", "when": when,
            "value": value, "currency": currency, "type": cell("type"),
            "status": cell("status"), "name": _collapse(cell("name")) or None,
            "note": _collapse(cell("note")) or None,
            "subject": _collapse(cell("subject")) or None,
            "item": _collapse(cell("item title")) or None,
            "from": cell("from email address").lower(),
            "to": cell("to email address").lower(),
            "reference": cell("reference txn id"),
            "extras": {k: v for k, v in extras.items() if v is not None}})
    return {"txns": txns, "skipped": skipped, "unmapped": unmapped, "examples": examples,
            "order": _DATE_ORDERS[order], "order_how": order_how, "guessed": guessed}


def _paypal_owner(txns: list[dict], settings: dict) -> tuple[str | None, str | None]:
    """(your email, how it was decided)."""
    seen = Counter()
    for t in txns:
        for side in (t["from"], t["to"]):
            if side in settings["emails"]:
                seen[side] += 1
    if seen:
        return sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[0][0], \
            "[connectors.payments] me / [owner] handles"
    votes = Counter((t["from"] if t["value"] < 0 else t["to"]) for t in txns
                    if not _is_transfer(t["type"]))
    votes.pop("", None)
    if votes:
        return sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0], \
            "the payer of money out and the payee of money in"
    return None, None


def _paypal_sides(t: dict, me: str | None) -> str | None:
    """The counterparty's email for one PayPal row, or None."""
    if _is_transfer(t["type"]):
        return None
    if me and (t["from"] == me) != (t["to"] == me):
        other = t["to"] if t["from"] == me else t["from"]
    else:
        other = t["to"] if t["value"] < 0 else t["from"]
    return other if other and "@" in other and other != me else None


# ---------------------------------------------------------------- records

def _title(counterparty: str | None, t: dict) -> str:
    what = t.get("note") or t.get("subject") or t.get("item")
    if counterparty and what:
        return f"{counterparty}: {what}"
    if counterparty or what:
        return counterparty or what
    if _is_transfer(t["type"]):
        where = t.get("destination") if t["value"] < 0 else t.get("funding")
        return f"{t['type']} {'to' if t['value'] < 0 else 'from'} {where}" if where \
            else t["type"]
    return t["type"] or t["app"].title()


def _record(t: dict, cfg: Config, counterparty: tuple[str, str] | None,
            shown: str | None, mine: tuple[str, str] | None) -> dict:
    value = t["value"]
    direction = "out" if value < 0 else "in" if value > 0 else None
    detail = {"app": t["app"], "type": t["type"], "status": t["status"],
              "note": t.get("note"), "funding_source": t.get("funding"),
              "destination": t.get("destination"), "subject": t.get("subject"),
              "item": t.get("item"), "reference_txn_id": t.get("reference")}
    for k, v in t["extras"].items():
        detail[k] = float(v)
    if _is_transfer(t["type"]):
        detail["transfer"] = True
    participants = []
    if counterparty:
        participants.append({"kind": counterparty[0], "value": counterparty[1],
                             "role": "counterparty"})
    if mine:
        participants.append({"kind": mine[0], "value": mine[1],
                             "role": {"out": "sender", "in": "recipient"}.get(
                                 direction, "participant")})
    return {
        "stream": "txn",
        "external_id": t["external_id"],
        "journal_date": base.journal_date_for_iso(t["when"], cfg),
        "started_at": t["when"],
        "ended_at": None,
        "utc_offset_sec": None,
        "direction": direction,
        "title": _title(shown, t),
        "body": t.get("note") or t.get("subject"),
        "value": float(value),
        "unit": t["currency"],
        "value_json": canonical_json({k: v for k, v in detail.items()
                                      if v not in (None, "")}),
        "media_path": None,
        "origin_app": t["app"],
        "last_modified_at": None,
        "participants": participants,
    }


# ---------------------------------------------------------------- targets

def _junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p.startswith(".") or p == "__MACOSX" for p in parts) or \
        PurePosixPath(rel).name.startswith("~$")


def _csv_files(target: Path) -> tuple[list[tuple[str, bytes]], list[str], str]:
    try:
        if target.is_dir():
            found, ignored = [], []
            for p in sorted(target.rglob("*"), key=lambda q: q.as_posix().lower()):
                rel = p.relative_to(target).as_posix()
                if not p.is_file() or _junk(rel):
                    continue
                (found.append((rel, p.read_bytes())) if p.suffix.lower() == ".csv"
                 else ignored.append(rel))
            return found, ignored, "folder"
        if zipfile.is_zipfile(target):
            found, ignored = [], []
            with zipfile.ZipFile(target) as zf:
                for info in sorted(zf.infolist(), key=lambda i: i.filename.lower()):
                    if info.is_dir() or _junk(info.filename):
                        continue
                    (found.append((info.filename, zf.read(info)))
                     if info.filename.lower().endswith(".csv")
                     else ignored.append(info.filename))
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
    """The `path` in [connectors.payments]: where statements are saved."""
    value = cfg.connector_settings(name).get("path")
    if not value:
        raise DomainError("payments needs a path to read — a Venmo or PayPal .csv, a "
                          "folder of them or a .zip (or set path in "
                          "[connectors.payments])")
    p = cfg.resolve(value)
    if not p.exists():
        raise DomainError(f"[connectors.payments] path does not exist: {p}")
    return p


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    settings = _settings(cfg)
    found, ignored, kind = _csv_files(target)
    if not found:
        listing = ", ".join(ignored[:8]) or "(empty)"
        raise DomainError(f"{target.name} holds no .csv statement — has: {listing}")

    venmo, paypal, files, others = [], [], [], []
    skipped, unmapped = Counter(), Counter()
    examples: dict[str, str] = {}
    usernames: list[str] = []
    warnings: list[str] = []
    for fname, raw in found:
        text, encoding = _ofx.decode(raw)
        try:
            rows = list(csv.reader(io.StringIO(text, newline="")))
        except csv.Error as e:
            if kind == "file":
                raise DomainError(f"{fname}: not readable as CSV: {e}") from e
            others.append(fname)
            continue
        sniffed = _kind(rows)
        if sniffed is None:
            if kind == "file":
                first = next((", ".join(c.strip() for c in r if c.strip())
                              for r in rows if any(c.strip() for c in r)), "")[:160]
                raise DomainError(
                    f"{fname}: not a Venmo statement or a PayPal activity download "
                    f"— first row: {first or '(empty)'}")
            others.append(fname)
            continue
        app, start, index = sniffed
        reader = _read_venmo if app == "venmo" else _read_paypal
        result = reader(fname, rows, start, index, settings, cfg)
        (venmo if app == "venmo" else paypal).extend(result["txns"])
        skipped.update(result["skipped"])
        unmapped.update(result["unmapped"])
        for k, v in result["examples"].items():
            examples.setdefault(k, v)
        whens = sorted(base.journal_date_for_iso(t["when"], cfg) for t in result["txns"])
        info = {"file": fname, "app": app, "encoding": encoding,
                "rows": len(result["txns"]),
                "first": whens[0] if whens else None, "last": whens[-1] if whens else None}
        if app == "venmo":
            if result["username"]:
                usernames.append(result["username"])
                info["username"] = "@" + result["username"]
        else:
            info["dates"] = f"{result['order']} ({result['order_how']})"
            if result["guessed"]:
                warnings.append(
                    f"{fname}: every date fits both month-first and day-first — read "
                    f"month-first; if this PayPal account writes the day first, set "
                    f"[connectors.payments] paypal_date_order = \"dmy\" before --apply")
        files.append(info)
    if not files:
        raise DomainError(f"none of the {len(found)} .csv file(s) in {target.name} is a "
                          f"Venmo statement or a PayPal activity download "
                          f"(e.g. {found[0][0]})")

    handles: dict[tuple[str, str], dict] = {}

    def seen(kind_: str, value: str, display: str | None) -> tuple[str, str]:
        k = (kind_, value.casefold())
        if k not in handles:
            handles[k] = {"kind": kind_, "value": value, "display_name": display or None}
        return kind_, handles[k]["value"]

    records, accounts, you = [], [], {}
    if venmo:
        me, me_shown, how = _venmo_owner(venmo, usernames, settings)
        if me_shown:
            accounts.append(f"venmo:{me_shown}")
            you["venmo"] = f"{me_shown} ({how})"
        else:
            warnings.append(
                "which Venmo name is yours is not known — each counterparty was read "
                "from the amount's sign and the row's type; set [connectors.payments] "
                "me = \"<your name as Venmo shows it>\" to be sure")
        for t in venmo:
            other, mine = _venmo_sides(t, me)
            cp = seen("app_account", f"venmo:{other}", other) if other else None
            own = seen("app_account", f"venmo:{mine}", mine) if mine else None
            records.append(_record(t, cfg, cp, other, own))
    if paypal:
        me_email, how = _paypal_owner(paypal, settings)
        owner_name = settings["owner_name"]
        if me_email:
            accounts.append(me_email)
            you["paypal"] = f"{me_email} ({how})"
        for t in paypal:
            other = _paypal_sides(t, me_email)
            cp = seen("email", other, t["name"]) if other else None
            own = None
            if me_email and me_email in (t["from"], t["to"]):
                own = seen("email", me_email, owner_name or "you (PayPal)")
            records.append(_record(t, cfg, cp, t["name"] if other or not
                                   _is_transfer(t["type"]) else None, own))

    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    skipped_lines = [f"{n} × {why}" for why, n in sorted(skipped.items())]
    if ignored or others:
        sample = (others or ignored)[0]
        skipped_lines.append(f"{len(ignored) + len(others)} × file that is not a Venmo "
                             f"or PayPal download (e.g. {sample})")
    dates = [r["journal_date"] for r in records]
    return base.empty_scan(
        name,
        account=", ".join(accounts) if accounts else None,
        source_file=target.name,
        source_hash=base.file_hash(target) if kind != "folder" else _content_hash(found),
        records=records,
        handles=[handles[k] for k in sorted(handles)],
        unmapped=[{"what": k, "count": v, "example": examples.get(k)}
                  for k, v in sorted(unmapped.items())],
        skipped=skipped_lines,
        # No present_ids: a statement is a window, not the account (docstring).
        facts={"files": files, "you": you, "warnings": warnings,
               "venmo_timezone": settings["venmo_zone"] if venmo else None,
               "oldest_transaction": min(dates) if dates else None,
               "newest_transaction": max(dates) if dates else None},
        target_kind=kind,
        files=files)


# ---------------------------------------------------------------- report

def _amount(value: float) -> str:
    return f"{Decimal(str(value)):+,.2f}"


def _sample(rec: dict) -> str:
    return f"{rec['journal_date']} {_amount(rec['value'])} {rec['unit']} {rec['title']}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan["facts"]
    files = facts["files"]
    kinds = Counter("Venmo statement" if f["app"] == "venmo" else "PayPal activity"
                    for f in files)
    lines = [f"payments: {scan['source_file']} ({len(files)} file(s): "
             + ", ".join(f"{k} ×{n}" for k, n in sorted(kinds.items())) + ")"]
    for f in files[:10]:
        span = f" {f['first']}..{f['last']}" if f["rows"] else ""
        extra = f", {f['username']}" if f.get("username") else \
            f", dates {f['dates']}" if f.get("dates") else ""
        lines.append(f"  file {f['file']}: {f['rows']} completed transaction(s)"
                     f"{span}{extra}")
    if len(files) > 10:
        lines.append(f"  … and {len(files) - 10} more file(s)")
    for app, who in sorted(facts["you"].items()):
        lines.append(f"  you on {app}: {who}")
    if facts["venmo_timezone"]:
        lines.append(f"  Venmo times read as {facts['venmo_timezone']} — the statement "
                     f"writes no zone ([connectors.payments] venmo_timezone)")
    for w in facts["warnings"]:
        lines.append(f"  WARNING {w}")

    new = plan["new"]
    changed = [rec for _, rec, _ in plan["updates"]]
    recs = new or changed
    if recs:
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda r: (r["started_at"], r["external_id"]))
        lines.append(f"  txn: {len(new)} new, {len(changed)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
        for unit in sorted({r["unit"] for r in recs}):
            mine = [r for r in recs if r["unit"] == unit]
            out = sum(Decimal(str(r["value"])) for r in mine if r["value"] < 0)
            inn = sum(Decimal(str(r["value"])) for r in mine if r["value"] > 0)
            lines.append(f"  {unit}: {out:+,.2f} out, {inn:+,.2f} in across these rows "
                         f"— compare with the statements' totals")
        people = Counter(p["value"] for r in recs for p in r["participants"]
                         if p["role"] == "counterparty")
        if people:
            top = sorted(people.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            lines.append("  counterparties: " + ", ".join(f"{v} ({n})" for v, n in top)
                         + (f" (+{len(people) - 5} more)" if len(people) > 5 else ""))
    else:
        lines.append("  nothing new — these statements match the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} transaction(s) in more than one file "
                    f"(overlapping statements) counted once")
    if tail:
        lines.append("  " + ", ".join(tail))

    others = [h for h in scan["handles"]
              if h["value"] not in (scan["account"] or "").split(", ")]
    if others:
        emails = sum(1 for h in others if h["kind"] == "email")
        lines.append(f"  handles: {len(others)} ({emails} by email, {len(others) - emails} "
                     f"by the name Venmo shows) — review with 'lifecrm handles list "
                     f"--suggest'")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:28]}: differs "
                     f"from the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
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

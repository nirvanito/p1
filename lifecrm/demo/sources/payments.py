"""Payments sample: the persona's money between friends, as Venmo and PayPal
hand it over.

  android   Windows laptop + Samsung: Venmo statements only — one CSV per
            calendar month that has activity, downloaded from venmo.com
            ('VenmoStatement_<Mon>_<YYYY>.csv'), with the real file's
            preamble, its unnamed first column, the balance rows above and
            below the activity, and '+ $' / '- $' amounts.
  iphone    MacBook + iPhone: the same Venmo statements, plus a PayPal
            activity download ('Download.CSV', UTF-8 with a BOM, every cell
            quoted) where Mom sends gas money for the family dinner.
  windows-iphone  Venmo statements only, as on the Windows + Samsung laptop.

Both are websites, so the phone does not decide which exists; the split
just lets each combo show something the other does not. Each combo's files
are returned separately (`write_samples`): one import per account, so each
import names its own account.

The story, from the persona's own events:

  day -20  Alex asks Jake for half the Uber home after the board
           celebration, then cancels it (a canceled Charge — skipped)
  day -9   Maya pays her half of the Uchi birthday dinner. Alex's card paid
           Uchi's $212.40 (persona.TRANSACTIONS), so the half comes back to
           Alex: Maya -> Alex, $106.20
  day -2   Jake books the lakeside place for Bled (the "Bled crew" group
           chat, 19:50) and Alex sends his share, $150.00
  day -1   Jake pays back his half of the Veracruz tacos ($29.75 on Alex's
           card): $14.88
  day 0    Sam bought two day passes for the new climbing gym (today's
           note); Alex pays him back $25.00
  iphone   day -14: Mom -> Alex on PayPal, $40.00 gas money for Sunday's
           dinner at Tía Rosa's; Alex's attempt to send it back is canceled

Every payment moves the Venmo balance, never a bank account — the demo's
bank statements stay exactly persona.TRANSACTIONS. Venmo times are written
in UTC, the reading the connector defaults to. IDs are fictional.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import persona

USERNAME = "Alex-Rivera-7"
BEGINNING_BALANCE = Decimal("212.55")     # what the Venmo balance held before


@dataclass(frozen=True)
class Payment:
    offset: int
    time: str                    # local HH:MM
    kind: str                    # Venmo Type: Payment | Charge
    status: str                  # Complete | Canceled
    note: str
    payer: str                   # person key of the From column
    payee: str                   # person key of the To column
    amount: str                  # positive; the sign is Alex's side


VENMO = (
    Payment(-20, "21:20", "Charge", "Canceled", "Uber home \N{AUTOMOBILE}", "me", "jake",
            "6.43"),
    Payment(-9, "22:40", "Payment", "Complete", "my half of Uchi \N{SUSHI}", "maya", "me",
            "106.20"),
    Payment(-2, "20:05", "Payment", "Complete",
            "Bled lakeside place \N{SNOW CAPPED MOUNTAIN} my share", "me", "jake", "150.00"),
    Payment(-1, "21:05", "Payment", "Complete", "Veracruz tacos \N{TACO}", "jake", "me",
            "14.88"),
    Payment(0, "12:31", "Payment", "Complete",
            "climbing gym day pass \N{PERSON CLIMBING}", "me", "sam", "25.00"),
)

PAYPAL = (
    Payment(-14, "18:20", "Mobile Payment", "Completed",
            "Gas money for Sunday. Drive safe, mijo \N{HEAVY BLACK HEART}", "mom", "me",
            "40.00"),
    Payment(-14, "18:45", "General Payment", "Canceled",
            "Mom, you don't have to!", "me", "mom", "40.00"),
)

_VENMO_COLUMNS = ["", "ID", "Datetime", "Type", "Status", "Note", "From", "To",
                  "Amount (total)", "Amount (tip)", "Amount (tax)", "Amount (fee)",
                  "Tax Rate", "Tax Exempt", "Funding Source", "Destination",
                  "Beginning Balance", "Ending Balance", "Statement Period Venmo Fees",
                  "Terminal Location", "Year to Date Venmo Fees", "Disclaimer"]
_DISCLAIMER = ("In case of errors or questions about your electronic transfers: "
               "This is a demo statement. Contact Venmo through the app.")

_PAYPAL_COLUMNS = [
    "Date", "Time", "TimeZone", "Name", "Type", "Status", "Currency", "Gross", "Fee",
    "Net", "From Email Address", "To Email Address", "Transaction ID",
    "Shipping Address", "Address Status", "Item Title", "Item ID",
    "Shipping and Handling Amount", "Insurance Amount", "Sales Tax", "Option 1 Name",
    "Option 1 Value", "Option 2 Name", "Option 2 Value", "Reference Txn ID",
    "Invoice Number", "Custom Number", "Quantity", "Receipt ID", "Balance",
    "Address Line 1", "Address Line 2/District/Neighborhood", "Town/City",
    "State/Province/Region/County/Territory/Prefecture/Republic", "Zip/Postal Code",
    "Country", "Contact Phone Number", "Subject", "Note", "Country Code",
    "Balance Impact"]


def _signed(p: Payment) -> Decimal:
    """The amount from Alex's side. A Payment's From pays its To; a Charge's
    From asked its To for the money, so the asker receives it."""
    amount = Decimal(p.amount)
    receives = p.payee == "me" if p.kind != "Charge" else p.payer == "me"
    return amount if receives else -amount


def _venmo_amount(value: Decimal) -> str:
    return f"{'+' if value >= 0 else '-'} ${abs(value):,.2f}"


def _csv(rows: list[list[str]], quote_all: bool = False) -> str:
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\n",
               quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL).writerows(rows)
    return buf.getvalue()


def venmo_id(anchor: date, n: int) -> str:
    return str(3_912_345_678_000_000_000 + anchor.toordinal() * 1000 + n * 37)


def _venmo_statement(month: list[tuple[int, Payment]], anchor: date,
                     opening: Decimal) -> tuple[str, Decimal]:
    blank = [""] * len(_VENMO_COLUMNS)
    rows = [[f"Account Statement - (@{USERNAME}) "] + blank[1:],
            ["Account Activity"] + blank[1:], list(_VENMO_COLUMNS)]
    first = list(blank)
    first[16] = f"${opening:,.2f}"
    rows.append(first)
    balance = opening
    for n, p in month:
        value = _signed(p)
        if p.status == "Complete":
            balance += value
        when = persona.local_dt(anchor, p.offset, p.time).astimezone(timezone.utc)
        out = value < 0
        rows.append(["", venmo_id(anchor, n), when.strftime("%Y-%m-%dT%H:%M:%S"), p.kind,
                     p.status, p.note, persona.person(p.payer).name,
                     persona.person(p.payee).name, _venmo_amount(value), "", "0", "", "0",
                     "", "Venmo balance" if out else "", "" if out else "Venmo balance",
                     "", "", "", "Venmo", "", ""])
    last = list(blank)
    last[17], last[18], last[20], last[21] = f"${balance:,.2f}", "$0.00", "$0.00", _DISCLAIMER
    rows.append(last)
    return _csv(rows), balance


def _venmo(dest: Path, anchor: date) -> Path:
    folder = dest / "venmo"
    folder.mkdir(parents=True, exist_ok=True)
    by_month: dict[tuple[int, int], list[tuple[int, Payment]]] = {}
    for n, p in enumerate(VENMO):
        d = persona.day(anchor, p.offset)
        by_month.setdefault((d.year, d.month), []).append((n, p))
    balance = BEGINNING_BALANCE
    for (year, month), items in sorted(by_month.items()):
        items.sort(key=lambda x: (x[1].offset, x[1].time))
        text, balance = _venmo_statement(items, anchor, balance)
        label = date(year, month, 1).strftime("%b_%Y")
        (folder / f"VenmoStatement_{label}.csv").write_bytes(text.encode("utf-8"))
    return folder


def paypal_id(anchor: date, n: int) -> str:
    alphabet = "0123456789ABCDEFGHJKLMNPRSTUVWXY"
    v = anchor.toordinal() * 7919 + n * 104729
    chars = []
    for _ in range(17):
        chars.append(alphabet[v % len(alphabet)])
        v = v // len(alphabet) + 31 * (n + 3)
    return "".join(chars)


def _paypal(dest: Path, anchor: date) -> Path:
    folder = dest / "paypal"
    folder.mkdir(parents=True, exist_ok=True)
    rows = [list(_PAYPAL_COLUMNS)]
    balance = Decimal("0.00")
    zone = ZoneInfo(persona.TIMEZONE)
    for n, p in sorted(enumerate(PAYPAL), key=lambda x: (x[1].offset, x[1].time),
                       reverse=True):
        local = persona.local_dt(anchor, p.offset, p.time).astimezone(zone)
        value = _signed(p)
        if p.status == "Completed":
            balance += value
        other = p.payer if p.payee == "me" else p.payee
        row = dict.fromkeys(_PAYPAL_COLUMNS, "")
        row.update({
            "Date": local.strftime("%m/%d/%Y"), "Time": local.strftime("%H:%M:%S"),
            "TimeZone": local.tzname(), "Name": persona.person(other).name,
            "Type": p.kind, "Status": p.status, "Currency": "USD",
            "Gross": f"{value:,.2f}", "Fee": "0.00", "Net": f"{value:,.2f}",
            "From Email Address": persona.person(p.payer).email,
            "To Email Address": persona.person(p.payee).email,
            "Transaction ID": paypal_id(anchor, n), "Address Status": "Non-Confirmed",
            "Quantity": "1", "Balance": f"{balance:,.2f}", "Note": p.note,
            "Balance Impact": "Credit" if value > 0 else "Debit"})
        rows.append([row[c] for c in _PAYPAL_COLUMNS])
    path = folder / "Download.CSV"
    path.write_bytes(_csv(rows, quote_all=True).encode("utf-8-sig"))
    return path


def write_samples(dest: Path, anchor: date, combo: str) -> list[Path]:
    """The combo's payment downloads, one path per account — what the owner
    would point `lifecrm observe payments` at."""
    if combo not in persona.COMBOS:
        raise ValueError(f"payments has no sample for combo '{combo}' "
                         f"(expected one of {', '.join(persona.COMBOS)})")
    dest = Path(dest)
    paths = [_venmo(dest, anchor)]
    if combo == "iphone":
        paths.append(_paypal(dest, anchor))
    return paths

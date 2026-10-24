"""Finance sample: the persona's money, as the banks hand it over.

Rendered from `persona.TRANSACTIONS` — the same tacos at Veracruz the journal
and the texts talk about, and the Lake Bled trip four years back — in each
combo's real download format:

  android   Windows laptop + Samsung: a Chase customer. Chase card CSV
            downloads ("Chase4417_Activity<from>_<to>_<made>.CSV") — the
            recent six weeks, and the one saved after the Bled trip, which
            Chase no longer offers for download four years on — plus the
            checking account's CSV, where the paychecks land.
  iphone    MacBook + iPhone: Apple Card's monthly CSV exports from Wallet
            (purchases positive, Apple's own Merchant column), plus a
            credit union checking account's Quicken download — a v1 SGML
            QFX, the dialect most banks still serve.
  windows-iphone  the same files: Wallet exports the Apple Card CSVs on the
            iPhone (saved to OneDrive), and the QFX is the bank's website's.

The purchases go on the card and the paychecks into checking, the way a
real person's statements split them. No card payment between the two is
invented: the demo's money is exactly the persona's, so "how much did I
spend" sums the same on both combos.

The categories in the files are what each bank would print, written here
the way the bank's own categoriser would; the connector stores them as
`bank_category` and never derives one of its own. Card and account numbers
are fictional.

Apple only exports *closed* statements; the in-progress month is included
anyway so both combos show the same last six weeks.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import persona

CHASE_CARD = "4417"
CHASE_CHECKING = "8812"
CU_ORG, CU_FID, CU_ROUTING = "HCCU", "7311", "999999999"
CU_ACCOUNT = "000123458812"
OPENING_BALANCE = Decimal("3180.42")     # checking, before the window

# persona merchant text -> (Chase category, Apple merchant, Apple category,
# Apple's raw description)
_MERCHANTS = {
    "MOVEMENT GYM AUSTIN TX": (
        "Health & Wellness", "Movement Gym", "Other",
        "MOVEMENT GYM 2600 E 5TH ST AUSTIN 78702 TX USA"),
    "CHIPOTLE 2231 AUSTIN TX": (
        "Food & Drink", "Chipotle", "Restaurants",
        "CHIPOTLE 2231 AUSTIN 78701 TX USA"),
    "VERACRUZ ALL NATURAL": (
        "Food & Drink", "Veracruz All Natural", "Restaurants",
        "VERACRUZ ALL NATURAL 1704 E CESAR CHAVEZ ST AUSTIN 78702 TX USA"),
    "H-E-B #742 AUSTIN TX": (
        "Groceries", "H-E-B", "Grocery",
        "H-E-B #742 1801 E 51ST ST AUSTIN 78723 TX USA"),
    "USPS PASSPORT FEE": (
        "Bills & Utilities", "USPS", "Other",
        "USPS PASSPORT FEE WASHINGTON 20520 DC USA"),
    "UCHI AUSTIN": (
        "Food & Drink", "Uchi", "Restaurants",
        "UCHI 801 S LAMAR BLVD AUSTIN 78704 TX USA"),
    "AUSTIN CLAY STUDIO": (
        "Shopping", "Austin Clay Studio", "Shopping",
        "AUSTIN CLAY STUDIO AUSTIN 78702 TX USA"),
    "AIR FRANCE AUS-LJU": (
        "Travel", "Air France", "Airlines", "AIR FRANCE 0572104588120 PARIS FR"),
    "VILA BLED HOTEL BLED SI": (
        "Travel", "Vila Bled", "Hotels", "VILA BLED HOTEL CESTA SVOBODE 26 BLED SI"),
    "PLETNA BLED SI": (
        "Travel", "Pletna Bled", "Travel", "PLETNA BLED 4260 BLED SI"),
}


def _merchant(text: str) -> tuple[str, str, str, str]:
    return _MERCHANTS.get(text, ("Shopping", text.title(), "Other", text))


def _dated(anchor: date) -> list[tuple[date, persona.Txn]]:
    trip = persona.bled_trip_days(anchor)
    out = []
    for t in persona.TRANSACTIONS:
        when = trip[t.trip_day or 0] if t.offset is None else persona.day(anchor, t.offset)
        out.append((when, t))
    return sorted(out, key=lambda x: (x[0], x[1].merchant, x[1].amount))


def _is_trip(t: persona.Txn) -> bool:
    return t.offset is None


def _us(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def _csv(rows: list[list[str]], quote_all: bool = False) -> bytes:
    buf = io.StringIO(newline="")
    w = csv.writer(buf, quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL,
                   lineterminator="\r\n")
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _amount(value: float) -> str:
    return f"{Decimal(str(value)):.2f}"


# ---------------------------------------------------------------- android: Chase

def _chase_card(txns: list[tuple[date, persona.Txn]]) -> bytes:
    rows = [["Transaction Date", "Post Date", "Description", "Category", "Type",
             "Amount", "Memo"]]
    for when, t in sorted(txns, key=lambda x: (x[0], x[1].merchant), reverse=True):
        posted = when + timedelta(days=2 if _is_trip(t) else 1)
        rows.append([_us(when), _us(posted), t.merchant, _merchant(t.merchant)[0],
                     "Sale", _amount(t.amount), t.memo or ""])
    return _csv(rows)


def _chase_checking(deposits: list[tuple[date, persona.Txn]]) -> bytes:
    rows = [["Details", "Posting Date", "Description", "Amount", "Type", "Balance",
             "Check or Slip #"]]
    balance, lines = OPENING_BALANCE, []
    for when, t in sorted(deposits, key=lambda x: x[0]):
        balance += Decimal(str(t.amount))
        # Chase pads the ACH company name and appends the originator id, and
        # ends every data row with one empty field the header does not have.
        lines.append(["CREDIT", _us(when), f"{t.merchant}       PPD ID: 4567890123",
                      _amount(t.amount), "ACH_CREDIT", f"{balance:.2f}", "", ""])
    return _csv(rows + list(reversed(lines)))


def _android(dest: Path, anchor: date) -> Path:
    folder = dest / "finance-android"
    folder.mkdir(parents=True, exist_ok=True)
    dated = _dated(anchor)
    recent = [(d, t) for d, t in dated if not _is_trip(t) and t.amount < 0]
    trip = [(d, t) for d, t in dated if _is_trip(t)]
    deposits = [(d, t) for d, t in dated if not _is_trip(t) and t.amount > 0]

    start = persona.day(anchor, -(persona.WINDOW_DAYS - 1))
    name = f"Chase{CHASE_CARD}_Activity{start:%Y%m%d}_{anchor:%Y%m%d}_{anchor:%Y%m%d}.CSV"
    (folder / name).write_bytes(_chase_card(recent))

    trip_days = persona.bled_trip_days(anchor)
    t_from, t_to = trip_days[0] - timedelta(days=7), trip_days[-1] + timedelta(days=7)
    made = t_to + timedelta(days=1)
    name = f"Chase{CHASE_CARD}_Activity{t_from:%Y%m%d}_{t_to:%Y%m%d}_{made:%Y%m%d}.CSV"
    (folder / name).write_bytes(_chase_card(trip))

    (folder / f"Chase{CHASE_CHECKING}_Activity_{anchor:%Y%m%d}.CSV").write_bytes(
        _chase_checking(deposits))
    return folder


# ---------------------------------------------------------------- iphone: Apple Card + QFX

def _apple_card(txns: list[tuple[date, persona.Txn]]) -> bytes:
    rows = [["Transaction Date", "Clearing Date", "Description", "Merchant",
             "Category", "Type", "Amount (USD)", "Purchased By"]]
    for when, t in sorted(txns, key=lambda x: (x[0], x[1].merchant), reverse=True):
        _, merchant, category, raw = _merchant(t.merchant)
        rows.append([_us(when), _us(when + timedelta(days=1)), raw, merchant, category,
                     "Purchase", _amount(-t.amount), persona.OWNER.name])
    return _csv(rows, quote_all=True)


def _ofx_time(d: date) -> str:
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=ZoneInfo(persona.TIMEZONE))
    hours = int(noon.utcoffset().total_seconds() // 3600)
    return f"{d:%Y%m%d}120000.000[{hours}:{noon.tzname()}]"


def _qfx(deposits: list[tuple[date, persona.Txn]], anchor: date) -> bytes:
    start = persona.day(anchor, -(persona.WINDOW_DAYS - 1))
    balance = OPENING_BALANCE + sum((Decimal(str(t.amount)) for _, t in deposits),
                                    Decimal(0))
    head = ["OFXHEADER:100", "DATA:OFXSGML", "VERSION:102", "SECURITY:NONE",
            "ENCODING:USASCII", "CHARSET:1252", "COMPRESSION:NONE",
            "OLDFILEUID:NONE", "NEWFILEUID:NONE", ""]
    body = ["<OFX>", "<SIGNONMSGSRSV1>", "<SONRS>", "<STATUS>", "<CODE>0",
            "<SEVERITY>INFO", "</STATUS>", f"<DTSERVER>{_ofx_time(anchor)}",
            "<LANGUAGE>ENG", "<FI>", f"<ORG>{CU_ORG}", f"<FID>{CU_FID}", "</FI>",
            f"<INTU.BID>{CU_FID}", "</SONRS>", "</SIGNONMSGSRSV1>",
            "<BANKMSGSRSV1>", "<STMTTRNRS>", "<TRNUID>1", "<STATUS>", "<CODE>0",
            "<SEVERITY>INFO", "</STATUS>", "<STMTRS>", "<CURDEF>USD",
            "<BANKACCTFROM>", f"<BANKID>{CU_ROUTING}", f"<ACCTID>{CU_ACCOUNT}",
            "<ACCTTYPE>CHECKING", "</BANKACCTFROM>", "<BANKTRANLIST>",
            f"<DTSTART>{_ofx_time(start)}", f"<DTEND>{_ofx_time(anchor)}"]
    for n, (when, t) in enumerate(sorted(deposits, key=lambda x: x[0]), start=1):
        body += ["<STMTTRN>", "<TRNTYPE>DIRECTDEP", f"<DTPOSTED>{_ofx_time(when)}",
                 f"<TRNAMT>{_amount(t.amount)}", f"<FITID>{when:%Y%m%d}{n:04d}",
                 f"<NAME>{t.merchant}", f"<MEMO>{(t.memo or '').upper()}",
                 "</STMTTRN>"]
    body += ["</BANKTRANLIST>", "<LEDGERBAL>", f"<BALAMT>{balance:.2f}",
             f"<DTASOF>{_ofx_time(anchor)}", "</LEDGERBAL>", "</STMTRS>",
             "</STMTTRNRS>", "</BANKMSGSRSV1>", "</OFX>", ""]
    return "\r\n".join(head + body).encode("ascii")


def _iphone(dest: Path, anchor: date) -> Path:
    folder = dest / "finance-iphone"
    folder.mkdir(parents=True, exist_ok=True)
    dated = _dated(anchor)
    by_month: dict[tuple[int, int], list] = {}
    for when, t in dated:
        if t.amount < 0:
            by_month.setdefault((when.year, when.month), []).append((when, t))
    for (year, month), txns in sorted(by_month.items()):
        label = date(year, month, 1).strftime("%B %Y")
        (folder / f"Apple Card Transactions - {label}.csv").write_bytes(_apple_card(txns))
    deposits = [(d, t) for d, t in dated if t.amount > 0]
    (folder / f"{CU_ORG}_Checking_{anchor:%Y%m%d}.qfx").write_bytes(_qfx(deposits, anchor))
    return folder


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """The combo's statement downloads, in one folder — what the owner would
    point `lifecrm observe finance` at."""
    if combo == "android":
        return _android(Path(dest), anchor)
    if combo in ("iphone", "windows-iphone"):
        # Apple Card's CSVs come off the iPhone's Wallet whichever computer
        # they are saved for; the credit union's QFX is a website download.
        return _iphone(Path(dest), anchor)
    raise ValueError(f"finance has no sample for combo '{combo}' "
                     f"(expected one of {', '.join(persona.COMBOS)})")

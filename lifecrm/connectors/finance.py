"""Finance connector: bank and card statements — CSV, OFX, QFX and QBO.

Where the money went is the most honest diary there is, and every bank will
hand it over for free: a CSV download, or an OFX/QFX file for Quicken. This
connector reads those files. It never logs in anywhere, never calls an
aggregator, and never sends anything.

Accepted targets
----------------
- one statement file (.csv, .ofx, .qfx, .qbo — sniffed by content, not name)
- a folder of them, searched recursively: statements saved month by month
- a .zip of them
- nothing at all, when `path` is set in `[connectors.finance]`

CSV formats are recognised by their header row, and each encodes its bank's
sign convention — getting that wrong silently turns every purchase into
income, so the dry run prints the largest money in and out to check:

  Chase checking   Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
  Chase card       Transaction Date,Post Date,Description,Category,Type,Amount,Memo
  Apple Card       Transaction Date,Clearing Date,Description,Merchant,Category,
                   Type,Amount (USD),Purchased By        purchases POSITIVE
  Capital One      Transaction Date,Posted Date,Card No.,Description,Category,
                   Debit,Credit                          separate columns
  Amex             Date,Description,Amount (+ the detailed export's extra
                   columns)                              charges POSITIVE
  Discover         Trans. Date,Post Date,Description,Amount,Category
                                                         purchases POSITIVE
  Citi             Status,Date,Description,Debit,Credit[,Member Name]
  Bank of America  Date,Description,Amount,Running Bal. (after the summary
                   block) and Posted Date,Reference Number,Payee,Address,Amount
  Wells Fargo      no header: date, amount, '*', check number, description

Everything else is spend-negative. A bank not listed is described once in
`[connectors.finance]` (date_col, amount_col or debit_col/credit_col,
description_col, date_format, sign = 'spend_negative' | 'spend_positive',
currency, institution, account, delimiter, match). The mapping is tried
before the presets, so it can claim a header two banks share; `match`, a
file-name glob, keeps it off the files a preset should read. Amounts may
carry '$', thousands commas, accounting parentheses, a trailing CR/DR, or a
decimal comma; ';'- and tab-separated files are recognised by their lines.

What comes in
-------------
One observation per transaction, stream `txn`:

  value       the signed amount in the account's currency — negative is
              money out, whatever the file's own convention was
  unit        lowercase ISO currency ('usd')
  direction   'out' / 'in' (None for a zero-amount line)
  title       the merchant text as the bank wrote it, whitespace runs
              collapsed — Apple's own Merchant column where it has one
  body        the memo
  value_json  bank_category (only what the bank said), type, account,
              posted_date, check_no, raw_description (when it differs from
              the title), and per-format extras: card_member, reference,
              details, sic, original_currency, currency_rate

Nothing is categorised, normalised or merged here. "VERACRUZ ALL NATURAL"
and "SQ *VERACRUZ ALL NAT" stay two spellings of the bank's text: deciding
they are one restaurant is a derived, regenerable layer, and storing that
guess as the record is exactly what the observed tier exists to avoid.

Dates: statements carry dates, not times. A transaction is placed at local
noon of the date the bank gives (the transaction date where the file has
one, else the posting date), and its journal date is that date — noon
because it survives every rollover hour and timezone shift without moving
the day. `started_at` is therefore a placeholder time, never an instant.

Identity
--------
- OFX/QFX: the bank's FITID, which the spec makes unique within an account:
  `ofx:<institution>:<last4>:<FITID>`.
- CSV has no ids, so one is derived: sha1 of the account, the transaction
  date, the amount, the description and an ordinal among identical rows of
  that day in the file. Overlapping exports (January, then mid-January to
  February) therefore produce the same ids for the same rows, and the second
  import is a no-op for the overlap. The ordinal is what keeps two identical
  $2.75 bus fares on one day as two transactions.
- A pending transaction is not the posted one: its amount (a tip added) or
  its date can still change, and it would get a different id. Citi marks
  pending rows and they are skipped and counted; OFX's pending list
  (STMTTRNP) is too. Export after things post.
- Pick one format per account. A CSV and an OFX of the same account share
  no id, so importing both counts every transaction twice.

Handles: the card, not the merchant
-----------------------------------
Each identified account becomes one `card` handle (`chase:4417`; the full
number is never kept — only the last four), participant 'sender' of money
out and 'recipient' of money in. Linking it once attributes every
transaction on it — useful the moment a partner's card is imported too.

Merchants get no handles, deliberately. A handle is a stable machine
identifier; a statement description is not one — the same restaurant
arrives as 'VERACRUZ ALL NATURAL', 'SQ *VERACRUZ ALL NAT' and a store-number
variant per location, and two businesses can share a processor's text.
Making handles of them would mean either normalising (the derived layer,
again) or one handle per spelling: hundreds of rows in `handles list`,
ranked by count, so the grocery store visited weekly outranks your best
friend's phone number in the queue whose whole point is O(contacts) review.
And handles are never deleted — a retracted import leaves them behind. What
merchant handles would buy (attaching Veracruz the place to its spend) is
served by the title instead: full-text search finds every spelling.

Deletion semantics — remember, never mirror
-------------------------------------------
A statement is a window, not the account's whole history: the transaction
that is absent from this month's export is in last month's. Nothing is ever
removed because a later file does not contain it. `imports retract` undoes
a run that should not have happened.

Known limitations, by design
----------------------------
- The last four digits come from the file where the format has them: OFX
  ACCTID, Capital One's Card No., Amex's Account #, and the names Chase
  (`Chase1234_Activity...`) and Bank of America's card download
  (`October2026_1234.csv`) give their files. A renamed file, or a format
  that never says (Discover, Citi, Wells Fargo, the basic Amex export),
  leaves the account unnamed: no card handle, and two cards of one bank
  with an identical charge on the same day would count once.
- Amex's basic export names no account and its detailed one does, so the
  same charge gets a different id from each: use the detailed export (it
  names the card) and stick to it.
- `card_member` (Amex's Card Member, Apple's Purchased By, Citi's Member
  Name) is the bank's text, kept as detail. It is never a participant: who
  holds a card is the card handle's link, decided once by the owner.
- OFX corrections: a REPLACE is imported as its own transaction (noting the
  FITID it corrects) and a DELETE is reported, never applied — the original
  stays until you retract it.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import _ofx, base

name = "finance"
streams = ("txn",)
# Statements come monthly; nothing newer in six weeks means the habit stopped.
stale_after_days = 45

STATEMENT_SUFFIXES = (".csv", ".ofx", ".qfx", ".qbo")
SIGNS = ("spend_negative", "spend_positive")
PENDING = "pending transaction (imported once it posts)"

_HEADER_SCAN = 30          # rows searched for a header (BoA puts a summary first)


# ---------------------------------------------------------------- money & dates

_MINUS = "-\u2212\u2012\u2013"     # hyphen, minus sign, figure dash, en dash


def parse_amount(text) -> tuple[Decimal | None, str | None]:
    """(amount in the file's own sign convention, 'CR' | 'DR' | None).

    Accepts '$1,234.56', '-$5.00', '$-5.00', '(34.50)' (accounting
    negative), '34.50-', '34.50 CR' / '34.50 DR', 'USD 12.00', '34,50' and
    '1.234,56' (decimal commas). An empty cell is (None, None); text that is
    not an amount raises ValueError. CR/DR is returned separately because it
    states the direction outright and overrides any convention.
    """
    s = str(text if text is not None else "").replace("\u00a0", " ").strip()
    if not s:
        return None, None
    marker = None
    m = re.search(r"\s*(CR|DR)\.?$", s, re.I)
    if m and m.start() > 0:
        marker, s = m.group(1).upper(), s[:m.start()].strip()
    s = re.sub(r"^[A-Z]{3}\s*|\s*[A-Z]{3}$", "", s, flags=re.I)
    s = re.sub("[$\u20ac\u00a3\u00a5]", "", s).strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s and s[0] in "+" + _MINUS:
        negative ^= s[0] in _MINUS
        s = s[1:].strip()
    if s.endswith(tuple(_MINUS)):
        negative, s = not negative, s[:-1].strip()
    s = s.replace(" ", "")
    if re.fullmatch(r"\d+,\d{1,2}", s):
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{1,2}", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        raise ValueError(f"not an amount: {text!r}")
    try:
        value = Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"not an amount: {text!r}") from e
    return (-value if negative else value), marker


_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%m-%d-%Y",
                 "%m-%d-%y", "%Y%m%d", "%Y.%m.%d", "%d %b %Y", "%d %B %Y",
                 "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%d-%b-%Y", "%d-%b-%y")
_TIME_TAIL = re.compile(r"[ T]\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?\s*([AaPp]\.?[Mm]\.?)?"
                        r"\s*(Z|[+-]\d{2}:?\d{2})?$")


def parse_date(text, fmt: str | None = None) -> date | None:
    """A statement date, or None for an empty cell; ValueError otherwise.

    Without `fmt`, ISO is tried first and then US month-first forms: these
    are US banks' exports, and 03/04/2026 is the 4th of March to all of
    them. A day-first bank names its `date_format` in the mapping. A
    trailing time ('2026-10-20T00:00:00', '10/20/2026 12:00:00 AM') is
    dropped — no statement time means anything.
    """
    s = " ".join(str(text if text is not None else "").split())
    if not s:
        return None
    candidates = [s, _TIME_TAIL.sub("", s)] if fmt else [_TIME_TAIL.sub("", s)]
    for value in candidates:
        for f in ((fmt,) if fmt else _DATE_FORMATS):
            try:
                return datetime.strptime(value, f).date()
            except ValueError:
                continue
    raise ValueError(f"not a date: {text!r}")


def _collapse(text) -> str:
    return " ".join(str(text or "").split())


def _norm(cell: str) -> str:
    return " ".join(cell.replace("\ufeff", "").strip().strip('"').lower().split())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _last4(text: str | None) -> str | None:
    s = re.sub(r"[^0-9A-Za-z]", "", text or "")
    return s[-4:] if s else None


# ---------------------------------------------------------------- formats

@dataclass(frozen=True)
class Format:
    """One bank's CSV layout. Column names are normalised (lower-case,
    single-spaced); `columns` must all be present, and for a preset nothing
    outside `columns` + `optional` may be — a header that only resembles a
    known one is refused rather than misread."""
    key: str
    label: str
    institution: str              # prefix of the account label, and origin_app
    columns: tuple[str, ...]
    optional: tuple[str, ...] = ()
    sign: str = "spend_negative"  # convention of the single amount column
    date: str = "date"
    posted: str | None = None
    amount: str | None = "amount"
    debit: str | None = None
    credit: str | None = None
    description: str = "description"
    merchant: str | None = None
    memo: str | None = None
    category: str | None = None
    type: str | None = None
    check: str | None = None
    account: str | None = None    # column holding the card/account number
    member: str | None = None
    reference: str | None = None
    status: str | None = None
    details: str | None = None
    file_account: str | None = None   # regex on the file name capturing the last 4
    own_account: bool = False         # the format is one account (Apple Card)
    fixed_account: str | None = None  # a mapping's configured account
    holder: str = ""                  # the account's display name, before 'ending 1234'
    currency: str = "usd"
    date_format: str | None = None
    exact: bool = True                # header must not carry unknown columns
    file_glob: str | None = None      # a mapping can be limited to some file names

    def holder_name(self, last4: str | None) -> str:
        stem = self.holder or self.label
        return f"{stem} ending {last4}" if last4 and not self.own_account else stem

    def sign_text(self) -> str:
        if self.amount is None:
            return "debit/credit columns"
        return "purchases positive" if self.sign == "spend_positive" else "spend negative"


_CHASE_FILE = r"^chase(\d{4})_activity"

FORMATS: tuple[Format, ...] = (
    Format("chase_checking", "Chase checking", "chase",
           ("details", "posting date", "description", "amount", "type", "balance",
            "check or slip #"),
           date="posting date", type="type", check="check or slip #",
           details="details", file_account=_CHASE_FILE),
    Format("chase_card", "Chase card", "chase",
           ("transaction date", "post date", "description", "category", "type",
            "amount", "memo"),
           date="transaction date", posted="post date", category="category",
           type="type", memo="memo", file_account=_CHASE_FILE),
    Format("apple_card", "Apple Card", "apple_card",
           ("transaction date", "clearing date", "description", "merchant",
            "category", "type", "amount (usd)", "purchased by"),
           sign="spend_positive", date="transaction date", posted="clearing date",
           amount="amount (usd)", merchant="merchant", category="category",
           type="type", member="purchased by", own_account=True),
    Format("capital_one", "Capital One card", "capital_one",
           ("transaction date", "posted date", "card no.", "description",
            "category", "debit", "credit"),
           date="transaction date", posted="posted date", amount=None,
           debit="debit", credit="credit", category="category", account="card no."),
    Format("discover", "Discover card", "discover",
           ("trans. date", "post date", "description", "amount", "category"),
           sign="spend_positive", date="trans. date", posted="post date",
           category="category"),
    Format("citi", "Citi card", "citi",
           ("status", "date", "description", "debit", "credit"),
           optional=("member name",), amount=None, debit="debit", credit="credit",
           status="status", member="member name"),
    Format("bofa_card", "Bank of America card", "bofa",
           ("posted date", "reference number", "payee", "address", "amount"),
           date="posted date", description="payee", reference="reference number",
           file_account=r"^[a-z]+\d{4}_(\d{4})\.csv$"),
    Format("bofa_checking", "Bank of America checking", "bofa",
           ("date", "description", "amount", "running bal.")),
    Format("amex", "American Express card", "amex",
           ("date", "description", "amount"),
           optional=("card member", "account #", "extended details",
                     "appears on your statement as", "address", "city/state",
                     "zip code", "country", "reference", "category"),
           sign="spend_positive", memo="extended details", category="category",
           account="account #", member="card member", reference="reference"),
)

# Wells Fargo's download has no header at all; these are its five positions.
WELLS_FARGO = Format("wells_fargo", "Wells Fargo", "wells_fargo",
                     ("date", "amount", "*", "check", "description"), check="check",
                     holder="Wells Fargo account")
_WELLS_FARGO_COLUMNS = ("date", "amount", "*", "check", "description")

_MAPPING_KEYS = ("date_col", "posted_col", "amount_col", "debit_col", "credit_col",
                 "description_col", "memo_col", "category_col", "type_col",
                 "check_col")


def _mapping(settings: dict) -> Format | None:
    """The owner's own column mapping from [connectors.finance], or None."""
    cols = {k: _norm(str(settings[k])) for k in _MAPPING_KEYS if settings.get(k)}
    if not cols:
        return None
    where = "[connectors.finance]"
    missing = [k for k in ("date_col", "description_col") if k not in cols]
    has_split = bool({"debit_col", "credit_col"} & set(cols))
    if "amount_col" not in cols and not has_split:
        missing.append("amount_col (or debit_col/credit_col)")
    if missing:
        raise DomainError(f"{where} maps a CSV but is missing {', '.join(missing)}")
    if "amount_col" in cols and has_split:
        raise DomainError(f"{where} names amount_col and debit_col/credit_col — "
                          f"give one or the other")
    sign = str(settings.get("sign") or "spend_negative").strip().lower()
    if sign not in SIGNS:
        raise DomainError(f"{where} sign must be 'spend_negative' or "
                          f"'spend_positive', got {settings.get('sign')!r}")
    currency = str(settings.get("currency") or "usd").strip().lower()
    if not re.fullmatch(r"[a-z]{3}", currency):
        raise DomainError(f"{where} currency must be a three-letter ISO code "
                          f"like 'usd', got {settings.get('currency')!r}")
    institution = _slug(str(settings.get("institution") or "")) or "csv"
    account = str(settings.get("account") or "").strip()
    if account:
        # An account number is cut to its last four; a label is kept as a slug.
        account = _last4(account) if re.fullmatch(r"[\d\s-]+", account) \
            else _slug(account)
    required = tuple(cols[k] for k in ("date_col", "description_col", "amount_col",
                                       "debit_col", "credit_col") if k in cols)
    label = " ".join(str(settings.get("label") or settings.get("institution")
                         or institution).split())
    return Format(
        "mapping", f"{label} CSV (mapping)", institution, required,
        sign=sign, date=cols["date_col"],
        posted=cols.get("posted_col"), amount=cols.get("amount_col"),
        debit=cols.get("debit_col"), credit=cols.get("credit_col"),
        description=cols["description_col"], memo=cols.get("memo_col"),
        category=cols.get("category_col"), type=cols.get("type_col"),
        check=cols.get("check_col"), currency=currency,
        date_format=str(settings["date_format"]) if settings.get("date_format") else None,
        fixed_account=account or None, holder=f"{label} account", exact=False,
        file_glob=str(settings["match"]).lower() if settings.get("match") else None)


def _match(names: list[str], mapping: Format | None, fname: str) -> Format | None:
    have = set(names)
    own = mapping is not None and (
        mapping.file_glob is None
        or fnmatch.fnmatchcase(PurePosixPath(fname).name.lower(), mapping.file_glob))
    candidates = ((mapping,) if own else ()) + FORMATS
    for f in candidates:
        need = set(f.columns)
        if need <= have and (not f.exact or have <= need | set(f.optional)):
            return f
    return None


def _looks_wells_fargo(row: list[str]) -> bool:
    cells = [c.strip() for c in row]
    while cells and not cells[-1]:
        cells.pop()
    if len(cells) != 5 or cells[2] not in ("*", ""):
        return False
    try:
        return parse_date(cells[0]) is not None and parse_amount(cells[1])[0] is not None
    except ValueError:
        return False


# ---------------------------------------------------------------- CSV

def _delimiter(text: str, configured: str | None) -> str:
    """',' unless the mapping names one, or the first lines plainly use ';'
    or tabs instead (European banks' exports)."""
    if configured:
        return "\t" if configured.lower() in ("\\t", "tab") else configured[0]
    head = "\n".join(text.splitlines()[:5])
    best = max((";", "\t"), key=head.count)
    return best if head.count(best) > head.count(",") else ","


def _read_csv(fname: str, raw: bytes, mapping: Format | None,
              delimiter: str | None = None) -> dict:
    text, encoding = _ofx.decode(raw)
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""),
                               delimiter=_delimiter(text, delimiter)))
    except csv.Error as e:
        raise DomainError(f"{fname}: not readable as CSV: {e}") from e
    if not any(any(c.strip() for c in r) for r in rows):
        raise DomainError(f"{fname} is empty")

    fmt, start, index, first = None, 0, {}, None
    for i, row in enumerate(rows[:_HEADER_SCAN]):
        names = [n for n in (_norm(c) for c in row) if n]
        if not names:
            continue
        if first is None:
            first = (i, row)
        fmt = _match(names, mapping, fname)
        if fmt is not None:
            start = i + 1
            for j, cell in enumerate(row):
                index.setdefault(_norm(cell), j)
            break
    if fmt is None:
        if first is not None and _looks_wells_fargo(first[1]):
            fmt, start = WELLS_FARGO, first[0]
            index = {c: j for j, c in enumerate(_WELLS_FARGO_COLUMNS)}
        else:
            seen = ", ".join(c.strip() for c in (first[1] if first else ())
                             if c.strip())[:160]
            raise DomainError(
                f"{fname}: not a statement export this connector knows — first "
                f"row: {seen} (known: Chase checking/card, Apple Card, Capital One, "
                f"Amex, Discover, Citi, Bank of America, Wells Fargo; or map its "
                f"columns in [connectors.finance])")

    file_last4 = None
    if fmt.file_account:
        m = re.match(fmt.file_account, PurePosixPath(fname).name, re.I)
        file_last4 = m.group(1) if m else None

    txns, skipped, unmapped = [], Counter(), Counter()
    examples: dict[str, str] = {}
    for lineno, row in enumerate(rows[start:], start=start + 1):
        if not any(c.strip() for c in row):
            continue

        def cell(col: str | None, raw_text: bool = False) -> str:
            j = index.get(col) if col else None
            if j is None or j >= len(row):
                return ""
            return row[j] if raw_text else row[j].strip()

        status = cell(fmt.status).lower()
        if status == "pending":
            skipped[PENDING] += 1
            continue
        try:
            when = parse_date(cell(fmt.date) or cell(fmt.posted), fmt.date_format)
            if when is None:
                raise ValueError("no date")
        except ValueError:
            unmapped["row with no readable date"] += 1
            examples.setdefault("row with no readable date", f"{fname}:{lineno}")
            continue
        try:
            value = _signed_value(fmt, cell)
        except ValueError:
            unmapped["row with an amount that does not parse"] += 1
            examples.setdefault("row with an amount that does not parse",
                                f"{fname}:{lineno}")
            continue
        description = _collapse(cell(fmt.description))
        if value is None:
            if re.match(r"(beginning|ending) balance", description, re.I):
                skipped["balance line"] += 1
            else:
                unmapped["row with no amount"] += 1
                examples.setdefault("row with no amount", f"{fname}:{lineno}")
            continue
        try:
            posted = parse_date(cell(fmt.posted), fmt.date_format) \
                if fmt.posted and fmt.posted != fmt.date else None
        except ValueError:
            posted = None
        last4 = _last4(cell(fmt.account)) or file_last4 or fmt.fixed_account
        account = f"{fmt.institution}:{last4}" if last4 else fmt.institution
        raw_desc = cell(fmt.description, raw_text=True).strip()
        title = _collapse(cell(fmt.merchant)) or description
        reference = cell(fmt.reference).strip("'\"")
        txns.append({
            "file": fname, "origin": fmt.institution, "date": when, "posted": posted,
            "value": value, "currency": fmt.currency, "id_text": description,
            "title": title or None, "memo": cell(fmt.memo) or None,
            "account": account, "identified": bool(last4) or fmt.own_account,
            "holder": fmt.holder_name(last4),
            "detail": {
                "bank_category": cell(fmt.category), "type": cell(fmt.type),
                "check_no": cell(fmt.check),
                "raw_description": raw_desc if raw_desc != title else None,
                "card_member": _collapse(cell(fmt.member)),
                "reference": reference, "details": cell(fmt.details)},
            "row": tuple(c.strip() for c in row),
        })
    _assign_csv_ids(txns)
    dates = [t["date"] for t in txns]
    return {"txns": txns, "skipped": skipped, "unmapped": unmapped,
            "examples": examples,
            "file": {"file": fname, "kind": "csv", "format": fmt.label,
                     "sign": fmt.sign_text(), "encoding": encoding,
                     "rows": len(txns),
                     "first": min(dates).isoformat() if dates else None,
                     "last": max(dates).isoformat() if dates else None,
                     "accounts": sorted({t["account"] for t in txns})}}


def _signed_value(fmt: Format, cell) -> Decimal | None:
    """The amount with money out negative, or None when the row has none."""
    if fmt.amount is not None:
        amount, marker = parse_amount(cell(fmt.amount))
        if amount is None:
            return None
        if marker == "CR":
            value = abs(amount)
        elif marker == "DR":
            value = -abs(amount)
        else:
            value = amount if fmt.sign == "spend_negative" else -amount
    else:
        debit, _ = parse_amount(cell(fmt.debit))
        credit, _ = parse_amount(cell(fmt.credit))
        if debit is None and credit is None:
            return None
        # Banks disagree on whether a credit column holds 500 or -500; the
        # column says which way the money went, so only its size counts.
        value = (abs(credit) if credit is not None else Decimal(0)) - \
            (abs(debit) if debit is not None else Decimal(0))
    return abs(value) if value == 0 else value


def _assign_csv_ids(txns: list[dict]) -> None:
    """sha1(account, date, amount, description, ordinal) — see the module
    docstring. The ordinal counts identical rows of one day within one file,
    ordered by their full content so a bank that sorts newest-first one month
    and oldest-first the next still yields the same ids."""
    groups: dict[tuple, list[dict]] = {}
    for t in txns:
        key = (t["account"], t["date"].isoformat(), f"{t['value']:.2f}", t["id_text"])
        groups.setdefault(key, []).append(t)
    for key, members in groups.items():
        for ordinal, t in enumerate(sorted(members, key=lambda x: x["row"])):
            digest = hashlib.sha1("|".join((*key, str(ordinal))).encode("utf-8"))
            t["external_id"] = f"csv:{key[0]}:{digest.hexdigest()[:24]}"


# ---------------------------------------------------------------- OFX

def _read_ofx(fname: str, raw: bytes) -> dict:
    parsed = _ofx.statements(raw, fname)
    org = parsed["org"] or parsed["fid"]
    institution = _slug(org or "") or "ofx"
    txns, skipped, unmapped = [], Counter(), Counter()
    examples: dict[str, str] = {}
    if parsed["investment"]:
        unmapped["investment statement (holdings and trades are not imported)"] += \
            parsed["investment"]
    accounts = set()
    coverage = []
    for st in parsed["statements"]:
        last4 = _last4(st["acctid"])
        account = f"{institution}:{last4}" if last4 else institution
        kind = "card" if st["kind"] == "card" else (st["accttype"] or "account").lower()
        holder = f"{org or 'OFX'} {kind}" + (f" ending {last4}" if last4 else "")
        currency = (st["currency"] or "USD").strip().lower()
        # The statement says which days it covers (BANKTRANLIST DTSTART/DTEND):
        # inside them a day without a row is a day without a transaction on
        # this account. A CSV says nothing of the kind (docs/assistant.md).
        if st.get("dtstart") and st.get("dtend") and st["dtstart"] <= st["dtend"]:
            coverage.append({"stream": "txn", "account": account,
                             "start": st["dtstart"], "end": st["dtend"],
                             "basis": "OFX statement range (BANKTRANLIST DTSTART/DTEND)"})
        if st["pending"]:
            skipped[PENDING] += st["pending"]
        for n, tx in enumerate(st["transactions"]):
            action = (tx.get("CORRECTACTION") or "").upper()
            if action == "DELETE":
                what = "OFX correction deleting an earlier transaction (not applied)"
                unmapped[what] += 1
                examples.setdefault(what, f"{fname}: FITID {tx.get('CORRECTFITID')}")
                continue
            try:
                when = date.fromisoformat(_ofx.ofx_date(tx.get("DTUSER"))
                                          or _ofx.ofx_date(tx.get("DTPOSTED")) or "")
                posted_iso = _ofx.ofx_date(tx.get("DTPOSTED"))
                posted = date.fromisoformat(posted_iso) if posted_iso else None
            except ValueError:
                unmapped["transaction with no readable date"] += 1
                examples.setdefault("transaction with no readable date",
                                    f"{fname}: FITID {tx.get('FITID')}")
                continue
            try:
                amount, _ = parse_amount(tx.get("TRNAMT"))
            except ValueError:
                amount = None
            if amount is None:
                unmapped["transaction with no readable TRNAMT"] += 1
                examples.setdefault("transaction with no readable TRNAMT",
                                    f"{fname}: FITID {tx.get('FITID')}")
                continue
            if amount == 0:
                amount = abs(amount)
            raw_name = tx.get("NAME") or tx.get("PAYEE.NAME") or tx.get("EXTDNAME") or ""
            memo = tx.get("MEMO")
            title = _collapse(raw_name) or _collapse(memo)
            if action == "REPLACE":
                unmapped["OFX correction replacing an earlier transaction "
                         "(imported as its own record; the original stays)"] += 1
            fitid = (tx.get("FITID") or "").strip()
            accounts.add(account)
            txns.append({
                "file": fname, "origin": institution, "date": when,
                "posted": posted if posted != when else None,
                "value": amount,
                "currency": (tx.get("CURRENCY.CURSYM") or currency).strip().lower(),
                "id_text": title, "title": title or None,
                "memo": memo if memo and _collapse(memo) != title else None,
                "account": account, "identified": bool(last4), "holder": holder,
                "external_id": f"ofx:{account}:{fitid}" if fitid else None,
                "detail": {
                    "type": tx.get("TRNTYPE"), "check_no": tx.get("CHECKNUM"),
                    "reference": tx.get("REFNUM"), "sic": tx.get("SIC"),
                    "raw_description": raw_name if raw_name and raw_name != title
                    else None,
                    "original_currency": (tx.get("ORIGCURRENCY.CURSYM") or "").lower(),
                    "currency_rate": tx.get("CURRENCY.CURRATE")
                    or tx.get("ORIGCURRENCY.CURRATE"),
                    "corrects": tx.get("CORRECTFITID") if action == "REPLACE" else None},
                "row": (str(n), *sorted(f"{k}={v}" for k, v in tx.items())),
            })
    # A FITID is mandatory in OFX; a file without one falls back to the CSV rule.
    no_id = [t for t in txns if t["external_id"] is None]
    _assign_csv_ids(no_id)
    for t in no_id:
        t["external_id"] = "ofx:" + t["external_id"][len("csv:"):]
    dates = [t["date"] for t in txns]
    return {"txns": txns, "skipped": skipped, "unmapped": unmapped,
            "examples": examples, "coverage": coverage,
            "file": {"file": fname, "kind": "ofx",
                     "format": f"OFX v{parsed['version']}"
                               + (f" from {org}" if org else ""),
                     "sign": "negative = money out", "encoding": parsed["encoding"],
                     "rows": len(txns),
                     "first": min(dates).isoformat() if dates else None,
                     "last": max(dates).isoformat() if dates else None,
                     "accounts": sorted(accounts)}}


# ---------------------------------------------------------------- records

def _record(t: dict, cfg: Config) -> dict:
    noon = datetime.combine(t["date"], time(12), ZoneInfo(cfg.timezone))
    value = t["value"]
    direction = "out" if value < 0 else "in" if value > 0 else None
    detail = {k: v for k, v in t["detail"].items() if v not in (None, "")}
    detail["account"] = t["account"]
    if t["posted"] is not None:
        detail["posted_date"] = t["posted"].isoformat()
    participants = []
    if t["identified"]:
        participants.append({
            "kind": "card", "value": t["account"],
            "role": {"out": "sender", "in": "recipient"}.get(direction, "participant")})
    return {
        "stream": "txn",
        "external_id": t["external_id"],
        "journal_date": t["date"].isoformat(),
        "started_at": noon.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ended_at": None,
        "utc_offset_sec": None,
        "direction": direction,
        "title": t["title"],
        "body": t["memo"],
        "value": float(value),
        "unit": t["currency"],
        "value_json": canonical_json(detail),
        "media_path": None,
        "origin_app": t["origin"],
        "last_modified_at": None,
        "participants": participants,
    }


# ---------------------------------------------------------------- targets

def _junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p.startswith(".") or p == "__MACOSX" for p in parts) or \
        PurePosixPath(rel).name.startswith("~$")


def _statement_files(target: Path) -> tuple[list[tuple[str, bytes]], list[str], str]:
    """([(name, bytes)], [ignored names], 'folder' | 'zip' | 'file')."""
    try:
        if target.is_dir():
            found, ignored = [], []
            for p in sorted(target.rglob("*")):
                rel = p.relative_to(target).as_posix()
                if not p.is_file() or _junk(rel):
                    continue
                if p.suffix.lower() in STATEMENT_SUFFIXES:
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
                    if PurePosixPath(info.filename).suffix.lower() in STATEMENT_SUFFIXES:
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
    """The `path` in [connectors.finance]: where statements are saved."""
    value = cfg.connector_settings(name).get("path")
    if not value:
        raise DomainError("finance needs a path to read — a statement .csv/.ofx/.qfx, "
                          "a folder of them or a .zip (or set path in "
                          "[connectors.finance])")
    p = Path(str(value)).expanduser()
    p = p if p.is_absolute() else cfg.root / p
    if not p.exists():
        raise DomainError(f"[connectors.finance] path does not exist: {p}")
    return p


def scan(target: Path, cfg: Config) -> base.Scan:
    settings = cfg.connector_settings(name)
    mapping = _mapping(settings)
    delimiter = str(settings["delimiter"]) if settings.get("delimiter") else None
    found, ignored, kind = _statement_files(target)
    if not found:
        listing = ", ".join(ignored[:8]) or "(empty)"
        raise DomainError(f"{target.name} holds no statement export "
                          f"(.csv, .ofx, .qfx, .qbo) — has: {listing}")

    txns, files, errors = [], [], []
    coverage: list[dict] = []
    skipped, unmapped = Counter(), Counter()
    examples: dict[str, str] = {}
    for fname, raw in found:
        try:
            result = _read_ofx(fname, raw) if _ofx.looks_like_ofx(raw) \
                else _read_csv(fname, raw, mapping, delimiter)
        except DomainError as e:
            if kind == "file":
                raise
            errors.append(str(e))
            continue
        txns += result["txns"]
        coverage += result.get("coverage", [])
        files.append(result["file"])
        skipped.update(result["skipped"])
        unmapped.update(result["unmapped"])
        for k, v in result["examples"].items():
            examples.setdefault(k, v)
    if not files:
        raise DomainError(errors[0] if len(errors) == 1 else
                          f"none of the {len(errors)} file(s) in {target.name} is a "
                          f"statement this connector reads — first: {errors[0]}")

    records = sorted((_record(t, cfg) for t in txns),
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    holders = {t["account"]: t["holder"] for t in txns if t["identified"]}
    handles = [{"kind": "card", "value": acct, "display_name": holders[acct]}
               for acct in sorted(holders)]
    accounts = sorted({t["account"] for t in txns})
    skipped_lines = [f"{n} × {why}" for why, n in sorted(skipped.items())]
    if ignored:
        skipped_lines.append(f"{len(ignored)} × file that is not a statement export "
                             f"(e.g. {ignored[0]})")
    unmapped_list = [{"what": k, "count": v, "example": examples.get(k)}
                     for k, v in sorted(unmapped.items())]
    unmapped_list += [{"what": e, "count": 1, "example": None} for e in errors]
    dates = [r["journal_date"] for r in records]
    return base.empty_scan(
        name,
        account=", ".join(accounts) if accounts else None,
        source_file=target.name,
        source_hash=base.file_hash(target) if kind != "folder" else _content_hash(found),
        records=records,
        handles=handles,
        unmapped=unmapped_list,
        skipped=skipped_lines,
        facts={"files": files, "accounts": accounts,
               "oldest_transaction": min(dates) if dates else None,
               "newest_transaction": max(dates) if dates else None},
        coverage=coverage,
        target_kind=kind,
        files=files,
        unnamed=sorted({t["file"] for t in txns if not t["identified"]}))


# ---------------------------------------------------------------- report

def _money(value: float) -> str:
    return f"{Decimal(str(value)):+,.2f}"


def _sample(rec: dict) -> str:
    return (f"{rec['journal_date']} {_money(rec['value'])} {rec['unit']} "
            f"{rec['title'] or '(no description)'}")


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    files = scan["files"]
    kinds = Counter(f["format"] for f in files)
    lines = [f"finance: {scan['source_file']} ({len(files)} statement file(s): "
             + ", ".join(f"{k} ×{n}" for k, n in sorted(kinds.items())) + ")"]
    for f in files[:10]:
        span = f" {f['first']}..{f['last']}" if f["rows"] else ""
        lines.append(f"  file {f['file']}: {f['format']} ({f['sign']}), "
                     f"{f['rows']} transaction(s){span}, account "
                     f"{', '.join(f['accounts']) or 'not named'}")
    if len(files) > 10:
        lines.append(f"  … and {len(files) - 10} more file(s)")

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
            lines.append(f"  {unit}: {out:+,.2f} out, {inn:+,.2f} in across these "
                         f"rows — compare with the statements' totals")
        biggest_out = min(recs, key=lambda r: (r["value"], r["external_id"]))
        biggest_in = max(recs, key=lambda r: (r["value"], r["external_id"]))
        check = []
        if biggest_out["value"] < 0:
            check.append(f"out {_sample(biggest_out)}")
        if biggest_in["value"] > 0:
            check.append(f"in {_sample(biggest_in)}")
        if check:
            lines.append("  largest: " + "; ".join(check) + " — if a purchase reads "
                         "as money in, the sign convention is backwards")
    if not recs:
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

    for h in scan["handles"]:
        lines.append(f"  handle: card:{h['value']} ({h['display_name']}) — link it to "
                     f"whoever holds it: lifecrm handles link card:{h['value']} "
                     f"--entity <id>")
    if scan["unnamed"]:
        lines.append(f"  no account number in {', '.join(scan['unnamed'][:3])}"
                     + (f" (+{len(scan['unnamed']) - 3} more)"
                        if len(scan["unnamed"]) > 3 else "")
                     + " — no card handle; same-day identical charges on two cards "
                       "of one bank would count once (docs/finance.md)")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:28]}…: differs "
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

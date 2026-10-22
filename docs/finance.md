# Bank and card statements

Where the money went is the most honest diary there is: the dinner at
Veracruz, the gym membership, the flights to Ljubljana four years ago. Every
bank hands it over for free as a CSV download or an OFX/QFX file for
Quicken. The `finance` connector reads those files. It never logs in to a
bank, never calls an aggregator, and never sends anything — zero accounts,
zero recurring cost.

Statements land in the **observed tier**: bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited.
Nothing here can touch `entries`, `measurements` or `mentions`. See
[docs/observed-tier.md](observed-tier.md) for why.

## Getting the exports

Download from the bank's website on the laptop — on both combos. Phone
banking apps rarely export anything.

| bank | where | file |
|---|---|---|
| Chase (checking and card) | Account → *Download account activity* → CSV | `Chase1234_Activity….CSV` — **keep the name**: the last four digits of the card come from it |
| Apple Card | iPhone: Wallet → Apple Card → Card Balance → a monthly statement → *Export Transactions* → CSV (or OFX). Mac: card.apple.com → Statements | one file per closed month |
| Capital One | Transactions → *Download* → CSV | the card number is a column |
| American Express | Statements & Activity → *Download* → CSV, **with "include all additional transaction details"** | `activity.csv` — the detailed export names the card; the basic one does not |
| Discover, Citi, Bank of America, Wells Fargo | Activity → *Download* → CSV | |
| any bank with Quicken / QuickBooks support | *Download* → QFX, OFX or QBO | carries the bank's own transaction ids |

Prefer OFX/QFX when the bank offers it: it is the only format with a real
transaction id. But pick **one format per account** — a CSV and an OFX of
the same account share no id, and importing both counts every transaction
twice.

Save statements into one folder as you download them, month after month.
Overlapping downloads are fine: see "Re-importing" below.

## Importing

Dry-run first — always. The sign convention is the thing to check: a wrong
one silently turns every purchase into income.

```bash
.venv/Scripts/python -m lifecrm observe finance statements/
```

```
finance: statements (3 statement file(s): Chase card ×2, Chase checking ×1)
  file Chase4417_Activity20220919_20221007_20221008.CSV: Chase card (spend negative), 3 transaction(s) 2022-09-26..2022-09-28, account chase:4417
  file Chase4417_Activity20260910_20261021_20261021.CSV: Chase card (spend negative), 12 transaction(s) 2026-09-11..2026-10-20, account chase:4417
  file Chase8812_Activity_20261021.CSV: Chase checking (spend negative), 2 transaction(s) 2026-09-23..2026-10-07, account chase:8812
  txn: 17 new, 0 changed | 2022-09-26..2026-10-20 | e.g. 2026-10-20 -29.75 usd VERACRUZ ALL NATURAL
  usd: -2,809.87 out, +8,400.00 in across these rows — compare with the statements' totals
  largest: out 2022-09-26 -1,124.60 usd AIR FRANCE AUS-LJU; in 2026-09-23 +4,200.00 usd ACME ROBOTICS PAYROLL PPD ID: 4567890123 — if a purchase reads as money in, the sign convention is backwards
  handle: card:chase:4417 (Chase card ending 4417) — link it to whoever holds it: lifecrm handles link card:chase:4417 --entity <id>
  handle: card:chase:8812 (Chase checking ending 8812) — link it to whoever holds it: lifecrm handles link card:chase:8812 --entity <id>
dry run — nothing written. Re-run with --apply to write.
```

Read three things: each file's **format** (is it the bank you downloaded
from?), the **totals** (do they match the statement?), and the **largest**
line (is the paycheck "in" and the flight "out"?). Then write:

```bash
.venv/Scripts/python -m lifecrm observe finance statements/ --apply
```

The command prints an import uuid; `lifecrm imports retract <uuid>` undoes
the whole run.

Accepted targets: one statement file, a folder of them (searched
recursively), or a `.zip`. In a folder or zip only `.csv`, `.ofx`, `.qfx`
and `.qbo` files are read — anything else is counted and ignored — and each
is recognised by its content, not its suffix. With the folder set once in
`config.toml`, the path can be left off:

```toml
[connectors.finance]
path = "~/Documents/Statements"     # relative paths are under the project root
```

## Formats it knows

Recognised by the header row. A header with a column the preset does not
know is refused rather than guessed at — say so, or map it (below).

| format | header | sign in the file |
|---|---|---|
| Chase checking | `Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #` | spend negative |
| Chase card | `Transaction Date,Post Date,Description,Category,Type,Amount,Memo` | spend negative |
| Apple Card | `Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD),Purchased By` | **purchases positive** |
| Capital One | `Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit` | separate columns |
| American Express | `Date,Description,Amount` (+ the detailed export's columns) | **charges positive** |
| Discover | `Trans. Date,Post Date,Description,Amount,Category` | **purchases positive** |
| Citi | `Status,Date,Description,Debit,Credit[,Member Name]` | separate columns |
| Bank of America checking | `Date,Description,Amount,Running Bal.` after the summary block | spend negative |
| Bank of America card | `Posted Date,Reference Number,Payee,Address,Amount` | spend negative |
| Wells Fargo | no header: date, amount, `*`, check number, description | spend negative |
| OFX / QFX / QBO | v1 SGML or v2 XML; bank (`STMTRS`) and card (`CCSTMTRS`) statements | negative = money out |

Amounts may be written `$1,234.56`, `(34.50)`, `34.50-`, `34.50 CR` /
`34.50 DR` (which state the direction outright), `USD 12.00`, or with a
decimal comma (`34,50`, `1.234,56`). Dates are tried as ISO, then US
month-first (`03/04/2026` is March 4th), then spelled-out months. `;`- and
tab-separated files are recognised by their first lines. Both UTF-8 (with
or without a BOM) and Windows-1252 files are read.

### A bank that is not listed

Describe its CSV once in `config.toml`:

```toml
[connectors.finance]
date_col = "Booked"
description_col = "Payee"
debit_col = "Money Out"            # or amount_col = "Amount" for one signed column
credit_col = "Money In"
memo_col = "Notes"                 # optional, as are category_col, type_col,
                                   # posted_col and check_col
date_format = "%d/%m/%Y"           # optional: day-first banks need it
sign = "spend_negative"            # or "spend_positive" when purchases are positive
currency = "gbp"                   # default "usd"
institution = "First Local"        # names the account: first_local:4321
account = "87654321"               # optional: cut to its last four
delimiter = ";"                    # optional: ',' / ';' / tab are detected anyway
match = "first-local*.csv"         # optional: only files named like this
```

The mapping is tried before the presets, so it can claim a header a preset
shares — `Date,Description,Amount` is Amex's (charges positive) and also
plenty of credit unions' (spend negative). Give it `match` if you import
that preset's bank too, or the mapping reads both.

## What gets imported

**One observation per transaction**, stream `txn`:

| field | holds |
|---|---|
| `value` | the signed amount in the account's currency — **negative is money out**, whatever the file's own convention was |
| `unit` | lowercase ISO currency, `usd` |
| `direction` | `out` / `in` (none for a zero-amount line) |
| `title` | the merchant as the bank wrote it, whitespace runs collapsed — Apple's own Merchant column where it has one |
| `body` | the memo |
| `value_json` | `bank_category` (only what the bank said), `type`, `account`, `posted_date`, `check_no`, `raw_description` (when it differs from the title), and per format `card_member`, `reference`, `details`, `sic`, `original_currency`, `currency_rate` |
| `origin_app` | the bank: `chase`, `apple_card`, `amex`, … or the OFX institution |

**Nothing is categorised.** `bank_category` is the bank's own label, kept
only where the file has one; the connector never derives a category, never
normalises a merchant name, and never merges two spellings of one shop.
Those are derived, regenerable interpretations — a later layer can compute
them from these rows without them ever being stored as the record.

**Dates, not times.** Statements carry no time of day. Each transaction is
placed at **local noon** on the date the bank gives — the transaction date
where the file has one (Chase card, Apple, Capital One, Discover, OFX
`DTUSER`), else the posting date — and its journal date is that date. Noon
survives every rollover hour and daylight-saving shift without moving the
day. `started_at` is therefore a placeholder, never an instant: do not read
"12:00" as when you paid.

**Never kept:** the running balance (it changes with every same-day reorder
and would make overlapping exports conflict), full account numbers (only
the last four), and anything from investment statements (holdings and
trades are reported, not imported).

## Re-importing, and what wins

- **OFX/QFX** rows are keyed on the bank's FITID: `ofx:<bank>:<last4>:<FITID>`.
- **CSV** rows have no id, so one is derived: a hash of the account, the
  transaction date, the amount, the description, and an *ordinal* among
  identical rows that day in the file. Two downloads that overlap —
  January, then mid-January to February — give the overlapping rows the
  same ids, and the second import skips them as identical. The ordinal is
  what keeps two identical $2.75 bus fares on one day as two transactions.
  A folder holding both downloads counts the overlap once, and the report
  says so.

A **pending** transaction is not the posted one: a tip changes the amount,
posting can move the date, and either gives it a new id. Citi marks its
pending rows and OFX lists them separately; both are skipped and counted.
For other banks, download after things post.

A row whose data changed under the same id (a bank re-sending a FITID with
a new name) is a **conflict**: never applied, surfaced in the report.
Statements carry no edit clock, so nothing is ever "newer".

## Deleted transactions: the archive remembers

A statement is a window, not the account's whole history — the transaction
missing from this month's download is in last month's. So nothing is ever
removed because a later file does not contain it (there is no mirroring,
unlike texts and AI chats). `lifecrm imports retract <uuid>` undoes a run
that should not have happened.

## Attribution: the card, not the merchant

Each identified account becomes **one `card` handle** — `card:chase:4417`,
`card:apple_card`, `card:hccu:8812` — participant *sender* of money out and
*recipient* of money in. Link it once and every transaction on that card is
attributed, past and future:

```bash
.venv/Scripts/python -m lifecrm handles list
.venv/Scripts/python -m lifecrm handles link card:chase:4417 --entity <your id>
```

That matters the moment a partner's card is imported too.

**Merchants get no handles**, deliberately:

- A handle is a stable machine identifier; a statement description is not
  one. The same restaurant arrives as `VERACRUZ ALL NATURAL`,
  `SQ *VERACRUZ ALL NAT` and a store-number variant per location, and two
  businesses can share a payment processor's text.
- Making handles of them means either normalising (the derived layer again)
  or one handle per spelling — hundreds of rows in `handles list`, ranked by
  count, so the grocery store visited weekly outranks your best friend's
  phone number in the queue whose point is a few hundred decisions about
  *people*. Handles are never deleted, either: retracting the import would
  leave them behind.
- What merchant handles would buy — Veracruz the place attached to its
  spend — is served by the title: full-text search finds every spelling,
  and a question like "how much did I spend at Veracruz" is answered from
  those rows, citing each one.

`card_member` (Amex's Card Member, Apple's Purchased By, Citi's Member Name)
is the bank's text, kept as detail. It is never turned into a participant.

`lifecrm doctor` warns while a card handle is unlinked, and — once statements
are in use — when the newest transaction is more than 45 days old
(`finance_data_age`): statements come monthly, so a gap that long means the
download habit stopped.

## Known limitations

- **Accounts the file does not name.** The last four digits come from OFX
  `ACCTID`, Capital One's *Card No.*, the detailed Amex export's *Account #*,
  and the file names Chase (`Chase1234_Activity…`) and Bank of America's
  card download (`October2026_1234.csv`) use. A renamed file, or a format
  that never says (Discover, Citi, Wells Fargo, the basic Amex export), has
  no card handle — and two cards of one bank with an identical charge on the
  same day would count once. The report lists such files.
- **Basic vs detailed Amex.** The basic export names no account and the
  detailed one does, so they give the same charge different ids. Use the
  detailed export and stick to it.
- **OFX corrections.** A `REPLACE` is imported as its own transaction,
  noting the FITID it corrects; a `DELETE` is reported and never applied —
  the original stays until you retract it.
- **Timezone of the date.** A charge made abroad carries the date the bank
  printed, which may be the bank's day rather than the local one.
- **Memory.** Files are read whole; a decade of statements is a few MB.

## First real import — check these

The presets were written from the banks' documented and commonly seen
download formats, and the demo renders them, but none has yet been run
against a real download from this owner's accounts. On the first real file
of each bank, check in the dry run:

1. the format line names the right bank and sign convention;
2. the totals match the statement's own totals;
3. the "largest" line has the paycheck in and a purchase out;
4. Chase: the account shows `chase:<last4>` (the file name was kept);
5. Apple Card: titles are Apple's cleaned merchant names;
6. OFX: the account's last four and the bank name are right.

Anything off — say so; a fixed preset is a one-line change.

## Commands

```bash
.venv/Scripts/python -m lifecrm connectors                       # what's available, last run
.venv/Scripts/python -m lifecrm observe finance <file|folder|zip> # dry run
.venv/Scripts/python -m lifecrm observe finance <...> --apply
.venv/Scripts/python -m lifecrm observe finance                   # the [connectors.finance] path
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
.venv/Scripts/python -m lifecrm handles list
.venv/Scripts/python -m lifecrm handles link card:<bank>:<last4> --entity <id>
```

On the Mac the interpreter is `.venv/bin/python`; everything else is the same.

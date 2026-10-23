# Venmo and PayPal (money between people)

A bank statement says `VENMO PAYMENT 1023849` and stops there. Venmo and
PayPal know who the money went to and why — "Jake Morales: Veracruz
tacos" — and both hand that over as a CSV from their website. The
`payments` connector reads those files. It never logs in, never calls an
API, and never sends anything.

Payments land in the **observed tier**, in the same `txn` stream as
[bank statements](finance.md) and with the same meaning, so `ask`'s money
answers read both. Nothing here can touch `entries`, `measurements` or
`mentions`. See [docs/observed-tier.md](observed-tier.md) for why.

## Getting the downloads

Both are website downloads, on the laptop — on both combos. Neither phone
app exports a CSV.

| service | where | file |
|---|---|---|
| Venmo | venmo.com → sign in → **Statements** (account.venmo.com/statement) → a month, or a custom range → **Download CSV** | one statement per download, e.g. `VenmoStatement_Oct_2026.csv` |
| PayPal | paypal.com → **Activity** → *Statements* → **Activity report** (older accounts: *Reports* → *Activity download*) → a date range, *All transactions*, **CSV** → *Create report*, then download it when it is ready | `Download.CSV` (or a report name) |

Save them into one folder as you go; overlapping ranges are fine (see
"Re-importing"). The folder can be the one your bank statements live in —
files that are not Venmo or PayPal downloads are counted and left alone.

**Never open and re-save a Venmo CSV in a spreadsheet.** Excel turns the
19-digit transaction ID into `3.91E+18`, and the digits are gone; such
rows are reported, not imported. Re-download the file.

*Unverified without a real download:* the menu wording on both sites, the
default file names, and whether Venmo's `Datetime` column is UTC (see
"Dates and times").

## Importing

Dry-run first — always:

```bash
.venv/Scripts/python -m lifecrm observe payments ~/Documents/Statements/venmo
```

```
payments: venmo (1 file(s): Venmo statement ×1)
  file VenmoStatement_Oct_2026.csv: 4 completed transaction(s) 2026-10-12..2026-10-21, @Alex-Rivera-7
  you on venmo: Alex Rivera (the name on every row of the statement)
  Venmo times read as UTC — the statement writes no zone ([connectors.payments] venmo_timezone)
  txn: 4 new, 0 changed | 2026-10-12..2026-10-21 | e.g. 2026-10-21 -25.00 usd Sam Okafor: climbing gym day pass 🧗
  usd: -175.00 out, +121.08 in across these rows — compare with the statements' totals
  counterparties: venmo:Jake Morales (2), venmo:Maya Chen (1), venmo:Sam Okafor (1)
  handles: 3 (0 by email, 3 by the name Venmo shows) — review with 'lifecrm handles list --suggest'
  skipped: 1 × canceled transaction (only completed ones are imported)
  skipped: 2 × statement summary row (balances, fees, disclaimer)
dry run — nothing written. Re-run with --apply to write.
```

Check the **you on …** line (your own side is never a counterparty) and
that money you sent reads negative. Then:

```bash
.venv/Scripts/python -m lifecrm observe payments ~/Documents/Statements/venmo --apply
```

Accepted targets: one `.csv`, a folder of them (searched recursively), or
a `.zip`. Each file is recognised by its header, not its name.

### Settings

```toml
[connectors.payments]
path = "~/Documents/Statements"     # what 'observe payments' reads with no path
me = ["Alex Rivera", "alex.rivera@example.com"]   # your Venmo name / PayPal email
# venmo_timezone = "America/Chicago" # only if Venmo's times turn out to be local
# paypal_date_order = "dmy"          # only if the dry run says it had to guess
```

None is required. Without `me`, `[owner] name` and `[owner] handles` are
used, and then the file itself: on Venmo the name on every row (or the one
matching the statement's `@username`); on PayPal the payer of money out and
the payee of money in, which the signed Gross states outright.

## The formats

**Venmo statement.** A few preamble rows (`Account Statement -
(@username)`, `Account Activity`), then

```
,ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (tip),Amount (tax),Amount (fee),Tax Rate,Tax Exempt,Funding Source,Destination,Beginning Balance,Ending Balance,Statement Period Venmo Fees,Terminal Location,Year to Date Venmo Fees,Disclaimer
```

— the first column has no name and no values. The rows above and below the
activity hold only balances, fees and the disclaimer; they are counted as
statement summary rows. Amounts read `+ $25.00` / `- $1,234.56` from your
side. Older statements name the first column `Username`; that reads too.

**PayPal activity download.** One quoted header row, usually with a BOM:
`Date, Time, TimeZone, Name, Type, Status, Currency, Gross, Fee, Net, From
Email Address, To Email Address, Transaction ID, …, Subject, Note, …,
Balance Impact`. Dates follow the account's locale: a day over 12 settles
the order; else `paypal_date_order`; else month-first when every row's
zone is a US one (PayPal's US site writes the month first); else
month-first with a WARNING in the dry run. Zones are abbreviations (`PDT`,
`CET`, …), offsets (`GMT+01:00`) or IANA names; an unknown one is read in
your home timezone and reported.

## What gets imported

**One observation per completed transaction**, stream `txn`:

| field | holds |
|---|---|
| `value` | the signed amount (Venmo's *Amount (total)*, PayPal's *Gross*) — **negative is money out** |
| `unit` | lowercase ISO currency (`usd`; Venmo is always USD) |
| `direction` | `out` / `in` |
| `title` | the counterparty and the note: `Jake Morales: Veracruz tacos` — a transfer: `Standard Transfer to Chase Bank *8812` |
| `body` | the note (PayPal: the note, else the subject) |
| `value_json` | `app` (`venmo`/`paypal`), `type`, `status`, `note`, `funding_source`, `destination`; non-zero `tip`, `tax`, `fee` (Venmo) or `fee`, `net`, `subject`, `item`, `reference_txn_id` (PayPal); `transfer: true` for money moved between your own accounts |
| `origin_app` | `venmo` / `paypal` |

**Not imported, counted:** pending, failed, declined, denied, canceled and
other unfinished rows — that payment never happened, or not yet; PayPal's
`Memo` lines (authorizations and holds, no balance impact); statement
summary rows. A status this reader does not know (PayPal's `Reversed`, say)
is reported under *not understood*, never guessed into the archive.

**Transfers** — Venmo cash-outs and top-ups, PayPal bank deposits,
withdrawals, card funding and currency conversions — are imported with
`transfer: true` and no counterparty. They really moved the account, but
they are not money paid to anyone, and the bank statement shows the same
money arriving or leaving: a spending total should leave them out.

## Counterparties: handles, never names

- **PayPal** names the other side by email: an `email` handle, role
  `counterparty`, with PayPal's *Name* as its display name. It is the same
  handle a contact card, a calendar invite or a LinkedIn connection
  carries, so one link attributes all of them.
- **Venmo's statement has no ids for people** — only the name their profile
  shows. The counterparty becomes `app_account venmo:<name as shown>`
  (`venmo:Jake Morales`), with that name as its display name. It is the
  export's only identifier: a friend who renames their Venmo profile
  becomes a new handle, and two people with the same display name share
  one (link it to whoever it is; the note usually tells).
- **Your own side** is never a counterparty. It is the account's handle
  (`venmo:Alex Rivera`, your PayPal email), participant `sender` of money
  out and `recipient` of money in — the bank card's rule — so linking it
  once marks every row as yours.

Link a counterparty once and every payment with them resolves:

```bash
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link "app_account:venmo:Jake Morales" --entity <Jake's id>
```

## Dates and times

PayPal's times are local to the zone each row names, and stored as UTC.
**Venmo writes no zone at all.** Its times are read as **UTC** — the
service's own clock — which is unverified: if a payment you made at 9 pm
shows up at 4 pm in the dry run's sample line, the statement is local; set
`venmo_timezone` to your zone *before* `--apply` (a later change shifts
every row and arrives as conflicts). Journal dates follow your home
timezone and the rollover hour, like every source.

## Re-importing, and what wins

Each service's own id is the key: `venmo:<ID>`, `paypal:<Transaction ID>`.
Overlapping statements — October, then mid-October to November — import
the overlap once (`counted once` in the report). A row whose data changed
under the same id is a **conflict**: surfaced, never applied. Statements
carry no edit clock, so nothing is ever "newer".

## Nothing is removed: the archive remembers

A statement is a window — a month, a chosen range — not the account's
whole history. Nothing is removed because a later file does not hold it.
`lifecrm imports retract <uuid>` undoes a run that should not have happened.

## Known limitations

- **Double counting with the bank.** A payment funded from a bank account
  or card shows on that statement too (`VENMO PAYMENT …`). Summing every
  `txn` counts it twice; this connector cannot see the bank side.
- **Fees.** `value` is the gross — what the other person paid or was paid.
  PayPal's fee and net are kept in `value_json` when there is a fee.
- **Merchants.** Venmo merchant payments name the business as shown, the
  same way as a person, and PayPal merchants have an email: they are
  handles to link or `handles ignore`.
- **No categories, no merging.** As with bank statements, deciding that two
  counterparties are one person, or what a payment was for, is a derived
  layer — never the record.

## First real import — check these

Written from the services' documented and commonly seen downloads, and
rendered by the demo, but not yet run against this owner's real files. On
the first of each, check in the dry run:

1. the file is recognised (`Venmo statement` / `PayPal activity`);
2. **you on …** names you, and no counterparty is you;
3. the out/in totals match the statement's own;
4. Venmo: the sample line's time matches when you paid (UTC or local?);
5. PayPal: the dates read in the right order (no WARNING, or set
   `paypal_date_order`);
6. `not understood:` lists no status that matters.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe payments <file|folder|zip>          # dry run
.venv/Scripts/python -m lifecrm observe payments <...> --apply
.venv/Scripts/python -m lifecrm observe payments                            # the [connectors.payments] path
.venv/Scripts/python -m lifecrm handles list --suggest
.venv/Scripts/python -m lifecrm handles link "app_account:venmo:<name>" --entity <id>
.venv/Scripts/python -m lifecrm handles link email:<address> --entity <id>
.venv/Scripts/python -m lifecrm imports retract <uuid>
```

On the Mac the interpreter is `.venv/bin/python`; everything else is the same.

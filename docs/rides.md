# Uber rides and Uber Eats orders

Where you went by car, and when, is a thread through a life the journal
rarely mentions: the ride to Uchi for a birthday dinner, the car to the
airport for Lake Bled, the late order on a long work night. Uber will send
the whole history as a data export. The `rides` connector reads it. It
never logs in, never calls an API, and never sends anything.

Rides and orders land in the **observed tier**: bulk records that never
pass the `/log` review gate, are never asserted as fact, and are always
cited. Nothing here can touch `entries`, `measurements` or `mentions`. See
[docs/observed-tier.md](observed-tier.md) for why.

## Getting the export

The same request from either phone or the laptop:

- **Laptop** — myprivacy.uber.com (the *Privacy Center*) → **Download your
  data** → request it; or help.uber.com → *Account* → *Request a copy of my
  personal data*.
- **Uber app (Samsung or iPhone)** — *Account* → *Settings* → *Privacy* →
  *Privacy Center* → **Download your data**.

Uber emails when the file is ready (hours to a few days) and the download
link is on the same page. Download it on the laptop: a zip holding `Uber
Data/Rider/trips_data.csv` and, if you have ordered food,
`Uber Data/Eats/eats_order_details.csv`, among other folders.

*Unverified without a real export:* the menu wording, the zip's name, and
the newer export layout — Uber has been seen to rename files
(`trips_data-0.csv`) and folders. That is why every `.csv` in the export is
recognised by its **header**, not its name: whatever it is called, a file
with Uber's trip columns is read as trips.

## Importing

Dry-run first — always:

```bash
.venv/Scripts/python -m lifecrm observe rides "~/Downloads/Uber Data.zip"
```

```
rides: Uber Data.zip (trips_data.csv: 3 trip(s), eats_order_details.csv: 1 order(s))
  times are UTC as Uber writes them; journal dates use the home timezone
  food_order: 1 new, 0 changed | 2026-10-16..2026-10-16 | e.g. 2026-10-16 Chipotle 18.95 usd
  usd: 18.95 in orders across these rows
  ride: 3 new, 0 changed | 2022-09-25..2026-10-12 | e.g. 2026-10-12 Uber to Uchi 16.87 usd
  usd: 57.15 in fares across these rows
  skipped: 1 × canceled trip (only completed trips are imported)
  skipped: 1 × other file in the export, not read (e.g. Uber Data/Account and Profile/profile_data.csv)
dry run — nothing written. Re-run with --apply to write.
```

Then:

```bash
.venv/Scripts/python -m lifecrm observe rides "~/Downloads/Uber Data.zip" --apply
```

Accepted targets: the export `.zip`, the folder it unzips to, or
`trips_data.csv` / `eats_order_details.csv` on its own. With `path` set,
the path can be left off:

```toml
[connectors.rides]
path = "~/Downloads/Uber Data.zip"
# currency = "usd"   # the Eats file names no currency: default is the trips' own
```

## What gets imported

**`ride`** — one per completed trip:

| field | holds |
|---|---|
| `title` | `Uber to <drop-off>` — the drop-off address's first part: the place's name when you picked one (`Uber to Uchi`), else the street address |
| `body` | `from <pickup address> to <drop-off address>` |
| `started_at` / `ended_at` | *Begin Trip Time* (the request time when there is none) and *Dropoff Time*, UTC |
| `value` / `unit` | the fare and its currency (`usd`, `eur`, …) |
| `value_json` | `product` (UberX, Comfort, …), `distance_mi`, `pickup` and `dropoff` (`lat`, `lon`, `address`), `city`, `status`, `requested_at` |
| `origin_app` | `uber` |

**`food_order`** — one per Uber Eats **order**. The Eats file holds one row
per item; rows sharing a restaurant and an order time are one order:

| field | holds |
|---|---|
| `title` | the restaurant |
| `body` | the items: `1 × Chicken Burrito Bowl (White Rice, Black Beans), 1 × Chips & Guacamole` |
| `value` / `unit` | the order price, in the trips' usual currency (the Eats file names none) — or `[connectors.rides] currency` |
| `value_json` | `items` (`name`, `quantity`, `price`, `customizations`), `status` |
| `origin_app` | `ubereats` |

**Not imported, counted:** trips canceled by you or the driver, unfulfilled
requests, and Eats orders not completed — they never happened. A canceled
trip that cost a cancellation fee is skipped too; the fee is on your card
statement. Older exports list Eats deliveries in `trips_data.csv` as well
(*Product Type* `UberEATS Marketplace`): those are skipped there and
counted, because the order — what was eaten, from where — is the Eats
file's.

## Dates and times

Uber writes UTC (`2026-10-12 23:41:00 +0000 UTC`), and writes the epoch,
`1970-01-01 00:00:00`, for a time that never happened — read as missing,
never as 1970. Journal dates use your home timezone and the rollover hour:
a ride home at 1 a.m. belongs to the evening before. A ride taken abroad
lands on the home-timezone day; the coordinates say where, not which zone.

## No handles

A ride names no other person (the driver is not in the rider's export), and
a place is not a handle: an address string is not a stable identifier, and
the observed tier never makes one of it (the merchant rule in
[finance.md](finance.md)). The coordinates are kept in `value_json`;
full-text search finds every ride to or from a place by its name or street.

## Re-importing, and what wins

Neither file has ids, so they are derived: a ride from its request instant,
product and city (with an ordinal among identical ones), an order from its
restaurant and order instant. A later export is a no-op for everything
already stored. A fare Uber adjusted afterwards arrives as a **conflict** —
surfaced in the report, never a silent overwrite: the export has no edit
clock to say which figure is newer.

## Nothing is removed: the archive remembers

Uber's export is the whole history when you request it, but a trip missing
from a later export is far likelier an export difference than an untaken
trip, and a ride is evidence of where you were. Nothing is ever removed by
a later export; `lifecrm imports retract <uuid>` undoes a run.

## Known limitations

- **Fares are not on the bank side here.** The same money shows on the
  card statement (`UBER *TRIP`); summing both counts it twice.
- **Tips added later** may not be in the fare column, depending on the
  export.
- **Scooters, bikes and Uber Reserve** appear as their own *Product Type*;
  they are imported as rides.
- **The driver side** (if you also drive) is a different folder and is not
  read.

## First real import — check these

Written from Uber's commonly seen export layout, and rendered by the demo,
but not yet run against this owner's real export. On the first one, check
in the dry run:

1. the header line lists your trips file (and the Eats file, if you order);
2. the ride count matches the app's *Activity* history, roughly;
3. the sample ride's date and destination are right (UTC → home time);
4. `skipped:` counts canceled trips, not completed ones;
5. `not understood:` is empty, or says something worth fixing.

## Commands

```bash
.venv/Scripts/python -m lifecrm observe rides <zip|folder|csv>          # dry run
.venv/Scripts/python -m lifecrm observe rides <zip|folder|csv> --apply
.venv/Scripts/python -m lifecrm observe rides                           # the [connectors.rides] path
.venv/Scripts/python -m lifecrm imports retract <uuid>
```

On the Mac the interpreter is `.venv/bin/python`; everything else is the same.

"""Rides connector: Uber's "Download your data" export — every trip, and
every Uber Eats order.

Where you went by car, and when, is a thread through a life the journal
rarely mentions ("took an Uber to Uchi", "a car to the airport for Bled").
Uber will send the whole history: the Privacy Center → Download your data,
or help.uber.com → Account → Request a copy of my data. This connector reads
that export. It never logs in, never calls an API, and never sends anything.

Accepted targets
----------------
- the export .zip, or the folder it unzips to ('Uber Data/Rider/
  trips_data.csv', 'Uber Data/Eats/eats_order_details.csv')
- one of those CSVs on its own
- nothing at all, when `path` is set in [connectors.rides]

Every .csv in the export is recognised by its header, not its name or
folder: Uber has renamed both over the years ('trips_data-0.csv'), and a
header is what says what a file holds. Everything else in the export —
the driver side, payment methods, app analytics — is counted and left
alone.

What comes in
-------------
`ride`, one per completed trip (trips_data.csv):
  title       "Uber to <drop-off>" — the drop-off address's first part,
              which is the place's name when you picked one ("Uchi") and
              the street address otherwise
  body        "from <pickup address> to <drop-off address>"
  started_at  Begin Trip Time (the request time when the trip has none);
              ended_at Dropoff Time
  value/unit  the fare and its currency ('usd')
  value_json  {product, distance_mi, pickup {lat, lon, address},
              dropoff {lat, lon, address}, city, status, requested_at}

`food_order`, one per Uber Eats ORDER (eats_order_details.csv holds one row
per item; rows sharing a restaurant and an order time are one order):
  title       the restaurant
  body        the items: "2 × Fish tacos (extra lime), 1 × Horchata"
  value/unit  the order price, in the trips' usual currency (the file names
              none: [connectors.rides] currency, else the trips' currency,
              else 'usd')
  value_json  {items: [{name, quantity, price, customizations}], status}

Times are UTC as Uber writes them ('2026-10-12 23:41:00 +0000 UTC');
journal dates use the home timezone, so a ride taken abroad lands on the
home-timezone day — the export's coordinates say where, not which zone.

Trips that did not happen — canceled by the rider or the driver,
unfulfilled — are skipped and counted, as are Eats orders not completed. A
canceled trip that cost a cancellation fee is still skipped: the fee shows
on the card statement. Older exports list Eats deliveries in
trips_data.csv as well (Product Type 'UberEATS Marketplace'); those are
skipped there and counted, because the order — what was eaten, from where
— is the Eats file's.

No handles
----------
A ride has no other person in it the export names (the driver is not in
the rider's file), and a place is not a handle: an address string is not a
stable identifier, and the observed tier never makes one of it (finance's
rule for merchants). The coordinates are kept in value_json.

Identity
--------
Neither file has ids, so they are derived: a ride is 'uber:' + SHA-1 of
its request instant, product and city (and an ordinal among identical
ones); an order is 'ubereats:' + SHA-1 of the restaurant and the order
instant. A re-export is therefore a no-op, and a fare Uber later adjusts
under the same trip arrives as a conflict to look at, never a silent
overwrite — the export has no edit clock to say which is newer.

Deletion semantics — remember
-----------------------------
Uber's history is complete when requested, but a trip missing from a later
export is far more likely an export difference than an untaken trip, and a
ride is evidence of where you were. The archive remembers: nothing is ever
removed by a later export. `imports retract` undoes a run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import _ofx, base
from .finance import parse_amount

name = "rides"
streams = ("ride", "food_order")

_TRIPS_HEADER = ("product type", "trip or order status", "request time",
                 "dropoff address", "fare amount")
_EATS_HEADER = ("restaurant_name", "order_time", "order_status", "item_name")
_HEADER_SCAN = 5
_DONE = {"completed", "complete", "delivered", "success", "succeeded"}
_UBER_TIME = re.compile(
    r"(?P<d>\d{4}-\d{2}-\d{2})[ T](?P<t>\d{1,2}:\d{2}(?::\d{2})?)(?:\.\d+)?"
    r"\s*(?P<z>Z|[+-]\d{2}:?\d{2})?(?:\s*(?:UTC|GMT))?", re.I)


# ---------------------------------------------------------------- small readers

def _norm(cell: str) -> str:
    return " ".join(str(cell).replace("﻿", "").strip().strip('"').lower().split())


def _collapse(text) -> str:
    return " ".join(str(text or "").split())


def uber_time(text: str) -> str | None:
    """'2026-10-12 23:41:00 +0000 UTC' -> '2026-10-12T23:41:00Z'; an ISO
    instant is read too. A value with no zone is UTC, as Uber writes it.
    Uber writes the epoch, 1970-01-01 00:00:00, for a time that never
    happened (a canceled trip's pickup): that is None, not 1970."""
    m = _UBER_TIME.fullmatch(_collapse(text))
    if not m or m["d"] == "1970-01-01":
        return None
    zone = (m["z"] or "+00:00").upper()
    if zone == "Z":
        zone = "+00:00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    clock = m["t"] if m["t"].count(":") == 2 else m["t"] + ":00"
    if len(clock.split(":")[0]) == 1:
        clock = "0" + clock
    return base.iso_utc(f"{m['d']}T{clock}{zone}")


def _float(text: str) -> float | None:
    try:
        return float(_collapse(text).replace(",", ""))
    except ValueError:
        return None


def _amount(text: str) -> Decimal | None:
    """A fare or price; None for an empty cell, ValueError for text that is
    not one."""
    return parse_amount(text)[0]


def short_place(address: str) -> str:
    """The first part of an address: the place's name when there is one
    ('Uchi, 801 S Lamar Blvd, …' -> 'Uchi'), else the street address."""
    first = _collapse(address).split(",")[0].strip()
    return first or _collapse(address)


def _header(rows: list[list[str]], required: tuple[str, ...]):
    for i, row in enumerate(rows[:_HEADER_SCAN]):
        names = [_norm(c) for c in row]
        if set(required) <= set(names):
            index: dict[str, int] = {}
            for j, n in enumerate(names):
                if n:
                    index.setdefault(n, j)
            return i + 1, index
    return None


# ---------------------------------------------------------------- trips

def _read_trips(fname: str, rows: list[list[str]], start: int, index: dict,
                cfg: Config, skipped: Counter, unmapped: Counter, examples: dict) -> list:
    out = []
    ordinals: Counter = Counter()
    for lineno, row in enumerate(rows[start:], start=start + 1):
        if not any(c.strip() for c in row):
            continue

        def cell(col: str) -> str:
            j = index.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        product = _collapse(cell("product type"))
        status = cell("trip or order status")
        if "eats" in product.casefold():
            skipped["Uber Eats delivery in the trips file (its order comes from the "
                    "Eats file)"] += 1
            continue
        if status.casefold() not in _DONE:
            low = status.casefold().replace("_", " ") or "blank-status"
            skipped[f"{low} trip (only completed trips are imported)"] += 1
            continue
        requested = uber_time(cell("request time"))
        begin = uber_time(cell("begin trip time")) or requested
        if begin is None:
            what = "trip whose Request/Begin Trip Time does not read"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno} ({cell('request time')!r})")
            continue
        dropoff = uber_time(cell("dropoff time"))
        if dropoff is not None and dropoff < begin:
            what = "trip whose Dropoff Time is before it began (end time dropped)"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            dropoff = None
        try:
            fare = _amount(cell("fare amount"))
        except ValueError:
            what = "trip whose Fare Amount does not parse (fare left out)"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno} ({cell('fare amount')!r})")
            fare = None
        currency = cell("fare currency").lower() or None
        if currency and not re.fullmatch(r"[a-z]{3}", currency):
            currency = None
        pickup_addr = _collapse(cell("begin trip address"))
        drop_addr = _collapse(cell("dropoff address"))
        city = _collapse(cell("city"))
        key = (requested or begin, product.casefold(), city.casefold())
        ordinal = ordinals[key]
        ordinals[key] += 1
        digest = hashlib.sha1("\x1f".join((*key, str(ordinal))).encode("utf-8"))

        def point(lat: str, lng: str, address: str) -> dict:
            p = {"lat": _float(cell(lat)), "lon": _float(cell(lng)), "address": address}
            return {k: v for k, v in p.items() if v not in (None, "")}

        detail = {"product": product, "city": city, "status": status.lower(),
                  "distance_mi": _float(cell("distance (miles)")),
                  "pickup": point("begin trip lat", "begin trip lng", pickup_addr),
                  "dropoff": point("dropoff lat", "dropoff lng", drop_addr),
                  "requested_at": requested if requested != begin else None}
        where_to = short_place(drop_addr) if drop_addr else None
        body = " ".join(x for x in (f"from {pickup_addr}" if pickup_addr else "",
                                    f"to {drop_addr}" if drop_addr else "") if x)
        out.append({
            "stream": "ride",
            "external_id": f"uber:{digest.hexdigest()[:24]}",
            "journal_date": base.journal_date_for_iso(begin, cfg),
            "started_at": begin,
            "ended_at": dropoff,
            "utc_offset_sec": None,
            "direction": None,
            "title": f"Uber to {where_to}" if where_to else f"Uber {product or 'ride'}",
            "body": body or None,
            "value": float(fare) if fare is not None else None,
            "unit": currency if fare is not None else None,
            "value_json": canonical_json({k: v for k, v in detail.items()
                                          if v not in (None, "", {})}),
            "media_path": None,
            "origin_app": "uber",
            "last_modified_at": dropoff or begin,
            "participants": [],
        })
    return out


# ---------------------------------------------------------------- Eats

def _quantity(text: str) -> int | float | None:
    q = _float(text)
    if q is None:
        return None
    return int(q) if q == int(q) else q


def _read_eats(fname: str, rows: list[list[str]], start: int, index: dict,
               cfg: Config, currency: str, skipped: Counter, unmapped: Counter,
               examples: dict) -> list:
    orders: dict[tuple[str, str], dict] = {}
    for lineno, row in enumerate(rows[start:], start=start + 1):
        if not any(c.strip() for c in row):
            continue

        def cell(col: str) -> str:
            j = index.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        restaurant = _collapse(cell("restaurant_name"))
        when = uber_time(cell("order_time"))
        if when is None or not restaurant:
            what = "Eats row with no readable Order_Time or Restaurant_Name"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{lineno}")
            continue
        order = orders.setdefault((restaurant.casefold(), when), {
            "restaurant": restaurant, "when": when, "status": cell("order_status"),
            "price": cell("order_price"), "items": [], "line": lineno})
        item = _collapse(cell("item_name"))
        custom = _collapse(cell("customizations"))
        try:
            price = _amount(cell("item_price"))
        except ValueError:
            price = None
        if not item and custom and order["items"]:
            prev = order["items"][-1]          # a customization on its own row
            prev["customizations"] = ", ".join(x for x in (prev.get("customizations"),
                                                           custom) if x)
            continue
        if item:
            entry = {"name": item, "quantity": _quantity(cell("item_quantity")),
                     "price": float(price) if price is not None else None,
                     "customizations": custom or None}
            order["items"].append({k: v for k, v in entry.items() if v is not None})
        if not order["price"]:
            order["price"] = cell("order_price")

    out = []
    for (key_name, when), o in sorted(orders.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if o["status"].casefold() not in _DONE:
            low = o["status"].casefold().replace("_", " ") or "blank-status"
            skipped[f"{low} Uber Eats order (only completed orders are imported)"] += 1
            continue
        try:
            total = _amount(o["price"])
        except ValueError:
            what = "Eats order whose Order_Price does not parse (price left out)"
            unmapped[what] += 1
            examples.setdefault(what, f"{fname}:{o['line']}")
            total = None
        body = ", ".join(
            (f"{i['quantity']} × " if i.get("quantity") is not None else "") + i["name"]
            + (f" ({i['customizations']})" if i.get("customizations") else "")
            for i in o["items"])
        digest = hashlib.sha1(f"{key_name}\x1f{when}".encode("utf-8"))
        out.append({
            "stream": "food_order",
            "external_id": f"ubereats:{digest.hexdigest()[:24]}",
            "journal_date": base.journal_date_for_iso(when, cfg),
            "started_at": when,
            "ended_at": None,
            "utc_offset_sec": None,
            "direction": None,
            "title": o["restaurant"],
            "body": body or None,
            "value": float(total) if total is not None else None,
            "unit": currency if total is not None else None,
            "value_json": canonical_json({"items": o["items"],
                                          "status": o["status"].lower()}),
            "media_path": None,
            "origin_app": "ubereats",
            "last_modified_at": when,
            "participants": [],
        })
    return out


# ---------------------------------------------------------------- targets

def _junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p.startswith(".") or p == "__MACOSX" for p in parts)


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
    """The `path` in [connectors.rides]: the Uber export, zipped or not."""
    value = cfg.connector_settings(name).get("path")
    if not value:
        raise DomainError("rides needs a path to read — Uber's data export .zip, the "
                          "folder it unzips to, or trips_data.csv (or set path in "
                          "[connectors.rides])")
    p = cfg.resolve(value)
    if not p.exists():
        raise DomainError(f"[connectors.rides] path does not exist: {p}")
    return p


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    setting = str(cfg.connector_settings(name).get("currency") or "").strip().lower()
    if setting and not re.fullmatch(r"[a-z]{3}", setting):
        raise DomainError(f"[connectors.rides] currency must be a three-letter ISO code "
                          f"like 'usd', got {setting!r}")
    found, ignored, kind = _csv_files(target)
    parsed = []
    others: list[str] = []          # CSVs that are neither, then everything else
    for fname, raw in found:
        text, _encoding = _ofx.decode(raw)
        try:
            rows = list(csv.reader(io.StringIO(text, newline="")))
        except csv.Error as e:
            if kind == "file":
                raise DomainError(f"{fname}: not readable as CSV: {e}") from e
            others.append(fname)
            continue
        trips, eats = _header(rows, _TRIPS_HEADER), _header(rows, _EATS_HEADER)
        if trips:
            parsed.append(("trips", fname, rows, *trips))
        elif eats:
            parsed.append(("eats", fname, rows, *eats))
        elif kind == "file":
            first = next((", ".join(c.strip() for c in r if c.strip())
                          for r in rows if any(c.strip() for c in r)), "")[:160]
            raise DomainError(f"{fname}: not an Uber trips_data.csv or "
                              f"eats_order_details.csv — first row: {first or '(empty)'}")
        else:
            others.append(fname)
    others += ignored
    if not parsed:
        listing = ", ".join(others[:8]) or "(empty)"
        raise DomainError(f"{target.name} holds no Uber trips or Eats orders — looked "
                          f"for trips_data.csv and eats_order_details.csv by their "
                          f"headers (has: {listing})")

    skipped, unmapped = Counter(), Counter()
    examples: dict[str, str] = {}
    records, files = [], []
    for what, fname, rows, start, index in parsed:
        if what != "trips":
            continue
        got = _read_trips(fname, rows, start, index, cfg, skipped, unmapped, examples)
        records += got
        files.append({"file": fname, "kind": "trips", "records": len(got)})
    fares = Counter(r["unit"] for r in records if r["unit"])
    currency = setting or (fares.most_common(1)[0][0] if fares else "usd")
    for what, fname, rows, start, index in parsed:
        if what != "eats":
            continue
        got = _read_eats(fname, rows, start, index, cfg, currency, skipped, unmapped,
                         examples)
        records += got
        files.append({"file": fname, "kind": "eats", "records": len(got)})

    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    skipped_lines = [f"{n} × {why}" for why, n in sorted(skipped.items())]
    if others:
        skipped_lines.append(f"{len(others)} × other file in the export, not read "
                             f"(e.g. {others[0]})")
    return base.empty_scan(
        name,
        account=None,
        source_file=target.name,
        source_hash=base.file_hash(target) if kind != "folder" else _content_hash(found),
        records=records,
        handles=[],
        unmapped=[{"what": k, "count": v, "example": examples.get(k)}
                  for k, v in sorted(unmapped.items())],
        skipped=skipped_lines,
        # No present_ids: the archive remembers (module docstring).
        facts={"files": files, "eats_currency": currency},
        target_kind=kind,
        files=files)


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    money = f" {Decimal(str(rec['value'])):,.2f} {rec['unit']}" if rec["value"] is not None \
        else ""
    return f"{rec['journal_date']} {rec['title']}{money}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    files = scan["facts"]["files"]
    lines = [f"rides: {scan['source_file']} ("
             + ", ".join(f"{PurePosixPath(f['file']).name}: {f['records']} "
                         f"{'trip(s)' if f['kind'] == 'trips' else 'order(s)'}"
                         for f in files) + ")"]
    lines.append("  times are UTC as Uber writes them; journal dates use the home "
                 "timezone")
    by_stream: dict[str, dict] = {}
    for rec in plan["new"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["new"].append(rec)
    for _, rec, _ in plan["updates"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["upd"].append(rec)
    for stream in sorted(by_stream):
        info = by_stream[stream]
        recs = info["new"] or info["upd"]
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda r: (r["started_at"], r["external_id"]))
        lines.append(f"  {stream}: {len(info['new'])} new, {len(info['upd'])} changed "
                     f"| {span} | e.g. {_sample(latest)}")
        for unit in sorted({r["unit"] for r in recs if r["unit"]}):
            total = sum(Decimal(str(r["value"])) for r in recs
                        if r["unit"] == unit and r["value"] is not None)
            what = "fares" if stream == "ride" else "orders"
            lines.append(f"  {unit}: {total:,.2f} in {what} across these rows")
    if not by_stream:
        lines.append("  nothing new — the export matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate record(s) in the export")
    if tail:
        lines.append("  " + ", ".join(tail))
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:20]}…: differs "
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

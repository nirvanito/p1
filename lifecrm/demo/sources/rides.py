"""Rides sample: the persona's Uber trips and one Uber Eats order, as Uber's
"Download your data" export hands them over.

A zip ('Uber Data.zip') laid out the way the export is:

  Uber Data/Rider/trips_data.csv            one row per trip request, UTC
                                            times with Uber's ' +0000 UTC'
  Uber Data/Eats/eats_order_details.csv     one row per item ordered
  Uber Data/Account and Profile/profile_data.csv   not read: counted

The trips, from the persona's own story:

  day -20   the ride home from Veracruz after the board celebration (Jake
            texts "Proud of you" at 21:10, right after) — preceded by a
            request Alex canceled, which the connector skips
  day -9    to Uchi for Maya's birthday dinner at 19:00
  Bled      to Austin-Bergstrom (AUS) for the flight to Ljubljana: the day
            BEFORE bled_trip_days()[0], because the persona's photo on the
            Ljubljana bridge is at 17:20 local on the trip's first day —
            10:20 in Austin, too soon to have left that morning
  day -5    an Uber Eats order from Chipotle on the night the launches
            piled up

The ride fares go on a card the demo does not import, so the demo's bank
statements stay exactly persona.TRANSACTIONS. Uber is the same app on both
phones, so both combos get the same export. Home is a fictional address;
the places are the persona's.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import persona

HOME = ("1100 E 5th St, Austin, TX 78702, US", 30.2640, -97.7330)
AIRPORT = ("Austin-Bergstrom International Airport (AUS), 3600 Presidential Blvd, "
           "Austin, TX 78719, US", 30.1975, -97.6664)


def _place(key: str) -> tuple[str, float, float]:
    p = persona.place(key)
    street = (p.address or p.city).replace(", Austin, TX", "")
    zips = {"uchi": "78704", "veracruz": "78702"}
    return (f"{p.name}, {street}, Austin, TX {zips.get(key, '78701')}, US", p.lat, p.lon)


@dataclass(frozen=True)
class Trip:
    when: str                    # 'offset:N' or 'bled-eve'
    request: str                 # local HH:MM
    wait_min: int                # request -> pickup
    ride_min: int
    pickup: tuple[str, float, float]
    dropoff: tuple[str, float, float]
    product: str
    miles: float
    fare: str
    status: str = "COMPLETED"


TRIPS = (
    Trip("offset:-20", "20:48", 0, 0, _place("veracruz"), HOME, "UberX", 0.0, "0.00",
         status="CANCELED"),
    Trip("offset:-20", "20:52", 5, 12, _place("veracruz"), HOME, "UberX", 2.31, "12.85"),
    Trip("offset:-9", "18:32", 6, 17, HOME, _place("uchi"), "UberX", 4.62, "16.87"),
    Trip("bled-eve", "13:05", 5, 21, HOME, AIRPORT, "UberX", 9.84, "27.43"),
)

# (offset, local order time, restaurant, status, order price,
#  [(item, quantity, customizations, item price)])
EATS = (
    (-5, "20:40", "Chipotle", "completed", "18.95",
     [("Chicken Burrito Bowl", 1, "White Rice, Black Beans, Fajita Veggies", "11.45"),
      ("Chips & Guacamole", 1, "", "5.35")]),
)

_TRIP_COLUMNS = ["City", "Product Type", "Trip or Order Status", "Request Time",
                 "Begin Trip Time", "Begin Trip Lat", "Begin Trip Lng",
                 "Begin Trip Address", "Dropoff Time", "Dropoff Lat", "Dropoff Lng",
                 "Dropoff Address", "Distance (miles)", "Fare Amount", "Fare Currency"]
_EATS_COLUMNS = ["Restaurant_Name", "Order_Time", "Order_Status", "Item_Name",
                 "Item_quantity", "Customizations", "Item_Price", "Order_Price"]
_EPOCH = "1970-01-01 00:00:00 +0000 UTC"      # what Uber writes for "never"


def trip_day(t: Trip, anchor: date) -> date:
    if t.when == "bled-eve":
        return persona.bled_trip_days(anchor)[0] - timedelta(days=1)
    return persona.day(anchor, int(t.when.split(":")[1]))


def _local(d: date, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ZoneInfo(persona.TIMEZONE))


def _uber(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")


def _csv(rows: list[list]) -> bytes:
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


def _trips(anchor: date) -> bytes:
    rows = [list(_TRIP_COLUMNS)]
    for t in sorted(TRIPS, key=lambda t: (trip_day(t, anchor), t.request), reverse=True):
        requested = _local(trip_day(t, anchor), t.request)
        if t.status != "COMPLETED":
            rows.append(["Austin", t.product, t.status, _uber(requested), _EPOCH,
                         f"{t.pickup[1]:.6f}", f"{t.pickup[2]:.6f}", t.pickup[0], _EPOCH,
                         "", "", "", "0.00", t.fare, "USD"])
            continue
        begin = requested + timedelta(minutes=t.wait_min)
        end = begin + timedelta(minutes=t.ride_min, seconds=37)
        rows.append(["Austin", t.product, t.status, _uber(requested), _uber(begin),
                     f"{t.pickup[1]:.6f}", f"{t.pickup[2]:.6f}", t.pickup[0], _uber(end),
                     f"{t.dropoff[1]:.6f}", f"{t.dropoff[2]:.6f}", t.dropoff[0],
                     f"{t.miles:.2f}", t.fare, "USD"])
    return _csv(rows)


def _eats(anchor: date) -> bytes:
    rows = [list(_EATS_COLUMNS)]
    for offset, hhmm, restaurant, status, total, items in EATS:
        when = _uber(persona.local_dt(anchor, offset, hhmm))
        for item, qty, custom, price in items:
            rows.append([restaurant, when, status, item, str(qty), custom, price, total])
    return _csv(rows)


def _profile() -> bytes:
    me = persona.OWNER
    return _csv([["Email", "First Name", "Last Name", "Mobile", "Rider Rating",
                  "Signup City"],
                 [me.email, me.first, me.last, me.phone, "4.93", "Austin"]])


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """The export zip — what the owner would point `lifecrm observe rides` at."""
    if combo not in persona.COMBOS:
        raise ValueError(f"rides has no sample for combo '{combo}' "
                         f"(expected one of {', '.join(persona.COMBOS)})")
    folder = Path(dest)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "Uber Data.zip"
    stamp = (anchor.year, anchor.month, anchor.day, 7, 30, 0)
    members = (("Uber Data/Account and Profile/profile_data.csv", _profile()),
               ("Uber Data/Eats/eats_order_details.csv", _eats(anchor)),
               ("Uber Data/Rider/trips_data.csv", _trips(anchor)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, raw in members:
            info = zipfile.ZipInfo(member, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, raw)
    return path

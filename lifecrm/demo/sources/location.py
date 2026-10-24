"""Sample location history for the demo: the persona's Google Maps Timeline.

  'android'  `Timeline.json` as a Galaxy writes it from Settings > Location >
             Timeline > Export Timeline data: semanticSegments with a visit
             per place the persona went — Movement, Veracruz, Zilker, Uchi,
             the Acme office, H-E-B, Ellis Dental, Tía Rosa's in San Antonio
             — plus home, the activity segments between them, the route
             points (timelinePath), a few rawSignals and the
             userLocationProfile. Four years back, the Lake Bled trip, in
             Slovenia's zone, the way a migrated on-device timeline keeps it.
             Like the real export it names no place: a visit carries a place
             id, a position and HOME / WORK where Google labelled one.
  'iphone', 'windows-iphone'   none. Apple's Significant Locations are
             encrypted on the phone and cannot be exported, whichever computer
             it pairs with; ValueError, as the generator contract asks. (Google
             Maps' Timeline on an iPhone would export, but this persona's
             iPhone uses Apple Maps.)

Dates follow the persona's other sources (calendar, photos, texts and card
transactions), so a visit, a photo and a payment agree on where the evening
was. The persona's journal puts the first Veracruz dinner a day after all of
those; the location history sides with the evidence.

Deterministic for a given anchor. Place ids are derived from the place, not
the anchor, so the same restaurant keeps its id when the demo is rebuilt for
another day; `PLACE_IDS` maps each persona place key to its id, for linking
`place_id:<id>` handles to the place entities.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import persona

# Places the persona goes that persona.PLACES does not list: home, the post
# office where the passport was renewed, and Tía Rosa's house.
_EXTRA = {
    "home": (30.2612, -97.7187),
    "post_office": (30.2650, -97.7357),
    "tia_rosa": (29.4512, -98.5200),
}
_SEMANTIC = {"home": "HOME", "acme": "WORK"}

# (offset, place key, local start 'HH:MM', minutes)
_OUTINGS = (
    (-41, "acme", "08:55", 525),
    (-40, "movement", "06:20", 85),
    (-40, "acme", "08:55", 500),
    (-38, "acme", "09:00", 510),
    (-38, "veracruz", "19:25", 100),
    (-34, "acme", "08:50", 555),
    (-30, "movement", "17:55", 95),
    (-30, "heb", "19:45", 30),
    (-27, "acme", "08:55", 495),
    (-27, "zilker", "18:05", 40),
    (-23, "post_office", "08:40", 35),
    (-23, "acme", "09:30", 470),
    (-20, "acme", "08:45", 545),
    (-20, "veracruz", "19:20", 125),
    (-13, "tia_rosa", "16:50", 195),
    (-12, "movement", "06:20", 75),
    (-12, "ellis_dental", "10:20", 55),
    (-12, "acme", "11:40", 330),
    (-9, "acme", "09:05", 480),
    (-9, "uchi", "18:50", 130),
    (-6, "acme", "08:55", 500),
    (-2, "movement", "06:20", 70),
    (-2, "acme", "08:45", 510),
    (-1, "acme", "09:00", 480),
    (-1, "veracruz", "18:55", 90),
)

# The Bled trip: (trip day, place key, local start, minutes), in Slovenia.
_TRIP = (
    (0, "ljubljana", "16:40", 330),
    (1, "bled", "09:40", 95),
    (1, "bled_castle", "11:25", 110),
    (2, "bled", "14:30", 400),
    (4, "ljubljana", "17:50", 300),
)


def _place_id(key: str) -> str:
    """A Google-shaped place id ('ChIJ' + 23 url-safe characters), fixed per
    place."""
    digest = hashlib.sha1(f"lifecrm-demo-place:{key}".encode()).digest()
    return "ChIJ" + base64.urlsafe_b64encode(digest).decode()[:23]


PLACE_IDS = {key: _place_id(key)
             for key in [p.key for p in persona.PLACES] + list(_EXTRA)}


def _position(key: str) -> tuple[float, float]:
    if key in _EXTRA:
        return _EXTRA[key]
    p = persona.place(key)
    return p.lat, p.lon


def _latlng(pos: tuple[float, float]) -> str:
    return f"{pos[0]:.7f}°, {pos[1]:.7f}°"


def _stamp(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _offset_minutes(dt: datetime) -> int:
    return int(dt.utcoffset().total_seconds() // 60)


def _drive(km: float) -> timedelta:
    """Door to door: a few minutes to get going, then city-to-highway speed."""
    return timedelta(minutes=min(120, 8 + round(km * 0.9)))


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (math.sin((la2 - la1) / 2) ** 2
         + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo in persona.COMBOS and persona.has_iphone(combo):
        raise ValueError("location has no iPhone sample: Apple's Significant "
                         "Locations are encrypted on the phone and have no export "
                         "(docs/location.md)")
    if combo != "android":
        raise ValueError(f"location has no sample for combo '{combo}' "
                         f"(expected: android)")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    rng = random.Random(f"lifecrm-demo-location-{anchor.isoformat()}")

    days: dict[tuple, list[tuple[datetime, datetime, str]]] = {}
    for off, key, hhmm, minutes in _OUTINGS:
        start = persona.local_dt(anchor, off, hhmm)
        days.setdefault(("home", off), []).append(
            (start, start + timedelta(minutes=minutes), key))
    for trip_day, key, hhmm, minutes in _TRIP:
        start = persona.trip_dt(trip_day, hhmm, anchor)
        days.setdefault(("trip", trip_day), []).append(
            (start, start + timedelta(minutes=minutes), key))

    segments = []
    for (where, _), outings in sorted(days.items(), key=lambda kv: kv[1][0][0]):
        outings.sort()
        prev_key, prev_end = ("home", None) if where == "home" else (None, None)
        for start, end, key in outings:
            if prev_key is not None:
                travel = _km(_position(prev_key), _position(key))
                leave = prev_end or start - _drive(travel)
                segments += _activity(leave, start, prev_key, key, travel, rng)
            segments.append(_visit(start, end, key, rng))
            prev_key, prev_end = key, end
        if where == "home":
            travel = _km(_position(prev_key), _position("home"))
            arrive = prev_end + _drive(travel)
            segments += _activity(prev_end, arrive, prev_key, "home", travel, rng)
            night = arrive.replace(hour=23, minute=30)
            if night > arrive:
                segments.append(_visit(arrive, night, "home", rng))

    trip = [persona.trip_dt(t, hhmm, anchor) for t, _, hhmm, _ in _TRIP]
    segments.append({
        "startTime": _stamp(trip[0]), "endTime": _stamp(trip[-1] + timedelta(hours=5)),
        "startTimeTimezoneUtcOffsetMinutes": _offset_minutes(trip[0]),
        "endTimeTimezoneUtcOffsetMinutes": _offset_minutes(trip[-1]),
        "timelineMemory": {"trip": {
            "distanceFromOriginKms": 8874,
            "destinations": [{"identifier": {"placeId": PLACE_IDS["bled"]}},
                             {"identifier": {"placeId": PLACE_IDS["ljubljana"]}}]}},
    })
    rank = {"activity": 0, "timelinePath": 1, "visit": 2, "timelineMemory": 3}
    segments.sort(key=lambda s: (datetime.fromisoformat(s["startTime"]),
                                 min(rank[k] for k in s if k in rank)))

    last = persona.local_dt(anchor, -1, "23:10")
    raw_signals = [{"position": {
        "LatLng": _latlng(_EXTRA["home"]), "accuracyMeters": 11 + i,
        "altitudeMeters": 151.4, "source": "WIFI",
        "timestamp": _stamp(last + timedelta(minutes=7 * i)),
        "speedMetersPerSecond": 0.0}} for i in range(3)]
    profile = {"frequentPlaces": [
        {"placeId": PLACE_IDS["home"], "placeLocation": _latlng(_EXTRA["home"]),
         "label": "HOME"},
        {"placeId": PLACE_IDS["acme"], "placeLocation": _latlng(_position("acme")),
         "label": "WORK"},
        {"placeId": PLACE_IDS["movement"],
         "placeLocation": _latlng(_position("movement"))},
    ], "persona": {"travelModeAffinities": [
        {"mode": "IN_PASSENGER_VEHICLE", "affinity": 0.72},
        {"mode": "WALKING", "affinity": 0.21}]}}

    out = dest / "Timeline.json"
    text = json.dumps({"semanticSegments": segments, "rawSignals": raw_signals,
                       "userLocationProfile": profile}, indent=2, ensure_ascii=False)
    out.write_bytes((text + "\n").encode("utf-8"))
    return out


def _visit(start: datetime, end: datetime, key: str, rng: random.Random) -> dict:
    return {
        "startTime": _stamp(start), "endTime": _stamp(end),
        "startTimeTimezoneUtcOffsetMinutes": _offset_minutes(start),
        "endTimeTimezoneUtcOffsetMinutes": _offset_minutes(end),
        "visit": {
            "hierarchyLevel": 0,
            "probability": round(rng.uniform(0.80, 0.99), 6),
            "topCandidate": {
                "placeId": PLACE_IDS[key],
                "semanticType": _SEMANTIC.get(key, "UNKNOWN"),
                "probability": round(rng.uniform(0.55, 0.97), 6),
                "placeLocation": {"latLng": _latlng(_position(key))},
            },
        },
    }


def _activity(start: datetime, end: datetime, a: str, b: str, km: float,
              rng: random.Random) -> list[dict]:
    """The trip between two visits, and its route as the separate
    timelinePath segment the export interleaves with it."""
    mode = "WALKING" if km < 1.5 else "IN_PASSENGER_VEHICLE"
    times = {"startTime": _stamp(start), "endTime": _stamp(end),
             "startTimeTimezoneUtcOffsetMinutes": _offset_minutes(start),
             "endTimeTimezoneUtcOffsetMinutes": _offset_minutes(end)}
    return [{
        **times,
        "activity": {
            "start": {"latLng": _latlng(_position(a))},
            "end": {"latLng": _latlng(_position(b))},
            "distanceMeters": round(km * 1000 * 1.25, 1),
            "probability": round(rng.uniform(0.85, 0.99), 6),
            "topCandidate": {"type": mode, "probability": round(rng.uniform(0.6, 0.95), 6)},
        },
    }, {
        **times,
        "timelinePath": [
            {"point": _latlng(_position(a)), "time": _stamp(start)},
            {"point": _latlng(_position(b)), "time": _stamp(end)},
        ],
    }]

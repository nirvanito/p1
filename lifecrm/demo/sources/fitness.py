"""Fitness sample: the persona's training, as Strong and Strava export it.

Two exports, the same on both combos — both apps run on either phone and
export the same files:

  strong.csv              Strong's Settings > Export Strong Data: one row
                          per set, the workout's Date / Name / Duration on
                          every row, weights in the app's unit (lb, the US
                          default — the file does not say).
  export_<athlete>.zip    Strava's bulk archive (strava.com > Settings > My
                          Account > Download or Delete Your Account):
                          activities.csv with its doubled Elapsed Time /
                          Distance / Max Heart Rate / Relative Effort /
                          Commute columns (display units first, raw seconds
                          and meters after), Activity Date in UTC, one GPX
                          per activity under activities/, and profile.csv.
                          The GPX files say which phone recorded them
                          ('StravaGPX Android' / 'StravaGPX iPhone') — the
                          only difference between the combos.

The sessions are persona._WORKOUTS, the same ones the watch's health series
holds: the strength days go to Strong — squats on day -40 ("squats felt
strong"), bench with Sam three sets of five at 185 lb on day -12, and the
lighter benches before it, so the dry run has a personal best to show — and
the runs to Strava: 5.6 km on -33, the 5k at Zilker in 27 minutes on -27,
7.2 km on -15. Four years back, on the Lake Bled trip, a hike to the
Ojstrica viewpoint. Climbing with Maya (-30) was logged by neither app.

Strava's dates are UTC, and the home timezone dates the journal day: the
hike starts at 13:30 in Bled (06:30 in Austin), so it lands on its own trip
day. A morning hike would have landed on the day before — the limitation
docs/fitness.md describes.

Deterministic for a given (anchor, combo): no clock, no randomness; activity
ids are derived from the start time.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import persona

ATHLETE_ID = 48213377
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")
# Where the runs start: home, as the location sample has it, by Lady Bird Lake.
_HOME = (30.2612, -97.7187)

# offset -> (workout name, workout notes, [(exercise, [(weight lb, reps, set order)])])
# Set order 'W' is a warm-up; weight 0 is a bodyweight exercise.
_W = "W"
STRENGTH: dict[int, tuple[str, str, list]] = {
    -40: ("Leg Day", "Early lift with Sam", [
        ("Squat (Barbell)", [(135, 5, _W), (185, 3, _W)] + [(225, 5, None)] * 5),
        ("Romanian Deadlift (Barbell)", [(185, 8, None)] * 3),
        ("Leg Press", [(360, 10, None)] * 3),
    ]),
    -37: ("Push Day", "", [
        ("Bench Press (Barbell)", [(135, 5, _W)] + [(175, 5, None)] * 3),
        ("Overhead Press (Barbell)", [(95, 8, None)] * 3),
        ("Triceps Pushdown (Cable - Straight Bar)", [(50, 12, None)] * 3),
    ]),
    -24: ("Push Day", "", [
        ("Bench Press (Barbell)", [(135, 5, _W)] + [(180, 5, None)] * 3),
        ("Overhead Press (Barbell)", [(100, 6, None)] * 3),
        ("Pull Up", [(0, 8, None), (0, 8, None), (0, 6, None)]),
    ]),
    -19: ("Leg Day", "", [
        ("Squat (Barbell)", [(135, 5, _W), (185, 3, _W)] + [(230, 5, None)] * 5),
        ("Deadlift (Barbell)", [(225, 3, _W), (275, 5, None)]),
        ("Leg Press", [(370, 10, None)] * 3),
    ]),
    -12: ("Push Day", "Bench day with Sam", [
        ("Bench Press (Barbell)", [(135, 5, _W)] + [(185, 5, None)] * 3),
        ("Overhead Press (Barbell)", [(100, 8, None)] * 3),
        ("Pull Up", [(0, 8, None)] * 3),
    ]),
    -2: ("Full Body", "Back on track", [
        ("Squat (Barbell)", [(135, 5, _W)] + [(205, 5, None)] * 3),
        ("Bench Press (Barbell)", [(175, 5, None)] * 3),
        ("Pull Up", [(0, 8, None)] * 3),
    ]),
}

# Strava: offset (None = the Bled trip) -> name, type, description, gear,
# moving / elapsed seconds, meters, elevation gain, avg / max HR.
_RUN_SHOES = "Nike Pegasus 40"
RUNS: dict[int, dict] = {
    -33: {"name": "Evening Run", "description": "", "moving": 1920, "elapsed": 1985,
          "meters": 5612.4, "gain": 41.0, "hr": (154, 171), "start": _HOME},
    -27: {"name": "Zilker Park 5k", "description": "After work, felt quick",
          "moving": 1620, "elapsed": 1695, "meters": 5003.1, "gain": 32.0,
          "hr": (158, 176), "start": (persona.place("zilker").lat,
                                      persona.place("zilker").lon)},
    -15: {"name": "Morning Run", "description": "", "moving": 2460, "elapsed": 2550,
          "meters": 7204.8, "gain": 58.0, "hr": (151, 168), "start": _HOME},
}
HIKE = {"trip_day": 3, "time": "13:30", "name": "Hike to Ojstrica above Lake Bled",
        "description": "Ojstrica viewpoint with Jake: the island church from above",
        "moving": 7500, "elapsed": 9600, "meters": 6812.0, "gain": 395.0,
        "hr": (128, 162), "calories": 640, "gear": "Salomon X Ultra"}

STRONG_HEADER = ["Date", "Workout Name", "Duration", "Exercise Name", "Set Order",
                 "Weight", "Reps", "Distance", "Seconds", "Notes", "Workout Notes", "RPE"]

# Strava's activities.csv as the archive lays it out: a summary block in the
# athlete's display units, then the raw block (seconds, meters, m/s) that
# repeats Elapsed Time, Distance, Max Heart Rate, Relative Effort and Commute.
STRAVA_HEADER = [
    "Activity ID", "Activity Date", "Activity Name", "Activity Type",
    "Activity Description", "Elapsed Time", "Distance", "Max Heart Rate",
    "Relative Effort", "Commute", "Activity Private Note", "Activity Gear",
    "Filename", "Athlete Weight", "Bike Weight", "Elapsed Time", "Moving Time",
    "Distance", "Max Speed", "Average Speed", "Elevation Gain", "Elevation Loss",
    "Elevation Low", "Elevation High", "Max Grade", "Average Grade",
    "Average Positive Grade", "Average Negative Grade", "Max Cadence",
    "Average Cadence", "Max Heart Rate", "Average Heart Rate", "Max Watts",
    "Average Watts", "Calories", "Max Temperature", "Average Temperature",
    "Relative Effort", "Total Work", "Number of Runs", "Uphill Time",
    "Downhill Time", "Other Time", "Perceived Exertion", "Type", "Start Time",
    "Weighted Average Power", "Power Count", "Prefer Perceived Exertion",
    "Perceived Relative Effort", "Commute", "Total Weight Lifted", "From Upload",
    "Grade Adjusted Distance", "Weather Observation Time", "Weather Condition",
    "Weather Temperature", "Apparent Temperature", "Dewpoint", "Humidity",
    "Weather Pressure", "Wind Speed", "Wind Gust", "Wind Bearing",
    "Precipitation Intensity", "Sunrise Time", "Sunset Time", "Moon Phase", "Bike",
    "Gear", "Precipitation Probability", "Precipitation Type", "Cloud Cover",
    "Weather Visibility", "UV Index", "Weather Ozone", "Jump Count", "Total Grit",
    "Average Flow", "Flagged", "Average Elapsed Speed", "Dirt Distance",
    "Newly Explored Distance", "Newly Explored Dirt Distance", "Activity Count",
    "Total Steps", "Carbon Saved", "Pool Length", "Training Load", "Intensity",
    "Average Grade Adjusted Pace", "Timer Time", "Total Cycles", "Media",
]


def _check(combo: str) -> None:
    if combo not in persona.COMBOS:
        raise ValueError(f"fitness has no sample for combo '{combo}' "
                         f"(expected one of {', '.join(persona.COMBOS)})")


def _csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


def _duration(minutes: int) -> str:
    """Strong's own spelling: '1h 15m', '55m', '1h'."""
    h, m = divmod(minutes, 60)
    return " ".join(p for p in ((f"{h}h" if h else ""), (f"{m}m" if m else "")) if p)


# ---------------------------------------------------------------- Strong

def strong_csv(anchor: date) -> bytes:
    rows = [STRONG_HEADER]
    for offset in sorted(STRENGTH):
        _, hhmm, minutes, _ = persona._WORKOUTS[offset]
        title, notes, exercises = STRENGTH[offset]
        start = persona.local_dt(anchor, offset, hhmm).strftime("%Y-%m-%d %H:%M:%S")
        for exercise, sets in exercises:
            n = 0
            for weight, reps, order in sets:
                if order is None:
                    n += 1
                rows.append([start, title, _duration(minutes), exercise,
                             order or str(n), f"{weight:.1f}", str(reps), "0", "0",
                             "", notes, ""])
    return _csv(rows)


# ---------------------------------------------------------------- Strava

def _activity_id(start: datetime) -> int:
    """Strava-sized (ten or eleven digits), growing with time like the real ones."""
    days = (start.date() - date(2020, 1, 1)).days
    return 4_200_000_000 + days * 4_000_000 + start.hour * 60 + start.minute


def _strava_date(utc: datetime) -> str:
    """'Sep 24, 2026, 11:15:00 PM' — month named by hand, never by the locale."""
    hour = utc.hour % 12 or 12
    half = "AM" if utc.hour < 12 else "PM"
    return (f"{_MONTHS[utc.month - 1]} {utc.day}, {utc.year}, "
            f"{hour}:{utc.minute:02d}:{utc.second:02d} {half}")


def _activities(anchor: date) -> list[dict]:
    out = []
    for offset in sorted(RUNS):
        _, hhmm, minutes, _ = persona._WORKOUTS[offset]
        run = RUNS[offset]
        out.append({**run, "type": "Run", "gear": _RUN_SHOES,
                    "local": persona.local_dt(anchor, offset, hhmm),
                    "calories": int(minutes * 10.5)})
    trip = persona.trip_dt(HIKE["trip_day"], HIKE["time"], anchor)
    bled = persona.place("bled")
    out.append({**HIKE, "type": "Hike", "local": trip, "start": (bled.lat, bled.lon)})
    for a in out:
        a["utc"] = a["local"].astimezone(timezone.utc)
        a["id"] = _activity_id(a["utc"])
    return sorted(out, key=lambda a: a["utc"])


def _num(x: float) -> str:
    return f"{float(x):.1f}"


def activities_csv(anchor: date) -> bytes:
    col = {}
    for j, n in enumerate(STRAVA_HEADER):
        col.setdefault(n, []).append(j)
    rows = [STRAVA_HEADER]
    # The persona is in the US: the summary block is in miles.
    for a in _activities(anchor):
        row = [""] * len(STRAVA_HEADER)

        def put(name: str, value, which: int = 0) -> None:
            row[col[name][which]] = str(value)

        avg, top = a["hr"]
        put("Activity ID", a["id"])
        put("Activity Date", _strava_date(a["utc"]))
        put("Activity Name", a["name"])
        put("Activity Type", a["type"])
        put("Activity Description", a["description"])
        put("Elapsed Time", a["elapsed"])
        put("Distance", f"{a['meters'] / 1609.344:.2f}")
        put("Max Heart Rate", _num(top))
        put("Relative Effort", int(a["moving"] / 60 * 1.9))
        put("Commute", "false")
        put("Activity Gear", a["gear"])
        put("Filename", f"activities/{a['id']}.gpx")
        put("Elapsed Time", _num(a["elapsed"]), 1)
        put("Moving Time", _num(a["moving"]))
        put("Distance", _num(a["meters"]), 1)
        put("Average Speed", f"{a['meters'] / a['moving']:.3f}")
        put("Max Speed", f"{a['meters'] / a['moving'] * 1.32:.3f}")
        put("Elevation Gain", _num(a["gain"]))
        put("Elevation Loss", _num(a["gain"] - 0.6))
        put("Max Heart Rate", _num(top), 1)
        put("Average Heart Rate", _num(avg))
        put("Calories", _num(a["calories"]))
        put("Relative Effort", _num(int(a["moving"] / 60 * 1.9)), 1)
        put("Commute", "0.0", 1)
        put("From Upload", "0.0")
        put("Grade Adjusted Distance", _num(a["meters"] * 1.01))
        put("Activity Count", "1.0")
        put("Timer Time", _num(a["elapsed"]))
        rows.append(row)
    return _csv(rows)


def _gpx(a: dict, combo: str) -> str:
    """A short track: start, two points along the way, finish. Never parsed
    by the connector — it only keeps the path."""
    lat, lon = a["start"]
    steps = ((0.0, 0.0, 0.0), (0.004, -0.006, 0.35), (0.006, 0.002, 0.7),
             (0.0005, 0.0005, 1.0))
    device = "Android" if combo == "android" else "iPhone"
    kind = "running" if a["type"] == "Run" else "hiking"
    points = []
    for dlat, dlon, frac in steps:
        t = (a["utc"] + timedelta(seconds=round(a["elapsed"] * frac))
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
        points.append(f'   <trkpt lat="{lat + dlat:.7f}" lon="{lon + dlon:.7f}">\n'
                      f"    <ele>{150 + a['gain'] * frac:.1f}</ele>\n"
                      f"    <time>{t}</time>\n   </trkpt>")
    start = a["utc"].strftime("%Y-%m-%dT%H:%M:%SZ")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<gpx creator="StravaGPX {device}" version="1.1" '
            'xmlns="http://www.topografix.com/GPX/1/1">\n'
            f" <metadata>\n  <time>{start}</time>\n </metadata>\n"
            f" <trk>\n  <name>{a['name']}</name>\n  <type>{kind}</type>\n  <trkseg>\n"
            + "\n".join(points) + "\n  </trkseg>\n </trk>\n</gpx>\n")


def _profile() -> bytes:
    me = persona.OWNER
    return _csv([["Athlete ID", "Email Address", "First Name", "Last Name", "Sex",
                  "Description", "Weight", "City", "State", "Country"],
                 [str(ATHLETE_ID), me.email, me.first, me.last, "", "", "78.4",
                  "Austin", "Texas", "United States"]])


def strava_zip(dest: Path, anchor: date, combo: str) -> Path:
    path = Path(dest) / f"export_{ATHLETE_ID}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (anchor.year, anchor.month, anchor.day, 9, 0, 0)
    members = [("activities.csv", activities_csv(anchor))]
    members += [(f"activities/{a['id']}.gpx", _gpx(a, combo).encode("utf-8"))
                for a in _activities(anchor)]
    members.append(("profile.csv", _profile()))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return path


# ---------------------------------------------------------------- the contract

def write_samples(dest: Path, anchor: date, combo: str) -> list[Path]:
    """strong.csv, then Strava's export zip — two imports, as the owner would
    run them."""
    _check(combo)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    strong = dest / "strong.csv"
    strong.write_bytes(strong_csv(anchor))
    return [strong, strava_zip(dest, anchor, combo)]


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """The Strong export alone (the demo builder calls write_samples)."""
    return write_samples(dest, anchor, combo)[0]

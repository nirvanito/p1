"""Sample Health Connect export for the Android combo: the zip Health
Connect's export writes, around its raw SQLite database, rendering
persona.health_series as Samsung Health would have written it.

Table and column shapes are the AOSP HealthFitness storage ones the importer
was written against (see health_connect.py): uuid BLOBs, app_info_id ->
application_info_table, epoch-ms times, zone offsets in seconds, grams for
mass, small calories for energy, meters for distance, and child tables for
sleep stages and heart-rate samples. Everything is written by one app,
com.sec.android.app.shealth — a Galaxy phone with a Galaxy Watch syncs both
through Samsung Health, so there is one origin per metric and no
double-counting.

Record uuids are hashes of what each record is, so two builds for the same
anchor are the same export and re-importing it is all-identical. A table no
mapper covers (active calories) is included, as a real export has many, so
the dry-run's "unmapped tables" line has something to say.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import persona

PACKAGE = "com.sec.android.app.shealth"
ZIP_NAME = "Health Connect.zip"
DB_NAME = "health_connect_export.db"
EXPORT_TIME = "21:30"

# ExerciseSessionRecord.EXERCISE_TYPE_* constants
EXERCISE_TYPES = {"strength": 70, "running": 56, "climbing": 51}
TITLES = {"strength": "Weight training", "running": "Running", "climbing": "Climbing"}
# SleepSessionRecord.STAGE_TYPE_*: 1 awake, 4 light, 5 deep, 6 rem
_SLEEP_CYCLE = ((4, 45), (5, 25), (4, 12), (6, 22))
MEALS = ((1, 0.24, "08:05", "Greek yogurt with oats"),
         (2, 0.34, "12:35", "Chicken burrito bowl"),
         (3, 0.42, "19:40", "Salmon and rice"))
_DINNERS = {-38: "Fish tacos at Veracruz All Natural",
            -20: "Celebration dinner at Veracruz All Natural",
            -9: "Omakase at Uchi",
            -1: "Tacos at Veracruz All Natural"}

_BASE = ("uuid BLOB, app_info_id INTEGER, last_modified_time INTEGER, "
         "client_record_id TEXT, client_record_version INTEGER, "
         "device_info_id INTEGER, recording_method INTEGER, dedupe_hash BLOB")
_INTERVAL = (_BASE + ", start_time INTEGER, end_time INTEGER, "
             "start_zone_offset INTEGER, end_zone_offset INTEGER, local_date INTEGER")
_INSTANT = _BASE + ", time INTEGER, zone_offset INTEGER, local_date INTEGER"

_SCHEMA = f"""
CREATE TABLE application_info_table (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT, package_name TEXT NOT NULL UNIQUE,
  app_name TEXT, app_icon BLOB, record_types_used TEXT);
CREATE TABLE device_info_table (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT, manufacturer TEXT, model TEXT,
  device_type INTEGER);
CREATE TABLE steps_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INTERVAL}, count INTEGER NOT NULL);
CREATE TABLE distance_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INTERVAL}, distance REAL NOT NULL);
CREATE TABLE sleep_session_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INTERVAL}, notes TEXT, title TEXT);
CREATE TABLE sleep_stages_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_key INTEGER NOT NULL, stage_start_time INTEGER NOT NULL,
  stage_end_time INTEGER NOT NULL, stage_type INTEGER NOT NULL);
CREATE TABLE heart_rate_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INTERVAL});
CREATE TABLE heart_rate_record_series_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_key INTEGER NOT NULL, beats_per_minute INTEGER NOT NULL,
  epoch_millis INTEGER NOT NULL);
CREATE TABLE resting_heart_rate_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INSTANT}, beats_per_minute INTEGER NOT NULL);
CREATE TABLE weight_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INSTANT}, weight REAL NOT NULL);
CREATE TABLE exercise_session_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INTERVAL}, notes TEXT, exercise_type INTEGER NOT NULL, title TEXT,
  has_route INTEGER, planned_exercise_session_id BLOB);
CREATE TABLE nutrition_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INTERVAL}, energy REAL, protein REAL, total_fat REAL,
  total_carbohydrate REAL, sugar REAL, meal_type INTEGER, meal_name TEXT);
CREATE TABLE active_calories_burned_record_table (row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  {_INTERVAL}, energy REAL NOT NULL);
"""


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _offset(dt: datetime) -> int:
    return int(dt.utcoffset().total_seconds())


def _uuid(*parts) -> bytes:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()[:16]


class _Writer:
    def __init__(self, conn: sqlite3.Connection, anchor: date):
        self.conn, self.anchor = conn, anchor

    def interval(self, table: str, kind: str, start: datetime, end: datetime,
                 synced: datetime, **cols) -> int:
        names = ", ".join(["uuid", "app_info_id", "device_info_id", "last_modified_time",
                           "recording_method", "start_time", "end_time",
                           "start_zone_offset", "end_zone_offset", "local_date",
                           *cols])
        values = [_uuid(self.anchor, kind, start.isoformat()), 1, 1, _ms(synced), 2,
                  _ms(start), _ms(end), _offset(start), _offset(end),
                  (start.date() - date(1970, 1, 1)).days, *cols.values()]
        cur = self.conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(values))})",
            values)
        return cur.lastrowid

    def instant(self, table: str, kind: str, at: datetime, synced: datetime,
                **cols) -> None:
        names = ", ".join(["uuid", "app_info_id", "device_info_id", "last_modified_time",
                           "recording_method", "time", "zone_offset", "local_date",
                           *cols])
        values = [_uuid(self.anchor, kind, at.isoformat()), 1, 1, _ms(synced), 2,
                  _ms(at), _offset(at), (at.date() - date(1970, 1, 1)).days,
                  *cols.values()]
        self.conn.execute(
            f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * len(values))})",
            values)


def _day(w: _Writer, h: persona.HealthDay, off: int, export_at: datetime,
         anchor: date) -> None:
    rng = random.Random(f"health-connect-{anchor.isoformat()}-{off}")
    tz = h.sleep_end.tzinfo
    d = h.date

    def at(hh: int, mm: int = 0, ss: int = 0) -> datetime:
        return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=tz)

    # sleep: awake while falling asleep, then stages filling the persona's hours
    synced = h.sleep_end + timedelta(minutes=12)
    parent = w.interval("sleep_session_record_table", "sleep",
                        h.sleep_start - timedelta(minutes=9), h.sleep_end, synced,
                        title=None, notes=None)
    stages = [(h.sleep_start - timedelta(minutes=9), h.sleep_start, 1)]
    t, i = h.sleep_start, 0
    while t < h.sleep_end:
        stage, minutes = _SLEEP_CYCLE[i % len(_SLEEP_CYCLE)]
        nxt = min(t + timedelta(minutes=minutes), h.sleep_end)
        stages.append((t, nxt, stage))
        t, i = nxt, i + 1
    w.conn.executemany(
        "INSERT INTO sleep_stages_table (parent_key, stage_start_time, "
        "stage_end_time, stage_type) VALUES (?, ?, ?, ?)",
        [(parent, _ms(s), _ms(e), k) for s, e, k in stages])

    # steps: Samsung Health hands Health Connect one record per active hour
    runs = [x for x in h.workouts if x["type"] == "running"]
    run_steps = {x["start"].hour: int((x["distance_km"] or 5) * 1300) for x in runs}
    hours = list(range(7, 21))
    weights = [rng.randint(4, 12) + (8 if hr in (8, 12, 17, 18) else 0) for hr in hours]
    base = h.steps - sum(run_steps.values())
    left = base
    for hr, wt in zip(hours, weights):
        n = base * wt // sum(weights) if hr != hours[-1] else left
        left -= n
        n += run_steps.get(hr, 0)
        start, end = at(hr, 0, 30), at(hr, 59, 30)
        if end > export_at:
            continue
        w.interval("steps_record_table", "steps", start, end,
                   end + timedelta(minutes=6), count=n)

    # resting heart rate in the morning; a day's heart-rate series
    w.instant("resting_heart_rate_record_table", "rhr", at(7, 0), at(7, 15),
              beats_per_minute=h.resting_hr)
    hr_end = min(at(21), export_at)
    parent = w.interval("heart_rate_record_table", "hr-day", at(7), hr_end,
                        hr_end + timedelta(minutes=10))
    t, samples = at(7), []
    while t < hr_end:
        samples.append((parent, h.resting_hr + 8 + rng.randint(0, 22), _ms(t)))
        t += timedelta(minutes=20)
    w.conn.executemany("INSERT INTO heart_rate_record_series_table (parent_key, "
                       "beats_per_minute, epoch_millis) VALUES (?, ?, ?)", samples)

    for x in h.workouts:
        start = x["start"]
        end = start + timedelta(minutes=x["minutes"])
        w.interval("exercise_session_record_table", "exercise", start, end,
                   end + timedelta(minutes=3), exercise_type=EXERCISE_TYPES[x["type"]],
                   title=TITLES[x["type"]], notes=None,
                   has_route=1 if x["type"] == "running" else 0)
        if x["distance_km"]:
            w.interval("distance_record_table", "distance", start, end,
                       end + timedelta(minutes=3), distance=x["distance_km"] * 1000.0)
        parent = w.interval("heart_rate_record_table", "hr-workout", start, end,
                            end + timedelta(minutes=3))
        peak = 150 if x["type"] == "running" else 118
        t, samples = start + timedelta(minutes=2), []
        while t < end:
            samples.append((parent, peak + rng.randint(-12, 14), _ms(t)))
            t += timedelta(minutes=5)
        w.conn.executemany("INSERT INTO heart_rate_record_series_table (parent_key, "
                           "beats_per_minute, epoch_millis) VALUES (?, ?, ?)", samples)
        w.interval("active_calories_burned_record_table", "active-kcal", start, end,
                   end + timedelta(minutes=3), energy=x["kcal"] * 1000.0)

    if h.weight_kg is not None:
        w.instant("weight_record_table", "weight", at(7, 10), at(7, 11),
                  weight=round(h.weight_kg * 1000.0, 1))

    left = h.kcal_in
    for i, (meal_type, share, hhmm, dish) in enumerate(MEALS):
        kcal = left if i == len(MEALS) - 1 else int(h.kcal_in * share)
        left -= kcal
        hh, mm = (int(x) for x in hhmm.split(":"))
        start = at(hh, mm)
        if start > export_at:
            continue
        name = (_DINNERS.get(off) if meal_type == 3 else None) or dish
        w.interval("nutrition_record_table", f"meal-{meal_type}", start,
                   start + timedelta(minutes=20), start + timedelta(minutes=31),
                   energy=kcal * 1000.0, protein=round(kcal * 0.30 / 4, 1),
                   total_fat=round(kcal * 0.30 / 9, 1),
                   total_carbohydrate=round(kcal * 0.40 / 4, 1), sugar=None,
                   meal_type=meal_type, meal_name=name)


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo != "android":
        raise ValueError(f"Health Connect is the Android combo's source, not "
                         f"'{combo}' (the iPhone combo uses Apple Health)")
    dest.mkdir(parents=True, exist_ok=True)
    export_at = persona.local_dt(anchor, 0, EXPORT_TIME)
    build = dest / f".{DB_NAME}.build"
    build.unlink(missing_ok=True)
    conn = sqlite3.connect(str(build))
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT INTO application_info_table (row_id, package_name, "
                     "app_name, record_types_used) VALUES (1, ?, 'Samsung Health', NULL)",
                     (PACKAGE,))
        conn.execute("INSERT INTO device_info_table (row_id, manufacturer, model, "
                     "device_type) VALUES (1, 'samsung', ?, 2)",
                     (persona.DEVICE["android"]["phone"],))
        w = _Writer(conn, anchor)
        for h in persona.health_series(anchor):
            _day(w, h, (h.date - anchor).days, export_at, anchor)
        conn.commit()
    finally:
        conn.close()
    out = dest / ZIP_NAME
    info = zipfile.ZipInfo(DB_NAME,
                           date_time=export_at.replace(tzinfo=None).timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr(info, build.read_bytes())
    build.unlink()
    return out

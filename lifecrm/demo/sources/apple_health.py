"""Sample Apple Health export for the iPhone combos: export.zip as the Health
app's "Export All Health Data" writes it, rendering persona.health_series.
The file is the phone's, so iphone and windows-iphone get the same bytes.

Layout and markup follow a current (iOS 17-era) export: export.xml with its
internal DTD, ExportDate and Me first, then Records grouped by type, Food
Correlations (whose members are listed at top level too), Workouts with
WorkoutStatistics children and a route file reference, ActivitySummary rows;
alongside it export_cda.xml (a clinical document the importer must ignore)
and workout-routes/*.gpx. Every date is rendered the way the export renders
them: wall-clock time in the zone the phone is in at export time
('2026-10-21 06:12:24 -0500').

Who records what is a one-person iPhone + Apple Watch household:

- Alex’s Apple Watch: heart rate, resting heart rate, sleep stages (Core /
  Deep / REM / Awake), active and basal energy, workouts.
- Alex’s iPhone: steps, walking distance, flights climbed, and the Bedtime
  schedule's InBed samples — so every night has two recorders and the
  importer must not count it twice.
- Health (typed into the Health app): the morning weigh-ins.
- Cronometer: meals, with protein/fat/carbs and the food's name — the
  food diary whose own export the nutrition sample is, so the two agree on
  which app logged the meals.

A real iPhone + Watch export has steps from BOTH devices, and the import
report flags exactly that ('steps origins: … recorded by more than one').
This sample gives steps to the iPhone only, so the demo's daily step totals
match the story while dashboards still sum per day; docs/apple-health.md
explains the overlap. From the Lake Bled trip four years back it keeps the
two hikes the iPhone recorded (with Slovenia's HKTimeZone) — a real export
would also hold every step since the phone was new.

Units are an American's Health settings with a metric scale: miles and
Calories, kilograms for body weight (Alex weighs in kilos).
"""

from __future__ import annotations

import random
import zipfile
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from .. import persona

WATCH = "Alex’s Apple Watch"
PHONE = "Alex’s iPhone"
HEALTH_APP = "Health"
FOOD_APP = "Cronometer"

_WATCH_DEV = ("<<HKDevice: 0x30282a9e0>, name:Apple Watch, manufacturer:Apple Inc., "
              "model:Watch, hardware:Watch7,4, software:10.6>")
_PHONE_DEV = ("<<HKDevice: 0x30282b1c0>, name:iPhone, manufacturer:Apple Inc., "
              "model:iPhone, hardware:iPhone16,1, software:17.6.1>")
_VERSIONS = {WATCH: "10.6", PHONE: "17.6.1", HEALTH_APP: "17.6.1", FOOD_APP: "4.31.2"}
_DEVICES = {WATCH: _WATCH_DEV, PHONE: _PHONE_DEV}

_KM_PER_MI = 1.609344
_STRIDE_KM = 0.000762
EXPORT_TIME = "21:30"                 # the moment the owner tapped Export

_ACTIVITY = {"strength": "HKWorkoutActivityTypeTraditionalStrengthTraining",
             "running": "HKWorkoutActivityTypeRunning",
             "climbing": "HKWorkoutActivityTypeClimbing"}
# (fraction of the day's food energy, local time, typical dish)
_MEALS = (("breakfast", 0.24, "08:05", ("Greek yogurt with oats", "Egg tacos",
                                          "Overnight oats")),
          ("lunch", 0.34, "12:35", ("Chicken burrito bowl", "Turkey chili",
                                     "Grain bowl")),
          ("dinner", 0.42, "19:40", ("Salmon and rice", "Chicken thighs and greens",
                                      "Black bean tacos")))
# Dinners the rest of the story pins down (the receipts and texts agree).
_DINNERS = {-38: "Fish tacos at Veracruz All Natural",
            -20: "Celebration dinner at Veracruz All Natural",
            -9: "Omakase at Uchi",
            -1: "Tacos at Veracruz All Natural"}


_HOME = ZoneInfo(persona.TIMEZONE)


@lru_cache(maxsize=1 << 15)
def _fmt(dt: datetime) -> str:
    """How the export renders a date: wall clock in the export-time zone."""
    t = dt.astimezone(_HOME)
    off = int(t.utcoffset().total_seconds()) // 60
    sign = "-" if off < 0 else "+"
    return (f"{t.year:04d}-{t.month:02d}-{t.day:02d} {t.hour:02d}:{t.minute:02d}:"
            f"{t.second:02d} {sign}{abs(off) // 60:02d}{abs(off) % 60:02d}")


def _q(v: str) -> str:
    if "&" in v or "<" in v or ">" in v or '"' in v:
        return escape(v, {'"': "&quot;"})
    return v


def _num(v: float) -> str:
    return f"{v:.7g}"


def _attrs(**kw) -> str:
    return " ".join(f'{k}="{_q(str(v))}"' for k, v in kw.items() if v is not None)


class _Doc:
    def __init__(self):
        self.records: dict[str, list[str]] = {}
        self.tail: list[str] = []

    def record(self, hk_type: str, source: str, start: datetime, end: datetime,
               value, unit: str | None = None, created: datetime | None = None,
               meta: dict | None = None, indent: str = " ") -> str:
        head = (f"{indent}<Record " + _attrs(
            type=hk_type, sourceName=source, sourceVersion=_VERSIONS.get(source),
            device=_DEVICES.get(source), unit=unit,
            creationDate=_fmt(created or end + timedelta(minutes=3)),
            startDate=_fmt(start), endDate=_fmt(end), value=value))
        if not meta:
            return head + "/>"
        inner = "".join(f'\n{indent}  <MetadataEntry {_attrs(key=k, value=v)}/>'
                        for k, v in meta.items())
        return f"{head}>{inner}\n{indent}</Record>"

    def add(self, hk_type: str, *args, **kw) -> None:
        self.records.setdefault(hk_type, []).append(self.record(hk_type, *args, **kw))


Q = "HKQuantityTypeIdentifier"
SLEEP = "HKCategoryTypeIdentifierSleepAnalysis"
_STAGE = "HKCategoryValueSleepAnalysis"
_SLEEP_CYCLE = (("AsleepCore", 45), ("AsleepDeep", 25), ("AsleepCore", 12),
                ("AsleepREM", 22))


def _sleep(doc: _Doc, h: persona.HealthDay) -> None:
    tz = {"HKTimeZone": persona.TIMEZONE}
    synced = h.sleep_end + timedelta(minutes=7)
    # The iPhone's Bedtime schedule: in bed a little before, up a little after.
    doc.add(SLEEP, PHONE, h.sleep_start - timedelta(minutes=16),
            h.sleep_end + timedelta(minutes=4), _STAGE + "InBed", created=synced)
    # The watch: awake while falling asleep, then cycles filling exactly the
    # hours the persona slept, then awake again on waking.
    doc.add(SLEEP, WATCH, h.sleep_start - timedelta(minutes=9), h.sleep_start,
            _STAGE + "Awake", created=synced, meta=tz)
    t, i = h.sleep_start, 0
    while t < h.sleep_end:
        stage, minutes = _SLEEP_CYCLE[i % len(_SLEEP_CYCLE)]
        nxt = min(t + timedelta(minutes=minutes), h.sleep_end)
        doc.add(SLEEP, WATCH, t, nxt, _STAGE + stage, created=synced, meta=tz)
        t, i = nxt, i + 1
    doc.add(SLEEP, WATCH, h.sleep_end, h.sleep_end + timedelta(minutes=3),
            _STAGE + "Awake", created=synced, meta=tz)


def _hours_split(total: int, hours: list[int], rng: random.Random) -> dict[int, int]:
    weights = [rng.randint(4, 12) + (8 if hr in (8, 12, 17, 18) else 0) for hr in hours]
    out, left = {}, total
    for hr, w in zip(hours, weights):
        out[hr] = total * w // sum(weights)
        left -= out[hr]
    out[hours[-1]] += left
    return out


def _day(doc: _Doc, h: persona.HealthDay, off: int, export_at: datetime,
         anchor: date) -> None:
    rng = random.Random(f"apple-health-{anchor.isoformat()}-{off}")
    tz = ZoneInfo(persona.TIMEZONE)
    d = h.date

    def at(hh: int, mm: int = 0, ss: int = 0) -> datetime:
        return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=tz)

    _sleep(doc, h)

    # steps + walking distance from the phone, two samples an hour
    runs = [w for w in h.workouts if w["type"] == "running"]
    run_steps = sum(int((w["distance_km"] or 5) * 1300) for w in runs)
    hours = list(range(7, 21))
    per_hour = _hours_split(h.steps - run_steps, hours, rng)
    for w in runs:
        per_hour[w["start"].hour] = per_hour.get(w["start"].hour, 0) + \
            int((w["distance_km"] or 5) * 1300)
    for hr in sorted(per_hour):
        steps = per_hour[hr]
        run_km = sum(w["distance_km"] or 5 for w in runs if w["start"].hour == hr)
        walk_steps = steps - sum(int((w["distance_km"] or 5) * 1300)
                                 for w in runs if w["start"].hour == hr)
        halves = (steps // 2, steps - steps // 2)
        for (m0, m1), n in zip(((4, 19), (33, 48)), halves):
            start, end = at(hr, m0, 11), at(hr, m1, 52)
            if end > export_at:
                continue
            share = n / steps if steps else 0
            km = (walk_steps * _STRIDE_KM + run_km) * share
            doc.add(Q + "StepCount", PHONE, start, end, n, "count")
            doc.add(Q + "DistanceWalkingRunning", PHONE, start, end,
                    _num(km / _KM_PER_MI), "mi")
    if off % 4 == 0:
        doc.add(Q + "FlightsClimbed", PHONE, at(9, 12), at(9, 13), 2, "count")

    # heart rate every 20 minutes while awake, every 5 during a workout
    busy = [(w["start"], w["start"] + timedelta(minutes=w["minutes"]), w)
            for w in h.workouts]
    t = at(7)
    while t <= at(21) and t <= export_at:
        bpm = h.resting_hr + 8 + rng.randint(0, 22)
        doc.add(Q + "HeartRate", WATCH, t, t, bpm, "count/min",
                created=t + timedelta(minutes=2),
                meta={"HKMetadataKeyHeartRateMotionContext": "0"})
        t += timedelta(minutes=20)
    for ws, we, w in busy:
        t = ws + timedelta(minutes=2)
        while t < we:
            peak = 150 if w["type"] == "running" else 118
            doc.add(Q + "HeartRate", WATCH, t, t, peak + rng.randint(-12, 14),
                    "count/min", created=we + timedelta(minutes=4),
                    meta={"HKMetadataKeyHeartRateMotionContext": "2"})
            t += timedelta(minutes=5)
    doc.add(Q + "RestingHeartRate", WATCH, at(6, 0, 3), at(20, 58, 41),
            h.resting_hr, "count/min", created=at(20, 59, 30))
    doc.add(Q + "HeartRateVariabilitySDNN", WATCH, h.sleep_end - timedelta(hours=2),
            h.sleep_end - timedelta(hours=2) + timedelta(seconds=58),
            _num(38 + rng.uniform(-6, 9)), "ms")

    # energy: basal every hour, active in the waking hours, workouts on top
    for hr in range(24):
        start, end = at(hr), at(hr) + timedelta(minutes=59, seconds=59)
        if end > export_at:
            break
        doc.add(Q + "BasalEnergyBurned", WATCH, start, end,
                _num(67.5 + rng.uniform(-1.5, 1.5)), "Cal")
        if 7 <= hr <= 21:
            active = 9 + rng.uniform(0, 14)
            active += sum(w["kcal"] * 0.85 for ws, _, w in busy if ws.hour == hr)
            doc.add(Q + "ActiveEnergyBurned", WATCH, start, end, _num(active), "Cal")

    if h.weight_kg is not None:
        doc.add(Q + "BodyMass", HEALTH_APP, at(7, 10), at(7, 10), _num(h.weight_kg),
                "kg", created=at(7, 10, 26))

    # meals: Food correlations whose members are also top-level Records
    left = h.kcal_in
    for i, (meal, share, hhmm, dishes) in enumerate(_MEALS):
        kcal = left if i == len(_MEALS) - 1 else int(h.kcal_in * share)
        left -= kcal
        hh, mm = (int(x) for x in hhmm.split(":"))
        when = at(hh, mm)
        if when > export_at:
            continue
        name = _DINNERS.get(off) if meal == "dinner" else None
        name = name or dishes[(off + i) % len(dishes)]
        logged = when + timedelta(minutes=11)
        members = [
            ("DietaryEnergyConsumed", _num(kcal), "Cal"),
            ("DietaryProtein", _num(round(kcal * 0.30 / 4, 1)), "g"),
            ("DietaryFatTotal", _num(round(kcal * 0.30 / 9, 1)), "g"),
            ("DietaryCarbohydrates", _num(round(kcal * 0.40 / 4, 1)), "g"),
        ]
        meta = {"HKFoodType": name}
        nested = []
        for t_name, v, unit in members:
            doc.add(Q + t_name, FOOD_APP, when, when, v, unit, created=logged, meta=meta)
            nested.append(doc.record(Q + t_name, FOOD_APP, when, when, v, unit,
                                     created=logged, meta=meta, indent="  "))
        doc.tail.append(
            " <Correlation " + _attrs(
                type="HKCorrelationTypeIdentifierFood", sourceName=FOOD_APP,
                sourceVersion=_VERSIONS[FOOD_APP], creationDate=_fmt(logged),
                startDate=_fmt(when), endDate=_fmt(when)) + ">\n"
            + f'  <MetadataEntry {_attrs(key="HKFoodType", value=name)}/>\n'
            + "\n".join(nested) + "\n </Correlation>")


def _workout(w: dict, source: str, zone: str, routes: dict, km_override=None,
             activity: str | None = None) -> str:
    start = w["start"]
    end = start + timedelta(minutes=w["minutes"])
    km = km_override if km_override is not None else w.get("distance_km")
    activity = activity or _ACTIVITY[w["type"]]
    meta = {"HKIndoorWorkout": "0" if km else "1", "HKTimeZone": zone,
            "HKAverageMETs": f"{6.1 if km else 4.4} kcal/hr·kg"}
    lines = [" <Workout " + _attrs(
        workoutActivityType=activity, duration=_num(w["minutes"]), durationUnit="min",
        sourceName=source, sourceVersion=_VERSIONS[source], device=_DEVICES[source],
        creationDate=_fmt(end + timedelta(minutes=1)), startDate=_fmt(start),
        endDate=_fmt(end)) + ">"]
    lines += [f'  <MetadataEntry {_attrs(key=k, value=v)}/>' for k, v in meta.items()]
    lines.append(f'  <WorkoutEvent {_attrs(type="HKWorkoutEventTypeSegment", date=_fmt(start), duration=_num(w["minutes"]), durationUnit="min")}/>')
    span = {"startDate": _fmt(start), "endDate": _fmt(end)}
    lines.append(f'  <WorkoutStatistics {_attrs(type=Q + "ActiveEnergyBurned", **span, sum=_num(w["kcal"]), unit="Cal")}/>')
    lines.append(f'  <WorkoutStatistics {_attrs(type=Q + "BasalEnergyBurned", **span, sum=_num(round(w["minutes"] * 1.13, 1)), unit="Cal")}/>')
    if km:
        lines.append(f'  <WorkoutStatistics {_attrs(type=Q + "DistanceWalkingRunning", **span, sum=_num(km / _KM_PER_MI), unit="mi")}/>')
    avg = 148 if w["type"] == "running" else (104 if w["type"] == "hiking" else 121)
    lines.append(f'  <WorkoutStatistics {_attrs(type=Q + "HeartRate", **span, average=_num(avg + 0.4), minimum=avg - 31, maximum=avg + 24, unit="count/min")}/>')
    if km:
        local = start.astimezone(ZoneInfo(zone))          # route_2026-09-15_6.10pm.gpx
        name = (f"route_{local:%Y-%m-%d}_{local.hour % 12 or 12}.{local:%M}"
                f"{'pm' if local.hour >= 12 else 'am'}.gpx")
        routes[name] = (start, end, w.get("place", "zilker"))
        lines.append("  <WorkoutRoute " + _attrs(
            sourceName=source, sourceVersion=_VERSIONS[source],
            creationDate=_fmt(end + timedelta(minutes=2)), startDate=_fmt(start),
            endDate=_fmt(end)) + ">")
        lines.append('   <MetadataEntry key="HKMetadataKeySyncVersion" value="2"/>')
        lines.append('   <MetadataEntry key="HKMetadataKeySyncIdentifier" '
                     f'value="{_q(name[:-4])}"/>')
        lines.append(f'   <FileReference path="/workout-routes/{_q(name)}"/>')
        lines.append("  </WorkoutRoute>")
    lines.append(" </Workout>")
    return "\n".join(lines)


def _gpx(start: datetime, end: datetime, place_key: str) -> str:
    p = persona.place(place_key)
    pts, t, i = [], start, 0
    while t <= end:
        lat = p.lat + 0.0021 * ((i % 10) - 5) / 5
        lon = p.lon + 0.0018 * (((i + 3) % 10) - 5) / 5
        stamp = t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pts.append(f'<trkpt lon="{lon:.6f}" lat="{lat:.6f}"><ele>{150 + i % 7}.0</ele>'
                   f"<time>{stamp}</time></trkpt>")
        t += timedelta(minutes=2)
        i += 1
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" '
            'creator="Apple Health Export" xmlns="http://www.topografix.com/GPX/1/1">\n'
            f'<trk><name>Route {start.astimezone(timezone.utc):%Y-%m-%d}</name><trkseg>\n'
            + "\n".join(pts) + "\n</trkseg></trk>\n</gpx>\n")


_DTD = """<!DOCTYPE HealthData [
<!-- HealthKit Export Version: 14 -->
<!ELEMENT HealthData (ExportDate,Me,(Record|Correlation|Workout|ActivitySummary|ClinicalRecord|Audiogram|VisionPrescription)*)>
<!ATTLIST HealthData
  locale CDATA #REQUIRED
>
<!ELEMENT ExportDate EMPTY>
<!ATTLIST ExportDate
  value CDATA #REQUIRED
>
<!ELEMENT Me EMPTY>
<!ATTLIST Me
  HKCharacteristicTypeIdentifierDateOfBirth                   CDATA #REQUIRED
  HKCharacteristicTypeIdentifierBiologicalSex                 CDATA #REQUIRED
  HKCharacteristicTypeIdentifierBloodType                     CDATA #REQUIRED
  HKCharacteristicTypeIdentifierFitzpatrickSkinType           CDATA #REQUIRED
  HKCharacteristicTypeIdentifierCardioFitnessMedicationsUse   CDATA #REQUIRED
>
<!ELEMENT Record ((MetadataEntry|HeartRateVariabilityMetadataList)*)>
<!ATTLIST Record
  type          CDATA #REQUIRED
  unit          CDATA #IMPLIED
  value         CDATA #IMPLIED
  sourceName    CDATA #REQUIRED
  sourceVersion CDATA #IMPLIED
  device        CDATA #IMPLIED
  creationDate  CDATA #IMPLIED
  startDate     CDATA #REQUIRED
  endDate       CDATA #REQUIRED
>
<!-- Note: Any Records that appear as children of a correlation also appear as top-level records in this document. -->
<!ELEMENT Correlation ((MetadataEntry|Record)*)>
<!ATTLIST Correlation
  type          CDATA #REQUIRED
  sourceName    CDATA #REQUIRED
  sourceVersion CDATA #IMPLIED
  device        CDATA #IMPLIED
  creationDate  CDATA #IMPLIED
  startDate     CDATA #REQUIRED
  endDate       CDATA #REQUIRED
>
<!ELEMENT Workout ((MetadataEntry|WorkoutEvent|WorkoutRoute|WorkoutStatistics)*)>
<!ATTLIST Workout
  workoutActivityType   CDATA #REQUIRED
  duration              CDATA #IMPLIED
  durationUnit          CDATA #IMPLIED
  totalDistance         CDATA #IMPLIED
  totalDistanceUnit     CDATA #IMPLIED
  totalEnergyBurned     CDATA #IMPLIED
  totalEnergyBurnedUnit CDATA #IMPLIED
  sourceName            CDATA #REQUIRED
  sourceVersion         CDATA #IMPLIED
  device                CDATA #IMPLIED
  creationDate          CDATA #IMPLIED
  startDate             CDATA #REQUIRED
  endDate               CDATA #REQUIRED
>
<!ELEMENT WorkoutStatistics EMPTY>
<!ATTLIST WorkoutStatistics
  type                  CDATA #REQUIRED
  startDate             CDATA #REQUIRED
  endDate               CDATA #REQUIRED
  average               CDATA #IMPLIED
  minimum               CDATA #IMPLIED
  maximum               CDATA #IMPLIED
  sum                   CDATA #IMPLIED
  unit                  CDATA #IMPLIED
>
<!ELEMENT ActivitySummary EMPTY>
<!ATTLIST ActivitySummary
  dateComponents           CDATA #IMPLIED
  activeEnergyBurned       CDATA #IMPLIED
  activeEnergyBurnedGoal   CDATA #IMPLIED
  activeEnergyBurnedUnit   CDATA #IMPLIED
  appleMoveTime            CDATA #IMPLIED
  appleMoveTimeGoal        CDATA #IMPLIED
  appleExerciseTime        CDATA #IMPLIED
  appleExerciseTimeGoal    CDATA #IMPLIED
  appleStandHours          CDATA #IMPLIED
  appleStandHoursGoal      CDATA #IMPLIED
>
<!ELEMENT MetadataEntry EMPTY>
<!ATTLIST MetadataEntry
  key   CDATA #REQUIRED
  value CDATA #REQUIRED
>
]>
"""

_CDA = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="urn:hl7-org:v3 ../../../CDA%20R2/cda-schemas-and-samples/infrastructure/cda/CDA.xsd" classCode="DOCCLIN" moodCode="EVN">
 <realmCode code="US"/>
 <typeId root="2.16.840.1.113883.1.3" extension="POCD_HD000040"/>
 <title>Health Data Export</title>
 <effectiveTime value="{stamp}"/>
 <component><structuredBody/></component>
</ClinicalDocument>
"""


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo not in persona.COMBOS or not persona.has_iphone(combo):
        raise ValueError(f"Apple Health is the iPhone combo's source, not '{combo}' "
                         f"(the Android combo uses Health Connect)")
    dest.mkdir(parents=True, exist_ok=True)
    export_at = persona.local_dt(anchor, 0, EXPORT_TIME)
    doc = _Doc()
    workouts, routes = [], {}
    summaries = []
    for h in persona.health_series(anchor):
        off = (h.date - anchor).days
        _day(doc, h, off, export_at, anchor)
        for w in h.workouts:
            workouts.append(_workout(w, WATCH, persona.TIMEZONE, routes))
        summaries.append(" <ActivitySummary " + _attrs(
            dateComponents=h.date.isoformat(),
            activeEnergyBurned=_num(sum(w["kcal"] for w in h.workouts) + 240),
            activeEnergyBurnedGoal="600", activeEnergyBurnedUnit="Cal",
            appleMoveTime="0", appleMoveTimeGoal="0",
            appleExerciseTime=str(sum(w["minutes"] for w in h.workouts) + 9),
            appleExerciseTimeGoal="30", appleStandHours="11",
            appleStandHoursGoal="12") + "/>")

    # Four years back: the two hikes the iPhone recorded on the Bled trip.
    trip = []
    for trip_day, hhmm, minutes, km, place in ((1, "09:40", 104, 6.4, "bled"),
                                               (3, "10:15", 131, 8.9, "bled_castle")):
        start = persona.trip_dt(trip_day, hhmm, anchor)
        w = {"type": "hiking", "start": start, "minutes": minutes,
             "distance_km": km, "kcal": int(minutes * 5.2), "place": place}
        trip.append(_workout(w, PHONE, "Europe/Ljubljana", routes,
                             activity="HKWorkoutActivityTypeHiking"))

    owner = persona.OWNER
    born = f"{owner.birth_year}-{owner.birthday[0]:02d}-{owner.birthday[1]:02d}"
    body = ['<?xml version="1.0" encoding="UTF-8"?>', _DTD.rstrip("\n"),
            '<HealthData locale="en_US">',
            f' <ExportDate value="{_fmt(export_at)}"/>',
            " <Me " + _attrs(
                HKCharacteristicTypeIdentifierDateOfBirth=born,
                HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet",
                HKCharacteristicTypeIdentifierBloodType="HKBloodTypeNotSet",
                HKCharacteristicTypeIdentifierFitzpatrickSkinType="HKFitzpatrickSkinTypeNotSet",
                HKCharacteristicTypeIdentifierCardioFitnessMedicationsUse="None") + "/>"]
    for hk_type in sorted(doc.records):
        body.extend(doc.records[hk_type])
    body.extend(doc.tail)
    body.extend(trip + workouts)
    body.extend(summaries)
    body.append("</HealthData>")
    xml = "\n".join(body) + "\n"

    out = dest / "export.zip"
    stamp = export_at.replace(tzinfo=None).timetuple()[:6]

    def put(zf, name, text):
        info = zipfile.ZipInfo(name, date_time=stamp)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, text.encode("utf-8"))

    with zipfile.ZipFile(out, "w") as zf:
        put(zf, "apple_health_export/export.xml", xml)
        put(zf, "apple_health_export/export_cda.xml",
            _CDA.format(stamp=export_at.strftime("%Y%m%d%H%M%S%z")))
        for name, (start, end, place) in sorted(routes.items()):
            put(zf, f"apple_health_export/workout-routes/{name}", _gpx(start, end, place))
    return out

"""Read an iPhone Health export ("Export All Health Data") into health_records.

The Health app's export is a zip around one XML file,
apple_health_export/export.xml, holding every sample HealthKit has — easily
hundreds of MB and millions of <Record> elements. It is read as a stream
(ElementTree.iterparse, each top-level element cleared once handled) straight
out of the zip: nothing is extracted and memory stays flat. The unzipped
`apple_health_export` folder (Safari on a Mac unzips downloads) and a bare
export.xml are accepted too.

Rows land in the same health_records tier as Health Connect's and go through
the same plan/apply path (health_connect.build_plan / apply_plan): one dedupe
rule, one newer-wins rule, one conflict rule, one retract/restore. Metric
names and canonical units are exactly the ones Health Connect rows use, so
`status`, doctor, dashboards and `ask` read an iPhone's data unchanged.

What differs from Health Connect, and why:

- **No record ids.** The export carries no HealthKit UUIDs, so identity is a
  hash of what the export keeps stable: type, sourceName, and the UTC start
  and end instants (plus an ordinal among same-instant samples). The value is
  deliberately NOT part of it — the export renders values in the phone's
  preferred unit, so switching lb -> kg would otherwise re-key every weight.
  A sample deleted and re-entered at the same instant keeps its key and has a
  newer creationDate, so it lands as a newer-wins update. Ids are prefixed
  'ah1:' (Apple Health, recipe 1) so the recipe can change without colliding.
- **Dates are rendered in the zone the phone was in AT EXPORT TIME**, not the
  zone the sample was recorded in. The string's offset is used only to find
  the instant. Journal dates come from the sample's HKTimeZone metadata when
  it has one, else the configured timezone — taking the offset from the
  string would move journal_date (a compared field) on every record whenever
  an export is taken while travelling, and the whole re-import would read as
  conflicts.
- **Volume.** Steps, heart rate, distance and energy are written every few
  minutes by every device. They are summed (averaged, for heart rate) into one
  row per type, source and UTC hour — Health Connect's heart_rate rows are
  series aggregates too — which keeps a year in the tens of thousands of rows
  instead of millions. Sums accumulate as integer micro-units, so a total
  does not depend on sample order: a float differing in its last digit would
  otherwise read as a CONFLICT on re-import.
- **Two recorders by default.** An iPhone and an Apple Watch both count steps.
  Each row keeps its own sourceName as origin_app (the export names devices,
  not bundle ids), and the dry-run report says when a summed metric has more
  than one origin on the same day, because a daily total over both would
  double-count.
- **Every export is named export.zip**, so the retract key is the file name
  plus the export's own timestamp: 'export.zip#2026-07-28T15:15:22Z'.

Sleep: Apple writes one category sample per stage (InBed, Asleep, Core, Deep,
REM, Awake). Samples from one source are grouped into sessions (a new session
after a 90-minute gap) and stored as minutes ASLEEP — the union of the asleep
intervals, so an InBed sample spanning the same night is never added on top.
A session with no asleep time at all — the iPhone's Bedtime schedule writes
InBed only — keeps its in-bed minutes in detail with no value, so a night the
phone and the watch both recorded is not counted twice. A session belongs to
the calendar day you woke up, as the Health app shows it: the 05:00 rollover
is for notes, and applying it here would date a 04:30 wake-up to the day
before, stacking two nights on one day.

Like the Health Connect reader, this never guesses: a unit it does not know
is skipped and counted, types it does not map are reported with counts, and
nothing is written outside an --apply run. The export itself is never
modified.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from . import health_connect as hc
from .config import Config
from .domain import days, health
from .domain.util import DomainError
from .recall import HEALTH_METRICS

FORMAT = "apple_health"
ID_PREFIX = "ah1:"

_QTY = "HKQuantityTypeIdentifier"
_CAT = "HKCategoryTypeIdentifier"
_COR = "HKCorrelationTypeIdentifier"
SLEEP = _CAT + "SleepAnalysis"
FOOD = _COR + "Food"
ENERGY_IN = _QTY + "DietaryEnergyConsumed"

# Daily totals are SUMs for these, so two recorders on one day double-count.
SUM_METRICS = frozenset(m for m, spec in HEALTH_METRICS.items()
                        if spec["agg"] == "sum")

# ---------------------------------------------------------------- units
# Export unit string -> factor to the canonical unit Health Connect rows use.
# Anything not listed is skipped and counted, never guessed.

MASS_KG = {"kg": 1.0, "g": 0.001, "lb": 0.45359237, "st": 6.35029318,
           "oz": 0.028349523125}
ENERGY_KCAL = {"kcal": 1.0, "Cal": 1.0, "kJ": 1 / 4.184}
LENGTH_KM = {"km": 1.0, "m": 0.001, "mi": 1.609344, "ft": 0.0003048,
             "yd": 0.0009144}
VOLUME_ML = {"mL": 1.0, "L": 1000.0, "fl_oz_us": 29.5735295625,
             "fl_oz_imp": 28.4130625, "cup_us": 236.5882365}
MACRO_G = {"g": 1.0, "mg": 0.001, "kg": 1000.0, "oz": 28.349523125}
BPM = {"count/min": 1.0}
DURATION_MIN = {"min": 1.0, "s": 1 / 60, "sec": 1 / 60, "hr": 60.0, "h": 60.0}


@dataclass(frozen=True)
class Quantity:
    metric: str
    units: dict                    # export unit -> factor to `unit`
    unit: str                      # canonical unit stored
    digits: int                    # rounding of the stored value
    bucket: str | None = None      # hourly bucket family; None = row per sample
    activity: str | None = None    # distance: which kind, kept in detail
    part: str | None = None        # energy: 'active' | 'basal'


# Rounding sits well inside each sensor's precision, so a unit-preference
# switch between exports (values re-rendered in another unit) still compares
# identical instead of differing in the last digit.
QUANTITIES: dict[str, Quantity] = {
    _QTY + "StepCount": Quantity("steps", {"count": 1.0}, "count", 3,
                                 bucket="StepCount"),
    _QTY + "HeartRate": Quantity("heart_rate", BPM, "bpm", 1, bucket="HeartRate"),
    _QTY + "DistanceWalkingRunning": Quantity(
        "distance", LENGTH_KM, "km", 3, bucket="DistanceWalkingRunning",
        activity="walking_running"),
    _QTY + "DistanceCycling": Quantity("distance", LENGTH_KM, "km", 3,
                                       bucket="DistanceCycling", activity="cycling"),
    _QTY + "DistanceSwimming": Quantity("distance", LENGTH_KM, "km", 3,
                                        bucket="DistanceSwimming", activity="swimming"),
    _QTY + "DistanceWheelchair": Quantity("distance", LENGTH_KM, "km", 3,
                                          bucket="DistanceWheelchair",
                                          activity="wheelchair"),
    # total_calories is Health Connect's TotalCaloriesBurned: active + basal.
    # Apple splits the two, so both feed one hourly row with the split kept
    # in detail; active energy alone would understate the metric's meaning.
    _QTY + "ActiveEnergyBurned": Quantity("total_calories", ENERGY_KCAL, "kcal", 1,
                                          bucket="Energy", part="active"),
    _QTY + "BasalEnergyBurned": Quantity("total_calories", ENERGY_KCAL, "kcal", 1,
                                         bucket="Energy", part="basal"),
    _QTY + "RestingHeartRate": Quantity("resting_heart_rate", BPM, "bpm", 1),
    _QTY + "BodyMass": Quantity("weight", MASS_KG, "kg", 2),
    # HealthKit percentages are fractions (0.183 = 18.3 %).
    _QTY + "BodyFatPercentage": Quantity("body_fat", {"%": 100.0}, "%", 2),
    ENERGY_IN: Quantity("nutrition", ENERGY_KCAL, "kcal", 1),
    _QTY + "DietaryWater": Quantity("hydration", VOLUME_ML, "ml", 1),
}

# Logged inside a Food correlation next to its energy: folded into the
# nutrition row's detail (Health Connect keeps the same keys as columns).
MACROS = {_QTY + "DietaryProtein": "protein_g", _QTY + "DietaryFatTotal": "fat_g",
          _QTY + "DietaryCarbohydrates": "carbs_g", _QTY + "DietarySugar": "sugar_g"}

# Types that can sit inside a Correlation. The export may list such a record
# both at top level and nested; these keep a signature so it counts once.
_NESTABLE = (_QTY + "Dietary", _QTY + "BloodPressure")

# Stage names follow Health Connect's (Core = light) so platforms compare.
SLEEP_STAGES = {
    "HKCategoryValueSleepAnalysisInBed": "in_bed", "0": "in_bed",
    "HKCategoryValueSleepAnalysisAsleep": "sleeping",
    "HKCategoryValueSleepAnalysisAsleepUnspecified": "sleeping", "1": "sleeping",
    "HKCategoryValueSleepAnalysisAwake": "awake", "2": "awake",
    "HKCategoryValueSleepAnalysisAsleepCore": "light", "3": "light",
    "HKCategoryValueSleepAnalysisAsleepDeep": "deep", "4": "deep",
    "HKCategoryValueSleepAnalysisAsleepREM": "rem", "5": "rem",
}
ASLEEP = frozenset({"sleeping", "light", "deep", "rem"})
SESSION_GAP_SEC = 90 * 60

_ROOT_TAG = b"<HealthData"
_HEAD_LIMIT = 8 * 1024 * 1024        # the DTD is ~10 KB; give up looking well past it
_SNIFF = 256 * 1024


# ---------------------------------------------------------------- small helpers

@lru_cache(maxsize=1 << 16)
def _epoch(s: str | None) -> int | None:
    """Epoch seconds for an export date ('2026-07-27 21:30:05 -0500').

    fromisoformat on the reshaped string is ~20x faster than strptime, which
    matters at millions of records; strptime is the fallback for any other
    shape. A date without an offset is unreadable, not local time.
    """
    if not s:
        return None
    try:
        if len(s) == 25 and s[19] == " ":
            return int(datetime.fromisoformat(s[:19] + s[20:]).timestamp())
        return int(datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S %z").timestamp())
    except ValueError:
        return None


def _iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@lru_cache(maxsize=256)
def _zone(name: str | None) -> str | None:
    """The HKTimeZone name if this machine can load it, else None."""
    if not name:
        return None
    try:
        ZoneInfo(name)
        return name
    except Exception:            # ZoneInfoNotFoundError, ValueError on odd keys
        return None


def _journal_date(epoch: int, zone: str | None, cfg: Config,
                  rollover: bool = True) -> str:
    """Journal date in the sample's zone. `rollover=False` is the wake-day
    rule for sleep: the rollover exists so a 00:30 note is still 'yesterday',
    but a night you woke from at 04:30 is that morning's sleep — dating it
    to the day before would put two nights on one day."""
    return days.journal_date_for(datetime.fromtimestamp(epoch, tz=timezone.utc),
                                 zone or cfg.timezone,
                                 cfg.day_rollover_hour if rollover else 0)


def _offset(epoch: int, zone: str | None) -> int | None:
    if not zone:
        return None
    return int(datetime.fromtimestamp(epoch, tz=ZoneInfo(zone))
               .utcoffset().total_seconds())


def _ext_id(*fields: str) -> str:
    digest = hashlib.sha256("\x1f".join(fields).encode("utf-8")).hexdigest()
    return ID_PREFIX + digest[:32]


def _micro(v: float) -> int:
    return round(v * 1_000_000)


def _num(v: float):
    """Detail numbers: whole values as ints (72, not 72.0), else one decimal."""
    return int(v) if float(v).is_integer() else round(v, 1)


def _meta(el) -> dict:
    return {m.get("key"): m.get("value") for m in el if m.tag == "MetadataEntry"}


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower() or "other"


def short_type(hk_type: str) -> str:
    """'HKQuantityTypeIdentifierFlightsClimbed' -> 'FlightsClimbed'."""
    for prefix in (_QTY, _CAT, _COR, "HKDataType"):
        if hk_type.startswith(prefix):
            return hk_type[len(prefix):]
    return hk_type


def _union_sec(intervals: list[tuple[int, int]]) -> int:
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


# ---------------------------------------------------------------- opening

class _Prefixed:
    """A read()-able stream: `head` bytes first, then the rest of `src`."""

    def __init__(self, head: bytes, src):
        self._head, self._src = head, src

    def read(self, n: int = -1) -> bytes:
        if self._head:
            if n is None or n < 0:
                out, self._head = self._head + self._src.read(), b""
                return out
            out, self._head = self._head[:n], self._head[n:]
            return out
        return self._src.read(n)


def _without_dtd(src) -> _Prefixed:
    """The stream with export.xml's internal DTD blanked out.

    The DTD carries nothing this reader uses (no entities, no defaults it
    relies on), and dropping it means a malformed or changed DTD in some iOS
    version cannot fail an import of well-formed records. Its newlines are
    kept so parse errors still name the real line.
    """
    head = b""
    while _ROOT_TAG not in head and len(head) < _HEAD_LIMIT:
        chunk = src.read(65536)
        if not chunk:
            break
        head += chunk
    root = head.find(_ROOT_TAG)
    start = head.find(b"<!DOCTYPE")
    if root != -1 and 0 <= start < root:
        if b"[" in head[start:root]:
            ends = list(re.finditer(rb"\]\s*>", head[start:root]))
            end = start + ends[-1].end() if ends else -1
        else:
            gt = head.find(b">", start, root)
            end = gt + 1 if gt != -1 else -1
        if end > start:
            head = head[:start] + b"\n" * head.count(b"\n", start, end) + head[end:]
    return _Prefixed(head, src)


def _looks_like_export(head: bytes) -> bool:
    return _ROOT_TAG in head


def _xml_candidates(names: list[str]) -> list[str]:
    """export.xml first (some locales localize the name), shallowest first (a
    re-zipped folder nests it deeper), never the CDA clinical document or
    Finder's __MACOSX shadows. Each candidate's root is still checked."""
    out = []
    for n in names:
        base = n.rsplit("/", 1)[-1].lower()
        if not base.endswith(".xml") or n.startswith("__MACOSX/") or \
                base.startswith("._") or "_cda" in base:
            continue
        out.append(n)
    return sorted(out, key=lambda n: (n.rsplit("/", 1)[-1].lower() != "export.xml",
                                      n.count("/"), n))


def open_export(path: Path):
    """(binary stream, inner name, close()) for export.zip, an unzipped
    apple_health_export folder, or export.xml itself."""
    if path.is_dir():
        for d in (path, path / "apple_health_export"):
            for name in _xml_candidates([p.name for p in sorted(d.glob("*.xml"))]):
                f = open(d / name, "rb")
                head = f.read(_SNIFF)
                f.seek(0)
                if _looks_like_export(head):
                    inner = (d / name).relative_to(path.parent).as_posix()
                    return f, inner, f.close
                f.close()
        raise DomainError(f"folder {path.name} holds no Apple Health export.xml "
                          f"(looked in it and in its apple_health_export/)")
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        try:
            names = zf.namelist()
            for member in _xml_candidates(names):
                with zf.open(member) as f:
                    if not _looks_like_export(f.read(_SNIFF)):
                        continue
                stream = zf.open(member)

                def close(stream=stream, zf=zf):
                    stream.close()
                    zf.close()
                return stream, member, close
        except Exception:
            zf.close()
            raise
        zf.close()
        listing = ", ".join(names[:10]) or "(empty)"
        raise DomainError(f"{path.name} contains no Apple Health export.xml "
                          f"(members: {listing})")
    f = open(path, "rb")
    return f, path.name, f.close


# ---------------------------------------------------------------- reading

class _Bucket:
    __slots__ = ("start", "end", "created", "n", "total", "lo", "hi", "parts",
                 "zones")

    def __init__(self, start: int, end: int):
        self.start, self.end = start, end
        self.created = None
        self.n = self.total = 0
        self.lo = self.hi = None
        self.parts: dict[str, int] = {}
        self.zones: set[str] = set()


@dataclass
class _Sample:
    """One per-sample (unbucketed) record, held until ordinals are known."""
    hk_type: str
    q: Quantity
    source: str
    start: int
    end: int
    created: int | None
    value: float               # canonical unit, unrounded
    zone: str | None
    sig: tuple | None
    meal: str | None


class _Reader:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.export_epoch: int | None = None
        self.buckets: dict[tuple, _Bucket] = {}
        self.samples: list[_Sample] = []
        self.sleep: list[tuple] = []
        self.workouts: list[dict] = []
        self.types_seen: set[str] = set()
        self.unmapped: Counter = Counter()
        self.skipped: Counter = Counter()
        self.top_sigs: Counter = Counter()
        self.nested: list[tuple] = []
        self.food: dict[tuple, dict] = {}
        self.macros_seen: Counter = Counter()
        self.macros_folded: Counter = Counter()
        self.parts_seen: set[str] = set()
        self.correlated_only = 0

    # ---- dispatch

    def parse(self, stream, inner: str) -> None:
        ctx = ET.iterparse(stream, events=("start", "end"))
        try:
            try:
                _, root = next(ctx)
            except StopIteration:
                raise DomainError(f"{inner} is empty") from None
            if root.tag != "HealthData":
                raise DomainError(f"{inner} is not an Apple Health export: root "
                                  f"element is <{root.tag}>, expected <HealthData>")
            depth = 0
            for event, el in ctx:
                if event == "start":
                    depth += 1
                    continue
                depth -= 1
                if depth:
                    continue                        # nested, or the root's own end
                tag = el.tag
                if tag == "Record":
                    self.record(el.attrib, el)
                elif tag == "Workout":
                    self.workout(el)
                elif tag == "Correlation":
                    self.correlation(el)
                elif tag == "ExportDate":
                    self.export_epoch = _epoch(el.get("value"))
                elif tag != "Me":
                    self.types_seen.add(tag)
                    self.unmapped[tag] += 1         # ActivitySummary, ClinicalRecord, ...
                root.clear()
        except ET.ParseError as e:
            line, col = e.position
            raise DomainError(f"{inner} line {line} col {col}: "
                              f"{str(e).split(':')[0]}") from None

    def record(self, a, el=None, meta: dict | None = None) -> None:
        t = a.get("type") or "?"
        self.types_seen.add(t)
        if t.startswith(_NESTABLE) and el is not None:
            self.top_sigs[_sig(a)] += 1
        if t in MACROS:
            self.macros_seen[t] += 1          # unmapped unless a Food row folds it
            return
        q = QUANTITIES.get(t)
        if q is None and t != SLEEP:
            self.unmapped[t] += 1
            return
        start, end = _epoch(a.get("startDate")), _epoch(a.get("endDate"))
        if start is None or end is None:
            self.skipped[(t, "unreadable startDate/endDate")] += 1
            return
        if end < start:
            self.skipped[(t, "endDate before startDate")] += 1
            return
        created = _epoch(a.get("creationDate"))
        if meta is None:
            meta = _meta(el) if el is not None and len(el) else {}
        zone = _zone(meta.get("HKTimeZone"))
        source = a.get("sourceName") or ""
        if t == SLEEP:
            raw = a.get("value") or ""
            stage = SLEEP_STAGES.get(raw) or (
                _snake(raw.replace("HKCategoryValueSleepAnalysis", ""))
                if raw else "unknown")          # kept in detail, never counted asleep
            self.sleep.append((source, start, end, stage, created, zone))
            return
        unit = a.get("unit") or ""
        factor = q.units.get(unit)
        if factor is None:
            self.skipped[(t, f"unit '{unit}' not converted")] += 1
            return
        try:
            v = float(a.get("value"))
        except (TypeError, ValueError):
            self.skipped[(t, "value is not a number")] += 1
            return
        if q.metric == "body_fat" and v > 1:
            # A fraction is what HealthKit stores; 18.3 would become 1830 %.
            self.skipped[(t, "value above 1 (expected a fraction)")] += 1
            return
        v *= factor
        if q.bucket:
            self._bucket(q, source, start, end, created, v, zone)
            return
        self.samples.append(_Sample(
            t, q, source, start, end, created, v, zone,
            _sig(a) if t.startswith(_NESTABLE) else None, _meal(meta)))

    def _bucket(self, q, source, start, end, created, v, zone) -> None:
        hour = start - start % 3600
        key = (q.bucket, source, hour)
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = _Bucket(start, end)
        else:
            b.start, b.end = min(b.start, start), max(b.end, end)
        if created is not None and (b.created is None or created > b.created):
            b.created = created
        b.n += 1
        if zone:
            b.zones.add(zone)
        m = _micro(v)
        if q.part:
            b.parts[q.part] = b.parts.get(q.part, 0) + m
            self.parts_seen.add(q.part)
        else:
            b.total += m
        if q.metric == "heart_rate":
            b.lo = v if b.lo is None else min(b.lo, v)
            b.hi = v if b.hi is None else max(b.hi, v)

    def correlation(self, el) -> None:
        ctype = el.get("type") or "?"
        self.types_seen.add(ctype)
        meta = _meta(el)
        members = [(dict(c.attrib), _meta(c)) for c in el if c.tag == "Record"]
        for a, m in members:
            self.nested.append((_sig(a), a, m))
        if ctype != FOOD or not any(a.get("type") == ENERGY_IN for a, _ in members):
            return                      # macros with no energy row stay unmapped
        detail: dict = {}
        grams: dict[str, float] = {}
        for a, _ in members:
            key = MACROS.get(a.get("type"))
            factor = MACRO_G.get(a.get("unit") or "")
            if key and factor is not None:
                try:
                    grams[key] = grams.get(key, 0.0) + float(a.get("value")) * factor
                except (TypeError, ValueError):
                    continue
                self.macros_folded[a.get("type")] += 1
        detail.update({k: round(v, 1) for k, v in sorted(grams.items())})
        name = meta.get("HKFoodType") or next(
            (m.get("HKFoodType") for _, m in members if m.get("HKFoodType")), None)
        if name:
            detail["meal_name"] = name
        meal = _meal(meta)
        if meal:
            detail["meal"] = meal
        for a, _ in members:
            if a.get("type") == ENERGY_IN:
                self.food[_sig(a)] = detail

    def workout(self, el) -> None:
        a = el.attrib
        self.types_seen.add("Workout")
        start, end = _epoch(a.get("startDate")), _epoch(a.get("endDate"))
        if start is None or end is None or end < start:
            self.skipped[("Workout", "unreadable or reversed dates")] += 1
            return
        meta, stats = {}, {}
        for c in el:
            if c.tag == "MetadataEntry":
                meta[c.get("key")] = c.get("value")
            elif c.tag == "WorkoutStatistics":
                stats[c.get("type")] = dict(c.attrib)
        activity = a.get("workoutActivityType") or "HKWorkoutActivityTypeOther"
        try:
            minutes = float(a["duration"]) * DURATION_MIN[a.get("durationUnit", "min")]
        except (KeyError, TypeError, ValueError):
            minutes = (end - start) / 60
        detail = {"exercise_type": _snake(activity.replace("HKWorkoutActivityType", ""))}
        # Distance and energy: pre-iOS 16 exports put totals on the element,
        # newer ones in WorkoutStatistics children. Never emitted as separate
        # rows — the underlying samples are already in the export.
        km = self._convert(a.get("totalDistance"), a.get("totalDistanceUnit"),
                           LENGTH_KM, "Workout distance")
        if km is None:
            for t, s in sorted(stats.items()):
                if t and t.startswith(_QTY + "Distance"):
                    km = self._convert(s.get("sum"), s.get("unit"), LENGTH_KM,
                                       "Workout distance")
                    break
        kcal = self._convert(a.get("totalEnergyBurned"),
                             a.get("totalEnergyBurnedUnit"), ENERGY_KCAL,
                             "Workout energy")
        if kcal is None and _QTY + "ActiveEnergyBurned" in stats:
            s = stats[_QTY + "ActiveEnergyBurned"]
            kcal = self._convert(s.get("sum"), s.get("unit"), ENERGY_KCAL,
                                 "Workout energy")
        hr = stats.get(_QTY + "HeartRate")
        if km:
            detail["distance_km"] = round(km, 3)
        if kcal:
            detail["energy_kcal"] = round(kcal, 1)
        if hr and hr.get("unit") == "count/min" and hr.get("average"):
            try:
                detail["avg_hr"] = round(float(hr["average"]), 1)
            except ValueError:
                pass
        self.workouts.append({
            "source": a.get("sourceName") or "", "activity": activity,
            "start": start, "end": end, "created": _epoch(a.get("creationDate")),
            "minutes": minutes, "detail": detail,
            "zone": _zone(meta.get("HKTimeZone"))})

    def _convert(self, value, unit, table: dict, what: str) -> float | None:
        if value in (None, ""):
            return None
        factor = table.get(unit or "")
        if factor is None:
            self.skipped[(what, f"unit '{unit}' not converted")] += 1
            return None
        try:
            return float(value) * factor
        except ValueError:
            return None

    # ---- building rows

    def finish(self, name: str, inner: str) -> dict:
        for sig, a, meta in self.nested:
            if self.top_sigs[sig] > 0:
                self.top_sigs[sig] -= 1       # the same record, also at top level
                continue
            self.correlated_only += 1
            self.record(a, None, meta)
        export_iso = _iso(self.export_epoch)
        self.source_file = f"{name}#{export_iso}" if export_iso else name
        records = (self._sample_rows() + self._bucket_rows()
                   + self._sleep_rows() + self._workout_rows())
        records.sort(key=lambda r: (r["metric"], r["external_id"]))

        notes = []
        if not export_iso:
            notes.append("no <ExportDate> in the export — the retract key is "
                         "the file name alone")
        if "active" in self.parts_seen and "basal" not in self.parts_seen:
            notes.append("total_calories: active energy but no basal energy in "
                         "this export — totals are active calories only")
        if self.correlated_only:
            notes.append(f"{self.correlated_only} record(s) present only inside "
                         f"a Correlation (imported once)")
        folded = sum(self.macros_folded.values())
        if folded:
            notes.append(f"{folded} protein/fat/carbs/sugar record(s) folded into "
                         f"the nutrition rows of the meals they were logged with")
        for t, n in self.macros_seen.items():
            if n - self.macros_folded[t] > 0:
                self.unmapped[t] += n - self.macros_folded[t]
        unmapped = [{"table": t, "rows": n} for t, n in sorted(self.unmapped.items())]
        skipped = [f"{QUANTITIES[t].metric if t in QUANTITIES else short_type(t)}: "
                   f"{n} {short_type(t)} record(s) skipped, {why}"
                   for (t, why), n in sorted(self.skipped.items())]
        return {"format": FORMAT, "source_file": self.source_file,
                "inner_db": inner, "export_date": export_iso,
                "tables_seen": len(self.types_seen), "records": records,
                "unmapped": unmapped, "skipped": skipped, "notes": notes}

    def _row(self, metric, *, start, end, anchor, zone, value, unit, detail,
             origin, ext, created) -> dict:
        return {
            "metric": metric,
            "journal_date": _journal_date(anchor, zone, self.cfg,
                                          rollover=metric != "sleep_session"),
            "started_at": _iso(start),
            "ended_at": _iso(end),
            "utc_offset_sec": _offset(anchor, zone),
            "value": value,
            "unit": unit if value is not None else None,
            "value_json": health.canonical_json(detail),
            "origin_app": origin or None,
            "external_id": ext,
            "source_file": self.source_file,
            "last_modified_at": _iso(created),
        }

    def _sample_rows(self) -> list[dict]:
        groups: dict[tuple, list[_Sample]] = defaultdict(list)
        for s in self.samples:
            groups[(s.hk_type, s.source, s.start, s.end)].append(s)
        out = []
        for (t, source, start, end), items in groups.items():
            # Same-instant samples (two foods logged at 12:00) are told apart by
            # an ordinal. Creation order first: a sample added later lands last
            # and leaves the earlier ones' keys alone.
            items.sort(key=lambda s: (s.created or 0, s.value,
                                      tuple(x or "" for x in s.sig or ())))
            for ordinal, s in enumerate(items):
                q = s.q
                detail = None
                if q.metric == "nutrition":
                    detail = dict(self.food.get(s.sig, {}))
                    if s.meal and "meal" not in detail:
                        detail["meal"] = s.meal
                out.append(self._row(
                    q.metric, start=start, end=end if end != start else None,
                    anchor=end, zone=s.zone, value=round(s.value, q.digits),
                    unit=q.unit, detail=detail, origin=source,
                    ext=_ext_id(t, source, _iso(start), _iso(end), str(ordinal)),
                    created=s.created))
        return out

    def _bucket_rows(self) -> list[dict]:
        out = []
        q_for = {q.bucket: q for q in QUANTITIES.values() if q.bucket}
        for (family, source, hour), b in self.buckets.items():
            q = q_for[family]
            if q.metric == "heart_rate":
                value = round(b.total / b.n / 1_000_000, 1)
                detail = {"min": _num(b.lo), "max": _num(b.hi), "samples": b.n}
            elif q.part:
                value = round(sum(b.parts.values()) / 1_000_000, q.digits)
                detail = {f"{p}_kcal": round(m / 1_000_000, 1)
                          for p, m in sorted(b.parts.items())}
                detail["samples"] = b.n
            else:
                value = round(b.total / 1_000_000, q.digits)
                detail = {"samples": b.n}
                if q.activity:
                    detail["activity"] = q.activity
            zone = min(b.zones) if b.zones else None
            out.append(self._row(
                q.metric, start=b.start, end=b.end, anchor=hour, zone=zone,
                value=value, unit=q.unit, detail=detail, origin=source,
                ext=_ext_id(family, source, _iso(hour)), created=b.created))
        return out

    def _sleep_rows(self) -> list[dict]:
        by_source: dict[str, list] = defaultdict(list)
        for s in self.sleep:
            by_source[s[0]].append(s)
        out = []
        for source, items in sorted(by_source.items()):
            items.sort(key=lambda s: (s[1], s[2], s[3]))
            sessions: list[list] = []
            cur_end = None
            for s in items:
                if cur_end is None or s[1] > cur_end + SESSION_GAP_SEC:
                    sessions.append([])
                    cur_end = s[2]
                sessions[-1].append(s)
                cur_end = max(cur_end, s[2])
            for sess in sessions:
                start = min(s[1] for s in sess)
                end = max(s[2] for s in sess)
                per: dict[str, list] = defaultdict(list)
                for s in sess:
                    per[s[3]].append((s[1], s[2]))
                asleep = _union_sec([iv for stage, ivs in per.items()
                                     if stage in ASLEEP for iv in ivs])
                detail: dict = {}
                stages = {k: round(_union_sec(v) / 60, 1)
                          for k, v in sorted(per.items()) if k != "in_bed"}
                if stages:
                    detail["stages"] = stages
                if per.get("in_bed"):
                    detail["in_bed"] = round(_union_sec(per["in_bed"]) / 60, 1)
                created = max((s[4] for s in sess if s[4] is not None), default=None)
                zones = sorted({s[5] for s in sess if s[5]})
                out.append(self._row(
                    "sleep_session", start=start, end=end, anchor=end,
                    zone=zones[0] if zones else None,
                    value=round(asleep / 60, 1) if asleep else None, unit="min",
                    detail=detail, origin=source,
                    ext=_ext_id("sleep", source, _iso(start)), created=created))
        return out

    def _workout_rows(self) -> list[dict]:
        return [self._row(
            "exercise_session", start=w["start"], end=w["end"], anchor=w["end"],
            zone=w["zone"], value=round(w["minutes"], 1), unit="min",
            detail=w["detail"], origin=w["source"],
            ext=_ext_id("Workout", w["source"], w["activity"], _iso(w["start"]),
                        _iso(w["end"])),
            created=w["created"]) for w in self.workouts]


def _sig(a) -> tuple:
    return (a.get("type"), a.get("sourceName"), a.get("startDate"),
            a.get("endDate"), a.get("creationDate"), a.get("value"), a.get("unit"))


def _meal(meta: dict) -> str | None:
    for k, v in meta.items():
        if k and k.lower() in ("meal", "hkmeal", "hkfoodmeal") and v:
            return str(v).strip().lower()
    return None


def read_export(path: Path, cfg: Config) -> dict:
    """Scan an Apple Health export into health record dicts (the same shape
    health_connect.read_export returns, plus 'format', 'export_date' and
    'notes'). Pure read: no database is opened."""
    stream, inner, close = open_export(path)
    try:
        reader = _Reader(cfg)
        reader.parse(_without_dtd(stream), inner)
        return reader.finish(path.name, inner)
    finally:
        close()


# ---------------------------------------------------------------- plan / apply

def build_plan(conn, records: list[dict]) -> dict:
    """Health Connect's plan (dedupe, newer-wins, conflicts — one rule set for
    every health import), plus what an Apple export needs said about it:
    which origins wrote each metric, the days a summed metric has two of them,
    and stored Apple rows this full-snapshot export no longer contains."""
    plan = hc.build_plan(conn, records)
    # Only rows with a value: the iPhone's in-bed-only nights are summed by
    # nothing, so they are not a second recorder of sleep.
    origins: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        if r["value"] is not None:
            origins[r["metric"]][r["origin_app"] or "?"] += 1
    plan["origins"] = {m: dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))
                       for m, c in sorted(origins.items())}
    plan["origin_overlap"] = _origin_overlap(conn, records)
    incoming = {(r["metric"], r["external_id"]) for r in records}
    absent: Counter = Counter()
    for row in conn.execute("SELECT metric, external_id FROM health_records "
                            "WHERE status='active' AND external_id LIKE ?",
                            (ID_PREFIX + "%",)):
        if (row["metric"], row["external_id"]) not in incoming:
            absent[row["metric"]] += 1
    plan["absent"] = dict(sorted(absent.items()))
    return plan


def _origin_overlap(conn, records: list[dict]) -> dict:
    """{metric: {origins, days}} for summed metrics recorded by more than one
    origin on the same journal day, across this export AND the active rows
    already stored for those days (a Samsung history next to a new iPhone
    counts too). Values-less rows (an iPhone's in-bed-only night) are not
    summed by anything, so they do not count."""
    per: dict[tuple[str, str], set] = defaultdict(set)
    for r in records:
        if r["metric"] in SUM_METRICS and r["value"] is not None:
            per[(r["metric"], r["journal_date"])].add(r["origin_app"] or "?")
    if per:
        metrics = sorted({m for m, _ in per})
        for row in conn.execute(
                "SELECT metric, journal_date, COALESCE(origin_app, '?') origin "
                "FROM health_records WHERE status='active' AND value IS NOT NULL "
                f"AND metric IN ({', '.join('?' * len(metrics))}) "
                "GROUP BY metric, journal_date, origin", metrics):
            key = (row["metric"], row["journal_date"])
            if key in per:
                per[key].add(row["origin"])
    out: dict[str, dict] = {}
    for (metric, _), names in per.items():
        if len(names) > 1:
            o = out.setdefault(metric, {"origins": set(), "days": 0})
            o["origins"] |= names
            o["days"] += 1
    return {m: {"origins": sorted(o["origins"]), "days": o["days"]}
            for m, o in sorted(out.items())}


def apply_plan(conn, plan: dict) -> dict:
    """The shared health apply path, unchanged: inserts and updates are
    audit-logged, refreshes are not, conflicts are never applied."""
    return hc.apply_plan(conn, plan)


def plan_summary(scan: dict, plan: dict) -> dict:
    per: dict[str, dict] = {}
    for rec in plan["new"]:
        per.setdefault(rec["metric"], {"new": 0, "changed": 0})["new"] += 1
    for _, rec, _ in plan["updates"]:
        per.setdefault(rec["metric"], {"new": 0, "changed": 0})["changed"] += 1
    return {"format": FORMAT, "source_file": scan["source_file"],
            "inner_db": scan["inner_db"], "export_date": scan.get("export_date"),
            "tables_seen": scan["tables_seen"], "per_metric": per,
            "new": len(plan["new"]), "updated": len(plan["updates"]),
            "refreshed": len(plan["refreshed"]), "identical": plan["identical"],
            "duplicates_in_export": plan["dup_incoming"],
            "conflicts": plan["conflicts"], "unmapped": scan["unmapped"],
            "skipped": scan["skipped"], "notes": scan.get("notes", []),
            "origins": plan.get("origins", {}),
            "origin_overlap": plan.get("origin_overlap", {}),
            "absent_from_export": plan.get("absent", {})}


# ---------------------------------------------------------------- report

_DAILY_TOTAL = {"steps": "steps", "distance": "km", "total_calories": "kcal"}


def _sample_line(recs: list[dict]) -> str:
    """The latest day's total for hourly-summed metrics (one bucket says
    nothing), else the latest record that has a value."""
    metric = recs[0]["metric"]
    if metric in _DAILY_TOTAL:
        day = max(r["journal_date"] for r in recs)
        on_day = [r for r in recs if r["journal_date"] == day]
        total = round(sum(r["value"] for r in on_day if r["value"] is not None), 3)
        return f"{day} total {total:g} {_DAILY_TOTAL[metric]} ({len(on_day)} record(s))"
    valued = [r for r in recs if r["value"] is not None] or recs
    r = max(valued, key=lambda x: (x["journal_date"], x["started_at"]))
    val = f"{r['value']:g} {r['unit']}" if r["value"] is not None else "-"
    detail = ""
    if r["value_json"]:
        d = json.loads(r["value_json"])
        if "stages" in d:
            detail = " (" + ", ".join(f"{k} {v:g}" for k, v in
                                      sorted(d["stages"].items())) + ")"
        elif "in_bed" in d:
            detail = f" (in bed {d['in_bed']:g} min)"
        elif "exercise_type" in d:
            detail = f" ({d['exercise_type']})"
        elif metric == "nutrition":
            parts = [f"{k} {v}" for k, v in sorted(d.items()) if k.endswith("_g")]
            detail = f" ({', '.join(parts)})" if parts else ""
    origin = f" from {r['origin_app']}" if r["origin_app"] else ""
    return f"{r['journal_date']} {val}{detail}{origin}"


def report_lines(scan: dict, plan: dict) -> list[str]:
    lines = [f"apple-health export: {scan['source_file']} "
             f"(xml: {scan['inner_db']}, {scan['tables_seen']} record types)"]
    by_metric: dict[str, dict] = {}
    for rec in plan["new"]:
        by_metric.setdefault(rec["metric"], {"new": [], "upd": 0})["new"].append(rec)
    for _, rec, _ in plan["updates"]:
        by_metric.setdefault(rec["metric"], {"new": [], "upd": 0})["upd"] += 1
    for metric in sorted(by_metric):
        info = by_metric[metric]
        recs = info["new"] or [rec for _, rec, _ in plan["updates"]
                               if rec["metric"] == metric]
        span = (f"{min(r['journal_date'] for r in recs)}"
                f"..{max(r['journal_date'] for r in recs)}")
        lines.append(f"  {metric}: {len(info['new'])} new, {info['upd']} "
                     f"changed | {span} | e.g. {_sample_line(recs)}")
    if not by_metric:
        lines.append("  nothing new — export matches the database")
    overlap = plan.get("origin_overlap", {})
    for metric, o in overlap.items():
        counts = plan.get("origins", {}).get(metric, {})
        named = ", ".join(f"{name} ({counts[name]})" if name in counts
                          else f"{name} (stored)" for name in o["origins"])
        lines.append(f"  {metric} origins: {named} — {o['days']} day(s) "
                     f"recorded by more than one: daily totals would double-count")
    # More than one recorder without a shared day still deserves a line: the
    # next export may overlap, and an average mixes devices silently.
    for metric, counts in plan.get("origins", {}).items():
        if len(counts) < 2 or metric in overlap:
            continue
        named = ", ".join(f"{name} ({n})" for name, n in counts.items())
        why = ("never on the same day" if metric in SUM_METRICS
               else "averaged together where they overlap")
        lines.append(f"  {metric} origins: {named} — {why}")
    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate row(s) inside the export")
    if tail:
        lines.append("  " + ", ".join(tail))
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['metric']} {c['external_id'][:12]}…: "
                     f"differs from stored record but is not newer — kept "
                     f"existing ({', '.join(sorted(c['changes']))})")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    if scan["unmapped"]:
        lines.append("  unmapped types (present, not imported): " + ", ".join(
            f"{short_type(u['table'])} ({u['rows']})" for u in scan["unmapped"]))
    for note in scan.get("notes", []):
        lines.append(f"  note: {note}")
    absent = plan.get("absent", {})
    if absent:
        lines.append(
            f"  {sum(absent.values())} stored Apple Health row(s) are not in this "
            f"export (deleted on the phone, or re-keyed): "
            + ", ".join(f"{m} x{n}" for m, n in absent.items())
            + " — left as they are")
    return lines

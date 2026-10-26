"""GPS track connector: places you stayed, found in the phone's own GPS log.

Google Maps Timeline lives on the phone now and leaves it only by a manual
export (connectors/location.py). The automatic route is a logging app on the
phone — GPSLogger for Android, open source — writing one track file a day
into a folder that syncs to the laptop's OneDrive (docs/gps_track.md has the
setup). This connector reads that folder and turns its raw points into the
same `place_visit` records Timeline gives, so trips, home, countries, on
this day and every other reader of place_visit work unchanged.

Files read (GPSLogger's "Log to ..." formats; any mix, any number):

  .gpx      GPX 1.0 (GPSLogger's default) or 1.1: <trkpt lat lon> with <time>
            (UTC 'Z', or local with its offset when "Log time with timezone
            offset" is on), <hdop> and <src> when the phone supplies them.
            GPX carries no accuracy in metres; GPSLogger's own Accuracy
            Filter (40 m by default) has already dropped the poor fixes
  .csv      time, lat, lon, accuracy, provider, hdop, timestamp_ms,
            time_offset, ...: the delimiter (and a decimal comma) as the app
            was set; time_offset says the zone the phone was in
  .geojson  a FeatureCollection of Points: [lon, lat], properties time,
            time_long, accuracy, provider
  .zip      any of those inside (GPSLogger zips what it uploads itself)

KML and NMEA are named in the report, not read. Several formats logged side
by side hold the same fixes: a point at the same second is one point.

From points to visits
---------------------
Points are taken in time order across every file, so a night at home that
starts in yesterday's file and ends in today's is one visit. A point less
accurate than MAX_ACCURACY_M (or, with no accuracy, an HDOP above MAX_HDOP)
is dropped. A visit is a run of consecutive points that all lie within
RADIUS_M of the run's centre and spans at least MIN_STAY_S:

  * a gap is part of a visit only when the points either side of it agree —
    the point after the gap is within the radius — and the gap is at most
    MAX_BRIDGE_S (a night with the phone still and silent). Otherwise the
    visit ends at the last point before the gap: what happened inside it is
    unknown, never assumed
  * a visit survives an absence of up to MAX_AWAY_S: points that wander off
    (a GPS jump, stepping out) and come back that soon are an excursion,
    dropped, and the visit goes on. MAX_AWAY_S is no longer than MIN_STAY_S,
    so an excursion can never itself be a visit
  * everything is decided as the points arrive, never by looking back, so
    re-reading a file that has grown since finds the same visits with the
    same starts, and only the last one can still change

A record
--------
  title           'Place at <lat>, <lon>' — the words location.py uses for a
                  visit with no name. GPS knows no place names, and none is
                  made up; `lifecrm places` names the town from a gazetteer
  started/ended   the first and last fix of the visit, UTC; utc_offset_sec is
                  the offset the file wrote with the first fix, when it wrote
                  one (CSV always does; GPX and GeoJSON with the option on)
  journal_date    the local date in that zone, rollover applied there; with
                  no stated offset, the home timezone (base.journal_date_for_iso)
  value / unit    minutes at the place
  value_json      {lat, lon, place_id}: the centre of the visit's fixes, and
                  its grid cell — a subset of location.py's keys, same meaning
  participants    the cell as a `place_id` handle 'gps:<geohash>', role
                  'location'. GPS has no place id; a geohash cell (~150 m) is
                  the stable stand-in: the same place gives the same handle
                  every day, so linking it to a place entity once attributes
                  every visit there, past and future
  external_id     'gps@<start>'. The start of a visit never moves as a file
                  grows, so today's still-growing file updates its visits
                  instead of adding new ones
  origin_app      'gpslogger' when the file says it was written by GPSLogger

The visit still open at the end of the data — you are still there — is
imported with its end so far. Every record's last_modified_at is the newest
fix the scan read, as location.py does with an export's as-of, so the next
morning's longer reading wins: the open visit gets its real end (an audited
update); a reading older than what was imported overwrites nothing.

Mirroring: none, and no coverage. A track file deleted from the folder is
not a visit that never happened, and a day with no visits is a day the
phone did not log, not a day you went nowhere — a missing row is unknown.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "gps_track"
streams = ("place_visit",)
# The phone logs every day; a newest visit this old means the logger or the
# sync stopped (Samsung's battery saver is the usual cause). Longer than any
# ordinary stay, since a visit is dated by its start.
stale_after_days = 7

RADIUS_M = 100
MIN_STAY_S = 10 * 60
MAX_BRIDGE_S = 12 * 3600
MAX_AWAY_S = 10 * 60
MAX_ACCURACY_M = 75
MAX_HDOP = 5.0
CELL_PRECISION = 7               # geohash characters: a cell ~153 m tall

# The OneDrive folder the phone's sync app uploads to (docs/gps_track.md);
# the second is the name GPSLogger gives its own Google Drive folder.
FOLDER_NAMES = ("GPSLogger", "GPSLogger for Android")
TRACK_SUFFIXES = (".gpx", ".csv", ".geojson")
_NOT_READ = {".kml": "KML — log GPX, CSV or GeoJSON instead",
             ".kmz": "KML — log GPX, CSV or GeoJSON instead",
             ".nmea": "raw NMEA sentences, not positions",
             ".txt": "not a track file"}
_ISO = "%Y-%m-%dT%H:%M:%SZ"
_EARTH_M = 6371008.8
_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

# A point: (epoch seconds, lat, lon, accuracy m, hdop, utc offset s, origin).
_T, _LAT, _LON, _ACC, _HDOP, _OFF, _ORIGIN = range(7)


# ---------------------------------------------------------------- geometry

def _wrap(dlon: float) -> float:
    return (dlon + 180.0) % 360.0 - 180.0


def metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres, equirectangular — exact enough at the scale of a
    stay, and cheap enough for every point of a year."""
    dy = math.radians(lat2 - lat1)
    dx = math.radians(_wrap(lon2 - lon1)) * math.cos(math.radians((lat1 + lat2) / 2))
    return _EARTH_M * math.hypot(dx, dy)


def geohash(lat: float, lon: float, precision: int = CELL_PRECISION) -> str:
    """The standard geohash: a cell name anyone can paste into a geohash map."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out, ch, bit, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch, lon_lo = ch * 2 + 1, mid
            else:
                ch, lon_hi = ch * 2, mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch, lat_lo = ch * 2 + 1, mid
            else:
                ch, lat_hi = ch * 2, mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(_BASE32[ch])
            ch = bit = 0
    return "".join(out)


# ---------------------------------------------------------------- values

def _float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    try:
        f = float(str(value).strip())
    except ValueError:
        return None
    return f if math.isfinite(f) else None


def _when(value) -> tuple[int | None, int | None]:
    """(epoch seconds, the offset in seconds the file stated) from an ISO
    time. 'Z' — GPSLogger's default — states no zone: GPX is UTC by
    definition, so a time with no zone at all is UTC too."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    utc = text[-1:] in ("Z", "z")
    try:
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if utc else text)
    except ValueError:
        return None, None
    if dt.tzinfo is None:
        return math.floor(dt.replace(tzinfo=timezone.utc).timestamp()), None
    offset = None if utc else int(dt.utcoffset().total_seconds())
    return math.floor(dt.timestamp()), offset


def _epoch_ms(value) -> int | None:
    f = _float(value)
    if f is None or f <= 0:
        return None
    return int(f // 1000) if f > 1e11 else int(f)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(_ISO)


def _journal_date(start: int, offset: int | None, cfg: Config) -> str:
    """The local date where the file stated the zone (rollover applied
    there, as location.py dates a visit); the home-timezone rule otherwise."""
    if offset is None:
        return base.journal_date_for_iso(_iso(start), cfg)
    local = datetime.fromtimestamp(start + offset, tz=timezone.utc)
    if local.hour < cfg.day_rollover_hour:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- reading

class _Acc:
    """What one scan has counted so far, across every file it reads."""

    def __init__(self):
        self.skipped: Counter = Counter()
        self.unmapped: Counter = Counter()
        self.formats: Counter = Counter()       # per format, the track files read
        self.points = 0
        self.as_of: int | None = None

    def point(self, t, lat, lon, acc=None, hdop=None, off=None, origin=None):
        """A checked point, or None (counted)."""
        if t is None:
            self.skipped["point with no usable time"] += 1
            return None
        self.points += 1
        if self.as_of is None or t > self.as_of:
            self.as_of = t
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180) \
                or (lat, lon) == (0.0, 0.0):
            self.skipped["point with no position on Earth"] += 1
            return None
        return (t, lat, lon, acc, hdop, off, origin)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_gpx(raw: bytes, rel: str, acc: _Acc) -> list[tuple]:
    out = []
    # <gpx creator="GPSLogger 137 - ..."> opens the file
    origin = "gpslogger" if b'creator="gpslogger' in raw[:4096].lower() else None
    try:
        for _event, el in ET.iterparse(io.BytesIO(raw)):
            if el.tag != "trkpt" and not el.tag.endswith("}trkpt"):
                continue
            fields = {_local(ch.tag): (ch.text or "").strip() for ch in el}
            t, off = _when(fields.get("time"))
            p = acc.point(t, _float(el.get("lat")), _float(el.get("lon")),
                          hdop=_float(fields.get("hdop")), off=off, origin=origin)
            if p:
                out.append(p)
            el.clear()
    except ET.ParseError:
        if not out:
            acc.unmapped[f"{rel} (not a readable GPX file)"] += 1
            return []
        acc.skipped["track file that ends early (still being written or synced?)"] += 1
    acc.formats["GPX"] += 1
    return out


_LAT_COLS = ("lat", "latitude")
_LON_COLS = ("lon", "lng", "long", "longitude")


def _read_csv(raw: bytes, rel: str, acc: _Acc) -> list[tuple]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        acc.unmapped[f"{rel} (not UTF-8 text)"] += 1
        return []
    first = text.split("\n", 1)[0]
    delim = None
    for d in (",", ";", "\t", "|"):
        cols = [c.strip().strip('"').lower() for c in first.split(d)]
        if any(c in _LAT_COLS for c in cols) and any(c in _LON_COLS for c in cols):
            delim = d
            break
    if delim is None:
        acc.unmapped[f"{rel} (no lat/lon columns — not a track)"] += 1
        return []
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header = [c.strip().lower() for c in next(reader)]
    col = {c: i for i, c in enumerate(header)}

    def idx(*names):
        return next((col[n] for n in names if n in col), None)
    i_lat, i_lon = idx(*_LAT_COLS), idx(*_LON_COLS)
    i_ms, i_off, i_time = idx("timestamp_ms", "time_long"), idx("time_offset"), idx("time")
    i_acc, i_hdop = idx("accuracy", "accuracy(m)"), idx("hdop")
    origin = "gpslogger" if {"timestamp_ms", "time_offset", "provider"} <= set(col) else None
    comma = delim != ","          # a decimal comma is only possible then

    def cell(row, i):
        if i is None or i >= len(row) or not row[i].strip():
            return None
        return row[i].strip()

    def num(row, i):
        v = cell(row, i)
        return _float(v.replace(",", ".") if comma and v else v)

    out = []
    for row in reader:
        if not row or not any(c.strip() for c in row):
            continue
        t, off = None, None
        stated = cell(row, i_off)
        if stated:
            t, off = _when(stated)
        ms = _epoch_ms(cell(row, i_ms))
        if ms is not None:
            t = ms
        if t is None:
            t, off2 = _when(cell(row, i_time))
            off = off if off is not None else off2
        p = acc.point(t, num(row, i_lat), num(row, i_lon), acc=num(row, i_acc),
                      hdop=num(row, i_hdop), off=off, origin=origin)
        if p:
            out.append(p)
    acc.formats["CSV"] += 1
    return out


def _read_geojson(raw: bytes, rel: str, acc: _Acc) -> list[tuple]:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        acc.skipped["GeoJSON file that is not complete JSON (still being written "
                    "or synced?)"] += 1
        return []
    feats = data.get("features") if isinstance(data, dict) else None
    if not isinstance(feats, list):
        acc.unmapped[f"{rel} (not a GeoJSON FeatureCollection)"] += 1
        return []
    out = []
    for f in feats:
        geom = f.get("geometry") if isinstance(f, dict) else None
        props = f.get("properties") if isinstance(f, dict) else None
        props = props if isinstance(props, dict) else {}
        if not isinstance(geom, dict) or geom.get("type") != "Point" \
                or not isinstance(geom.get("coordinates"), list) \
                or len(geom["coordinates"]) < 2:
            acc.skipped["GeoJSON feature that is not a point"] += 1
            continue
        lon, lat = (_float(v) for v in geom["coordinates"][:2])
        t, off = _when(props.get("time"))
        ms = _epoch_ms(props.get("time_long"))
        if ms is not None:
            t = ms
        origin = "gpslogger" if "time_long" in props else None
        p = acc.point(t, lat, lon, acc=_float(props.get("accuracy")),
                      hdop=_float(props.get("hdop")), off=off, origin=origin)
        if p:
            out.append(p)
    acc.formats["GeoJSON"] += 1
    return out


_READERS = {".gpx": _read_gpx, ".csv": _read_csv, ".geojson": _read_geojson}


def _zip_read(path: Path, member: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(member)


def _stem(rel: str) -> str:
    base_name = rel.rsplit("/", 1)[-1]
    return base_name.rsplit(".", 1)[0].lower()


def _groups(target: Path, acc: _Acc) -> list[tuple[str, list]]:
    """[(day key, [(name, suffix, loader)])], oldest first. GPSLogger names
    each file for its day (yyyyMMdd), so files sharing a name are the same
    fixes in different formats and are read together."""
    entries: list[tuple[str, str, object]] = []

    def take(rel: str, load) -> None:
        suffix = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if suffix in _READERS:
            entries.append((rel, suffix, load))
        elif suffix in _NOT_READ:
            acc.unmapped[f"{rel.rsplit('/', 1)[-1]} ({_NOT_READ[suffix]})"] += 1
        else:
            acc.unmapped[f"{rel.rsplit('/', 1)[-1]} (not a track file)"] += 1

    def zip_members(path: Path, rel: str) -> None:
        try:
            with zipfile.ZipFile(path) as zf:
                members = [m for m in zf.namelist() if not m.endswith("/")]
        except zipfile.BadZipFile:
            acc.unmapped[f"{rel} (not a readable zip)"] += 1
            return
        for m in sorted(members):
            take(f"{rel}/{m}", lambda path=path, m=m: _zip_read(path, m))

    if target.is_file():
        if zipfile.is_zipfile(target):
            zip_members(target, target.name)
        elif target.suffix.lower() in _READERS:
            take(target.name, target.read_bytes)
        else:
            raise DomainError(
                f"{target.name} is not a .gpx, .csv, .geojson or .zip file — pass a "
                f"GPSLogger track file or the folder the phone's tracks sync to")
    else:
        os.listdir(target)        # a folder macOS keeps us out of says so, not "empty"
        files = sorted((p for p in target.rglob("*") if p.is_file()
                        and not any(part.startswith(".") for part in
                                    p.relative_to(target).parts)),
                       key=lambda p: p.relative_to(target).as_posix().lower())
        for p in files:
            rel = p.relative_to(target).as_posix()
            if p.suffix.lower() == ".zip":
                zip_members(p, rel)
            else:
                take(rel, p.read_bytes)
        if not entries:
            listing = ", ".join(p.name for p in files[:10]) or "(empty)"
            raise DomainError(f"{target.name} holds no .gpx, .csv or .geojson track "
                              f"files (has: {listing})")
    by_key: dict[str, list] = {}
    for rel, suffix, load in entries:
        by_key.setdefault(_stem(rel), []).append((rel, suffix, load))
    return sorted(by_key.items())


def _merged(points: list[tuple]) -> list[tuple]:
    """Sorted by time, one point per second: a fix logged in two formats is
    one fix, keeping the accuracy, HDOP and offset whichever file had them."""
    points.sort(key=lambda p: p[_T])
    out: list[tuple] = []
    for p in points:
        if out and out[-1][_T] == p[_T]:
            q = out[-1]
            out[-1] = (q[_T], q[_LAT], q[_LON],
                       q[_ACC] if q[_ACC] is not None else p[_ACC],
                       q[_HDOP] if q[_HDOP] is not None else p[_HDOP],
                       q[_OFF] if q[_OFF] is not None else p[_OFF],
                       q[_ORIGIN] or p[_ORIGIN])
        else:
            out.append(p)
    return out


def _points(groups: list, acc: _Acc):
    """Every usable point in time order. One day's files are held at a time,
    plus the day before when the two overlap, so a year of tracks is never
    all in memory."""
    held: list[tuple] = []
    for _key, files in groups:
        day: list[tuple] = []
        for rel, suffix, load in files:
            try:
                raw = load()
            except (OSError, zipfile.BadZipFile) as e:
                # Windows: held open by the sync app, or online-only in
                # OneDrive with no connection. Tomorrow's run reads it.
                acc.unmapped[f"{rel} (could not be read now: "
                             f"{getattr(e, 'strerror', None) or e})"] += 1
                continue
            day += _READERS[suffix](raw, rel, acc)
        day = _merged(day)
        if held and day and day[0][_T] <= held[-1][_T]:
            held = _merged(held + day)
            continue
        yield from _usable(held, acc)
        held = day
    yield from _usable(held, acc)


def _usable(points: list[tuple], acc: _Acc):
    for p in points:
        if p[_ACC] is not None:
            if p[_ACC] > MAX_ACCURACY_M:
                acc.skipped[f"point less accurate than {MAX_ACCURACY_M} m"] += 1
                continue
        elif p[_HDOP] is not None and p[_HDOP] > MAX_HDOP:
            acc.skipped[f"point with HDOP above {MAX_HDOP:g} (a poor fix)"] += 1
            continue
        yield p


# ---------------------------------------------------------------- stays

class _Run:
    """Consecutive points around one centre. Positions are summed as offsets
    from the first point, so a run across the 180th meridian averages right."""

    __slots__ = ("lat0", "lon0", "n", "sy", "sx", "start", "end", "off", "origin")

    def __init__(self, p: tuple):
        self.lat0, self.lon0 = p[_LAT], p[_LON]
        self.n, self.sy, self.sx = 1, 0.0, 0.0
        self.start = self.end = p[_T]
        self.off, self.origin = p[_OFF], p[_ORIGIN]

    def centre(self) -> tuple[float, float]:
        return self.lat0 + self.sy / self.n, _wrap(self.lon0 + self.sx / self.n)

    def near(self, p: tuple) -> bool:
        lat, lon = self.centre()
        return metres(lat, lon, p[_LAT], p[_LON]) <= RADIUS_M

    def add(self, p: tuple) -> None:
        self.n += 1
        self.sy += p[_LAT] - self.lat0
        self.sx += _wrap(p[_LON] - self.lon0)
        self.end = p[_T]

    def is_stay(self) -> bool:
        return self.end - self.start >= MIN_STAY_S


class _Detector:
    """The rules in the module docstring, one point at a time."""

    def __init__(self):
        self.cur: _Run | None = None
        self.away: list[tuple] = []
        self.stays: list[_Run] = []
        self.last_t: int | None = None
        self.out_of_order = 0

    def feed(self, p: tuple) -> None:
        if self.last_t is not None and p[_T] <= self.last_t:
            self.out_of_order += 1
            return
        self.last_t = p[_T]
        queue, i = [p], 0
        while i < len(queue):
            q = queue[i]
            i += 1
            cur = self.cur
            if cur is None:
                self.cur = _Run(q)
                continue
            gap = q[_T] - cur.end
            if gap <= (MAX_AWAY_S if self.away else MAX_BRIDGE_S) and cur.near(q):
                cur.add(q)
                self.away = []
                continue
            if gap <= MAX_AWAY_S and cur.is_stay():      # away, perhaps not for long
                self.away.append(q)
                continue
            if cur.is_stay():
                self.stays.append(cur)
            # what left the run starts over, in order, as fresh points
            queue = self.away + [q] + queue[i:]
            i, self.away, self.cur = 0, [], None

    def finish(self) -> tuple[list[_Run], _Run | None]:
        """(every visit, the one still open at the end of the data or None)."""
        if self.cur is not None and self.cur.is_stay():
            return self.stays + [self.cur], self.cur
        return list(self.stays), None


# ---------------------------------------------------------------- targets

def _holds_tracks(folder: Path) -> bool:
    """A track file anywhere in it — what scan would read."""
    try:
        return any(p.suffix.lower() in TRACK_SUFFIXES + (".zip",) and p.is_file()
                   for p in folder.rglob("*"))
    except OSError:
        return False


def _subdirs(folder: Path, names: set[str] | None = None) -> list[Path]:
    """Visible subfolders, only those called one of `names` when given (the
    name is checked first: a camera folder's thousands of files are never
    stat'ed)."""
    try:
        return sorted(p for p in folder.iterdir() if not p.name.startswith(".")
                      and (names is None or p.name.lower() in names) and p.is_dir())
    except OSError:
        return []


def search_folders(cfg: Config | None = None) -> list[Path]:
    """Every folder named like the phone's track folder at the top of a
    OneDrive, or one folder down (OneDrive › Apps › GPSLogger) — and, given
    the config, the folder itself on a phone shown in File Explorer
    (lifecrm/phone.py), where no sync app is needed."""
    wanted = {n.lower() for n in FOLDER_NAMES}
    out: list[Path] = []
    for root in platforms.onedrive_candidates():
        top = _subdirs(root)
        out += [p for p in top if p.name.lower() in wanted]
        for sub in top:
            if sub.name.lower() not in wanted:
                out += _subdirs(sub, wanted)
    if cfg is not None:
        from .. import phone
        out += phone.folders(cfg, *FOLDER_NAMES)
    return list({str(p).lower(): p for p in out}.values())


def default_target(cfg: Config) -> Path:
    """[connectors.gps_track] path, else the one track folder the phone's
    sync app fills in OneDrive. Two are refused: two phones, or an old copy,
    and guessing would mix them."""
    path = cfg.connector_settings(name).get("path")
    if path not in (None, ""):
        if not isinstance(path, str):
            raise DomainError(f"[connectors.gps_track] path = {path!r} must be a folder "
                              f"path in quotes")
        p = cfg.resolve(path)
        if not p.exists():
            raise DomainError(f"[connectors.gps_track] path does not exist: {p} — fix it, "
                              f"or pass the folder the phone's tracks sync to")
        return p
    folders = search_folders(cfg)
    found = [f for f in folders if _holds_tracks(f)]
    if len(found) == 1:
        return found[0]
    if found:
        raise base.Ambiguous("found several GPS track folders — pass the one to import, "
                             "or set [connectors.gps_track] path: "
                             + "; ".join(str(f) for f in found))
    if folders:
        raise DomainError(f"{folders[0]} holds no track files yet (.gpx, .csv, .geojson) — "
                          f"check that GPSLogger is logging and the sync app is uploading "
                          f"(docs/gps_track.md)")
    roots = ", ".join(str(r) for r in platforms.onedrive_candidates())
    raise DomainError(
        f"no GPS track folder found — looked for a folder named "
        f"{' or '.join(FOLDER_NAMES)} in {roots} and one folder down; set up the "
        f"phone (docs/gps_track.md), pass the folder, or set [connectors.gps_track] path")


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    acc = _Acc()
    det = _Detector()
    try:
        for p in _points(_groups(target, acc), acc):
            det.feed(p)
    except PermissionError as e:
        raise DomainError(
            f"cannot read {e.filename or target}: permission denied — on a Mac, allow "
            f"the app running this to use that folder (System Settings > Privacy & "
            f"Security > Files and Folders), then run it again") from e
    if det.out_of_order:
        acc.skipped["point earlier than the file before it (files out of order)"] += \
            det.out_of_order
    stays, open_stay = det.finish()
    as_of = _iso(acc.as_of) if acc.as_of is not None else None
    records, cells = [], {}
    for run in stays:
        lat, lon = run.centre()
        lat, lon = round(lat, 6), round(lon, 6)
        cell = f"gps:{geohash(lat, lon)}"
        cells[cell] = None
        start = _iso(run.start)
        records.append({
            "stream": "place_visit",
            "external_id": f"gps@{start}",
            "journal_date": _journal_date(run.start, run.off, cfg),
            "started_at": start,
            "ended_at": _iso(run.end),
            "utc_offset_sec": run.off,
            "direction": None,
            "title": f"Place at {lat:.4f}, {lon:.4f}",
            "body": None,
            "value": int(round((run.end - run.start) / 60)),
            "unit": "min",
            "value_json": canonical_json({"lat": lat, "lon": lon, "place_id": cell}),
            "media_path": None,
            "origin_app": run.origin,
            "participants": [{"kind": "place_id", "value": cell, "role": "location"}],
            "last_modified_at": as_of,
        })
    records.sort(key=lambda r: (r["started_at"], r["external_id"]))
    dates = [r["journal_date"] for r in records]
    return base.empty_scan(
        name,
        source_file=target.name,
        source_hash=base.file_hash(target) if target.is_file() else None,
        records=records,
        handles=[{"kind": "place_id", "value": c, "display_name": None}
                 for c in sorted(cells)],
        unmapped=[{"what": k, "count": v} for k, v in sorted(acc.unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(acc.skipped.items())],
        facts={"files_read": sum(acc.formats.values()),
               "formats": dict(sorted(acc.formats.items())),
               "points": acc.points, "as_of": as_of,
               "open_since": _iso(open_stay.start) if open_stay else None,
               "oldest_visit": min(dates) if dates else None,
               "newest_visit": max(dates) if dates else None,
               "timezone": cfg.timezone},
        target_kind="file" if target.is_file() else "folder")


# ---------------------------------------------------------------- report

def _local_hm(iso: str, offset: int | None, tz: str | None) -> str:
    dt = datetime.strptime(iso, _ISO).replace(tzinfo=timezone.utc)
    if offset is not None:
        return (dt + timedelta(seconds=offset)).strftime("%H:%M")
    try:
        return dt.astimezone(ZoneInfo(tz or "UTC")).strftime("%H:%M")
    except (KeyError, ValueError):
        return dt.strftime("%H:%MZ")


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    tz = facts.get("timezone")
    formats = ", ".join(f"{n} {k}" for k, n in (facts.get("formats") or {}).items())
    head = (f"gps track: {scan['source_file']} ({scan.get('target_kind', 'folder')}, "
            f"{facts.get('files_read', 0)} file(s) read{': ' + formats if formats else ''}; "
            f"{facts.get('points', 0):,} point(s)")
    if facts.get("as_of"):
        head += f"; newest fix {facts['as_of'][:10]}"
    lines = [head + ")"]
    if facts.get("oldest_visit"):
        lines.append(f"  visits span {facts['oldest_visit']}..{facts['newest_visit']} "
                     f"— check the oldest against the day logging began")

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    if new or upd:
        recs = new or upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        r = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        sample = (f"{r['journal_date']} {_local_hm(r['started_at'], r['utc_offset_sec'], tz)} "
                  f"{r['title']}, {r['value']} min")
        lines.append(f"  place_visit: {len(new)} new, {len(upd)} changed | {span} "
                     f"| e.g. {sample}")
    elif scan["records"]:
        lines.append("  nothing new — the tracks match the database")
    else:
        lines.append("  no visits found — no run of points stayed within "
                     f"{RADIUS_M} m for {MIN_STAY_S // 60} minutes")
    if facts.get("open_since"):
        lines.append(f"  still open: the newest visit (since {facts['open_since'][:16]}Z) "
                     f"has no departure yet — its end moves forward as the file grows")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} unchanged visit(s) re-dated to the newest fix")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} visit(s) listed twice")
    if tail:
        lines.append("  " + ", ".join(tail))
    if scan["handles"]:
        lines.append(f"  places: {len(scan['handles'])} grid cell(s) of about 150 m — link "
                     f"one to a place once with 'lifecrm handles link place_id:gps:<cell> "
                     f"--entity <id>'")
    if plan["conflicts"]:
        lines.append(f"  kept existing: {len(plan['conflicts'])} visit(s) differ from what a "
                     f"newer reading already stored — an older one cannot overwrite them")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not read: {u['what']}" + (f" × {u['count']}" if u["count"] > 1 else ""))
    return lines

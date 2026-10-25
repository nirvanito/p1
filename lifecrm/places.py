"""Places: town and country names for where you were, trips away from home,
and the countries you have records in — from a local copy of GeoNames.

Google's on-device Timeline export names no place: a visit arrives as
"Place at 46.3625, 14.0938", and a photo carries only its GPS position. The
names come from GeoNames (geonames.org, CC BY 4.0), a free gazetteer of
every town, read from a file on this machine — never a web API, so nothing
about where you were leaves the computer. `lifecrm places fetch` downloads
it once into data/geo/ (it is the only command here that touches the
network, and only when you run it); `[places] gazetteer` points at any
GeoNames cities file instead.

Everything here is derived and reads only. A town is "the nearest GeoNames
town within max_km of the recorded position", said as such and cited to the
row whose position it is — never stored, never asserted as where you were.
Without the file the feature says so and names nothing.

    home     `[places] home = "lat, lon"`, else for each day the town with the
             most days of records within 90 days either side (a move to a
             new city moves home with it) — the town with the most days of
             all where those 180 days hold fewer than 14 located days
    trip     consecutive days with a record more than trip_km from that
             day's home; a day with no located record between two away days
             does not end it, a day at home does
    country  every country a located record falls in, first and last day
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .domain.util import DomainError

SECTION = "places"
DEFAULTS = {"gazetteer": None, "home": None, "trip_km": 100, "max_km": 50}
LOCATED_STREAMS = ("place_visit", "photo")
GEONAMES_BASE = "https://download.geonames.org/export/dump/"
DATASETS = ("cities500", "cities1000", "cities5000", "cities15000")
DEFAULT_DATASET = "cities1000"
HOME_WINDOW_DAYS = 90
HOME_MIN_DAYS = 14             # fewer located days than this around a day: overall home
CELL = 0.25                     # grid cell, degrees
HEADER = "#lifecrm-gazetteer"
ATTRIBUTION = "GeoNames (geonames.org), CC BY 4.0"

# ISO 3166-1 alpha-2 -> the short English name an answer uses (GeoNames' XK
# for Kosovo included).
_COUNTRY_TABLE = """
AD Andorra|AE United Arab Emirates|AF Afghanistan|AG Antigua and Barbuda|AI Anguilla|
AL Albania|AM Armenia|AO Angola|AQ Antarctica|AR Argentina|AS American Samoa|AT Austria|
AU Australia|AW Aruba|AX Åland Islands|AZ Azerbaijan|BA Bosnia and Herzegovina|
BB Barbados|BD Bangladesh|BE Belgium|BF Burkina Faso|BG Bulgaria|BH Bahrain|BI Burundi|
BJ Benin|BL Saint Barthélemy|BM Bermuda|BN Brunei|BO Bolivia|BQ Caribbean Netherlands|
BR Brazil|BS Bahamas|BT Bhutan|BV Bouvet Island|BW Botswana|BY Belarus|BZ Belize|
CA Canada|CC Cocos (Keeling) Islands|CD DR Congo|CF Central African Republic|
CG Republic of the Congo|CH Switzerland|CI Côte d'Ivoire|CK Cook Islands|CL Chile|
CM Cameroon|CN China|CO Colombia|CR Costa Rica|CU Cuba|CV Cabo Verde|CW Curaçao|
CX Christmas Island|CY Cyprus|CZ Czechia|DE Germany|DJ Djibouti|DK Denmark|DM Dominica|
DO Dominican Republic|DZ Algeria|EC Ecuador|EE Estonia|EG Egypt|EH Western Sahara|
ER Eritrea|ES Spain|ET Ethiopia|FI Finland|FJ Fiji|FK Falkland Islands|FM Micronesia|
FO Faroe Islands|FR France|GA Gabon|GB United Kingdom|GD Grenada|GE Georgia|
GF French Guiana|GG Guernsey|GH Ghana|GI Gibraltar|GL Greenland|GM Gambia|GN Guinea|
GP Guadeloupe|GQ Equatorial Guinea|GR Greece|GS South Georgia and the South Sandwich Islands|
GT Guatemala|GU Guam|GW Guinea-Bissau|GY Guyana|HK Hong Kong|
HM Heard Island and McDonald Islands|HN Honduras|HR Croatia|HT Haiti|HU Hungary|
ID Indonesia|IE Ireland|IL Israel|IM Isle of Man|IN India|IO British Indian Ocean Territory|
IQ Iraq|IR Iran|IS Iceland|IT Italy|JE Jersey|JM Jamaica|JO Jordan|JP Japan|KE Kenya|
KG Kyrgyzstan|KH Cambodia|KI Kiribati|KM Comoros|KN Saint Kitts and Nevis|KP North Korea|
KR South Korea|KW Kuwait|KY Cayman Islands|KZ Kazakhstan|LA Laos|LB Lebanon|LC Saint Lucia|
LI Liechtenstein|LK Sri Lanka|LR Liberia|LS Lesotho|LT Lithuania|LU Luxembourg|LV Latvia|
LY Libya|MA Morocco|MC Monaco|MD Moldova|ME Montenegro|MF Saint Martin|MG Madagascar|
MH Marshall Islands|MK North Macedonia|ML Mali|MM Myanmar|MN Mongolia|MO Macao|
MP Northern Mariana Islands|MQ Martinique|MR Mauritania|MS Montserrat|MT Malta|
MU Mauritius|MV Maldives|MW Malawi|MX Mexico|MY Malaysia|MZ Mozambique|NA Namibia|
NC New Caledonia|NE Niger|NF Norfolk Island|NG Nigeria|NI Nicaragua|NL Netherlands|
NO Norway|NP Nepal|NR Nauru|NU Niue|NZ New Zealand|OM Oman|PA Panama|PE Peru|
PF French Polynesia|PG Papua New Guinea|PH Philippines|PK Pakistan|PL Poland|
PM Saint Pierre and Miquelon|PN Pitcairn Islands|PR Puerto Rico|PS Palestine|PT Portugal|
PW Palau|PY Paraguay|QA Qatar|RE Réunion|RO Romania|RS Serbia|RU Russia|RW Rwanda|
SA Saudi Arabia|SB Solomon Islands|SC Seychelles|SD Sudan|SE Sweden|SG Singapore|
SH Saint Helena|SI Slovenia|SJ Svalbard and Jan Mayen|SK Slovakia|SL Sierra Leone|
SM San Marino|SN Senegal|SO Somalia|SR Suriname|SS South Sudan|ST São Tomé and Príncipe|
SV El Salvador|SX Sint Maarten|SY Syria|SZ Eswatini|TC Turks and Caicos Islands|TD Chad|
TF French Southern Territories|TG Togo|TH Thailand|TJ Tajikistan|TK Tokelau|TL Timor-Leste|
TM Turkmenistan|TN Tunisia|TO Tonga|TR Turkey|TT Trinidad and Tobago|TV Tuvalu|TW Taiwan|
TZ Tanzania|UA Ukraine|UG Uganda|UM U.S. Minor Outlying Islands|US United States|
UY Uruguay|UZ Uzbekistan|VA Vatican City|VC Saint Vincent and the Grenadines|
VE Venezuela|VG British Virgin Islands|VI U.S. Virgin Islands|VN Vietnam|VU Vanuatu|
WF Wallis and Futuna|WS Samoa|XK Kosovo|YE Yemen|YT Mayotte|ZA South Africa|ZM Zambia|
ZW Zimbabwe
"""
COUNTRIES: dict[str, str] = {}
for _part in _COUNTRY_TABLE.replace("\n", "").split("|"):
    _part = _part.strip()
    if _part:
        COUNTRIES[_part[:2]] = _part[3:].strip()
# Other names a question may use for a country.
ALIASES = {"usa": "US", "us": "US", "u s": "US", "u s a": "US", "america": "US",
           "united states of america": "US", "the states": "US", "uk": "GB",
           "u k": "GB", "britain": "GB", "great britain": "GB", "england": "GB",
           "scotland": "GB", "wales": "GB", "northern ireland": "GB", "holland": "NL",
           "the netherlands": "NL", "czech republic": "CZ", "turkiye": "TR",
           "ivory coast": "CI", "burma": "MM", "korea": "KR", "cape verde": "CV",
           "swaziland": "SZ", "macedonia": "MK", "vatican": "VA", "drc": "CD",
           "congo": "CG", "east timor": "TL", "bosnia": "BA", "uae": "AE",
           "the uae": "AE", "the us": "US", "the uk": "GB", "the usa": "US"}


def fold(text: str) -> str:
    """Lowercase, accents off, punctuation to spaces: 'Ljubljana' and
    'ljubljana', 'Zürich' and 'zurich', 'U.S.' and 'u s' compare equal."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^\w]+", " ", t).split())


_COUNTRY_BY_NAME = {fold(n): c for c, n in COUNTRIES.items()}
_COUNTRY_BY_NAME.update({fold(k): v for k, v in ALIASES.items()})


def country_name(code: str | None) -> str | None:
    if not code:
        return None
    return COUNTRIES.get(code.upper(), code.upper())


def country_code(name: str) -> str | None:
    """The country a name means ('Slovenia', 'the UK', 'Türkiye'), or None."""
    return _COUNTRY_BY_NAME.get(fold(name))


# ---------------------------------------------------------------- settings

def settings(cfg: Config) -> dict:
    raw = (cfg.extra or {}).get(SECTION)
    raw = raw if isinstance(raw, dict) else {}
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise DomainError(f"[{SECTION}] has unknown setting(s) {', '.join(unknown)} — "
                          f"known: {', '.join(sorted(DEFAULTS))}")
    out = dict(DEFAULTS)
    if raw.get("gazetteer") not in (None, ""):
        if not isinstance(raw["gazetteer"], str):
            raise DomainError(f"[{SECTION}] gazetteer must be a file path in quotes")
        out["gazetteer"] = raw["gazetteer"]
    if raw.get("home") not in (None, ""):
        out["home"] = parse_point(raw["home"], f"[{SECTION}] home")
    for key, lo, hi in (("trip_km", 5, 5000), ("max_km", 1, 500)):
        if key in raw:
            v = raw[key]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise DomainError(f"[{SECTION}] {key} must be a distance in km, {lo}-{hi}, "
                                  f"got {v!r}")
            out[key] = float(v)
    return out


def parse_point(value, label: str) -> tuple[float, float]:
    """'46.36, 14.09' (or [46.36, 14.09]) -> (lat, lon); one-line error."""
    try:
        if isinstance(value, str):
            lat_s, lon_s = value.replace(";", ",").split(",")
            lat, lon = float(lat_s), float(lon_s)
        else:
            lat, lon = float(value[0]), float(value[1])
    except (ValueError, TypeError, IndexError):
        raise DomainError(f'{label} must be "latitude, longitude", got {value!r}') from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise DomainError(f"{label} {value!r} is not a position on Earth")
    return lat, lon


def gazetteer_path(cfg: Config, s: dict | None = None) -> Path:
    s = s or settings(cfg)
    if s["gazetteer"]:
        return cfg.resolve(s["gazetteer"])
    return cfg.data_dir / "geo" / "places.tsv"


# ---------------------------------------------------------------- the gazetteer

@dataclass(frozen=True)
class Town:
    name: str
    lat: float
    lon: float
    cc: str
    region: str
    population: int

    @property
    def key(self) -> str:
        return f"{self.cc}|{self.region}|{self.name}"

    @property
    def country(self) -> str:
        return country_name(self.cc) or self.cc

    @property
    def label(self) -> str:
        return f"{self.name}, {self.country}"


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine), kilometres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


class Gazetteer:
    """Towns on a grid of CELL-degree cells; nearest() scans only the cells
    a max_km circle can reach."""

    def __init__(self, towns: list[Town], source: str):
        self.towns = towns
        self.source = source
        self.grid: dict[tuple[int, int], list[Town]] = {}
        for t in towns:
            self.grid.setdefault(self._cell(t.lat, t.lon), []).append(t)
        self._memo: dict[tuple, Town | None] = {}

    @staticmethod
    def _cell(lat: float, lon: float) -> tuple[int, int]:
        return int(math.floor(lat / CELL)), int(math.floor(lon / CELL))

    def nearest(self, lat: float, lon: float, max_km: float) -> Town | None:
        memo_key = (round(lat, 2), round(lon, 2), max_km)
        if memo_key in self._memo:
            return self._memo[memo_key]
        ci, cj = self._cell(lat, lon)
        di = int(math.ceil(max_km / (110.574 * CELL))) + 1
        coslat = max(math.cos(math.radians(min(abs(lat) + di * CELL, 89.9))), 1e-6)
        dj = min(int(math.ceil(max_km / (111.320 * coslat * CELL))) + 1, int(360 / CELL))
        best, best_d = None, max_km
        for i in range(ci - di, ci + di + 1):
            for j in range(cj - dj, cj + dj + 1):
                jj = (j + int(180 / CELL)) % int(360 / CELL) - int(180 / CELL)
                for t in self.grid.get((i, jj), ()):
                    d = km(lat, lon, t.lat, t.lon)
                    # nearer wins; at the same distance the bigger town, then the name
                    if d < best_d or (best is not None and d == best_d and (
                            -t.population, t.name) < (-best.population, best.name)):
                        best, best_d = t, d
        self._memo[memo_key] = best
        return best


_CACHE: dict[tuple, Gazetteer] = {}


def _read(path: Path) -> Gazetteer:
    """A compact file `places fetch` wrote (6 columns, a header line) or a
    GeoNames cities file as downloaded (19 columns, no header)."""
    towns = []
    source = path.name
    with path.open("r", encoding="utf-8-sig") as fh:
        for line in fh:
            if line.startswith("#"):
                if line.startswith(HEADER):
                    bits = line.rstrip("\n").split("\t")
                    source = bits[2] if len(bits) > 2 else source
                continue
            cols = line.rstrip("\r\n").split("\t")
            try:
                if len(cols) >= 19:              # geonameid, name, asciiname, ...
                    if cols[6] and cols[6] != "P":
                        continue
                    towns.append(Town(cols[1], float(cols[4]), float(cols[5]),
                                      cols[8].upper(), cols[10],
                                      int(cols[14] or 0)))
                elif len(cols) >= 6:
                    towns.append(Town(cols[0], float(cols[1]), float(cols[2]),
                                      cols[3].upper(), cols[4], int(cols[5] or 0)))
            except ValueError:
                continue
    return Gazetteer(towns, source)


def load(cfg: Config) -> Gazetteer | None:
    """The gazetteer for this configuration, read once per process (and
    again when the file changes); None when there is no file."""
    path = gazetteer_path(cfg)
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = _read(path)
    return _CACHE[key]


def status(cfg: Config) -> dict:
    """What the feature has to work with, for `places status` and the lists."""
    try:
        path = gazetteer_path(cfg)
    except DomainError as e:
        return {"ok": False, "path": None, "towns": 0, "error": str(e)}
    g = load(cfg)
    return {"ok": g is not None and bool(g.towns), "path": str(path),
            "towns": len(g.towns) if g else 0, "source": g.source if g else None,
            "error": None if g else f"no gazetteer at {path} — run: python -m lifecrm "
                                    f"places fetch"}


def town_at(cfg: Config, lat, lon) -> Town | None:
    if isinstance(lat, bool) or isinstance(lon, bool) \
            or not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat, lon) == (0, 0):
        return None
    g = load(cfg)
    if g is None:
        return None
    return g.nearest(float(lat), float(lon), settings(cfg)["max_km"])


def label_for(cfg: Config, lat, lon) -> str | None:
    """'Bled, Slovenia' for a position, or None (no gazetteer, no position,
    nothing within max_km)."""
    t = town_at(cfg, lat, lon)
    return t.label if t else None


# ---------------------------------------------------------------- the records

@dataclass
class Point:
    obs_id: int
    uuid: str
    stream: str
    date: str
    started_at: str | None
    lat: float
    lon: float
    town: Town | None
    import_uuid: str


def points(conn: sqlite3.Connection, cfg: Config, start: str | None = None,
           end: str | None = None) -> list[Point]:
    """Every active place visit and photo with a position, oldest first, each
    with its nearest town."""
    marks = ",".join("?" * len(LOCATED_STREAMS))
    where, args = "", []
    if start:
        where += " AND o.journal_date >= ?"
        args.append(start)
    if end:
        where += " AND o.journal_date <= ?"
        args.append(end)
    try:
        rows = conn.execute(
            "SELECT o.id, o.uuid, o.stream, o.journal_date, o.started_at, "
            "json_extract(o.value_json, '$.lat') AS lat, "
            "json_extract(o.value_json, '$.lon') AS lon, i.uuid AS import_uuid "
            "FROM observations o JOIN imports i ON i.id = o.import_id "
            f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
            f"AND json_valid(o.value_json) {where} "
            "ORDER BY o.journal_date, o.started_at, o.id", (*LOCATED_STREAMS, *args)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        lat, lon = r["lat"], r["lon"]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) \
                or isinstance(lat, bool) or (lat, lon) == (0, 0):
            continue
        out.append(Point(r["id"], r["uuid"], r["stream"], r["journal_date"],
                         r["started_at"], float(lat), float(lon),
                         town_at(cfg, lat, lon), r["import_uuid"]))
    return out


def _dominant(day_points: list[Point]) -> Town | None:
    counts: dict[str, list] = {}
    for i, p in enumerate(day_points):
        if p.town is None:
            continue
        slot = counts.setdefault(p.town.key, [0, i, p.town])
        slot[0] += 1
    if not counts:
        return None
    return min(counts.values(), key=lambda s: (-s[0], s[1]))[2]


_TIMELINES: dict[tuple, dict] = {}


def _located_stamp(conn: sqlite3.Connection) -> tuple:
    """What a timeline depends on in the database: the located rows' count,
    newest id and newest change — cheap, through the stream index."""
    marks = ",".join("?" * len(LOCATED_STREAMS))
    try:
        r = conn.execute(f"SELECT COUNT(*), MAX(id), MAX(updated_at) FROM observations "
                         f"WHERE stream IN ({marks}) AND status = 'active'",
                         LOCATED_STREAMS).fetchone()
        path = conn.execute("PRAGMA database_list").fetchone()["file"]
    except sqlite3.OperationalError:
        return ("none",)
    return (path, r[0], r[1], r[2])


def timeline(conn: sqlite3.Connection, cfg: Config) -> dict:
    """The whole located history, worked out once: per day its towns and
    its home, the trips, the countries. {'ok': False, 'error'} without a
    gazetteer. Kept per process until the located rows, the gazetteer or the
    settings change (the web UI asks for it on every day view)."""
    st = status(cfg)
    if not st["ok"]:
        return {"ok": False, "error": st["error"], "days": {}, "trips": [],
                "countries": [], "home": None}
    s = settings(cfg)
    key = (_located_stamp(conn), next(iter(_CACHE), None), tuple(sorted(
        (k, str(v)) for k, v in s.items())))
    if key in _TIMELINES:
        return _TIMELINES[key]
    out = _timeline(conn, cfg, s, st)
    if len(_TIMELINES) >= 4:
        _TIMELINES.clear()
    _TIMELINES[key] = out
    return out


def _timeline(conn: sqlite3.Connection, cfg: Config, s: dict, st: dict) -> dict:
    pts = points(conn, cfg)
    by_day: dict[str, list[Point]] = {}
    for p in pts:
        by_day.setdefault(p.date, []).append(p)
    days = sorted(by_day)
    dominant = {d: _dominant(by_day[d]) for d in days}
    homes = _homes(days, dominant, s)
    info: dict[str, dict] = {}
    for d in days:
        home = homes.get(d)
        towns, seen = [], set()
        for p in by_day[d]:
            if p.town is not None and p.town.key not in seen:
                seen.add(p.town.key)
                towns.append(p.town)
        far = 0.0
        if home is not None:
            far = max(km(home[0], home[1], p.lat, p.lon) for p in by_day[d])
        info[d] = {"towns": towns, "home": home, "far_km": round(far, 1),
                   "away": home is not None and far > s["trip_km"],
                   "points": by_day[d]}
    trips = _trips(days, info, s)
    return {"ok": True, "error": None, "days": info, "trips": trips,
            "countries": _countries(pts), "home": _home_summary(homes, dominant, s),
            "trip_km": s["trip_km"], "source": st["source"]}


def _homes(days: list[str], dominant: dict, s: dict) -> dict[str, tuple]:
    """Each located day's home (lat, lon, label): the configured point, else
    the dominant town with the most days within HOME_WINDOW_DAYS either side
    — or, where that window holds fewer than HOME_MIN_DAYS located days (a
    trip recorded with nothing around it), the town with the most days of
    all."""
    if s["home"]:
        lat, lon = s["home"]
        return {d: (lat, lon, "home (config)") for d in days}
    overall: dict[str, list] = {}
    for d in days:
        t = dominant[d]
        if t is not None:
            overall.setdefault(t.key, [0, t])[0] += 1
    fallback = (min(overall.values(), key=lambda x: (-x[0], x[1].key))[1]
                if overall else None)
    out = {}
    dates = [date.fromisoformat(d) for d in days]
    lo = hi = 0
    counts: dict[str, list] = {}
    for i, d in enumerate(dates):
        while hi < len(dates) and dates[hi] <= d + timedelta(days=HOME_WINDOW_DAYS):
            t = dominant[days[hi]]
            if t is not None:
                slot = counts.setdefault(t.key, [0, t])
                slot[0] += 1
            hi += 1
        while dates[lo] < d - timedelta(days=HOME_WINDOW_DAYS):
            t = dominant[days[lo]]
            if t is not None:
                counts[t.key][0] -= 1
            lo += 1
        live = [(n, t) for n, t in counts.values() if n > 0]
        if live and sum(n for n, _t in live) >= HOME_MIN_DAYS:
            n, t = min(live, key=lambda x: (-x[0], x[1].key))
            out[days[i]] = (t.lat, t.lon, t.label)
        elif fallback is not None:
            out[days[i]] = (fallback.lat, fallback.lon, fallback.label)
    return out


def _home_summary(homes: dict, dominant: dict, s: dict) -> dict | None:
    if not homes:
        return None
    labels: dict[str, dict] = {}
    for d in sorted(homes):
        h = homes[d]
        slot = labels.setdefault(h[2], {"label": h[2], "lat": h[0], "lon": h[1],
                                         "first_date": d, "last_date": d, "days": 0})
        slot["last_date"] = d
        slot["days"] += 1
    ordered = sorted(labels.values(), key=lambda x: x["first_date"])
    return {"how": "config" if s["home"] else ("most days within 90 days either side, else "
                                               "most days overall"),
            "homes": ordered}


def _trips(days: list[str], info: dict, s: dict) -> list[dict]:
    """Runs of away days; a gap of one day with no located record keeps a
    trip going, a day at home ends it."""
    trips, cur = [], None
    prev = None
    for d in days:
        day = info[d]
        if day["away"]:
            gap = (date.fromisoformat(d) - date.fromisoformat(prev)).days if prev else 99
            if cur is not None and gap <= 2:
                cur["days"].append(d)
            else:
                if cur:
                    trips.append(cur)
                cur = {"days": [d]}
        else:
            if cur:
                trips.append(cur)
                cur = None
        prev = d
    if cur:
        trips.append(cur)
    out = []
    for t in trips:
        towns, seen, ccs = [], set(), []
        pts = []
        far = 0.0
        home = info[t["days"][0]]["home"]
        for d in t["days"]:
            far = max(far, info[d]["far_km"])
            pts += info[d]["points"]
            for town in info[d]["towns"]:
                if home and km(home[0], home[1], town.lat, town.lon) <= s["trip_km"] / 2:
                    continue                       # the way out and back
                if town.key not in seen:
                    seen.add(town.key)
                    towns.append(town)
                if town.cc not in ccs:
                    ccs.append(town.cc)
        start, end = t["days"][0], t["days"][-1]
        out.append({"start": start, "end": end,
                    "nights": (date.fromisoformat(end) - date.fromisoformat(start)).days,
                    "days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1,
                    "days_located": len(t["days"]),
                    "towns": [{"name": x.name, "region": x.region, "cc": x.cc,
                               "country": x.country, "label": x.label} for x in towns],
                    "countries": [{"cc": c, "name": country_name(c)} for c in ccs],
                    "far_km": round(far), "home": home[2] if home else None,
                    "records": len(pts), "first_uuid": pts[0].uuid, "last_uuid": pts[-1].uuid,
                    "uuids": [p.uuid for p in pts][:20],
                    "imports": sorted({p.import_uuid for p in pts}),
                    "first_obs_id": pts[0].obs_id, "last_obs_id": pts[-1].obs_id})
    return out


def _countries(pts: list[Point]) -> list[dict]:
    by: dict[str, dict] = {}
    for p in pts:
        if p.town is None:
            continue
        c = by.setdefault(p.town.cc, {"cc": p.town.cc, "name": p.town.country,
                                      "first_date": p.date, "last_date": p.date,
                                      "days": set(), "records": 0, "towns": [],
                                      "first_uuid": p.uuid, "last_uuid": p.uuid,
                                      "uuids": [], "imports": set(),
                                      "first_obs_id": p.obs_id, "last_obs_id": p.obs_id})
        c["last_date"], c["last_uuid"], c["last_obs_id"] = p.date, p.uuid, p.obs_id
        c["days"].add(p.date)
        c["records"] += 1
        if p.town.name not in c["towns"]:
            c["towns"].append(p.town.name)
        if len(c["uuids"]) < 20:
            c["uuids"].append(p.uuid)
        c["imports"].add(p.import_uuid)
    out = []
    for c in sorted(by.values(), key=lambda c: (c["first_date"], c["cc"])):
        out.append({**c, "days": len(c["days"]), "imports": sorted(c["imports"])})
    return out


def trips_between(tl: dict, start: str, end: str) -> list[dict]:
    """The trips that overlap [start, end]."""
    return [t for t in tl["trips"] if t["end"] >= start and t["start"] <= end]


def visits_to(tl: dict, *, cc: str | None = None, town: str | None = None) -> dict:
    """Every located day in a country (cc) or a town (folded name): the days,
    the towns, first and last, and the trips they belong to."""
    want = fold(town) if town else None
    days, towns = [], []
    first_obs = last_obs = None
    uuids, imports, records = [], set(), 0
    for d in sorted(tl["days"]):
        hits = [p for p in tl["days"][d]["points"] if p.town is not None
                and (p.town.cc == cc if cc else fold(p.town.name) == want)]
        if not hits:
            continue
        days.append(d)
        records += len(hits)
        for p in hits:
            if p.town.name not in towns:
                towns.append(p.town.name)
            if len(uuids) < 20:
                uuids.append(p.uuid)
            imports.add(p.import_uuid)
        first_obs = first_obs or hits[0]
        last_obs = hits[-1]
    trips = [t for t in tl["trips"] if any(t["start"] <= d <= t["end"] for d in days)]
    return {"days": days, "towns": towns, "records": records, "uuids": uuids,
            "imports": sorted(imports), "first": first_obs, "last": last_obs,
            "trips": trips}


def known_town(tl: dict, name: str) -> str | None:
    """A visited town's own spelling for a folded name, or None."""
    want = fold(name)
    for d in tl["days"].values():
        for t in d["towns"]:
            if fold(t.name) == want:
                return t.name
    return None


# ---------------------------------------------------------------- fetch

def fetch(cfg: Config, *, dataset: str = DEFAULT_DATASET, base: str = GEONAMES_BASE,
          opener=None, dry_run: bool = False) -> dict:
    """Download GeoNames' `dataset` (a zip of towns) and its region names into
    data/geo/places.tsv — the only network access in this module, run by the
    owner. Written atomically; the previous file stays until the new one is
    complete."""
    if dataset not in DATASETS:
        raise DomainError(f"dataset must be one of {', '.join(DATASETS)}, got {dataset!r}")
    cities_url = f"{base}{dataset}.zip"
    admin_url = f"{base}admin1CodesASCII.txt"
    target = gazetteer_path(cfg)
    if dry_run:
        return {"dry_run": True, "downloads": [cities_url, admin_url], "target": str(target)}
    if opener is None:
        import urllib.request
        opener = lambda url: urllib.request.urlopen(url, timeout=120)  # noqa: E731

    def get(url: str) -> bytes:
        try:
            with opener(url) as resp:
                return resp.read()
        except OSError as e:
            raise DomainError(f"could not download {url}: {e}") from e

    raw_zip = get(cities_url)
    try:
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
            name = next((n for n in zf.namelist() if n.endswith(".txt")), None)
            if name is None:
                raise DomainError(f"{cities_url} holds no .txt file")
            cities = zf.read(name).decode("utf-8")
    except zipfile.BadZipFile as e:
        raise DomainError(f"{cities_url} is not a zip file ({e})") from e
    admin_names: dict[str, str] = {}
    for line in get(admin_url).decode("utf-8").splitlines():
        cols = line.split("\t")
        if len(cols) >= 2:
            admin_names[cols[0]] = cols[1]
    rows = []
    for line in cities.splitlines():
        cols = line.split("\t")
        if len(cols) < 19 or (cols[6] and cols[6] != "P"):
            continue
        try:
            float(cols[4]), float(cols[5])
        except ValueError:
            continue
        region = admin_names.get(f"{cols[8]}.{cols[10]}", cols[10])
        rows.append((cols[1], cols[4], cols[5], cols[8].upper(), region,
                     cols[14] or "0"))
    if not rows:
        raise DomainError(f"{cities_url} held no towns — nothing written")
    rows.sort(key=lambda r: (r[3], r[4], r[0], r[1], r[2]))
    target.parent.mkdir(parents=True, exist_ok=True)
    header = f"{HEADER}\tv1\t{dataset} from {base}\t{ATTRIBUTION}\n"
    body = header + "".join("\t".join(r) + "\n" for r in rows)
    fd, tmp = tempfile.mkstemp(prefix=".places.", suffix=".tsv", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.replace(tmp, target)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        raise
    _CACHE.clear()
    return {"dry_run": False, "target": str(target), "towns": len(rows),
            "bytes": len(body.encode("utf-8")), "downloads": [cities_url, admin_url]}


def write_compact(path: Path, towns: list[tuple], source: str = "test") -> None:
    """A compact gazetteer from (name, lat, lon, cc, region, population)
    rows — what tests and the demo use instead of a download."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{HEADER}\tv1\t{source}\t{ATTRIBUTION}"]
    lines += ["\t".join(str(x) for x in t) for t in towns]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    _CACHE.clear()


def describe_trip(t: dict) -> str:
    towns = ", ".join(x["label"] if len(t["countries"]) > 1 else x["name"]
                      for x in t["towns"][:6]) or "unnamed places"
    more = f" and {len(t['towns']) - 6} more" if len(t["towns"]) > 6 else ""
    where = towns + more
    if len(t["countries"]) == 1 and t["towns"]:
        where += f" ({t['countries'][0]['name']})"
    span = t["start"] if t["start"] == t["end"] else f"{t['start']} to {t['end']}"
    return (f"{span}: {where} — {t['days']} day{'' if t['days'] == 1 else 's'}, "
            f"up to {t['far_km']} km from {t['home'] or 'home'}")


def json_safe_trip(t: dict) -> dict:
    return {k: v for k, v in t.items() if k not in ("first_obs_id", "last_obs_id")}


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)

"""Sample photos for the demo: the persona's camera roll, rendered three ways.

  'android'  The Samsung Gallery folder as OneDrive syncs it from a Galaxy:
             `Samsung Gallery/DCIM/Camera/20220927_101207.jpg`, one small but
             valid JPEG per persona photo carrying the EXIF a Galaxy S24
             writes — make 'samsung', model 'SM-S921U', DateTimeOriginal with
             OffsetTimeOriginal, GPS position, altitude and clock. Plus one
             screenshot with no EXIF at all, so the file-name fallback shows
             up in the demo the way it does on a real phone.
  'iphone'   A Mac Photos library: `Photos Library.photoslibrary/database/
             Photos.sqlite` with the Core Data tables Photos 6+ writes
             (ZASSET, ZADDITIONALASSETATTRIBUTES, ZASSETDESCRIPTION,
             ZEXTENDEDATTRIBUTES, Z_METADATA). Only the database is written —
             the connector never opens the originals, and an HEIC original
             cannot be produced without an HEVC encoder. Beyond the persona's
             photos it holds a Live-Photo-era video of the rowboat on Lake
             Bled, a screenshot, a trashed duplicate and a hidden passport
             scan: the cases a real library always has.
  'windows-iphone'  iCloud for Windows' folder (`Pictures/iCloud Photos/
             Photos/IMG_7402.JPG`, ...): the same iPhone's photos downloaded
             to the laptop as JPEGs with Apple's big-endian EXIF.

The pixel data is a two-band picture — sky over ground, coloured by the kind
of place — so the files open in any viewer. The JPEG writer is a minimal
baseline encoder (one DC coefficient per block, every AC run an EOB), and the
EXIF writer builds a real TIFF structure; both are reused by the tests to
make inputs the connector must read.

Deterministic for a given (anchor, combo): positions jitter around the
persona's places with a seeded generator, and file times are set from the
capture time. Asset identities depend on the photo, not the anchor, so the
same picture keeps its id when the demo is rebuilt for another day.
"""

from __future__ import annotations

import os
import random
import sqlite3
import struct
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import persona

ANDROID_MAKE, ANDROID_MODEL = "samsung", "SM-S921U"
IPHONE_MAKE = "Apple"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
LIBRARY_UUID = "6A1C3E52-7D0B-4E59-9B7C-2F1D8E4A9C31"
_NS = uuid.UUID("0b7c2a5e-6d3f-4c1a-9e8b-5a4f3c2d1e0f")

# Metres above sea level; Austin is ~150 m everywhere the persona goes.
_ALTITUDE = {"bled": 476.0, "bled_castle": 601.0, "ljubljana": 295.0}
# (sky, ground) per kind of place, as RGB.
_COLORS = {
    "landmark": ((128, 176, 222), (38, 104, 140)),
    "city": ((150, 170, 196), (150, 128, 110)),
    "restaurant": ((92, 60, 44), (204, 128, 62)),
    "gym": ((176, 176, 186), (96, 104, 124)),
    "park": ((140, 190, 232), (72, 140, 70)),
    "office": ((200, 204, 214), (120, 124, 136)),
    "store": ((210, 210, 200), (180, 40, 40)),
}


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo == "android":
        return _samsung_gallery(Path(dest), anchor)
    if combo == "iphone":
        return _photos_library(Path(dest), anchor)
    if combo == "windows-iphone":
        return _icloud_for_windows(Path(dest), anchor)
    raise ValueError(f"photos has no sample for combo '{combo}' "
                     f"(expected one of: {', '.join(persona.COMBOS)})")


# ---------------------------------------------------------------- the story

def _shots(anchor: date, combo: str) -> list[dict]:
    """Every persona photo with its capture instant, zone and position."""
    rng = random.Random(f"lifecrm-demo-photos-{anchor.isoformat()}-{combo}")
    out = []
    for p in persona.PHOTOS:
        local = persona.photo_dt(p, anchor).replace(second=rng.randint(0, 59))
        place = persona.place(p.place)
        out.append({
            "photo": p, "local": local, "place": place,
            "lat": round(place.lat + rng.uniform(-0.0004, 0.0004), 6),
            "lon": round(place.lon + rng.uniform(-0.0004, 0.0004), 6),
            "alt": round(_ALTITUDE.get(place.key, 149.0) + rng.uniform(-3, 3), 1),
        })
    return sorted(out, key=lambda s: s["local"].astimezone(timezone.utc))


def _epoch(dt: datetime) -> float:
    return dt.astimezone(timezone.utc).timestamp()


def _offset_text(dt: datetime) -> str:
    sec = int(dt.utcoffset().total_seconds())
    sign = "-" if sec < 0 else "+"
    sec = abs(sec)
    return f"{sign}{sec // 3600:02d}:{sec % 3600 // 60:02d}"


# ---------------------------------------------------------------- android

def _samsung_gallery(dest: Path, anchor: date) -> Path:
    gallery = dest / "Samsung Gallery"
    camera = gallery / "DCIM" / "Camera"
    camera.mkdir(parents=True, exist_ok=True)
    for i, s in enumerate(_shots(anchor, "android")):
        local = s["local"]
        sky, ground = _COLORS.get(s["place"].kind, _COLORS["city"])
        tiff = exif_tiff(
            make=ANDROID_MAKE, model=ANDROID_MODEL, taken=local,
            offset=_offset_text(local), subsec=f"{(i * 137) % 1000:03d}",
            width=96, height=72, orientation=1,
            lat=s["lat"], lon=s["lon"], alt=s["alt"],
            gps_utc=local.astimezone(timezone.utc), little_endian=True)
        path = camera / f"{local:%Y%m%d_%H%M%S}.jpg"
        path.write_bytes(jpeg_bytes(96, 72, [sky, ground], exif=tiff))
        os.utime(path, (_epoch(local), _epoch(local)))

    # A screenshot carries no camera EXIF; its time is in its name.
    shots = gallery / "DCIM" / "Screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    when = persona.local_dt(anchor, -2, "13:07:41")
    path = shots / f"Screenshot_{when:%Y%m%d_%H%M%S}_Chrome.jpg"
    path.write_bytes(jpeg_bytes(72, 152, [(250, 250, 250), (32, 96, 200)]))
    os.utime(path, (_epoch(when), _epoch(when)))
    return gallery


# ---------------------------------------------------------------- windows-iphone

def _icloud_for_windows(dest: Path, anchor: date) -> Path:
    """iCloud for Windows' Photos folder: the iPhone's camera roll as the
    laptop downloads it, one file per photo under its original iPhone name.
    HEIC photos arrive as JPEG unless 'Keep high efficiency original' is
    ticked, so these are JPEGs carrying the iPhone's EXIF — big-endian TIFF,
    as Apple writes it, with make, model, local time, offset and GPS. Only
    what iCloud Photos shows comes down: nothing hidden or in Recently
    Deleted."""
    root = dest / "Pictures" / "iCloud Photos"
    folder = root / "Photos"
    folder.mkdir(parents=True, exist_ok=True)
    model = persona.DEVICE["windows-iphone"]["phone"]
    old, recent = 1180, 7402                       # the trip's numbers, then today's
    for i, s in enumerate(_shots(anchor, "windows-iphone")):
        local = s["local"]
        if local.date() < persona.day(anchor, -365):
            number, old = old, old + 1
        else:
            number, recent = recent, recent + 1
        sky, ground = _COLORS.get(s["place"].kind, _COLORS["city"])
        tiff = exif_tiff(
            make=IPHONE_MAKE, model=model, taken=local, offset=_offset_text(local),
            subsec=f"{(i * 211) % 1000:03d}", width=96, height=72, orientation=1,
            lat=s["lat"], lon=s["lon"], alt=s["alt"],
            gps_utc=local.astimezone(timezone.utc), little_endian=False)
        path = folder / f"IMG_{number:04d}.JPG"
        path.write_bytes(jpeg_bytes(96, 72, [sky, ground], exif=tiff))
        os.utime(path, (_epoch(local), _epoch(local)))
    return root


# ---------------------------------------------------------------- iphone

def _asset_uuid(key: str) -> str:
    return str(uuid.uuid5(_NS, f"photos-{key}")).upper()


def _photos_library(dest: Path, anchor: date) -> Path:
    library = dest / "Photos Library.photoslibrary"
    db = library / "database" / "Photos.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    model = persona.DEVICE["iphone"]["phone"]
    assets = []
    for s in _shots(anchor, "iphone"):
        p = s["photo"]
        assets.append({
            "key": p.name, "created": s["local"], "lat": s["lat"], "lon": s["lon"],
            "kind": 0, "subtype": 2, "width": 4032, "height": 3024,
            "uti": "public.heic", "ext": "heic", "caption": p.caption,
            "favorite": p.name == "bled_lake_island",
            "make": IPHONE_MAKE, "model": model,
        })

    trip = persona.bled_trip_days(anchor)
    tz = ZoneInfo("Europe/Ljubljana")
    rowboat = persona.place("bled")
    assets.append({
        "key": "bled_rowboat_video",
        "created": datetime(trip[2].year, trip[2].month, trip[2].day, 15, 7, 12, tzinfo=tz),
        "lat": round(rowboat.lat + 0.0002, 6), "lon": round(rowboat.lon - 0.0003, 6),
        "kind": 1, "subtype": 0, "width": 1920, "height": 1080, "duration": 14.53,
        "uti": "com.apple.quicktime-movie", "ext": "mov",
        "caption": "Jake rowing us out to the island",
        "make": IPHONE_MAKE, "model": model,
    })
    assets.append({
        "key": "flight_search_screenshot",
        "created": persona.local_dt(anchor, -2, "13:07:41"), "lat": None, "lon": None,
        "kind": 0, "subtype": 10, "width": 1179, "height": 2556,
        "uti": "public.png", "ext": "png",
    })
    veracruz = persona.place("veracruz")
    assets.append({
        "key": "veracruz_tacos_blurry",
        "created": persona.local_dt(anchor, -38, "19:48:31"),
        "lat": veracruz.lat, "lon": veracruz.lon, "kind": 0, "subtype": 0,
        "width": 4032, "height": 3024, "uti": "public.heic", "ext": "heic",
        "trashed": True, "make": IPHONE_MAKE, "model": model,
    })
    assets.append({
        "key": "passport_scan",
        "created": persona.local_dt(anchor, -24, "21:15:03"), "lat": None, "lon": None,
        "kind": 0, "subtype": 0, "width": 3024, "height": 4032,
        "uti": "public.heic", "ext": "heic", "hidden": True,
        "make": IPHONE_MAKE, "model": model,
    })

    assets.sort(key=lambda a: a["created"].astimezone(timezone.utc))
    old, recent = 1180, 7402
    for a in assets:
        a["uuid"] = _asset_uuid(a["key"])
        if a["created"].date() < persona.day(anchor, -365):
            number, old = old, old + 1
        else:
            number, recent = recent, recent + 1
        a["original_filename"] = f"IMG_{number:04d}.{a['ext'].upper()}"
        a["tz_name"] = str(a["created"].tzinfo)
    write_library(db, assets)
    return library


# ---------------------------------------------------------------- Photos.sqlite

def _core_data(dt: datetime) -> float:
    return (dt.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds()


def write_library(db: Path, assets: list[dict], *, table: str = "ZASSET",
                  library_uuid: str = LIBRARY_UUID) -> Path:
    """A Photos.sqlite holding `assets`, with the columns Photos writes.

    `table` is 'ZASSET' (macOS 11+) or 'ZGENERICASSET' (macOS 10.15, which
    has no ZEXTENDEDATTRIBUTES — camera make and model are absent there).
    Each asset dict: uuid, created (aware datetime), and optionally lat, lon,
    kind, subtype, hidden, favorite, trashed, width, height, duration, uti,
    ext, original_filename, tz_name, title, caption, make, model, modified.
    """
    extended = table == "ZASSET"
    conn = sqlite3.connect(db)
    try:
        conn.executescript(f"""
            CREATE TABLE Z_METADATA (Z_VERSION INTEGER PRIMARY KEY, Z_UUID VARCHAR(255),
                Z_PLIST BLOB);
            CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR,
                Z_SUPER INTEGER, Z_MAX INTEGER);
            CREATE TABLE {table} (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
                ZKIND INTEGER, ZKINDSUBTYPE INTEGER, ZHIDDEN INTEGER, ZFAVORITE INTEGER,
                ZTRASHEDSTATE INTEGER, ZWIDTH INTEGER, ZHEIGHT INTEGER,
                ZORIENTATION INTEGER, ZADDITIONALATTRIBUTES INTEGER,
                {"ZEXTENDEDATTRIBUTES INTEGER," if extended else ""}
                ZDATECREATED TIMESTAMP, ZADDEDDATE TIMESTAMP,
                ZMODIFICATIONDATE TIMESTAMP, ZTRASHEDDATE TIMESTAMP, ZDURATION FLOAT,
                ZLATITUDE FLOAT, ZLONGITUDE FLOAT, ZDIRECTORY VARCHAR,
                ZFILENAME VARCHAR, ZUNIFORMTYPEIDENTIFIER VARCHAR, ZUUID VARCHAR,
                ZCLOUDASSETGUID VARCHAR);
            CREATE TABLE ZADDITIONALASSETATTRIBUTES (Z_PK INTEGER PRIMARY KEY,
                Z_ENT INTEGER, Z_OPT INTEGER, ZASSET INTEGER, ZASSETDESCRIPTION INTEGER,
                ZTIMEZONEOFFSET INTEGER, ZORIGINALFILESIZE INTEGER,
                ZORIGINALWIDTH INTEGER, ZORIGINALHEIGHT INTEGER,
                ZORIGINALFILENAME VARCHAR, ZTIMEZONENAME VARCHAR, ZTITLE VARCHAR,
                ZEXIFTIMESTAMPSTRING VARCHAR);
            CREATE TABLE ZASSETDESCRIPTION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER,
                Z_OPT INTEGER, ZASSETATTRIBUTES INTEGER, ZLONGDESCRIPTION VARCHAR);
        """)
        if extended:
            conn.execute("""
                CREATE TABLE ZEXTENDEDATTRIBUTES (Z_PK INTEGER PRIMARY KEY,
                    Z_ENT INTEGER, Z_OPT INTEGER, ZASSET INTEGER, ZCAMERAMAKE VARCHAR,
                    ZCAMERAMODEL VARCHAR, ZLENSMODEL VARCHAR, ZLATITUDE FLOAT,
                    ZLONGITUDE FLOAT, ZTIMEZONEOFFSET INTEGER, ZTIMEZONENAME VARCHAR,
                    ZDATECREATED TIMESTAMP)""")
        conn.execute("INSERT INTO Z_METADATA VALUES (1, ?, ?)", (library_uuid, b""))
        conn.executemany("INSERT INTO Z_PRIMARYKEY VALUES (?, ?, 0, ?)", [
            (3, "Asset", len(assets)), (1, "AdditionalAssetAttributes", len(assets))])
        descriptions = 0
        for pk, a in enumerate(assets, start=1):
            created = a["created"]
            offset = int(created.utcoffset().total_seconds())
            ext = a.get("ext", "heic")
            no_fix = a.get("lat") is None or a.get("lon") is None
            lat = -180.0 if no_fix else a["lat"]
            lon = -180.0 if no_fix else a["lon"]
            modified = a.get("modified") or created + timedelta(minutes=7)
            columns = {
                "Z_PK": pk, "Z_ENT": 3, "Z_OPT": 4, "ZKIND": a.get("kind", 0),
                "ZKINDSUBTYPE": a.get("subtype", 0), "ZHIDDEN": int(bool(a.get("hidden"))),
                "ZFAVORITE": int(bool(a.get("favorite"))),
                "ZTRASHEDSTATE": int(bool(a.get("trashed"))),
                "ZWIDTH": a.get("width", 4032), "ZHEIGHT": a.get("height", 3024),
                "ZORIENTATION": 1, "ZADDITIONALATTRIBUTES": pk,
                "ZDATECREATED": _core_data(created),
                "ZADDEDDATE": _core_data(created + timedelta(seconds=40)),
                "ZMODIFICATIONDATE": _core_data(modified),
                "ZTRASHEDDATE": _core_data(modified) if a.get("trashed") else None,
                "ZDURATION": a.get("duration", 0.0),
                "ZLATITUDE": lat, "ZLONGITUDE": lon,
                "ZDIRECTORY": a["uuid"][0], "ZFILENAME": f"{a['uuid']}.{ext}",
                "ZUNIFORMTYPEIDENTIFIER": a.get("uti", "public.heic"),
                "ZUUID": a["uuid"], "ZCLOUDASSETGUID": None,
            }
            if extended:
                columns["ZEXTENDEDATTRIBUTES"] = pk
            conn.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
                         f"({', '.join('?' for _ in columns)})", tuple(columns.values()))
            description_pk = None
            if a.get("caption"):
                descriptions += 1
                description_pk = descriptions
                conn.execute("INSERT INTO ZASSETDESCRIPTION VALUES (?, 5, 1, ?, ?)",
                             (description_pk, pk, a["caption"]))
            conn.execute(
                "INSERT INTO ZADDITIONALASSETATTRIBUTES (Z_PK, Z_ENT, Z_OPT, ZASSET, "
                "ZASSETDESCRIPTION, ZTIMEZONEOFFSET, ZORIGINALFILESIZE, ZORIGINALWIDTH, "
                "ZORIGINALHEIGHT, ZORIGINALFILENAME, ZTIMEZONENAME, ZTITLE, "
                "ZEXIFTIMESTAMPSTRING) VALUES (?, 1, 3, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pk, pk, description_pk, offset, 2_400_000 + pk * 7919,
                 a.get("width", 4032), a.get("height", 3024),
                 a.get("original_filename") or f"IMG_{pk:04d}.{ext.upper()}",
                 a.get("tz_name") or str(created.tzinfo), a.get("title"),
                 created.strftime("%Y:%m:%d %H:%M:%S")))
            if extended:
                conn.execute(
                    "INSERT INTO ZEXTENDEDATTRIBUTES (Z_PK, Z_ENT, Z_OPT, ZASSET, "
                    "ZCAMERAMAKE, ZCAMERAMODEL, ZLATITUDE, ZLONGITUDE, ZTIMEZONEOFFSET, "
                    "ZTIMEZONENAME, ZDATECREATED) VALUES (?, 2, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pk, pk, a.get("make"), a.get("model"), None if no_fix else lat,
                     None if no_fix else lon, offset, a.get("tz_name"),
                     _core_data(created)))
        conn.commit()
    finally:
        conn.close()
    return db


# ---------------------------------------------------------------- EXIF writer

_ASCII, _BYTE, _SHORT, _LONG, _RATIONAL, _UNDEFINED = 2, 1, 3, 4, 5, 7


def _encode(typ: int, value, e: str) -> tuple[bytes, int]:
    if typ == _ASCII:
        raw = value.encode("utf-8") + b"\x00"
        return raw, len(raw)
    if typ in (_BYTE, _UNDEFINED):
        return bytes(value), len(value)
    if typ == _RATIONAL:
        flat = [x for pair in value for x in pair]
        return struct.pack(e + "I" * len(flat), *flat), len(value)
    code = {_SHORT: "H", _LONG: "I"}[typ]
    return struct.pack(e + code * len(value), *value), len(value)


def _ifd(entries: list[tuple], start: int, e: str) -> bytes:
    """One IFD at `start` (an offset from the TIFF header): the entry table,
    sorted by tag as TIFF requires, then the values too big to sit inline."""
    entries = sorted(entries, key=lambda x: x[0])
    data_at = start + 2 + 12 * len(entries) + 4
    table, area = bytearray(struct.pack(e + "H", len(entries))), bytearray()
    for tag, typ, value in entries:
        raw, count = _encode(typ, value, e)
        if len(raw) <= 4:
            table += struct.pack(e + "HHI", tag, typ, count) + raw.ljust(4, b"\x00")
        else:
            table += struct.pack(e + "HHII", tag, typ, count, data_at + len(area))
            area += raw + (b"\x00" if len(raw) % 2 else b"")
    table += struct.pack(e + "I", 0)
    return bytes(table + area)


def _dms(value: float) -> list[tuple[int, int]]:
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = (value - degrees - minutes / 60) * 3600
    return [(degrees, 1), (minutes, 1), (int(round(seconds * 10000)), 10000)]


def exif_tiff(*, make: str | None = None, model: str | None = None,
              taken: datetime | None = None, offset: str | None = None,
              subsec: str | None = None, width: int | None = None,
              height: int | None = None, orientation: int | None = None,
              lat: float | None = None, lon: float | None = None,
              alt: float | None = None, gps_utc: datetime | None = None,
              description: str | None = None, little_endian: bool = True) -> bytes:
    """A TIFF structure as a camera writes it into JPEG APP1 / HEIF 'Exif':
    IFD0 (who made it), the Exif IFD (when, how big), the GPS IFD (where).
    `taken` is the local wall-clock time; its zone is only what `offset` says."""
    e = "<" if little_endian else ">"
    ifd0 = [(0x011A, _RATIONAL, [(72, 1)]), (0x011B, _RATIONAL, [(72, 1)]),
            (0x0128, _SHORT, [2]), (0x0213, _SHORT, [1])]
    if description is not None:
        ifd0.append((0x010E, _ASCII, description))
    if make:
        ifd0.append((0x010F, _ASCII, make))
    if model:
        ifd0.append((0x0110, _ASCII, model))
    if orientation:
        ifd0.append((0x0112, _SHORT, [orientation]))
    stamp = taken.strftime("%Y:%m:%d %H:%M:%S") if taken else None
    if stamp:
        ifd0.append((0x0132, _ASCII, stamp))

    exif = [(0x9000, _UNDEFINED, b"0231"), (0xA001, _SHORT, [1])]
    if stamp:
        exif += [(0x9003, _ASCII, stamp), (0x9004, _ASCII, stamp)]
    if offset:
        exif += [(0x9010, _ASCII, offset), (0x9011, _ASCII, offset),
                 (0x9012, _ASCII, offset)]
    if subsec:
        exif += [(0x9290, _ASCII, subsec), (0x9291, _ASCII, subsec)]
    if width and height:
        exif += [(0xA002, _LONG, [width]), (0xA003, _LONG, [height])]

    gps = []
    if lat is not None and lon is not None:
        gps += [(0x0000, _BYTE, b"\x02\x02\x00\x00"),
                (0x0001, _ASCII, "N" if lat >= 0 else "S"), (0x0002, _RATIONAL, _dms(lat)),
                (0x0003, _ASCII, "E" if lon >= 0 else "W"), (0x0004, _RATIONAL, _dms(lon))]
        if alt is not None:
            gps += [(0x0005, _BYTE, b"\x01" if alt < 0 else b"\x00"),
                    (0x0006, _RATIONAL, [(int(round(abs(alt) * 100)), 100)])]
    if gps_utc is not None:
        u = gps_utc.astimezone(timezone.utc)
        gps += [(0x0007, _RATIONAL, [(u.hour, 1), (u.minute, 1), (u.second, 1)]),
                (0x001D, _ASCII, u.strftime("%Y:%m:%d"))]

    def build(exif_at: int, gps_at: int) -> bytes:
        entries = ifd0 + [(0x8769, _LONG, [exif_at])]
        if gps:
            entries.append((0x8825, _LONG, [gps_at]))
        return _ifd(entries, 8, e)

    first = build(0, 0)                       # pointer values do not change its size
    exif_at = 8 + len(first)
    exif_block = _ifd(exif, exif_at, e)
    gps_at = exif_at + len(exif_block)
    gps_block = _ifd(gps, gps_at, e) if gps else b""
    header = (b"II" if little_endian else b"MM") + struct.pack(e + "HI", 42, 8)
    return header + build(exif_at, gps_at) + exif_block + gps_block


# ---------------------------------------------------------------- JPEG writer

_DC_BITS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)     # ITU T.81 Annex K.3
_DC_VALUES = tuple(range(12))


def _codes(bits, values) -> dict[int, tuple[int, int]]:
    codes, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[values[k]] = (code, length)
            code, k = code + 1, k + 1
        code <<= 1
    return codes


_DC_CODES = _codes(_DC_BITS, _DC_VALUES)


class _Bits:
    def __init__(self):
        self.acc, self.n, self.out = 0, 0, bytearray()

    def put(self, code: int, length: int) -> None:
        self.acc = (self.acc << length) | code
        self.n += length
        while self.n >= 8:
            self.n -= 8
            byte = (self.acc >> self.n) & 0xFF
            self.out.append(byte)
            if byte == 0xFF:
                self.out.append(0)                # byte stuffing
        self.acc &= (1 << self.n) - 1

    def flush(self) -> bytes:
        if self.n:
            self.put((1 << (8 - self.n)) - 1, 8 - self.n)
        return bytes(self.out)


def _ycbcr(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = rgb
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return tuple(max(0, min(255, int(round(v)))) for v in (y, cb, cr))


def _segment(marker: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload


def jpeg_bytes(width: int, height: int, bands: list[tuple[int, int, int]],
               exif: bytes | None = None) -> bytes:
    """A baseline JPEG of horizontal colour bands, `width` x `height` (both
    multiples of 8). With `exif`, an APP1 Exif segment comes first, as a
    camera writes it; without, a JFIF APP0, as a screenshot tool does."""
    if width % 8 or height % 8 or not bands:
        raise ValueError("jpeg_bytes needs a size in whole 8x8 blocks and a colour")
    blocks_x, blocks_y = width // 8, height // 8
    out = bytearray(b"\xff\xd8")
    if exif is not None:
        out += _segment(0xE1, b"Exif\x00\x00" + exif)
    else:
        out += _segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    out += _segment(0xDB, b"\x00" + bytes([1] * 64))          # quantize by 1: exact DCs
    out += _segment(0xC0, struct.pack(">BHHB", 8, height, width, 3)
                    + b"".join(bytes([c, 0x11, 0]) for c in (1, 2, 3)))
    out += _segment(0xC4, b"\x00" + bytes(_DC_BITS) + bytes(_DC_VALUES)
                    + b"\x10" + bytes([1] + [0] * 15) + b"\x00")   # AC: EOB only, code '0'
    out += _segment(0xDA, b"\x03\x01\x00\x02\x00\x03\x00\x00\x3f\x00")

    bits, previous = _Bits(), [0, 0, 0]
    for by in range(blocks_y):
        band = _ycbcr(bands[min(len(bands) - 1, by * len(bands) // blocks_y)])
        for _ in range(blocks_x):
            for c in range(3):
                dc = 8 * (band[c] - 128)
                diff, previous[c] = dc - previous[c], dc
                size = abs(diff).bit_length()
                bits.put(*_DC_CODES[size])
                if size:
                    bits.put(diff if diff > 0 else diff + (1 << size) - 1, size)
                bits.put(0, 1)                                   # EOB
    out += bits.flush() + b"\xff\xd9"
    return bytes(out)

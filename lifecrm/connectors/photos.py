"""Photos connector: when and where each photo was taken — metadata only.

Photos are the cheapest strong evidence of where a life happened. "The lake
I went to in Slovenia four years ago" is a GPS position and a date on a few
dozen files; nobody has to have written anything down. This connector reads
that metadata — capture time, UTC offset, GPS, camera, pixel size — and never
the pixels: no image is decoded, no thumbnail is made, and media never
enters the database. `media_path` says where the file is.

Accepted targets
----------------
- a folder of photos and videos, read recursively: a OneDrive camera roll, a
  Samsung Gallery sync, an SD card's DCIM. Each file's own metadata is read
  by `_exif.py` (JPEG, HEIC/HEIF, AVIF, PNG, WebP, TIFF/DNG, MP4/MOV).
- a Google Photos Takeout: the .zip, the extracted folder, or a folder of
  'takeout-*.zip' parts (a sidecar often lands in a different part than its
  photo). The JSON sidecar beside each file supplies the capture instant,
  the location as edited in Google Photos, and the description; the file's
  EXIF supplies the camera and the zone.
- a Mac Photos library: the .photoslibrary package or its
  database/Photos.sqlite, copied through SQLite's backup API — never read in
  place, since Photos may be running.
- nothing at all: `default_target` picks the Mac's Photos library, or on
  Windows the one OneDrive camera folder ('Pictures/Camera Roll' or
  'Pictures/Samsung Gallery'); `[connectors.photos] path` overrides both.

What comes in
-------------
One observation per photo or video, in stream `photo`:

  title           the file name (the original name, for a library or Takeout)
  body            the caption, where the source has one
  started_at      the capture instant, UTC; for a video ended_at adds the duration
  utc_offset_sec  the zone the camera was in, when known — a photo taken in
                  Slovenia keeps +02:00
  value_json      kind (photo | video | screenshot), time_source, lat, lon,
                  alt, make, model, width, height, orientation, duration_sec,
                  favorite, hidden, albums, people
  media_path      absolute, for a folder; relative to the library package, for
                  a Photos library; relative to the archive ('Google Photos/
                  ...'), for a Takeout, so a zip and its extraction are the
                  same records
  origin_app      'apple_photos', 'google_photos', or a folder's camera make

No participants and no handles: a photo names no phone number or account.
Google Photos' face labels (`people`) are names the owner typed into Google
Photos. They ride along in value_json the way a caption does, and never
become handles, participants or entity links. Turning a position into a
place name is a derived layer, not import-time work.

Time
----
`time_source` records where each capture time came from, best first:

  library     Photos' own capture date and zone
  takeout     Google's capture instant; the zone from the file's EXIF
  exif        the camera's local time and its OffsetTimeOriginal
  exif_gps    the camera's local time; the zone inferred from its GPS clock
  exif_local  the camera's local time, zone unknown: read in the home zone
  quicktime   a video header: UTC, plus the zone where Apple wrote one
  filename    an Android camera name — '20260727_213005.jpg' or
              'IMG_20260727_213005.jpg' (local), 'PXL_20260727_213005123.jpg'
              (a Pixel names files in UTC)
  mtime       the file's modification time: the weakest, a copy may reset it

The journal date is the **local capture date** where the zone is known, with
the configured rollover hour applied in that zone: a photo taken at 09:00 in
Slovenia belongs to that Slovenian morning, not to the Austin night before.
Where the zone is unknown the configured home timezone decides, as it does
for every other connector.

Identity, and what a re-import does
-----------------------------------
  Photos library  'apple-photos:<ZUUID>'. The archive mirrors the library
                  (the owner's rule for sources that hold a whole history,
                  2026-09-24): a photo trashed or deleted in Photos is removed
                  by the next import, scoped to that library — its Core Data
                  store UUID is the account. A hidden photo is still in the
                  library: skipped unless `include_hidden = true`, never
                  removed.
  Takeout         'takeout:<path under Google Photos/>', taken from the copy in
                  'Photos from YYYY' (or its wording in the Takeout's
                  language: 'Fotos von YYYY', ...); album copies of one
                  photo are one record that lists its albums.
  folder          'file:<path inside the folder>|<size in bytes>'.
  Folders and Takeouts are remembered, not mirrored: a folder is often a
  partial copy, and a file missing from it proves nothing.

Volume
------
Libraries run to 100k photos. Files are listed, never loaded: each read seeks
to the metadata and stops (at most 256 KB per file, usually a few KB), and
counts per year are reported so a wrong date range is visible before
--apply. Files OneDrive or iCloud has not downloaded are skipped and counted —
opening one would download it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import _abcddb, _exif, base

name = "photos"
streams = ("photo",)

PHOTO_EXT = {".jpg", ".jpeg", ".jpe", ".heic", ".heif", ".hif", ".avif", ".png",
             ".webp", ".gif", ".bmp", ".tif", ".tiff", ".dng", ".cr2", ".nef", ".arw"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".avi", ".mkv", ".webm",
             ".mts", ".m2ts", ".wmv"}
# A zip member is decompressed to reach any offset; past this, give up on it.
ZIP_SEEK_LIMIT = 16 << 20

_ISO = "%Y-%m-%dT%H:%M:%SZ"
_MAX_OFFSET_SEC = 14 * 3600
_ONEDRIVE_FOLDERS = (("Pictures", "Camera Roll"), ("Pictures", "Samsung Gallery"))
_TAKEOUT_ROOTS = {"google photos", "google fotos", "google foto"}
# A Takeout made in another language names its year folders in that language.
# Listed by wording, not matched by shape: an album the owner called 'Bled
# 2022' is an album, and must keep its label. A wording missing from this list
# still gets a stable id (see _merge_album_copies) — only its label is wrong.
_YEAR_FOLDER = re.compile(
    r"^(?:Photos from|Fotos von|Fotos de|Photos de|Foto's van|Foto's uit|Foto del"
    r"|Foto dal|Zdjęcia z|Фото за) (?:19|20)\d{2}$", re.IGNORECASE)
# The suffix Google adds to the copy of a photo edited in Google Photos, by
# the language the Takeout was made in (as GooglePhotosTakeoutHelper lists
# them). The original is imported; the edited copy is the same photo.
_EDITED_SUFFIXES = ("-edited", "-bearbeitet", "-bewerkt", "-modifié", "-modificato",
                    "-ha editado", "-editat", "-edytowane", "-編集済み")
_YEAR_AT_END = re.compile(r"(?<!\d)((?:19|20)\d{2})$")
_PIXEL_NAME = re.compile(r"^PXL_(\d{8})_(\d{6})", re.IGNORECASE)
_STAMP_NAME = re.compile(r"(?<!\d)((?:19|20)\d{6})[_-](\d{6})(?!\d)")
_DUP = re.compile(r"^(.*)\((\d+)\)$")
_SUPPLEMENTAL = ".supplemental-metadata"
_FULL_DISK = ("grant Full Disk Access to the app running this (System Settings > "
              "Privacy & Security > Full Disk Access), then run it again")

# Windows: a OneDrive Files-On-Demand placeholder; macOS: a dataless file
# (File Provider). Either one is downloaded in full by reading a byte of it.
_RECALL_ON_DATA_ACCESS, _OFFLINE, _DATALESS = 0x00400000, 0x00001000, 0x40000000

_CLOUD_ONLY = "cloud-only file not on this device (not opened: that would download it)"
_UNREADABLE = "file that could not be read"


# ---------------------------------------------------------------- finding the photos

def default_target(cfg: Config) -> Path:
    """Where to read when no path is given: `[connectors.photos] path`, else
    the Mac's Photos library, else the one OneDrive camera folder — never a
    guess between two."""
    configured = cfg.connector_settings(name).get("path")
    if configured:
        return cfg.resolve(configured)
    if platforms.is_mac():
        db = platforms.mac_photos_db()
        try:
            found = db.exists()
        except PermissionError as e:
            raise DomainError(f"cannot read {db.parent.parent} — {_FULL_DISK}") from e
        if found:
            return db.parent.parent
        raise DomainError(f"no Photos library at {db.parent.parent} — pass the library "
                          f"or a folder of photos, or set [connectors.photos] path")
    roots = platforms.onedrive_candidates()
    found = []
    for root in roots:
        for parts in _ONEDRIVE_FOLDERS:
            p = root.joinpath(*parts)
            if p.is_dir() and p not in found:
                found.append(p)
    if len(found) == 1:
        return found[0]
    if not found:
        raise DomainError(
            "no camera folder found — looked for Pictures/Camera Roll and "
            "Pictures/Samsung Gallery under " + ", ".join(str(r) for r in roots)
            + "; pass the folder, or set [connectors.photos] path in config.toml")
    raise base.Ambiguous(f"{len(found)} camera folders found — pass one of them (they "
                      f"often hold the same photos): " + "; ".join(str(p) for p in found))


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    target = Path(target)
    try:
        return _scan(target, cfg)
    except PermissionError as e:
        where = e.filename or target
        hint = f" — {_FULL_DISK}" if platforms.is_mac() else ""
        raise DomainError(f"cannot read {where}: permission denied{hint}") from e


def _scan(target: Path, cfg: Config) -> base.Scan:
    if target.is_dir():
        db = target / "database" / "Photos.sqlite"
        if target.suffix.lower() == ".photoslibrary" or db.exists():
            if not db.exists():
                raise DomainError(f"{target.name} has no database/Photos.sqlite — is it "
                                  f"only partly copied, or never opened in Photos?")
            return _scan_library(db, cfg)
        return _scan_tree(target, cfg, "folder")
    if not target.is_file():
        raise DomainError(f"not found: {target}")
    with open(target, "rb") as fh:
        head = fh.read(16)
    if head.startswith(b"SQLite format 3\x00"):
        return _scan_library(target, cfg)
    if _ext(target.name) in PHOTO_EXT | VIDEO_EXT:
        return _scan_tree(target, cfg, "file")
    if zipfile.is_zipfile(target):
        return _scan_tree(target, cfg, "zip")
    raise DomainError(f"{target.name} is not a photo, a video, a folder, a .zip "
                      f"or a Photos library")


def _finish(records: list[dict], skipped: Counter, **kw) -> base.Scan:
    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    years: Counter = Counter(r["journal_date"][:4] for r in records)
    details = [json.loads(r["value_json"]) for r in records]
    facts = {**kw.pop("facts", {}),
             "years": dict(sorted(years.items())),
             "time_sources": dict(Counter(d.get("time_source") for d in details)),
             "kinds": dict(Counter(d.get("kind") for d in details)),
             "with_gps": sum(1 for d in details if "lat" in d)}
    return base.empty_scan(
        name, records=records, facts=facts,
        skipped=[f"{n} × {why}" for why, n in sorted(skipped.items())], **kw)


# ---------------------------------------------------------------- time

def _iso(epoch: float) -> str | None:
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(_ISO)
    except (OverflowError, OSError, ValueError):
        return None


def _from_local(local: str, offset: int) -> str:
    return (datetime.fromisoformat(local) - timedelta(seconds=offset)).strftime(_ISO)


def _in_zone(local: str, zone: str) -> str:
    dt = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(zone))
    return dt.astimezone(timezone.utc).strftime(_ISO)


def _offset_between(local: str, utc: str) -> int | None:
    """The zone a wall-clock time was in, given the same moment in UTC — to
    the quarter hour, and only when the two clocks agree to two minutes (a
    GPS fix can be stale; a guessed zone would be worse than none)."""
    diff = (datetime.fromisoformat(local)
            - datetime.strptime(utc, _ISO)).total_seconds()
    offset = int(round(diff / 900)) * 900
    if abs(diff - offset) > 120 or abs(offset) > _MAX_OFFSET_SEC:
        return None
    return offset


def _filename_time(fname: str, cfg: Config) -> str | None:
    pixel = _PIXEL_NAME.match(fname)
    m = pixel or _STAMP_NAME.search(fname)
    if not m:
        return None
    try:
        dt = datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if not 1990 <= dt.year <= 2100:
        return None
    zone = timezone.utc if pixel else ZoneInfo(cfg.timezone)
    return dt.replace(tzinfo=zone).astimezone(timezone.utc).strftime(_ISO)


def _journal_date(started: str, offset: int | None, cfg: Config) -> str:
    """The local capture date where the zone is known (rollover applied in
    that zone); the home-timezone rule otherwise."""
    if offset is None:
        return base.journal_date_for_iso(started, cfg)
    local = datetime.strptime(started, _ISO) + timedelta(seconds=offset)
    if local.hour < cfg.day_rollover_hour:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def _compact(detail: dict) -> dict:
    return {k: v for k, v in detail.items()
            if v is not None and v is not False and v != [] and v != ""}


def _record(*, external_id: str, started: str, offset: int | None, cfg: Config,
            title: str, body: str | None, detail: dict, media_path: str | None,
            origin_app: str | None, last_modified: str | None) -> dict:
    ended = None
    duration = detail.get("duration_sec")
    if isinstance(duration, (int, float)) and duration > 0:
        ended = (datetime.strptime(started, _ISO)
                 + timedelta(seconds=int(duration))).strftime(_ISO)
    return {
        "stream": "photo",
        "external_id": external_id,
        "journal_date": _journal_date(started, offset, cfg),
        "started_at": started,
        "ended_at": ended,
        "utc_offset_sec": offset,
        "direction": None,
        "title": title,
        "body": (body or "").strip() or None,
        "value": None,
        "unit": None,
        "value_json": canonical_json(_compact(detail)),
        "media_path": media_path,
        "origin_app": origin_app,
        "last_modified_at": last_modified or started,
        "participants": [],
    }


# ---------------------------------------------------------------- files: listing

@dataclass
class _File:
    rel: str                         # POSIX path inside the target
    size: int
    mtime: str | None                # UTC
    opener: Callable
    path: Path | None = None         # the file on disk, when there is one
    zipped: bool = False
    cloud: bool = False

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def parent(self) -> str:
        return self.rel.rsplit("/", 1)[0] if "/" in self.rel else ""


def _ext(fname: str) -> str:
    return os.path.splitext(fname)[1].lower()


def _stem(fname: str) -> str:
    return os.path.splitext(fname)[0]


def _fold(name: str) -> str:
    """A file name compared as a name: one case, and one Unicode form — a Mac
    writes 'modifié' decomposed, Windows composed."""
    return unicodedata.normalize("NFC", name).casefold()


def _edited_original(stem: str) -> str | None:
    """The folded stem of the photo an edited copy was made from
    ('IMG_1-bearbeitet' -> 'img_1'), or None when it is not an edited copy."""
    folded = _fold(stem)
    for suffix in _EDITED_SUFFIXES:
        if folded.endswith(suffix) and len(folded) > len(suffix):
            return folded[:-len(suffix)]
    return None


def _cloud_only(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0) or 0
    if attrs & (_RECALL_ON_DATA_ACCESS | _OFFLINE):
        return True
    return bool((getattr(st, "st_flags", 0) or 0) & _DATALESS)


def _list_folder(root: Path, skipped: Counter, archives: list[Path]) -> list[_File]:
    files = []

    def failed(err: OSError) -> None:
        if err.filename and Path(err.filename) == root:
            raise err
        skipped["folder that could not be read"] += 1

    for dirpath, dirnames, filenames in os.walk(root, onerror=failed):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if d.startswith(".") or d == "__MACOSX":
                skipped["hidden folder (not read)"] += 1
            elif d.lower().endswith(".photoslibrary"):
                skipped["Photos library inside the folder (import the library itself)"] += 1
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            p = here / fn
            if platforms.is_icloud_placeholder(p):
                skipped["iCloud placeholder (not downloaded to this Mac)"] += 1
                continue
            if fn.startswith("."):
                skipped["hidden file"] += 1
                continue
            if here == root and fn.lower().startswith("takeout") \
                    and fn.lower().endswith(".zip"):
                archives.append(p)
                continue
            try:
                st = p.stat()
            except OSError:
                skipped[_UNREADABLE] += 1
                continue
            files.append(_File(p.relative_to(root).as_posix(), st.st_size,
                               _iso(st.st_mtime), (lambda p=p: open(p, "rb")), p,
                               cloud=_cloud_only(st)))
    return files


def _list_zip(zf: zipfile.ZipFile, cfg: Config, skipped: Counter) -> list[_File]:
    zone = ZoneInfo(cfg.timezone)
    out = []
    for info in sorted(zf.infolist(), key=lambda i: i.filename):
        if info.is_dir():
            continue
        rel = info.filename.replace("\\", "/").lstrip("/")
        if any(part.startswith(".") or part == "__MACOSX" for part in rel.split("/")):
            skipped["hidden file"] += 1
            continue
        try:
            # A zip stores local wall-clock times with no zone.
            mtime = datetime(*info.date_time, tzinfo=zone).astimezone(
                timezone.utc).strftime(_ISO)
        except ValueError:
            mtime = None
        out.append(_File(rel, info.file_size, mtime, (lambda i=info: zf.open(i)),
                         zipped=True))
    return out


def _tree_hash(files: list[_File]) -> str:
    """Over the listing, not the bytes: hashing a 300 GB camera roll to learn
    it is unchanged would cost more than reading it."""
    h = hashlib.sha256()
    for f in sorted(files, key=lambda f: f.rel):
        h.update(f"{f.rel}|{f.size}|{f.mtime}\n".encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------- files: Takeout sidecars

def _is_album_json(fname: str) -> bool:
    return re.match(r"^metadata(\(\d+\))?\.json$", fname, re.IGNORECASE) is not None


class _Sidecars:
    """Google's sidecar name for each photo in one folder.

    'IMG_1.jpg.json' (older exports) and 'IMG_1.jpg.supplemental-metadata.json'
    (2024+), either one cut short to fit 51 characters
    ('IMG_1.jpg.supplemental-metad.json', 'A long file name.j.json'), and for
    a duplicate 'IMG_1(1).jpg' the '(1)' goes last ('IMG_1.jpg(1).json',
    'IMG_1.jpg.supplemental-metadata(1).json'). Indexed once per folder:
    a 'Photos from 2022' folder holds thousands of pairs.
    """

    def __init__(self, names):
        self.exact: dict[tuple[str, str], tuple[int, str]] = {}
        self.cut: dict[tuple[str, str], str] = {}
        self.cut_lengths: set[int] = set()
        for n in sorted(names):
            if not n.lower().endswith(".json") or _is_album_json(n):
                continue
            core = n[:-5]
            m = _DUP.match(core)
            core, dup = (m[1], m[2]) if m else (core, "")
            stripped = None
            for k in range(max(1, len(core) - len(_SUPPLEMENTAL)), len(core)):
                if core[k] == "." and _SUPPLEMENTAL.startswith(core[k:]):
                    stripped = core[:k]
                    break
            if stripped is not None:
                self._put((stripped, dup), 0 if core.endswith(_SUPPLEMENTAL) else 1, n)
            else:
                self._put((core, dup), 0, n)
            if len(core) >= 40:                   # long enough to have been cut
                self.cut.setdefault((core, dup), n)
                self.cut_lengths.add(len(core))

    def _put(self, key, rank: int, n: str) -> None:
        if key not in self.exact or rank < self.exact[key][0]:
            self.exact[key] = (rank, n)

    def find(self, media: str) -> str | None:
        stem, ext = os.path.splitext(media)
        m = _DUP.match(stem)
        tries = ([(m[1] + ext, m[2])] if m else []) + [(media, "")]
        for key in tries:
            if key in self.exact:
                return self.exact[key][1]
        for base_name, dup in tries:
            for length in sorted(self.cut_lengths, reverse=True):
                if length < len(base_name) and (base_name[:length], dup) in self.cut:
                    return self.cut[(base_name[:length], dup)]
        return None


def _stamp(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("timestamp")
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return base.iso_utc(n) if n > 0 else None


def _geo(value) -> tuple | None:
    """(lat, lon, alt) from a sidecar geo block; 0,0 is Google's 'unknown',
    and so is an altitude of exactly 0."""
    if not isinstance(value, dict):
        return None
    try:
        lat, lon = float(value.get("latitude")), float(value.get("longitude"))
    except (TypeError, ValueError):
        return None
    if (lat, lon) == (0, 0) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    try:
        alt = float(value.get("altitude"))
    except (TypeError, ValueError):
        alt = 0.0
    return round(lat, 6), round(lon, 6), (round(alt, 1) if alt else None)


def _sidecar(f: _File, skipped: Counter) -> dict | None:
    try:
        with f.opener() as fh:
            raw = fh.read(1 << 20)
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, zipfile.BadZipFile, zlib.error, RuntimeError,
            UnicodeDecodeError, ValueError):
        skipped["sidecar JSON that could not be read (the file's own metadata used)"] += 1
        return None
    if not isinstance(data, dict) or not (
            {"photoTakenTime", "creationTime", "geoData", "geoDataExif"} & data.keys()):
        skipped["JSON beside a photo that is not a Google Photos sidecar"] += 1
        return None
    people = sorted({p["name"].strip() for p in data.get("people") or []
                     if isinstance(p, dict) and isinstance(p.get("name"), str)
                     and p["name"].strip()})
    title = data.get("title")
    description = data.get("description")
    return {
        "title": title.strip() if isinstance(title, str) and title.strip() else None,
        "description": description if isinstance(description, str) else None,
        "taken_utc": _stamp(data.get("photoTakenTime")),
        "created": _stamp(data.get("creationTime")),
        "modified": _stamp(data.get("photoLastModifiedTime")),
        "geo": _geo(data.get("geoData")) or _geo(data.get("geoDataExif")),
        "favorited": data.get("favorited") is True,
        "trashed": data.get("trashed") is True,
        "people": people,
    }


def _takeout_path(rel: str) -> tuple[str, str] | None:
    """(id path under 'Google Photos/', archive-relative media path), when
    the file sits inside a Takeout's Google Photos folder."""
    parts = rel.split("/")
    for i, part in enumerate(parts[:-1]):
        if part.lower() in _TAKEOUT_ROOTS:
            return "/".join(parts[i + 1:]), "/".join(parts[i:])
    return None


# ---------------------------------------------------------------- files: records

def _when(meta: dict, side: dict | None, f: _File,
          cfg: Config) -> tuple[str, int | None, str] | None:
    """(started_at, utc_offset_sec, time_source); see the module docstring."""
    local = meta.get("taken_local")
    if side and side.get("taken_utc"):
        utc = side["taken_utc"]
        offset = meta.get("offset_sec")
        if offset is None and local:
            offset = _offset_between(local, utc)
        return utc, offset, "takeout"
    if local:
        offset = meta.get("offset_sec")
        source = "quicktime" if meta.get("format") == "quicktime" else "exif"
        if offset is not None:
            return _from_local(local, offset), offset, source
        if meta.get("gps_utc"):
            offset = _offset_between(local, meta["gps_utc"])
            if offset is not None:
                return _from_local(local, offset), offset, "exif_gps"
        return _in_zone(local, cfg.timezone), None, "exif_local"
    if meta.get("taken_utc"):
        return meta["taken_utc"], None, "quicktime"
    named = _filename_time(f.name, cfg)
    if named:
        return named, None, "filename"
    if f.mtime:
        return f.mtime, None, "mtime"
    return None


def _read_meta(f: _File) -> dict | None:
    try:
        with f.opener() as fh:
            return _exif.read(fh, f.size, limit=ZIP_SEEK_LIMIT if f.zipped else None)
    except (OSError, zipfile.BadZipFile, zlib.error, EOFError, RuntimeError,
            NotImplementedError):
        return None


def _is_screenshot(rel: str) -> bool:
    parts = rel.lower().split("/")
    return parts[-1].startswith("screenshot") or "screenshots" in parts[:-1]


def _scan_tree(target: Path, cfg: Config, kind: str) -> base.Scan:
    skipped: Counter = Counter()
    archives: list[Path] = []
    opened: list[zipfile.ZipFile] = []
    try:
        if kind == "folder":
            files = _list_folder(target, skipped, archives)
        elif kind == "zip":
            files, archives = [], [target]
        else:
            st = target.stat()
            files = [_File(target.name, st.st_size, _iso(st.st_mtime),
                           (lambda: open(target, "rb")), target, cloud=_cloud_only(st))]
        seen = {f.rel for f in files}
        for z in archives:
            try:
                zf = zipfile.ZipFile(z)
            except (zipfile.BadZipFile, OSError) as e:
                raise DomainError(f"{z.name} is not a readable zip: {e}") from e
            opened.append(zf)
            for f in _list_zip(zf, cfg, skipped):
                if f.rel not in seen:
                    seen.add(f.rel)
                    files.append(f)
        result = _read_tree(files, cfg, skipped, kind)
    finally:
        for zf in opened:
            zf.close()

    if not result["media_seen"]:
        others = ", ".join(f"{ext} {n}" for ext, n in result["others"].most_common(6))
        raise DomainError(f"no photos or videos in {target.name} "
                          f"({len(files)} file(s) seen{': ' + others if others else ''})")
    if result["others"]:
        skipped["not a photo or video (" + ", ".join(
            f"{ext} {n}" for ext, n in sorted(result["others"].items())) + ")"] = \
            sum(result["others"].values())
    if result["orphans"]:
        skipped["Takeout sidecar with no photo beside it (a split Takeout? "
                "extract every part into one folder)"] = result["orphans"]
    target_kind = "takeout" if result["takeout"] else kind
    return _finish(result["records"], skipped,
                   source_file=target.name, source_hash=_tree_hash(files),
                   target_kind=target_kind, media_seen=result["media_seen"],
                   facts={"folder": str(target.resolve())})


def _read_tree(files: list[_File], cfg: Config, skipped: Counter, kind: str) -> dict:
    by_dir: dict[str, list[_File]] = {}
    for f in files:
        by_dir.setdefault(f.parent, []).append(f)
    others: Counter = Counter()
    records, takeout = [], []
    media_seen = orphans = 0
    for parent in sorted(by_dir):
        entries = by_dir[parent]
        sidecar_files = {f.name: f for f in entries if f.name.lower().endswith(".json")}
        sidecars = _Sidecars(sidecar_files)
        stills = {_fold(_stem(f.name)) for f in entries if _ext(f.name) in PHOTO_EXT}
        claimed: set[str] = set()
        for f in entries:
            ext = _ext(f.name)
            if ext == ".json":
                continue
            if ext not in PHOTO_EXT and ext not in VIDEO_EXT:
                others[ext or "(no extension)"] += 1
                continue
            media_seen += 1
            if f.cloud:
                skipped[_CLOUD_ONLY] += 1
                continue
            is_video = ext in VIDEO_EXT
            stem = _stem(f.name)
            if is_video and _fold(stem) in stills:
                skipped["Live Photo motion clip (its still is imported)"] += 1
                continue
            if _edited_original(stem) in stills:
                skipped["edited copy (the original is imported)"] += 1
                continue
            side = None
            side_name = sidecars.find(f.name)
            if side_name is not None:
                claimed.add(side_name)
                side_file = sidecar_files[side_name]
                if side_file.cloud:
                    skipped[_CLOUD_ONLY] += 1
                else:
                    side = _sidecar(side_file, skipped)
            if side and side["trashed"]:
                skipped["in Google Photos' trash"] += 1
                continue
            meta = _read_meta(f)
            if meta is None:
                skipped[_UNREADABLE] += 1
                continue
            rec = _file_record(f, meta, side, cfg, is_video)
            if rec is None:
                skipped["file with no capture time of any kind"] += 1
                continue
            if rec.pop("_takeout"):
                takeout.append(rec)
            else:
                records.append(rec)
        orphans += sum(1 for n in sidecar_files
                       if n not in claimed and not _is_album_json(n))
    records += _merge_album_copies(takeout, skipped)
    return {"records": records, "others": others, "media_seen": media_seen,
            "orphans": orphans, "takeout": bool(takeout)}


def _file_record(f: _File, meta: dict, side: dict | None, cfg: Config,
                 is_video: bool) -> dict | None:
    when = _when(meta, side, f, cfg)
    if when is None:
        return None
    started, offset, source = when
    detail = {
        "kind": "video" if is_video else "screenshot" if _is_screenshot(f.rel) else "photo",
        "time_source": source,
        "lat": meta.get("lat"), "lon": meta.get("lon"), "alt": meta.get("alt"),
        "make": meta.get("make"), "model": meta.get("model"),
        "width": meta.get("width"), "height": meta.get("height"),
        "orientation": meta.get("orientation"),
        "duration_sec": meta.get("duration_sec") if is_video else None,
    }
    in_takeout = _takeout_path(f.rel)
    if side is None and in_takeout is None:
        return {**_record(
            external_id=f"file:{f.rel}|{f.size}", started=started, offset=offset,
            cfg=cfg, title=f.name, body=meta.get("description"), detail=detail,
            media_path=str(f.path.resolve()) if f.path else f.rel,
            origin_app=(meta.get("make") or "").strip().lower() or None,
            last_modified=f.mtime), "_takeout": False}

    id_path, media_path = in_takeout or (f.rel, f.rel)
    if side:
        if side["geo"]:
            detail["lat"], detail["lon"], detail["alt"] = side["geo"]
        detail["favorite"] = side["favorited"]
        detail["people"] = side["people"]
    rec = _record(
        external_id=f"takeout:{id_path}", started=started, offset=offset, cfg=cfg,
        title=(side or {}).get("title") or f.name,
        body=(side or {}).get("description") or meta.get("description"),
        detail=detail, media_path=media_path, origin_app="google_photos",
        last_modified=(side or {}).get("modified") or (side or {}).get("created")
        or f.mtime)
    folder = f.parent.rsplit("/", 1)[-1] if f.parent else None
    rec["_album"] = None if not folder or folder.lower() in _TAKEOUT_ROOTS \
        or _YEAR_FOLDER.match(folder) else folder
    year = _YEAR_AT_END.search(folder or "")
    rec["_year_shaped"] = bool(year) and year[1] in {started[:4], rec["journal_date"][:4]}
    # One photo in three albums is three files with three sidecars that agree
    # on the original name and the capture instant.
    rec["_key"] = ((rec["title"], side["taken_utc"]) if side and side["taken_utc"]
                   else rec["external_id"])
    rec["_detail"] = detail
    rec["_takeout"] = True
    return rec


def _merge_album_copies(takeout: list[dict], skipped: Counter) -> list[dict]:
    """A Takeout repeats a photo in every album it is in. Keep one record —
    the 'Photos from YYYY' copy where there is one — listing the albums.

    Which copy is kept is the photo's id, so it must not depend on which
    albums the photo is in today. A year folder in a wording _YEAR_FOLDER
    does not list reads as an album; the copy in a folder ending in the
    photo's own year is still preferred, so taking the photo out of an album
    later does not give it a second id."""
    groups: dict = {}
    for rec in takeout:
        groups.setdefault(rec["_key"], []).append(rec)
    out = []
    for group in groups.values():
        group.sort(key=lambda r: (r["_album"] is not None, not r["_year_shaped"],
                                  len(r["external_id"]), r["external_id"]))
        keep = group[0]
        albums = sorted({r["_album"] for r in group if r["_album"]})
        if albums:
            detail = dict(keep["_detail"], albums=albums)
            keep["value_json"] = canonical_json(_compact(detail))
        if len(group) > 1:
            skipped["album copy of a photo already listed (its album is recorded)"] += \
                len(group) - 1
        for k in ("_album", "_key", "_detail", "_year_shaped"):
            keep.pop(k, None)
        out.append(keep)
    return out


# ---------------------------------------------------------------- Photos library

def _snapshot(db: Path, tmp: Path) -> sqlite3.Connection:
    """A private, consistent copy of Photos.sqlite, opened query-only.

    A live library has its -wal and -shm, and SQLite's backup API copies it
    consistently even while Photos writes. A library copied off another disk
    may carry a -wal without its -shm, which SQLite cannot open read-only —
    take both files as they are instead.
    """
    dest = tmp / "photos.db"
    wal, shm = db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")
    try:
        if wal.exists() and not shm.exists():
            shutil.copyfile(db, dest)
            shutil.copyfile(wal, tmp / "photos.db-wal")
        else:
            source = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
            try:
                copy = sqlite3.connect(dest)
                try:
                    base.sqlite_backup(source, copy, db)       # gives up, never hangs
                finally:
                    copy.close()
            finally:
                source.close()
        conn = sqlite3.connect(dest)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        return conn
    except sqlite3.DatabaseError as e:
        hint = f" — {_FULL_DISK}" if platforms.is_mac() and "open" in str(e) else ""
        raise DomainError(f"cannot read {db.name} as a database: {e}{hint}") from e


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _sel(cols: set[str], alias: str, col: str, as_name: str) -> str:
    return f"{alias}.{col} AS {as_name}" if col in cols else f"NULL AS {as_name}"


def _scan_library(db: Path, cfg: Config) -> base.Scan:
    library = db.parent.parent if db.parent.name.lower() == "database" else db.parent
    include_hidden = bool(cfg.connector_settings(name).get("include_hidden"))
    tmp = tempfile.TemporaryDirectory(prefix="lifecrm-photos-")
    try:
        conn = _snapshot(db, Path(tmp.name))
        try:
            result = _read_library(conn, db, library, cfg, include_hidden)
        finally:
            conn.close()
    finally:
        tmp.cleanup()
    return _finish(
        result["records"], result["skipped"],
        account=result["account"], source_file=library.name,
        source_hash=result["hash"], target_kind="library", schema=result["schema"],
        media_seen=result["assets"], present_ids=result["present"],
        mirror_streams=list(streams),
        facts={"folder": str(library.resolve()), "assets": result["assets"]})


def _read_library(conn: sqlite3.Connection, db: Path, library: Path, cfg: Config,
                  include_hidden: bool) -> dict:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    table = next((t for t in ("ZASSET", "ZGENERICASSET") if t in tables), None)
    if table is None:
        listing = ", ".join(sorted(tables)[:8]) or "(no tables)"
        raise DomainError(f"{db.name} is a database but not a Photos library — no ZASSET "
                          f"(macOS 11+) or ZGENERICASSET (macOS 10.15) table (has: {listing})")
    ac = _columns(conn, table)
    missing = {"ZUUID", "ZDATECREATED"} - ac
    if missing:
        raise DomainError(f"{table} has no {', '.join(sorted(missing))} column — not a "
                          f"Photos schema this connector knows")

    select = [_sel(ac, "a", c, a) for c, a in (
        ("ZUUID", "uuid"), ("ZDATECREATED", "created"), ("ZLATITUDE", "lat"),
        ("ZLONGITUDE", "lon"), ("ZFILENAME", "filename"), ("ZDIRECTORY", "directory"),
        ("ZTRASHEDSTATE", "trashed"), ("ZKIND", "kind"), ("ZKINDSUBTYPE", "subtype"),
        ("ZHIDDEN", "hidden"), ("ZFAVORITE", "favorite"), ("ZWIDTH", "width"),
        ("ZHEIGHT", "height"), ("ZORIENTATION", "orientation"),
        ("ZDURATION", "duration"), ("ZMODIFICATIONDATE", "modified"),
        ("ZADDEDDATE", "added"))]
    joins = []
    aa = _columns(conn, "ZADDITIONALASSETATTRIBUTES") \
        if "ZADDITIONALASSETATTRIBUTES" in tables else set()
    if "ZASSET" in aa:
        joins.append("LEFT JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET = a.Z_PK")
    else:
        aa = set()
    select += [_sel(aa, "aa", c, a) for c, a in (
        ("ZORIGINALFILENAME", "original_filename"), ("ZTIMEZONEOFFSET", "tz_offset"),
        ("ZTITLE", "user_title"))]
    ad = _columns(conn, "ZASSETDESCRIPTION") if "ZASSETDESCRIPTION" in tables else set()
    if "ZASSETDESCRIPTION" in aa and "ZLONGDESCRIPTION" in ad:
        joins.append("LEFT JOIN ZASSETDESCRIPTION ad ON ad.Z_PK = aa.ZASSETDESCRIPTION")
        select.append("ad.ZLONGDESCRIPTION AS caption")
    else:
        select.append("NULL AS caption")
    ea = _columns(conn, "ZEXTENDEDATTRIBUTES") if "ZEXTENDEDATTRIBUTES" in tables else set()
    if "ZASSET" in ea:
        joins.append("LEFT JOIN ZEXTENDEDATTRIBUTES ea ON ea.ZASSET = a.Z_PK")
    else:
        ea = set()
    select += [_sel(ea, "ea", c, a) for c, a in (("ZCAMERAMAKE", "make"),
                                                 ("ZCAMERAMODEL", "model"))]

    account = None
    if "Z_METADATA" in tables and "Z_UUID" in _columns(conn, "Z_METADATA"):
        row = conn.execute("SELECT Z_UUID FROM Z_METADATA ORDER BY Z_VERSION LIMIT 1").fetchone()
        if row and row[0]:
            account = f"photos-library:{row[0]}"
    if account is None:
        account = f"photos-library:{library.resolve().as_posix()}"

    records, present = [], set()
    skipped: Counter = Counter()
    digest = hashlib.sha256()
    assets = 0
    for r in conn.execute(f"SELECT {', '.join(select)} FROM {table} a "
                          f"{' '.join(joins)} ORDER BY a.Z_PK"):
        assets += 1
        uid = r["uuid"]
        if not uid:
            skipped["asset with no UUID"] += 1
            continue
        ext_id = f"apple-photos:{uid}"
        digest.update(f"{uid}|{r['modified']}|{r['trashed']}|{r['hidden']}\n".encode())
        if r["trashed"]:
            skipped["in Recently Deleted (removed from the archive if it was imported)"] += 1
            continue
        present.add(ext_id)           # in the library, whether imported or not
        kind = {0: "photo", 1: "video"}.get(r["kind"] or 0)
        if kind is None:
            skipped[f"asset of kind {r['kind']} (neither photo nor video)"] += 1
            continue
        if kind == "photo" and r["subtype"] == 10:
            kind = "screenshot"
        if r["hidden"] and not include_hidden:
            skipped["hidden in Photos (set [connectors.photos] include_hidden = true "
                    "to import)"] += 1
            continue
        started = _abcddb.core_data_iso(r["created"])
        if started is None:
            skipped["asset with no capture date"] += 1
            continue
        offset = r["tz_offset"]
        if not isinstance(offset, int) or abs(offset) > _MAX_OFFSET_SEC:
            offset = None
        lat, lon = r["lat"], r["lon"]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)) \
                or lat == -180 or lon == -180 or not (-90 <= lat <= 90) \
                or (lat, lon) == (0, 0):
            lat = lon = None
        filename, directory = r["filename"], r["directory"]
        media = None
        if filename:
            if directory and directory.startswith("/"):      # referenced, not copied in
                media = f"{directory.rstrip('/')}/{filename}"
            else:
                media = "/".join(x for x in ("originals", directory, filename) if x)
        duration = r["duration"] if kind == "video" else None
        records.append(_record(
            external_id=ext_id, started=started, offset=offset, cfg=cfg,
            title=r["original_filename"] or filename or uid,
            body=" — ".join(x.strip() for x in (r["user_title"], r["caption"])
                            if isinstance(x, str) and x.strip()),
            detail={
                "kind": kind, "time_source": "library",
                "lat": round(lat, 6) if lat is not None else None,
                "lon": round(lon, 6) if lon is not None else None,
                "make": r["make"], "model": r["model"],
                "width": r["width"] or None, "height": r["height"] or None,
                "orientation": r["orientation"] or None,
                "duration_sec": round(duration, 2) if duration else None,
                "favorite": bool(r["favorite"]), "hidden": bool(r["hidden"]),
            },
            media_path=media, origin_app="apple_photos",
            last_modified=_abcddb.core_data_iso(r["modified"])
            or _abcddb.core_data_iso(r["added"])))
    return {"records": records, "present": present, "skipped": skipped,
            "account": account, "hash": digest.hexdigest(), "assets": assets,
            "schema": "macOS 11+" if table == "ZASSET" else "macOS 10.15"}


# ---------------------------------------------------------------- report

_WHERE = {"folder": "folder", "zip": "zip archive", "takeout": "Google Photos Takeout",
          "library": "Photos library", "file": "single file"}


def _sample(rec: dict) -> str:
    d = json.loads(rec["value_json"]) if rec["value_json"] else {}
    bits = [" ".join(x for x in (d.get("make"), d.get("model")) if x)]
    bits.append(f"{d['lat']:.4f},{d['lon']:.4f}" if "lat" in d else "no GPS")
    if rec["utc_offset_sec"] is not None:
        sec = rec["utc_offset_sec"]
        bits.append(f"UTC{'-' if sec < 0 else '+'}{abs(sec) // 3600:02d}:"
                    f"{abs(sec) % 3600 // 60:02d}")
    return f"{rec['journal_date']} {rec['title']} ({', '.join(b for b in bits if b)})"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    kinds = facts.get("kinds") or {}
    head = f"photos: {scan['source_file']} ({_WHERE.get(scan['target_kind'], '?')}"
    if scan.get("schema"):
        head += f", {scan['schema']} schema"
    counted = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in sorted(kinds.items()))
    lines = [head + f"; {counted or 'nothing importable'})"]
    if facts.get("folder"):
        lines.append(f"  read from: {facts['folder']}")
    if scan["account"]:
        lines.append(f"  account: {scan['account']}")
    if facts.get("years"):
        lines.append("  per year: " + ", ".join(f"{y} {n}" for y, n in facts["years"].items())
                     + " — check the oldest against what the camera roll shows")
    sources = facts.get("time_sources") or {}
    if sources:
        weak = sum(sources.get(s, 0) for s in ("exif_local", "filename", "mtime"))
        lines.append("  time from: " + ", ".join(
            f"{s} {n}" for s, n in sorted(sources.items(), key=lambda kv: (-kv[1], kv[0])))
            + (f" ({weak} without the camera's own zone)" if weak else ""))
    total = sum(kinds.values())
    if total:
        lines.append(f"  location: {facts.get('with_gps', 0)} of {total} have GPS")

    new = [r for r in plan["new"] if r["stream"] == "photo"]
    upd = [rec for _, rec, _ in plan["updates"] if rec["stream"] == "photo"]
    if new or upd:
        recs = new or upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  photo: {len(new)} new, {len(upd)} changed | {span} | "
                     f"e.g. {_sample(latest)}")
    elif not plan.get("removed"):
        lines.append("  nothing new — the photos match the database")
    lines += base.removal_lines(plan, "no longer in the library (trashed or deleted)",
                                lambda row: row["title"])

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate id(s) inside the source")
    if tail:
        lines.append("  " + ", ".join(tail))
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:40]}: differs from "
                     f"the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    return lines

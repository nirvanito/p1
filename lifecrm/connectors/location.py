"""Location connector: places you visited, from Google Maps Timeline.

Where a day happened is the cheapest strong evidence there is ("the lake in
Slovenia four years ago", "how often did I make it to the gym"), and Google
Maps Timeline has already turned raw positions into visits. This connector
reads those visits — one `place_visit` observation per visit — and nothing
else.

Two export formats, read the same way:

  on-device export   Android: Settings > Location > Location services >
                     Timeline > Export Timeline data, a `Timeline.json`:
                       {semanticSegments: [{startTime, endTime,
                         visit: {probability, hierarchyLevel, topCandidate:
                           {placeId, semanticType, probability,
                            placeLocation: {latLng: '30.2585°, -97.7124°'}}}}
                        | {activity: ...} | {timelinePath: [...]}],
                        rawSignals: [...], userLocationProfile: {...}}
                     Google Maps on an iPhone exports the same segments as a
                     bare list (`location-history.json`), with 'geo:lat,lng'
                     strings, 'placeID', and numbers written as strings.
  legacy Takeout     `Semantic Location History/YYYY/YYYY_MONTH.json`:
                       {timelineObjects: [{placeVisit: {location: {name,
                         address, placeId, latitudeE7, longitudeE7,
                         semanticType}, duration: {startTimestamp,
                         endTimestamp}}} | {activitySegment: ...}]}
                     Google stopped producing it when Timeline moved onto the
                     phone; old Takeouts still hold years of it.

Only visits come in. Travel between places (activity segments), raw route
points (timelinePath, rawSignals) and Takeout's Records.json pings are counted
and reported, never imported: they are positions, not places, and storing
every GPS fix would bury the visits they add up to.

A record
--------
  title           the place name where the export has one (Takeout). The
                  on-device export names no place, so Home / Work where
                  Google labelled it, else the address's first line, else
                  'Place at <lat>, <lon>'
  body            the address, when the export has one
  started/ended   the visit, UTC; utc_offset_sec is the export's own offset,
                  so a morning in Slovenia is dated as that Slovenian morning
                  (the configured rollover hour applied in that zone).
                  Takeout carries no offset: the home timezone dates it
  value / unit    minutes at the place
  value_json      {lat, lon, place_id, semantic_type, address, probability,
                   hierarchy_level (nested visits only), confirmed}
  participants    the place as a `place_id` handle, role 'location'. Link the
                  handle to a place entity once and every visit there, past
                  and future, is attributed to it
  external_id     '<placeId>@<start>' (or 'geo:<lat>,<lon>@<start>'), the same
                  in both formats, so one visit read from a Takeout and from a
                  later device export is one record

Re-importing, and what wins
---------------------------
The exports carry no per-visit edit time, so every record's last_modified_at
is the export's own "as of" instant — the latest moment it describes. A later
export therefore wins: a visit still in progress when the previous export was
taken gets its real end, and a visit Google re-inferred is updated, audited.
An export older than the one already imported overwrites nothing; a
difference shows as a conflict and the stored visit is kept. Unchanged visits
only have that instant moved forward (silently, no audit).

Mirroring: none. Timeline's auto-delete, or an export that reaches back less
far, is not the owner deleting a visit, so the archive remembers. A visit
corrected in Timeline after it was imported arrives as a new visit (the place
is part of its identity) and the old one stays — the price of matching a
visit across both formats.

iPhone: Apple's own Significant Locations are encrypted on the device and have
no export. Only Google Maps' Timeline, where it is turned on, reaches here.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "location"
streams = ("place_visit",)

_ISO = "%Y-%m-%dT%H:%M:%SZ"
_DEVICE_NAMES = ("timeline.json", "location-history.json")
_MONTH_FILE = re.compile(r"^\d{4}_[A-Za-z]+\.json$")
# Takeout files that hold no visits, named in the report instead of parsed.
_NOT_VISITS = {
    "records.json": "raw location pings, not visits",
    "settings.json": "Timeline settings",
    "timeline edits.json": "Timeline's own edit log",
}
_SEMANTIC_LABELS = {"HOME": "Home", "INFERRED_HOME": "Home",
                    "WORK": "Work", "INFERRED_WORK": "Work"}
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


# ---------------------------------------------------------------- values

def _float(value) -> float | None:
    """Numbers arrive as numbers (Android) or as strings (iPhone)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _checked(lat: float, lon: float) -> tuple[float, float] | None:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 7), round(lon, 7)


def _latlng(value) -> tuple[float, float] | None:
    """(lat, lon) from every spelling the exports use: '30.2585°, -97.7124°',
    'geo:30.2585,-97.7124', {latLng: ...}, or Takeout's latitudeE7/longitudeE7."""
    if isinstance(value, dict):
        if "latitudeE7" in value and "longitudeE7" in value:
            lat, lon = _float(value["latitudeE7"]), _float(value["longitudeE7"])
            if lat is None or lon is None:
                return None
            return _checked(lat / 1e7, lon / 1e7)
        for key in ("latLng", "LatLng", "point"):
            if key in value:
                return _latlng(value[key])
        return None
    if not isinstance(value, str):
        return None
    nums = _NUMBER.findall(value.removeprefix("geo:"))
    if len(nums) < 2:
        return None
    return _checked(float(nums[0]), float(nums[1]))


def _semantic(raw) -> str | None:
    """One vocabulary for both formats: 'TYPE_HOME', 'HOME' and 'Inferred Home'
    become HOME / INFERRED_HOME. UNKNOWN says nothing and is dropped."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = re.sub(r"[\s\-]+", "_", raw.strip()).upper().removeprefix("TYPE_")
    return None if s in ("UNKNOWN", "SEMANTIC_TYPE_UNKNOWN") else s


def _instant(value, offset_minutes=None, home: str | None = None
             ) -> tuple[str | None, int | None]:
    """(UTC instant, the zone offset in seconds the export stated).

    Device exports write local time with its offset; Takeout writes UTC ('Z')
    or epoch milliseconds, which say nothing about the zone. A naive time is
    read with the segment's own offset field when there is one, else in the
    home timezone.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return base.iso_utc(value / 1000 if value > 1e11 else value), None
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    if text.isdigit():
        return _instant(int(text))
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    minutes = _float(offset_minutes)
    stated = int(minutes * 60) if minutes is not None else None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(minutes=minutes)) if minutes is not None
                        else ZoneInfo(home or "UTC"))
    elif not text.upper().endswith("Z"):
        # A written offset is the zone the visit happened in — '+00:00' is
        # London in winter, not "unknown". 'Z' is Takeout's UTC, which is.
        stated = int(dt.utcoffset().total_seconds())
    return dt.astimezone(timezone.utc).strftime(_ISO), stated


def _journal_date(started: str, offset: int | None, cfg: Config) -> str:
    """The local date of the visit where the export states the zone (rollover
    applied in that zone); the home-timezone rule otherwise."""
    if offset is None:
        return base.journal_date_for_iso(started, cfg)
    local = datetime.strptime(started, _ISO) + timedelta(seconds=offset)
    if local.hour < cfg.day_rollover_hour:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- reading

class _Acc:
    """What one scan has gathered so far, across every file it reads."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.records: list[dict] = []
        self.places: dict[str, str | None] = {}       # place_id -> display name
        self.skipped: Counter = Counter()
        self.as_of: str | None = None
        self.formats: Counter = Counter()

    def seen_instant(self, iso: str | None) -> None:
        if iso and (self.as_of is None or iso > self.as_of):
            self.as_of = iso

    def visit(self, *, place_id, pos, semantic, place_name, address, probability,
              start, end, offset, hierarchy=0, confirmed=False) -> None:
        if start is None:
            self.skipped["visit with no usable start time"] += 1
            return
        if end is not None and end < start:
            self.skipped["visit that ends before it starts"] += 1
            return
        if not place_id and pos is None:
            self.skipped["visit naming no place and no position"] += 1
            return
        label = _SEMANTIC_LABELS.get(semantic or "")
        # "Home" answers more questions than the street it is on; the address
        # stays in body either way.
        if place_name:
            title = place_name
        elif label:
            title = label
        elif address:
            title = address.splitlines()[0].strip()
        elif pos is not None:
            title = f"Place at {pos[0]:.4f}, {pos[1]:.4f}"
        else:
            title = "Unnamed place"
        minutes = None
        if end is not None:
            span = datetime.strptime(end, _ISO) - datetime.strptime(start, _ISO)
            minutes = int(round(span.total_seconds() / 60))
        detail = {"lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
                  "place_id": place_id, "semantic_type": semantic,
                  "address": address, "probability": probability,
                  "hierarchy_level": hierarchy or None,
                  "confirmed": True if confirmed else None}
        participants = []
        if place_id:
            participants.append({"kind": "place_id", "value": place_id,
                                 "role": "location"})
            if self.places.get(place_id) is None:
                self.places[place_id] = place_name or label
        key = place_id or f"geo:{pos[0]:.5f},{pos[1]:.5f}"
        self.records.append({
            "stream": "place_visit",
            "external_id": f"{key}@{start}",
            "journal_date": _journal_date(start, offset, self.cfg),
            "started_at": start,
            "ended_at": end,
            "utc_offset_sec": offset,
            "direction": None,
            "title": title,
            "body": address,
            "value": minutes,
            "unit": "min" if minutes is not None else None,
            "value_json": canonical_json({k: v for k, v in detail.items()
                                          if v is not None}),
            "media_path": None,
            "origin_app": "google_maps",
            "participants": participants,
        })


def _read_segments(segments: list, profile, acc: _Acc) -> None:
    """The on-device export: Android's {semanticSegments}, or the iPhone list."""
    labels = {}
    frequent = profile.get("frequentPlaces") if isinstance(profile, dict) else None
    for fp in frequent or []:
        if isinstance(fp, dict) and fp.get("placeId") and _semantic(fp.get("label")):
            labels[str(fp["placeId"])] = _semantic(fp.get("label"))
    home = acc.cfg.timezone
    for seg in segments:
        if not isinstance(seg, dict):
            acc.skipped["timeline segment that is not an object"] += 1
            continue
        start, start_off = _instant(seg.get("startTime"),
                                    seg.get("startTimeTimezoneUtcOffsetMinutes"), home)
        end, _ = _instant(seg.get("endTime"),
                          seg.get("endTimeTimezoneUtcOffsetMinutes"), home)
        acc.seen_instant(start)
        acc.seen_instant(end)
        visit = seg.get("visit")
        if not isinstance(visit, dict):
            if "activity" in seg:
                acc.skipped["activity segment (travel between places)"] += 1
            elif "timelinePath" in seg:
                acc.skipped["timelinePath segment (route points)"] += 1
            elif "timelineMemory" in seg:
                acc.skipped["timelineMemory segment (trip summary)"] += 1
            else:
                keys = ", ".join(sorted(k for k in seg if not k.endswith(("Time", "Minutes"))))
                acc.skipped[f"segment of an unknown kind ({keys or 'no content'})"] += 1
            continue
        top = visit.get("topCandidate")
        top = top if isinstance(top, dict) else {}
        place_id = top.get("placeId") or top.get("placeID")
        place_id = str(place_id).strip() or None if place_id else None
        prob = _float(top.get("probability"))
        if prob is None:
            prob = _float(visit.get("probability"))
        hierarchy = _float(visit.get("hierarchyLevel"))
        acc.visit(place_id=place_id, pos=_latlng(top.get("placeLocation")),
                  semantic=_semantic(top.get("semanticType")) or labels.get(place_id or ""),
                  place_name=None, address=None,
                  probability=round(prob, 4) if prob is not None else None,
                  start=start, end=end, offset=start_off,
                  hierarchy=int(hierarchy) if hierarchy else 0)


def _takeout_visit(pv: dict, acc: _Acc, hierarchy: int = 0) -> None:
    loc = pv.get("location") if isinstance(pv.get("location"), dict) else {}
    dur = pv.get("duration") if isinstance(pv.get("duration"), dict) else {}
    start, _ = _instant(dur.get("startTimestamp") or dur.get("startTimestampMs"))
    end, _ = _instant(dur.get("endTimestamp") or dur.get("endTimestampMs"))
    acc.seen_instant(start)
    acc.seen_instant(end)
    conf = _float(loc.get("locationConfidence"))
    place_name = loc.get("name").strip() if isinstance(loc.get("name"), str) else None
    address = loc.get("address").strip() if isinstance(loc.get("address"), str) else None
    place_id = loc.get("placeId")
    acc.visit(place_id=str(place_id).strip() or None if place_id else None,
              pos=_latlng(loc) or _latlng({"latitudeE7": pv.get("centerLatE7"),
                                           "longitudeE7": pv.get("centerLngE7")}),
              semantic=_semantic(loc.get("semanticType")),
              place_name=place_name or None, address=address or None,
              # Takeout states confidence in percent; the device export, and
              # this archive, in 0..1.
              probability=round(conf / 100, 4) if conf is not None else None,
              start=start, end=end, offset=None, hierarchy=hierarchy,
              confirmed=pv.get("editConfirmationStatus") == "CONFIRMED")
    for child in pv.get("childVisits") or []:
        if isinstance(child, dict):
            _takeout_visit(child, acc, hierarchy + 1)


def _read_takeout(objects: list, acc: _Acc) -> None:
    for obj in objects:
        if not isinstance(obj, dict):
            acc.skipped["timeline object that is not an object"] += 1
            continue
        if isinstance(obj.get("placeVisit"), dict):
            _takeout_visit(obj["placeVisit"], acc)
        elif isinstance(obj.get("activitySegment"), dict):
            dur = obj["activitySegment"].get("duration")
            if isinstance(dur, dict):
                acc.seen_instant(_instant(dur.get("endTimestamp")
                                          or dur.get("endTimestampMs"))[0])
            acc.skipped["activity segment (travel between places)"] += 1
        else:
            acc.skipped["timeline object of an unknown kind"] += 1


def _kind_of(data) -> str:
    """'device' | 'device_list' | 'takeout' | 'records', or a DomainError
    naming what the file holds instead."""
    if isinstance(data, dict):
        if isinstance(data.get("semanticSegments"), list):
            return "device"
        if isinstance(data.get("timelineObjects"), list):
            return "takeout"
        if isinstance(data.get("locations"), list):
            return "records"
        keys = ", ".join(sorted(data)[:8]) or "(empty object)"
        raise DomainError(
            f"has neither 'semanticSegments' (Timeline.json) nor 'timelineObjects' "
            f"(Takeout Semantic Location History) — it has: {keys}")
    if isinstance(data, list):
        if not data:
            raise DomainError("is an empty list — no timeline segments in it")
        segs = [s for s in data if isinstance(s, dict)]
        if any("startTime" in s and ("visit" in s or "activity" in s
                                     or "timelinePath" in s) for s in segs):
            return "device_list"
        first = ", ".join(sorted(segs[0])[:8]) if segs else "(not objects)"
        raise DomainError(f"is a list, but not of timeline segments — the first "
                          f"item has: {first}")
    raise DomainError(f"is a JSON {type(data).__name__}, not a location history")


def _parse(raw: bytes, rel: str):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise DomainError(f"{rel} is not valid UTF-8: {e}") from e
    except json.JSONDecodeError as e:
        raise DomainError(f"{rel} is not valid JSON: line {e.lineno} "
                          f"column {e.colno}: {e.msg}") from e


# ---------------------------------------------------------------- targets

def _select(entries: list[str]) -> tuple[list[str], list[str]]:
    """(paths to parse, files named in the report but not read). Inside a
    folder or a zip only the files Google keeps visits in are parsed —
    Takeout's Records.json alone can run to gigabytes of pings."""
    take, unread, devices = [], [], []
    for rel in entries:
        base_name = rel.rsplit("/", 1)[-1]
        low = base_name.lower()
        if low in _DEVICE_NAMES:
            devices.append(rel)
            take.append(rel)
        elif _MONTH_FILE.match(base_name):
            take.append(rel)
        elif low in _NOT_VISITS:
            unread.append(f"{base_name} ({_NOT_VISITS[low]})")
        elif low.endswith(".json"):
            unread.append(f"{base_name} (not a Timeline or Semantic Location History file)")
    if len(devices) > 1:
        raise DomainError(
            f"holds {len(devices)} device exports ({', '.join(devices[:4])}) — each "
            f"is the whole timeline as of its own day, so pass the one to import")
    if not take:
        listing = ", ".join(entries[:10]) or "(empty)"
        raise DomainError(
            f"holds no Timeline.json and no Semantic Location History month files "
            f"(YYYY_MONTH.json) — it has: {listing}")
    return take, unread


def _sources(target: Path):
    """Yield (target kind, unread, [(name, loader)]) once. Files are loaded one
    at a time, so a decade of Takeout months is never all in memory at once."""
    if target.is_file() and not zipfile.is_zipfile(target):
        if target.suffix.lower() != ".json":
            raise DomainError(
                f"{target.name} is not a .json file, a Takeout .zip or a folder — "
                f"expected Timeline.json or Semantic Location History")
        yield "file", [], [(target.name, target.read_bytes)]
        return
    if target.is_dir():
        by_rel = {p.relative_to(target).as_posix(): p
                  for p in target.rglob("*") if p.is_file()}
        try:
            take, unread = _select(sorted(by_rel))
        except DomainError as e:
            raise DomainError(f"{target.name} {e}") from e
        yield "folder", unread, [(rel, by_rel[rel].read_bytes) for rel in take]
        return
    with zipfile.ZipFile(target) as zf:
        try:
            take, unread = _select(sorted(m for m in zf.namelist() if not m.endswith("/")))
        except DomainError as e:
            raise DomainError(f"{target.name} {e}") from e
        yield "zip", unread, [(m, lambda m=m: zf.read(m)) for m in take]


def _is_file(p: Path) -> bool:
    """is_file() that answers False for a folder macOS will not let us look
    into (Downloads, iCloud Drive before permission is granted)."""
    try:
        return p.is_file()
    except OSError:
        return False


def default_target(cfg: Config) -> Path:
    """[connectors.location] path, else the one Timeline export sitting where
    downloads and synced folders land on this machine. Several are refused:
    each is the whole timeline as of its own day, and guessing is worse."""
    settings = cfg.connector_settings(name)
    if settings.get("path"):
        p = Path(str(settings["path"])).expanduser()
        if not p.exists():
            raise DomainError(f"[connectors.location] path does not exist: {p}")
        return p
    folders = [platforms.home() / "Downloads", *platforms.onedrive_candidates()]
    if platforms.is_mac():
        folders.append(platforms.icloud_drive())
    found, seen = [], set()
    for folder in folders:
        for fname in ("Timeline.json", "location-history.json"):
            p = folder / fname
            if str(p).lower() not in seen and _is_file(p):
                seen.add(str(p).lower())
                found.append(p)
    if len(found) == 1:
        return found[0]
    if found:
        raise DomainError("found several Timeline exports — pass the one to import: "
                          + "; ".join(str(p) for p in found))
    raise DomainError(
        "no Timeline export found — looked for Timeline.json and location-history.json "
        "in " + ", ".join(str(f) for f in folders)
        + "; export it from the phone (docs/location.md), pass the file or Takeout "
          "path, or set [connectors.location] path")


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    acc = _Acc(cfg)
    unmapped: Counter = Counter()
    try:
        for target_kind, unread, files in _sources(target):
            for rel, load in files:
                data = _parse(load(), rel)
                try:
                    kind = _kind_of(data)
                except DomainError as e:
                    if len(files) == 1:
                        raise DomainError(f"{rel} {e}") from e
                    unmapped[f"{rel.rsplit('/', 1)[-1]} (not a location history)"] += 1
                    continue
                acc.formats[kind] += 1
                if kind == "device":
                    _read_segments(data["semanticSegments"],
                                   data.get("userLocationProfile"), acc)
                    signals = data.get("rawSignals")
                    if isinstance(signals, list) and signals:
                        acc.skipped["raw signal (position, wifi or activity "
                                    "reading)"] += len(signals)
                elif kind == "device_list":
                    _read_segments(data, None, acc)
                elif kind == "takeout":
                    _read_takeout(data["timelineObjects"], acc)
                elif len(files) == 1:
                    raise DomainError(
                        f"{rel} holds raw location pings, not visits — import the "
                        f"'Semantic Location History' folder beside it instead")
                else:
                    unmapped[f"{rel.rsplit('/', 1)[-1]} (raw location pings, "
                             f"not visits)"] += 1
            for u in unread:
                unmapped[u] += 1
            files_read = len(files)
    except PermissionError as e:
        raise DomainError(
            f"cannot read {e.filename or target}: permission denied — on a Mac, "
            f"allow the app running this to use that folder (System Settings > "
            f"Privacy & Security > Files and Folders), then run it again") from e

    for rec in acc.records:
        rec["last_modified_at"] = acc.as_of
    records = sorted(acc.records,
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    dates = [r["journal_date"] for r in records]
    kinds = sorted(k for k in acc.formats if k != "records")
    return base.empty_scan(
        name,
        source_file=target.name,
        source_hash=base.file_hash(target) if target.is_file() else None,
        records=records,
        handles=[{"kind": "place_id", "value": pid, "display_name": shown}
                 for pid, shown in sorted(acc.places.items())],
        unmapped=[{"what": k, "count": v} for k, v in sorted(unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(acc.skipped.items())],
        facts={"format": kinds[0] if len(kinds) == 1 else ("mixed" if kinds else None),
               "files_read": files_read, "as_of": acc.as_of,
               "oldest_visit": min(dates) if dates else None,
               "newest_visit": max(dates) if dates else None},
        target_kind=target_kind,
        named_places=sum(1 for v in acc.places.values() if v))


# ---------------------------------------------------------------- report

_FORMATS = {"device": "Google Maps Timeline, on-device export",
            "device_list": "Google Maps Timeline, iPhone export",
            "takeout": "Google Takeout, Semantic Location History",
            "mixed": "Takeout and on-device exports together"}


def _sample(rec: dict) -> str:
    when = rec["started_at"][11:16] + "Z"
    if rec.get("utc_offset_sec") is not None:
        local = datetime.strptime(rec["started_at"], _ISO) + \
            timedelta(seconds=rec["utc_offset_sec"])
        when = local.strftime("%H:%M")
    mins = f", {rec['value']} min" if rec.get("value") is not None else ""
    return f"{rec['journal_date']} {when} {rec['title']}{mins}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    head = (f"location history: {scan['source_file']} "
            f"({_FORMATS.get(facts.get('format'), 'no visits found')}, "
            f"{facts.get('files_read', 0)} file(s) read")
    if facts.get("as_of"):
        head += f"; as of {facts['as_of'][:10]}"
    lines = [head + ")"]
    if facts.get("oldest_visit"):
        lines.append(f"  visits span {facts['oldest_visit']}..{facts['newest_visit']} "
                     f"— check the oldest against what Timeline shows")

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    if new or upd:
        recs = new or upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  place_visit: {len(new)} new, {len(upd)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
    else:
        lines.append("  nothing new — the export matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} unchanged visit(s) re-dated to this export")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} visit(s) listed twice")
    if tail:
        lines.append("  " + ", ".join(tail))

    if scan["handles"]:
        lines.append(f"  places: {len(scan['handles'])} place id(s), "
                     f"{scan['named_places']} named by the export — link one to a "
                     f"place once with 'lifecrm handles link place_id:<id> --entity <id>'")
    if plan["conflicts"]:
        lines.append(f"  kept existing: {len(plan['conflicts'])} visit(s) differ from "
                     f"what a newer export already stored — an older export cannot "
                     f"overwrite them")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not read: {u['what']}"
                     + (f" × {u['count']}" if u["count"] > 1 else ""))
    return lines

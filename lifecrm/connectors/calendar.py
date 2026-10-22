"""Calendar connector: .ics exports from Google Calendar, Apple Calendar, Outlook.

The connector that bootstraps the people graph (brief 4.1). A calendar is the
one source where other people appear with a stable address attached: every
attendee is an email handle, every organizer too, and every location is a
candidate place. Nothing else the owner already has names so many people so
cleanly, and the export is free on both supported setups.

Accepted targets
----------------
- a .ics file — what Apple Calendar's File > Export > Export… writes (one
  calendar per file), or any single calendar's .ics.
- a folder of .ics files, searched recursively — an unzipped Google Takeout
  (Takeout/Calendar/<calendar>.ics, one file per calendar).
- a zip holding .ics files — the Takeout zip itself, or what Google
  Calendar's Settings > Import & export > Export downloads
  (`<you>@gmail.com.ical.zip`).
- nothing at all: `[connectors.calendar] path` in config.toml, else the
  newest Takeout zip (or folder) or .ical.zip in ~/Downloads that holds a
  calendar.

What comes in
-------------
One observation per event **occurrence**, stream `calendar_event`: a weekly
1:1 is one row per week, because "when did I last meet Priya" is a question
about occurrences, not about a rule. `title` is SUMMARY, `body` DESCRIPTION
(Google's "Join with Google Meet" boilerplate and any HTML stripped),
`ended_at` the end, and `value_json` holds location, all_day, recurring, the
rrule text, the calendar's name, status, url, geo and each attendee's RSVP.

Participants are **handles, never names**: ORGANIZER -> role `organizer`,
ATTENDEE -> role `participant`, both `email` handles with the invite's CN as
display_name. The owner's own address is kept — it is a handle like any
other, and linking it to the owner once is what says "I was there". Rooms and
resources are not people and are skipped, counted. A physical LOCATION also
becomes a `place_id` handle (`calendar:<location, normalized>`, role
`location`): linking it to a place entity once attributes every event held
there. Meeting links and "Zoom" are not places and get no handle.

Time
----
Each record is dated in the event's own zone: a 08:00 breakfast in Ljubljana
belongs to that day there, not to the night before in Austin. All-day events
take their date as written, with no rollover — DTEND is exclusive, so a
one-day event ends at the next midnight. Times written in UTC (Google writes
single events that way, Outlook as TZID=UTC) carry no zone of their own and
are dated in the configured home timezone, like every other connector
without one; floating times are read in the home timezone too.

Repeating events
----------------
Expanded here (connectors/_ics.py), from the first occurrence through today
+ 90 days (`[connectors.calendar] horizon_days`), at most the newest 1000 per
series. EXDATE removes an occurrence; a RECURRENCE-ID override replaces its
occurrence and keeps its id, so a moved meeting is an update, not a new row;
a cancelled override removes it. A rule this connector cannot expand keeps
its first occurrence, is reported under "not understood", and turns
mirroring off for that run (below). STATUS:CANCELLED events are skipped and
counted.

Identity and re-import
----------------------
`external_id` is `UID@<occurrence start>` — the UTC instant, or the date for
an all-day event — and for an override the start of the slot it replaces.
`last_modified_at` is LAST-MODIFIED, else DTSTAMP, else CREATED. Google
always writes LAST-MODIFIED; its DTSTAMP is the export time, which would make
every re-export look newer, so it is only the fallback.

Deletion semantics — the archive mirrors the calendar
-----------------------------------------------------
The owner chose "mirror the source" for texts and AI chats (2026-09-24); a
calendar export is likewise its calendars' whole current state, so it follows
the same rule. An event deleted or cancelled in the calendar is removed by
the next import, scoped to the import's account:

- An export of **one** calendar (Apple's File > Export, a single .ics) is
  account `calendar:<its name>` and mirrors only that calendar — exporting
  "Home" never removes "Work".
- An export of **several** (Takeout, Google's Export zip) is the Google
  account's whole set: account = `[connectors.calendar] account`, else the
  email in a `<you>@gmail.com.ical.zip` file name, else the one calendar
  named by an email address (Google names your primary calendar that way).
  Several calendars and no owner -> nothing is removed, and the report says
  how to fix it.
- Occurrences are expanded past the import window for presence (another 366
  days), so an occurrence archived by an earlier run is never mistaken for a
  deletion because today's window ends sooner.
- An event this connector cannot read, or a rule it cannot expand, is still
  in the calendar — so any such run removes nothing at all, and says why.
- The shared guards apply (domain/observations.py): a removal large enough
  to look like a truncated export is refused without --allow-mass-removal,
  a snapshot is taken first, and a row a confirmed entry cites is hidden, not
  deleted.

Known limitations, by design
----------------------------
- An event exported by two apps (a Google calendar synced to a Mac and
  exported from both) shares its id; the second import's differences —
  calendar name, description formatting — surface as conflicts, never
  applied.
- Moving a one-off event to another time changes its id: the mirror removes
  the old row and adds the new one.
- VTODO (tasks) and VJOURNAL entries are not events; they are counted only.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import _ics, base

name = "calendar"
streams = ("calendar_event",)
# No stale_after_days: a calendar is exported by hand now and then, and its
# newest record is an event months ahead — "newest record" says nothing here
# about whether exports have stopped.

HORIZON_DAYS = 90                 # repeating events expand to today + this
MAX_PER_SERIES = 1000             # occurrences imported per series (the newest)
PRESENCE_SLACK_DAYS = 366         # expanded further, for presence only
_CAL_SUFFIXES = (".ics", ".ical", ".icalendar")
_OTHER_COMPONENTS = {"VTODO": "task (VTODO) — not an event",
                     "VJOURNAL": "journal entry (VJOURNAL) — not an event",
                     "VFREEBUSY": "free/busy block (VFREEBUSY) — not an event"}

_EMAIL = re.compile(r"^[^@\s:;,<>]+@[^@\s:;,<>]+\.[^@\s:;,<>]+$")
# Google's ids for rooms, resources and other calendars, which turn up as
# attendee addresses and as calendar names: not people.
_CALENDAR_ID = re.compile(r"@(?:resource\.|group\.(?:v\.)?|import\.)?calendar\.google\.com$",
                          re.I)
_VIRTUAL = re.compile(r"(?:://|^(?:zoom|google meet|meet|microsoft teams|teams|webex|"
                      r"skype|facetime|phone|call|online|virtual|tbd|tba)\b)", re.I)
_GOOGLE_INVITE = re.compile(r"-::~[:~]+::-.*?-::~[:~]+::-", re.S)
_HTML = re.compile(r"<\s*/?\s*(?:br|p|div|span|b|i|u|a|ul|ol|li|strong|em|h[1-6]|"
                   r"html|body|font)\b[^>]*>", re.I)


def _today(cfg: Config) -> date:
    """The scan date the expansion window hangs from. A function, so tests can
    pin it; never the export's own date, which says nothing about today."""
    return datetime.now(ZoneInfo(cfg.timezone)).date()


# ---------------------------------------------------------------- finding the export

def downloads_dirs() -> list[Path]:
    """Where a browser puts a Takeout or a Google Calendar export."""
    return [Path.home() / "Downloads"]


def _zip_calendars(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return [i.filename for i in zf.infolist() if not i.is_dir()
                    and Path(i.filename).suffix.lower() in _CAL_SUFFIXES
                    and not i.filename.startswith("__MACOSX/")]
    except (zipfile.BadZipFile, OSError):
        return []


def _folder_calendars(folder: Path) -> list[Path]:
    return sorted((p for p in folder.rglob("*") if p.is_file()
                   and p.suffix.lower() in _CAL_SUFFIXES
                   and not p.name.startswith("._")),
                  key=lambda p: p.relative_to(folder).as_posix().lower())


def default_target(cfg: Config) -> Path:
    """[connectors.calendar] path, else the newest calendar export in
    ~/Downloads: a Takeout zip or folder holding .ics files, or Google
    Calendar's own <you>.ical.zip."""
    settings = cfg.connector_settings(name)
    if settings.get("path"):
        p = Path(str(settings["path"])).expanduser()
        if not p.exists():
            raise DomainError(f"[connectors.calendar] path {p} does not exist — fix it "
                              f"in config.toml, or pass the export's path")
        return p
    found: list[Path] = []
    searched = []
    for folder in downloads_dirs():
        searched.append(str(folder))
        try:
            if not folder.is_dir():
                continue
            for p in folder.iterdir():
                low = p.name.lower()
                if p.is_file() and low.endswith(".zip") and \
                        (low.startswith("takeout") or low.endswith((".ical.zip", ".ics.zip"))):
                    if _zip_calendars(p):
                        found.append(p)
                elif p.is_dir() and low.startswith("takeout") and _folder_calendars(p):
                    found.append(p)
        except PermissionError as e:
            raise DomainError(f"cannot list {folder}: permission denied — on a Mac, allow "
                              f"the terminal to access Downloads (System Settings > "
                              f"Privacy & Security > Files and Folders)") from e
    if not found:
        raise DomainError(
            f"no calendar export found in {', '.join(searched) or '~/Downloads'} "
            f"(looked for a takeout-*.zip holding Calendar/*.ics, and <you>.ical.zip) "
            f"— pass the .ics, zip or folder, or set [connectors.calendar] path "
            f"(docs/calendar.md)")
    return max(found, key=lambda p: (p.stat().st_mtime, p.name))


def _sources(target: Path) -> tuple[str, list[tuple[str, bytes]]]:
    """(kind, [(member name, raw bytes)]); kind is file, folder, zip or takeout."""
    if target.suffix.lower() == ".icbu":
        raise DomainError(f"{target.name} is a Calendar Archive — Apple's own backup "
                          f"format, not iCalendar. In Calendar.app select one calendar, "
                          f"then File > Export > Export… (docs/calendar.md)")
    try:
        if target.is_dir():
            files = _folder_calendars(target)
            if not files:
                listing = ", ".join(sorted(p.name for p in target.iterdir())[:10]) \
                    or "(empty)"
                raise DomainError(f"{target.name} holds no .ics calendar files "
                                  f"(has: {listing})")
            members = [(p.relative_to(target).as_posix(), p.read_bytes()) for p in files]
            takeout = target.name.lower() == "takeout" or any(
                m.lower().startswith("takeout/") for m, _ in members)
            return ("takeout" if takeout else "folder"), members
        if zipfile.is_zipfile(target):
            names = sorted(_zip_calendars(target), key=str.lower)
            if not names:
                with zipfile.ZipFile(target) as zf:
                    listing = ", ".join(zf.namelist()[:10]) or "(empty)"
                raise DomainError(f"{target.name} contains no .ics calendar "
                                  f"(members: {listing})")
            with zipfile.ZipFile(target) as zf:
                members = [(n, zf.read(n)) for n in names]
            takeout = any(n.lower().startswith("takeout/") for n in names)
            return ("takeout" if takeout else "zip"), members
        return "file", [(target.name, target.read_bytes())]
    except PermissionError as e:
        raise DomainError(f"cannot read {target}: permission denied — on a Mac, allow "
                          f"the terminal to access that folder (System Settings > "
                          f"Privacy & Security)") from e
    except zipfile.BadZipFile as e:
        raise DomainError(f"{target.name} is a damaged zip: {e}") from e
    except OSError as e:
        raise DomainError(f"cannot read {target}: {e.strerror or e}") from e


def _content_hash(target: Path, members: list[tuple[str, bytes]]) -> str:
    """The file's hash; for a folder, one hash over every calendar in it."""
    if target.is_file():
        return base.file_hash(target)
    h = hashlib.sha256()
    for member, raw in members:
        h.update(member.encode())
        h.update(hashlib.sha256(raw).digest())
    return h.hexdigest()


# ---------------------------------------------------------------- calendars

@dataclass
class _Cal:
    name: str
    member: str
    origin: str
    zones: _ics.Zones
    events: list[_ics.Component]


def _origin(prodid: str | None) -> str:
    p = (prodid or "").lower()
    for needle, app in (("google", "google_calendar"), ("apple", "apple_calendar"),
                        ("microsoft", "outlook"), ("mozilla", "thunderbird"),
                        ("samsung", "samsung_calendar")):
        if needle in p:
            return app
    return "calendar"


def _calendars(members: list[tuple[str, bytes]], counts: Counter, unmapped: Counter
               ) -> list[_Cal]:
    cals = []
    for member, raw in members:
        roots, malformed = _ics.parse(raw, Path(member).name)
        if malformed:
            unmapped["malformed content line (skipped)"] += malformed
        for root in roots:
            if root.name != "VCALENDAR":
                continue
            stem = Path(member).name
            for suffix in _CAL_SUFFIXES:
                if stem.lower().endswith(suffix):
                    stem = stem[:-len(suffix)]
            prodid = root.first("PRODID")
            cals.append(_Cal(
                name=root.text("X-WR-CALNAME") or stem,
                member=member,
                origin=_origin(prodid.value if prodid else None),
                zones=_ics.Zones(root.kids("VTIMEZONE")),
                events=root.kids("VEVENT")))
            for child in root.children:
                if child.name in _OTHER_COMPONENTS:
                    counts[_OTHER_COMPONENTS[child.name]] += 1
    return cals


def _person_email(s: str) -> bool:
    return bool(_EMAIL.match(s.strip())) and not _CALENDAR_ID.search(s.strip())


def _account(cals: list[_Cal], target: Path, settings: dict) -> tuple[str | None, str]:
    """(account, how it was decided). See the module docstring."""
    names = list(dict.fromkeys(c.name for c in cals))
    if len(names) == 1:
        return f"calendar:{names[0]}", "a single calendar"
    configured = str(settings.get("account") or "").strip()
    if configured:
        return configured, "[connectors.calendar] account"
    m = re.match(r"^(.+?)\.(?:ical|ics)\.zip$", target.name, re.I)
    if m and _person_email(m.group(1)):
        return m.group(1).lower(), "the export's file name"
    emails = [n for n in names if _person_email(n)]
    if len(emails) == 1:
        return emails[0].lower(), "the calendar named by an email address"
    return None, (f"{len(names)} calendars and no owner named — set "
                  f"[connectors.calendar] account = \"you@gmail.com\" in config.toml, "
                  f"or import each calendar's .ics on its own")


# ---------------------------------------------------------------- one event's fields

class _Unreadable(ValueError):
    pass


@dataclass
class _Start:
    value: date | datetime        # a date, or a naive wall-clock datetime in `tz`
    tz: tzinfo | None             # None for all-day events
    utc_frame: bool = False       # written in UTC: the event's own zone is unknown

    @property
    def all_day(self) -> bool:
        return not isinstance(self.value, datetime)


# Zones that are UTC under another name. Outlook writes TZID=UTC (or the
# "Coordinated Universal Time" display name) where Google writes 'Z'; either
# way the event's own zone is unknown, so both are dated in the home zone.
_UTC_KEYS = {"UTC", "Etc/UTC", "Etc/UCT", "UCT", "GMT", "Etc/GMT", "Etc/GMT0",
             "Etc/GMT+0", "Etc/GMT-0", "Etc/Universal", "Universal", "Etc/Zulu", "Zulu"}


def _frame(value: date | datetime, cfg: Config) -> _Start:
    if not isinstance(value, datetime):
        return _Start(value, None)
    if value.tzinfo is None:                        # floating (or an unknown TZID)
        return _Start(value, ZoneInfo(cfg.timezone))
    utc = value.tzinfo is timezone.utc or getattr(value.tzinfo, "key", None) in _UTC_KEYS
    return _Start(value.replace(tzinfo=None), value.tzinfo, utc_frame=utc)


def _into_frame(value: date | datetime, start: _Start, cfg: Config) -> date | datetime:
    """Another date property (EXDATE, RDATE, RECURRENCE-ID, UNTIL) expressed in
    the series' own frame, so it compares with generated occurrences."""
    if start.all_day:
        if isinstance(value, datetime):
            return (value.astimezone(ZoneInfo(cfg.timezone)) if value.tzinfo
                    else value).date()
        return value
    if not isinstance(value, datetime):
        return value                                # a whole day, matched by date
    if value.tzinfo is not None:
        return value.astimezone(start.tz).replace(tzinfo=None)
    return value


def _key(value: date | datetime, start: _Start) -> str:
    if not isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    return value.replace(tzinfo=start.tz).astimezone(timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")


def _iso(value: date | datetime | None) -> str | None:
    """LAST-MODIFIED and friends: UTC by the RFC; a naive one is read as UTC."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Text(HTMLParser):
    _BLOCK = {"p", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.link: tuple[str, int] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "br" or tag in self._BLOCK:
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self.link = (href, len(self.out)) if href else None

    def handle_endtag(self, tag):
        if tag in self._BLOCK:
            self.out.append("\n")
        elif tag == "a" and self.link:
            href, mark = self.link
            shown = "".join(self.out[mark:])
            if href not in shown and not href.lower().startswith("mailto:"):
                self.out.append(f" ({href})")
            self.link = None

    def handle_data(self, data):
        self.out.append(data)


def _description(text: str | None) -> str | None:
    """DESCRIPTION as prose: Google's conferencing boilerplate removed (the
    link itself is kept in value_json.url) and HTML, which Google's editor
    writes, turned back into text."""
    if not text:
        return None
    text = _GOOGLE_INVITE.sub("", text)
    if _HTML.search(text):
        parser = _Text()
        parser.feed(text)
        parser.close()
        text = "".join(parser.out)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text or None


def _location(comp: _ics.Component) -> tuple[str | None, str | None, list | None]:
    """(location text, a place title if the source gives one, [lat, lon])."""
    text = comp.text("LOCATION")
    if text:
        text = ", ".join(part.strip() for part in text.splitlines() if part.strip())
    title, geo = None, None
    apple = comp.first("X-APPLE-STRUCTURED-LOCATION")
    if apple is not None:
        title = (apple.param("X-TITLE") or "").strip() or None
        m = re.match(r"^geo:\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", apple.value.strip(),
                     re.I)
        if m:
            geo = [float(m.group(1)), float(m.group(2))]
    g = comp.first("GEO")
    if g is not None and geo is None:
        m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[;,]\s*(-?\d+(?:\.\d+)?)", g.value)
        if m:
            geo = [float(m.group(1)), float(m.group(2))]
    return text or title, title, geo


def _address(prop: _ics.Prop) -> tuple[str, str] | None:
    """(kind, value) for an ORGANIZER/ATTENDEE, or None when it names no
    address (an urn:uuid with no EMAIL parameter)."""
    v = prop.value.strip()
    low = v.lower()
    email = (prop.param("EMAIL") or "").strip()
    if low.startswith("mailto:"):
        addr = v[7:].strip()
    elif low.startswith("tel:"):
        digits = v[4:].strip()
        return ("phone", base.normalize_phone(digits)) if re.search(r"\d", digits) else None
    elif email:
        addr = email
    elif "@" in v and ":" not in v:
        addr = v
    else:
        return None
    if "@" not in addr and "@" in email:
        addr = email
    addr = addr.strip().strip("<>").strip()
    return ("email", addr.lower()) if _EMAIL.match(addr) else None


def _place_handle(location: str) -> str:
    norm = " ".join(location.split()).casefold().strip(" ,;.")
    return f"calendar:{norm}"


@dataclass
class _Fields:
    start: _Start
    length: timedelta
    summary: str | None
    body: str | None
    location: str | None
    geo: list | None
    status: str | None
    url: str | None
    modified: str | None
    participants: list[dict]
    responses: dict
    handles: list[tuple[str, str, str | None]] = field(default_factory=list)


def _fields(comp: _ics.Component, cal: _Cal, cfg: Config, counts: Counter,
            unmapped: Counter) -> _Fields:
    prop = comp.first("DTSTART")
    if prop is None:
        raise _Unreadable("no DTSTART")
    try:
        start = _frame(_ics.parse_value(prop, cal.zones), cfg)
    except ValueError as e:
        raise _Unreadable(str(e)) from None
    tzid = prop.param("TZID")
    if tzid and cal.zones.note(tzid):
        unmapped[f"time zone '{tzid}' ({cal.zones.note(tzid)})"] += 1

    length = timedelta(days=1) if start.all_day else timedelta(0)
    try:
        if comp.first("DTEND") is not None:
            end = _ics.parse_value(comp.first("DTEND"), cal.zones)
            if start.all_day:
                end_day = end.date() if isinstance(end, datetime) else end
                length = timedelta(days=max(1, (end_day - start.value).days))
            else:
                end_local = _into_frame(end, start, cfg)
                if not isinstance(end_local, datetime):
                    end_local = datetime.combine(end_local, time())
                length = max(timedelta(0), end_local - start.value)
        elif comp.first("DURATION") is not None:
            d = _ics.parse_duration(comp.first("DURATION").value)
            length = timedelta(days=max(1, d.days)) if start.all_day \
                else max(timedelta(0), d)
    except ValueError:
        unmapped["unreadable DTEND/DURATION (the event's length was assumed)"] += 1

    location, place_title, geo = _location(comp)
    status = comp.first("STATUS")
    url = comp.first("URL") or comp.first("X-GOOGLE-CONFERENCE")
    modified = None
    for key in ("LAST-MODIFIED", "DTSTAMP", "CREATED"):
        p = comp.first(key)
        if p is not None:
            try:
                modified = _iso(_ics.parse_value(p, None))
            except ValueError:
                modified = None
            if modified:
                break

    participants: list[dict] = []
    handles: list[tuple[str, str, str | None]] = []
    responses: dict[str, str] = {}
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, value: str, role: str, display: str | None) -> None:
        if (kind, value, role) in seen:
            return
        seen.add((kind, value, role))
        participants.append({"kind": kind, "value": value, "role": role})
        handles.append((kind, value, display))

    for p, role in [(p, "organizer") for p in comp.all("ORGANIZER")] + \
                   [(p, "participant") for p in comp.all("ATTENDEE")]:
        cutype = (p.param("CUTYPE") or "").upper()
        raw = p.value.strip()
        if cutype in ("ROOM", "RESOURCE") or _CALENDAR_ID.search(raw):
            counts["room, resource or calendar address among the attendees"] += 1
            continue
        h = _address(p)
        if h is None:
            unmapped["attendee with no email address"] += 1
            continue
        cn = (p.param("CN") or "").strip()
        display = cn if cn and "@" not in cn and cn.lower() != h[1] else None
        add(h[0], h[1], role, display)
        if role == "participant" and p.param("PARTSTAT"):
            responses[h[1]] = p.param("PARTSTAT").strip().lower()

    if location and not _VIRTUAL.search(location):
        add("place_id", _place_handle(location), "location", place_title or location)

    return _Fields(
        start=start, length=length, summary=comp.text("SUMMARY"),
        body=_description(comp.text("DESCRIPTION")), location=location, geo=geo,
        status=status.value.strip().upper() if status and status.value.strip() else None,
        url=url.value.strip() if url and url.value.strip() else None,
        modified=modified, participants=participants, responses=responses,
        handles=handles)


# ---------------------------------------------------------------- records

def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(uid: str, slot: str, f: _Fields, at: date | datetime, cal: _Cal,
            cfg: Config, recurring: bool, rrule: str | None,
            original: str | None = None) -> dict:
    start = f.start
    if start.all_day:
        # A date, not an instant: local midnight to (exclusive) midnight, and
        # the journal date as written — no rollover shift to the day before.
        home = ZoneInfo(cfg.timezone)
        started = _utc_iso(datetime.combine(at, time(), tzinfo=home))
        ended = _utc_iso(datetime.combine(at + timedelta(days=f.length.days), time(),
                                          tzinfo=home))
        journal_date, offset = at.isoformat(), None
    else:
        local = at.replace(tzinfo=start.tz)
        started = _utc_iso(local)
        ended = max(_utc_iso((at + f.length).replace(tzinfo=start.tz)), started)
        if start.utc_frame:
            journal_date, offset = base.journal_date_for_iso(started, cfg), None
        else:
            day = local - timedelta(days=1) if local.hour < cfg.day_rollover_hour \
                else local
            journal_date = day.strftime("%Y-%m-%d")
            offset = int(local.utcoffset().total_seconds())
    detail: dict = {"calendar": cal.name, "all_day": start.all_day,
                    "recurring": recurring}
    if f.location:
        detail["location"] = f.location
    if recurring and rrule:
        detail["rrule"] = rrule
    if f.status:
        detail["status"] = f.status.lower()
    if f.url:
        detail["url"] = f.url
    if f.geo:
        detail["geo"] = f.geo
    if f.responses:
        detail["responses"] = f.responses
    if original:
        detail["original_start"] = original
    return {
        "stream": "calendar_event",
        "external_id": f"{uid}@{slot}",
        "journal_date": journal_date,
        "started_at": started,
        "ended_at": ended,
        "utc_offset_sec": offset,
        "direction": None,
        "title": f.summary or "(no title)",
        "body": f.body,
        "value": None,
        "unit": None,
        "value_json": canonical_json(detail),
        "media_path": None,
        "origin_app": cal.origin,
        "last_modified_at": f.modified,
        "participants": f.participants,
    }


def _slot_iso(slot: str) -> str:
    if len(slot) == 8:
        return f"{slot[:4]}-{slot[4:6]}-{slot[6:]}"
    return f"{slot[:4]}-{slot[4:6]}-{slot[6:8]}T{slot[9:11]}:{slot[11:13]}:{slot[13:15]}Z"


@dataclass
class _Out:
    records: list = field(default_factory=list)      # (calendar name, record)
    present: set = field(default_factory=set)
    names: dict = field(default_factory=dict)        # (kind, value) -> display name
    counts: Counter = field(default_factory=Counter)
    unmapped: Counter = field(default_factory=Counter)
    mirror_off: set = field(default_factory=set)
    series: int = 0
    occurrences: int = 0

    def add(self, cal: _Cal, rec: dict, f: _Fields) -> None:
        self.records.append((cal.name, rec))
        for kind, value, display in f.handles:     # first name seen sticks
            if self.names.get((kind, value)) is None:
                self.names[(kind, value)] = display


def _uid(comp: _ics.Component, cal: _Cal, out: _Out) -> str:
    uid = comp.first("UID")
    if uid is not None and uid.value.strip():
        return uid.value.strip()
    # The RFC requires a UID; a producer that omits one still deserves its
    # events. Start and title identify it — stable as long as neither moves.
    out.unmapped["event without a UID (identified by its start and title)"] += 1
    start = comp.first("DTSTART")
    seed = f"{cal.name}|{start.value if start else ''}|{comp.text('SUMMARY') or ''}"
    return "nouid-" + hashlib.sha1(seed.encode()).hexdigest()[:16]


def _rid(comp: _ics.Component, start: _Start | None, cal: _Cal, cfg: Config,
         out: _Out) -> str | None:
    """The slot an override replaces, keyed like the master's occurrences."""
    prop = comp.first("RECURRENCE-ID")
    try:
        value = _ics.parse_value(prop, cal.zones)
    except ValueError:
        out.unmapped["unreadable RECURRENCE-ID (override skipped)"] += 1
        out.mirror_off.add("an event it cannot read")
        return None
    if (prop.param("RANGE") or "").upper() == "THISANDFUTURE":
        out.unmapped["RANGE=THISANDFUTURE override (applied to its own occurrence only)"] += 1
    frame = start or _frame(value, cfg)
    return _key(_into_frame(value, frame, cfg), frame)


def _series(uid: str, masters: list, overrides: list, cal: _Cal, cfg: Config,
            horizon: date, presence_horizon: date, out: _Out) -> None:
    """Every occurrence of one UID in one calendar, overrides applied."""
    def read(comp):
        try:
            return _fields(comp, cal, cfg, out.counts, out.unmapped)
        except _Unreadable as e:
            out.unmapped[f"event with no readable start ({e})"] += 1
            out.mirror_off.add("an event it cannot read")
            return None

    for master in masters or [None]:
        mf = read(master) if master is not None else None
        if master is not None and mf is None:
            continue
        if mf is not None and mf.status == "CANCELLED":
            out.counts["cancelled event"] += 1     # and with it, every override
            continue
        start = mf.start if mf else None

        replaced: dict[str, _Fields] = {}
        cancelled: set[str] = set()
        for ov in overrides:
            slot = _rid(ov, start, cal, cfg, out)
            if slot is None:
                continue
            of = read(ov)
            if of is None:
                continue
            if of.status == "CANCELLED":
                cancelled.add(slot)
            else:
                replaced[slot] = of

        slots: dict[str, date | datetime] = {}
        rrule_text = None
        recurring = mf is None or bool(master.all("RRULE") or master.all("RDATE"))
        if mf is not None:
            occs: list = [start.value]
            rules = master.all("RRULE")
            if rules:
                rrule_text = rules[0].value.strip()
                try:
                    if len(rules) > 1:
                        raise _ics.Unsupported("more than one RRULE")
                    rule = _ics.parse_rule(rrule_text, start.value)
                    until = _into_frame(rule.until, start, cfg) \
                        if rule.until is not None else None
                    occs = list(_ics.expand(rule, start.value, presence_horizon, until))
                except _ics.Unsupported as e:
                    out.unmapped[f"repeat rule '{rrule_text}' ({e}) — first "
                                 f"occurrence kept"] += 1
                    out.mirror_off.add("a repeat rule it cannot expand")
                    occs = [start.value]
            for p in master.all("RDATE"):
                if (p.param("VALUE") or "").upper() == "PERIOD":
                    out.unmapped["RDATE;VALUE=PERIOD (not added)"] += 1
                    continue
                try:
                    occs += [_into_frame(v, start, cfg) for v in _ics.parse_values(p, cal.zones)]
                except ValueError:
                    out.unmapped["unreadable RDATE (not added)"] += 1
            ex_keys, ex_days = set(), set()
            for p in master.all("EXDATE"):
                try:
                    for v in _ics.parse_values(p, cal.zones):
                        v = _into_frame(v, start, cfg)
                        if not start.all_day and not isinstance(v, datetime):
                            ex_days.add(v)
                        else:
                            ex_keys.add(_key(v, start))
                except ValueError:
                    out.unmapped["unreadable EXDATE (ignored)"] += 1
            for v in occs:
                if not isinstance(v, type(start.value)):
                    continue                    # an RDATE of the other value type
                day = v.date() if isinstance(v, datetime) else v
                if recurring and day > presence_horizon:
                    continue
                k = _key(v, start)
                if k in ex_keys or day in ex_days:
                    out.counts["occurrence removed by EXDATE"] += 1
                    continue
                slots[k] = v
        for slot in cancelled:
            if slots.pop(slot, None) is not None or mf is None:
                out.counts["cancelled occurrence"] += 1
        for slot, of in replaced.items():
            if slot not in slots:
                slots[slot] = of.start.value      # an override with no generated slot

        out.present.update(f"{uid}@{k}" for k in slots)
        keys = sorted(slots)
        if recurring:
            keys = [k for k in keys if k[:8] <= horizon.strftime("%Y%m%d")]
            if len(keys) > MAX_PER_SERIES:
                out.counts[f"occurrence older than the newest {MAX_PER_SERIES} of its "
                           f"series (not imported)"] += len(keys) - MAX_PER_SERIES
                keys = keys[-MAX_PER_SERIES:]
            if mf is not None:
                out.series += 1
        for k in keys:
            if k in replaced:
                of = replaced[k]
                own = _key(of.start.value, of.start)
                out.add(cal, _record(uid, k, of, of.start.value, cal, cfg, True, rrule_text,
                                     original=_slot_iso(k) if own != k else None), of)
            else:
                out.add(cal, _record(uid, k, mf, slots[k], cal, cfg, recurring,
                                     rrule_text), mf)
            if recurring:
                out.occurrences += 1


def _read(cals: list[_Cal], cfg: Config, horizon: date, presence_horizon: date) -> _Out:
    out = _Out()
    for cal in cals:
        groups: dict[str, dict[str, list]] = {}
        for comp in cal.events:
            uid = _uid(comp, cal, out)
            g = groups.setdefault(uid, {"masters": [], "overrides": []})
            g["overrides" if comp.first("RECURRENCE-ID") else "masters"].append(comp)
        for uid, g in groups.items():
            _series(uid, g["masters"], g["overrides"], cal, cfg, horizon,
                    presence_horizon, out)
    return out


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    settings = cfg.connector_settings(name)
    try:
        horizon_days = int(settings.get("horizon_days", HORIZON_DAYS))
    except (TypeError, ValueError):
        raise DomainError(f"[connectors.calendar] horizon_days must be a whole number "
                          f"of days, got {settings.get('horizon_days')!r}") from None
    if not 0 <= horizon_days <= 3650:
        raise DomainError(f"[connectors.calendar] horizon_days must be 0-3650, "
                          f"got {horizon_days}")
    kind, members = _sources(target)
    counts: Counter = Counter()
    unmapped: Counter = Counter()
    cals = _calendars(members, counts, unmapped)
    if not cals:
        raise DomainError(f"{target.name} holds no VCALENDAR — nothing to import")

    today = _today(cfg)
    horizon = today + timedelta(days=horizon_days)
    out = _read(cals, cfg, horizon, horizon + timedelta(days=PRESENCE_SLACK_DAYS))
    out.counts.update(counts)
    out.unmapped.update(unmapped)

    # A calendar the owner chose not to import (a public holidays feed) is
    # dropped here — but its events are still in the source, so their ids
    # stay present and nothing already archived from it is removed.
    excluded = {str(n).casefold() for n in settings.get("exclude_calendars") or ()}
    kept = []
    for cal_name, rec in out.records:
        if cal_name.casefold() in excluded:
            out.counts[f"event in an excluded calendar ({cal_name})"] += 1
        else:
            kept.append((cal_name, rec))

    account, account_how = _account(cals, target, settings)
    records = sorted((rec for _, rec in kept),
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    used = {(p["kind"], p["value"]) for r in records for p in r["participants"]}
    handles = [{"kind": k, "value": v, "display_name": out.names.get((k, v))}
               for k, v in sorted(used)]
    organizers = Counter(p["value"] for r in records for p in r["participants"]
                         if p["role"] == "organizer" and p["kind"] == "email")
    owner = account if account and _person_email(account) else \
        (organizers.most_common(1)[0][0] if organizers else None)

    per_cal = Counter({c.name: 0 for c in cals})
    per_cal.update(cal_name for cal_name, _ in kept)
    facts = {
        "calendars": [{"name": n, "events": per_cal[n]} for n in per_cal],
        "expanded_through": horizon.isoformat(),
        "horizon_days": horizon_days,
        "series": out.series,
        "occurrences": out.occurrences,
        "owner": owner,
        "account_from": account_how,
    }
    extra = {}
    if out.mirror_off:
        facts["not_mirroring"] = sorted(out.mirror_off)
    else:
        extra = {"present_ids": out.present, "mirror_streams": list(streams)}
    return base.empty_scan(
        name,
        account=account,
        source_file=target.name,
        source_hash=_content_hash(target, members),
        records=records,
        handles=handles,
        unmapped=[{"what": k, "count": v} for k, v in sorted(out.unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(out.counts.items())],
        facts=facts,
        target_kind=kind,
        today=today.isoformat(),
        owner=owner,
        **extra)


# ---------------------------------------------------------------- report

def _sample(recs: list[dict], today: str) -> str:
    past = [r for r in recs if r["journal_date"] <= today]
    r = max(past or recs, key=lambda x: (x["started_at"], x["external_id"]))
    title = " ".join(r["title"].split())
    if len(title) > 60:
        title = title[:59] + "…"
    return f"{r['journal_date']} {title}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    where = {"file": "calendar file", "folder": "folder", "zip": "zip",
             "takeout": "Google Takeout"}
    facts = scan.get("facts") or {}
    cals = facts.get("calendars") or []
    lines = [f"calendar: {scan['source_file']} ({where.get(scan['target_kind'], '?')}, "
             f"{len(cals)} calendar(s): "
             + ", ".join(f"{c['name']} ({c['events']})" for c in cals) + ")"]
    if scan["account"]:
        lines.append(f"  account: {scan['account']} (from {facts.get('account_from')})")
    lines.append(f"  repeating events expanded through {facts.get('expanded_through')} "
                 f"(today + {facts.get('horizon_days')} days): {facts.get('series', 0)} "
                 f"series, {facts.get('occurrences', 0)} occurrence(s)")

    by_stream: dict[str, dict] = {}
    for rec in plan["new"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["new"].append(rec)
    for _, rec, _ in plan["updates"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": []})["upd"].append(rec)
    for stream in sorted(by_stream):
        info = by_stream[stream]
        recs = info["new"] or info["upd"]
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        lines.append(f"  {stream}: {len(info['new'])} new, {len(info['upd'])} changed "
                     f"| {span} | e.g. {_sample(recs, scan.get('today') or '9999')}")
    if not by_stream and not plan.get("removed"):
        lines.append("  nothing new — the export matches the database")
    lines += base.removal_lines(plan, "no longer in the calendar (deleted or cancelled)",
                                lambda row: row["title"])
    # A calendar left out of a later export (Takeout lets you untick one)
    # looks exactly like every one of its events deleted; say so by name.
    here = {c["name"] for c in cals}
    gone = Counter()
    for row in plan.get("removed") or []:
        try:
            cal_name = json.loads(row["value_json"] or "{}").get("calendar")
        except (TypeError, ValueError):
            cal_name = None
        if cal_name and cal_name not in here:
            gone[cal_name] += 1
    if gone:
        lines.append("  WARNING: " + ", ".join(f"{cal_name} ({count})"
                                               for cal_name, count in sorted(gone.items()))
                     + " — calendar(s) not in this export at all; if they still exist, "
                       "re-export with them included instead of applying")
    if facts.get("not_mirroring"):
        lines.append("  not mirroring deletions this run: the export holds "
                     + " and ".join(facts["not_mirroring"])
                     + " — what it cannot read is not evidence of a deletion")
    elif not scan["account"]:
        lines.append(f"  to mirror deletions: {facts.get('account_from')}")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate occurrence(s) inside the export")
    if tail:
        lines.append("  " + ", ".join(tail))

    people = [h for h in scan["handles"] if h["kind"] in ("email", "phone")]
    places = [h for h in scan["handles"] if h["kind"] == "place_id"]
    if people or places:
        named = sum(1 for h in people if h["display_name"])
        lines.append(f"  handles: {len(people)} people ({named} named by their invites), "
                     f"{len(places)} place(s) — review with 'lifecrm handles list --suggest'")
    if scan.get("owner"):
        lines.append(f"  your own address is likely {scan['owner']} — link it to yourself "
                     f"once: lifecrm handles link email:{scan['owner']} --entity <id>")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:24]}…: differs "
                     f"from the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not understood: {u['count']} × {u['what']}")
    return lines

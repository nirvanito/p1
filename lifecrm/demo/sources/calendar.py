"""Calendar sample exports: a Google Takeout (android) and an Apple Calendar
.ics (iphone, windows-iphone), all rendering `persona.CALENDAR`.

The combos get their calendars the way their owners actually would:

  android  takeout-<anchor>T120000Z-001.zip — Google Takeout, which is where
           a Samsung phone's events end up once its calendar syncs to the
           Google account. One .ics per calendar under Takeout/Calendar/:
             alex.rivera@example.com.ics      the primary calendar
             Family.ics                       birthdays (yearly) and family
             Holidays in United States.ics    one event per holiday, no rules
           Single events in UTC ('Z'), repeating ones with a TZID; Meet
           boilerplate in the 1:1's description; a boardroom resource
           among the board meeting's attendees; DTSTAMP = the export time.
  iphone   Home.ics — Calendar.app's File > Export of the Home calendar
           (iCloud, the iPhone's calendar). VTIMEZONE blocks, uppercase UUID
           UIDs, quoted CN/EMAIL parameters, X-APPLE-STRUCTURED-LOCATION with
           geo, one attendee written as urn:uuid with an EMAIL parameter.
  windows-iphone  the same Home calendar with no Mac to export it: shared
           as a Public Calendar on icloud.com and downloaded from its link
           as Home.ics. The events are the same Apple-written ones; only the
           calendar's PRODID is iCloud's.

All carry the same story: the weekly 1:1 with Priya (seven occurrences,
one of them edited — a RECURRENCE-ID override), the board meeting, dinners
at Veracruz with Jake, Maya's birthday at Uchi, the dentist, Launch week,
and — added here, consistent with the persona — the Lake Bled trip four
years back (an all-day span plus the rowboat with Jake, in Slovenia's zone),
everyone's birthday as a yearly all-day event, and Saturday's tentative plan
to try the new climbing gym with Sam.

Deterministic for (anchor, combo): no clock, no randomness — ids are hashes
of the persona's own keys and the zip's member times are the anchor's.
"""

from __future__ import annotations

import hashlib
import uuid
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ... import ics as _ics_mod
from .. import persona as P

OWNER = P.OWNER
HOME_TZ = P.TIMEZONE
TRIP_TZ = "Europe/Ljubljana"
GOOGLE_PRIMARY = OWNER.email


@dataclass
class _Ev:
    key: str
    title: str
    start: datetime | date           # aware local datetime, or a date (all-day)
    end: datetime | date             # exclusive for all-day events
    calendar: str = "primary"        # primary | family | holidays
    attendees: tuple[str, ...] = ()
    organizer: str | None = None     # person key
    place: P.Place | None = None
    description: str | None = None
    weekly_count: int | None = None
    yearly: bool = False
    status: str = "CONFIRMED"
    meet: str | None = None
    resource: str | None = None      # a room, on Google only
    edited: int | None = None        # occurrence index that was edited
    edited_description: str | None = None


# ---------------------------------------------------------------- the events

def _next_saturday(anchor: date) -> date:
    return anchor + timedelta(days=(5 - anchor.weekday()) % 7 or 7)


def _birthday_start(month: int, day: int, anchor: date) -> date:
    """The yearly series starts when Alex started keeping this calendar."""
    try:
        return date(anchor.year - 3, month, day)
    except ValueError:              # Feb 29 in a non-leap year
        return date(anchor.year - 3, month, 28)


def _events(anchor: date) -> list[_Ev]:
    out: list[_Ev] = []
    for ev in P.CALENDAR:
        if ev.all_day:
            start = P.day(anchor, ev.offset)
            # "Launch week": the work week the journal says is coming.
            end = start + timedelta(days=5 if "week" in ev.title.lower() else 1)
        else:
            start = P.local_dt(anchor, ev.offset, ev.start)
            end = start + timedelta(minutes=ev.minutes)
        organizer = "priya" if ev.uid in ("demo-1on1-priya", "demo-board-q4") \
            else ("me" if ev.attendees else None)
        out.append(_Ev(
            key=ev.uid, title=ev.title, start=start, end=end,
            calendar="family" if ev.uid == "demo-family-dinner" else "primary",
            attendees=ev.attendees, organizer=organizer,
            place=P.place(ev.place) if ev.place else None,
            description=ev.description, weekly_count=ev.weekly_count,
            meet="https://meet.google.com/qwe-rtyu-iop" if ev.weekly_count else None,
            resource="Acme HQ-3-Boardroom (12)" if ev.uid == "demo-board-q4" else None,
            edited=2 if ev.weekly_count else None,
            edited_description="Weekly 1:1 — deck review: lead with the customer "
                               "story, not the TAM slide" if ev.weekly_count else None))

    trip = P.bled_trip_days(anchor)
    out.append(_Ev("demo-bled-trip", "Lake Bled trip", trip[0],
                   trip[-1] + timedelta(days=1), attendees=("jake",), organizer="me",
                   place=P.place("bled"), description="Ljubljana, Lake Bled and back"))
    row = P.trip_dt(2, "15:00", anchor)
    out.append(_Ev("demo-bled-rowboat", "Rowboat to Bled island", row,
                   row + timedelta(minutes=90), attendees=("jake",), organizer="me",
                   place=P.place("bled")))

    for key in ("jake", "mom", "sam", "maya"):
        p = P.person(key)
        when = P.birthday_date(p, anchor)
        if when is None:
            continue
        first = _birthday_start(when.month, when.day, anchor)
        label = "Mom" if key == "mom" else p.first
        out.append(_Ev(f"demo-bday-{key}", f"{label}'s birthday", first,
                       first + timedelta(days=1), calendar="family", yearly=True))

    sat = _next_saturday(anchor)
    start = datetime(sat.year, sat.month, sat.day, 10, 0, tzinfo=ZoneInfo(HOME_TZ))
    out.append(_Ev("demo-new-gym-sam", "New climbing gym w/ Sam", start,
                   start + timedelta(hours=2), attendees=("sam",), organizer="me",
                   status="TENTATIVE", description="Sam wants to try it"))
    return out


def _nth_weekday(y: int, m: int, weekday: int, nth: int) -> date:
    if nth > 0:
        first = date(y, m, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))
    last = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _holidays(anchor: date) -> list[_Ev]:
    """Google's public holiday calendar: one plain event per holiday, around
    the demo window."""
    out = []
    for y in (anchor.year - 1, anchor.year, anchor.year + 1):
        days = [
            ("New Year's Day", date(y, 1, 1), "Public holiday"),
            ("Martin Luther King Jr. Day", _nth_weekday(y, 1, 0, 3), "Public holiday"),
            ("Memorial Day", _nth_weekday(y, 5, 0, -1), "Public holiday"),
            ("Independence Day", date(y, 7, 4), "Public holiday"),
            ("Labor Day", _nth_weekday(y, 9, 0, 1), "Public holiday"),
            ("Halloween", date(y, 10, 31), "Observance"),
            ("Thanksgiving Day", _nth_weekday(y, 11, 3, 4), "Public holiday"),
            ("Christmas Day", date(y, 12, 25), "Public holiday"),
        ]
        for title, d, kind in days:
            if -P.WINDOW_DAYS - 30 <= (d - anchor).days <= 120:
                desc = kind if kind == "Public holiday" else (
                    "Observance\nTo hide observances, go to Google Calendar Settings "
                    "> Holidays in United States")
                out.append(_Ev(f"holiday-{d.isoformat()}", title, d,
                               d + timedelta(days=1), calendar="holidays",
                               description=desc, status="CONFIRMED"))
    return out


# ---------------------------------------------------------------- iCalendar text

# The writer helpers live in lifecrm/ics.py, shared with the reminders
# calendar; the local names stay so the rendering below reads as before.
_esc, _fold, _ics, _utc, _local = (_ics_mod.esc, _ics_mod.fold, _ics_mod.render,
                                   _ics_mod.utc, _ics_mod.local)


def _created(ev: _Ev) -> datetime:
    """When the event was made: a week before it (a month for the trip)."""
    start = ev.start if isinstance(ev.start, datetime) else datetime(
        ev.start.year, ev.start.month, ev.start.day, 12, tzinfo=ZoneInfo(HOME_TZ))
    return start - timedelta(days=60 if "bled" in ev.key else 7, hours=3)


def _weekday_code(d: date) -> str:
    return ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[d.weekday()]


_VTIMEZONES = {
    "America/Chicago": [
        "BEGIN:VTIMEZONE", "TZID:America/Chicago",
        "BEGIN:DAYLIGHT", "TZOFFSETFROM:-0600", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "DTSTART:20070311T020000", "TZNAME:CDT", "TZOFFSETTO:-0500", "END:DAYLIGHT",
        "BEGIN:STANDARD", "TZOFFSETFROM:-0500", "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "DTSTART:20071104T020000", "TZNAME:CST", "TZOFFSETTO:-0600", "END:STANDARD",
        "END:VTIMEZONE"],
    "Europe/Ljubljana": [
        "BEGIN:VTIMEZONE", "TZID:Europe/Ljubljana",
        "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
        "DTSTART:19810329T020000", "TZNAME:CEST", "TZOFFSETTO:+0200", "END:DAYLIGHT",
        "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
        "DTSTART:19961027T030000", "TZNAME:CET", "TZOFFSETTO:+0100", "END:STANDARD",
        "END:VTIMEZONE"],
}


def _zone_name(dt: datetime) -> str:
    return getattr(dt.tzinfo, "key", HOME_TZ)


# ---------------------------------------------------------------- Google

def _google_uid(key: str) -> str:
    return hashlib.sha1(f"google|{key}".encode()).hexdigest()[:26] + "@google.com"


def _google_people(ev: _Ev) -> list[str]:
    if ev.organizer is None:
        return []
    org = P.person(ev.organizer)
    lines = [f"ORGANIZER;CN={org.name}:mailto:{org.email}"]
    keys = ["me"] + [k for k in ev.attendees if k != "me"]
    for k in keys:
        p = P.person(k)
        # Google writes the calendar owner's CN as the bare address.
        cn = p.email if k == "me" else p.name
        lines.append(f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
                     f"PARTSTAT={'ACCEPTED' if ev.status != 'TENTATIVE' else 'TENTATIVE'};"
                     f"CN={cn};X-NUM-GUESTS=0:mailto:{p.email}")
    if ev.resource:
        lines.append(f"ATTENDEE;CUTYPE=RESOURCE;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;"
                     f"CN={ev.resource};X-NUM-GUESTS=0:mailto:"
                     f"c_188acme3boardroom@resource.calendar.google.com")
    return lines


def _google_description(ev: _Ev, text: str | None) -> str | None:
    if not ev.meet:
        return text
    rule = "-::~:~::~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~:~::~:~::-"
    return ((text + "\n\n") if text else "") + (
        f"{rule}\nJoin with Google Meet: {ev.meet}\n\nLearn more about Meet at: "
        f"https://support.google.com/a/users/answer/9282720\n\n"
        f"Please do not edit this section.\n{rule}")


def _google_location(place: P.Place) -> str:
    return ", ".join(x for x in (place.name, place.address or place.city) if x)


def _google_event(ev: _Ev, stamp: str, *, override_of: datetime | None = None,
                  start: datetime | date | None = None, modified: datetime | None = None,
                  description: str | None = None) -> list[str]:
    start = start if start is not None else ev.start
    end = ev.end if override_of is None else start + (ev.end - ev.start)
    lines = ["BEGIN:VEVENT"]
    if isinstance(start, datetime):
        if ev.weekly_count or override_of is not None:
            zone = _zone_name(start)
            lines += [f"DTSTART;TZID={zone}:{_local(start)}",
                      f"DTEND;TZID={zone}:{_local(end)}"]
        else:
            lines += [f"DTSTART:{_utc(start)}", f"DTEND:{_utc(end)}"]
    else:
        lines += [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
    if override_of is None and ev.weekly_count:
        lines.append(f"RRULE:FREQ=WEEKLY;WKST=SU;COUNT={ev.weekly_count};"
                     f"BYDAY={_weekday_code(ev.start)}")
    if override_of is None and ev.yearly:
        lines.append("RRULE:FREQ=YEARLY")
    lines.append(f"DTSTAMP:{stamp}")
    lines += _google_people(ev)
    lines.append(f"UID:{_google_uid(ev.key)}")
    if override_of is not None:
        lines.append(f"RECURRENCE-ID;TZID={_zone_name(override_of)}:{_local(override_of)}")
    if ev.meet:
        lines.append(f"X-GOOGLE-CONFERENCE:{ev.meet}")
    created = _created(ev)
    lines.append(f"CREATED:{_utc(created)}")
    desc = _google_description(ev, description if description is not None
                               else ev.description)
    lines.append(f"DESCRIPTION:{_esc(desc)}" if desc else "DESCRIPTION:")
    lines.append(f"LAST-MODIFIED:{_utc(modified or created + timedelta(hours=1))}")
    lines.append(f"LOCATION:{_esc(_google_location(ev.place))}" if ev.place else "LOCATION:")
    lines += ["SEQUENCE:0", f"STATUS:{ev.status}", f"SUMMARY:{_esc(ev.title)}",
              "TRANSP:OPAQUE" if ev.calendar != "holidays" else "TRANSP:TRANSPARENT"]
    if ev.calendar != "holidays":
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY",
                  "DESCRIPTION:This is an event reminder", "TRIGGER:-P0DT0H30M0S",
                  "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


def _google_calendar(calname: str, events: list[_Ev], anchor: date) -> bytes:
    stamp = f"{anchor:%Y%m%d}T120000Z"          # Google's DTSTAMP is the export time
    lines = ["BEGIN:VCALENDAR", "PRODID:-//Google Inc//Google Calendar 70.9054//EN",
             "VERSION:2.0", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(calname)}", f"X-WR-TIMEZONE:{HOME_TZ}"]
    if any(ev.weekly_count for ev in events):
        lines += _VTIMEZONES[HOME_TZ]
    for ev in events:
        lines += _google_event(ev, stamp)
        if ev.edited is not None:
            slot = ev.start + timedelta(weeks=ev.edited)
            lines += _google_event(ev, stamp, override_of=slot, start=slot,
                                   modified=slot - timedelta(days=1),
                                   description=ev.edited_description)
    lines.append("END:VCALENDAR")
    return _ics(lines)


def _google_takeout(dest: Path, anchor: date) -> Path:
    events = _events(anchor)
    calendars = {
        f"{GOOGLE_PRIMARY}.ics": (GOOGLE_PRIMARY,
                                  [e for e in events if e.calendar == "primary"]),
        "Family.ics": ("Family", [e for e in events if e.calendar == "family"]),
        "Holidays in United States.ics": ("Holidays in United States",
                                          _holidays(anchor)),
    }
    path = dest / f"takeout-{anchor:%Y%m%d}T120000Z-001.zip"
    stamp = (anchor.year, anchor.month, anchor.day, 12, 0, 0)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        def put(member: str, data: bytes) -> None:
            info = zipfile.ZipInfo(member, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
        put("Takeout/archive_browser.html",
            b"<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Archive "
            b"Overview</title></head><body><h1>Your Google data archive</h1>"
            b"<p>Calendar: 3 calendars</p></body></html>")
        for member, (calname, evs) in calendars.items():
            put(f"Takeout/Calendar/{member}", _google_calendar(calname, evs, anchor))
    return path


# ---------------------------------------------------------------- Apple

def _apple_uid(key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lifecrm-demo/{key}")).upper()


def _apple_attendee(key: str, role: str, partstat: str) -> str:
    p = P.person(key)
    # Calendar.app writes some invitees by their contact-card uuid, with the
    # address only in EMAIL — the reader must not depend on mailto:.
    value = f"urn:uuid:{_apple_uid('person-' + key).lower()}" if key == "mom" \
        else f"mailto:{p.email}"
    return (f'ATTENDEE;CN="{p.name}";CUTYPE=INDIVIDUAL;EMAIL="{p.email}";'
            f"PARTSTAT={partstat};ROLE={role}:{value}")


def _apple_people(ev: _Ev) -> list[str]:
    if ev.organizer is None:
        return []
    org = P.person(ev.organizer)
    lines = [f'ORGANIZER;CN="{org.name}";EMAIL="{org.email}":mailto:{org.email}']
    partstat = "TENTATIVE" if ev.status == "TENTATIVE" else "ACCEPTED"
    for k in ["me"] + [k for k in ev.attendees if k != "me"]:
        role = "CHAIR" if k == ev.organizer else "REQ-PARTICIPANT"
        stat = "NEEDS-ACTION" if k == "mom" else partstat
        lines.append(_apple_attendee(k, role, stat))
    return lines


def _apple_location(place: P.Place) -> list[str]:
    addr = place.address or place.city
    return [f"LOCATION:{_esc(place.name + chr(10) + addr)}",
            f'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="{addr}";'
            f'X-APPLE-RADIUS=70.58;X-TITLE="{place.name}":geo:{place.lat},{place.lon}']


def _apple_event(ev: _Ev, *, override_of: datetime | None = None,
                 start: datetime | date | None = None, modified: datetime | None = None,
                 description: str | None = None) -> list[str]:
    start = start if start is not None else ev.start
    end = ev.end if override_of is None else start + (ev.end - ev.start)
    created = _created(ev)
    lines = ["BEGIN:VEVENT", "TRANSP:OPAQUE"]
    if isinstance(start, datetime):
        zone = _zone_name(start)
        lines.append(f"DTEND;TZID={zone}:{_local(end)}")
    else:
        lines.append(f"DTEND;VALUE=DATE:{end:%Y%m%d}")
    lines += [f"UID:{_apple_uid(ev.key)}",
              f"DTSTAMP:{_utc(modified or created + timedelta(hours=1))}"]
    if ev.place:
        lines += _apple_location(ev.place)
    desc = description if description is not None else ev.description
    if desc:
        lines.append(f"DESCRIPTION:{_esc(desc)}")
    lines += ["X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC", "SEQUENCE:1"]
    if override_of is None and ev.weekly_count:
        lines.append(f"RRULE:FREQ=WEEKLY;INTERVAL=1;COUNT={ev.weekly_count}")
    if override_of is None and ev.yearly:
        lines.append("RRULE:FREQ=YEARLY;INTERVAL=1")
    if override_of is not None:
        lines.append(f"RECURRENCE-ID;TZID={_zone_name(override_of)}:{_local(override_of)}")
    lines += [f"SUMMARY:{_esc(ev.title)}",
              f"LAST-MODIFIED:{_utc(modified or created + timedelta(hours=1))}",
              f"CREATED:{_utc(created)}"]
    if isinstance(start, datetime):
        lines.append(f"DTSTART;TZID={_zone_name(start)}:{_local(start)}")
    else:
        lines.append(f"DTSTART;VALUE=DATE:{start:%Y%m%d}")
    if ev.status != "CONFIRMED":
        lines.append(f"STATUS:{ev.status}")
    lines += _apple_people(ev)
    if not ev.yearly:
        alarm = _apple_uid(ev.key + "-alarm")
        lines += ["BEGIN:VALARM", f"X-WR-ALARMUID:{alarm}", f"UID:{alarm}",
                  "TRIGGER:-PT15M", "ATTACH;VALUE=URI:Chord", "ACTION:AUDIO",
                  "END:VALARM"]
    lines.append("END:VEVENT")
    return lines


MAC_PRODID = "-//Apple Inc.//macOS 15.1//EN"
# The feed iCloud.com serves for a calendar shared as a Public Calendar; the
# events in it are the ones the iPhone and iCloud wrote. Its exact PRODID is
# unverified.
ICLOUD_PRODID = "-//Apple Inc.//iCloud Calendar Server//EN"


def _apple_export(dest: Path, anchor: date, prodid: str = MAC_PRODID) -> Path:
    events = _events(anchor)
    lines = ["BEGIN:VCALENDAR", "METHOD:PUBLISH", "VERSION:2.0", "X-WR-CALNAME:Home",
             f"PRODID:{prodid}", "X-APPLE-CALENDAR-COLOR:#34AADC",
             f"X-WR-TIMEZONE:{HOME_TZ}", "CALSCALE:GREGORIAN"]
    lines += _VTIMEZONES[HOME_TZ] + _VTIMEZONES[TRIP_TZ]
    for ev in events:
        lines += _apple_event(ev)
        if ev.edited is not None:
            slot = ev.start + timedelta(weeks=ev.edited)
            lines += _apple_event(ev, override_of=slot, start=slot,
                                  modified=slot - timedelta(days=1),
                                  description=ev.edited_description)
    lines.append("END:VCALENDAR")
    path = dest / "Home.ics"
    path.write_bytes(_ics(lines))
    return path


# ---------------------------------------------------------------- entry point

def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """Write this combo's calendar export under `dest`; return what to import."""
    if combo not in P.COMBOS:
        raise ValueError(f"calendar has no sample for combo '{combo}' "
                         f"(expected one of: {', '.join(P.COMBOS)})")
    dest.mkdir(parents=True, exist_ok=True)
    if combo == "android":
        return _google_takeout(dest, anchor)
    if P.on_mac(combo):
        return _apple_export(dest, anchor)
    # Windows + iPhone: no Calendar.app — the Home calendar's iCloud feed.
    return _apple_export(dest, anchor, prodid=ICLOUD_PRODID)

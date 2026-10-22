"""iCalendar (RFC 5545) reading, written by hand for the calendar connector.

A helper, not a connector (the leading underscore keeps it out of the
registry). The standard library has no iCalendar parser and the project takes
no dependency for a reader, so this is the part of the RFC that calendar
exports actually use — Google Takeout and Google Calendar's own export, Apple
Calendar's File > Export, Outlook — not the whole of it:

- **Content lines.** Unfolding is done on bytes, before decoding: the RFC
  forbids splitting a multi-byte character across a fold, and some producers
  do it anyway. Parameters may be quoted (`CN="Morales, Jake"`) and carry
  RFC 6868 escapes; TEXT values carry `\\n \\, \\; \\\\`.
- **Structure.** A BEGIN/END tree. A file whose BEGIN/END do not balance is
  refused as truncated rather than read partially: to an import that mirrors
  the calendar, a half-downloaded file would look like deleted events.
- **Time.** DATE (all-day), DATE-TIME in UTC, with a TZID, or floating.
  A TZID resolves to zoneinfo: IANA names directly (the VTIMEZONE block is
  then ignored — zoneinfo knows the zone's whole history, the block only a
  rule or two), the Windows zone names Outlook writes, Outlook.com's
  "(UTC-06:00) Central Time (US & Canada)" display names, and vendor-prefixed
  paths such as Thunderbird's "/mozilla.org/20050126_1/America/New_York".
  Anything else falls back to its VTIMEZONE's standard offset, and says so.
- **Recurrence.** RRULE with FREQ DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL,
  COUNT, UNTIL, WKST, BYDAY (plain weekdays, and nth weekdays where the RFC
  allows them), BYMONTHDAY, BYMONTH and BYSETPOS. Occurrences are generated in
  the event's own wall-clock time, so a weekly 10:00 meeting stays at 10:00
  across a daylight-saving change. Anything else raises `Unsupported` naming
  the part, and the caller decides what that means.

Nothing here knows about observations; `connectors/calendar.py` maps the
parsed events to records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.util import DomainError

__all__ = ["Prop", "Component", "Unsupported", "Rule", "parse", "unescape",
           "parse_value", "parse_values", "parse_duration", "Zones",
           "zone_for", "parse_rule", "expand", "WINDOWS_ZONES"]

# A series needing more periods than this before reaching the horizon is not
# a real calendar's (a daily event since the year 1604 is ~155,000).
MAX_PERIODS = 200_000


class Unsupported(ValueError):
    """A repeat rule this module cannot expand; the message names the part."""


# ---------------------------------------------------------------- content lines

@dataclass
class Prop:
    name: str
    params: dict[str, list[str]]
    value: str
    line: int = 0

    def param(self, key: str) -> str | None:
        values = self.params.get(key.upper())
        return values[0] if values else None


@dataclass
class Component:
    name: str
    props: list[Prop] = field(default_factory=list)
    children: list["Component"] = field(default_factory=list)
    line: int = 0

    def first(self, name: str) -> Prop | None:
        name = name.upper()
        return next((p for p in self.props if p.name == name), None)

    def all(self, name: str) -> list[Prop]:
        name = name.upper()
        return [p for p in self.props if p.name == name]

    def text(self, name: str) -> str | None:
        """A TEXT property, unescaped and stripped; None when absent or blank."""
        p = self.first(name)
        if p is None:
            return None
        value = unescape(p.value).strip()
        return value or None

    def kids(self, name: str) -> list["Component"]:
        name = name.upper()
        return [c for c in self.children if c.name == name]


def _lines(raw: bytes, source: str) -> list[tuple[int, str]]:
    """Logical content lines with the physical line number each starts on."""
    if raw.startswith(b"\xef\xbb\xbf"):          # utf-8-sig: Notepad, PowerShell
        raw = raw[3:]
    data = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    logical: list[tuple[int, bytearray]] = []
    for n, physical in enumerate(data.split(b"\n"), start=1):
        if physical[:1] in (b" ", b"\t") and logical:
            logical[-1][1].extend(physical[1:])   # a fold: drop the break and one blank
        elif physical.strip():
            logical.append((n, bytearray(physical)))
    out = []
    for n, buf in logical:
        try:
            out.append((n, buf.decode("utf-8")))
        except UnicodeDecodeError as e:
            raise DomainError(f"{source} line {n} is not valid UTF-8 "
                              f"(byte {e.start}: {bytes(buf[e.start:e.start + 1])!r})"
                              ) from e
    return out


def _param_unescape(value: str) -> str:
    """RFC 6868: ^n newline, ^' double quote, ^^ caret."""
    if "^" not in value:
        return value
    return re.sub(r"\^([n'^])",
                  lambda m: {"n": "\n", "'": '"', "^": "^"}[m.group(1)], value)


def parse_line(text: str, line: int = 0) -> Prop | None:
    """NAME;PARAM=a,"b:c";P2=x:VALUE -> Prop, or None if the line is malformed.

    The value starts at the first colon outside quotes, so a quoted parameter
    may hold colons, commas and semicolons (Apple's X-ADDRESS does)."""
    i, n = 0, len(text)
    while i < n and text[i] not in ";:":
        i += 1
    name = text[:i].strip().upper()
    if not name or i >= n:
        return None
    params: dict[str, list[str]] = {}
    while i < n and text[i] == ";":
        i += 1
        j = i
        while j < n and text[j] not in "=;:":
            j += 1
        pname = text[i:j].strip().upper()
        values: list[str] = []
        if j < n and text[j] == "=":
            j += 1
            while True:
                if j < n and text[j] == '"':
                    k = text.find('"', j + 1)
                    if k == -1:
                        return None
                    values.append(_param_unescape(text[j + 1:k]))
                    j = k + 1
                else:
                    k = j
                    while k < n and text[k] not in ",;:":
                        k += 1
                    values.append(_param_unescape(text[j:k]))
                    j = k
                if j < n and text[j] == ",":
                    j += 1
                    continue
                break
        if pname:
            params.setdefault(pname, []).extend(values)
        i = j
    if i >= n or text[i] != ":":
        return None
    return Prop(name, params, text[i + 1:], line)


def unescape(value: str) -> str:
    """TEXT escapes: \\n or \\N newline, \\, \\; \\\\ themselves. An unknown
    escape keeps its character — being lenient here loses nothing."""
    if "\\" not in value:
        return value
    out, i, n = [], 0, len(value)
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n:
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse(raw: bytes, source: str) -> tuple[list[Component], int]:
    """(top-level components, count of malformed lines skipped).

    Raises DomainError when the file is not iCalendar at all or its
    BEGIN/END do not balance — naming the line, and what it saw."""
    lines = _lines(raw, source)
    roots: list[Component] = []
    stack: list[Component] = []
    malformed = 0
    for n, text in lines:
        prop = parse_line(text, n)
        if prop is None:
            malformed += 1
            continue
        if prop.name == "BEGIN":
            comp = Component(prop.value.strip().upper(), line=n)
            (stack[-1].children if stack else roots).append(comp)
            stack.append(comp)
        elif prop.name == "END":
            want = prop.value.strip().upper()
            if not stack or stack[-1].name != want:
                open_ = f"BEGIN:{stack[-1].name} from line {stack[-1].line}" \
                    if stack else "nothing (no component is open)"
                raise DomainError(f"{source} line {n}: END:{want} does not close "
                                  f"{open_} — the file is damaged")
            stack.pop()
        elif stack:
            stack[-1].props.append(prop)
        else:
            malformed += 1                       # a property outside any component
    if stack:
        raise DomainError(f"{source} ends inside BEGIN:{stack[-1].name} (opened at "
                          f"line {stack[-1].line}) — the file looks truncated; "
                          f"download the export again")
    if not any(c.name == "VCALENDAR" for c in roots):
        start = lines[0][1][:40] if lines else "(empty)"
        raise DomainError(f"{source} has no BEGIN:VCALENDAR — is this an iCalendar "
                          f"(.ics) file? (starts with: {start!r})")
    return roots, malformed


# ---------------------------------------------------------------- time zones

# Windows zone ids, as Outlook and Exchange write them in TZID, to the IANA
# zone CLDR maps each to ("001" territory).
WINDOWS_ZONES = {
    "dateline standard time": "Etc/GMT+12",
    "utc-11": "Etc/GMT+11",
    "aleutian standard time": "America/Adak",
    "hawaiian standard time": "Pacific/Honolulu",
    "marquesas standard time": "Pacific/Marquesas",
    "alaskan standard time": "America/Anchorage",
    "utc-09": "Etc/GMT+9",
    "pacific standard time (mexico)": "America/Tijuana",
    "utc-08": "Etc/GMT+8",
    "pacific standard time": "America/Los_Angeles",
    "us mountain standard time": "America/Phoenix",
    "mountain standard time (mexico)": "America/Mazatlan",
    "mountain standard time": "America/Denver",
    "yukon standard time": "America/Whitehorse",
    "central america standard time": "America/Guatemala",
    "central standard time": "America/Chicago",
    "easter island standard time": "Pacific/Easter",
    "central standard time (mexico)": "America/Mexico_City",
    "canada central standard time": "America/Regina",
    "sa pacific standard time": "America/Bogota",
    "eastern standard time (mexico)": "America/Cancun",
    "eastern standard time": "America/New_York",
    "haiti standard time": "America/Port-au-Prince",
    "cuba standard time": "America/Havana",
    "us eastern standard time": "America/Indiana/Indianapolis",
    "turks and caicos standard time": "America/Grand_Turk",
    "paraguay standard time": "America/Asuncion",
    "atlantic standard time": "America/Halifax",
    "venezuela standard time": "America/Caracas",
    "central brazilian standard time": "America/Cuiaba",
    "sa western standard time": "America/La_Paz",
    "pacific sa standard time": "America/Santiago",
    "newfoundland standard time": "America/St_Johns",
    "tocantins standard time": "America/Araguaina",
    "e. south america standard time": "America/Sao_Paulo",
    "sa eastern standard time": "America/Cayenne",
    "argentina standard time": "America/Argentina/Buenos_Aires",
    "greenland standard time": "America/Nuuk",
    "montevideo standard time": "America/Montevideo",
    "magallanes standard time": "America/Punta_Arenas",
    "saint pierre standard time": "America/Miquelon",
    "bahia standard time": "America/Bahia",
    "utc-02": "Etc/GMT+2",
    "azores standard time": "Atlantic/Azores",
    "cape verde standard time": "Atlantic/Cape_Verde",
    "utc": "Etc/UTC",
    "gmt standard time": "Europe/London",
    "greenwich standard time": "Atlantic/Reykjavik",
    "sao tome standard time": "Africa/Sao_Tome",
    "morocco standard time": "Africa/Casablanca",
    "w. europe standard time": "Europe/Berlin",
    "central europe standard time": "Europe/Budapest",
    "romance standard time": "Europe/Paris",
    "central european standard time": "Europe/Warsaw",
    "w. central africa standard time": "Africa/Lagos",
    "jordan standard time": "Asia/Amman",
    "gtb standard time": "Europe/Bucharest",
    "middle east standard time": "Asia/Beirut",
    "egypt standard time": "Africa/Cairo",
    "e. europe standard time": "Europe/Chisinau",
    "syria standard time": "Asia/Damascus",
    "west bank standard time": "Asia/Hebron",
    "south africa standard time": "Africa/Johannesburg",
    "fle standard time": "Europe/Kyiv",
    "israel standard time": "Asia/Jerusalem",
    "south sudan standard time": "Africa/Juba",
    "kaliningrad standard time": "Europe/Kaliningrad",
    "sudan standard time": "Africa/Khartoum",
    "libya standard time": "Africa/Tripoli",
    "namibia standard time": "Africa/Windhoek",
    "arabic standard time": "Asia/Baghdad",
    "turkey standard time": "Europe/Istanbul",
    "arab standard time": "Asia/Riyadh",
    "belarus standard time": "Europe/Minsk",
    "russian standard time": "Europe/Moscow",
    "e. africa standard time": "Africa/Nairobi",
    "volgograd standard time": "Europe/Volgograd",
    "iran standard time": "Asia/Tehran",
    "arabian standard time": "Asia/Dubai",
    "astrakhan standard time": "Europe/Astrakhan",
    "azerbaijan standard time": "Asia/Baku",
    "russia time zone 3": "Europe/Samara",
    "mauritius standard time": "Indian/Mauritius",
    "saratov standard time": "Europe/Saratov",
    "georgian standard time": "Asia/Tbilisi",
    "caucasus standard time": "Asia/Yerevan",
    "afghanistan standard time": "Asia/Kabul",
    "west asia standard time": "Asia/Tashkent",
    "ekaterinburg standard time": "Asia/Yekaterinburg",
    "pakistan standard time": "Asia/Karachi",
    "qyzylorda standard time": "Asia/Qyzylorda",
    "india standard time": "Asia/Kolkata",
    "sri lanka standard time": "Asia/Colombo",
    "nepal standard time": "Asia/Kathmandu",
    "central asia standard time": "Asia/Almaty",
    "bangladesh standard time": "Asia/Dhaka",
    "omsk standard time": "Asia/Omsk",
    "myanmar standard time": "Asia/Yangon",
    "se asia standard time": "Asia/Bangkok",
    "altai standard time": "Asia/Barnaul",
    "w. mongolia standard time": "Asia/Hovd",
    "north asia standard time": "Asia/Krasnoyarsk",
    "n. central asia standard time": "Asia/Novosibirsk",
    "tomsk standard time": "Asia/Tomsk",
    "china standard time": "Asia/Shanghai",
    "north asia east standard time": "Asia/Irkutsk",
    "singapore standard time": "Asia/Singapore",
    "w. australia standard time": "Australia/Perth",
    "taipei standard time": "Asia/Taipei",
    "ulaanbaatar standard time": "Asia/Ulaanbaatar",
    "aus central w. standard time": "Australia/Eucla",
    "transbaikal standard time": "Asia/Chita",
    "tokyo standard time": "Asia/Tokyo",
    "north korea standard time": "Asia/Pyongyang",
    "korea standard time": "Asia/Seoul",
    "yakutsk standard time": "Asia/Yakutsk",
    "cen. australia standard time": "Australia/Adelaide",
    "aus central standard time": "Australia/Darwin",
    "e. australia standard time": "Australia/Brisbane",
    "aus eastern standard time": "Australia/Sydney",
    "west pacific standard time": "Pacific/Port_Moresby",
    "tasmania standard time": "Australia/Hobart",
    "vladivostok standard time": "Asia/Vladivostok",
    "lord howe standard time": "Australia/Lord_Howe",
    "bougainville standard time": "Pacific/Bougainville",
    "russia time zone 10": "Asia/Srednekolymsk",
    "magadan standard time": "Asia/Magadan",
    "norfolk standard time": "Pacific/Norfolk",
    "sakhalin standard time": "Asia/Sakhalin",
    "central pacific standard time": "Pacific/Guadalcanal",
    "russia time zone 11": "Asia/Kamchatka",
    "new zealand standard time": "Pacific/Auckland",
    "utc+12": "Etc/GMT-12",
    "fiji standard time": "Pacific/Fiji",
    "chatham islands standard time": "Pacific/Chatham",
    "utc+13": "Etc/GMT-13",
    "tonga standard time": "Pacific/Tongatapu",
    "samoa standard time": "Pacific/Apia",
    "line islands standard time": "Pacific/Kiritimati",
}

# Outlook.com and some Exchange exports write the Windows *display* name,
# "(UTC-06:00) Central Time (US & Canada)". The offset prefix is stripped and
# the rest looked up here — the common ones only; the rest fall back.
_DISPLAY_ZONES = {
    "central time (us & canada)": "America/Chicago",
    "eastern time (us & canada)": "America/New_York",
    "mountain time (us & canada)": "America/Denver",
    "pacific time (us & canada)": "America/Los_Angeles",
    "arizona": "America/Phoenix",
    "alaska": "America/Anchorage",
    "hawaii": "Pacific/Honolulu",
    "atlantic time (canada)": "America/Halifax",
    "guadalajara, mexico city, monterrey": "America/Mexico_City",
    "coordinated universal time": "Etc/UTC",
    "dublin, edinburgh, lisbon, london": "Europe/London",
    "amsterdam, berlin, bern, rome, stockholm, vienna": "Europe/Berlin",
    "belgrade, bratislava, budapest, ljubljana, prague": "Europe/Budapest",
    "brussels, copenhagen, madrid, paris": "Europe/Paris",
    "sarajevo, skopje, warsaw, zagreb": "Europe/Warsaw",
    "athens, bucharest": "Europe/Bucharest",
    "helsinki, kyiv, riga, sofia, tallinn, vilnius": "Europe/Kyiv",
    "chennai, kolkata, mumbai, new delhi": "Asia/Kolkata",
    "beijing, chongqing, hong kong, urumqi": "Asia/Shanghai",
    "osaka, sapporo, tokyo": "Asia/Tokyo",
    "canberra, melbourne, sydney": "Australia/Sydney",
}

_UTC_PREFIX = re.compile(r"^\(\s*(?:utc|gmt)\s*[+-]?[\d:]*\s*\)\s*", re.I)


def _try_zone(key: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def zone_for(tzid: str) -> ZoneInfo | None:
    """The zoneinfo zone a TZID names, or None when it names none we know."""
    name = tzid.strip().strip('"').strip()
    if not name:
        return None
    stripped = _UTC_PREFIX.sub("", name)
    candidates = [name, WINDOWS_ZONES.get(name.lower()),
                  _DISPLAY_ZONES.get(stripped.lower()),
                  WINDOWS_ZONES.get(stripped.lower())]
    parts = [p for p in name.split("/") if p]
    if len(parts) > 1:                  # /mozilla.org/20050126_1/America/New_York
        candidates += ["/".join(parts[-k:]) for k in (3, 2) if len(parts) >= k]
    for cand in candidates:
        if cand and ("/" in cand or cand in ("UTC", "GMT")):
            z = _try_zone(cand)
            if z is not None:
                return z
    # A bare name zoneinfo also knows ("EST5EDT", "Japan") — tried last, so a
    # Windows name never resolves to some coincidental legacy alias first.
    return _try_zone(name) if re.fullmatch(r"[A-Za-z0-9_+\-]+", name) else None


def _offset(text: str | None) -> timedelta | None:
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})(\d{2})?", (text or "").strip())
    if not m:
        return None
    sign = -1 if m.group(1) == "-" else 1
    return sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3)),
                            seconds=int(m.group(4) or 0))


class Zones:
    """TZID -> tzinfo for one VCALENDAR, remembering how each was read."""

    def __init__(self, vtimezones: list[Component]):
        self.blocks = {}
        for vt in vtimezones:
            tzid = vt.first("TZID")
            if tzid is not None and tzid.value.strip():
                self.blocks[tzid.value.strip()] = vt
        self.cache: dict[str, tzinfo | None] = {}
        self.notes: dict[str, str] = {}

    def get(self, tzid: str) -> tzinfo | None:
        key = tzid.strip().strip('"')
        if key in self.cache:
            return self.cache[key]
        z: tzinfo | None = zone_for(key)
        if z is not None and z.key != key:
            self.notes[key] = f"read as {z.key}"
        if z is None:
            z = self._from_block(key)
        self.cache[key] = z
        return z

    def note(self, tzid: str) -> str | None:
        """How a TZID was read, when it was not simply an IANA name."""
        return self.notes.get(tzid.strip().strip('"'))

    def _from_block(self, key: str) -> tzinfo | None:
        block = self.blocks.get(key)
        if block is None:
            self.notes[key] = "unknown and not described in the file — read as the home timezone"
            return None
        std = block.kids("STANDARD") or block.kids("DAYLIGHT")
        off = _offset(std[-1].first("TZOFFSETTO").value) if std and \
            std[-1].first("TZOFFSETTO") else None
        if off is None:
            self.notes[key] = "unknown, and its VTIMEZONE has no offset — read as the home timezone"
            return None
        dst = " (daylight saving ignored)" if block.kids("DAYLIGHT") and \
            block.kids("STANDARD") else ""
        sign = "-" if off < timedelta(0) else "+"
        mins = abs(int(off.total_seconds())) // 60
        self.notes[key] = (f"not a known zone; its VTIMEZONE's standard offset "
                           f"{sign}{mins // 60:02d}{mins % 60:02d} was used{dst}")
        return timezone(off)


# ---------------------------------------------------------------- values

_DT = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$")


def _one_value(text: str, is_date: bool, tz: tzinfo | None) -> date | datetime:
    s = text.strip().replace("-", "").replace(":", "")
    m = _DT.match(s)
    if not m:
        raise ValueError(f"'{text.strip()}' is not an iCalendar date or date-time")
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if m.group(4) is None or is_date:
        return date(y, mo, d)
    hh, mm, ss = int(m.group(4)), int(m.group(5)), int(m.group(6) or 0)
    dt = datetime(y, mo, d, hh, mm, min(ss, 59))      # 60 is a leap second
    if m.group(7):
        return dt.replace(tzinfo=timezone.utc)
    return dt.replace(tzinfo=tz) if tz is not None else dt


def parse_values(prop: Prop, zones: Zones | None) -> list[date | datetime]:
    """Every value of a DATE / DATE-TIME property (EXDATE and RDATE may list
    several). DATE -> date; UTC -> aware UTC; TZID -> aware in that zone (or
    naive, floating, when the zone is unknown); floating -> naive."""
    is_date = (prop.param("VALUE") or "").upper() == "DATE"
    tz = None
    tzid = prop.param("TZID")
    if tzid and zones is not None:
        tz = zones.get(tzid)
    return [_one_value(v, is_date, tz) for v in prop.value.split(",") if v.strip()]


def parse_value(prop: Prop, zones: Zones | None) -> date | datetime:
    values = parse_values(prop, zones)
    if not values:
        raise ValueError(f"{prop.name} has no value")
    return values[0]


_DURATION = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?"
                       r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(text: str) -> timedelta:
    s = text.strip().upper()
    m = _DURATION.match(s)
    if not m or s in ("P", "PT", "+P", "-P") or s.endswith("T"):
        raise ValueError(f"'{text.strip()}' is not an iCalendar duration")
    w, d, h, mi, sec = (int(g or 0) for g in m.groups()[1:])
    delta = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=sec)
    return -delta if m.group(1) == "-" else delta


# ---------------------------------------------------------------- recurrence

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
FREQS = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
_BYDAY = re.compile(r"^([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)$")


@dataclass(frozen=True)
class Rule:
    text: str
    freq: str
    interval: int = 1
    count: int | None = None
    until: date | datetime | None = None
    byday: tuple[tuple[int | None, int], ...] = ()
    bymonthday: tuple[int, ...] = ()
    bymonth: tuple[int, ...] = ()
    bysetpos: tuple[int, ...] = ()
    wkst: int = 0


def _ints(key: str, raw: str, lo: int, hi: int, signed: bool) -> tuple[int, ...]:
    out = []
    for part in raw.split(","):
        try:
            v = int(part)
        except ValueError:
            raise Unsupported(f"{key}={raw} is malformed") from None
        if not (lo <= abs(v) <= hi) or (v < 0 and not signed):
            raise Unsupported(f"{key}={raw} is out of range")
        out.append(v)
    return tuple(out)


def parse_rule(text: str, start: date | datetime) -> Rule:
    """RRULE text -> Rule, or Unsupported naming the part that is not.

    UNTIL is returned as parsed (date, aware UTC, or naive); `expand` wants
    it in the event's own frame, which the caller knows and this does not."""
    parts: dict[str, str] = {}
    for item in text.strip().split(";"):
        if not item.strip():
            continue
        key, sep, value = item.partition("=")
        if not sep:
            raise Unsupported(f"'{item}' is malformed")
        parts[key.strip().upper()] = value.strip().upper()
    freq = parts.pop("FREQ", None)
    if freq not in FREQS:
        raise Unsupported(f"FREQ={freq}" if freq else "no FREQ")
    timed = isinstance(start, datetime)
    kw: dict = {}
    for key, value in parts.items():
        if key.startswith("X-"):
            continue
        if key == "INTERVAL":
            kw["interval"] = _ints(key, value, 1, 10_000, False)[0]
        elif key == "COUNT":
            kw["count"] = _ints(key, value, 1, 10_000_000, False)[0]
        elif key == "UNTIL":
            try:
                kw["until"] = _one_value(value, False, None)
            except ValueError:
                raise Unsupported(f"UNTIL={value} is malformed") from None
        elif key == "WKST":
            if value not in WEEKDAYS:
                raise Unsupported(f"WKST={value} is malformed")
            kw["wkst"] = WEEKDAYS.index(value)
        elif key == "BYDAY":
            days = []
            for item in value.split(","):
                m = _BYDAY.match(item.strip())
                if not m:
                    raise Unsupported(f"BYDAY={value} is malformed")
                nth = int(m.group(1)) if m.group(1) else None
                if nth is not None and (nth == 0 or abs(nth) > 53):
                    raise Unsupported(f"BYDAY={value} is out of range")
                if nth is not None and freq in ("DAILY", "WEEKLY"):
                    raise Unsupported(f"BYDAY={value} with FREQ={freq}")
                days.append((nth, WEEKDAYS.index(m.group(2))))
            kw["byday"] = tuple(days)
        elif key == "BYMONTHDAY":
            if freq == "WEEKLY":
                raise Unsupported("BYMONTHDAY with FREQ=WEEKLY")
            kw["bymonthday"] = _ints(key, value, 1, 31, True)
        elif key == "BYMONTH":
            kw["bymonth"] = tuple(sorted(set(_ints(key, value, 1, 12, False))))
        elif key == "BYSETPOS":
            kw["bysetpos"] = _ints(key, value, 1, 366, True)
        elif key in ("BYHOUR", "BYMINUTE", "BYSECOND"):
            # Harmless when it only restates DTSTART's own time, which some
            # producers do; anything more would mean several times a day.
            own = {"BYHOUR": "hour", "BYMINUTE": "minute", "BYSECOND": "second"}[key]
            if not timed or value != str(getattr(start, own)):
                raise Unsupported(f"{key}={value}")
        else:
            raise Unsupported(f"{key}={value}" if value else key)
    return Rule(text=text.strip(), freq=freq, **kw)


def _days_in_month(y: int, m: int) -> int:
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return (nxt - date(y, m, 1)).days


def _nth(days: list[int], nth: int | None) -> list[int]:
    if nth is None:
        return days
    idx = nth - 1 if nth > 0 else len(days) + nth
    return [days[idx]] if 0 <= idx < len(days) else []


def _month_days(y: int, m: int, rule: Rule, d0: date) -> list[date]:
    n = _days_in_month(y, m)
    sets = []
    if rule.bymonthday:
        sets.append({v if v > 0 else n + 1 + v for v in rule.bymonthday
                     if 1 <= (v if v > 0 else n + 1 + v) <= n})
    if rule.byday:
        first_wd = date(y, m, 1).weekday()
        chosen: set[int] = set()
        for nth, wd in rule.byday:
            all_wd = list(range(1 + (wd - first_wd) % 7, n + 1, 7))
            chosen.update(_nth(all_wd, nth))
        sets.append(chosen)
    if not sets:
        return [date(y, m, d0.day)] if d0.day <= n else []   # the 31st skips short months
    return [date(y, m, d) for d in sorted(set.intersection(*sets))]


def _year_days(y: int, rule: Rule, d0: date) -> list[date]:
    if not rule.bymonth and rule.byday and not rule.bymonthday:
        out: set[date] = set()                  # nth weekday of the *year*
        jan1 = date(y, 1, 1)
        span = (date(y + 1, 1, 1) - jan1).days
        for nth, wd in rule.byday:
            offsets = list(range((wd - jan1.weekday()) % 7, span, 7))
            out.update(jan1 + timedelta(days=o) for o in _nth(offsets, nth))
        return sorted(out)
    months = rule.bymonth or (tuple(range(1, 13)) if rule.bymonthday else (d0.month,))
    out_list: list[date] = []
    for m in months:
        out_list += _month_days(y, m, rule, d0)
    return sorted(out_list)


def _setpos(rule: Rule, days: list[date]) -> list[date]:
    if not rule.bysetpos or not days:
        return days
    picked = set()
    for p in rule.bysetpos:
        idx = p - 1 if p > 0 else len(days) + p
        if 0 <= idx < len(days):
            picked.add(days[idx])
    return sorted(picked)


def _periods(rule: Rule, d0: date) -> Iterator[tuple[date, list[date]]]:
    """(first day of the period, its candidate dates in order), forever."""
    if rule.freq == "DAILY":
        d, step = d0, timedelta(days=rule.interval)
        weekdays = {wd for _, wd in rule.byday}
        while True:
            n = _days_in_month(d.year, d.month)
            ok = ((not rule.bymonth or d.month in rule.bymonth)
                  and (not weekdays or d.weekday() in weekdays)
                  and (not rule.bymonthday or any(
                      (v if v > 0 else n + 1 + v) == d.day for v in rule.bymonthday)))
            yield d, _setpos(rule, [d] if ok else [])
            d += step
    elif rule.freq == "WEEKLY":
        ws = d0 - timedelta(days=(d0.weekday() - rule.wkst) % 7)
        weekdays = {wd for _, wd in rule.byday} or {d0.weekday()}
        while True:
            days = [ws + timedelta(days=i) for i in range(7)]
            days = [d for d in days if d.weekday() in weekdays
                    and (not rule.bymonth or d.month in rule.bymonth)]
            yield ws, _setpos(rule, days)
            ws += timedelta(weeks=rule.interval)
    elif rule.freq == "MONTHLY":
        y, m = d0.year, d0.month
        while True:
            days = _month_days(y, m, rule, d0) \
                if not rule.bymonth or m in rule.bymonth else []
            yield date(y, m, 1), _setpos(rule, days)
            m += rule.interval
            y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    else:
        y = d0.year
        while True:
            yield date(y, 1, 1), _setpos(rule, _year_days(y, rule, d0))
            y += rule.interval


def expand(rule: Rule, start: date | datetime, horizon: date,
           until: date | datetime | None = None,
           max_periods: int = MAX_PERIODS) -> Iterator[date | datetime]:
    """Occurrence starts, in order, in the event's own frame: dates for an
    all-day event, naive wall-clock datetimes otherwise.

    DTSTART is always the first occurrence and counts toward COUNT (RFC 5545
    3.8.5.3), even when it does not match the rule. Stops at COUNT, at
    `until` (already converted by the caller into the same frame as
    `start`), or at the first occurrence after `horizon`. A rule that would
    need more than `max_periods` periods to get there raises Unsupported.
    """
    timed = isinstance(start, datetime)
    d0 = start.date() if timed else start
    tod: time | None = start.time() if timed else None

    def within_until(v) -> bool:
        if until is None:
            return True
        if isinstance(until, datetime):
            return v <= until if timed else v <= until.date()
        return (v.date() if timed else v) <= until

    yield start
    emitted = 1
    if rule.count == 1:
        return
    try:
        for seen, (first, days) in enumerate(_periods(rule, d0)):
            if first > horizon:
                return
            if seen > max_periods:
                raise Unsupported(f"needs more than {max_periods} periods to reach "
                                  f"{horizon.isoformat()}")
            for d in days:
                if d <= d0:
                    continue
                if d > horizon:
                    return
                v = datetime.combine(d, tod) if timed else d
                if not within_until(v):
                    return
                yield v
                emitted += 1
                if rule.count is not None and emitted >= rule.count:
                    return
    except (OverflowError, ValueError) as e:     # walked past date.max
        if isinstance(e, Unsupported):
            raise
        return

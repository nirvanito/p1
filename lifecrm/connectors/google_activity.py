"""Google activity connector: what you searched for and watched, from Google
Takeout's My Activity and YouTube history.

What you looked up is a record of what you were planning and worrying about
— the passport renewal, Mom's knee surgery, the restaurant for a birthday,
the trip back to Lake Bled — and Google keeps every search and every YouTube
watch for as long as its auto-delete setting lets it. Takeout hands that over
as files. This connector reads them; it never talks to Google.

Sources
-------
  My Activity      Takeout > My Activity: one folder per product (Search,
                   Image Search, Maps, YouTube, Chrome, Discover, Assistant,
                   ...), each holding `MyActivity.json` or `MyActivity.html`,
                   whichever format was picked in Takeout:
                     JSON  [{header, title, titleUrl, time ('...Z', UTC),
                             products, activityControls, details,
                             subtitles, locationInfos}]
                     HTML  one `div.outer-cell` per entry: a header cell
                           naming the product; a content cell — "Searched
                           for <a>query</a>" or "Watched <a>title</a><br>
                           <a>channel</a>", then a date line like
                           "Oct 12, 2026, 9:41:07 PM CDT"; and a caption cell
                           (Products / Details / Locations).
  YouTube history  Takeout > YouTube and YouTube Music > history:
                   `watch-history.json|html` and `search-history.json|html`,
                   entries of the same shape.

Both formats are read streaming — the JSON array entry by entry, the HTML
through html.parser fed a chunk at a time — so a decade of activity, hundreds
of MB, is never held as one document.

Two streams, one record per event
---------------------------------
  web_search   every "Searched for" entry, whatever the product (Search,
               Image Search, Maps, YouTube, Play Store, ...):
                 title       the query
                 value_json  {product, url}
  video_watch  every "Watched" entry of YouTube or YouTube Music:
                 title       the video's title
                 body        the channel's name
                 value_json  {product, url, channel, channel_url}
started_at is the moment of the event, journal_date its home-timezone day
(the rollover applied: a search at 00:40 belongs to the evening before).
No participants and no handles: nobody else is in a search or a watch.

One row per event, not a daily count: "what did I google about the passport"
needs the query and its moment. Heavy use is tens of thousands of rows a
year, a few hundred bytes each, which the archive holds without trouble.

Times
-----
JSON states UTC ('Z'). HTML writes each time in the account's display zone,
with its abbreviation: CDT/CST, PDT/PST, EDT/EST, MDT/MST, BST, GMT, UTC,
CET, CEST and 'GMT+02:00'-style offsets are read exactly. Any other
abbreviation (AEST, IST, ...) is read as home-timezone wall time, and the
record says so: value_json time_source = 'home_timezone'. Only English date
lines are understood (US 'Oct 12, 2026, 9:41:07 PM CDT', UK '12 Oct 2026,
21:41:07 BST'); entries of a page in another language are counted, with an
example, and not imported — a misread date is worse than none.

The zone an HTML page shows is the account's setting when the export was
made, not where the search happened, so it is never stored as the record's
own offset: an HTML entry and its JSON twin are the same record.

Skipped and counted per product, never imported
-----------------------------------------------
"Visited" entries (pages opened from Search or Chrome — browsing is the
browser connector's), ads ("From Google Ads"), Discover feed activity,
Assistant voice activity, every other kind of activity (Maps "Directions
to", "Viewed", "Used", ...), searches and watches with no time, and watches
of a video since removed (no link left).

Identity, and re-importing
--------------------------
external_id = sha1(product, kind, url — the title when there is no link —,
time), so one event is one record whether it was read from JSON or HTML,
from My Activity or YouTube history, from this Takeout or the next. The
title is deliberately not part of it when there is a link: creators retitle
videos, and a retitled video would otherwise be watched twice. A retitled
video or renamed channel keeps the names it was imported with (a conflict,
counted on one line). An event never changes, so last_modified_at is its
time, and re-importing the same Takeout is identical.

Mirroring: none — the archive remembers. My Activity's auto-delete (after 3,
18 or 36 months) and YouTube's history expiry drop old activity from every
later Takeout; a search that has expired there is not a deleted fact.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "google_activity"
streams = ("web_search", "video_watch")

_ISO = "%Y-%m-%dT%H:%M:%SZ"
_CHUNK = 1 << 20
_ACTIVITY_FILES = {"myactivity.json", "myactivity.html",
                   "my activity.json", "my activity.html"}
_YOUTUBE_FILES = {"watch-history.json", "watch-history.html",
                  "search-history.json", "search-history.html"}
_PRODUCT_ALIASES = {"google search": "Search", "google maps": "Maps",
                    "google images": "Image Search", "image search": "Image Search",
                    "google assistant": "Assistant", "google discover": "Discover",
                    "google chrome": "Chrome", "youtube": "YouTube",
                    "youtube music": "YouTube Music"}
_ORIGIN = {"Search": "google_search", "Image Search": "google_image_search",
           "Video Search": "google_video_search", "Maps": "google_maps",
           "YouTube": "youtube", "YouTube Music": "youtube_music", "Chrome": "chrome"}
# Longest first where one starts another ("Searched for" before "Searched").
_VERBS = ("Searched for", "Watched", "Visited", "Viewed", "Directions to",
          "Explored", "Listened to", "Subscribed to", "Unsubscribed from",
          "Commented on", "Liked", "Disliked", "Shared", "Saved", "Used",
          "Opened", "Installed", "Updated", "Downloaded", "Played", "Read",
          "Answered", "Said", "Called", "Searched")
_ADS = "From Google Ads"
_CAPTION_LABELS = ("Products:", "Details:", "Locations:", "Why is this here?")


# ---------------------------------------------------------------- values

def _clean(value) -> str | None:
    """Whitespace collapsed — HTML spreads text over &nbsp; and narrow
    no-break spaces that JSON does not have."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _product(raw) -> str | None:
    s = _clean(raw)
    return _PRODUCT_ALIASES.get(s.lower(), s) if s else None


def _origin(product: str) -> str:
    """'google_search', 'youtube', 'google_play_store' (not 'google_google_...')."""
    if product in _ORIGIN:
        return _ORIGIN[product]
    slug = re.sub(r"[^a-z0-9]+", "_", product.lower()).strip("_").removeprefix("google_")
    return "google_" + (slug if slug and slug != "google" else "activity")


def _split_title(title: str) -> tuple[str, str]:
    """('Searched for', 'lake bled pletna boat'). An unknown verb is the
    first word, which is what the report names it by."""
    for verb in _VERBS:
        if title == verb or title.startswith(verb + " "):
            return verb, title[len(verb):].strip()
    head, _, rest = title.partition(" ")
    return head, rest.strip()


# ---------------------------------------------------------------- HTML dates

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul",
                                       "aug", "sep", "oct", "nov", "dec"), start=1)}
_ZONE_MINUTES = {"UTC": 0, "GMT": 0, "BST": 60, "CET": 60, "CEST": 120,
                 "EST": -300, "EDT": -240, "CST": -360, "CDT": -300,
                 "MST": -420, "MDT": -360, "PST": -480, "PDT": -420}
_AMPM = r"(?P<ap>[AaPp]\.? ?[Mm]\.?)"
_CLOCK = r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?"
_ZONE = r"(?: (?P<zone>\S+))?"
_US_DATE = re.compile(r"(?P<mon>[A-Za-z]{3,9})\.? (?P<day>\d{1,2}), (?P<year>\d{4}),? "
                      + _CLOCK + " ?" + _AMPM + _ZONE)
_UK_DATE = re.compile(r"(?P<day>\d{1,2}) (?P<mon>[A-Za-z]{3,9})\.? (?P<year>\d{4}),? "
                      + _CLOCK + "(?: ?" + _AMPM + ")?" + _ZONE)
_OFFSET = re.compile(r"(?:GMT|UTC)(?P<sign>[+\-−])(?P<h>\d{1,2})(?::?(?P<m>\d{2}))?")


def _zone_minutes(zone: str) -> int | None:
    if zone.upper() in _ZONE_MINUTES:
        return _ZONE_MINUTES[zone.upper()]
    m = _OFFSET.fullmatch(zone.upper())
    if m is None:
        return None
    minutes = int(m["h"]) * 60 + int(m["m"] or 0)
    return -minutes if m["sign"] != "+" else minutes


def _html_time(line: str, home: str) -> tuple[str, str, bool] | None:
    """(UTC instant, the zone the line names, whether that zone is known) for
    an English Takeout date line, or None for any other form.

    'Oct 12, 2026, 9:41:07 PM CDT' (US; newer exports put a narrow no-break
    space before PM) and '12 Oct 2026, 21:41:07 BST' (UK). A zone this reader
    does not know is read as home-timezone wall time — the caller marks it.
    """
    text = " ".join(line.split())
    m = _US_DATE.fullmatch(text) or _UK_DATE.fullmatch(text)
    if m is None:
        return None
    month = _MONTHS.get(m["mon"][:3].lower())
    if month is None:
        return None
    hour = int(m["h"])
    if m["ap"]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if m["ap"][0] in "Pp" else 0)
    try:
        wall = datetime(int(m["year"]), month, int(m["day"]), hour,
                        int(m["mi"]), int(m["s"] or 0))
    except ValueError:
        return None
    zone = m["zone"] or "(no zone)"
    minutes = _zone_minutes(zone)
    if minutes is None:
        local = wall.replace(tzinfo=ZoneInfo(home))
        return base.iso_utc(local.isoformat()), zone, False
    return (wall - timedelta(minutes=minutes)).strftime(_ISO), zone, True


# ---------------------------------------------------------------- HTML pages

class _Page(HTMLParser):
    """Takeout's My Activity HTML, fed a chunk at a time. Each finished
    `outer-cell` lands in `entries` for the caller to drain, as
    {header, content, caption}: each a list of lines (split at <br>), each
    line a list of ('t', text) and ('a', text, href) segments.

    Only <div> nesting is tracked; inside a cell, <br> and <b> break lines
    and <a> is a link. Nothing else in the markup matters here.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[dict] = []
        self._depth = 0
        self._entry: dict | None = None
        self._entry_depth = 0
        self._cell: str | None = None
        self._cell_depth = 0
        self._lines: list[list] = []
        self._href: str | None = None
        self._anchor: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            self._depth += 1
            classes = set((dict(attrs).get("class") or "").split())
            if self._entry is None:
                if "outer-cell" in classes:
                    self._entry = {"header": [], "content": None, "caption": []}
                    self._entry_depth = self._depth
                return
            if self._cell is not None:
                return
            cell = None
            if "header-cell" in classes:
                cell = "header"
            elif "content-cell" in classes:
                if "mdl-typography--caption" in classes:
                    cell = "caption"
                elif ("mdl-typography--text-right" not in classes
                      and self._entry["content"] is None):
                    cell = "content"
            if cell:
                self._cell, self._cell_depth, self._lines = cell, self._depth, [[]]
            return
        if self._cell is None:
            return
        if tag in ("br", "b"):           # a caption's bold label stands on its own line
            self._lines.append([])
        elif tag == "a":
            self._href, self._anchor = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag == "div":
            if self._cell is not None and self._depth == self._cell_depth:
                self._close_cell()
            if self._entry is not None and self._depth == self._entry_depth:
                self.entries.append(self._entry)
                self._entry = None
            self._depth = max(0, self._depth - 1)
            return
        if self._cell is None:
            return
        if tag == "a":
            self._close_anchor()
        elif tag == "b":
            self._lines.append([])

    def handle_data(self, data):
        if self._cell is None:
            return
        if self._anchor is not None:
            self._anchor.append(data)
        else:
            self._lines[-1].append(("t", data))

    def close(self):
        super().close()
        if self._entry is not None:          # a file cut off mid-entry
            if self._cell is not None:
                self._close_cell()
            self.entries.append(self._entry)
            self._entry = None

    def _close_anchor(self):
        if self._anchor is not None:
            self._lines[-1].append(("a", "".join(self._anchor), self._href))
        self._href = self._anchor = None

    def _close_cell(self):
        self._close_anchor()
        self._entry[self._cell] = self._lines
        self._cell, self._lines = None, []


def _text(line: list) -> str:
    return " ".join("".join(seg[1] for seg in line).split())


def _caption(lines: list) -> tuple[list[str], list[str]]:
    """(products, details) from the caption cell's labelled lines."""
    products, details, label = [], [], None
    for line in lines:
        text = _text(line)
        if not text:
            continue
        if text in _CAPTION_LABELS:
            label = text
        elif label == "Products:":
            products.append(text)
        elif label == "Details:":
            details.append(text)
    return products, details


def _html_item(entry: dict, hint: str | None) -> dict:
    """One HTML entry in the shape `_json_item` gives a JSON one. The first
    line is the title ('Searched for' + the link's text); the last line with
    no link, after it, is the date; the lines between are subtitles (the
    channel)."""
    products, details = _caption(entry.get("caption") or [])
    header = _text([seg for line in entry.get("header") or [] for seg in line])
    lines = [ln for ln in entry.get("content") or [] if _text(ln)]
    title = url = raw_time = None
    subtitles = []
    if lines:
        first = lines[0]
        link = next((i for i, seg in enumerate(first) if seg[0] == "a"), None)
        if link is None:
            title = _text(first)
        else:
            title = _clean(f"{_text(first[:link])} {first[link][1]}")
            url = _clean(first[link][2])
        rest = lines[1:]
        if rest and not any(seg[0] == "a" for seg in rest[-1]):
            raw_time, rest = _text(rest[-1]), rest[:-1]
        for line in rest:
            a = next((seg for seg in line if seg[0] == "a"), None)
            subtitles.append({"name": _clean(a[1]) if a else _text(line),
                              "url": _clean(a[2]) if a else None})
    return {"product": _product(header) or _product(products[0] if products else None)
            or hint or "Google",
            "title": title, "url": url, "subtitles": subtitles, "details": details,
            "time": ("html", raw_time)}


def _json_item(item: dict, hint: str | None) -> dict:
    products = [p for p in _list(item.get("products")) if isinstance(p, str)]
    return {"product": _product(item.get("header"))
            or _product(products[0] if products else None) or hint or "Google",
            "title": _clean(item.get("title")), "url": _clean(item.get("titleUrl")),
            "subtitles": [{"name": _clean(s.get("name")), "url": _clean(s.get("url"))}
                          for s in _list(item.get("subtitles")) if isinstance(s, dict)],
            "details": [_clean(d.get("name")) for d in _list(item.get("details"))
                        if isinstance(d, dict)],
            "time": ("iso", item.get("time"))}


# ---------------------------------------------------------------- reading

class _NotActivity(DomainError):
    """A file that parses but is not a list of activity entries."""


def _decoder(rel: str):
    text = codecs.getincrementaldecoder("utf-8-sig")()

    def decode(chunk: bytes) -> str:
        try:
            return text.decode(chunk, final=not chunk)
        except UnicodeDecodeError as e:
            raise DomainError(f"{rel} is not valid UTF-8: {e}") from e
    return decode


def _json_entries(stream, rel: str):
    """Each element of the top-level JSON array, read a chunk at a time —
    a decade of searches is one array of hundreds of MB."""
    decoder = json.JSONDecoder()
    decode = _decoder(rel)
    buf, pos, eof, count = "", 0, False, 0

    def fill():
        nonlocal buf, pos, eof
        chunk = stream.read(_CHUNK)
        buf, pos = buf[pos:] + decode(chunk), 0
        eof = not chunk

    def skip_space():
        nonlocal pos
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n":
                pos += 1
            if pos < len(buf) or eof:
                return
            fill()

    fill()
    skip_space()
    if pos >= len(buf):
        raise _NotActivity(f"{rel} is empty")
    if buf[pos] != "[":
        what = "a JSON object" if buf[pos] == "{" else f"'{buf[pos]}'"
        raise _NotActivity(f"{rel} starts with {what}, not a list of activity entries "
                           f"— it is not a My Activity or YouTube history export")
    pos += 1
    while True:
        skip_space()
        if pos >= len(buf):
            raise DomainError(f"{rel} is not valid JSON: the list never closes "
                              f"(after {count} entries)")
        if buf[pos] == "]":
            return
        if count:
            if buf[pos] != ",":
                raise DomainError(f"{rel} is not valid JSON: expected ',' or ']' "
                                  f"after entry {count}")
            pos += 1
            skip_space()
        while True:
            try:
                item, end = decoder.raw_decode(buf, pos)
            except json.JSONDecodeError as e:
                if eof:
                    raise DomainError(f"{rel} is not valid JSON at entry {count + 1}: "
                                      f"{e.msg}") from e
                fill()
                continue
            if end >= len(buf) and not eof and not isinstance(item, (dict, list)):
                fill()                  # a bare number may continue in the next chunk
                continue
            break
        pos = end
        count += 1
        yield item


def _html_entries(stream, rel: str):
    page = _Page()
    decode = _decoder(rel)
    while True:
        chunk = stream.read(_CHUNK)
        text = decode(chunk)
        if text:
            page.feed(text)
        if not chunk:
            page.close()
        yield from page.entries
        page.entries.clear()
        if not chunk:
            return


class _Acc:
    """What one scan has gathered, across every file it reads."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.records: dict[str, dict] = {}
        self.skipped: Counter = Counter()
        self.unmapped: Counter = Counter()
        self.zones: Counter = Counter()
        self.home_zones: Counter = Counter()
        self.products: Counter = Counter()
        self.bad_dates = 0
        self.bad_example: str | None = None

    def _when(self, item: dict) -> tuple[str | None, str | None, bool]:
        """(UTC instant, time_source, whether an unreadable date was counted)."""
        kind, raw = item["time"]
        if kind == "iso":
            return (base.iso_utc(raw) if isinstance(raw, str) else None), None, False
        if not raw:
            return None, None, False
        parsed = _html_time(raw, self.cfg.timezone)
        if parsed is None:
            self.bad_dates += 1
            self.bad_example = self.bad_example or raw
            return None, None, True
        at, zone, known = parsed
        (self.zones if known else self.home_zones)[zone] += 1
        return at, (None if known else "home_timezone"), False

    def take(self, item: dict) -> None:
        product = item["product"]
        if product == "Ads" or _ADS in item["details"]:
            self.skipped[f"{product}: ad ({_ADS})"] += 1
            return
        if product == "Discover":
            self.skipped["Discover: feed activity — not imported"] += 1
            return
        if product == "Assistant":
            self.skipped["Assistant: voice activity — not imported"] += 1
            return
        if not item["title"]:
            self.skipped[f"{product}: entry with no title"] += 1
            return
        verb, obj = _split_title(item["title"])
        if verb == "Visited":
            self.skipped[f"{product}: Visited a page — browsing is the browser "
                         f"connector's (docs/browser.md)"] += 1
            return
        if verb == "Searched for":
            kind, label = "search", "search"
        elif verb == "Watched" and product.startswith("YouTube"):
            kind, label = "watch", "watch"
        else:
            self.skipped[f"{product}: {verb} — neither a search nor a YouTube "
                         f"watch"] += 1
            return
        if not obj:
            self.skipped[f"{product}: {label} naming nothing"] += 1
            return
        if kind == "watch" and not item["url"]:
            self.skipped[f"{product}: watch of a video since removed (no link "
                         f"left)"] += 1
            return
        at, time_source, counted = self._when(item)
        if at is None:
            if not counted:
                self.skipped[f"{product}: {label} with no time"] += 1
            return
        rec = _record(kind, item, obj, at, time_source, self.cfg)
        if rec["external_id"] in self.records:
            self.skipped["event listed twice (in two files, or twice in one) — "
                         "kept once"] += 1
            return
        self.records[rec["external_id"]] = rec
        self.products[product] += 1


def _record(kind: str, item: dict, obj: str, at: str, time_source: str | None,
            cfg: Config) -> dict:
    product = item["product"]
    detail = {"product": product, "url": item["url"], "time_source": time_source}
    body = None
    if kind == "watch":
        channel = item["subtitles"][0] if item["subtitles"] else {}
        body = channel.get("name")
        detail.update(channel=body, channel_url=channel.get("url"))
    ident = "\n".join((product, kind, item["url"] or obj, at))
    return {
        "stream": "web_search" if kind == "search" else "video_watch",
        "external_id": hashlib.sha1(ident.encode("utf-8")).hexdigest(),
        "journal_date": base.journal_date_for_iso(at, cfg),
        "started_at": at,
        "ended_at": None,
        # The HTML's zone is the account's display setting, not where the
        # event happened: never the record's own offset.
        "utc_offset_sec": None,
        "direction": None,
        "title": obj,
        "body": body,
        "value": None,
        "unit": None,
        "value_json": canonical_json({k: v for k, v in detail.items() if v is not None}),
        "media_path": None,
        "origin_app": _origin(product),
        "last_modified_at": at,
        "participants": [],
    }


# ---------------------------------------------------------------- targets

def _wanted(rel: str) -> tuple[bool, str | None]:
    """(read this file?, the product its folder names). My Activity files
    sit in a folder named for their product; YouTube history is YouTube's."""
    parts = rel.split("/")
    low = parts[-1].lower()
    if low in _ACTIVITY_FILES:
        folder = parts[-2] if len(parts) >= 2 else None
        if folder and folder.lower() == "my activity":
            folder = None
        return True, _product(folder)
    if low in _YOUTUBE_FILES:
        return True, "YouTube"
    return False, None


def _group(rel: str) -> str:
    """What a file not read belongs to, for the report: the Takeout product
    folder ('file(s) in Google Photos/'), or the file itself at the top."""
    parts = rel.split("/")
    if parts[0].lower() == "takeout" and len(parts) > 1:
        parts = parts[1:]
    if len(parts) == 1:
        return parts[0]
    if parts[0].lower() in ("my activity", "youtube and youtube music") and len(parts) > 2:
        return f"file(s) in {'/'.join(parts[:2])}/"
    return f"file(s) in {parts[0]}/"


def _select(entries: list[str]) -> tuple[list[tuple[str, str | None]], Counter]:
    take, unread = [], Counter()
    for rel in entries:
        wanted, hint = _wanted(rel)
        if wanted:
            take.append((rel, hint))
        else:
            unread[_group(rel)] += 1
    if not take:
        tops = set()
        for rel in entries:
            parts = rel.split("/")
            tops.add(parts[1] if parts[0].lower() == "takeout" and len(parts) > 1
                     else parts[0])
        listing = ", ".join(sorted(tops)[:10]) or "(empty)"
        raise DomainError(
            f"holds no My Activity file (MyActivity.json / MyActivity.html) and no "
            f"YouTube history (watch-history / search-history) — it has: {listing}. "
            f"A Takeout in another language names them differently; this reader "
            f"knows the English names")
    return take, unread


def _format(rel: str) -> str:
    return "json" if rel.lower().endswith(".json") else "html"


def _sources(target: Path):
    """Yield (target kind, unread, [(rel, product hint, format, opener)]) once.
    Files are opened one at a time, as streams."""
    if target.is_file() and not zipfile.is_zipfile(target):
        suffix = target.suffix.lower()
        if suffix not in (".json", ".html", ".htm"):
            raise DomainError(
                f"{target.name} is not a .json or .html file, a Takeout .zip or a "
                f"folder — expected MyActivity.json / MyActivity.html or YouTube's "
                f"watch-history / search-history")
        # The folder names the product only inside My Activity; a file
        # copied to Downloads says nothing by where it sits.
        hint = None
        if target.name.lower() in _YOUTUBE_FILES:
            hint = "YouTube"
        elif target.parent.parent.name.lower() == "my activity":
            hint = _product(target.parent.name)
        yield "file", Counter(), [(target.name, hint, "json" if suffix == ".json" else "html",
                                   lambda: open(target, "rb"))]
        return
    if target.is_dir():
        by_rel = {p.relative_to(target).as_posix(): p
                  for p in target.rglob("*") if p.is_file()}
        try:
            take, unread = _select(sorted(by_rel))
        except DomainError as e:
            raise DomainError(f"{target.name} {e}") from e
        yield "folder", unread, [(rel, hint, _format(rel),
                                  lambda p=by_rel[rel]: open(p, "rb"))
                                 for rel, hint in take]
        return
    with zipfile.ZipFile(target) as zf:
        try:
            take, unread = _select(sorted(m for m in zf.namelist() if not m.endswith("/")))
        except DomainError as e:
            raise DomainError(f"{target.name} {e}") from e
        yield "zip", unread, [(m, hint, _format(m), lambda m=m: zf.open(m))
                              for m, hint in take]


def _folder_hash(target: Path, files: list) -> str:
    h = hashlib.sha256()
    for rel, *_ in files:
        h.update(f"{rel}\n{base.file_hash(target / rel)}\n".encode("utf-8"))
    return h.hexdigest()


def downloads_dir() -> Path:
    return platforms.home() / "Downloads"


def _holds_activity(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path) as zf:
            return any(_wanted(m)[0] for m in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def default_target(cfg: Config) -> Path:
    """[connectors.google_activity] path, else the newest takeout-*.zip in
    Downloads that holds My Activity or YouTube history. A Takeout split into
    several zips is imported one part at a time; this finds the newest part
    with activity in it."""
    settings = cfg.connector_settings(name)
    if settings.get("path"):
        p = cfg.resolve(settings["path"])
        if not p.exists():
            raise DomainError(f"[connectors.google_activity] path does not exist: {p}")
        return p
    folder = downloads_dir()
    try:
        zips = sorted(folder.glob("takeout-*.zip"),
                      key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    except OSError:
        zips = []
    for z in zips:
        if _holds_activity(z):
            return z
    raise DomainError(
        f"no Google Takeout with My Activity or YouTube history found — looked for "
        f"takeout-*.zip in {folder}; export one (docs/google_activity.md), pass the "
        f"zip or its folder, or set [connectors.google_activity] path")


# ---------------------------------------------------------------- scan

def scan(target: Path, cfg: Config) -> base.Scan:
    acc = _Acc(cfg)
    formats: Counter = Counter()
    labels: list[str] = []
    try:
        for target_kind, unread, files in _sources(target):
            strict = len(files) == 1
            for rel, hint, fmt, opener in files:
                with opener() as stream:
                    entries = (_json_entries(stream, rel) if fmt == "json"
                               else _html_entries(stream, rel))
                    seen = 0
                    try:
                        for entry in entries:
                            seen += 1
                            if fmt == "html":
                                acc.take(_html_item(entry, hint))
                            elif isinstance(entry, dict):
                                acc.take(_json_item(entry, hint))
                            else:
                                acc.skipped["activity entry that is not an object"] += 1
                    except _NotActivity as e:
                        if strict:
                            raise
                        acc.unmapped[f"{rel} — {str(e).removeprefix(rel + ' ')}"] += 1
                        continue
                if fmt == "html" and not seen:
                    why = ("holds no activity entries (no 'outer-cell' blocks) — it "
                           "does not look like a Takeout My Activity page")
                    if strict:
                        raise DomainError(f"{rel} {why}")
                    acc.unmapped[f"{rel} — {why}"] += 1
                    continue
                formats[fmt] += 1
                labels.append(_label(rel, hint))
            for group, n in unread.items():
                acc.unmapped[f"{group} — not My Activity or YouTube history"] += n
            if target_kind == "folder":
                source_hash = _folder_hash(target, files)
            else:
                source_hash = base.file_hash(target)
    except PermissionError as e:
        raise DomainError(
            f"cannot read {e.filename or target}: permission denied — on a Mac, "
            f"allow the app running this to use that folder (System Settings > "
            f"Privacy & Security > Files and Folders), then run it again") from e

    if acc.bad_dates:
        acc.unmapped[f"entries dated in a form this reader does not know (English "
                     f"US/UK dates only), e.g. '{acc.bad_example}'"] += acc.bad_dates
    records = sorted(acc.records.values(),
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    dates = [r["journal_date"] for r in records]

    def ranked(c: Counter) -> list[list]:
        return [[k, v] for k, v in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]

    return base.empty_scan(
        name,
        source_file=target.name,
        source_hash=source_hash,
        records=records,
        unmapped=[{"what": k, "count": v} for k, v in sorted(acc.unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(acc.skipped.items())],
        facts={"formats": dict(sorted(formats.items())), "files": sorted(labels),
               "products": ranked(acc.products), "zones": ranked(acc.zones),
               "home_zones": ranked(acc.home_zones), "home_timezone": cfg.timezone,
               "oldest_day": min(dates) if dates else None,
               "newest_day": max(dates) if dates else None},
        target_kind=target_kind)


def _label(rel: str, hint: str | None) -> str:
    low = rel.rsplit("/", 1)[-1].lower()
    if low in _YOUTUBE_FILES:
        return f"YouTube {low.split('-', 1)[0]} history"
    return f"My Activity: {hint}" if hint else rel


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    detail = json.loads(rec["value_json"] or "{}")
    title = rec["title"] if len(rec["title"]) <= 60 else rec["title"][:59] + "…"
    if rec["stream"] == "web_search":
        return f'{rec["journal_date"]} {detail.get("product")}: "{title}"'
    channel = f" — {rec['body']}" if rec["body"] else ""
    return f'{rec["journal_date"]} "{title}"{channel}'


def _renamed(conflicts: list[dict]) -> tuple[int, list[dict]]:
    """(watches whose video or channel was renamed since, the rest). A
    rename is expected: the archive keeps the names it imported."""
    renamed, other = 0, []
    for c in conflicts:
        if c["stream"] == "video_watch" and set(c["changes"]) <= {"title", "body",
                                                                  "value_json"}:
            renamed += 1
        else:
            other.append(c)
    return renamed, other


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    formats = facts.get("formats") or {}
    fmt = " and ".join(f.upper() for f in sorted(formats)) or "no activity found"
    lines = [f"google activity: {scan['source_file']} (Google Takeout, {fmt}; "
             f"{sum(formats.values())} file(s) read)"]
    if facts.get("files"):
        lines.append("  read: " + ", ".join(facts["files"]))
    if facts.get("oldest_day"):
        lines.append(f"  activity spans {facts['oldest_day']}..{facts['newest_day']} — "
                     f"Google keeps what auto-delete (3, 18 or 36 months, or off) has "
                     f"not removed")
    if facts.get("products"):
        lines.append("  products: " + ", ".join(f"{p} {n}" for p, n in facts["products"]))

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    shown = False
    for stream in streams:
        n_new = [r for r in new if r["stream"] == stream]
        n_upd = [r for r in upd if r["stream"] == stream]
        if not (n_new or n_upd):
            continue
        recs = n_new or n_upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  {stream}: {len(n_new)} new, {len(n_upd)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
        shown = True
    if not shown:
        lines.append("  nothing new — the export matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate event(s)")
    if tail:
        lines.append("  " + ", ".join(tail))

    if facts.get("zones"):
        lines.append("  times: HTML dates read in the zone each one names ("
                     + ", ".join(f"{z} {n}" for z, n in facts["zones"]) + ")")
    if facts.get("home_zones"):
        total = sum(n for _, n in facts["home_zones"])
        lines.append(f"  times: {total} date(s) name a zone this reader does not know ("
                     + ", ".join(z for z, _ in facts["home_zones"])
                     + f") — read as {facts.get('home_timezone')} wall time and marked "
                       f"time_source=home_timezone")

    renamed, other = _renamed(plan["conflicts"])
    if renamed:
        lines.append(f"  kept existing: {renamed} watch(es) whose video or channel has "
                     f"been renamed since — kept the names they were imported with")
    for c in other[:5]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:12]}…: differs from "
                     f"the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    if len(other) > 5:
        lines.append(f"  … and {len(other) - 5} more conflict(s)")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not read: {u['count']} × {u['what']}")
    return lines

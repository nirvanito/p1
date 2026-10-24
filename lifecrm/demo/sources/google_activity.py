"""Sample Google Takeout for the demo: the persona's My Activity and YouTube
history.

  'android'  `takeout-<anchor>T190000Z-001.zip` in Takeout's JSON format (My
             Activity and "YouTube and YouTube Music" each set to JSON when
             the export was made), as the Galaxy's Google account exports it:
               Takeout/My Activity/<Search | Image Search | Maps | Chrome |
                 Discover | Assistant>/MyActivity.json
               Takeout/YouTube and YouTube Music/history/watch-history.json
                 and search-history.json
               plus archive_browser.html and a subscriptions.csv, which the
               connector names as not read.
  'iphone'   the same Takeout in its default HTML format (MyActivity.html,
             watch-history.html, search-history.html). The Google app and
             YouTube on the iPhone save to the same Google account, so the
             export is the same one; it has no Chrome folder (Safari is this
             iPhone's browser) and no Assistant (Siri's requests never reach
             Google).
  'windows-iphone'  the iphone combo's HTML Takeout: Edge is this laptop's
             browser (no Chrome folder either) and Siri the phone's assistant.

The activity follows the persona: the passport renewal searched the evening
it went on the list, Veracruz's menu right after Jake's text, Mom's knee
surgery after the call, the board-deck template after Priya's advice, Uchi
before Maya's birthday, a sleepless search after midnight in the stretched-
thin week (it belongs to the evening before), the flights to Ljubljana and
the new climbing gym Sam wants to try. Four years back, on the Bled trip:
the bus to Bled, Bled Castle on Maps, Vintgar gorge's hours and the pletna
boat. On YouTube: bouldering technique the night before climbing with Maya,
a board-deck storytelling talk the evening of the Claude chat about the
deck, and a Lake Bled travel vlog the day the return trip was planned.
Visited pages, Discover cards, an Assistant request, Maps directions, an ad
and a removed video are there too — the connector skips and counts them.

JSON times are UTC with milliseconds, as Takeout writes them. HTML times are
written in the account's zone (America/Chicago, CDT/CST) — the trip's too,
which is how Takeout renders them — with a narrow no-break space before
AM/PM, as newer exports have. Deterministic for a given (anchor, combo).
"""

from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from .. import persona

_TRIP_ZONE = ZoneInfo("Europe/Ljubljana")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")
_WEB_APP = ("Web & App Activity",)

# (offset | None, trip day | None, local 'HH:MM:SS', product, query)
_SEARCHES = (
    (-40, None, "20:41:12", "Search", "how to renew passport usps"),
    (-38, None, "18:09:40", "Search", "veracruz all natural menu"),
    (-34, None, "18:37:05", "Search", "knee surgery recovery time"),
    (-31, None, "21:02:18", "YouTube", "bouldering technique"),
    (-28, None, "21:29:51", "YouTube", "board deck storytelling"),
    (-27, None, "10:12:33", "Search", "board deck customer story template"),
    (-25, None, "20:04:09", "Search", "ceramics class gift voucher austin"),
    (-25, None, "20:06:44", "Image Search", "patagonia jacket sage"),
    (-16, None, "13:38:27", "YouTube", "lake bled travel vlog"),
    (-16, None, "14:02:15", "Search", "lake bled best time to visit"),
    (-11, None, "21:15:02", "Search", "uchi austin reservations"),
    (-4, None, "00:38:44", "Search", "how to fall asleep when stressed"),
    (-2, None, "13:09:48", "Search", "flights austin to ljubljana"),
    (-1, None, "18:41:30", "Maps", "Veracruz All Natural"),
    (0, None, "12:31:06", "Search", "climbing gym austin new"),
    # Four years back, in Slovenia (local times there).
    (None, 0, "15:52:10", "Search", "ljubljana to bled bus"),
    (None, 0, "21:15:20", "Maps", "Bled Castle"),
    (None, 1, "21:40:55", "Search", "vintgar gorge opening hours"),
    (None, 2, "13:18:02", "Search", "lake bled pletna boat"),
)

# (offset | None, trip day | None, local 'HH:MM:SS', product, video title, channel)
_WATCHES = (
    (-31, None, "21:05:40", "YouTube",
     "Bouldering Technique: Footwork Drills for V3 and Up", "Crag Notes"),
    (-28, None, "21:33:12", "YouTube",
     "How to Tell a Customer Story in a Board Deck", "Founder Talks"),
    (-16, None, "13:44:03", "YouTube",
     "Lake Bled, Slovenia in 4K — Travel Vlog", "Wander Slowly"),
    (-12, None, "19:58:31", "YouTube", "Bench Press Form: Three Fixes", "Barbell Basics"),
    (-5, None, "22:20:15", "YouTube Music", "Be Sweet", "Japanese Breakfast - Topic"),
    (None, 1, "22:30:08", "YouTube",
     "Pletna Boats and the Island Church — Lake Bled Guide", "Wander Slowly"),
)

# Searches the persona's Austin searches were made near (Takeout's
# "At this general area"), keyed by query.
_NEAR_HOME = {"how to renew passport usps", "knee surgery recovery time",
              "uchi austin reservations", "climbing gym austin new"}


@dataclass(frozen=True)
class _Entry:
    product: str
    when: datetime                    # aware
    verb: str | None                  # 'Searched for', 'Watched', ... ; None: plain title
    text: str                         # the linked text, or the whole title
    url: str | None
    controls: tuple[str, ...] = _WEB_APP
    channel: str | None = None
    ad: bool = False
    near_home: bool = False

    @property
    def title(self) -> str:
        return f"{self.verb} {self.text}" if self.verb else self.text


# ---------------------------------------------------------------- the story

def _when(anchor: date, offset: int | None, trip_day: int | None, hms: str) -> datetime:
    if offset is not None:
        return persona.local_dt(anchor, offset, hms)
    d = persona.bled_trip_days(anchor)[trip_day or 0]
    hh, mm, ss = (int(x) for x in hms.split(":"))
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=_TRIP_ZONE)


def _token(seed: str, n: int) -> str:
    return base64.urlsafe_b64encode(hashlib.sha1(seed.encode()).digest()).decode()[:n]


def video_url(title: str, music: bool = False) -> str:
    host = "music.youtube.com" if music else "www.youtube.com"
    return f"https://{host}/watch?v={_token('video:' + title, 11)}"


def channel_url(channel: str) -> str:
    return f"https://www.youtube.com/channel/UC{_token('channel:' + channel, 22)}"


def _search_url(product: str, query: str) -> str:
    q = quote_plus(query)
    if product == "YouTube":
        return f"https://www.youtube.com/results?search_query={q}"
    if product == "Maps":
        return f"https://www.google.com/maps/search/{q}/"
    if product == "Image Search":
        return f"https://www.google.com/search?q={q}&tbm=isch"
    return f"https://www.google.com/search?q={q}"


def _redirect(url: str) -> str:
    """Search's 'Visited' links go through Google's redirector."""
    return (f"https://www.google.com/url?q={url}&usg=AOvVaw"
            f"{_token('usg:' + url, 18)}")


def entries(anchor: date, combo: str) -> list[_Entry]:
    out = []
    for offset, trip_day, hms, product, query in _SEARCHES:
        controls = ("YouTube search history",) if product == "YouTube" else _WEB_APP
        out.append(_Entry(product, _when(anchor, offset, trip_day, hms), "Searched for",
                          query, _search_url(product, query), controls,
                          near_home=query in _NEAR_HOME))
    for offset, trip_day, hms, product, title, channel in _WATCHES:
        out.append(_Entry(product, _when(anchor, offset, trip_day, hms), "Watched", title,
                          video_url(title, music=product == "YouTube Music"),
                          ("YouTube watch history",), channel=channel))
    # What the connector skips, and counts.
    out += [
        _Entry("Search", _when(anchor, -38, None, "18:10:31"), "Visited",
               "Menu – Veracruz All Natural",
               _redirect("https://www.veracruzallnatural.com/menu")),
        _Entry("Search", _when(anchor, None, 2, "13:19:40"), "Visited",
               "Pletna boats – the traditional ride to Bled Island",
               _redirect("https://www.bled.example/pletna")),
        _Entry("Maps", _when(anchor, -9, None, "18:31:22"), "Directions to", "Uchi",
               "https://www.google.com/maps/dir//Uchi,+801+S+Lamar+Blvd,+Austin,+TX/"),
        _Entry("Discover", _when(anchor, -10, None, "07:45:12"), None,
               "5 cards in your feed", None),
        _Entry("Discover", _when(anchor, -3, None, "07:52:40"), None,
               "3 cards in your feed", None),
        _Entry("YouTube", _when(anchor, -4, None, "21:10:00"), "Watched",
               "Trailhead Outfitters — Fall Gear Sale",
               video_url("Trailhead Outfitters — Fall Gear Sale"),
               ("YouTube watch history",), channel="Trailhead Outfitters", ad=True),
        _Entry("YouTube", _when(anchor, -20, None, "22:41:09"), "Watched",
               "a video that has been removed", None, ("YouTube watch history",)),
    ]
    if persona.browser(combo) == "chrome":          # Chrome history in My Activity
        out += [
            _Entry("Chrome", _when(anchor, -27, None, "10:14:02"), "Visited",
                   "Customer story slide template - Google Slides",
                   "https://docs.google.com/presentation/d/1Q4bOaRdDeCkDemoAlexRivera/edit"),
            _Entry("Chrome", _when(anchor, -23, None, "08:20:11"), "Visited",
                   "Passport renewal - USPS", "https://www.usps.com/international/passports.htm"),
        ]
    if not persona.has_iphone(combo):               # Siri's requests never reach Google
        out += [
            _Entry("Assistant", _when(anchor, -6, None, "06:59:31"), "Said",
                   "set an alarm for 6:15 AM",
                   "https://www.google.com/search?q=set+an+alarm+for+6%3A15+AM",
                   ("Web & App Activity", "Voice & Audio Activity")),
        ]
    return out


def _file_for(e: _Entry) -> str:
    if e.product.startswith("YouTube"):
        history = "watch-history" if e.verb == "Watched" else "search-history"
        return f"YouTube and YouTube Music/history/{history}"
    return f"My Activity/{e.product}/MyActivity"


# ---------------------------------------------------------------- JSON

def _json_time(when: datetime) -> str:
    utc = when.astimezone(timezone.utc)
    ms = int(hashlib.sha1(utc.isoformat().encode()).hexdigest()[:4], 16) % 1000
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{ms:03d}Z"


def _json_entry(e: _Entry) -> dict:
    item: dict = {"header": e.product, "title": e.title}
    if e.url:
        item["titleUrl"] = e.url
    if e.channel:
        item["subtitles"] = [{"name": e.channel, "url": channel_url(e.channel)}]
    item["time"] = _json_time(e.when)
    item["products"] = [e.product]
    if e.ad:
        item["details"] = [{"name": "From Google Ads"}]
    if e.near_home:
        item["locationInfos"] = [{
            "name": "At this general area",
            "url": "https://www.google.com/maps/@?api=1&map_action=map"
                   "&center=30.267153,-97.743057&zoom=12",
            "source": "From your places (Home)",
            "sourceUrl": "https://support.google.com/websearch/answer/1696565"}]
    item["activityControls"] = list(e.controls)
    return item


# ---------------------------------------------------------------- HTML

_STYLE = (".mdl-grid{display:flex;flex-flow:row wrap}.outer-cell{margin:8px;padding:8px;"
          "box-shadow:0 2px 2px rgba(0,0,0,.14)}.header-cell{width:100%}"
          ".content-cell{width:50%}.mdl-typography--caption{width:100%;color:#757575}"
          ".mdl-typography--text-right{text-align:right}")


def _html_time(when: datetime) -> str:
    """'Oct 12, 2026, 9:41:07 PM CDT' in the account's zone. Month names
    spelled out here, not by strftime, which follows the machine's locale."""
    t = when.astimezone(ZoneInfo(persona.TIMEZONE))
    hour = t.hour % 12 or 12
    return (f"{_MONTHS[t.month - 1]} {t.day}, {t.year}, {hour}:{t:%M:%S}"
            f" {'AM' if t.hour < 12 else 'PM'} {t.tzname()}")


def _html_entry(e: _Entry) -> str:
    if e.verb and e.url:
        line = f'{escape(e.verb)}&nbsp;<a href="{escape(e.url)}">{escape(e.text)}</a>'
    else:
        line = escape(e.title)
    content = line + "<br>"
    if e.channel:
        content += f'<a href="{escape(channel_url(e.channel))}">{escape(e.channel)}</a><br>'
    content += escape(_html_time(e.when)) + "<br>"
    caption = f"<b>Products:</b><br>&emsp;{escape(e.product)}<br>"
    if e.ad:
        caption += "<b>Details:</b><br>&emsp;From Google Ads<br>"
    if e.near_home:
        caption += ('<b>Locations:</b><br>&emsp;At this general area: <a href="'
                    + escape("https://www.google.com/maps/@?api=1&map_action=map"
                             "&center=30.267153,-97.743057&zoom=12")
                    + '">From your places (Home)</a><br>')
    caption += ("<b>Why is this here?</b><br>&emsp;This activity was saved to your Google "
                "Account because the following settings were on:&nbsp;"
                + escape(", ".join(e.controls))
                + '.&nbsp;You can control these settings &nbsp;<a href="'
                  'https://myaccount.google.com/activitycontrols">here</a>.')
    return ('<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">'
            '<div class="mdl-grid">'
            '<div class="header-cell mdl-cell mdl-cell--12-col">'
            f'<p class="mdl-typography--title">{escape(e.product)}<br></p></div>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
            f'{content}</div>'
            '<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1 '
            'mdl-typography--text-right"></div>'
            '<div class="content-cell mdl-cell mdl-cell--12-col mdl-typography--caption">'
            f'{caption}</div></div></div>')


def _html_page(blocks: list[str]) -> str:
    return ('<html><head><meta http-equiv="Content-Type" content="text/html; '
            'charset=UTF-8"><title>My Activity</title><style type="text/css">'
            + _STYLE + '</style></head><body><div class="mdl-grid">'
            + "".join(blocks) + "</div></body></html>")


# ---------------------------------------------------------------- the zip

_ARCHIVE_BROWSER = (
    '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Download your data</title>'
    '</head><body><h1>Your account, your data.</h1><p>This archive contains your data '
    'from: My Activity, YouTube and YouTube Music.</p></body></html>')


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo not in persona.COMBOS:
        raise ValueError(f"google_activity has no sample for combo '{combo}' "
                         f"(expected: {', '.join(persona.COMBOS)})")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    ext = "json" if combo == "android" else "html"

    files: dict[str, list[_Entry]] = {}
    for e in entries(anchor, combo):
        files.setdefault(_file_for(e), []).append(e)
    members: dict[str, bytes] = {"Takeout/archive_browser.html":
                                 _ARCHIVE_BROWSER.encode("utf-8")}
    for path, items in sorted(files.items()):
        items.sort(key=lambda e: e.when, reverse=True)      # Takeout lists newest first
        if ext == "json":
            text = json.dumps([_json_entry(e) for e in items], indent=2, ensure_ascii=False)
        else:
            text = _html_page([_html_entry(e) for e in items])
        members[f"Takeout/{path}.{ext}"] = text.encode("utf-8")
    channels = sorted({w[5] for w in _WATCHES if w[3] == "YouTube"})
    members["Takeout/YouTube and YouTube Music/subscriptions/subscriptions.csv"] = (
        "Channel Id,Channel Url,Channel Title\n" + "".join(
            f"{channel_url(c).rsplit('/', 1)[1]},{channel_url(c)},{c}\n"
            for c in channels)).encode("utf-8")

    out = dest / f"takeout-{anchor:%Y%m%d}T190000Z-001.zip"
    stamp = (anchor.year, anchor.month, anchor.day, 14, 0, 0)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, data in sorted(members.items()):
            info = zipfile.ZipInfo(member, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return out

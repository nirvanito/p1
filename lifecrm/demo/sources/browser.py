"""Sample browser history for the demo: what the persona read on the laptop.

  'android'  Chrome on the Windows laptop: the profile's `History` database
             at its real place under AppData/Local/Google/Chrome/User Data/
             Default, with the tables the connector reads (urls, visits,
             visit_source) and the ones beside them (meta, keyword_search_terms).
             Times are Chrome's microseconds since 1601. Pages opened on the
             Galaxy arrive through Chrome sync, marked as synced visits.
  'iphone'   Safari on the MacBook: `Library/Safari/History.db` with
             history_items and history_visits (Core Data seconds since 2001,
             a title per visit) and the bookkeeping tables. Pages opened on
             the iPhone arrive through iCloud, marked origin 1.
  'windows-iphone'  Microsoft Edge on the Windows laptop: the same Chromium
             `History` database under AppData/Local/Microsoft/Edge/User Data/
             Default, Edge's own pages as edge://. Safari on an iPhone syncs
             nowhere on Windows, so the phone's pages here are the ones
             opened in Edge on the iPhone, synced in.

The story is the persona's six weeks: the board deck template the morning
Priya hands it over, then the deck itself on the days the journal works on it
(one session past midnight, which still counts toward the evening before);
the passport renewal pages before the post office; the Veracruz All Natural
menu when Jake texts; the Lake Bled itinerary, Bled Castle and Vintgar Gorge
the afternoon Claude was asked for one; flights to Ljubljana; the ceramics
voucher and the Uchi booking before Maya's birthday; and a Gmail inbox whose
title changes with the unread count. Each browser's own noise is there too —
chrome:// pages and an embedded video frame in Chrome, a failed load in
Safari, and a PDF opened from Downloads — so the demo shows what is skipped.

Deterministic for a given (anchor, combo).
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from .. import persona

CHROME_EPOCH = 11644473600
APPLE_EPOCH = 978307200

_GOOGLE = "https://www.google.com/search?q="
_GMAIL = "https://mail.google.com/mail/u/0/#inbox"
_DECK = "https://docs.google.com/presentation/d/1Q4bOaRdDeCkDemoAlexRivera/edit"
_DECK_TITLE = "Board deck template - Q4 product update - Google Slides"
_MENU = "https://www.veracruzallnatural.com/menu"
_FLIGHTS = "https://www.google.com/travel/flights?q=Flights%20to%20LJU%20from%20AUS"
_OVERSTORY = "https://en.wikipedia.org/wiki/The_Overstory"
_USPS = "https://www.usps.com/international/passports.htm"
_PDF = {"windows": "file:///C:/Users/alex/Downloads/Q4-board-deck-final.pdf",
        "macos": "file:///Users/alex/Downloads/Q4-board-deck-final.pdf"}
# Where each Chromium browser keeps its profile under AppData\Local, and the
# sync id of the phone its synced visits came from.
_CHROMIUM = {"chrome": ("Google", "Chrome", "c2a7e9f0-galaxy-s24-demo"),
             "edge": ("Microsoft", "Edge", "5e0b4d21-iphone-15-pro-demo")}


def _inbox(n: int) -> str:
    return f"Inbox ({n}) - {persona.OWNER.email} - Gmail"


# (offset, local 'HH:MM', url, title, how, only in this browser family:
# 'chromium' (Chrome, Edge — Edge's own pages are edge://) or 'safari')
#   how: typed (address bar), link, bookmark, search (typed into the address
#   bar as a search), reload, phone (opened on the phone, synced in),
#   frame (a page's embedded frame), failed (did not load)
_VISITS = (
    (-40, "08:58", _GMAIL, _inbox(14), "typed", None),
    (-40, "09:40", _GOOGLE + "board+deck+template", "board deck template - Google Search",
     "search", None),
    (-40, "09:44", _DECK, _DECK_TITLE, "link", None),
    (-40, "21:35", _GOOGLE + "passport+renewal+USPS", "passport renewal USPS - Google Search",
     "search", None),
    (-40, "21:37", _USPS, "Passports | USPS", "link", None),
    (-40, "21:41", "https://travel.state.gov/content/travel/en/passports/have-passport/"
                   "renew.html", "Renew an Adult Passport", "link", None),
    (-38, "18:03", _GOOGLE + "veracruz+all+natural+menu",
     "veracruz all natural menu - Google Search", "phone", None),
    (-38, "18:04", _MENU, "Menu | Veracruz All Natural", "phone", None),
    (-34, "08:55", _GMAIL, _inbox(9), "typed", None),
    (-34, "13:10", _DECK, _DECK_TITLE, "bookmark", None),
    (-34, "15:32", _DECK, _DECK_TITLE, "reload", None),
    (-34, "16:20", _GOOGLE + "warehouse+robotics+market+size",
     "warehouse robotics market size - Google Search", "search", None),
    (-34, "20:05", _DECK, _DECK_TITLE, "bookmark", None),
    (-33, "00:40", _DECK, _DECK_TITLE, "bookmark", None),     # still the -34 journal day
    (-30, "21:00", "chrome://settings/", "Settings", "typed", "chromium"),
    (-29, "10:00", _DECK, _DECK_TITLE, "bookmark", None),
    (-27, "09:05", _GMAIL, _inbox(6), "typed", None),
    (-27, "20:15", _DECK, _DECK_TITLE, "bookmark", None),
    (-27, "20:40", _DECK, _DECK_TITLE, "reload", None),
    (-25, "12:08", "https://tools.usps.com/rcas.htm", None, "failed", "safari"),
    (-25, "12:10", "https://tools.usps.com/rcas.htm", "USPS Passport Appointment Scheduler",
     "typed", None),
    (-24, "07:55", _USPS, "Passports | USPS", "typed", None),
    (-21, "16:00", _DECK, _DECK_TITLE, "bookmark", None),
    (-20, "11:30", _DECK, _DECK_TITLE, "bookmark", None),
    (-20, "13:40", _DECK, _DECK_TITLE, "reload", None),
    (-20, "13:55", "PDF", "Q4-board-deck-final.pdf", "link", None),
    (-20, "18:30", "https://www.google.com/maps/place/Veracruz+All+Natural",
     "Veracruz All Natural - Google Maps", "phone", None),
    (-16, "14:20", _GOOGLE + "lake+bled+itinerary+5+days",
     "lake bled itinerary 5 days - Google Search", "search", None),
    (-16, "14:24", "https://www.slovenia.info/en/stories/lake-bled-itinerary",
     "Lake Bled itinerary: five days in the Julian Alps | Slovenia", "link", None),
    (-16, "14:26", "https://www.youtube.com/embed/demoBledLakeDrone", None, "frame",
     "chromium"),
    (-16, "14:31", "https://claude.ai/chat/7d3b2c1a-5e8f-4a6b-9c0d-b1ed0demo016",
     "Return trip to Lake Bled - Claude", "typed", None),
    (-16, "14:50", "https://www.blejski-grad.si/en/", "Bled Castle | Opening hours and tickets",
     "link", None),
    (-16, "14:58", "https://www.vintgar.si/en/", "Vintgar Gorge | Bled", "link", None),
    (-16, "19:05", _OVERSTORY, "The Overstory - Wikipedia", "link", None),
    (-12, "08:40", _GMAIL, _inbox(11), "typed", None),
    (-11, "20:10", "https://austinclaystudio.example/gift-cards",
     "Gift cards | Austin Clay Studio", "link", None),
    (-10, "21:05", "https://www.opentable.com/r/uchi-austin",
     "Uchi Austin Restaurant - Austin, TX | OpenTable", "link", None),
    (-5, "08:50", _GMAIL, _inbox(27), "typed", None),
    (-5, "22:35", _GOOGLE + "how+to+get+back+into+the+gym+after+a+break",
     "how to get back into the gym after a break - Google Search", "search", None),
    (-3, "22:00", "chrome://history/", "History", "typed", "chromium"),
    (-2, "13:08", _FLIGHTS, "Austin to Ljubljana | Google Flights", "typed", None),
    (-2, "13:15", _FLIGHTS, "Austin to Ljubljana | Google Flights", "reload", None),
    (-2, "19:52", "https://www.vilabled.com/en/", "Vila Bled | Lakeside hotel on Lake Bled",
     "link", None),
    (-1, "08:45", _GMAIL, _inbox(3), "typed", None),
    (-1, "12:17", _MENU, "Menu | Veracruz All Natural", "phone", None),
    (-1, "22:40", _OVERSTORY, "The Overstory - Wikipedia", "link", None),
)

# Chromium page transitions (core type | qualifiers for a whole navigation).
_CORE = {"link": 0, "phone": 0, "typed": 1, "bookmark": 2, "frame": 3,
         "search": 5, "reload": 8}
_CHAIN = 0x30000000                 # CHAIN_START | CHAIN_END: a navigation of its own


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    if combo not in persona.COMBOS:
        raise ValueError(f"browser has no sample for combo '{combo}' "
                         f"(expected one of: {', '.join(persona.COMBOS)})")
    browser = persona.browser(combo)
    family = "safari" if browser == "safari" else "chromium"
    visits = []
    for off, hhmm, url, title, how, only in _VISITS:
        if only is not None and only != family:
            continue
        if url == "PDF":
            url = _PDF["macos" if persona.on_mac(combo) else "windows"]
        elif browser == "edge" and url.startswith("chrome://"):
            url = "edge://" + url[len("chrome://"):]       # Edge's own pages
        unix = int(persona.local_dt(anchor, off, hhmm).timestamp())
        visits.append((unix, url, title, how))
    visits.sort(key=lambda v: v[0])
    dest = Path(dest)
    if family == "chromium":
        vendor, product, phone_guid = _CHROMIUM[browser]
        out = dest / "AppData" / "Local" / vendor / product / "User Data" / "Default" / "History"
        _fresh(out)
        _write_chrome(out, visits, phone_guid)
    else:
        out = dest / "Library" / "Safari" / "History.db"
        _fresh(out)
        _write_safari(out, visits)
    return out


def _fresh(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    for p in (out, out.with_name(out.name + "-journal"), out.with_name(out.name + "-wal")):
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------- Chrome

_CHROME_SCHEMA = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
CREATE TABLE urls(id INTEGER PRIMARY KEY AUTOINCREMENT, url LONGVARCHAR, title LONGVARCHAR,
  visit_count INTEGER DEFAULT 0 NOT NULL, typed_count INTEGER DEFAULT 0 NOT NULL,
  last_visit_time INTEGER NOT NULL, hidden INTEGER DEFAULT 0 NOT NULL);
CREATE TABLE visits(id INTEGER PRIMARY KEY AUTOINCREMENT, url INTEGER NOT NULL,
  visit_time INTEGER NOT NULL, from_visit INTEGER, external_referrer_url TEXT,
  transition INTEGER DEFAULT 0 NOT NULL, segment_id INTEGER,
  visit_duration INTEGER DEFAULT 0 NOT NULL,
  incremented_omnibox_typed_score BOOLEAN DEFAULT FALSE NOT NULL, opener_visit INTEGER,
  originator_cache_guid TEXT, originator_visit_id INTEGER, originator_from_visit INTEGER,
  originator_opener_visit INTEGER, is_known_to_sync BOOLEAN DEFAULT FALSE NOT NULL,
  consider_for_ntp_most_visited BOOLEAN DEFAULT FALSE NOT NULL,
  visited_link_id INTEGER DEFAULT 0 NOT NULL, app_id TEXT);
CREATE TABLE visit_source(id INTEGER PRIMARY KEY, source INTEGER NOT NULL);
CREATE TABLE keyword_search_terms (keyword_id INTEGER NOT NULL, url_id INTEGER NOT NULL,
  term LONGVARCHAR NOT NULL, normalized_term LONGVARCHAR NOT NULL);
CREATE INDEX visits_url_index ON visits (url);
CREATE INDEX visits_time_index ON visits (visit_time);
CREATE INDEX urls_url_index ON urls (url);
CREATE INDEX keyword_search_terms_index1 ON keyword_search_terms (keyword_id, normalized_term);
"""
_PHONE_GUID = "c2a7e9f0-galaxy-s24-demo"


def _write_chrome(out: Path, visits: list, phone_guid: str = _PHONE_GUID) -> None:
    conn = sqlite3.connect(out)
    try:
        conn.executescript(_CHROME_SCHEMA)
        conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                         [("version", "69"), ("last_compatible_version", "16"),
                          ("mmap_status", "-1")])
        url_ids: dict[str, int] = {}
        for unix, url, title, how in visits:
            if url not in url_ids:
                url_ids[url] = len(url_ids) + 1
        stats: dict[str, dict] = {}
        for i, (unix, url, title, how) in enumerate(visits, start=1):
            # Sub-second noise, as Chrome writes it: the connector truncates.
            micros = (unix + CHROME_EPOCH) * 1_000_000 + 123_457 + i
            s = stats.setdefault(url, {"title": None, "visits": 0, "typed": 0,
                                       "last": 0, "hidden": 1})
            if how != "frame":
                s["visits"] += 1
                s["hidden"] = 0
            s["typed"] += how == "typed"
            s["last"] = max(s["last"], micros)
            if title:
                s["title"] = title          # Chrome keeps only the latest title
            transition = _CORE[how] | (0 if how == "frame" else _CHAIN)
            conn.execute(
                "INSERT INTO visits (id, url, visit_time, from_visit, transition, "
                "segment_id, visit_duration, originator_cache_guid, is_known_to_sync) "
                "VALUES (?, ?, ?, 0, ?, 0, ?, ?, 1)",
                (i, url_ids[url], micros, transition, 45_000_000,
                 phone_guid if how == "phone" else None))
            if how == "phone":
                conn.execute("INSERT INTO visit_source (id, source) VALUES (?, 0)", (i,))
        for url, uid in url_ids.items():
            s = stats[url]
            conn.execute("INSERT INTO urls (id, url, title, visit_count, typed_count, "
                         "last_visit_time, hidden) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (uid, url, s["title"] or "", s["visits"], s["typed"], s["last"],
                          s["hidden"]))
            if url.startswith(_GOOGLE):
                term = url[len(_GOOGLE):].replace("+", " ")
                conn.execute("INSERT INTO keyword_search_terms VALUES (2, ?, ?, ?)",
                             (uid, term, term.lower()))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- Safari

_SAFARI_SCHEMA = """
CREATE TABLE history_items (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE,
  domain_expansion TEXT NULL, visit_count INTEGER NOT NULL, daily_visit_counts BLOB NOT NULL,
  weekly_visit_counts BLOB NULL, autocomplete_triggers BLOB NULL,
  should_recompute_derived_visit_counts INTEGER NOT NULL, visit_count_score INTEGER NOT NULL,
  status_code INTEGER NOT NULL DEFAULT 0);
CREATE TABLE history_visits (id INTEGER PRIMARY KEY AUTOINCREMENT,
  history_item INTEGER NOT NULL REFERENCES history_items(id) ON DELETE CASCADE,
  visit_time REAL NOT NULL, title TEXT NULL, load_successful BOOLEAN NOT NULL DEFAULT 1,
  http_non_get BOOLEAN NOT NULL DEFAULT 0, synthesized BOOLEAN NOT NULL DEFAULT 0,
  redirect_source INTEGER NULL UNIQUE REFERENCES history_visits(id) ON DELETE CASCADE,
  redirect_destination INTEGER NULL UNIQUE REFERENCES history_visits(id) ON DELETE CASCADE,
  origin INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
  attributes INTEGER NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0);
CREATE TABLE history_tombstones (id INTEGER PRIMARY KEY AUTOINCREMENT,
  start_time REAL NOT NULL, end_time REAL NOT NULL, url TEXT,
  generation INTEGER NOT NULL DEFAULT 0);
CREATE TABLE metadata (key TEXT NOT NULL UNIQUE, value);
CREATE TABLE history_client_versions (client_version INTEGER PRIMARY KEY,
  last_seen REAL NOT NULL);
CREATE TABLE history_event_listeners (listener_name TEXT PRIMARY KEY NOT NULL UNIQUE,
  last_seen REAL NOT NULL);
CREATE TABLE history_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
  event_time REAL NOT NULL, pending_listeners TEXT NOT NULL, value BLOB);
CREATE INDEX history_items__domain_expansion ON history_items (domain_expansion);
CREATE INDEX history_visits__last_visit ON history_visits (history_item, visit_time DESC,
  synthesized ASC);
CREATE INDEX history_visits__origin ON history_visits (origin, generation);
"""


def _expansion(url: str) -> str | None:
    """Safari's domain_expansion: the host without 'www.' and its last label."""
    host = url.split("/")[2] if url.count("/") >= 2 else ""
    labels = host.removeprefix("www.").split(".")
    return ".".join(labels[:-1]) or None


def _write_safari(out: Path, visits: list) -> None:
    conn = sqlite3.connect(out)
    try:
        conn.executescript(_SAFARI_SCHEMA)
        item_ids: dict[str, int] = {}
        counts: dict[str, int] = {}
        for unix, url, title, how in visits:
            item_ids.setdefault(url, len(item_ids) + 1)
            counts[url] = counts.get(url, 0) + (how != "failed")
        for url, iid in item_ids.items():
            conn.execute("INSERT INTO history_items (id, url, domain_expansion, visit_count, "
                         "daily_visit_counts, should_recompute_derived_visit_counts, "
                         "visit_count_score) VALUES (?, ?, ?, ?, ?, 0, ?)",
                         (iid, url, _expansion(url), counts[url], b"", 100 * counts[url]))
        for i, (unix, url, title, how) in enumerate(visits, start=1):
            conn.execute("INSERT INTO history_visits (id, history_item, visit_time, title, "
                         "load_successful, origin) VALUES (?, ?, ?, ?, ?, ?)",
                         (i, item_ids[url], unix - APPLE_EPOCH + 0.318 + i / 1000, title,
                          0 if how == "failed" else 1, 1 if how == "phone" else 0))
        conn.execute("INSERT INTO history_client_versions VALUES (20, ?)",
                     (float(visits[-1][0] - APPLE_EPOCH),))
        conn.commit()
    finally:
        conn.close()

"""Sample Spotify downloads: the persona's listening, in both of Spotify's
own export formats, as the `spotify` connector reads them.

Both downloads arrive from Account > Privacy settings > Download your data
as `my_spotify_data.zip`; the second one saved beside the first becomes
`my_spotify_data (1).zip`. The sample is a `Spotify/` folder holding both —
the way the guide says to import them, so the extended history takes
precedence for the days both cover:

  my_spotify_data.zip       Account data, `Spotify Account Data/`:
                              StreamingHistory_music_0.json   recent music
                              StreamingHistory_podcast_0.json recent podcasts
                              Userdata.json                   the account
                              YourLibrary.json                saved tracks
  my_spotify_data (1).zip   Extended streaming history, `Spotify Extended
                            Streaming History/Streaming_History_Audio_
                            <trip year>-<anchor year>_0.json`: the Lake Bled
                            trip four years ago (and the podcast that
                            planned it), plus the last two days, which the
                            Account data also has — at the minute, without
                            URIs.

What the persona listened to: focus music on the deck days (-34, -27) and a
pre-board playlist before the 14:00 board meeting (-20); a running mix at
Zilker (-27); Japanese Breakfast on the day the gift list named their
concert (-25) and on the drive home from Maya's birthday (-9); a travel
podcast about Slovenia on the day the return trip took shape (-16, in two
sittings) and about Ljubljana on the day Jake said yes (-2); the gym (-2).
Short skips (< 30 s) are in there too, as on every real account.

The same account on either phone, so both combos get both downloads; only
the extended history's `platform` differs ('android' / 'ios'). Its IP
addresses are documentation addresses (203.0.113.0/24, RFC 5737) — and are
never imported anyway. Deterministic for (anchor, combo).
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import persona

USERNAME = "alexrivera94"
SHOW = "The Slow Traveler"
SLOVENIA_EPISODE = "Slovenia: Lake Bled, Vintgar Gorge and the Julian Alps"
LJUBLJANA_EPISODE = "Ljubljana in Three Days"
TRIP_EPISODE = "Slovenia's Lakes: Bled and Bohinj"

# (artist, track, album, seconds long)
TRACKS = {
    "awake": ("Tycho", "Awake", "Awake", 284),
    "kerala": ("Bonobo", "Kerala", "Migration", 242),
    "cirrus": ("Bonobo", "Cirrus", "The North Borders", 351),
    "says": ("Nils Frahm", "Says", "Spaces", 498),
    "saman": ("Ólafur Arnalds", "saman", "re:member", 190),
    "brightside": ("The Killers", "Mr. Brightside", "Hot Fuss", 222),
    "dog_days": ("Florence + The Machine", "Dog Days Are Over", "Lungs", 252),
    "hbfs": ("Daft Punk", "Harder, Better, Faster, Stronger", "Discovery", 224),
    "all_my_friends": ("LCD Soundsystem", "All My Friends", "Sound of Silver", 457),
    "lose_yourself": ("Eminem", "Lose Yourself", "8 Mile", 326),
    "be_sweet": ("Japanese Breakfast", "Be Sweet", "Jubilee", 203),
    "paprika": ("Japanese Breakfast", "Paprika", "Jubilee", 254),
    "slide_tackle": ("Japanese Breakfast", "Slide Tackle", "Jubilee", 290),
    "posing": ("Japanese Breakfast", "Posing in Bondage", "Jubilee", 263),
    "holocene": ("Bon Iver", "Holocene", "Bon Iver, Bon Iver", 337),
    "mykonos": ("Fleet Foxes", "Mykonos", "Sun Giant", 275),
}

# (offset, local start 'HH:MM:SS', track key, seconds played — None plays it
# through; a number under 30 is a skip)
RECENT_MUSIC: tuple[tuple[int, str, str, int | None], ...] = (
    (-34, "14:05:12", "awake", None),
    (-34, "14:09:58", "kerala", None),
    (-34, "14:14:02", "cirrus", 12),
    (-34, "14:14:15", "says", None),
    (-34, "14:22:35", "saman", None),
    (-34, "14:25:47", "awake", None),
    (-27, "18:15:04", "brightside", None),
    (-27, "18:18:48", "dog_days", None),
    (-27, "18:23:02", "hbfs", None),
    (-27, "18:26:48", "lose_yourself", None),
    (-27, "21:04:30", "awake", None),
    (-27, "21:09:16", "says", None),
    (-25, "20:31:09", "be_sweet", None),
    (-25, "20:34:34", "paprika", None),
    (-25, "20:38:50", "posing", 15),
    (-25, "20:39:07", "slide_tackle", None),
    (-25, "20:44:00", "be_sweet", None),
    (-20, "12:40:21", "hbfs", None),
    (-20, "12:44:07", "brightside", None),
    (-20, "12:47:51", "all_my_friends", None),
    (-20, "12:55:30", "awake", None),
    (-9, "21:14:40", "be_sweet", None),
    (-2, "06:31:10", "lose_yourself", None),
    (-2, "06:36:40", "brightside", 95),
    (-2, "06:38:20", "hbfs", None),
    (-1, "16:02:44", "saman", None),
)
# (offset, local start, episode, seconds played)
RECENT_PODCASTS: tuple[tuple[int, str, str, int], ...] = (
    (-16, "15:30:05", SLOVENIA_EPISODE, 2280),
    (-16, "17:10:40", SLOVENIA_EPISODE, 1200),
    (-2, "20:02:15", LJUBLJANA_EPISODE, 1860),
)
# The extended history's own part: the Bled trip (trip day, Ljubljana local
# time — afternoons and evenings, so the home-timezone day is the trip day)
# and the podcast three days before it, at home in Austin.
TRIP_MUSIC: tuple[tuple[int, str, str], ...] = (
    (0, "17:41:05", "holocene"),
    (0, "17:46:50", "mykonos"),
    (1, "21:15:30", "holocene"),
    (2, "16:10:12", "mykonos"),
    (2, "20:40:02", "holocene"),
    (2, "20:45:45", "be_sweet"),
)
TRIP_PODCAST_DAYS_BEFORE = 3
EXTENDED_RECENT = (-2,)          # days the extended history overlaps Account data


def _check(combo: str) -> None:
    if combo not in persona.COMBOS:
        raise ValueError(f"unknown combo '{combo}' (expected one of "
                         f"{', '.join(persona.COMBOS)})")


def _uri(kind: str, *parts: str) -> str:
    """A Spotify-shaped URI: 22 base-62 characters, derived from the names."""
    digest = int(hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest(), 16)
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    out = ""
    for _ in range(22):
        digest, r = divmod(digest, 62)
        out += chars[r]
    return f"spotify:{kind}:{out}"


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _plays(anchor: date) -> list[dict]:
    """Every play, oldest first: {end (UTC), ms, kind, ...}."""
    out = []
    for off, hhmm, key, played in RECENT_MUSIC:
        artist, track, album, length = TRACKS[key]
        start = _utc(persona.local_dt(anchor, off, hhmm))
        secs = played if played is not None else length
        out.append({"start": start, "end": start + timedelta(seconds=secs),
                    "ms": secs * 1000 + 417, "kind": "music", "creator": artist,
                    "title": track, "album": album, "offset": off,
                    "skipped": played is not None, "where": "US"})
    for off, hhmm, episode, secs in RECENT_PODCASTS:
        start = _utc(persona.local_dt(anchor, off, hhmm))
        out.append({"start": start, "end": start + timedelta(seconds=secs),
                    "ms": secs * 1000 + 233, "kind": "podcast", "creator": SHOW,
                    "title": episode, "album": None, "offset": off,
                    "skipped": False, "where": "US"})
    for trip_day, hhmm, key in TRIP_MUSIC:
        artist, track, album, length = TRACKS[key]
        start = _utc(persona.trip_dt(trip_day, hhmm[:5], anchor)) + \
            timedelta(seconds=int(hhmm[6:]))
        out.append({"start": start, "end": start + timedelta(seconds=length),
                    "ms": length * 1000 + 91, "kind": "music", "creator": artist,
                    "title": track, "album": album, "offset": None,
                    "skipped": False, "where": "SI"})
    first = persona.bled_trip_days(anchor)[0] - timedelta(days=TRIP_PODCAST_DAYS_BEFORE)
    start = _utc(datetime(first.year, first.month, first.day, 20, 5, 30,
                          tzinfo=ZoneInfo(persona.TIMEZONE)))
    out.append({"start": start, "end": start + timedelta(seconds=2650),
                "ms": 2_650_120, "kind": "podcast", "creator": SHOW,
                "title": TRIP_EPISODE, "album": None, "offset": None,
                "skipped": False, "where": "US"})
    return sorted(out, key=lambda p: (p["end"], p["title"]))


def _end_minute(p: dict) -> str:
    return p["end"].strftime("%Y-%m-%d %H:%M")


def account_music(anchor: date) -> list[dict]:
    return [{"endTime": _end_minute(p), "artistName": p["creator"],
             "trackName": p["title"], "msPlayed": p["ms"]}
            for p in _plays(anchor) if p["kind"] == "music" and p["offset"] is not None]


def account_podcasts(anchor: date) -> list[dict]:
    return [{"endTime": _end_minute(p), "podcastName": p["creator"],
             "episodeName": p["title"], "msPlayed": p["ms"]}
            for p in _plays(anchor) if p["kind"] == "podcast" and p["offset"] is not None]


def extended(anchor: date, combo: str) -> list[dict]:
    """The extended rows, every field Spotify writes — the privacy ones too."""
    platform = "ios" if persona.has_iphone(combo) else "android"
    rows = []
    for n, p in enumerate(_plays(anchor)):
        if p["offset"] is not None and p["offset"] not in EXTENDED_RECENT:
            continue
        music = p["kind"] == "music"
        rows.append({
            "ts": p["end"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "platform": platform,
            "ms_played": p["ms"],
            "conn_country": p["where"],
            "ip_addr": f"203.0.113.{40 + n % 50}",
            "master_metadata_track_name": p["title"] if music else None,
            "master_metadata_album_artist_name": p["creator"] if music else None,
            "master_metadata_album_album_name": p["album"] if music else None,
            "spotify_track_uri": _uri("track", p["creator"], p["title"]) if music else None,
            "episode_name": None if music else p["title"],
            "episode_show_name": None if music else p["creator"],
            "spotify_episode_uri": None if music else _uri("episode", p["creator"], p["title"]),
            "audiobook_title": None,
            "audiobook_uri": None,
            "audiobook_chapter_uri": None,
            "audiobook_chapter_title": None,
            "reason_start": "clickrow" if n % 4 == 0 else "trackdone",
            "reason_end": "fwdbtn" if p["skipped"] else "trackdone",
            "shuffle": music and n % 3 == 0,
            "skipped": p["skipped"],
            "offline": p["where"] == "SI" and n % 2 == 0,
            "offline_timestamp": int(p["start"].timestamp()) if p["where"] == "SI" else None,
            "incognito_mode": False,
        })
    return rows


def _userdata() -> dict:
    me = persona.OWNER
    return {"username": USERNAME, "email": me.email, "country": "US",
            "createdFromFacebook": False, "facebookUid": None,
            "birthdate": f"{me.birth_year}-{me.birthday[0]:02d}-{me.birthday[1]:02d}",
            "gender": "neutral", "postalCode": "78702", "mobileNumber": me.phone,
            "mobileOperator": None, "mobileBrand": None, "creationTime": "2014-05-11"}


def _library(anchor: date) -> dict:
    saved = ("be_sweet", "paprika", "awake", "holocene")
    return {"tracks": [{"artist": TRACKS[k][0], "album": TRACKS[k][2],
                        "track": TRACKS[k][1], "uri": _uri("track", TRACKS[k][0],
                                                            TRACKS[k][1])}
                       for k in saved],
            "albums": [], "shows": [{"name": SHOW, "publisher": "Slow Traveler Media",
                                     "uri": _uri("show", SHOW)}],
            "episodes": [], "bannedTracks": [], "artists": [], "bannedArtists": [],
            "other": []}


def _zip(path: Path, members: list[tuple[str, str]], anchor: date) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (anchor.year, anchor.month, anchor.day, 9, 0, 0)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text.encode("utf-8"))
    return path


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def write_sample(dest: Path, anchor: date, combo: str) -> Path:
    """`Spotify/` holding both downloads, ready for `lifecrm observe spotify`."""
    _check(combo)
    folder = Path(dest) / "Spotify"
    acct = "Spotify Account Data/"
    _zip(folder / "my_spotify_data.zip", [
        (acct + "StreamingHistory_music_0.json", _dump(account_music(anchor))),
        (acct + "StreamingHistory_podcast_0.json", _dump(account_podcasts(anchor))),
        (acct + "Userdata.json", _dump(_userdata())),
        (acct + "YourLibrary.json", _dump(_library(anchor))),
    ], anchor)
    trip_year = persona.bled_trip_days(anchor)[0].year
    ext = "Spotify Extended Streaming History/"
    _zip(folder / "my_spotify_data (1).zip", [
        (ext + f"Streaming_History_Audio_{trip_year}-{anchor.year}_0.json",
         _dump(extended(anchor, combo))),
    ], anchor)
    return folder

"""Spotify connector: what you listened to, one record per track per day.

Listening is a diary kept without trying: the focus music on the day the
board deck was due, the album behind a concert-ticket gift idea, the travel
podcast the week a trip took shape. Spotify hands all of it over under
Account > Privacy settings > Download your data. This connector reads those
downloads. It never logs in, never calls Spotify's API, and never sends
anything.

Two downloads, read the same way
--------------------------------
  Account data         arrives in about five days and covers roughly the
                       last year, in `Spotify Account Data/`:
                         StreamingHistory_music_N.json
                           [{endTime: 'YYYY-MM-DD HH:MM' (UTC), artistName,
                             trackName, msPlayed}]
                         StreamingHistory_podcast_N.json
                           [{endTime, podcastName, episodeName, msPlayed}]
                       Older downloads have one StreamingHistoryN.json that
                       mixes both, podcasts under artistName/trackName.
                       Nothing tells them apart there, so those records
                       carry no kind.
  Extended streaming   arrives in up to thirty days and covers the account's
  history              whole life, in `Spotify Extended Streaming History/`:
                         Streaming_History_Audio_<years>_N.json (and _Video_;
                         endsong_N.json in downloads from before 2023)
                           [{ts (ISO, UTC), ms_played,
                             master_metadata_track_name,
                             master_metadata_album_artist_name,
                             master_metadata_album_album_name,
                             spotify_track_uri, episode_name,
                             episode_show_name, spotify_episode_uri,
                             audiobook_title, audiobook_uri,
                             audiobook_chapter_title, skipped,
                             incognito_mode, ...}]

Both arrive as `my_spotify_data.zip`. A target is one of those zips, a folder
(searched recursively, zips inside it included — so a folder holding
my_spotify_data.zip and my_spotify_data (1).zip is one target), or one of the
.json files itself. A play's time is the instant it ENDED; its start is that
minus the time played, and the start dates it.

Privacy: what is never read
---------------------------
Every extended-history play also carries the IP address it was played from,
the country, the device's user agent and platform, and whether it was
offline. None of that is kept — not in a record, not in value_json, not in
the report; only the fields named above are read from a play. Userdata.json
(email, birth date, postal code, phone) is read for its `username` alone,
which labels the import.

Volume control: one record per track per day
--------------------------------------------
A decade of listening is hundreds of thousands of plays; "what was I
listening to the week of the board meeting" needs the track and the day, not
every repeat. So each `music_play` record is one track (or podcast episode,
or audiobook) on one journal day — home timezone, the configured rollover
hour applied, so a 1 a.m. play counts toward the evening before:

  title       the track, episode or audiobook
  body        the artist or show (an audiobook's author is not exported)
  value       minutes played that day (unit 'min'), plays of 30 s or more
  value_json  {plays, skipped (plays Spotify marks skipped; extended history
              only), first_play, last_play, album, uri, kind: 'music' |
              'podcast' | 'audiobook', video (from a _Video_ file), chapter
              (an audiobook's last chapter that day)}
  external_id sha1(artist or show, track or episode, day), case- and
              space-insensitive

Plays under 30 seconds are dropped and counted: they are skips and previews,
and Spotify itself only counts a stream from 30 s.

Files are read and parsed one at a time — Spotify already splits a history
into files of some ten thousand plays — and only the fields above are kept
from each play until the day's records are built.

Identity is the names, not the Spotify URI, on purpose: Account data has no
URIs, and both downloads cover the last year, so a URI-keyed record and a
name-keyed record would be the same listening counted twice. The URI is kept
in value_json.

The two downloads together
--------------------------
In one scan the extended history wins: for every journal day it covers, the
Account-data plays of that day are dropped (counted) — the extended history
has the same plays with seconds, URIs, albums and skips. Imported on their
own, one after the other, the same track-day is the same record. An extended
play ends at the second, Account data's at the minute, so the extended
record is (almost always) newer and replaces the Account-data one, audited;
Account data imported after the extended history is never newer, and the
fuller record is kept — the report counts those on one line. Putting both
downloads in one folder avoids the churn.

Re-importing, and what wins
---------------------------
A record's last_modified_at is its last play. Today's record grows while you
keep listening and the next import updates it; an earlier day is identical
on every re-import. Overlapping downloads (two Account-data zips a month
apart) in one folder are read once: a play listed in more than one file
counts once.

Mirroring: none. Account data only reaches back a year: a play missing from
a later download has aged out of the window, not been deleted, so the archive
remembers.

One archive is one listener: ids do not name the account (Account data's
history files do not either), so a scan holding two accounts' downloads is
refused, and a second person's plays imported on their own would merge into
the same track-days.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "spotify"
streams = ("music_play",)
# No stale_after_days: both downloads are requested by hand, now and then.

MIN_PLAY_MS = 30_000
_ISO = "%Y-%m-%dT%H:%M:%SZ"

_ROLES = (
    ("account_music", re.compile(r"^StreamingHistory_music_\d+\.json$", re.I)),
    ("account_podcast", re.compile(r"^StreamingHistory_podcast_\d+\.json$", re.I)),
    ("account_legacy", re.compile(r"^StreamingHistory\d+\.json$", re.I)),
    ("extended_audio", re.compile(r"^(Streaming_History_Audio_.+|endsong_\d+)\.json$",
                                  re.I)),
    ("extended_video", re.compile(r"^(Streaming_History_Video_.+|endvideo(_\d+)?)\.json$",
                                  re.I)),
)
_FAMILY_LABELS = {"account": "Account data", "extended": "extended streaming history"}
SHORT = ("play under 30 seconds (a skip or a preview — Spotify itself counts a "
         "stream from 30 s)")
COVERED = ("Account-data play on a day the extended history also covers (the "
           "extended history is the fuller record of that day)")
OVERLAP = "play listed in more than one file (overlapping downloads) — counted once"
NAMELESS = ("play naming no track, episode or audiobook (removed from Spotify, or "
            "a local file)")


# ---------------------------------------------------------------- values

def _text(value) -> str | None:
    if not isinstance(value, str):
        return None
    s = " ".join(value.split())
    return s or None


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _ms(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    try:
        n = int(str(value).strip())
    except ValueError:
        return None
    return n if n >= 0 else None


def _epoch(text: str | None) -> int | None:
    iso = base.iso_utc(text) if isinstance(text, str) else None
    if iso is None:
        return None
    return int(datetime.strptime(iso, _ISO).replace(tzinfo=timezone.utc).timestamp())


def _account_epoch(value) -> int | None:
    """Account data's endTime: 'YYYY-MM-DD HH:MM', UTC, to the minute."""
    if not isinstance(value, str):
        return None
    s = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    return _epoch(s)


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(_ISO)


# ---------------------------------------------------------------- plays

class _Play:
    """One play as read, before any aggregation. Only what a record keeps."""

    __slots__ = ("family", "kind", "creator", "title", "album", "uri", "chapter",
                 "start", "end", "ms", "skipped", "video", "private", "file",
                 "day")

    def __init__(self, family, kind, creator, title, album, uri, chapter, end, ms,
                 skipped, video, private, file, cfg):
        self.family, self.kind = family, kind
        self.creator, self.title = creator, title
        self.album, self.uri, self.chapter = album, uri, chapter
        self.end, self.ms = end, ms
        self.start = end - ms // 1000
        self.skipped, self.video, self.private = skipped, video, private
        self.file = file
        self.day = base.journal_date_for_iso(_iso(self.start), cfg)

    def signature(self) -> tuple:
        return (self.family, _norm(self.creator), _norm(self.title), self.end, self.ms)


class _Acc:
    """What one scan has gathered so far, across every file it reads."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.plays: list[_Play] = []
        self.skipped: Counter = Counter()
        self.unmapped: Counter = Counter()
        self.examples: dict[str, str] = {}
        self.usernames: set[str] = set()
        self.files: list[dict] = []
        self.unread: list[str] = []

    def unknown(self, what: str, where: str) -> None:
        self.unmapped[what] += 1
        self.examples.setdefault(what, where)


_MAX_PLAY_MS = 24 * 3600 * 1000


def _sane(end: int, ms: int, rel: str, acc: _Acc) -> bool:
    """No single play lasts a day, or started before 1970: such a row is a
    damaged file, counted rather than dated wrongly."""
    if ms > _MAX_PLAY_MS or end - ms // 1000 <= 0:
        acc.unknown("play with an impossible time or length", rel)
        return False
    return True


def _role(fname: str) -> str | None:
    base_name = PurePosixPath(fname).name
    for role, pattern in _ROLES:
        if pattern.match(base_name):
            return role
    return "userdata" if base_name.lower() == "userdata.json" else None


def _extended(row: dict, video: bool, n: int, rel: str, acc: _Acc) -> _Play | None:
    end = _epoch(row.get("ts"))
    if end is None:
        acc.unknown("play with no readable ts", rel)
        return None
    ms = _ms(row.get("ms_played"))
    if ms is None:
        acc.unknown("play with no readable ms_played", rel)
        return None
    if not _sane(end, ms, rel, acc):
        return None
    if isinstance(row.get("username"), str) and row["username"].strip():
        acc.usernames.add(row["username"].strip())
    book = _text(row.get("audiobook_title"))
    episode = _text(row.get("episode_name"))
    track = _text(row.get("master_metadata_track_name"))
    album = chapter = None
    if book:
        kind, creator, title = "audiobook", None, book
        uri = _text(row.get("audiobook_uri"))
        chapter = _text(row.get("audiobook_chapter_title"))
    elif episode:
        kind, creator, title = "podcast", _text(row.get("episode_show_name")), episode
        uri = _text(row.get("spotify_episode_uri"))
    elif track:
        kind, creator, title = ("music", _text(row.get("master_metadata_album_artist_name")),
                                track)
        album = _text(row.get("master_metadata_album_album_name"))
        uri = _text(row.get("spotify_track_uri"))
    else:
        acc.skipped[NAMELESS] += 1
        return None
    skipped = row.get("skipped") if isinstance(row.get("skipped"), bool) else None
    return _Play("extended", kind, creator, title, album, uri, chapter, end, ms,
                 skipped, video, row.get("incognito_mode") is True, n, acc.cfg)


def _account(row: dict, role: str, n: int, rel: str, acc: _Acc) -> _Play | None:
    end = _account_epoch(row.get("endTime"))
    if end is None:
        acc.unknown("play with no readable endTime", rel)
        return None
    ms = _ms(row.get("msPlayed"))
    if ms is None:
        acc.unknown("play with no readable msPlayed", rel)
        return None
    if not _sane(end, ms, rel, acc):
        return None
    if "podcastName" in row or "episodeName" in row:
        kind = "podcast"
        creator, title = _text(row.get("podcastName")), _text(row.get("episodeName"))
    else:
        # StreamingHistoryN.json mixes music and podcasts under the same keys.
        kind = "music" if role == "account_music" else None
        creator, title = _text(row.get("artistName")), _text(row.get("trackName"))
    if not title:
        acc.skipped[NAMELESS] += 1
        return None
    return _Play("account", kind, creator, title, None, None, None, end, ms,
                 None, False, False, n, acc.cfg)


def _parse(raw: bytes, rel: str):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise DomainError(f"{rel} is not valid UTF-8: {e}") from e
    except json.JSONDecodeError as e:
        raise DomainError(f"{rel} is not valid JSON: line {e.lineno} "
                          f"column {e.colno}: {e.msg}") from e


def _read_history(rel: str, role: str, raw: bytes, alone: bool, acc: _Acc) -> None:
    data = _parse(raw, rel)
    if not isinstance(data, list):
        what = (f"is a JSON {type(data).__name__}, not a list of plays — expected "
                f"StreamingHistory_music_N.json or Streaming_History_Audio_*.json")
        if alone:
            raise DomainError(f"{rel} {what}")
        acc.unknown("history file that is not a list of plays", rel)
        return
    n = len(acc.files)
    families: Counter = Counter()
    for row in data:
        if not isinstance(row, dict):
            acc.unknown("play that is not an object", rel)
            continue
        if "ts" in row and "ms_played" in row:
            play = _extended(row, role == "extended_video", n, rel, acc)
        elif "endTime" in row and "msPlayed" in row:
            play = _account(row, role, n, rel, acc)
        else:
            keys = ", ".join(sorted(row)[:6]) or "no keys"
            if alone and not families and not acc.plays:
                raise DomainError(
                    f"{rel} is a list, but not of Spotify plays — the first item has: "
                    f"{keys} (expected endTime/msPlayed or ts/ms_played)")
            acc.unknown(f"play of an unknown shape ({keys})", rel)
            continue
        if play is not None:
            acc.plays.append(play)
            families[play.family] += 1
    family = (families.most_common(1)[0][0] if families
              else "extended" if role.startswith("extended") else "account")
    acc.files.append({"file": rel, "family": family, "plays": sum(families.values())})


def _read_userdata(rel: str, raw: bytes, acc: _Acc) -> None:
    """The username, and nothing else: Userdata.json also holds the email,
    birth date, postal code and phone number."""
    try:
        data = _parse(raw, rel)
    except DomainError:
        acc.unknown("Userdata.json that is not readable JSON", rel)
        return
    user = data.get("username") if isinstance(data, dict) else None
    if isinstance(user, str) and user.strip():
        acc.usernames.add(user.strip())


# ---------------------------------------------------------------- targets

def _junk(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return any(p.startswith(".") or p.lower() == "__macosx" for p in parts)


def _zip_members(zf: zipfile.ZipFile, prefix: str, acc: _Acc):
    for info in sorted(zf.infolist(), key=lambda i: i.filename):
        if info.is_dir() or _junk(info.filename):
            continue
        rel = f"{prefix}{info.filename}"
        role = _role(info.filename)
        if role is None:
            acc.unread.append(rel)
            continue
        yield rel, role, (lambda info=info: zf.read(info))


def _walk(target: Path, acc: _Acc):
    """Yield (rel, role, read) for each history file (and Userdata.json) in a
    stable order; `read()` returns its bytes, one file at a time. Everything
    else is listed in acc.unread."""
    if target.is_dir():
        for p in sorted(target.rglob("*")):
            rel = p.relative_to(target).as_posix()
            if not p.is_file() or _junk(rel):
                continue
            if p.suffix.lower() == ".zip" and zipfile.is_zipfile(p):
                with zipfile.ZipFile(p) as zf:
                    yield from _zip_members(zf, f"{rel}/", acc)
                continue
            role = _role(p.name)
            if role is None:
                acc.unread.append(rel)
                continue
            yield rel, role, p.read_bytes
        return
    if zipfile.is_zipfile(target):
        with zipfile.ZipFile(target) as zf:
            yield from _zip_members(zf, "", acc)
        return
    if target.suffix.lower() != ".json":
        raise DomainError(
            f"{target.name} is not a .zip, a folder or a .json file — expected "
            f"Spotify's my_spotify_data.zip or one of its StreamingHistory files")
    yield target.name, _role(target.name) or "sniff", target.read_bytes


def default_target(cfg: Config) -> Path:
    """[connectors.spotify] path, else the one my_spotify_data*.zip sitting
    where downloads land on this machine. Several are refused: put them in one
    folder and pass that, so the extended history can take precedence."""
    settings = cfg.connector_settings(name)
    if settings.get("path"):
        p = cfg.resolve(settings["path"])
        if not p.exists():
            raise DomainError(f"[connectors.spotify] path does not exist: {p}")
        return p
    folders = [platforms.home() / "Downloads", *platforms.onedrive_candidates()]
    if platforms.is_mac():
        folders.append(platforms.icloud_drive())
    found, seen = [], set()
    for folder in folders:
        try:
            hits = sorted(folder.glob("my_spotify_data*.zip"))
        except OSError:
            continue
        for p in hits:
            if str(p).lower() not in seen and p.is_file():
                seen.add(str(p).lower())
                found.append(p)
    if len(found) == 1:
        return found[0]
    if found:
        raise base.Ambiguous("found several Spotify downloads — put them in one folder and "
                          "pass the folder: " + "; ".join(str(p) for p in found))
    raise DomainError(
        "no Spotify download found — looked for my_spotify_data*.zip in "
        + ", ".join(str(f) for f in folders)
        + "; request it under Account > Privacy settings > Download your data "
          "(docs/spotify.md), pass the zip or folder, or set [connectors.spotify] path")


# ---------------------------------------------------------------- scan

def _dedupe(plays: list[_Play], acc: _Acc) -> list[_Play]:
    """A play listed in several files (two overlapping downloads) once. The
    same play repeated within ONE file — a short track played twice in a
    minute — keeps both: the file with the most copies of it decides."""
    groups: dict[tuple, dict[int, list[_Play]]] = {}
    for p in plays:
        groups.setdefault(p.signature(), {}).setdefault(p.file, []).append(p)
    kept = []
    for by_file in groups.values():
        if len(by_file) == 1:
            kept.extend(next(iter(by_file.values())))
            continue
        _, best = max(by_file.items(), key=lambda kv: (len(kv[1]), -kv[0]))
        kept.extend(best)
        acc.skipped[OVERLAP] += sum(len(v) for v in by_file.values()) - len(best)
    return kept


def _bucket_key(p: _Play) -> tuple[str, str, str]:
    return (_norm(p.creator), _norm(p.title), p.day)


def _records(plays: list[_Play]) -> tuple[list[dict], int]:
    buckets: dict[tuple, dict] = {}
    private = 0
    order = sorted(plays, key=lambda p: (p.end, p.ms, _norm(p.creator), _norm(p.title),
                                         p.family, p.file))
    for p in order:
        b = buckets.get(_bucket_key(p))
        if b is None:
            b = buckets[_bucket_key(p)] = {
                "plays": 0, "ms": 0, "skips": 0, "skip_known": False,
                "first": p.start, "last": p.end, "kind": None, "album": None,
                "uri": None, "chapter": None, "video": False}
        b["plays"] += 1
        b["ms"] += p.ms
        b["first"] = min(b["first"], p.start)
        b["last"] = max(b["last"], p.end)
        # Plays come oldest first: the latest spelling and detail win.
        b["creator"], b["title"] = p.creator, p.title
        for field in ("kind", "album", "uri", "chapter"):
            if getattr(p, field):
                b[field] = getattr(p, field)
        if p.skipped is not None:
            b["skip_known"] = True
            b["skips"] += p.skipped
        b["video"] = b["video"] or p.video
        private += p.private

    records = []
    for (creator_key, title_key, day), b in buckets.items():
        first, last = _iso(b["first"]), _iso(b["last"])
        detail = {"plays": b["plays"],
                  "skipped": b["skips"] if b["skip_known"] else None,
                  "first_play": first, "last_play": last, "album": b["album"],
                  "uri": b["uri"], "kind": b["kind"], "chapter": b["chapter"],
                  "video": True if b["video"] else None}
        records.append({
            "stream": "music_play",
            "external_id": hashlib.sha1(
                f"{creator_key}\n{title_key}\n{day}".encode("utf-8")).hexdigest(),
            "journal_date": day,
            "started_at": first,
            "ended_at": last,
            "utc_offset_sec": None,
            "direction": None,
            "title": b["title"],
            "body": b["creator"],
            "value": round(b["ms"] / 60000, 1),
            "unit": "min",
            "value_json": canonical_json({k: v for k, v in detail.items()
                                          if v is not None}),
            "media_path": None,
            "origin_app": "spotify",
            "last_modified_at": last,
            "participants": [],
        })
    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    return records, private


def scan(target: Path, cfg: Config) -> base.Scan:
    acc = _Acc(cfg)
    content = hashlib.sha256()          # file by file: a decade of history is never all held
    seen_any = False
    alone = target.is_file() and not zipfile.is_zipfile(target)
    try:
        for rel, role, read in _walk(target, acc):
            raw = read()
            content.update(f"{rel}\0{hashlib.sha256(raw).hexdigest()}\n".encode("utf-8"))
            if role == "userdata":
                _read_userdata(rel, raw, acc)
                continue
            seen_any = True
            _read_history(rel, role, raw, alone, acc)
    except PermissionError as e:
        raise DomainError(
            f"cannot read {e.filename or target}: permission denied — on a Mac, allow "
            f"the app running this to use that folder (System Settings > Privacy & "
            f"Security > Files and Folders), then run it again") from e
    except zipfile.BadZipFile as e:
        raise DomainError(f"cannot read {target.name} as a zip: {e}") from e
    if not seen_any:
        listing = ", ".join(acc.unread[:10]) or "(empty)"
        raise DomainError(
            f"{target.name} holds no Spotify listening history (no StreamingHistory_*.json "
            f"or Streaming_History_Audio_*.json) — it has: {listing}")
    if len(acc.usernames) > 1:
        raise DomainError(
            f"{target.name} holds downloads of more than one Spotify account "
            f"({', '.join(sorted(acc.usernames))}) — import each account's on its own")

    plays = _dedupe(acc.plays, acc)
    covered = {p.day for p in plays if p.family == "extended"}
    kept = []
    for p in plays:
        if p.family == "account" and p.day in covered:
            acc.skipped[COVERED] += 1
        elif p.ms < MIN_PLAY_MS:
            acc.skipped[SHORT] += 1
        else:
            kept.append(p)
    records, private = _records(kept)

    minutes: Counter = Counter()
    creators: Counter = Counter()
    for r in records:
        kind = json.loads(r["value_json"]).get("kind") or "unknown"
        minutes[kind] += r["value"]
        if r["body"]:
            creators[r["body"]] += r["value"]
    families: dict[str, dict] = {}
    for f in acc.files:
        fam = families.setdefault(f["family"], {"files": 0, "plays": 0})
        fam["files"] += 1
        fam["plays"] += f["plays"]
    dates = [r["journal_date"] for r in records]
    return base.empty_scan(
        name,
        # Labels the import (imports list); nothing is mirrored, so it scopes nothing.
        account=next(iter(acc.usernames)) if acc.usernames else None,
        source_file=target.name,
        source_hash=base.file_hash(target) if target.is_file() else content.hexdigest(),
        records=records,
        unmapped=[{"what": k, "count": v, "example": acc.examples.get(k)}
                  for k, v in sorted(acc.unmapped.items())],
        skipped=[f"{v} × {k}" for k, v in sorted(acc.skipped.items())],
        facts={"formats": families,
               "files": [f["file"] for f in acc.files],
               "private_session_plays": private,
               "minutes": {k: round(v, 1) for k, v in sorted(minutes.items())},
               "top": [[c, round(m, 1)] for c, m in
                       sorted(creators.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
               "oldest_day": min(dates) if dates else None,
               "newest_day": max(dates) if dates else None},
        unread=acc.unread)


# ---------------------------------------------------------------- report

def _hours(minutes: float) -> str:
    return f"{minutes / 60:.1f} h" if minutes >= 60 else f"{minutes:g} min"


def _sample(rec: dict) -> str:
    plays = json.loads(rec["value_json"]).get("plays", 0)
    who = f"{rec['body']} — " if rec["body"] else ""
    title = f"{who}{rec['title']}"
    if len(title) > 70:
        title = title[:69] + "…"
    return f"{rec['journal_date']} {title}, {plays} play(s), {rec['value']:g} min"


def _cross_format(conflict: dict) -> bool:
    """A track-day the other download already stored: one side has a URI (the
    extended history), the other does not (Account data)."""
    change = conflict["changes"].get("value_json")
    if not change:
        return False
    try:
        old = json.loads(change["existing"] or "{}")
        new = json.loads(change["incoming"] or "{}")
    except ValueError:
        return False
    return ("uri" in old) != ("uri" in new)


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    formats = facts.get("formats") or {}
    parts = [f"{_FAMILY_LABELS[fam]}: {formats[fam]['files']} file(s), "
             f"{formats[fam]['plays']} play(s)" for fam in ("account", "extended")
             if fam in formats]
    head = f"spotify: {scan['source_file']} (" + ("; ".join(parts) or "no plays")
    if scan.get("account"):
        head += f"; account {scan['account']}"
    lines = [head + ")"]
    if facts.get("oldest_day"):
        lines.append(f"  plays span {facts['oldest_day']}..{facts['newest_day']} — "
                     f"Account data reaches back about a year, the extended history "
                     f"to the account's first play")
    if facts.get("minutes"):
        listened = ", ".join(f"{_hours(m)} of {k}" for k, m in facts["minutes"].items())
        top = ", ".join(f"{c} ({_hours(m)})" for c, m in facts.get("top") or [])
        lines.append(f"  listened: {listened}" + (f" | top: {top}" if top else ""))

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    if new or upd:
        recs = new or upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  music_play: {len(new)} new, {len(upd)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
    else:
        lines.append("  nothing new — the download matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate track-day(s)")
    if tail:
        lines.append("  " + ", ".join(tail))

    other = [c for c in plan["conflicts"] if not _cross_format(c)]
    crossed = len(plan["conflicts"]) - len(other)
    if crossed:
        lines.append(f"  kept existing: {crossed} track-day(s) the other Spotify download "
                     f"already stored, which is not older — import both from one folder "
                     f"to let the extended history win (docs/spotify.md)")
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
        lines.append(f"  not understood: {u['count']} × {u['what']}"
                     + (f" (e.g. in {u['example']})" if u.get("example") else ""))
    if facts.get("private_session_plays"):
        lines.append(f"  private session: {facts['private_session_plays']} play(s) were in "
                     f"one — imported like any other (it hides listening from "
                     f"followers, not from you)")
    unread = scan.get("unread") or []
    if unread:
        names = sorted({PurePosixPath(u).name for u in unread})
        lines.append(f"  not read: {len(unread)} other file(s) of the download (library, "
                     f"playlists, searches — not listening history), e.g. "
                     + ", ".join(names[:4]))
    if "extended" in formats:
        lines.append("  never read: the IP address, country, device and user agent of "
                     "each play (docs/spotify.md, Privacy)")
    return lines

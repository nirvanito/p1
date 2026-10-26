"""Last.fm connector: scrobbles into music_play, one record per track per day.

The owner listens on Spotify, and Spotify scrobbles every song to Last.fm by
itself once the two are connected; Last.fm's API then hands the scrobbles
over (Spotify's own API gives only the last 50 plays, and its whole history
is a download requested by hand). `lifecrm/online/lastfm.py` fetches them
each morning into `data/fetched/lastfm/pending/`, one file per API page;
this module reads such a folder, or one page file, and never touches the
network. Records go into the stream Spotify's download fills, built the
same way, so every reader of `music_play` reads both unchanged.

A fetched page
--------------
Each file is one `user.getRecentTracks` response, kept whole under
`response`, in a wrapper saying how it was fetched:

    {"format": "lifecrm-lastfm-page/1", "user": "rj",
     "fetched_at": "2026-10-25T11:30:00Z",
     "window": {"from": <epoch>, "to": <epoch>, "part": "new" | "history",
                "page": 1, "pages": 3},
     "history": {"back_to": "2019-03-02", "done": false, "older": 31200},
     "response": {"recenttracks": {"track": [...], "@attr": {...}}}}

`window` is the promise the fetcher makes: every scrobble Last.fm held in
[from, to) at fetched_at is in the window's pages. `from` is always the
first second of a journal day (`day_start`), so a window holds whole days —
but for the last one when `to` is the moment of the fetch (today, so far).
A scrobble outside its window (the API's pages overlap the edges) is
dropped and counted: the edge day is read whole from the window that
covers it. A bare API response, saved by hand, reads too, with no window.

The same day in several fetches
-------------------------------
Until the owner's yes, and then each morning, pending/ can hold several
fetches of one day — yesterday's in-progress day is fetched again whole
the next morning, and a late scrobble (a phone that was offline) brings
back a day already fetched. Per journal day the newest fetch covering it
is the day: its scrobbles, nothing from the older fetches (counted as
superseded). Within one fetch a scrobble listed twice (a page boundary)
counts once.

One record per track per day
----------------------------
Exactly spotify.py's shape (docs/spotify.md), so dashboards, `ask` and the
review read it as they read Spotify's:

  title       the track
  body        the artist
  value/unit  none — Last.fm records THAT a track was played, never for
              how long; the minutes Spotify's download has are not invented
  value_json  {plays (scrobbles that day), first_play, last_play, album,
              kind: 'music'}
  external_id sha1('lastfm', artist, track, day), case- and space-
              insensitive — never Spotify's id for the same track-day, so a
              Spotify download of the same days is a separate record, and
              neither overwrites the other

A scrobble is stamped with the moment the track STARTED (Last.fm's
track.scrobble), and that dates it, the configured timezone and rollover
applied. first_play and last_play are the day's first and last start —
Spotify's last_play is when the last play ended. Spotify's `skipped`,
`uri`, `video` and `chapter` are absent: Last.fm has none of them (a skip
is never scrobbled at all — a track counts once played half its length or
four minutes). `kind` is 'music': a scrobble is a song to Last.fm, and
Spotify has been reported to scrobble music only, not podcast episodes.

A record's last_modified_at is the fetch it came from (a bare page: its
last play), so a later fetch of a day — longer by the rest of the evening,
or by a late scrobble from earlier in it — is always the newer record, and
an unchanged day only moves that stamp (a refresh).

Remembers: no present_ids. Scrobbles are fetched only from the edge
onward, so a page is never the whole history, and one deleted on Last.fm
stays here. No handles: artists are not people. One listener per archive:
a scan holding two users' pages is refused.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from zoneinfo import ZoneInfo

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "lastfm"
streams = ("music_play",)
# A daily feed: a Spotify that stops scrobbling (its link to Last.fm ended)
# would otherwise read as quiet mornings. Measured over music_play once this
# connector has an import of its own (doctor), so a Spotify download alone
# never trips it.
stale_after_days = 14

FORMAT = "lifecrm-lastfm-page/1"
_ISO = "%Y-%m-%dT%H:%M:%SZ"
NOW_PLAYING = ("now-playing track (not a scrobble yet — Last.fm lists it once "
               "it has played long enough)")
OUTSIDE = ("scrobble outside the window its page was fetched for (an edge day, "
           "read whole from the fetch that covers it)")
SUPERSEDED = "scrobble of a day a newer fetch also covers (the newer fetch is that day)"
TWICE = "scrobble listed twice in one fetch (a page boundary) — counted once"
NAMELESS = "scrobble naming no track"


# ---------------------------------------------------------------- days

def iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(_ISO)


def journal_day(epoch: int, cfg: Config) -> str:
    return base.journal_date_for_iso(iso(epoch), cfg)


def day_start(day: str, cfg: Config) -> int:
    """The first second (UTC epoch) of a journal day: its date at the
    rollover hour, home time. A DST gap or overlap at that hour resolves as
    days.journal_date_for reads it (first occurrence), so a second before
    this is the day before and this second is the day."""
    d = date.fromisoformat(day)
    local = datetime(d.year, d.month, d.day, cfg.day_rollover_hour,
                     tzinfo=ZoneInfo(cfg.timezone))
    return int(local.timestamp())


def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def window_days(start: int, end: int, cfg: Config) -> list[str]:
    """Every journal day [start, end) touches."""
    if end <= start:
        return []
    out, d, last = [], journal_day(start, cfg), journal_day(end - 1, cfg)
    while d <= last:
        out.append(d)
        d = next_day(d)
    return out


# ---------------------------------------------------------------- one page

class Scrobble(NamedTuple):
    uts: int
    artist: str | None
    track: str
    album: str | None

    def key(self) -> tuple:
        return (self.uts, _norm(self.artist), _norm(self.track))


def _text(value) -> str | None:
    if isinstance(value, dict):          # {"#text": ...}, or extended=1's {"name": ...}
        value = value.get("#text") if value.get("#text") is not None else value.get("name")
    if not isinstance(value, str):
        return None
    s = " ".join(value.split())
    return s or None


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n


def read_page(data, where: str) -> dict:
    """One user.getRecentTracks response: {user, page, pages, total,
    scrobbles, now_playing, nameless, undated}. The now-playing track has
    no date and is not a scrobble; it is counted, never kept. Last.fm sends
    a lone track as an object rather than a list of one."""
    rt = data.get("recenttracks") if isinstance(data, dict) else None
    if not isinstance(rt, dict):
        keys = ", ".join(sorted(data)[:6]) if isinstance(data, dict) else type(data).__name__
        raise DomainError(f"{where} is not a Last.fm recent-tracks page (no 'recenttracks'; "
                          f"has: {keys or 'nothing'})")
    attr = rt.get("@attr") if isinstance(rt.get("@attr"), dict) else {}
    items = rt.get("track") or []
    if isinstance(items, dict):
        items = [items]
    out = {"user": _text(attr.get("user")), "page": _int(attr.get("page")),
           "pages": _int(attr.get("totalPages")), "total": _int(attr.get("total")),
           "items": len(items) if isinstance(items, list) else 0,
           "scrobbles": [], "now_playing": 0, "nameless": 0, "undated": 0}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            out["undated"] += 1
            continue
        flags = item.get("@attr") if isinstance(item.get("@attr"), dict) else {}
        if str(flags.get("nowplaying", "")).lower() == "true":
            out["now_playing"] += 1
            continue
        uts = _int((item.get("date") or {}).get("uts")) if isinstance(
            item.get("date"), dict) else None
        if uts is None or uts <= 0:
            out["undated"] += 1
            continue
        track = _text(item.get("name"))
        if not track:
            out["nameless"] += 1
            continue
        out["scrobbles"].append(Scrobble(uts, _text(item.get("artist")), track,
                                         _text(item.get("album"))))
    return out


def page_scrobbles(data) -> list[Scrobble]:
    """The scrobbles of one response (the fetcher's view of a page)."""
    return read_page(data, "Last.fm's answer")["scrobbles"]


# ---------------------------------------------------------------- files

def _junk(rel: str) -> bool:
    return any(p.startswith(".") for p in PurePosixPath(rel).parts)


def _files(target: Path) -> list[tuple[str, Path]]:
    if target.is_dir():
        return [(p.relative_to(target).as_posix(), p) for p in sorted(target.rglob("*.json"))
                if p.is_file() and not _junk(p.relative_to(target).as_posix())]
    if target.suffix.lower() != ".json":
        raise DomainError(f"{target.name} is not a .json page or a folder of them — expected "
                          f"what `python -m lifecrm fetch lastfm` saves in "
                          f"data/fetched/lastfm/pending/ (docs/lastfm.md)")
    return [(target.name, target)]


def _load(raw: bytes, rel: str):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as e:
        raise DomainError(f"{rel} is not valid UTF-8: {e}") from e
    except json.JSONDecodeError as e:
        raise DomainError(f"{rel} is not valid JSON: line {e.lineno} column {e.colno}: "
                          f"{e.msg}") from e


class _Batch:
    """One fetch of one window (or every bare page together): the day it
    can speak for is decided by fetched_at, newest first."""

    def __init__(self, key: tuple, fetched_at: str | None, start: int | None,
                 end: int | None, part: str | None):
        self.key, self.fetched_at = key, fetched_at
        self.start, self.end, self.part = start, end, part
        self.scrobbles: list[Scrobble] = []
        self.pages: set[int] = set()
        self.expected: int | None = None
        self.files = 0

    def rank(self) -> tuple:
        return (self.fetched_at or "", self.end or 0, self.key)


def _window(wrapper: dict) -> tuple[int, int, str | None, int | None, int | None] | None:
    w = wrapper.get("window")
    if not isinstance(w, dict):
        return None
    start, end = _int(w.get("from")), _int(w.get("to"))
    if start is None or end is None or end <= start:
        return None
    return start, end, w.get("part"), _int(w.get("page")), _int(w.get("pages"))


# ---------------------------------------------------------------- scan

def default_target(cfg: Config) -> Path:
    """Last.fm is read where its online service fetches it: with that not
    connected there is nothing to look for (collect skips the source)."""
    raise DomainError("Last.fm is not connected — connect it once: python -m lifecrm "
                      "connect lastfm <username> (docs/lastfm.md); or pass fetched pages: "
                      "python -m lifecrm observe lastfm data/fetched/lastfm/archive")


def scan(target: Path, cfg: Config) -> base.Scan:
    files = _files(target)
    alone = target.is_file()
    content = hashlib.sha256()
    batches: dict[tuple, _Batch] = {}
    users: dict[str, str] = {}
    skipped: Counter = Counter()
    unmapped: Counter = Counter()
    examples: dict[str, str] = {}
    history = None

    def unknown(what: str, rel: str) -> None:
        unmapped[what] += 1
        examples.setdefault(what, rel)

    for rel, path in files:
        raw = path.read_bytes()
        content.update(f"{rel}\0{hashlib.sha256(raw).hexdigest()}\n".encode("utf-8"))
        data = _load(raw, rel)
        wrapped = isinstance(data, dict) and data.get("format") == FORMAT
        try:
            page = read_page(data.get("response") if wrapped else data, rel)
        except DomainError:
            if alone:
                raise
            unknown("JSON file that is not a Last.fm page", rel)
            continue
        user = _text(data.get("user")) if wrapped else None
        user = user or page["user"]
        if user:
            users.setdefault(user.casefold(), user)
        win = _window(data) if wrapped else None
        if wrapped and win is None:
            unknown("fetched page with no readable window (read as a bare page)", rel)
        if win is None:
            key = ("bare",)
            if key not in batches:
                batches[key] = _Batch(key, None, None, None, None)
            batch = batches[key]
        else:
            start, end, part, n, of = win
            fetched = data.get("fetched_at") if isinstance(data.get("fetched_at"), str) else ""
            key = (fetched, start, end)
            if key not in batches:
                batches[key] = _Batch(key, fetched, start, end, part)
            batch = batches[key]
            if n is not None:
                batch.pages.add(n)
            if of is not None:
                batch.expected = of
            if isinstance(data.get("history"), dict) and (
                    history is None or fetched >= history[0]):
                history = (fetched, data["history"])
        batch.files += 1
        skipped[NOW_PLAYING] += page["now_playing"]
        skipped[NAMELESS] += page["nameless"]
        if page["undated"]:
            unmapped["scrobble with no readable date"] += page["undated"]
            examples.setdefault("scrobble with no readable date", rel)
        for s in page["scrobbles"]:
            if batch.start is not None and not batch.start <= s.uts < batch.end:
                skipped[OUTSIDE] += 1
                continue
            batch.scrobbles.append(s)
    if not files:
        raise DomainError(f"{target.name} holds no Last.fm pages (no .json files) — nothing "
                          f"fetched yet, or everything already imported and archived")
    if not any(b.files for b in batches.values()):
        raise DomainError(f"{target.name} holds no Last.fm pages — its .json files are not "
                          f"user.getRecentTracks responses")
    if len(users) > 1:
        raise DomainError(f"{target.name} holds pages of more than one Last.fm user "
                          f"({', '.join(sorted(users.values()))}) — import each on its own")

    # Per day, the newest fetch that covers it speaks for it.
    owner: dict[str, tuple] = {}
    ranks = {k: b.rank() for k, b in batches.items()}
    for key, b in batches.items():
        days = (window_days(b.start, b.end, cfg) if b.start is not None
                else sorted({journal_day(s.uts, cfg) for s in b.scrobbles}))
        for d in days:
            if d not in owner or ranks[key] > ranks[owner[d]]:
                owner[d] = key
    kept: list[tuple[Scrobble, str, _Batch]] = []
    for key, b in sorted(batches.items(), key=lambda kv: kv[1].rank()):
        seen: set[tuple] = set()
        for s in b.scrobbles:
            d = journal_day(s.uts, cfg)
            if owner.get(d) != key:
                skipped[SUPERSEDED] += 1
                continue
            if s.key() in seen:
                skipped[TWICE] += 1
                continue
            seen.add(s.key())
            kept.append((s, d, b))
    incomplete = sorted(f"{b.fetched_at or '?'} ({len(b.pages)} of {b.expected} page(s))"
                        for b in batches.values()
                        if b.expected and b.pages != set(range(1, b.expected + 1)))
    records = _records(kept)

    plays: Counter = Counter()
    for r in records:
        if r["body"]:
            plays[r["body"]] += json.loads(r["value_json"])["plays"]
    dates = [r["journal_date"] for r in records]
    parts = Counter(b.part or "bare" for b in batches.values() for _ in range(b.files))
    user = next(iter(users.values())) if users else None
    return base.empty_scan(
        name,
        account=user,
        source_file=target.name,
        source_hash=base.file_hash(target) if target.is_file() else content.hexdigest(),
        records=records,
        unmapped=[{"what": k, "count": v, "example": examples.get(k)}
                  for k, v in sorted(unmapped.items()) if v],
        skipped=[f"{v} × {k}" for k, v in sorted(skipped.items()) if v],
        facts={"pages": dict(sorted(parts.items())), "scrobbles": len(kept),
               "history": history[1] if history else None,
               "incomplete": incomplete,
               "top": [[a, n] for a, n in sorted(plays.items(),
                                                 key=lambda kv: (-kv[1], kv[0]))[:5]],
               "oldest_day": min(dates) if dates else None,
               "newest_day": max(dates) if dates else None})


def _records(kept: list[tuple[Scrobble, str, _Batch]]) -> list[dict]:
    buckets: dict[tuple, dict] = {}
    for s, day, batch in sorted(kept, key=lambda t: (t[0].uts, _norm(t[0].artist),
                                                     _norm(t[0].track))):
        key = (_norm(s.artist), _norm(s.track), day)
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {"plays": 0, "first": s.uts, "last": s.uts, "album": None,
                                "fetched": batch.fetched_at}
        b["plays"] += 1
        b["first"], b["last"] = min(b["first"], s.uts), max(b["last"], s.uts)
        # Scrobbles come oldest first: the latest spelling and album win.
        b["artist"], b["track"] = s.artist, s.track
        b["album"] = s.album or b["album"]
    records = []
    for (artist_key, track_key, day), b in buckets.items():
        first, last = iso(b["first"]), iso(b["last"])
        detail = {"plays": b["plays"], "first_play": first, "last_play": last,
                  "album": b["album"], "kind": "music"}
        records.append({
            "stream": "music_play",
            "external_id": hashlib.sha1(
                f"lastfm\n{artist_key}\n{track_key}\n{day}".encode("utf-8")).hexdigest(),
            "journal_date": day,
            "started_at": first,
            "ended_at": last,
            "utc_offset_sec": None,
            "direction": None,
            "title": b["track"],
            "body": b["artist"],
            "value": None,
            "unit": None,
            "value_json": canonical_json({k: v for k, v in detail.items() if v is not None}),
            "media_path": None,
            "origin_app": "lastfm",
            "last_modified_at": b["fetched"] or last,
            "participants": [],
        })
    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    return records


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    plays = json.loads(rec["value_json"]).get("plays", 0)
    title = f"{rec['body']} — {rec['title']}" if rec["body"] else rec["title"]
    if len(title) > 70:
        title = title[:69] + "…"
    return f"{rec['journal_date']} {title}, {plays} play(s)"


def _history_line(h: dict | None) -> str | None:
    if not isinstance(h, dict):
        return None
    if h.get("done"):
        back = f" — back to the first scrobble, {h['back_to']}" if h.get("back_to") else ""
        return f"  history: complete{back}"
    line = f"  history: fetched back to {h.get('back_to') or 'the last few days'}"
    if isinstance(h.get("older"), int):
        line += (f"; {h['older']:,} older scrobble(s) still to come, newest first, a "
                 f"batch each morning (docs/lastfm.md, Backfill)")
    if h.get("error"):
        line += f" | this morning's batch stopped: {h['error']}"
    return line


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    pages = facts.get("pages") or {}
    parts = ", ".join(f"{n} {p}" for p, n in pages.items())
    head = f"lastfm: {scan['source_file']} ({sum(pages.values())} page(s): {parts}"
    if scan.get("account"):
        head += f"; user {scan['account']}"
    lines = [head + ")"]
    if facts.get("oldest_day"):
        top = ", ".join(f"{a} ({n})" for a, n in facts.get("top") or [])
        lines.append(f"  {facts['scrobbles']:,} scrobble(s), {facts['oldest_day']}.."
                     f"{facts['newest_day']}" + (f" | top: {top}" if top else ""))
    hist = _history_line(facts.get("history"))
    if hist:
        lines.append(hist)
    for w in facts.get("incomplete") or []:
        lines.append(f"  incomplete fetch: {w} — its edge days may be short; read the "
                     f"whole folder it came from")

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    if new or upd:
        recs = new or upd
        span = (f"{min(r['journal_date'] for r in recs)}.."
                f"{max(r['journal_date'] for r in recs)}")
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  music_play: {len(new)} new, {len(upd)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
    else:
        lines.append("  nothing new — the pages match the database")
    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} fetched again unchanged (refreshed)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate track-day(s)")
    if tail:
        lines.append("  " + ", ".join(tail))
    for c in plan["conflicts"][:5]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:12]}…: differs from the "
                     f"stored record but is from an older fetch — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    if len(plan["conflicts"]) > 5:
        lines.append(f"  … and {len(plan['conflicts']) - 5} more conflict(s)")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    for u in scan["unmapped"]:
        lines.append(f"  not understood: {u['count']} × {u['what']}"
                     + (f" (e.g. in {u['example']})" if u.get("example") else ""))
    if scan["records"]:
        lines.append("  plays, not minutes: Last.fm records that a track was played, not "
                     "for how long (docs/lastfm.md)")
    return lines

"""Last.fm: the online service (lifecrm/online/lastfm.py) fetching scrobbles
each morning, and the connector (lifecrm/connectors/lastfm.py) reading the
pages into music_play — one record per track per journal day, as Spotify's
download is read.

What is easy to get wrong, and is checked here: the API key never shown;
the now-playing track never a scrobble; a morning's pages whole or not at
all; the backfill cut at a day boundary, so no day is ever imported short;
a day fetched twice (yesterday's evening, a late scrobble) taken from the
newer fetch; a quiet morning saving nothing; and the collect flow — held
for the owner's yes, then every morning by itself. The network is never
touched: a fake Last.fm answers every URL.
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import collect, online
from lifecrm.cli import app
from lifecrm.connectors import base, lastfm, spotify
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError
from lifecrm.online import lastfm as service
from lifecrm.online import secrets
from test_collect import home  # noqa: F401  (the autouse home isolation)

runner = CliRunner()
KEY = "0123456789abcdef0123456789abcdef"
TZ = ZoneInfo("America/Chicago")          # the cfg fixture's; rollover at 05:00
CONFIRMED = ("entries", "measurements", "mentions", "entity_aliases", "entity_links")
# spotify.py's value_json keys (docs/spotify.md): a Last.fm record uses only these.
SPOTIFY_KEYS = {"plays", "skipped", "first_play", "last_play", "album", "uri", "kind",
                "chapter", "video"}


def at(day: str, hh: int, mm: int = 0) -> int:
    y, m, d = (int(x) for x in day.split("-"))
    return int(datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp())


NOW = at("2026-10-25", 12)                # journal day 2026-10-25, mid-day


# ---------------------------------------------------------------- a fake Last.fm

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _track(s, now_playing=False):
    uts, artist, track, album = (*s, None)[:4]
    item = {"artist": {"mbid": "", "#text": artist}, "streamable": "0",
            "image": [{"size": "small", "#text": "https://lastfm.freetls.fastly.net/i/x.png"}],
            "mbid": "", "album": {"mbid": "", "#text": album or ""}, "name": track,
            "url": f"https://www.last.fm/music/{artist}/_/{track}"}
    if now_playing:
        item["@attr"] = {"nowplaying": "true"}
    else:
        item["date"] = {"uts": str(uts), "#text": "25 Oct 2026, 17:00"}
    return item


def page(scrobbles, user="RJ", page_no=1, pages=1, total=None, now_playing=None,
         single=False):
    """A user.getRecentTracks response as Last.fm's JSON spells it."""
    items = ([_track((0, *now_playing, None), True)] if now_playing else []) + \
        [_track(s) for s in scrobbles]
    return {"recenttracks": {
        "track": items[0] if single and len(items) == 1 else items,
        "@attr": {"user": user, "totalPages": str(pages), "page": str(page_no),
                  "perPage": "200", "total": str(len(scrobbles) if total is None else total)}}}


def wrapped(resp, fetched_at, start, end, part="new", n=1, of=1, history=None):
    return {"format": lastfm.FORMAT, "service": "lastfm", "user": "RJ",
            "fetched_at": fetched_at, "window": {"from": start, "to": end, "part": part,
                                                 "page": n, "pages": of},
            "history": history or {"done": True}, "response": resp}


class FakeLastfm:
    """Answers user.getInfo and user.getRecentTracks the way the API page
    describes them: newest first, from/to exclusive, the now-playing track
    on top of page 1, errors as {"error", "message"} with an HTTP status."""

    def __init__(self):
        self.scrobbles: list[tuple] = []          # (uts, artist, track, album)
        self.now_playing = None
        self.hidden = False
        self.fail: list = []                      # answers served before the real ones
        self.asked: list[dict] = []

    def add(self, uts, artist, track, album=None):
        self.scrobbles.append((uts, artist, track, album))

    def _error(self, url, status, code, message):
        body = json.dumps({"error": code, "message": message, "links": []}).encode()
        raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(body))

    def __call__(self, url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.asked.append(q)
        if self.fail:
            f = self.fail.pop(0)
            if isinstance(f, Exception):
                raise f
            status, body = f
            if status >= 400:
                raise urllib.error.HTTPError(url, status, "error", {}, io.BytesIO(body))
            return _Resp(body)
        if q.get("api_key") != KEY:
            self._error(url, 403, 10, "Invalid API key - You must be granted a valid key "
                                      "by last.fm")
        if q.get("user", "").casefold() != "rj":
            self._error(url, 404, 6, "User not found")
        if q["method"] == "user.getInfo":
            return _Resp(json.dumps({"user": {
                "name": "RJ", "playcount": str(len(self.scrobbles)),
                "registered": {"unixtime": "1200000000", "#text": 1200000000}}}).encode())
        if self.hidden:
            self._error(url, 403, 17, "Login: User requires to be logged in")
        lo = int(q["from"]) if "from" in q else None
        hi = int(q["to"]) if "to" in q else None
        rows = sorted((s for s in self.scrobbles
                       if (lo is None or s[0] > lo) and (hi is None or s[0] < hi)),
                      key=lambda s: -s[0])
        limit, n = int(q.get("limit", 50)), int(q.get("page", 1))
        pages = (len(rows) + limit - 1) // limit
        chunk = rows[(n - 1) * limit:n * limit]
        resp = page(chunk, page_no=n, pages=pages, total=len(rows),
                    now_playing=self.now_playing if n == 1 else None)
        return _Resp(json.dumps(resp).encode())


@pytest.fixture
def fake(monkeypatch):
    server = FakeLastfm()
    clock = {"now": NOW}
    pauses: list[float] = []
    monkeypatch.setattr(service, "_open", server)
    monkeypatch.setattr(service, "_now", lambda: clock["now"])
    monkeypatch.setattr(service, "_pause", pauses.append)
    server.clock, server.pauses = clock, pauses
    return server


def _key_asked(prompt, hide):
    assert hide, "the API key is always asked for hidden"
    return KEY


def _connect(cfg, fake):
    return online.connect(cfg, "lastfm", ["rj"], _key_asked)


def _pending(cfg):
    return online.dest_dir(cfg, "lastfm") / "pending"


def _expected(scrobbles, cfg) -> Counter:
    """Ground truth: plays per Last.fm track-day id."""
    out: Counter = Counter()
    for uts, artist, track, _ in scrobbles:
        day = lastfm.journal_day(uts, cfg)
        out[hashlib.sha1(f"lastfm\n{artist.casefold()}\n{track.casefold()}\n{day}"
                         .encode()).hexdigest()] += 1
    return out


def _plays(scan) -> Counter:
    return Counter({r["external_id"]: json.loads(r["value_json"])["plays"]
                    for r in scan["records"]})


# ---------------------------------------------------------------- registries

def test_both_registries_find_it_and_collect_can_take_it():
    assert base.registry()["lastfm"] is lastfm
    assert online.registry()["lastfm"] is service
    assert service.CONNECTOR == "lastfm" and "lastfm" in online.feeds()
    assert "lastfm" in collect.collectable()


# ---------------------------------------------------------------- connect

def test_connect_checks_the_user_and_the_key_and_never_shows_the_key(cfg, fake):
    def asked(value):
        return lambda prompt, hide: value

    with pytest.raises(DomainError, match="refused the API key") as e:
        online.connect(cfg, "lastfm", ["rj"], asked("f" * 32))
    assert "f" * 32 not in str(e.value) and "connect lastfm again" in str(e.value)
    with pytest.raises(DomainError, match="no such user"):
        online.connect(cfg, "lastfm", ["nobody"], _key_asked)
    with pytest.raises(DomainError, match="does not look like a Last.fm API key"):
        online.connect(cfg, "lastfm", ["rj"], asked("https://www.last.fm/api/accounts"))
    with pytest.raises(DomainError, match="only the username"):
        online.connect(cfg, "lastfm", ["rj", KEY], _key_asked)
    fake.hidden = True
    with pytest.raises(DomainError, match="Hide recent listening information"):
        _connect(cfg, fake)
    assert secrets.load(cfg) == {}                     # nothing kept on a failure

    fake.hidden = False
    asks = []
    result = online.connect(cfg, "lastfm", [], lambda p, hide: asks.append(hide) or (
        KEY if hide else "https://www.last.fm/user/rj"))
    assert asks == [False, True] and result["connector"] == "lastfm"
    assert secrets.load(cfg)["lastfm"] == {"user": "RJ", "api_key": KEY}   # as Last.fm spells it
    assert KEY not in json.dumps(result)
    assert all(q["api_key"] == KEY for q in fake.asked[-2:])      # checked, both calls


def test_reconnecting_the_same_listener_keeps_how_far_it_got(cfg, fake):
    _connect(cfg, fake)
    secrets.update(cfg, "lastfm", cursor={"oldest": 5, "history_done": True})
    _connect(cfg, fake)
    assert secrets.load(cfg)["lastfm"]["cursor"] == {"oldest": 5, "history_done": True}


# ---------------------------------------------------------------- fetch

def test_pages_are_fetched_whole_and_now_playing_is_never_a_scrobble(cfg, fake,
                                                                     monkeypatch):
    monkeypatch.setattr(service, "PER_PAGE", 2)
    for i, track in enumerate(["Kyoto", "Motion Sickness", "Kyoto", "Garden Song",
                               "Halloween"]):
        fake.add(at("2026-10-24", 9 + i), "Phoebe Bridgers", track, "Punisher")
    fake.now_playing = ("Phoebe Bridgers", "Moon Song")
    _connect(cfg, fake)
    target = online.fetch(cfg, online.get("lastfm"))
    assert target == _pending(cfg)
    new = sorted(target.glob("*-new-*.json"))
    assert len(new) == 3                               # 5 scrobbles, 2 a page
    first = json.loads(new[0].read_text(encoding="utf-8"))
    assert first["window"]["to"] == NOW and first["window"]["pages"] == 3
    assert first["response"]["recenttracks"]["track"][0]["@attr"] == {"nowplaying": "true"}
    assert KEY not in "".join(p.read_text(encoding="utf-8") for p in target.iterdir())
    assert fake.pauses and all(p == service.PAUSE for p in fake.pauses)

    scan = lastfm.scan(target, cfg)
    assert {r["title"]: json.loads(r["value_json"])["plays"] for r in scan["records"]} == {
        "Kyoto": 2, "Motion Sickness": 1, "Garden Song": 1, "Halloween": 1}
    assert any("now-playing" in s for s in scan["skipped"])
    assert "Moon Song" not in json.dumps(scan["records"])


def test_a_quiet_morning_saves_nothing_and_says_so(cfg, fake):
    fake.add(at("2026-10-24", 21), "Big Thief", "Not")
    _connect(cfg, fake)
    target = online.fetch(cfg, online.get("lastfm"))
    files = sorted(p.name for p in target.iterdir())
    assert online.fetch(cfg, online.get("lastfm")) == target      # pending stays
    assert sorted(p.name for p in target.iterdir()) == files      # nothing added
    service.settle(cfg, {}, target)
    assert not any(target.iterdir())
    archived = online.dest_dir(cfg, "lastfm") / "archive"
    assert sorted(p.name for p in archived.iterdir()) == files    # kept, never deleted
    fake.clock["now"] = NOW + 3600
    assert online.fetch(cfg, online.get("lastfm")) is None        # nothing new, nothing pending


def test_the_history_arrives_newest_first_in_whole_days(cfg, fake, monkeypatch):
    """Twenty days of history, eight scrobbles a morning: each morning's batch
    ends at a day boundary, and once it is complete every track-day holds
    exactly its plays — none short, none twice — although yesterday's
    evening and each new day were fetched more than once."""
    monkeypatch.setattr(service, "PER_PAGE", 4)
    monkeypatch.setattr(service, "HISTORY_PAGES", 2)
    for d in range(1, 21):
        day = f"2026-10-{d:02d}"
        for h, track in ((9, "Bones"), (13, "Masterpiece"), (22, "Bones")):
            fake.add(at(day, h), "Big Thief", track, "U.F.O.F.")
        fake.add(at(day, 2), "Big Thief", "Paul")          # 02:00: the evening before
    _connect(cfg, fake)
    oldest = []
    for morning in range(30):
        fake.clock["now"] = NOW + morning * 86400
        fake.add(fake.clock["now"] - 1800, "Adrianne Lenker", "anything")
        online.fetch(cfg, online.get("lastfm"))
        cursor = secrets.load(cfg)["lastfm"]["cursor"]
        oldest.append(cursor["oldest"])
        assert lastfm.day_start(lastfm.journal_day(cursor["oldest"], cfg), cfg) \
            == cursor["oldest"]                            # always a day's first second
        if cursor["history_done"]:
            break
    assert cursor["history_done"] and len(oldest) > 3
    assert oldest == sorted(oldest, reverse=True)          # newest first, never forward
    scan = lastfm.scan(_pending(cfg), cfg)
    assert _plays(scan) == _expected(fake.scrobbles, cfg)
    assert scan["facts"]["history"]["done"]
    asked_to = [int(q["to"]) for q in fake.asked if "to" in q and "from" not in q]
    assert asked_to == sorted(asked_to, reverse=True)


def test_a_day_longer_than_the_batch_is_read_to_its_start(cfg, fake, monkeypatch):
    monkeypatch.setattr(service, "PER_PAGE", 2)
    monkeypatch.setattr(service, "HISTORY_PAGES", 1)
    for i in range(9):
        fake.add(at("2026-10-17", 8, i), "Mitski", f"Track {i}")   # one long day
    fake.add(at("2026-10-16", 8), "Mitski", "Older")
    fake.add(at("2026-10-15", 8), "Mitski", "Oldest")
    _connect(cfg, fake)
    online.fetch(cfg, online.get("lastfm"))
    cursor = secrets.load(cfg)["lastfm"]["cursor"]
    assert cursor["oldest"] == lastfm.day_start("2026-10-17", cfg)
    assert not cursor["history_done"] and cursor["history_older"] == 2
    scan = lastfm.scan(_pending(cfg), cfg)
    assert sum(_plays(scan).values()) == 9 and "Older" not in json.dumps(scan["records"])
    assert any("outside the window" in s for s in scan["skipped"])  # the 16th's, cut
    assert "2 older scrobble(s) still to come" in "\n".join(
        lastfm.report_lines(scan, _plan_of(scan)))


def test_a_late_scrobble_brings_its_day_back_whole(conn, cfg, fake):
    fake.add(at("2026-10-22", 20), "Wednesday", "Bath County")
    _connect(cfg, fake)
    target = online.fetch(cfg, online.get("lastfm"))
    first = lastfm.scan(target, cfg)
    collect.apply_observed(conn, cfg, first, obs.build_plan(conn, first))
    conn.commit()
    service.settle(cfg, {}, target)

    fake.add(at("2026-10-22", 19), "Wednesday", "Bath County")    # scrobbled late
    fake.clock["now"] = NOW + 86400
    target = online.fetch(cfg, online.get("lastfm"))
    names = [p.name for p in target.iterdir()]
    assert names and all("-new-" in n for n in names)
    again = lastfm.scan(target, cfg)
    plan = obs.build_plan(conn, again)
    (row, rec, changes), = plan["updates"]
    assert json.loads(rec["value_json"])["plays"] == 2 and "value_json" in changes
    assert rec["started_at"] == lastfm.iso(at("2026-10-22", 19))


def test_a_failing_page_is_retried_once_then_said_in_one_line(cfg, fake):
    fake.add(at("2026-10-24", 9), "Lorde", "Ribs")
    _connect(cfg, fake)
    fake.fail = [(500, b"<html>oops</html>")]
    assert online.fetch(cfg, online.get("lastfm")) is not None
    assert service.RETRY_PAUSE in fake.pauses

    for answer, said in (
            ([(429, b'{"error": 29, "message": "Rate limit exceeded"}')] * 2,
             "too many requests"),
            ([OSError("timed out")] * 2, "could not reach Last.fm"),
            ([(503, b"")] * 2, "HTTP 503"),
            ([(200, b"not json")] * 2, "not JSON")):
        fake.fail = list(answer)
        before = len(fake.asked)
        with pytest.raises(DomainError, match=said) as e:
            online.fetch(cfg, online.get("lastfm"))
        assert len(fake.asked) - before == 2               # one try, one retry
        assert KEY not in str(e.value) and "\n" not in str(e.value)

    fake.fail = [(403, b'{"error": 10, "message": "Invalid API key"}')]
    before = len(fake.asked)
    with pytest.raises(DomainError, match="connect lastfm again"):
        online.fetch(cfg, online.get("lastfm"))
    assert len(fake.asked) - before == 1                   # never retried


def test_a_failed_history_batch_keeps_its_whole_days_and_says_why(cfg, fake, monkeypatch):
    monkeypatch.setattr(service, "PER_PAGE", 2)
    for d in (10, 11, 12, 13):
        fake.add(at(f"2026-10-{d}", 9), "Bon Iver", "Holocene")
        fake.add(at(f"2026-10-{d}", 10), "Bon Iver", "Towers")
    fake.add(at("2026-10-24", 9), "Bon Iver", "Re: Stacks")
    _connect(cfg, fake)
    real = fake.__call__

    def flaky(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "to" in q and "from" not in q and q.get("page") == "3":
            fake.asked.append(q)
            raise urllib.error.HTTPError(url, 500, "error", {}, io.BytesIO(b""))
        return real(url)
    monkeypatch.setattr(service, "_open", flaky)
    target = online.fetch(cfg, online.get("lastfm"))       # new part saved: no raise
    cursor = secrets.load(cfg)["lastfm"]["cursor"]
    # the 12th was reached, but nothing older showed before the failure, so it
    # may be short: it is fetched whole the next morning
    assert cursor["oldest"] == lastfm.day_start("2026-10-13", cfg)
    assert "HTTP 500" in cursor["history_error"]
    scan = lastfm.scan(target, cfg)
    days = sorted({r["journal_date"] for r in scan["records"]})
    assert days == ["2026-10-13", "2026-10-24"]
    lines = "\n".join(lastfm.report_lines(scan, _plan_of(scan)))
    assert "this morning's batch stopped" in lines

    monkeypatch.setattr(service, "_open", real)
    fake.clock["now"] = NOW + 3600
    online.fetch(cfg, online.get("lastfm"))                 # carries on, says no error
    cursor = secrets.load(cfg)["lastfm"]["cursor"]
    assert cursor["history_done"] and cursor["history_error"] is None
    assert _plays(lastfm.scan(target, cfg)) == _expected(fake.scrobbles, cfg)


def _plan_of(scan) -> dict:
    return {"new": scan["records"], "updates": [], "refreshed": [], "conflicts": [],
            "identical": 0, "dup_incoming": 0, "invalid": []}


# ---------------------------------------------------------------- the connector

def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_one_record_per_track_per_day_dated_by_its_start_with_the_rollover(tmp_path, cfg):
    rows = [(at("2026-10-23", 22), "Japanese Breakfast", "Be Sweet", "Jubilee"),
            (at("2026-10-24", 4, 30), "Japanese Breakfast", "Be Sweet", "Jubilee"),
            (at("2026-10-24", 5, 10), "Japanese Breakfast", "Be Sweet", "Jubilee"),
            (at("2026-10-24", 6), "japanese  breakfast", "be sweet", None)]
    scan = lastfm.scan(_write(tmp_path / "p.json", page(rows)), cfg)
    by_day = {r["journal_date"]: r for r in scan["records"]}
    assert set(by_day) == {"2026-10-23", "2026-10-24"}       # 04:30 is the evening before
    late, today = by_day["2026-10-23"], by_day["2026-10-24"]
    assert json.loads(late["value_json"]) == {
        "plays": 2, "first_play": lastfm.iso(rows[0][0]), "last_play": lastfm.iso(rows[1][0]),
        "album": "Jubilee", "kind": "music"}
    assert json.loads(today["value_json"])["plays"] == 2     # case and spaces are one track
    assert (today["title"], today["body"]) == ("be sweet", "japanese breakfast")  # latest
    for r in scan["records"]:
        assert r["value"] is None and r["unit"] is None    # never invented minutes
        assert r["stream"] == "music_play" and r["participants"] == []
        assert r["origin_app"] == "lastfm" and obs.validate(r) is None
    assert scan["handles"] == [] and "present_ids" not in scan
    assert scan["account"] == "RJ"


def test_a_spotify_download_of_the_same_listening_is_a_separate_record(tmp_path, cfg, conn):
    """The two sources never share an id: neither overwrites the other, and
    the same track-day is two rows — see docs/lastfm.md for who counts it."""
    played = at("2026-10-24", 9)
    lf_scan = lastfm.scan(_write(tmp_path / "p.json", page(
        [(played, "Tycho", "Awake", "Awake")])), cfg)
    sp = _write(tmp_path / "sp" / "StreamingHistory_music_0.json", [{
        "endTime": datetime.fromtimestamp(played + 240, timezone.utc).strftime(
            "%Y-%m-%d %H:%M"),
        "artistName": "Tycho", "trackName": "Awake", "msPlayed": 240000}])
    sp_scan = spotify.scan(sp.parent, cfg)
    (a,), (b,) = lf_scan["records"], sp_scan["records"]
    assert a["journal_date"] == b["journal_date"] and a["external_id"] != b["external_id"]
    assert set(json.loads(a["value_json"])) <= SPOTIFY_KEYS          # same keys, same meaning
    for s in (lf_scan, sp_scan):
        collect.apply_observed(conn, cfg, s, obs.build_plan(conn, s))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE stream='music_play'"
                        ).fetchone()[0] == 2


def test_the_newer_fetch_of_a_day_wins_and_window_edges_are_clipped(tmp_path, cfg):
    d23, d24 = lastfm.day_start("2026-10-23", cfg), lastfm.day_start("2026-10-24", cfg)
    morning1 = page([(at("2026-10-24", 8), "Lomelda", "Hannah"),
                     (at("2026-10-23", 21), "Lomelda", "Hannah"),
                     (at("2026-10-22", 23), "Lomelda", "Edge")])        # before its window
    morning2 = page([(at("2026-10-24", 20), "Lomelda", "Hannah"),
                     (at("2026-10-24", 8), "Lomelda", "Hannah")])
    _write(tmp_path / "f" / "a-new-p0001.json",
           wrapped(morning1, "2026-10-24T14:00:00Z", d23, at("2026-10-24", 9)))
    _write(tmp_path / "f" / "b-new-p0001.json",
           wrapped(morning2, "2026-10-25T14:00:00Z", d24, at("2026-10-25", 9)))
    scan = lastfm.scan(tmp_path / "f", cfg)
    by_day = {r["journal_date"]: r for r in scan["records"]}
    assert sorted(by_day) == ["2026-10-23", "2026-10-24"]
    assert json.loads(by_day["2026-10-24"]["value_json"])["plays"] == 2
    assert by_day["2026-10-24"]["last_modified_at"] == "2026-10-25T14:00:00Z"
    assert by_day["2026-10-23"]["last_modified_at"] == "2026-10-24T14:00:00Z"
    skipped = " | ".join(scan["skipped"])
    assert "1 × scrobble of a day a newer fetch also covers" in skipped
    assert "1 × scrobble outside the window" in skipped


def test_pages_it_cannot_read_are_said_never_hidden(tmp_path, cfg):
    lone = page([(at("2026-10-24", 9), "Clairo", "Bags")], single=True)
    assert isinstance(lone["recenttracks"]["track"], dict)  # Last.fm's lone track
    assert len(lastfm.scan(_write(tmp_path / "lone.json", lone), cfg)["records"]) == 1
    with pytest.raises(DomainError, match="not a Last.fm recent-tracks page"):
        lastfm.scan(_write(tmp_path / "x.json", {"toptracks": {}}), cfg)
    with pytest.raises(DomainError, match="not valid JSON"):
        (tmp_path / "bad.json").write_text("{", encoding="utf-8")
        lastfm.scan(tmp_path / "bad.json", cfg)
    with pytest.raises(DomainError, match="not a .json page"):
        lastfm.scan(_write(tmp_path / "p.txt", {}), cfg)
    _write(tmp_path / "two" / "a.json", page([(at("2026-10-24", 9), "A", "B")]))
    _write(tmp_path / "two" / "b.json", page([(at("2026-10-24", 9), "A", "B")], user="kim"))
    with pytest.raises(DomainError, match="more than one Last.fm user"):
        lastfm.scan(tmp_path / "two", cfg)
    with pytest.raises(DomainError, match="not connected"):
        lastfm.default_target(cfg)


def test_scan_is_deterministic(tmp_path, cfg):
    p = _write(tmp_path / "p.json", page([(at("2026-10-24", h), "Men I Trust", "Show Me How")
                                          for h in (8, 9, 10)]))
    assert lastfm.scan(p, cfg) == lastfm.scan(p, cfg)


# ---------------------------------------------------------------- collect

def _only(result):
    return next(r for r in result["sources"] if r["source"] == "lastfm")


def test_collect_asks_then_imports_then_collects_every_morning(conn, cfg, fake):
    fake.add(at("2026-10-24", 21), "Fleet Foxes", "Mykonos", "Sun Giant")
    fake.add(at("2026-10-12", 9), "Fleet Foxes", "Helplessness Blues")    # history
    _connect(cfg, fake)
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["hold"]) == ("held", "first_import"), r
    assert "RJ" in r["reason"] and KEY not in json.dumps(r)
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0

    done = collect.approve(conn, cfg, "lastfm", apply=True)
    assert done["applied"] and done["applied_counts"]["added"] == 2
    assert not any(_pending(cfg).iterdir())                # settled into archive/
    for table in CONFIRMED:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    fake.add(NOW + 1800, "Fleet Foxes", "Mykonos", "Sun Giant")
    fake.add(NOW + 2400, "Fleet Foxes", "Ragged Wood", "Fleet Foxes")
    fake.clock["now"] = NOW + 3600
    r = _only(collect.run(conn, cfg))
    assert r["outcome"] == "applied" and r["counts"]["added"] == 2, r
    assert not any(_pending(cfg).iterdir())

    fake.clock["now"] = NOW + 7200
    r = _only(collect.run(conn, cfg))
    assert (r["outcome"], r["target"]) == ("unchanged", None), r


def test_a_bad_key_in_the_morning_is_an_error_never_a_skip(conn, cfg, fake):
    _connect(cfg, fake)
    secrets.update(cfg, "lastfm", api_key="f" * 32)
    r = _only(collect.run(conn, cfg))
    assert r["outcome"] == "error" and "connect lastfm again" in r["reason"]
    assert "f" * 32 not in r["reason"]


def test_not_connected_it_is_skipped_or_said(conn, cfg):
    r = _only(collect.run(conn, cfg))
    assert r["outcome"] == "skipped"
    r = _only(collect.run(conn, cfg, ["lastfm"]))
    assert r["outcome"] == "missing" and "connect lastfm" in r["reason"]


# ---------------------------------------------------------------- the CLI

def test_cli_observe_a_page_file_is_a_dry_run(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    p = _write(tmp_path / "pages" / "page.json", page(
        [(at("2026-10-24", h), "Phoebe Bridgers", "Kyoto", "Punisher") for h in (9, 10)]))
    r = runner.invoke(app, ["observe", "lastfm", str(p)])
    assert r.exit_code == 0, r.output
    assert "music_play: 1 new, 0 changed" in r.output and "2 play(s)" in r.output
    assert "plays, not minutes" in r.output and "dry run" in r.output
    r = runner.invoke(app, ["observe", "lastfm"])
    assert r.exit_code != 0 and "not connected" in r.output


def test_cli_connect_fetch_observe_disconnect(tmp_path, monkeypatch, fake):
    fake.add(at("2026-10-24", 9), "Phoebe Bridgers", "Kyoto", "Punisher")
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["connect"])
    assert "lastfm" in r.output and "Spotify Scrobbling" in r.output
    r = runner.invoke(app, ["connect", "lastfm", "rj"], input=KEY + "\n")
    assert r.exit_code == 0 and "connected: Last.fm" in r.output, r.output
    assert KEY not in r.output
    r = runner.invoke(app, ["fetch", "lastfm"])
    assert r.exit_code == 0 and "nothing imported" in r.output, r.output
    r = runner.invoke(app, ["observe", "lastfm"])             # no path: today's pages
    assert r.exit_code == 0 and "Kyoto" in r.output and KEY not in r.output, r.output
    r = runner.invoke(app, ["disconnect", "lastfm"])
    assert r.exit_code == 0 and "disconnected" in r.output

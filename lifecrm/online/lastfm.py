"""Last.fm — the owner's listening, fetched each morning (docs/lastfm.md).

Spotify's own API returns only the last 50 plays, and its whole history is
a download requested by hand and ready days later. So the automatic route
is Spotify scrobbling to Last.fm — connected once on last.fm, then every
song played on any device is sent there — and this module asking Last.fm's
API for the new scrobbles each morning. The `lastfm` connector reads what
it saves (lifecrm/connectors/lastfm.py), and collect takes it from there:
the first import waits for the owner's yes.

What is kept in data/secrets.json: the username (as Last.fm spells it),
the API key (the owner makes one, free, at last.fm/api/account/create), and
the cursor — how far the fetches got. Reading a user's scrobbles needs only
the key: no password, no sign-in, nothing that can write to the account.
The backfill ends where Last.fm's pages do, not at the registration date:
scrobbles imported into Last.fm can be older than the account.

What a morning fetches, from `user.getRecentTracks` (200 a page):

  new      everything from the start of the last fetch's in-progress day
           (or LOOKBACK_DAYS back, if that is earlier: a phone that was
           offline scrobbles late, with the time it played) up to now.
           Saved only when a day in it differs from what was fetched
           before — per-day digests in the cursor — and then from the
           first day that changed: a quiet morning saves nothing and
           fetch returns None (collect: unchanged).
  history  the backfill, newest first: HISTORY_PAGES older pages a
           morning (10,000 scrobbles) until the account's first
           scrobble, so a 100,000-scrobble history arrives over ten
           mornings and no morning's job runs for an hour. Cut at a day
           boundary: the oldest day a batch only reached into is fetched
           whole the next morning.

Each page is saved as fetched (`response`), in a wrapper naming its
window — the whole days it promises to hold — and when it was fetched; the
connector's docstring has the format. Pages wait in pending/ until a
committed import has read them, then `settle` moves them to archive/,
kept, never deleted. A morning is all or nothing for its new part: a page
that fails (after one retry, for a 5xx or Last.fm's "try again" errors)
saves nothing new and is said in one line. The history part keeps the
whole days it got before a failure and says the failure in the report;
the next morning carries on from there.

Polite to the API: one request at a time, a pause between pages, an
identifying User-Agent (Last.fm asks for one), and a longer pause before
the one retry of a rate-limit answer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from ..config import Config
from ..connectors import lastfm as lf
from ..domain.util import DomainError
from . import secrets

SERVICE = "lastfm"
CONNECTOR = "lastfm"
TITLE = "Last.fm"
DOC = "docs/lastfm.md"
API = "https://ws.audioscrobbler.com/2.0/"
TIMEOUT = 60
PER_PAGE = 200            # the API's maximum
HISTORY_PAGES = 50        # older pages a morning: 10,000 scrobbles
LOOKBACK_DAYS = 7         # late scrobbles are looked for this far back
PAUSE = 0.5               # seconds between requests
RETRY_PAUSE = 10          # before the one retry
# 8 operation failed ("please try again"), 11 service offline, 16 temporary
# error, 29 rate limit: worth one more try. Anything else is said at once.
RETRY_CODES = {8, 11, 16, 29}
AGAIN = "python -m lifecrm connect lastfm <username>"


def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "lifecrm (personal use)"})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> int:
    return int(time.time())


def steps() -> list[str]:
    return [
        "On last.fm (a free account, if you have none): Settings › Applications › "
        "Spotify Scrobbling › Connect, and allow it — from then on Spotify sends each "
        "song you play there, on any device (verify the menu path).",
        "Still in Settings › Privacy: leave 'Hide recent listening information' "
        "unticked, or Last.fm's API hands nothing over.",
        "At last.fm/api/account/create make an API account (any name, e.g. lifecrm; "
        "callback and homepage can stay empty) and copy its 'API key' — not the "
        "shared secret.",
        "In a terminal here: python -m lifecrm connect lastfm <your username> — "
        "paste the key when asked (hidden, kept in data/secrets.json).",
        "The next morning collect fetches it and /morning asks before the first "
        "import; a long history then arrives 10,000 scrobbles a morning, newest first.",
    ]


# ---------------------------------------------------------------- the API

def _one_line(text, limit: int = 160) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _api_error(code: int, message) -> DomainError:
    said = f"error {code}: {_one_line(message) or 'no message'}"
    if code in (10, 26):
        return DomainError(f"Last.fm refused the API key ({said}) — copy the 'API key' "
                           f"(not the shared secret) from last.fm/api/accounts, or make a "
                           f"new one at last.fm/api/account/create, and connect lastfm "
                           f"again: {AGAIN}")
    if code == 6 and "user" in str(message or "").lower():
        return DomainError(f"Last.fm has no such user ({said}) — the username is the end "
                           f"of your profile's address, last.fm/user/<username>; connect "
                           f"lastfm again: {AGAIN}")
    if code == 17:
        return DomainError(f"Last.fm hides this user's listening ({said}) — on last.fm › "
                           f"Settings › Privacy untick 'Hide recent listening "
                           f"information', then: python -m lifecrm fetch lastfm")
    if code == 29:
        return DomainError(f"Last.fm says too many requests ({said}) — nothing more "
                           f"fetched now; the next run carries on")
    return DomainError(f"Last.fm answered {said} — the next run tries again; if it keeps "
                       f"failing, connect lastfm again: {AGAIN}")


def _call(method: str, key: str, **params) -> dict:
    """One API call, as JSON. The key is in the URL, so no URL — and no
    exception that carries one — ever reaches a message."""
    query = urllib.parse.urlencode({"method": method, **params, "api_key": key,
                                    "format": "json"})
    url = f"{API}?{query}"
    for attempt in (1, 2):
        status, body = 200, b""
        try:
            with _open(url) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = e.read() or b""
            except (OSError, AttributeError, ValueError):
                body = b""
        except OSError as e:
            reason = _one_line(getattr(e, "reason", None) or e)
            if attempt == 1:
                _pause(RETRY_PAUSE)
                continue
            raise DomainError(f"could not reach Last.fm ({reason}) — nothing fetched; the "
                              f"next run tries again") from None
        try:
            data = json.loads(body.decode("utf-8-sig")) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        code = data.get("error") if isinstance(data, dict) else None
        if code is not None:
            code = lf._int(code) or 0
            if code in RETRY_CODES and attempt == 1:
                _pause(RETRY_PAUSE)
                continue
            raise _api_error(code, data.get("message"))
        if status >= 500 or (status == 200 and not isinstance(data, dict)):
            if attempt == 1:
                _pause(RETRY_PAUSE)
                continue
            what = f"HTTP {status}" if status != 200 else "something that is not JSON"
            raise DomainError(f"Last.fm answered {what} — nothing fetched; the next run "
                              f"tries again")
        if status >= 400:
            raise DomainError(f"Last.fm answered HTTP {status} — check the account, or "
                              f"connect lastfm again: {AGAIN}")
        return data
    raise AssertionError("unreachable")


def _recent(user: str, key: str, page: int, **span) -> tuple[dict, dict]:
    """(response, read_page of it) for one page of user.getRecentTracks."""
    data = _call("user.getRecentTracks", key, user=user, limit=PER_PAGE, page=page, **span)
    return data, lf.read_page(data, "Last.fm's answer")


# ---------------------------------------------------------------- connect

def _username(raw) -> str:
    s = str(raw or "").strip().strip("<>\"'")
    m = re.search(r"last\.fm/user/([^/?#\s]+)", s, re.I)
    if m:
        s = urllib.parse.unquote(m.group(1))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", s):
        raise DomainError("that is not a Last.fm username — it is the end of your profile's "
                          "address, last.fm/user/<username>")
    return s


def _api_key(raw) -> str:
    s = str(raw or "").strip().strip("<>\"'")
    if not re.fullmatch(r"[A-Za-z0-9]{16,64}", s):
        raise DomainError("that does not look like a Last.fm API key (32 letters and digits) "
                          "— copy 'API key' from last.fm/api/accounts, not the shared secret "
                          "or a link")
    return s


def connect(cfg: Config, args: list[str], ask) -> dict:
    """The username (an argument, or asked) and the API key (always asked,
    hidden: a key on the command line stays in the shell's history), both
    checked now — a typo is said here, not at 06:30. Reconnecting the same
    user (a new key) keeps the cursor: the history is not fetched again."""
    if len(args) > 1:
        raise DomainError("connect lastfm takes only the username — the API key is asked "
                          "for, hidden, so it never lands in your shell's history")
    if ask is None:
        raise DomainError(f"Last.fm's API key is typed in when asked — run it in a "
                          f"terminal: {AGAIN}")
    user = _username(args[0] if args else ask("Your Last.fm username", False))
    key = _api_key(ask("Your Last.fm API key (from last.fm/api/accounts; hidden)", True))
    info = _call("user.getInfo", key, user=user).get("user")
    if not isinstance(info, dict):
        raise DomainError("Last.fm's answer about the user had no user in it — try again "
                          "later")
    user = lf._text(info.get("name")) or user
    _call("user.getRecentTracks", key, user=user, limit=1)   # hidden listening, said now
    kept = {"user": user, "api_key": key}
    old = secrets.load(cfg).get(SERVICE) or {}
    if old.get("cursor") and str(old.get("user", "")).casefold() == user.casefold():
        kept["cursor"] = old["cursor"]
    return kept


# ---------------------------------------------------------------- fetch

def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def _all_pages(user: str, key: str, start: int, end: int) -> list[dict]:
    """Every page of [start, end). `from` one second early and `to` as
    given: whether Last.fm counts either edge in, the connector clips the
    window exactly, so nothing at a boundary second is lost."""
    span = {"from": start - 1, "to": end}
    out, page = [], 1
    while True:
        data, read = _recent(user, key, page, **span)
        out.append(data)
        if not read["items"] or page >= (read["pages"] or 1):
            return out
        page += 1
        _pause(PAUSE)


def _digests(pages: list[dict], start: int, end: int, cfg: Config) -> dict[str, str]:
    """{day: digest of its scrobbles} — what tells a quiet morning, or a day
    a late scrobble changed, without keeping the scrobbles themselves."""
    per_day: dict[str, set] = {}
    for data in pages:
        for s in lf.page_scrobbles(data):
            if start <= s.uts < end:
                per_day.setdefault(lf.journal_day(s.uts, cfg), set()).add(
                    (s.uts, s.artist or "", s.track, s.album or ""))
    return {d: hashlib.sha1(repr(sorted(rows)).encode("utf-8")).hexdigest()[:16]
            for d, rows in sorted(per_day.items())}


def _holding(pages: list[dict], start: int, end: int) -> list[dict]:
    return [p for p in pages if any(start <= s.uts < end for s in lf.page_scrobbles(p))]


def _history(user: str, key: str, oldest: int, cfg: Config) -> dict:
    """One morning's backfill before `oldest`, newest first, cut at a day
    boundary. Returns {pages, from, done, older, error}: `pages` hold every
    scrobble in [from, oldest); `from` is None when nothing whole came."""
    pages: list[dict] = []
    total = seen_min = None
    done, error, page = False, None, 1
    while True:
        try:
            data, read = _recent(user, key, page, to=oldest)
        except DomainError as e:
            error = str(e)
            break
        pages.append(data)
        if total is None:
            total = read["total"]
        times = [s.uts for s in read["scrobbles"] if s.uts < oldest]
        if times:
            seen_min = min(times + ([seen_min] if seen_min is not None else []))
        if not read["items"] or page >= (read["pages"] or 1):
            done = True
            break
        whole = (seen_min is not None and
                 lf.day_start(lf.next_day(lf.journal_day(seen_min, cfg)), cfg) < oldest)
        if page >= HISTORY_PAGES and whole:
            break            # a day longer than the batch is read to its start first
        page += 1
        _pause(PAUSE)
    if seen_min is None:
        start = None
    elif done:
        start = lf.day_start(lf.journal_day(seen_min, cfg), cfg)
    else:
        start = lf.day_start(lf.next_day(lf.journal_day(seen_min, cfg)), cfg)
        if start >= oldest:
            start = None     # stopped inside its first day: nothing whole yet
    held = set()
    if start is not None:
        for p in pages:
            held |= {s.key() for s in lf.page_scrobbles(p) if start <= s.uts < oldest}
    older = 0 if done else (max(total - len(held), 0) if isinstance(total, int) else None)
    return {"pages": _holding(pages, start, oldest) if start is not None else [],
            "from": start, "done": done and error is None, "older": older, "error": error}


def _stamp(now: int, *folders: Path) -> str:
    base_stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    stamp, n = base_stamp, 1
    while any(any(f.glob(f"{stamp}-*.json")) for f in folders if f.is_dir()):
        n += 1
        stamp = f"{base_stamp}-{n}"
    return stamp


def fetch(cfg: Config, secret: dict, dest: Path) -> Path | None:
    user, key = secret.get("user"), secret.get("api_key")
    if not user or not key:
        raise DomainError(f"Last.fm is connected without a username or key — connect it "
                          f"again: {AGAIN}")
    pending, archive, incoming = dest / "pending", dest / "archive", dest / ".incoming"
    shutil.rmtree(incoming, ignore_errors=True)         # a run that died half-way
    cursor = dict(secret.get("cursor") or {})
    now = _now()
    today = lf.journal_day(now, cfg)
    look = lf.day_start(_shift(today, -LOOKBACK_DAYS), cfg)
    newest = cursor.get("newest")
    start = min(newest, look) if isinstance(newest, int) else look

    saved: list[tuple[str, dict, dict]] = []             # (part, window, response)
    new_pages = _all_pages(user, key, start, now)        # fails whole, saves nothing
    digests = _digests(new_pages, start, now, cfg)
    before = {d: v for d, v in (cursor.get("digests") or {}).items()
              if d >= lf.journal_day(start, cfg)}
    changed = sorted(d for d in set(digests) | set(before)
                     if digests.get(d) != before.get(d))
    if changed:
        frm = lf.day_start(changed[0], cfg)
        saved += [("new", {"from": frm, "to": now}, p)
                  for p in _holding(new_pages, frm, now)]
    cursor.update(newest=lf.day_start(today, cfg), digests=digests)

    oldest = cursor.get("oldest") if isinstance(cursor.get("oldest"), int) else start
    if not cursor.get("history_done"):
        _pause(PAUSE)
        h = _history(user, key, oldest, cfg)
        saved += [("history", {"from": h["from"], "to": oldest}, p) for p in h["pages"]]
        if h["from"] is not None:
            oldest = h["from"]
        cursor.update(oldest=oldest, history_done=h["done"], history_error=h["error"],
                      history_older=(h["older"] if h["older"] is not None
                                     else cursor.get("history_older")))
    status = {"back_to": lf.journal_day(oldest, cfg), "done": bool(cursor.get("history_done")),
              "older": cursor.get("history_older"), "error": cursor.get("history_error")}

    if saved:
        _save(saved, user, now, status, incoming, pending, archive)
    secrets.update(cfg, SERVICE, cursor=cursor)
    return pending if pending.is_dir() and any(pending.glob("*.json")) else None


def _save(saved, user, now, status, incoming, pending, archive) -> None:
    """Every page of the morning into a scratch folder first, then into
    pending/ — a run cut short leaves no half a window behind."""
    incoming.mkdir(parents=True, exist_ok=True)
    stamp = _stamp(now, pending, archive)
    by_part: dict[str, list] = {}
    for part, window, data in saved:
        by_part.setdefault(part, []).append((window, data))
    for part, items in by_part.items():
        for n, (window, data) in enumerate(items, 1):
            wrapper = {"format": lf.FORMAT, "service": SERVICE, "user": user,
                       "fetched_at": lf.iso(now),
                       "window": {**window, "part": part, "page": n, "pages": len(items)},
                       "history": status, "response": data}
            (incoming / f"{stamp}-{part}-p{n:04d}.json").write_text(
                json.dumps(wrapper, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    pending.mkdir(parents=True, exist_ok=True)
    for f in sorted(incoming.iterdir()):
        os.replace(f, pending / f.name)
    incoming.rmdir()


def settle(cfg: Config, secret: dict, target: Path) -> None:
    """A committed import read `target` (pending/): its pages move to
    archive/, kept, never deleted, so tomorrow's read is tomorrow's."""
    target = Path(target)
    if target.name != "pending" or not target.is_dir():
        return
    archive = target.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    for f in sorted(target.glob("*.json")):
        out, n = archive / f.name, 1
        while out.exists():
            n += 1
            out = archive / f"{f.stem}-{n}{f.suffix}"
        os.replace(f, out)

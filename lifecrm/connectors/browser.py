"""Browser connector: the pages you visited, one record per page per day.

What you read is a record of what you were working on and planning — the
board deck template, the passport renewal page, the Lake Bled itinerary —
and every browser already keeps it in a local SQLite file. This connector
reads that file. It never opens it for writing and never talks to the
browser.

Sources
-------
  Chrome, Edge, Brave   the profile's `History` database: `urls` (one row
  (any Chromium)        per URL, with its current title) and `visits` (one
                        row per visit). Times are microseconds since
                        1601-01-01 UTC.
                          Windows  %LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default\\History
                                   (Microsoft\\Edge\\..., BraveSoftware\\Brave-Browser\\...)
                          macOS    ~/Library/Application Support/Google/Chrome/Default/History
  Safari                `~/Library/Safari/History.db`: `history_items` (URLs)
                        and `history_visits` (visits, each with the title
                        the page had then). Times are Core Data seconds
                        since 2001-01-01. macOS guards the folder: without
                        Full Disk Access the read fails, and says so.

A running browser holds its history open — Chromium with an exclusive lock
on Windows — so the file is snapshotted through SQLite's backup API, and when
that is refused, copied byte for byte first (with its -journal / -wal) and
the copy read instead. The report says which happened.

Volume control: one observation per (page, day)
-----------------------------------------------
A visit-per-row archive would be tens of thousands of rows a month for no
gain: "what was I reading the week of the board meeting" needs the page and
the day, not every reload. So each record is one URL on one journal day
(home timezone, the configured rollover hour applied — a 1 a.m. visit counts
toward the evening before, as everything else does):

  title       the page's title (Safari: the last title it had that day;
              Chromium keeps only each URL's current one). Unread-count
              prefixes like '(3) ' and Gmail's 'Inbox (12)' are dropped, so
              a title does not change every time mail arrives
  body        the URL
  value       visits that day (unit 'visits')
  value_json  {domain, first_visit, last_visit, typed_count (Chromium:
              visits typed into the address bar), browser, profile,
              synced (visits another of your devices synced in, when any)}
  external_id sha1(browser/profile, url, day)

Skipped and counted, never imported: anything that is not an http(s) page
(chrome://, edge://, about:, file:, extensions, data:), frames a page loaded
by itself, loads that failed, and the domains listed in
[connectors.browser] exclude_domains (a domain covers its subdomains).

Re-importing, and what wins
---------------------------
A record's last_modified_at is its last visit. Today's record grows as you
keep browsing and the next import updates it. An earlier day never gains a
visit, so it is identical on every re-import — except when the browser has
since expired or deleted some of that day's visits, or (Chromium) the page's
current title changed. Both are conflicts and the archived record is kept;
the report counts them on one line each instead of listing them.

Mirroring: none. Browsers expire history on their own (Chrome after about
90 days, Safari after a year by default) — expiry is not the owner deleting
anything, so the archive remembers.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from .. import platforms
from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "browser"
streams = ("browser_visit",)

CHROME_EPOCH = 11644473600          # 1601-01-01 to 1970-01-01, in seconds
APPLE_EPOCH = 978307200             # 2001-01-01T00:00:00Z in Unix seconds
# Chromium page transitions: the low byte is the core type.
_TYPED, _AUTO_SUBFRAME = 1, 3
_SOURCE_SYNCED = 0                  # visit_source.source: arrived through sync

BROWSERS = ("chrome", "edge", "brave", "chromium", "vivaldi", "opera", "safari")
_LABELS = {"chrome": "Chrome", "edge": "Edge", "brave": "Brave",
           "chromium": "Chromium", "vivaldi": "Vivaldi", "opera": "Opera",
           "safari": "Safari"}
_FULL_DISK = ("grant Full Disk Access to the app running this (System Settings > "
              "Privacy & Security > Full Disk Access), then run it again")
_COUNTER_PREFIX = re.compile(r"^\(\d+\+?\)\s+")
_INBOX_COUNT = re.compile(r"\b(Inbox|Posteingang|Bandeja de entrada) \(\d[\d,.]*\)")


# ---------------------------------------------------------------- locations

def _candidates() -> list[tuple[str, Path]]:
    """(browser, default profile's history) on this OS, in the order tried.

    Browsers the OS ships with (Edge on Windows, Safari on a Mac) come last:
    that they are installed says nothing about whether they are used.
    """
    home = platforms.home()
    if platforms.is_windows():
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return [("chrome", local / "Google" / "Chrome" / "User Data" / "Default" / "History"),
                ("brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"
                 / "Default" / "History"),
                ("edge", local / "Microsoft" / "Edge" / "User Data" / "Default" / "History")]
    if platforms.is_mac():
        sup = home / "Library" / "Application Support"
        return [("chrome", sup / "Google" / "Chrome" / "Default" / "History"),
                ("brave", sup / "BraveSoftware" / "Brave-Browser" / "Default" / "History"),
                ("edge", sup / "Microsoft Edge" / "Default" / "History"),
                ("safari", home / "Library" / "Safari" / "History.db")]
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [("chrome", conf / "google-chrome" / "Default" / "History"),
            ("brave", conf / "BraveSoftware" / "Brave-Browser" / "Default" / "History"),
            ("edge", conf / "microsoft-edge" / "Default" / "History"),
            ("chromium", conf / "chromium" / "Default" / "History")]


def _present(p: Path) -> bool:
    """A file macOS will not let us stat still exists — scan() then explains
    the permission instead of the history seeming to be absent."""
    try:
        return p.is_file()
    except PermissionError:
        return True
    except OSError:
        return False


def default_target(cfg: Config) -> Path:
    """[connectors.browser] path, else the default profile of the first
    browser found (`browser = "edge"` in the same table picks one)."""
    settings = cfg.connector_settings(name)
    if settings.get("path"):
        p = Path(str(settings["path"])).expanduser()
        if not _present(p) and not p.is_dir():
            raise DomainError(f"[connectors.browser] path does not exist: {p}")
        return p
    want = settings.get("browser")
    if want is not None and str(want).lower() not in BROWSERS:
        raise DomainError(f"[connectors.browser] browser '{want}' is not one of "
                          f"{', '.join(BROWSERS)}")
    tried = [(b, p) for b, p in _candidates() if want is None or b == str(want).lower()]
    if not tried:
        raise DomainError(f"no default location is known for {_LABELS[str(want).lower()]} "
                          f"on this system — set [connectors.browser] path to its History file")
    for _, p in tried:
        if _present(p):
            return p
    raise DomainError(
        "no browser history found — looked for "
        + "; ".join(f"{_LABELS[b]}: {p}" for b, p in tried)
        + ". Pass the profile's History file (or folder), or set "
          "[connectors.browser] path")


def _resolve(target: Path) -> Path:
    """The history database itself, from the file or the folder holding it."""
    if not target.is_dir():
        return target
    for rel in ("History", "History.db", "Default/History"):
        p = target / rel
        if p.is_file():
            return p
    listing = ", ".join(sorted(p.name for p in target.iterdir())[:10]) or "(empty)"
    raise DomainError(f"{target.name} holds no browser history (no History, History.db "
                      f"or Default/History) — it has: {listing}")


def _browser_from_path(p: Path) -> str | None:
    """Which browser a history file belongs to, from the folder it lives in.
    Whole folder names only, so a user called 'edgar' is not Edge."""
    parts = [x.lower() for x in p.parts]
    pairs = set(zip(parts, parts[1:]))
    if "bravesoftware" in parts or "brave-browser" in parts:
        return "brave"
    if ("microsoft", "edge") in pairs or "microsoft edge" in parts \
            or "microsoft-edge" in parts:
        return "edge"
    if "vivaldi" in parts:
        return "vivaldi"
    if "opera software" in parts or "com.operasoftware.opera" in parts:
        return "opera"
    if ("google", "chrome") in pairs or "google-chrome" in parts:
        return "chrome"
    if "chromium" in parts:
        return "chromium"
    if "safari" in parts:
        return "safari"
    return None


_PROFILE_DIR = re.compile(r"^(Default|Profile \d+|Guest Profile|System Profile)$")


def _profile(db: Path, flavor: str) -> str:
    """The Chromium profile folder ('Default', 'Profile 2'); Safari has one
    history per user. A copied file outside its profile folder reads as
    'Default' — import from the browser's own folder to keep profiles apart."""
    if flavor == "chromium" and _PROFILE_DIR.match(db.parent.name):
        return db.parent.name
    return "Default"


def _sibling_profiles(db: Path) -> list[str]:
    """Other Chromium profiles beside this one, which this import does not read."""
    root = db.parent.parent
    if not _PROFILE_DIR.match(db.parent.name):
        return []
    try:
        return sorted(p.parent.name for p in root.glob("*/History")
                      if p.parent != db.parent and _PROFILE_DIR.match(p.parent.name))
    except OSError:
        return []


# ---------------------------------------------------------------- opening

def _denied(src: Path, e: OSError) -> DomainError:
    if platforms.is_mac():
        return DomainError(f"cannot read {src}: macOS keeps it behind Full Disk "
                           f"Access ({e.strerror}) — {_FULL_DISK}")
    return DomainError(f"cannot read {src}: {e.strerror} — the browser may hold it "
                       f"open; close the browser and run again")


class _Locked(Exception):
    """The browser holds the database; the backup API would wait for ever."""


_SQLITE_BUSY, _SQLITE_LOCKED = 5, 6


def _give_up_when_locked(status: int, remaining: int, total: int) -> None:
    # Connection.backup() retries a busy source forever (sleeping between
    # tries) — against a running Chromium that is a hang, not a wait. Each
    # step has already waited out the 0.5 s busy timeout when it reports
    # BUSY, so stop there and copy the bytes instead.
    if status in (_SQLITE_BUSY, _SQLITE_LOCKED):
        raise _Locked


def _snapshot(src: Path, tmp: Path) -> tuple[sqlite3.Connection, str]:
    """A private copy of the history, opened query-only, and how it was made:
    'backup' (SQLite's backup API — consistent even while the browser writes)
    or 'copy' (the browser held the file locked, so its bytes were copied
    first, -journal / -wal included, and SQLite recovers the copy)."""
    try:
        with open(src, "rb") as f:
            head = f.read(16)
    except PermissionError as e:
        raise _denied(src, e) from e
    if head != b"SQLite format 3\x00":
        raise DomainError(f"{src.name} is not an SQLite database — expected a "
                          f"browser's History (Chrome, Edge, Brave) or History.db "
                          f"(Safari)")
    dest = tmp / "history.db"
    how = "backup"
    try:
        source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True,
                                 timeout=0.5)
        try:
            copy = sqlite3.connect(dest)
            try:
                source.backup(copy, progress=_give_up_when_locked)
            finally:
                copy.close()
        finally:
            source.close()
    except (_Locked, sqlite3.OperationalError):
        # Locked (the browser is running) or a journal a read-only connection
        # cannot roll back. Either way the bytes can still be copied.
        for leftover in tmp.glob("history.db*"):
            leftover.unlink()
        try:
            shutil.copyfile(src, dest)
            for suffix in ("-journal", "-wal"):
                side = src.with_name(src.name + suffix)
                if side.exists():
                    shutil.copyfile(side, tmp / f"history.db{suffix}")
        except PermissionError as e:
            raise _denied(src, e) from e
        how = "copy"
    except sqlite3.DatabaseError as e:
        raise DomainError(f"cannot read {src.name} as a database: {e}") from e
    try:
        conn = sqlite3.connect(dest)
        conn.row_factory = sqlite3.Row
        # The first read recovers a copied hot journal; only then lock it down.
        conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        conn.execute("PRAGMA query_only = ON")
        return conn, how
    except sqlite3.DatabaseError as e:
        raise DomainError(f"cannot read {src.name} as a database: {e}") from e


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _flavor(tables: set[str], src: Path) -> str:
    if {"history_items", "history_visits"} <= tables:
        return "safari"
    if {"urls", "visits"} <= tables:
        return "chromium"
    raise DomainError(
        f"{src.name} is an SQLite database but not a browser history — it has "
        f"tables: {', '.join(sorted(tables)[:10]) or '(none)'}")


# ---------------------------------------------------------------- reading

def _chromium_visits(conn: sqlite3.Connection, tables: set[str]):
    """(url, title, unix seconds, typed, synced, frame, failed) per visit,
    oldest first. Chromium keeps one title per URL, so every visit carries
    the page's current one."""
    synced = "visit_source" in tables
    sql = ("SELECT v.id, v.visit_time, v.transition, u.url, u.title"
           + (", vs.source AS source" if synced else ", NULL AS source")
           + " FROM visits v JOIN urls u ON u.id = v.url"
           + (" LEFT JOIN visit_source vs ON vs.id = v.id" if synced else "")
           + " ORDER BY v.visit_time, v.id")
    for r in conn.execute(sql):
        t = r["visit_time"]
        # Integer division: microseconds through a float can land a hair
        # below the second they belong to.
        unix = int(t) // 1_000_000 - CHROME_EPOCH if isinstance(t, (int, float)) and t > 0 \
            else None
        core = int(r["transition"] or 0) & 0xFF
        yield (r["url"], r["title"], unix, core == _TYPED,
               r["source"] == _SOURCE_SYNCED, core == _AUTO_SUBFRAME, False)


def _safari_visits(conn: sqlite3.Connection):
    """The same tuples from Safari, whose visits carry the title the page had
    at the time. Columns newer Safaris added are read only where present."""
    cols = _columns(conn, "history_visits")
    ok = "v.load_successful" if "load_successful" in cols else "1"
    origin = "v.origin" if "origin" in cols else "0"
    sql = (f"SELECT v.id, v.visit_time, v.title, i.url, {ok} AS ok, {origin} AS origin "
           f"FROM history_visits v JOIN history_items i ON i.id = v.history_item "
           f"ORDER BY v.visit_time, v.id")
    for r in conn.execute(sql):
        t = r["visit_time"]
        unix = APPLE_EPOCH + int(t // 1) if isinstance(t, (int, float)) and t > 0 else None
        # origin 1: the visit was made on another device and synced in.
        yield (r["url"], r["title"], unix, False, r["origin"] == 1, False, r["ok"] == 0)


def _title(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    t = " ".join(raw.split())
    t = _INBOX_COUNT.sub(r"\1", _COUNTER_PREFIX.sub("", t))
    return t or None


def _excludes(cfg: Config) -> tuple[str, ...]:
    raw = cfg.connector_settings(name).get("exclude_domains", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(d, str) for d in raw):
        raise DomainError(f"[connectors.browser] exclude_domains must be a list of "
                          f"domain names, got {raw!r}")
    return tuple(sorted({d.strip().lower().strip(".").removeprefix("www.")
                         for d in raw if d.strip(".").strip()}))


def _domain(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower().removeprefix("www.") if host else None


def scan(target: Path, cfg: Config) -> base.Scan:
    excludes = _excludes(cfg)
    try:
        db = _resolve(target)
    except PermissionError as e:
        raise _denied(target, e) from e
    tmp = tempfile.TemporaryDirectory(prefix="lifecrm-browser-")
    try:
        conn, how = _snapshot(db, Path(tmp.name))
        try:
            tables = _tables(conn)
            flavor = _flavor(tables, db)
            schema = None
            if flavor == "chromium" and "meta" in tables:
                row = conn.execute("SELECT value FROM meta WHERE key='version'").fetchone()
                schema = row["value"] if row else None
            visits = list(_chromium_visits(conn, tables) if flavor == "chromium"
                          else _safari_visits(conn))
        finally:
            conn.close()
    finally:
        tmp.cleanup()

    if flavor == "safari":
        browser = "safari"
    else:
        hint = _browser_from_path(db)
        browser = hint if hint not in (None, "safari") else "chromium"

    profile = _profile(db, flavor)
    skipped: Counter = Counter()
    buckets: dict[tuple[str, str], dict] = {}
    for url, title, unix, typed, synced, frame, failed in visits:
        if not isinstance(url, str) or not url.strip():
            skipped["visit with no URL"] += 1
            continue
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme not in ("http", "https"):
            skipped[f"{scheme or 'schemeless'}: URL (browser-internal or local, "
                    f"not a web page)"] += 1
            continue
        if frame:
            skipped["frame a page loaded by itself (not a page you opened)"] += 1
            continue
        if failed:
            skipped["page that failed to load"] += 1
            continue
        if unix is None:
            skipped["visit with no usable time"] += 1
            continue
        host = _domain(url)
        if host is None:
            skipped["URL with no readable host"] += 1
            continue
        if any(host == d or host.endswith("." + d) for d in excludes):
            skipped["visit to a domain in exclude_domains"] += 1
            continue
        at = base.iso_utc(unix)
        day = base.journal_date_for_iso(at, cfg)
        b = buckets.get((url, day))
        if b is None:
            b = buckets[(url, day)] = {"host": host, "count": 0, "typed": 0,
                                       "synced": 0, "first": at, "title": None}
        b["count"] += 1
        b["last"] = at
        b["typed"] += typed
        b["synced"] += synced
        shown = _title(title)
        if shown:
            b["title"] = shown          # visits come oldest first: the last one wins

    key = f"{browser}/{profile}"
    records = []
    for (url, day), b in buckets.items():
        detail = {"domain": b["host"], "first_visit": b["first"], "last_visit": b["last"],
                  "typed_count": b["typed"] if flavor == "chromium" else None,
                  "browser": browser, "profile": profile,
                  "synced": b["synced"] or None}
        records.append({
            "stream": "browser_visit",
            "external_id": hashlib.sha1(f"{key}\n{url}\n{day}".encode()).hexdigest(),
            "journal_date": day,
            "started_at": b["first"],
            "ended_at": b["last"],
            "utc_offset_sec": None,
            "direction": None,
            "title": b["title"],
            "body": url,
            "value": b["count"],
            "unit": "visits",
            "value_json": canonical_json({k: v for k, v in detail.items() if v is not None}),
            "media_path": None,
            "origin_app": browser,
            "last_modified_at": b["last"],
            "participants": [],
        })
    records.sort(key=lambda r: (r["stream"], r["started_at"], r["external_id"]))

    domains: Counter = Counter()
    for b in buckets.values():
        domains[b["host"]] += b["count"]
    dates = [r["journal_date"] for r in records]
    side = [p for p in (db.with_name(db.name + "-journal"), db.with_name(db.name + "-wal"))
            if p.exists()]
    try:
        h = hashlib.sha256()
        for p in (db, *side):
            h.update(base.file_hash(p).encode())
        source_hash = h.hexdigest()
    except OSError:
        source_hash = None
    return base.empty_scan(
        name,
        # Labels the import (imports list); nothing is mirrored, so it scopes nothing.
        account=key,
        source_file=f"{db.parent.name}/{db.name}",
        source_hash=source_hash,
        records=records,
        skipped=[f"{v} × {k}" for k, v in sorted(skipped.items())],
        facts={"browser": browser, "profile": profile, "path": str(db.resolve()),
               "read": how, "schema_version": schema,
               "visits": sum(b["count"] for b in buckets.values()),
               "oldest_day": min(dates) if dates else None,
               "newest_day": max(dates) if dates else None,
               "top_domains": [[d, n] for d, n in
                               sorted(domains.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
               "other_profiles": _sibling_profiles(db)},
        flavor=flavor)


# ---------------------------------------------------------------- report

def _sample(rec: dict) -> str:
    title = rec["title"] or rec["body"]
    if len(title) > 60:
        title = title[:59] + "…"
    host = rec["body"].split("/")[2] if rec["body"].count("/") >= 2 else rec["body"]
    return f"{rec['journal_date']} ×{int(rec['value'])} {host.removeprefix('www.')} — {title}"


def _sort_conflicts(conflicts: list[dict]) -> tuple[int, int, list[dict]]:
    """(days the browser has since thinned, days whose page title changed,
    the rest). The first two are expected and kept quietly."""
    thinned = retitled = 0
    other = []
    for c in conflicts:
        ch = c["changes"]
        value = ch.get("value")
        if value and (value["incoming"] or 0) < (value["existing"] or 0):
            thinned += 1
        elif set(ch) == {"title"}:
            retitled += 1
        else:
            other.append(c)
    return thinned, retitled, other


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    facts = scan.get("facts") or {}
    label = _LABELS.get(facts.get("browser"), facts.get("browser") or "?")
    lines = [f"browser history: {label}, profile {facts.get('profile')} — "
             f"{scan['source_file']} ({facts.get('visits', 0)} visit(s) on "
             f"{len(scan['records'])} page-day(s)"
             + (f"; History schema v{facts['schema_version']}"
                if facts.get("schema_version") else "") + ")"]
    how = ("SQLite's backup API" if facts.get("read") == "backup" else
           "a byte copy — the browser held the file locked (it is probably "
           "running), so the file was copied first and the copy read")
    lines.append(f"  read from: {facts.get('path')} via {how}")
    if facts.get("oldest_day"):
        lines.append(f"  history spans {facts['oldest_day']}..{facts['newest_day']} — "
                     f"browsers keep about 90 days (Chrome, Edge, Brave) or a year "
                     f"(Safari) unless set otherwise")
    if facts.get("top_domains"):
        lines.append("  top domains: " + ", ".join(
            f"{d} ({n})" for d, n in facts["top_domains"]))

    new = plan["new"]
    upd = [rec for _, rec, _ in plan["updates"]]
    if new or upd:
        recs = new or upd
        span = f"{min(r['journal_date'] for r in recs)}..{max(r['journal_date'] for r in recs)}"
        latest = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
        lines.append(f"  browser_visit: {len(new)} new, {len(upd)} changed | {span} "
                     f"| e.g. {_sample(latest)}")
    else:
        lines.append("  nothing new — the history matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate page-day(s)")
    if tail:
        lines.append("  " + ", ".join(tail))

    thinned, retitled, other = _sort_conflicts(plan["conflicts"])
    if thinned:
        lines.append(f"  kept existing: {thinned} earlier page-day(s) now hold fewer "
                     f"visits in the browser (expired or deleted there) — the archive "
                     f"keeps the fuller count")
    if retitled:
        lines.append(f"  kept existing: {retitled} earlier page-day(s) whose page "
                     f"title has changed since — kept the title they were imported with")
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
    if facts.get("other_profiles"):
        lines.append(f"  not read: other {label} profile(s) beside this one — "
                     + ", ".join(facts["other_profiles"])
                     + " — import each by passing its folder")
    return lines

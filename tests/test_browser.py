"""Browser connector: Chrome/Edge/Brave and Safari history into browser_visit.

scan() is a pure reader over a private snapshot, so these build real History
databases with the demo generator's writers — Chromium's urls/visits with
microseconds since 1601, Safari's history_items/history_visits with Core Data
seconds — and check what is easy to get wrong: one record per page per
journal day, unread counters not churning titles, a running browser's lock,
macOS's Full Disk Access, and earlier days staying put while today grows.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.connectors import browser
from lifecrm.demo import persona
from lifecrm.demo.sources import browser as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()
ANCHOR = date(2026, 10, 21)
# 2026-03-10 10:00 in Austin.
T0 = int(datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc).timestamp())
DAY = 86400


def chrome(root, visits, profile="Default", vendor=("Google", "Chrome")):
    db = root.joinpath(*vendor, "User Data", profile, "History")
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    sample._write_chrome(db, sorted(visits))
    return db


def safari(root, visits):
    db = root / "Library" / "Safari" / "History.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    sample._write_safari(db, sorted(visits))
    return db


BASIC = [
    (T0, "https://example.com/a", "Page A", "typed"),
    (T0 + 3600, "https://example.com/a", "Page A", "link"),
    (T0 + 7200, "https://www.example.org/b?x=1", "Page B", "link"),
    (T0 + DAY, "https://example.com/a", "Page A", "bookmark"),
]


def _rec(scan, url, day):
    return next(r for r in scan["records"] if r["body"] == url and r["journal_date"] == day)


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- the demo histories

def test_the_demo_chrome_history_tells_the_persona_story(tmp_path, cfg):
    scan = browser.scan(sample.write_sample(tmp_path, ANCHOR, "android"), cfg)
    assert (scan["facts"]["browser"], scan["facts"]["profile"]) == ("chrome", "Default")
    titles = " | ".join(r["title"].lower() for r in scan["records"] if r["title"])
    for phrase in ("lake bled itinerary", "veracruz all natural menu",
                   "passport renewal usps", "board deck template"):
        assert phrase in titles
    # The deck on the day the journal finished market sizing: three visits that
    # day and one at 00:40, which still belongs to that evening.
    deck = _rec(scan, sample._DECK, persona.day(ANCHOR, -34).isoformat())
    assert deck["value"] == 4 and deck["unit"] == "visits"
    menu = json.loads(_rec(scan, sample._MENU, persona.day(ANCHOR, -38).isoformat())
                      ["value_json"])
    assert menu["synced"] == 1 and menu["domain"] == "veracruzallnatural.com"
    gmail = _rec(scan, sample._GMAIL, persona.day(ANCHOR, -40).isoformat())
    assert gmail["title"] == f"Inbox - {persona.OWNER.email} - Gmail"
    assert json.loads(gmail["value_json"])["typed_count"] == 1
    assert scan["skipped"] == [
        "2 × chrome: URL (browser-internal or local, not a web page)",
        "1 × file: URL (browser-internal or local, not a web page)",
        "1 × frame a page loaded by itself (not a page you opened)"]


def test_the_demo_safari_history_tells_the_same_story(tmp_path, cfg):
    scan = browser.scan(sample.write_sample(tmp_path, ANCHOR, "iphone"), cfg)
    assert scan["facts"]["browser"] == "safari"
    assert scan["flavor"] == "safari"
    titles = " | ".join(r["title"].lower() for r in scan["records"] if r["title"])
    assert "lake bled itinerary" in titles and "board deck template" in titles
    menu = json.loads(_rec(scan, sample._MENU, persona.day(ANCHOR, -1).isoformat())
                      ["value_json"])
    assert menu["synced"] == 1                      # opened on the iPhone, via iCloud
    assert "typed_count" not in menu                # Safari does not record it
    assert "1 × page that failed to load" in scan["skipped"]
    assert all(r["origin_app"] == "safari" for r in scan["records"])


def test_safari_keeps_the_title_the_page_had_that_day(tmp_path, cfg):
    db = safari(tmp_path, [(T0, "https://news.example/", "Monday's paper", "link"),
                           (T0 + DAY, "https://news.example/", "Tuesday's paper", "link")])
    scan = browser.scan(db, cfg)
    assert [r["title"] for r in scan["records"]] == ["Monday's paper", "Tuesday's paper"]


@pytest.mark.parametrize("combo", ["android", "iphone"])
def test_the_demo_applies_once_then_is_identical_and_doctor_stays_green(
        tmp_path, cfg, conn, combo):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities",
              "entry_sources", "extraction_runs", "entity_handles")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    path = sample.write_sample(tmp_path, ANCHOR, combo)
    _, first = _apply(conn, browser.scan(path, cfg))
    assert first["added"] == 35
    plan, again = _apply(conn, browser.scan(path, cfg))
    assert again["added"] == 0 and plan["identical"] == 35
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    assert after == before
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_the_demo_sample_is_deterministic(tmp_path, cfg):
    a = browser.scan(sample.write_sample(tmp_path / "a", ANCHOR, "android"), cfg)
    b = browser.scan(sample.write_sample(tmp_path / "b", ANCHOR, "android"), cfg)
    assert a["records"] == b["records"]


def test_an_unknown_combo_has_no_sample(tmp_path):
    with pytest.raises(ValueError, match="no sample for combo 'linux'"):
        sample.write_sample(tmp_path, ANCHOR, "linux")


# ---------------------------------------------------------------- one record per page per day

def test_visits_to_a_page_on_one_day_are_one_record(tmp_path, cfg):
    scan = browser.scan(chrome(tmp_path, BASIC), cfg)
    a = _rec(scan, "https://example.com/a", "2026-03-10")
    assert a["value"] == 2
    assert a["started_at"] == "2026-03-10T15:00:00Z"        # Chrome's micros truncated
    assert a["ended_at"] == a["last_modified_at"] == "2026-03-10T16:00:00Z"
    detail = json.loads(a["value_json"])
    assert detail == {"browser": "chrome", "domain": "example.com",
                      "first_visit": "2026-03-10T15:00:00Z",
                      "last_visit": "2026-03-10T16:00:00Z", "profile": "Default",
                      "typed_count": 1}
    assert _rec(scan, "https://example.com/a", "2026-03-11")["value"] == 1
    b = _rec(scan, "https://www.example.org/b?x=1", "2026-03-10")
    assert json.loads(b["value_json"])["domain"] == "example.org"
    assert len(scan["records"]) == 3
    assert all(r["participants"] == [] and r["direction"] is None for r in scan["records"])


def test_chrome_and_safari_times_land_on_the_same_instant(tmp_path, cfg):
    c = browser.scan(chrome(tmp_path / "c", BASIC), cfg)
    s = browser.scan(safari(tmp_path / "s", BASIC), cfg)
    assert [r["started_at"] for r in c["records"]] == [r["started_at"] for r in s["records"]]
    assert [r["value"] for r in c["records"]] == [r["value"] for r in s["records"]]


def test_a_visit_after_midnight_counts_toward_the_evening_before(tmp_path, cfg):
    late = T0 + 15 * 3600 - 20 * 60          # 00:40 the next morning in Austin
    scan = browser.scan(chrome(tmp_path, [(T0, "https://example.com/a", "A", "link"),
                                          (late, "https://example.com/a", "A", "link")]), cfg)
    (rec,) = scan["records"]
    assert rec["journal_date"] == "2026-03-10" and rec["value"] == 2


@pytest.mark.parametrize("raw,shown", [
    ("(3) Facebook", "Facebook"),
    ("(12+) X", "X"),
    ("Inbox (1,204) - me@example.com - Gmail", "Inbox - me@example.com - Gmail"),
    ("Top 10 films (2024)", "Top 10 films (2024)"),
    ("  spaced   out  ", "spaced out"),
    ("", None),
    (None, None),
])
def test_unread_counters_do_not_change_a_page_title(raw, shown):
    assert browser._title(raw) == shown


def test_the_same_page_in_another_profile_is_another_record(tmp_path, cfg):
    a = browser.scan(chrome(tmp_path / "c", BASIC), cfg)
    edge = chrome(tmp_path / "e", BASIC, profile="Profile 2", vendor=("Microsoft", "Edge"))
    chrome(tmp_path / "e", BASIC, vendor=("Microsoft", "Edge"))
    b = browser.scan(edge, cfg)
    assert (b["facts"]["browser"], b["facts"]["profile"]) == ("edge", "Profile 2")
    assert b["account"] == "edge/Profile 2"
    assert not {r["external_id"] for r in a["records"]} & {r["external_id"] for r in b["records"]}
    assert b["facts"]["other_profiles"] == ["Default"]
    text = "\n".join(browser.report_lines(b, {"new": b["records"], "updates": [],
                                               "refreshed": [], "conflicts": [],
                                               "identical": 0, "dup_incoming": 0,
                                               "invalid": []}))
    assert "not read: other Edge profile(s) beside this one — Default" in text


@pytest.mark.parametrize("parts,expected", [
    (("Users", "edgar", "AppData", "Local", "Google", "Chrome", "User Data", "Default"), "chrome"),
    (("Users", "a", "AppData", "Local", "BraveSoftware", "Brave-Browser", "User Data",
      "Default"), "brave"),
    (("Users", "a", "Library", "Application Support", "Microsoft Edge", "Default"), "edge"),
    (("home", "a", ".config", "chromium", "Default"), "chromium"),
    (("Users", "a", "Downloads"), None),
])
def test_the_browser_is_read_from_whole_folder_names(tmp_path, parts, expected):
    assert browser._browser_from_path(tmp_path.joinpath(*parts, "History")) == expected


def test_a_copied_file_outside_its_profile_reads_as_chromium_default(tmp_path, cfg):
    src = chrome(tmp_path / "c", BASIC)
    copy = tmp_path / "Downloads" / "History"
    copy.parent.mkdir()
    copy.write_bytes(src.read_bytes())
    scan = browser.scan(copy, cfg)
    assert (scan["facts"]["browser"], scan["facts"]["profile"]) == ("chromium", "Default")


# ---------------------------------------------------------------- what is skipped

def test_exclude_domains_drops_a_site_and_its_subdomains(tmp_path, cfg):
    cfg.connectors = {"browser": {"exclude_domains": ["www.Example.com", "nothing.test"]}}
    db = chrome(tmp_path, BASIC + [(T0 + 60, "https://mail.example.com/", "Mail", "typed"),
                                   (T0 + 120, "https://notexample.com/", "Other", "link")])
    scan = browser.scan(db, cfg)
    assert sorted(r["body"] for r in scan["records"]) == [
        "https://notexample.com/", "https://www.example.org/b?x=1"]
    assert "4 × visit to a domain in exclude_domains" in scan["skipped"]


def test_a_malformed_exclude_domains_setting_is_refused(tmp_path, cfg):
    cfg.connectors = {"browser": {"exclude_domains": {"bank.example": True}}}
    with pytest.raises(DomainError, match="exclude_domains must be a list"):
        browser.scan(chrome(tmp_path, BASIC), cfg)


# ---------------------------------------------------------------- re-importing

def test_today_grows_and_updates_while_earlier_days_stay_identical(tmp_path, cfg, conn):
    _apply(conn, browser.scan(chrome(tmp_path / "a", BASIC), cfg))
    later = BASIC + [(T0 + DAY + 600, "https://example.com/a", "Page A", "reload")]
    scan = browser.scan(chrome(tmp_path / "b", later), cfg)
    plan, result = _apply(conn, scan)
    assert plan["identical"] == 2 and result["updated"] == 1 and not plan["conflicts"]
    row = conn.execute("SELECT value FROM observations WHERE journal_date='2026-03-11'"
                       ).fetchone()
    assert row["value"] == 2
    assert "browser_visit: 0 new, 1 changed" in "\n".join(browser.report_lines(scan, plan))


def test_expired_visits_keep_the_fuller_count_and_say_so(tmp_path, cfg, conn):
    """Chrome deletes visits past ~90 days: the oldest day thins out. That is
    not a deletion by the owner, and the archive keeps what it had."""
    _apply(conn, browser.scan(chrome(tmp_path / "a", BASIC), cfg))
    scan = browser.scan(chrome(tmp_path / "b", BASIC[1:]), cfg)
    plan, result = _apply(conn, scan)
    assert len(plan["conflicts"]) == 1 and result["updated"] == 0
    assert conn.execute("SELECT value FROM observations WHERE journal_date='2026-03-10' "
                        "AND body='https://example.com/a'").fetchone()["value"] == 2
    text = "\n".join(browser.report_lines(scan, plan))
    assert "kept existing: 1 earlier page-day(s) now hold fewer visits" in text
    assert "CONFLICT" not in text


def test_a_changed_chromium_title_keeps_the_imported_one(tmp_path, cfg, conn):
    _apply(conn, browser.scan(chrome(tmp_path / "a", BASIC), cfg))
    renamed = [(t, u, "Page A, redesigned" if u.endswith("/a") else title, how)
               for t, u, title, how in BASIC]
    scan = browser.scan(chrome(tmp_path / "b", renamed), cfg)
    plan, _ = _apply(conn, scan)
    text = "\n".join(browser.report_lines(scan, plan))
    assert "kept existing: 2 earlier page-day(s) whose page title has changed" in text
    assert {r["title"] for r in conn.execute(
        "SELECT title FROM observations WHERE body='https://example.com/a'")} == {"Page A"}


def test_nothing_is_mirrored(tmp_path, cfg, conn):
    _apply(conn, browser.scan(chrome(tmp_path / "a", BASIC), cfg))
    scan = browser.scan(chrome(tmp_path / "b", BASIC[3:]), cfg)
    assert "present_ids" not in scan
    plan, _ = _apply(conn, scan)
    assert plan["removed"] == []
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 3


# ---------------------------------------------------------------- opening the file

def test_a_locked_history_is_copied_and_the_report_says_so(tmp_path, cfg):
    """A running Chromium holds History with an exclusive lock: the backup API
    is refused, the bytes are copied, and only committed visits are read."""
    db = chrome(tmp_path, BASIC)
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute("PRAGMA locking_mode=EXCLUSIVE")
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("INSERT INTO visits (url, visit_time, transition) VALUES (1, ?, 1)",
                       ((T0 + 2 * DAY + browser.CHROME_EPOCH) * 1_000_000,))
        scan = browser.scan(db, cfg)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert scan["facts"]["read"] == "copy"
    assert len(scan["records"]) == 3
    plan = {"new": scan["records"], "updates": [], "refreshed": [], "conflicts": [],
            "identical": 0, "dup_incoming": 0, "invalid": []}
    assert "byte copy — the browser held the file locked" in \
        "\n".join(browser.report_lines(scan, plan))


def test_an_unreadable_safari_history_asks_for_full_disk_access(tmp_path, cfg, monkeypatch):
    db = safari(tmp_path, BASIC)

    def denied(path, *a, **kw):
        raise PermissionError(1, "Operation not permitted", str(path))

    monkeypatch.setattr(browser, "open", denied, raising=False)
    monkeypatch.setattr(platforms, "SYSTEM", "mac")
    with pytest.raises(DomainError, match="Full Disk Access"):
        browser.scan(db, cfg)
    monkeypatch.setattr(platforms, "SYSTEM", "windows")
    with pytest.raises(DomainError, match="close the browser"):
        browser.scan(db, cfg)


def test_a_file_that_is_not_sqlite_is_refused(tmp_path, cfg):
    p = tmp_path / "History"
    p.write_text("not a database", encoding="utf-8")
    with pytest.raises(DomainError, match="not an SQLite database"):
        browser.scan(p, cfg)


def test_a_database_that_is_not_a_history_names_its_tables(tmp_path, cfg):
    p = tmp_path / "History"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE cookies (name TEXT)")
    c.commit()
    c.close()
    with pytest.raises(DomainError, match="not a browser history — it has tables: cookies"):
        browser.scan(p, cfg)


def test_a_profile_or_user_data_folder_finds_its_history(tmp_path, cfg):
    db = chrome(tmp_path, BASIC)
    assert len(browser.scan(db.parent, cfg)["records"]) == 3            # Default/
    assert len(browser.scan(db.parent.parent, cfg)["records"]) == 3     # User Data/
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty" / "Bookmarks").write_text("{}", encoding="utf-8")
    with pytest.raises(DomainError, match="holds no browser history.*Bookmarks"):
        browser.scan(tmp_path / "empty", cfg)


def test_scan_is_deterministic(tmp_path, cfg):
    db = chrome(tmp_path, BASIC)
    a, b = browser.scan(db, cfg), browser.scan(db, cfg)
    assert a["records"] == b["records"] and a["source_hash"] == b["source_hash"]
    assert a["source_hash"]


# ---------------------------------------------------------------- report

def test_report_names_the_stream_span_and_how_it_read(tmp_path, cfg, conn):
    scan = browser.scan(sample.write_sample(tmp_path, ANCHOR, "android"), cfg)
    text = "\n".join(browser.report_lines(scan, obs.build_plan(conn, scan)))
    first, last = persona.day(ANCHOR, -40).isoformat(), persona.day(ANCHOR, -1).isoformat()
    assert f"browser_visit: 35 new, 0 changed | {first}..{last}" in text
    assert "browser history: Chrome, profile Default" in text
    assert "via SQLite's backup API" in text
    assert "top domains: docs.google.com (11)" in text
    assert "History schema v69" in text


# ---------------------------------------------------------------- default target

def _home(tmp_path, monkeypatch, system):
    monkeypatch.setattr(platforms, "SYSTEM", system)
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"SQLite format 3\x00")
    return p


def test_default_target_on_windows_prefers_chrome_then_brave_then_edge(
        tmp_path, cfg, monkeypatch):
    _home(tmp_path, monkeypatch, "windows")
    local = tmp_path / "AppData" / "Local"
    with pytest.raises(DomainError, match="no browser history found.*Chrome: "):
        browser.default_target(cfg)
    edge = _touch(local / "Microsoft" / "Edge" / "User Data" / "Default" / "History")
    assert browser.default_target(cfg) == edge
    brave = _touch(local / "BraveSoftware" / "Brave-Browser" / "User Data" / "Default"
                   / "History")
    assert browser.default_target(cfg) == brave
    chrome_db = _touch(local / "Google" / "Chrome" / "User Data" / "Default" / "History")
    assert browser.default_target(cfg) == chrome_db
    cfg.connectors = {"browser": {"browser": "edge"}}
    assert browser.default_target(cfg) == edge
    cfg.connectors = {"browser": {"browser": "netscape"}}
    with pytest.raises(DomainError, match="'netscape' is not one of"):
        browser.default_target(cfg)


def test_default_target_on_a_mac_falls_back_to_safari(tmp_path, cfg, monkeypatch):
    _home(tmp_path, monkeypatch, "mac")
    safari_db = _touch(tmp_path / "Library" / "Safari" / "History.db")
    assert browser.default_target(cfg) == safari_db
    chrome_db = _touch(tmp_path / "Library" / "Application Support" / "Google" / "Chrome"
                       / "Default" / "History")
    assert browser.default_target(cfg) == chrome_db


def test_default_target_is_the_configured_path(tmp_path, cfg):
    db = chrome(tmp_path, BASIC)
    cfg.connectors = {"browser": {"path": str(db)}}
    assert browser.default_target(cfg) == db
    cfg.connectors = {"browser": {"path": str(tmp_path / "nope" / "History")}}
    with pytest.raises(DomainError, match="path does not exist"):
        browser.default_target(cfg)


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_then_apply_then_identical(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    db = sample.write_sample(tmp_path / "exp", ANCHOR, "iphone")
    dry = runner.invoke(app, ["observe", "browser", str(db)])
    assert dry.exit_code == 0, dry.output
    assert "browser_visit: 35 new" in dry.output
    assert "dry run — nothing written" in dry.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0
    applied = runner.invoke(app, ["observe", "browser", str(db), "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "35 added" in applied.output
    again = runner.invoke(app, ["observe", "browser", str(db), "--apply"])
    assert "0 added" in again.output and "35 identical skipped" in again.output
    imports = runner.invoke(app, ["sql", "SELECT account FROM imports", "--json"])
    assert {r["account"] for r in json.loads(imports.output)} == {"safari/Default"}


def test_visit_times_are_utc_whatever_the_machine_zone(tmp_path, cfg):
    """Both browsers store UTC; nothing about the machine's zone leaks in."""
    instant = datetime(2026, 7, 1, 23, 59, 59, tzinfo=timezone.utc)
    db = chrome(tmp_path, [(int(instant.timestamp()), "https://example.com/", "x", "link")])
    (rec,) = browser.scan(db, cfg)["records"]
    assert rec["started_at"] == "2026-07-01T23:59:59Z"
    assert rec["journal_date"] == (instant - timedelta(hours=5)).date().isoformat()

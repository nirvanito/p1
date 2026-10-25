"""The assistant layer's fronts: the web UI's nudges endpoint and the
published calendar it serves, and the Obsidian Home note's Today section.

The web server computes nudges live on the clock's date and returns the
calendar byte for byte as the publisher wrote it; it never writes. The Home
note is dated by the database's last change and never reads the clock.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import date

import pytest

from lifecrm import db as db_mod
from lifecrm import recall
from lifecrm import reminders as reminders_mod
from lifecrm.config import load_config
from lifecrm.domain import nudges as state
from lifecrm.domain.util import now_utc
from lifecrm.export import build_vault
from lifecrm.web import server as web_server
from test_nudges import NOW, TODAY, seed
from test_web import Client


def _root(tmp_path, extra: str = ""):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.toml").write_text(
        '[paths]\ndata_dir = "data"\nbackups_dir = "backups"\n'
        '[time]\ntimezone = "America/Chicago"\nday_rollover_hour = 5\n'
        '[whisper]\nmodel = "small"\n' + extra, encoding="utf-8")
    cfg = load_config(root)
    db_mod.init_db(cfg.db_path, now_utc())
    conn = db_mod.connect(cfg.db_path)
    try:
        world = seed(conn, cfg)
    finally:
        conn.close()
    return cfg, world


@pytest.fixture
def served(tmp_path):
    cfg, world = _root(tmp_path, '[assistant]\nreminders_dir = "synced"\n')
    started = []

    def start(**opts):
        opts.setdefault("log_questions", False)
        opts.setdefault("use_llm", False)
        opts.setdefault("refresh_seconds", 0)
        server = web_server.create_server(cfg, "127.0.0.1", 0, **opts)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return Client(server)

    yield cfg, world, start
    for server in started:
        server.shutdown()
        server.server_close()


def _db_hash(cfg) -> str:
    return hashlib.sha256(cfg.db_path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- web

def test_api_nudges_is_the_live_pack_on_the_clock(served):
    cfg, world, start = served
    c = start()
    pack = c.get("/api/nudges")
    assert pack["pack_id"] and pack["today"] >= "2026-10-21"      # the clock, never a fixture
    assert {n["kind"] for n in pack["nudges"]} >= {"reminder"}
    assert any(n["key"] == f"due:{world['flights']['uuid']}" for n in pack["nudges"])
    assert "index" in pack and all(k.startswith(("N", "E", "O")) for k in pack["index"])


def test_reminders_ics_serves_the_published_bytes_and_never_writes(served):
    cfg, world, start = served
    c = start()
    before = _db_hash(cfg)
    status, msg, data = c.request("GET", "/reminders.ics")
    assert status == 404 and b"reminders publish" in data        # nothing published yet

    r = reminders_mod.publish(cfg, today=TODAY, now=NOW)
    assert r["components"] == 7
    status, msg, data = c.request("GET", "/reminders.ics")
    assert status == 200 and msg.get("Content-Type") == "text/calendar; charset=utf-8"
    assert data == reminders_mod.data_path(cfg).read_bytes()
    assert data.count(b"BEGIN:VEVENT") == 7
    assert before                            # the never-writes claim is its own test below


def test_reminders_ics_needs_the_key_like_the_api(served):
    cfg, world, start = served
    reminders_mod.publish(cfg, today=TODAY, now=NOW)
    c = start(token="s3cret")
    status, _, _ = c.request("GET", "/reminders.ics")
    assert status == 401
    status, msg, data = c.request("GET", "/reminders.ics?k=s3cret")
    assert status == 200 and b"BEGIN:VCALENDAR" in data
    status, _, _ = c.request("GET", "/api/nudges")
    assert status == 401
    assert c.get("/api/nudges?k=s3cret")["today"]


def test_the_server_never_writes_the_database(served):
    cfg, world, start = served
    reminders_mod.publish(cfg, today=TODAY, now=NOW)
    before = _db_hash(cfg)
    c = start()
    c.get("/api/nudges")
    c.request("GET", "/reminders.ics")
    assert _db_hash(cfg) == before


def test_the_page_draws_the_today_card_from_the_endpoint(served):
    cfg, world, start = served
    c = start()
    status, msg, html = c.request("GET", "/")
    assert status == 200
    text = html.decode("utf-8")
    assert 'api("/api/nudges")' in text and "function nudgesCard(" in text
    assert "lifecrm nudges snooze <key>" in text          # the writes stay in the CLI


# ---------------------------------------------------------------- Obsidian Home

AS_OF_TS = "2026-10-21T17:00:00Z"            # 12:00 in Chicago: journal day 10-21


@pytest.fixture
def home(conn, cfg):
    world = seed(conn, cfg)
    conn.execute("UPDATE audit_log SET ts=?", (AS_OF_TS,))
    conn.commit()
    notes = build_vault(conn, tz_name=cfg.timezone, cfg=cfg)
    return world, notes


def test_home_opens_with_today_from_the_as_of_date(home):
    world, notes = home
    text = notes["Home.md"]
    today = text.index("## Today")
    assert text.index("[[Sources]] · [[Index]]") < today < text.index("## Latest day")
    assert "As of 2026-10-21: 5 nudges. Live list, snooze and dismiss: `lifecrm nudges`" in text
    assert ("- **Overdue by 3 days: Renew passport** `task` · recorded [[2026-10-01]] · "
            f"`due:{world['passport']['uuid']}`") in text
    assert "- **Due in 3 days (2026-10-24): Book flights to Ljubljana** `task`" in text
    sam = world["people"]["Sam Okafor"]
    assert (f"'s birthday is in 4 days (2026-10-25), turning 34 *(observed: a contact card)* "
            f"· `birthday:{sam}:2026`") in text
    assert "[[" in text.split("'s birthday is in 4 days")[0].rsplit("\n", 1)[-1]   # a person link
    assert "not medical advice" in text and "Vitamin D averaged 2.0" in text
    assert "`nutrient:vitamin_d`" in text and "`stale:android_sms`" in text
    assert "**Agenda** *(calendar, observed)*" in text
    assert "- [[2026-10-21]] 10:00 — 1:1 Alex / Priya with Priya Natarajan" in text
    assert "- [[2026-10-22]] 13:00 — Dentist (moved) *(cancelled)*" in text
    assert "Visit Mom" not in text.split("## Open loops")[0].split("## Today")[1]


def test_the_today_section_never_reads_the_clock(home, conn, cfg, monkeypatch):
    world, notes = home
    monkeypatch.setattr(recall, "local_today", lambda c: date(2031, 1, 1))
    assert build_vault(conn, tz_name=cfg.timezone, cfg=cfg) == notes


def test_a_snoozed_nudge_leaves_today_and_is_counted(home, conn, cfg):
    world, notes = home
    state.snooze(conn, f"due:{world['passport']['uuid']}", until="2026-10-30")
    conn.execute("UPDATE audit_log SET ts=?", (AS_OF_TS,))      # keep the as-of date
    conn.commit()
    text = build_vault(conn, tz_name=cfg.timezone, cfg=cfg)["Home.md"]
    assert "Renew passport" not in text.split("## Latest day")[0]
    assert "As of 2026-10-21: 4 nudges, 1 snoozed or dismissed." in text

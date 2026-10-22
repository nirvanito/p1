"""The web UI server: every endpoint over a real seeded root, through HTTP.

The server runs on port 0 in a thread against a temp root holding the
golden draft (applied), a Claude chat import, a Health Connect import, and a
few texts from a phone handle linked to Jake. What is under test is the
contract the page relies on: JSON shapes; that the live database is never
written (except recall_log, when enabled) and never held open; that the
snapshot follows a live change; token and cross-site refusal; and that the
page is self-contained, with no external URL to fetch.
"""

import hashlib
import http.client
import json
import os
import re
import shutil
import struct
import threading
import zipfile

import pytest
from conftest import FIXTURES, cli_project, golden_draft
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import health_connect as hc
from lifecrm import recall
from lifecrm.config import load_config
from lifecrm.connectors import ai_chat
from lifecrm.domain import entities, entries, observations as obs, reconcile
from lifecrm.domain.util import now_utc
from lifecrm.web import api, icons
from lifecrm.web import server as web_server
from test_health_connect import make_hc_db
from test_reconcile import _ingest

JAKE = "+15125550142"
ME = "+15125550100"


# ---------------------------------------------------------------- seeding

def _texts_scan() -> dict:
    def rec(eid, ts, day, direction, body):
        sender, recipient = (JAKE, ME) if direction == "in" else (ME, JAKE)
        return {"stream": "imessage", "external_id": eid, "journal_date": day,
                "started_at": ts, "direction": direction, "body": body,
                "value_json": json.dumps({"chat": "chat-jake"}),
                "origin_app": "messages",
                "participants": [{"kind": "phone", "value": sender, "role": "sender"},
                                 {"kind": "phone", "value": recipient,
                                  "role": "recipient"}]}
    return {"connector": "ios_messages", "account": ME, "source_file": "sms.db",
            "source_hash": "test-texts-1",
            "records": [
                rec("t1", "2026-07-27T22:00:00Z", "2026-07-27", "in",
                    "Movement at 6? bring the chalk"),
                rec("t2", "2026-07-27T22:05:00Z", "2026-07-27", "out", "on my way"),
                rec("t3", "2026-07-28T17:00:00Z", "2026-07-28", "in",
                    "Veracruz tonight? my favorite")],
            "handles": [{"kind": "phone", "value": JAKE, "display_name": "Jake Morales"},
                        {"kind": "phone", "value": ME,
                         "display_name": "you (this phone)"}],
            "unmapped": [], "skipped": []}


def seed_root(tmp_path, extra_config: str = ""):
    """A project root with every tier populated, written through the domain
    layer exactly as the CLI would."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.toml").write_text(
        '[paths]\ndata_dir = "data"\nbackups_dir = "backups"\n'
        '[time]\ntimezone = "America/Chicago"\nday_rollover_hour = 5\n'
        '[whisper]\nmodel = "small"\n' + extra_config, encoding="utf-8")
    cfg = load_config(root)
    db_mod.init_db(cfg.db_path, now_utc())
    conn = db_mod.connect(cfg.db_path)
    try:
        reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))
        conn.commit()

        export = tmp_path / "exp" / "conversations.json"
        export.parent.mkdir()
        export.write_bytes((FIXTURES / "ai_chat_claude.json").read_bytes())
        scan = ai_chat.scan(export, cfg)
        obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
        conn.commit()

        hc_db = tmp_path / "healthconnect.db"
        make_hc_db(hc_db)
        z = tmp_path / "hc-export.zip"
        with zipfile.ZipFile(z, "w") as zf:
            zf.write(hc_db, "healthconnect.db")
        hc.apply_plan(conn, hc.build_plan(conn, hc.read_export(z, cfg)["records"]))
        conn.commit()

        texts = _texts_scan()
        obs.apply_plan(conn, texts, obs.build_plan(conn, texts))
        jake = conn.execute("SELECT id FROM entities WHERE name='Jake'").fetchone()["id"]
        obs.link_handle(conn, "phone", JAKE, jake)
        conn.commit()
    finally:
        conn.close()
    return cfg


class Client:
    def __init__(self, server):
        self.server = server
        self.port = server.server_address[1]

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.msg, r.read()
        finally:
            c.close()

    def get(self, path, headers=None, expect=200):
        status, msg, data = self.request("GET", path, headers=headers)
        assert status == expect, (path, status, data[:300])
        return json.loads(data) if "json" in (msg.get("Content-Type") or "") else data


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """Seeding takes seconds (every commit syncs), so it happens once and each
    test gets its own copy of the root."""
    base = tmp_path_factory.mktemp("web-seed")
    seed_root(base)
    return base / "root"


def copy_root(seeded, tmp_path):
    root = tmp_path / "root"
    shutil.copytree(seeded, root)
    return load_config(root)


@pytest.fixture(scope="module")
def shared(seeded, tmp_path_factory):
    """One server for the tests that only read; the rest start their own."""
    cfg = copy_root(seeded, tmp_path_factory.mktemp("web-shared"))
    server = web_server.create_server(cfg, "127.0.0.1", 0, log_questions=False,
                                      use_llm=False, refresh_seconds=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield cfg, Client(server)
    server.shutdown()
    server.server_close()


@pytest.fixture
def web(seeded, tmp_path):
    cfg = copy_root(seeded, tmp_path)
    started = []

    def start(**opts):
        opts.setdefault("log_questions", False)
        opts.setdefault("use_llm", False)
        opts.setdefault("refresh_seconds", 0)
        server = web_server.create_server(cfg, "127.0.0.1", 0, **opts)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return Client(server)

    yield cfg, start
    for server in started:
        server.shutdown()
        server.server_close()


def _db_hash(cfg) -> str:
    return hashlib.sha256(cfg.db_path.read_bytes()).hexdigest()


def _live(cfg, sql, args=()):
    conn = db_mod.connect(cfg.db_path, readonly=True)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


# ---------------------------------------------------------------- shapes

def test_every_endpoint_answers_json_in_the_documented_shape(web):
    cfg, start = web
    c = start()
    status = c.get("/api/status")
    assert {"today", "counts", "demo", "examples", "snapshot", "llm", "voice",
            "dashboards", "log_questions"} <= set(status)
    assert status["counts"]["entries"] == 6 and status["counts"]["observations"] >= 5
    assert status["snapshot"]["taken_at"]

    ask = c.get("/api/ask?q=how+did+I+sleep")
    assert {"question", "answer", "answer_source", "citations", "evidence",
            "evidence_text", "model", "index", "speech", "logged"} <= set(ask)

    day = c.get("/api/day?date=2026-07-27")
    assert {"date", "summary", "by_category", "health", "observed", "prev",
            "next", "mood", "energy", "sleep_hours", "sources"} <= set(day)
    assert set(c.get("/api/days?start=2026-07-01&end=2026-07-31")) == {"start", "end", "days"}

    people = c.get("/api/people")["people"]
    jake = next(p for p in people if p["name"] == "Jake")
    person = c.get(f"/api/person/{jake['id']}")
    assert {"aliases", "handles", "mentions", "observations", "last_contact",
            "contact_cards", "streams"} <= set(person)

    uuid = day["by_category"][0]["entries"][0]["uuid"]
    assert c.get(f"/api/entry/{uuid}")["uuid"] == uuid
    obs_uuid = day["observed"][0]["items"][0]["uuid"]
    assert c.get(f"/api/observation/{obs_uuid}")["uuid"] == obs_uuid
    assert {"open", "overdue", "recently_closed"} <= set(c.get("/api/loops"))
    assert {"imports", "unresolved_handles", "connectors", "health_files",
            "journal"} <= set(c.get("/api/imports"))
    assert {"start", "end", "metrics", "journal", "missing"} <= set(
        c.get("/api/health?days=14&end=2026-07-28"))


def test_ask_matches_the_cli_json_shape_and_cites_real_evidence(shared):
    cfg, c = shared
    r = c.get("/api/ask?q=" + "how did I sleep in July".replace(" ", "+"))
    conn = db_mod.connect(cfg.db_path, readonly=True)
    try:
        direct = recall.answer(conn, cfg, "how did I sleep in July", use_llm=False)
    finally:
        conn.close()
    assert r["answer"] == direct["answer"]
    assert r["evidence"]["window"] == direct["evidence"]["window"]
    assert r["answer_source"] == "deterministic"
    assert r["citations"]["cited"] and not r["citations"]["unknown"]
    assert set(r["citations"]["cited"]) <= set(r["index"])
    assert "[" not in r["speech"]                     # spoken without citations
    assert r["logged"] is False


def test_ask_through_a_local_model_has_its_citations_checked(web, monkeypatch):
    from lifecrm import llm
    cfg, start = web
    monkeypatch.setattr(llm, "status", lambda cfg: {
        "available": True, "reason": "", "model": "fake:1b", "models": ["fake:1b"]})
    monkeypatch.setattr(llm, "answer", lambda cfg, ev, q, model=None:
                        "You slept fine [M1], and ran a marathon [M9].")
    c = start(use_llm=True)
    r = c.get("/api/ask?q=how+did+I+sleep+in+July")
    assert r["answer_source"] == "model:fake:1b"
    assert r["citations"]["unknown"] == ["M9"]
    off = c.get("/api/ask?q=how+did+I+sleep+in+July&llm=0")
    assert off["answer_source"] == "deterministic"


def test_ask_passes_observed_evidence_through_when_recall_provides_it(web, monkeypatch):
    cfg, start = web
    real = recall.answer

    def with_observation(conn, cfg, question, **kw):
        out = real(conn, cfg, question, **kw)
        out["evidence"]["observations"] = [{
            "id": "O1", "tier": "observed", "observation_uuid": "u-1",
            "stream": "imessage", "date": "2026-07-28", "import_uuid": "abcdef0123"}]
        return out

    monkeypatch.setattr(recall, "answer", with_observation)
    r = start().get("/api/ask?q=veracruz")
    assert r["evidence"]["observations"][0]["id"] == "O1"
    assert "imessage" in r["index"]["O1"] and "abcdef01" in r["index"]["O1"]


def test_ask_refuses_an_empty_or_overlong_question(shared):
    _, c = shared
    assert "ask what" in c.get("/api/ask?q=", expect=400)["error"]
    assert "under 500" in c.get("/api/ask?q=" + "a" * 501, expect=400)["error"]
    assert "days must be" in c.get("/api/ask?q=sleep&days=0", expect=400)["error"]


# ---------------------------------------------------------------- pages

def test_day_view_groups_entries_by_category_and_observed_records_by_stream(shared):
    _, c = shared
    d = c.get("/api/day?date=2026-07-27")
    assert d["weekday"] == "Monday"
    assert "quarterly report" in d["summary"]
    assert (d["mood"], d["energy"], d["sleep_hours"]) == (7, 6, 6.5)
    cats = {g["category"]: len(g["entries"]) for g in d["by_category"]}
    assert cats == {"food": 3, "work": 1, "exercise": 1, "idea": 1}
    # time-of-day order inside a category: breakfast before dinner
    food = [e["title"] for e in next(g for g in d["by_category"]
                                     if g["category"] == "food")["entries"]]
    assert food[0].startswith("Breakfast") and food[-1].startswith("Dinner")
    bouldering = next(e for g in d["by_category"] for e in g["entries"]
                      if "Bouldering" in e["title"])
    assert {x["name"] for x in bouldering["entities"]} >= {"Jake", "Movement"}
    assert all(e["tier"] == "confirmed" for g in d["by_category"] for e in g["entries"])

    health = {m["metric"]: m for m in d["health"]}
    assert health["steps"]["value"] == 6000 and health["steps"]["origin"] == "sensor import"
    assert health["weight"]["value"] == 84.5          # 04:30 local -> the day before

    texts = next(g for g in d["observed"] if g["stream"] == "imessage")
    assert texts["count"] == 2
    first = texts["items"][0]
    assert first["tier"] == "observed" and first["import"]["uuid"]
    assert first["who"] == "Jake → you" and first["local_time"] == "2026-07-27 17:00"
    assert texts["items"][1]["who"] == "you → Jake"
    assert d["next"] == "2026-07-28" and d["prev"] == "2026-03-10"


def test_day_view_of_an_ai_chat_day_words_it_as_you_and_claude(shared):
    _, c = shared
    d = c.get("/api/day?date=2026-03-10")
    chat = next(g for g in d["observed"] if g["stream"] == "claude_chat")
    whos = {i["who"] for i in chat["items"]}
    assert whos == {"you → Claude", "Claude → you"}
    assert d["entries_count"] == 0 and d["by_category"] == []


def test_days_heat_map_is_dense_and_counts_both_tiers(shared):
    _, c = shared
    d = c.get("/api/days?start=2026-07-25&end=2026-07-29")
    assert [x["date"] for x in d["days"]] == ["2026-07-25", "2026-07-26", "2026-07-27",
                                              "2026-07-28", "2026-07-29"]
    by = {x["date"]: x for x in d["days"]}
    assert by["2026-07-27"]["entries"] == 6 and by["2026-07-27"]["observations"] == 2
    assert by["2026-07-27"]["mood"] == 7
    assert by["2026-07-28"]["health"] >= 1 and by["2026-07-25"]["entries"] == 0
    assert "before start" in c.get("/api/days?start=2026-07-29&end=2026-07-01",
                                   expect=400)["error"]
    assert "400 days or fewer" in c.get("/api/days?start=2020-01-01&end=2026-07-01",
                                        expect=400)["error"]


def test_person_view_spans_both_tiers_and_cites_each_observation(shared):
    _, c = shared
    jake = next(p for p in c.get("/api/people")["people"] if p["name"] == "Jake")
    p = c.get(f"/api/person/{jake['id']}")
    assert p["mention_count"] == 1 and p["mentions"][0]["title"].startswith("Bouldering")
    assert p["handles"][0]["value"] == JAKE and p["handles"][0]["display_name"] == "Jake Morales"
    assert len(p["observations"]) == 3
    assert all(o["tier"] == "observed" and o["import"]["uuid"] for o in p["observations"])
    # the text on the 28th is later than the journal mention on the 27th
    assert p["last_contact"]["tier"] == "observed"
    assert p["last_contact"]["date"] == "2026-07-28"
    assert p["last_in"]["text"].startswith("Veracruz")
    assert p["last_out"]["text"] == "on my way"
    assert {s["stream"]: s["count"] for s in p["streams"]} == {"imessage": 3}
    assert "does not exist" in c.get("/api/person/99999", expect=404)["error"]
    assert "must be a number" in c.get("/api/person/jake", expect=400)["error"]


def test_people_list_orders_by_last_contact_across_tiers(shared):
    _, c = shared
    people = c.get("/api/people")["people"]
    assert [p["name"] for p in people] == ["Jake", "Dana"]
    assert people[0]["last_contact"] == {"date": "2026-07-28", "tier": "observed"}
    assert people[1]["last_contact"] == {"date": "2026-07-27", "tier": "confirmed"}
    assert people[0]["handles"] == 1 and people[0]["observations"] == 3


def test_observation_view_carries_its_import_and_thread_context(shared):
    _, c = shared
    d = c.get("/api/day?date=2026-07-28")
    text = next(g for g in d["observed"] if g["stream"] == "imessage")["items"][0]
    o = c.get(f"/api/observation/{text['uuid']}")
    assert o["text"] == "Veracruz tonight? my favorite"
    assert o["import"]["connector"] == "ios_messages" and o["import"]["source_file"] == "sms.db"
    assert o["external_id"] == "t3" and o["detail"] == {"chat": "chat-jake"}
    assert o["local_time"] == "2026-07-28 12:00" and o["local_end"] is None
    assert [x["text"] for x in o["context"]] == [
        "Movement at 6? bring the chalk", "on my way", "Veracruz tonight? my favorite"]
    assert [x.get("current") for x in o["context"]] == [False, False, True]
    assert "does not exist" in c.get("/api/observation/nope", expect=404)["error"]


def test_entry_view_includes_the_transcript_passage(shared):
    _, c = shared
    d = c.get("/api/day?date=2026-07-27")
    breakfast = next(e for g in d["by_category"] for e in g["entries"]
                     if e["title"].startswith("Breakfast"))
    e = c.get(f"/api/entry/{breakfast['uuid']}")
    excerpt = e["provenance"][0]["transcript_excerpt"]
    assert "July 27th" in excerpt
    assert e["provenance"][0]["source_uuid"]
    assert e["categories"]["primary"] == "food" and e["tier"] == "confirmed"
    assert "does not exist" in c.get("/api/entry/not-a-uuid", expect=404)["error"]


def test_loops_put_overdue_first_and_list_recent_closures(web):
    from lifecrm.domain import corrections
    cfg, start = web
    conn = db_mod.connect(cfg.db_path)
    try:
        entries.create(conn, journal_date="2026-07-27", kind="plan",
                       title="Visit Mom", primary_category="travel")
        entries.create(conn, journal_date="2026-07-28", kind="task",
                       title="Renew passport", primary_category="chores",
                       due_at="2026-08-01")
        done = entries.create(conn, journal_date="2026-07-20", kind="task",
                              title="Book dentist", primary_category="health")
        corrections.close_entry(conn, done["uuid"], "done")
        conn.commit()
    finally:
        conn.close()
    d = start().get("/api/loops")
    assert [e["title"] for e in d["open"]] == ["Renew passport", "Visit Mom"]
    assert d["open"][0]["overdue"] is True and d["overdue"] == 1
    assert d["open"][1]["overdue"] is False
    assert [e["title"] for e in d["recently_closed"]] == ["Book dentist"]
    assert d["recently_closed"][0]["closure"] == "done"


def test_sources_page_reports_imports_handles_connectors_and_health_files(shared):
    _, c = shared
    d = c.get("/api/imports")
    assert {i["connector"] for i in d["imports"]} == {"ai_chat", "ios_messages"}
    assert all(i["status"] == "applied" and i["counts"]["added"] for i in d["imports"])
    # the owner's own phone and the Claude account are not linked to a person
    assert d["unresolved_handles"] == 2
    assert {u["kind"] for u in d["unresolved"]} == {"phone", "app_account"}
    conns = {c["connector"]: c for c in d["connectors"]}
    assert conns["ios_messages"]["records"] == 3
    assert conns["ios_messages"]["stale"] is True       # July data, 14-day limit
    assert conns["ai_chat"]["stale"] is None            # no freshness promise
    assert d["health_files"][0]["records"] == 8
    assert d["journal"]["entries"] == 6 and d["journal"]["days"] == 1


def test_health_series_are_the_ones_ask_cites(shared):
    cfg, c = shared
    d = c.get("/api/health?days=14&end=2026-07-28")
    assert (d["start"], d["end"]) == ("2026-07-15", "2026-07-28")
    steps = next(m for m in d["metrics"] if m["metric"] == "steps")
    conn = db_mod.connect(cfg.db_path, readonly=True)
    try:
        direct = recall._metric_series(
            conn, table="health_records", metric="steps",
            spec=recall.HEALTH_METRICS["steps"],
            window={"start": "2026-07-15", "end": "2026-07-28", "explicit": True})
    finally:
        conn.close()
    assert steps["series"] == direct["series"]
    assert [s["value"] for s in steps["series"]] == [6000, 8432]
    sleep = next(m for m in d["metrics"] if m["metric"] == "sleep_session")
    assert sleep["unit"] == "h" and sleep["latest"] == 6.5    # asleep stages only
    assert {j["metric"] for j in d["journal"]} >= {"mood", "energy", "sleep_hours"}
    late = c.get("/api/health?days=14&end=2026-10-01")
    assert late["metrics"] == [] and {m["metric"] for m in late["missing"]} >= {"steps"}


def test_status_offers_persona_questions_only_for_the_demo(web):
    from lifecrm.demo import persona
    cfg, start = web
    c = start()
    generic = c.get("/api/status")
    assert generic["demo"] is False
    assert "when did I last see Dana" in generic["examples"]   # a person you mention
    conn = db_mod.connect(cfg.db_path)
    try:
        for p in persona.PEOPLE[:3]:
            entities.create(conn, "person", p.name)
        conn.commit()
    finally:
        conn.close()
    demo = c.get("/api/status")
    assert demo["demo"] is True
    assert demo["examples"] == list(persona.QUESTIONS)


# ---------------------------------------------------------------- the live database

def test_the_server_never_writes_the_live_database(web):
    cfg, start = web
    before = _db_hash(cfg)
    c = start(log_questions=False)
    for path in ("/api/status", "/api/ask?q=sleep", "/api/day?date=2026-07-27",
                 "/api/days", "/api/people", "/api/loops", "/api/imports",
                 "/api/health", "/"):
        c.get(path)
    assert _db_hash(cfg) == before
    assert _live(cfg, "SELECT COUNT(*) AS c FROM recall_log")[0]["c"] == 0


def test_questions_are_logged_to_recall_log_when_enabled(web):
    from lifecrm.doctor import run_checks
    cfg, start = web
    r = start(log_questions=True).get("/api/ask?q=how+did+I+sleep+in+July")
    assert r["logged"] is True
    rows = _live(cfg, "SELECT question, answered, answer_summary FROM recall_log")
    assert rows == [{"question": "how did I sleep in July", "answered": 1,
                     "answer_summary": r["answer"][:500]}]
    conn = db_mod.connect(cfg.db_path)
    try:
        report = run_checks(conn, cfg)
        conn.rollback()
    finally:
        conn.close()
    assert report["failures"] == 0, [x for x in report["checks"] if x["status"] == "fail"]


def test_the_live_database_is_not_held_open_between_requests(web):
    cfg, start = web
    c = start()
    c.get("/api/people")
    c.get("/api/day?date=2026-07-27")
    # On Windows a file with an open handle cannot be renamed: restore's
    # file swap depends on this.
    moved = cfg.db_path.with_name("moved.db")
    os.replace(cfg.db_path, moved)
    os.replace(moved, cfg.db_path)
    assert (cfg.data_dir / "web" / "lifecrm.db").exists()
    assert c.get("/api/people")["people"]           # and it keeps serving


def test_the_snapshot_follows_a_live_change(web):
    cfg, start = web
    c = start(refresh_seconds=0)
    assert c.get("/api/loops")["open"] == []
    conn = db_mod.connect(cfg.db_path)
    try:
        entries.create(conn, journal_date="2026-07-28", kind="task",
                       title="Call the landlord", primary_category="chores")
        conn.commit()
    finally:
        conn.close()
    assert [e["title"] for e in c.get("/api/loops")["open"]] == ["Call the landlord"]
    assert c.server.app.snapshot.refreshes >= 1


def test_the_snapshot_is_not_retaken_when_nothing_changed(web):
    _, start = web
    c = start(refresh_seconds=0)
    for _ in range(4):
        c.get("/api/people")
    assert c.server.app.snapshot.refreshes == 0


def test_a_refresh_waits_for_readers_of_the_old_snapshot(seeded, tmp_path):
    cfg = copy_root(seeded, tmp_path)
    snap = web_server.Snapshot(cfg.db_path, cfg.data_dir / "web" / "lifecrm.db",
                               refresh_seconds=0)
    snap.take()
    with snap.reader() as held:
        conn = db_mod.connect(cfg.db_path)
        try:
            entries.create(conn, journal_date="2026-07-28", kind="task",
                           title="Water the plants", primary_category="chores")
            conn.commit()
        finally:
            conn.close()
        with snap.reader() as second:            # sees the old copy, never blocks
            assert second.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 6
        assert held.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 6
    with snap.reader() as fresh:                 # swapped in once readers left
        assert fresh.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 7
    snap.close()
    assert not list(snap.path.parent.glob("lifecrm.db.next-*"))


# ---------------------------------------------------------------- access

def test_a_token_is_required_and_remembered_by_a_cookie(web):
    _, start = web
    c = start(token="s3cret")
    status, _, data = c.request("GET", "/api/status")
    assert status == 401 and "key" in json.loads(data)["error"]
    assert c.request("GET", "/api/status?k=wrong")[0] == 401
    assert c.request("GET", "/")[0] == 401
    status, msg, _ = c.request("GET", "/api/status?k=s3cret")
    assert status == 200
    cookie = msg.get("Set-Cookie")
    assert cookie.startswith("lifecrm_k=s3cret") and "HttpOnly" in cookie
    assert c.request("GET", "/api/people",
                     headers={"Cookie": "lifecrm_k=s3cret"})[0] == 200
    assert c.request("GET", "/api/people",
                     headers={"Cookie": "lifecrm_k=nope"})[0] == 401


def test_manifest_and_icons_are_public_but_never_leak_the_token(web):
    _, start = web
    c = start(token="s3cret")
    public = c.get("/manifest.webmanifest")
    assert public["start_url"] == "/" and public["display"] == "standalone"
    keyed = c.get("/manifest.webmanifest?k=s3cret")
    assert keyed["start_url"] == "/?k=s3cret"
    for path in ("/icon.svg", "/icon-192.png", "/apple-touch-icon.png"):
        assert c.request("GET", path)[0] == 200
    page = c.get("/?k=s3cret").decode("utf-8")
    assert 'href="/manifest.webmanifest?k=s3cret"' in page


def test_cross_site_and_foreign_host_requests_are_refused(shared):
    _, c = shared
    status, _, data = c.request("GET", "/api/people",
                                headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403 and "cross-site" in json.loads(data)["error"]
    assert c.request("GET", "/api/people",
                     headers={"Sec-Fetch-Site": "same-origin"})[0] == 200
    # a page on another site may still link to the page itself
    assert c.request("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0] == 200
    status, _, data = c.request("GET", "/api/people",
                                headers={"Host": "evil.example:8765"})
    assert status == 403 and "allowed_hosts" in json.loads(data)["error"]
    for ok in ("localhost:8765", "192.168.1.23:8765", "alexs-macbook.local:8765",
               "[::1]:8765"):
        assert c.request("GET", "/api/status", headers={"Host": ok})[0] == 200, ok


def test_errors_are_json_with_a_message(shared):
    _, c = shared
    for path, code, fragment in (
            ("/api/nope", 404, "no such endpoint"),
            ("/api/day?date=yesterday", 400, "YYYY-MM-DD"),
            ("/api/health?days=0", 400, "days must be"),
            ("/api/health?days=x", 400, "whole number"),
            ("/api/entry/missing", 404, "does not exist")):
        status, msg, data = c.request("GET", path)
        assert status == code, path
        assert msg.get("Content-Type").startswith("application/json")
        assert fragment in json.loads(data)["error"], path
    status, _, data = c.request("POST", "/api/ask?q=x")
    assert status == 405 and "GET only" in json.loads(data)["error"]


def test_the_page_is_self_contained_with_no_external_urls(shared):
    _, c = shared
    status, msg, data = c.request("GET", "/")
    assert status == 200 and msg.get("Content-Type").startswith("text/html")
    html = data.decode("utf-8")
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", html))
    assert urls <= {"http://www.w3.org/2000/svg"}, urls
    assert "connect-src 'self'" in msg.get("Content-Security-Policy")
    assert "default-src 'none'" in msg.get("Content-Security-Policy")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in html
    assert 'rel="apple-touch-icon"' in html and "prefers-color-scheme: dark" in html
    assert "{{" not in html


# ---------------------------------------------------------------- voice

def test_transcribe_answers_503_when_whisper_is_unavailable(web, monkeypatch):
    from lifecrm import transcribe

    def missing(cfg, model_name=None):
        raise ImportError("No module named 'faster_whisper'")

    monkeypatch.setattr(transcribe, "load_model", missing)
    _, start = web
    status, _, data = start().request("POST", "/api/transcribe", body=b"\x1a\x45" * 600,
                                      headers={"Content-Type": "audio/webm"})
    assert status == 503
    assert "faster-whisper" in json.loads(data)["error"]


def test_transcribe_returns_the_text_and_keeps_no_audio(web, monkeypatch):
    from lifecrm import transcribe
    loaded = []

    class Segment:
        def __init__(self, text):
            self.text, self.start, self.end = text, 0.0, 1.0

    class Info:
        duration, language = 1.5, "en"

    class FakeModel:
        def transcribe(self, path, **kw):
            assert os.path.exists(path) and path.endswith(".webm")
            assert "Jake" in (kw.get("initial_prompt") or "")
            return iter([Segment(" how did I sleep "), Segment("last week")]), Info()

    def load(cfg, model_name=None):
        loaded.append(model_name)
        return FakeModel()

    monkeypatch.setattr(transcribe, "load_model", load)
    cfg, start = web
    c = start()
    for _ in range(2):
        status, _, data = c.request("POST", "/api/transcribe", body=b"\x1a\x45" * 600,
                                    headers={"Content-Type": "audio/webm;codecs=opus"})
        assert status == 200, data
        assert json.loads(data)["text"] == "how did I sleep last week"
    assert loaded == ["small"]                    # [web] model default, loaded once
    assert not list((cfg.data_dir / "web" / "voice").iterdir())
    assert "request body" in json.loads(c.request("POST", "/api/transcribe")[2])["error"]


# ---------------------------------------------------------------- icons, banner, command

def test_icons_are_valid_pngs_of_the_declared_sizes():
    for size in (180, 192):
        data = icons.png(size)
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", data[16:24])
        assert (width, height) == (size, size)
    assert icons.SVG.startswith("<svg")


def test_lan_ip_and_the_banner_warn_only_off_loopback(tmp_path):
    cfg = load_config(cli_root(tmp_path))
    ip = web_server.lan_ip()
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip)
    local = web_server.banner(cfg, "127.0.0.1", 8765, None, True)
    assert not any(line.startswith("warning") for line in local)
    lan = web_server.banner(cfg, "0.0.0.0", 8765, "abc", False)
    assert any(f"http://{ip}:8765/?k=abc" in line for line in lan)
    assert any(line.startswith("warning: no login") for line in lan)


def cli_root(tmp_path):
    root = tmp_path / "cfgroot"
    root.mkdir()
    (root / "config.toml").write_text('[paths]\ndata_dir = "data"\n', encoding="utf-8")
    return root


def test_serve_command_prints_the_lan_url_and_the_warning(tmp_path, monkeypatch):
    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0

    def stop(self, *a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(web_server.Server, "serve_forever", stop)
    r = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--port", "0", "--no-log",
                            "--token", "random"])
    assert r.exit_code == 0, r.output
    assert "on your phone (same Wi-Fi): http://" in r.output
    assert "warning: no login" in r.output and "?k=" in r.output
    assert "questions are not logged" in r.output and "stopped" in r.output
    assert (tmp_path / "data" / "web" / "lifecrm.db").exists()


def test_serve_command_refuses_a_database_behind_the_code(tmp_path, monkeypatch):
    from conftest import init_v1_db
    from lifecrm.cli import app
    cli_project(tmp_path, monkeypatch)
    (tmp_path / "data").mkdir()
    init_v1_db(tmp_path / "data" / "lifecrm.db").close()
    r = CliRunner().invoke(app, ["serve", "--port", "0"])
    assert r.exit_code == 1 and "migrate" in r.output


def test_web_settings_come_from_the_web_section(tmp_path):
    root = cli_root(tmp_path)
    with open(root / "config.toml", "a", encoding="utf-8") as f:
        f.write('[web]\nlog_questions = "false"\nmodel = "base"\n'
                'use_llm = false\nrefresh_seconds = 7\n')
    cfg = load_config(root)
    app = web_server.WebApp(cfg)
    assert app.log_questions is False and app.use_llm is False
    assert app.whisper_model == "base" and app.snapshot.refresh_seconds == 7
    assert api.speakable("Slept 7 h [M1]; tacos «Jake» [E2, O3].") == "Slept 7 h; tacos Jake."

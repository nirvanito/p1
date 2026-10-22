"""The front ends agree with `ask` and stay safe with imported text.

Seven defects, each pinned by the case that showed it:

- two apps recording one day's steps, sleep, food and workouts (Samsung
  Health + Google Fit, an iPhone + a Watch) are never added up — the day view,
  the Obsidian health line and the Health dashboards give the number `ask`
  and the Health page give;
- "last contact" is never next week's calendar event, a missed call, a
  tapback or a contact card;
- a group message Jake sent is not "Sam → you";
- a stranger's invite title or text cannot put an image, a link, a tag or a
  comment into the vault;
- a key of your own with '+', '&', '#', ';' or a space still opens the page;
- the owner is not one of their own contacts;
- the Health page offers the full dashboards only where they would open.
"""

import json
import re
import socket
import threading
import http.client
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import recall
from lifecrm.config import load_config
from lifecrm.connectors import base
from lifecrm.domain import entities, health
from lifecrm.domain import observations as obs
from lifecrm.domain.util import now_utc
from lifecrm.export import _callout, _code, _health_line, build_vault
from lifecrm.web import api
from lifecrm.web import server as web_server
from test_dashboard import _chart, run

SHEALTH = "com.sec.android.app.shealth"
FIT = "com.google.android.apps.fitness"
DAY = "2026-10-20"

ME = "+15125550100"
SAM = "+15125550166"
PRIYA = "+15125550123"
JAKE = "+15125550142"
MAYA = "+15125550188"
STRANGER = "prizes@attacker.example"


# ---------------------------------------------------------------- health

def _two_writers(conn) -> None:
    """Samsung Health and Google Fit both recording DAY: the same walk, night,
    lunch and run — and Fit also caught a short evening walk Samsung missed."""
    for app in (SHEALTH, FIT):
        for metric, value, unit in (("steps", 8000.0, "count"),
                                    ("sleep_session", 450.0, "min"),
                                    ("nutrition", 2100.0, "kcal"),
                                    ("exercise_session", 45.0, "min")):
            health.insert(conn, {"metric": metric, "journal_date": DAY,
                                 "started_at": f"{DAY}T14:00:00Z", "value": value,
                                 "unit": unit, "external_id": f"{app}-{metric}",
                                 "source_file": "hc.zip", "origin_app": app})
    health.insert(conn, {"metric": "steps", "journal_date": DAY,
                         "started_at": f"{DAY}T23:00:00Z", "value": 500.0,
                         "unit": "count", "external_id": "fit-evening-walk",
                         "source_file": "hc.zip", "origin_app": FIT})
    conn.commit()


def test_the_day_view_takes_the_busier_app_like_ask_and_the_health_page(conn, cfg):
    _two_writers(conn)
    tiles = {t["metric"]: t for t in api.day(conn, cfg, DAY)["health"]}
    assert tiles["steps"]["value"] == 8500                    # not 16,500
    assert tiles["steps"]["origin_app"] == FIT and tiles["steps"]["records"] == 2
    assert tiles["sleep_session"]["value"] == 7.5             # not 15 h
    assert tiles["nutrition"]["value"] == 2100
    assert (tiles["exercise_session"]["value"], tiles["exercise_session"]["records"]) \
        == (45, 1)
    assert tiles["steps"]["source_files"] == ["hc.zip"]

    page = {m["metric"]: m for m in api.health(conn, cfg, days=1, end=DAY)["metrics"]}
    for metric in ("steps", "sleep_session", "nutrition", "exercise_session"):
        asked = recall._metric_series(
            conn, table="health_records", metric=metric,
            spec=recall.HEALTH_METRICS[metric],
            window={"start": DAY, "end": DAY, "explicit": True})
        assert page[metric]["series"][0]["value"] == asked["series"][0]["value"] \
            == tiles[metric]["value"], metric


def test_the_obsidian_health_line_never_adds_two_apps_up(conn, cfg):
    _two_writers(conn)
    line = _health_line(conn, DAY)
    assert "sleep 7.5 h · 8,500 steps · 1 workout (45 min)" in line, line


def test_the_health_dashboards_take_the_busier_app_per_day(conn):
    _two_writers(conn)

    def rows(name):
        return run(conn, _chart(f"health.{name}"), {})[1]

    assert rows("steps-per-day") == [{"day": DAY, "steps": 8500}]
    assert rows("sleep-per-night") == [{"night": DAY, "hours": 7.5}]
    assert rows("kcal-per-day") == [{"day": DAY, "kcal": 2100.0}]
    assert rows("last-night-sleep") == [{"hours": "7.5 h"}]
    assert rows("workouts-count") == [{"sessions": 1}]
    # one app alone is still summed across its own records
    filtered = run(conn, _chart("health.steps-per-day"),
                   {"date_start": DAY, "date_end": DAY})[1]
    assert filtered == [{"day": DAY, "steps": 8500}]


# ---------------------------------------------------------------- people

def _rec(stream, ext, day, *, direction=None, who=(), body=None, title=None,
         value=None, detail=None, at="17:00:00"):
    return {"stream": stream, "external_id": ext, "journal_date": day,
            "started_at": f"{day}T{at}Z", "direction": direction, "body": body,
            "title": title, "value": value, "unit": "s" if value is not None else None,
            "value_json": json.dumps(detail) if detail else None,
            "participants": [{"kind": k, "value": v, "role": role}
                             for (k, v), role in who]}


def _phone(v):
    return ("phone", v)


@pytest.fixture
def circle(conn, cfg):
    """The owner, Sam and Priya, with texts, a group message Jake sent, a
    missed call, a tapback, a contact card, and next week's meeting — dated
    around today, so "after today" means what it says."""
    t = recall.local_today(cfg)

    def d(n):
        return (t + timedelta(days=n)).isoformat()

    me_in = (_phone(ME), "recipient")
    records = [
        _rec("android_sms", "s1", d(-5), direction="in", body="Back at it?",
             who=((_phone(SAM), "sender"), me_in)),
        _rec("android_sms", "s2", d(-5), direction="out", body="on my way",
             at="17:05:00", who=((_phone(ME), "sender"), (_phone(SAM), "recipient"))),
        _rec("android_mms", "g1", d(-1), direction="in",
             body="Booking the lakeside place, 4 of us",
             who=((_phone(JAKE), "sender"), me_in, (_phone(SAM), "recipient"),
                  (_phone(MAYA), "recipient"))),
        _rec("calendar_event", "c1", d(3), title="New climbing gym w/ Sam",
             who=((("email", "alex@example.com"), "organizer"),
                  (_phone(SAM), "participant"))),
        _rec("android_sms", "p1", d(-10), direction="in",
             body="The board loved it. Nice work.",
             who=((_phone(PRIYA), "sender"), me_in)),
        _rec("android_call", "p2", d(-2), direction="in", value=0,
             detail={"call": "missed"}, who=((_phone(PRIYA), "sender"), me_in)),
        _rec("text_reaction", "p3", d(-1), direction="in",
             detail={"reaction": "love", "target": "p1"},
             who=((_phone(PRIYA), "sender"), me_in)),
        _rec("contact_card", "p4", d(-1), title="Priya Natarajan",
             who=((_phone(PRIYA), "participant"),)),
    ]
    named = sorted({(p["kind"], p["value"]) for r in records for p in r["participants"]})
    scan = base.empty_scan(
        "test_feed", account=ME, source_file="feed.json", records=records,
        handles=[{"kind": k, "value": v,
                  "display_name": "you (this phone)" if v == ME else None}
                 for k, v in named])
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    ids = {}
    for name, number in (("Alex Rivera", ME), ("Sam Okafor", SAM),
                         ("Priya Natarajan", PRIYA)):
        ids[name] = entities.create(conn, "person", name)["id"]
        obs.link_handle(conn, "phone", number, ids[name])
    conn.commit()
    return {"ids": ids, "d": d}


def _asked(conn, cfg, entity_id) -> str:
    """The date `ask` gives for 'when did I last talk to X'."""
    items = recall.last_contact(conn, cfg, [entity_id], {}, recall.local_today(cfg))
    return max(i["date"] for i in items)


def test_last_contact_is_never_an_upcoming_event_a_missed_call_or_a_tapback(
        conn, cfg, circle):
    d, ids = circle["d"], circle["ids"]
    people = {p["name"]: p for p in api.people(conn, cfg)["people"]}
    # Sam's newest record is next week's meeting; Priya's a tapback, a contact
    # card and a missed call. None of them is contact.
    # Jake's group message (d-1) is not Sam texting you: the 1:1 texts (d-5) are.
    assert people["Sam Okafor"]["last_contact"] == {"date": d(-5), "tier": "observed"}
    assert people["Priya Natarajan"]["last_contact"] == {"date": d(-10),
                                                         "tier": "observed"}
    for name in ("Sam Okafor", "Priya Natarajan"):
        assert people[name]["last_contact"]["date"] == _asked(conn, cfg, ids[name])
        page = api.person(conn, cfg, ids[name])
        assert page["last_contact"]["date"] == _asked(conn, cfg, ids[name])
        assert all(o["date"] <= d(0) for o in page["observations"])

    sam = api.person(conn, cfg, ids["Sam Okafor"])
    assert [(o["date"], o["title"]) for o in sam["upcoming"]] == [
        (d(3), "New climbing gym w/ Sam")]
    priya = api.person(conn, cfg, ids["Priya Natarajan"])
    assert priya["last_in"]["text"] == "The board loved it. Nice work."   # not the missed call


def test_them_to_you_is_a_message_they_sent_not_a_group_message_they_were_in(
        conn, cfg, circle):
    d, ids = circle["d"], circle["ids"]
    sam = api.person(conn, cfg, ids["Sam Okafor"])
    # Jake's group message to Sam, Maya and the owner is newer, but Jake sent it
    assert (sam["last_in"]["date"], sam["last_in"]["text"]) == (d(-5), "Back at it?")
    assert sam["last_in"]["who"] == "Sam Okafor → you"
    assert (sam["last_out"]["date"], sam["last_out"]["text"]) == (d(-5), "on my way")


def test_the_owner_is_not_listed_as_one_of_their_own_contacts(conn, cfg, circle):
    ids = circle["ids"]
    names = [p["name"] for p in api.people(conn, cfg)["people"]]
    assert "Alex Rivera" not in names
    assert names[:2] == ["Sam Okafor", "Priya Natarajan"]
    me = api.person(conn, cfg, ids["Alex Rivera"])
    assert me["is_owner"] is True
    assert (me["last_contact"], me["last_in"], me["last_out"]) == (None, None, None)
    assert api.person(conn, cfg, ids["Sam Okafor"])["is_owner"] is False


def test_the_people_dashboard_counts_no_upcoming_event_or_missed_call(conn, circle):
    d = circle["d"]
    rows = {r["person"]: r for r in run(conn, _chart("people.last-contact"), {})[1]}
    assert (rows["Sam Okafor"]["last_contact"], rows["Sam Okafor"]["via"]) == (
        d(-5), "android_sms")
    assert (rows["Priya Natarajan"]["last_contact"],
            rows["Priya Natarajan"]["via"]) == (d(-10), "android_sms")
    assert "Alex Rivera" not in rows
    weeks = run(conn, _chart("people.contacts-per-week"), {})[1]
    assert all(r["week"] <= d(0) for r in weeks)


def test_obsidian_last_seen_is_never_a_contact_card_or_an_upcoming_event(
        conn, cfg, circle):
    d = circle["d"]
    vault = build_vault(conn, tz_name="America/Chicago")
    sam = vault["People/Sam Okafor.md"]
    assert f"\nlast_seen: {d(-1)}\n" in sam
    priya = vault["People/Priya Natarajan.md"]
    assert f"\nlast_seen: {d(-10)}\n" in priya
    latest = priya.split("## Latest contact", 1)[1].split("##", 1)[0]
    assert "The board loved it" in latest and "contact_card" not in latest
    index = vault["Index.md"]
    assert f"[[Sam Okafor]] — last seen [[{d(-1)}]]" in index
    assert f"[[Priya Natarajan]] — last seen [[{d(-10)}]]" in index
    # still listed among the days the person appears in imported data
    assert f"- [[{d(3)}]] — calendar_event 1" in sam
    assert build_vault(conn, tz_name="America/Chicago") == vault     # deterministic


# ---------------------------------------------------------------- imported text in the vault

# Short enough to survive the digest's cut whole.
INJECT = "![](https://a.example/c.png) <img src=x> #urgent [[Index]] %%hide"


def test_imported_text_cannot_add_images_links_tags_or_comments_to_the_vault(conn, cfg):
    jake = entities.create(conn, "person", "Jake Morales")["id"]
    records = [
        # an invite from a stranger: no linked handle needed to reach the vault
        _rec("calendar_event", "spam", DAY, title="You won! " + INJECT, at="17:00:00",
             who=((("email", STRANGER), "organizer"),)),
        _rec("android_sms", "x1", DAY, direction="in", body="see " + INJECT,
             at="18:00:00", who=((_phone(JAKE), "sender"), (_phone(ME), "recipient"))),
        _rec("note", "n1", DAY, title="==mark== $x$ ~~gone~~ `tick | a^b *em*",
             at="19:00:00"),
        _rec("place_visit", "v1", DAY, title="Café ![](https://a.example/p.png)",
             at="20:00:00"),
    ]
    scan = base.empty_scan(
        "test_feed", account=ME, source_file="feed.json", records=records,
        handles=[{"kind": "phone", "value": JAKE,
                  "display_name": "Jake <img src=https://attacker.example/h.gif>"},
                 {"kind": "phone", "value": ME, "display_name": "you (this phone)"},
                 {"kind": "email", "value": STRANGER, "display_name": None}])
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    obs.link_handle(conn, "phone", JAKE, jake)
    conn.commit()
    vault = build_vault(conn, tz_name="America/Chicago")
    day = vault[f"Journal/2026/{DAY}.md"]
    jake_note = vault["People/Jake Morales.md"]
    for rel, note in ((DAY, day), ("Jake", jake_note)):
        assert "![](" not in note, rel
        for live in (r"<img", r"#urgent"):
            assert not re.search(r"(?<!\\)" + live, note), (rel, live)
        assert "[[Index]] %" not in note and "%%hide" not in note, rel
    # the day's own navigation link is the only live [[Index]]
    assert day.count("[[Index]]") == 1 and "\n[[Index]]\n" in day
    # the words are all still there, only inert
    shown = ("!\\[\\](https://a.example/c.png) \\<img src=x\\> \\#urgent "
             "\\[\\[Index\\]\\] \\%\\%hide")
    assert f"- 12:00 You won! {shown} — observed `" in day
    assert f"latest 13:00: “see {shown}”" in day
    assert "note: \\=\\=mark\\=\\= \\$x\\$ \\~\\~gone\\~\\~ \\`tick \\| a\\^b \\*em\\*" in day
    assert "at Café !\\[\\](https://a.example/p.png) — observed" in day
    assert f"“see {shown}”" in jake_note                   # his latest contact
    assert "the source calls it “Jake \\<img src=https://attacker.example/h.gif\\>”" \
        in jake_note


def test_an_observed_callout_is_escaped_and_a_code_span_cannot_be_closed_early():
    lines = _callout("Observed · imessage", "hi ![](https://x/y.png)\n<img src=x> [[Index]]",
                     imported=True)
    assert lines == ["> [!quote] Observed · imessage",
                     "> hi !\\[\\](https://x/y.png)",
                     "> \\<img src=x\\> \\[\\[Index\\]\\]"]
    # the owner's own transcript is quoted as spoken
    assert _callout("Source 1", "I said [this]")[1] == "> I said [this]"
    assert _code("+15125550142") == "`+15125550142`"
    assert _code("a`b") == "`` a`b ``"
    assert _code("x\n# heading") == "`x # heading`"


# ---------------------------------------------------------------- the key in links

def _root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.toml").write_text('[paths]\ndata_dir = "data"\n', encoding="utf-8")
    cfg = load_config(root)
    db_mod.init_db(cfg.db_path, now_utc())
    return cfg


def _serve(cfg, **opts):
    opts.setdefault("log_questions", False)
    opts.setdefault("use_llm", False)
    opts.setdefault("refresh_seconds", 0)
    server = web_server.create_server(cfg, "127.0.0.1", 0, **opts)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(server, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
    try:
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.msg, r.read()
    finally:
        c.close()


KEY = "a+b c&d#e;f"


def test_a_key_with_reserved_characters_prints_a_link_that_opens(tmp_path):
    cfg = _root(tmp_path)
    lines = web_server.banner(cfg, "0.0.0.0", 8765, KEY, False)
    printed = next(ln for ln in lines if ln.startswith("open: ")).split(" ", 1)[1]
    assert printed == "http://127.0.0.1:8765/?k=a%2Bb%20c%26d%23e%3Bf"
    assert parse_qs(urlsplit(printed).query)["k"] == [KEY]

    server = _serve(cfg, token=KEY)
    try:
        query = urlsplit(printed).query
        status, msg, _ = _get(server, "/api/status?" + query)
        assert status == 200
        cookie = msg.get("Set-Cookie").split(";", 1)[0]
        assert cookie == "lifecrm_k=a%2Bb%20c%26d%23e%3Bf"
        assert _get(server, "/api/people", {"Cookie": cookie})[0] == 200
        assert _get(server, "/api/people", {"Cookie": "lifecrm_k=a+b"})[0] == 401
        assert _get(server, "/api/status?k=a+b")[0] == 401          # what it printed before
        start = json.loads(_get(server, "/manifest.webmanifest?" + query)[2])["start_url"]
        assert parse_qs(urlsplit(start).query)["k"] == [KEY]
        page = _get(server, "/?" + query)[2].decode("utf-8")
        assert 'href="/manifest.webmanifest?k=a%2Bb%20c%26d%23e%3Bf"' in page
    finally:
        server.shutdown()
        server.server_close()


def test_serve_open_opens_the_encoded_link(tmp_path, monkeypatch):
    from lifecrm.cli import app
    cli_project(tmp_path, monkeypatch)
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url, *a, **kw: opened.append(url))

    def stop(self, *a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(web_server.Server, "serve_forever", stop)
    r = runner.invoke(app, ["serve", "--port", "0", "--no-log", "--token", "a+b",
                            "--open"])
    assert r.exit_code == 0, r.output
    (url,) = opened
    assert url.endswith("/?k=a%2Bb") and "open: " + url in r.output


# ---------------------------------------------------------------- the dashboards link

def test_the_dashboards_link_is_offered_only_where_it_would_open(tmp_path):
    lan = web_server.lan_ip()
    if web_server.is_loopback(lan):
        pytest.skip("no LAN address on this machine")
    cfg = _root(tmp_path)
    # Datasette started the default way: listening on this computer's loopback
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    server = _serve(cfg)
    try:
        server.app.dashboard_port = listener.getsockname()[1]

        def dashboards(host):
            status, _, data = _get(server, "/api/status", {"Host": host})
            assert status == 200
            return json.loads(data)["dashboards"]

        assert dashboards("localhost:8765") == {
            "port": server.app.dashboard_port, "running": True, "local_only": False}
        # the phone: its page is at the laptop's LAN address, where nothing listens
        assert dashboards(f"{lan}:8765") == {
            "port": server.app.dashboard_port, "running": False, "local_only": True}
    finally:
        server.shutdown()
        server.server_close()
        listener.close()


def test_with_no_dashboards_running_there_is_no_link_and_no_hint(tmp_path):
    cfg = _root(tmp_path)
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    app = web_server.WebApp(cfg)
    app.dashboard_port = port
    assert app.dashboards_state("127.0.0.1") == {"port": port, "running": False,
                                                 "local_only": False}

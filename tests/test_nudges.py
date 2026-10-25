"""`lifecrm nudges`: what matters today, recomputed from the records and cited.

The load-bearing claims: each kind fires from seeded data in a fixed order
and names the rows it rests on; the owner is never nudged about themself;
snooze and dismiss hide by durable key and the pack id changes with any
content; a brief is checked against the saved pack; an N# resolves for a
write only through that pack; building a pack writes nothing.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import nudges as mod
from lifecrm.cli import app
from lifecrm.config import Config, load_config
from lifecrm.domain import corrections, entities, entries
from lifecrm.domain import nudges as state
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError, canonical_json

runner = CliRunner()

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)
OWNER = "+15125550100"
SAM = "+15125550155"
MOM = "+12105550133"
PRIYA = "priya@acme-robotics.example"


# ---------------------------------------------------------------- seeding

def _rec(n, stream, day, hhmm, parts=(), **kw):
    rec = {"stream": stream, "external_id": f"n-{n:03d}", "journal_date": day,
           "started_at": f"{day}T{hhmm}:00Z", "direction": None,
           "participants": [{"kind": k, "value": v, "role": r} for k, v, r in parts]}
    rec.update(kw)
    return rec


def _card(n, name, phone, birthday, day="2026-10-01"):
    return _rec(n, "contact_card", day, "12:00", [("phone", phone, "participant")],
                title=name, body=f"{name}\nBirthday: {birthday}",
                value_json=canonical_json({"birthday": birthday,
                                           "phones": [{"label": "mobile",
                                                       "number": phone}]}))


def _nutrition_day(n, day, vitamin_d, magnesium):
    return _rec(n, "nutrition_day", day, "23:00", [], value=2050.0, unit="kcal",
                title=f"Cronometer {day}", origin_app="Cronometer",
                value_json=canonical_json({"completed": True, "nutrients": {
                    "vitamin_d": [vitamin_d, "µg"], "magnesium": [magnesium, "mg"]}}))


def seed(conn, cfg) -> dict:
    flights = entries.create(conn, journal_date="2026-10-19", kind="task",
                             title="Book flights to Ljubljana", primary_category="travel",
                             due_at="2026-10-24")
    passport = entries.create(conn, journal_date="2026-10-01", kind="task",
                              title="Renew passport", primary_category="chores",
                              due_at="2026-10-18")
    entries.create(conn, journal_date="2026-10-02", kind="plan", title="Visit Mom",
                   primary_category="social", due_at="2026-11-15")      # too far out
    done = entries.create(conn, journal_date="2026-10-10", kind="task", title="Dentist",
                          primary_category="health", due_at="2026-10-22")
    corrections.close_entry(conn, done["uuid"], "done")              # closed: no nudge
    entries.create(conn, journal_date="2026-10-19", kind="reflection",
                   title="Weekly review", primary_category="reflection",
                   data={"derived_from": "lifecrm review"})
    records = [
        _card(1, "Sam Okafor", SAM, "1992-10-25"),                   # in 4 days
        _card(2, "Linda Rivera", MOM, "--11-30"),                     # 40 days: out
        _card(3, "Alex Rivera", OWNER, "1994-10-23"),                 # the owner: never
        _rec(4, "calendar_event", "2026-10-21", "15:00",
             [("email", PRIYA, "organizer"), ("phone", OWNER, "participant")],
             title="1:1 Alex / Priya", ended_at="2026-10-21T15:30:00Z",
             value_json=canonical_json({"calendar": "work", "all_day": False})),
        _rec(5, "calendar_event", "2026-10-22", "05:00", [], title="Launch week",
             ended_at="2026-10-23T05:00:00Z",
             value_json=canonical_json({"calendar": "work", "all_day": True})),
        _rec(6, "calendar_event", "2026-10-22", "18:00", [], title="Dentist (moved)",
             ended_at="2026-10-22T19:00:00Z",
             value_json=canonical_json({"calendar": "personal", "all_day": False,
                                        "status": "cancelled"})),
        _rec(7, "calendar_event", "2026-10-25", "18:00", [], title="Next week",
             ended_at="2026-10-25T19:00:00Z"),                         # not the agenda
        _rec(8, "android_sms", "2026-09-01", "12:00",
             [("phone", SAM, "sender"), ("phone", OWNER, "recipient")],
             direction="in", body="old text"),                        # 50 days: stale
        _nutrition_day(9, "2026-10-15", 2.0, 450.0),
        _nutrition_day(10, "2026-10-16", 1.5, 440.0),
        _nutrition_day(11, "2026-10-17", 2.5, 460.0),
    ]
    handles = [
        {"kind": "phone", "value": OWNER, "display_name": "you (this phone)"},
        {"kind": "phone", "value": SAM, "display_name": "Sam Okafor"},
        {"kind": "phone", "value": MOM, "display_name": "Linda Rivera"},
        {"kind": "email", "value": PRIYA, "display_name": "Priya Natarajan"},
    ]
    scan = {"connector": "nudge_fixture", "account": OWNER, "source_file": "fixture",
            "source_hash": "a" * 64, "records": records, "handles": handles}
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    people = {}
    for name, kind, value in (("Alex Rivera", "phone", OWNER), ("Sam Okafor", "phone", SAM),
                              ("Linda Rivera", "phone", MOM),
                              ("Priya Natarajan", "email", PRIYA)):
        people[name] = entities.create(conn, "person", name)["id"]
        obs.link_handle(conn, kind, value, people[name])
    conn.commit()
    return {"flights": flights, "passport": passport, "people": people}


@pytest.fixture
def world(conn, cfg):
    return seed(conn, cfg)


def _build(conn, cfg, **kw):
    return mod.build(conn, cfg, today=TODAY, now=NOW, **kw)


# ---------------------------------------------------------------- the kinds

def test_each_kind_fires_in_a_fixed_order_and_cites_its_rows(conn, cfg, world):
    pack = _build(conn, cfg)
    kinds = [(n["id"], n["kind"]) for n in pack["nudges"]]
    assert kinds == [("N1", "reminder"), ("N2", "reminder"), ("N3", "birthday"),
                     ("N4", "nutrient"), ("N5", "stale")]
    n1, n2, n3, n4, n5 = pack["nudges"]
    assert n1["title"] == "Overdue by 3 days: Renew passport"
    assert n1["key"] == f"due:{world['passport']['uuid']}" and n1["tier"] == "confirmed"
    assert n2["title"] == "Due in 3 days (2026-10-24): Book flights to Ljubljana"
    assert pack["index"][n2["ids"][0]]["uuid"] == world["flights"]["uuid"]
    sam = world["people"]["Sam Okafor"]
    assert n3["title"] == "Sam Okafor's birthday is in 4 days (2026-10-25), turning 34"
    assert n3["key"] == f"birthday:{sam}:2026" and n3["tier"] == "observed"
    assert pack["index"][n3["ids"][0]]["stream"] == "contact_card"
    assert n4["key"] == "nutrient:vitamin_d" and "Vitamin D averaged 2.0" in n4["title"]
    assert n4["detail"].startswith("This compares logged intake")
    cited = pack["index"][n4["ids"][0]]
    assert cited["type"] == "observation_count" and cited["count"] == 3
    assert n5["key"] == "stale:android_sms"
    assert n5["title"] == "android_sms: newest record is 2026-09-01, nothing newer in over 14 days"
    titles = " ".join(n["title"] for n in pack["nudges"])
    assert "Visit Mom" not in titles and "Dentist" not in titles
    assert "Linda" not in titles and "Alex Rivera" not in titles   # too far; the owner
    assert pack["hidden"] == [] and pack["today"] == "2026-10-21"
    assert pack["as_of"] is not None                              # from the audit log


def test_the_agenda_lists_today_and_tomorrow_without_the_owner(conn, cfg, world):
    pack = _build(conn, cfg)
    agenda = [(a["date"], a["time"], a["title"], a["with"], a["cancelled"])
              for a in pack["agenda"]]
    assert agenda == [
        ("2026-10-21", "10:00", "1:1 Alex / Priya", ["Priya Natarajan"], False),
        ("2026-10-22", None, "Launch week", [], False),
        ("2026-10-22", "13:00", "Dentist (moved)", [], True),
    ]
    assert all(a["id"].startswith("O") for a in pack["agenda"])
    assert pack["agenda"][1]["all_day"] is True


def test_building_a_pack_is_deterministic_and_writes_nothing(conn, cfg, world):
    before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    a, b = _build(conn, cfg), _build(conn, cfg)
    assert a == b and a["pack_id"] == b["pack_id"]
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before


def test_an_empty_database_is_a_valid_pack(conn, cfg):
    pack = _build(conn, cfg)
    assert pack["nudges"] == [] and pack["agenda"] == [] and pack["hidden"] == []
    assert pack["as_of"] is None
    assert "nothing due" in "\n".join(mod.render_lines(pack))


# ---------------------------------------------------------------- state

def test_snooze_and_dismiss_hide_by_key_and_change_the_pack_id(conn, cfg, world):
    base = _build(conn, cfg)
    key = f"due:{world['passport']['uuid']}"
    state.snooze(conn, key, until="2026-10-25", note="after the trip")
    conn.commit()
    snoozed = _build(conn, cfg)
    assert [n["key"] for n in snoozed["hidden"]] == [key]
    assert snoozed["hidden"][0]["state"] == {"state": "snoozed", "until": "2026-10-25",
                                             "note": "after the trip"}
    assert snoozed["nudges"][0]["title"].startswith("Due in 3 days")   # renumbered
    assert snoozed["pack_id"] != base["pack_id"]
    # the hidden nudge keeps an id in the index, so --all can name it
    assert snoozed["hidden"][0]["id"] in snoozed["index"]
    without = _build(conn, cfg, include_hidden=False)
    assert without["hidden"] == [] and not any(
        i.get("type") == "nudge" and i["key"] == key for i in without["index"].values())

    state.snooze(conn, key, until="2026-10-20")                       # already past
    conn.commit()
    expired = _build(conn, cfg)
    assert expired["nudges"][0]["key"] == key
    assert expired["nudges"][0]["state"]["expired"] is True

    state.dismiss(conn, "stale:android_sms")
    conn.commit()
    pack = _build(conn, cfg)
    assert [n["key"] for n in pack["hidden"]] == ["stale:android_sms"]
    assert "stale" not in {n["kind"] for n in pack["nudges"]}


# ---------------------------------------------------------------- check

def test_check_reads_the_saved_pack_and_holds_every_number_to_it(conn, cfg, world):
    pack = _build(conn, cfg)
    good = ("Renew the passport: it is 3 days overdue [N1]. Flights to Ljubljana are "
            "due 2026-10-24 [N2]. Sam Okafor turns 34 on 2026-10-25 [N3].")
    r = mod.check(good, pack)
    assert r["cited"] == ["N1", "N2", "N3"] and not r["unknown"] and not r["unsupported"]
    bad = mod.check("The passport is 5 days overdue [N1]. See [N9].", pack)
    assert bad["unknown"] == ["N9"] and bad["unsupported"] == ["The passport is 5 days "
                                                                "overdue [N1]."]
    # the underlying row ids are citable too, and today's date is free context
    e = pack["nudges"][1]["ids"][0]
    assert not mod.check(f"Today is 2026-10-21; the flights were noted on 2026-10-19 [{e}].",
                         pack)["unsupported"]
    # the index lines carry what a saved brief needs: uuids and dates
    line = mod.lines(pack)["N1"]
    assert "due:" in line and "Renew passport" in line and "rests on E" in line


# ---------------------------------------------------------------- settings

def test_settings_default_and_refuse_bad_values(cfg):
    assert mod.settings(cfg) == mod.DEFAULTS
    cfg.extra = {"assistant": {"due_days": 3, "reminders_dir": "~/OneDrive/reminders",
                               "auto_publish": "false"}}
    s = mod.settings(cfg)
    assert s["due_days"] == 3 and s["reminders_dir"] == "~/OneDrive/reminders"
    assert s["auto_publish"] is False and s["birthday_days"] == 14
    for bad in ({"due_days": 0}, {"due_days": True}, {"reminder_hour": 24},
                {"reminders_dir": 3}, {"auto_publish": "maybe"}, {"snooze_days": 2}):
        cfg.extra = {"assistant": bad}
        with pytest.raises(DomainError, match=r"\[assistant\]"):
            mod.settings(cfg)


def test_birthday_parsing_and_next_occurrence():
    assert mod.parse_birthday("1992-10-25") == (1992, 10, 25)
    assert mod.parse_birthday("--02-29") == (None, 2, 29)
    assert mod.parse_birthday("March 14") is None and mod.parse_birthday(None) is None
    assert mod.next_occurrence(2, 29, date(2027, 1, 1)) == date(2027, 2, 28)
    assert mod.next_occurrence(10, 21, TODAY) == TODAY
    assert mod.next_occurrence(10, 20, TODAY) == date(2027, 10, 20)


# ---------------------------------------------------------------- CLI

def _project(tmp_path, monkeypatch) -> tuple[Config, dict]:
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    try:
        world = seed(conn, cfg)
    finally:
        conn.close()
    return cfg, world


def test_cli_lists_saves_and_checks_against_the_saved_pack(tmp_path, monkeypatch):
    cfg, world = _project(tmp_path, monkeypatch)
    out = tmp_path / "pack.json"
    r = runner.invoke(app, ["nudges", "--today", "2026-10-21", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "[N1] Overdue by 3 days: Renew passport" in r.output
    assert "Agenda (calendar, observed):" in r.output and "pack saved" in r.output
    pack = json.loads(out.read_text(encoding="utf-8"))
    assert pack["pack_id"] and pack["nudges"][0]["key"].startswith("due:")

    r = runner.invoke(app, ["nudges", "--today", "2026-10-21", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["pack_id"] == pack["pack_id"]

    brief = tmp_path / "brief.md"
    brief.write_text("Renew the passport, 3 days overdue [N1].", encoding="utf-8")
    r = runner.invoke(app, ["nudges", "--check", str(brief), "--pack", str(out)])
    assert r.exit_code == 0 and r.output.startswith("ok: 1 id(s) cited")
    brief.write_text("Renew the passport, 4 days overdue [N1]. [N7]", encoding="utf-8")
    r = runner.invoke(app, ["nudges", "--check", str(brief), "--pack", str(out)])
    assert r.exit_code == 1 and "unknown id(s): N7" in r.output and "unsupported" in r.output
    r = runner.invoke(app, ["nudges", "--check", str(brief)])
    assert r.exit_code == 1 and "needs --pack" in r.output


def test_cli_writes_take_a_key_or_an_n_number_with_its_pack(tmp_path, monkeypatch):
    cfg, world = _project(tmp_path, monkeypatch)
    out = tmp_path / "pack.json"
    assert runner.invoke(app, ["nudges", "--today", "2026-10-21", "--out", str(out),
                               "--json"]).exit_code == 0
    key = f"due:{world['passport']['uuid']}"

    r = runner.invoke(app, ["nudges", "snooze", "N1", "--days", "3"])
    assert r.exit_code == 1 and "pass the key printed beside it" in r.output
    r = runner.invoke(app, ["nudges", "snooze", "N1", "--days", "3", "--pack", str(out)])
    assert r.exit_code == 0, r.output
    assert re.fullmatch(rf"snoozed {re.escape(key)} until \d{{4}}-\d{{2}}-\d{{2}}\n", r.output)
    r = runner.invoke(app, ["nudges", "snooze", key, "--until", "2026-10-30",
                            "--note", "after the trip"])
    assert r.exit_code == 0 and r.output.strip() == f"snoozed {key} until 2026-10-30"
    r = runner.invoke(app, ["nudges", "snooze", key])
    assert r.exit_code == 1 and "one of them" in r.output
    r = runner.invoke(app, ["nudges", "dismiss", "stale:android_sms"])
    assert r.exit_code == 0 and r.output.strip() == "dismissed stale:android_sms"

    r = runner.invoke(app, ["nudges", "--today", "2026-10-21", "--all"])
    assert r.exit_code == 0 and "Hidden:" in r.output
    assert f"snoozed until 2026-10-30 · key {key}" in r.output
    r = runner.invoke(app, ["nudges", "states"])
    assert key in r.output and "stale:android_sms  dismissed" in r.output

    r = runner.invoke(app, ["nudges", "restore", key])
    assert r.exit_code == 0 and r.output.strip() == f"restored {key} (was snoozed)"
    r = runner.invoke(app, ["nudges", "restore", key])
    assert r.exit_code == 1 and "neither snoozed nor dismissed" in r.output
    r = runner.invoke(app, ["nudges", "dismiss", "bogus:1"])
    assert r.exit_code == 1 and "nudge key must look like" in r.output

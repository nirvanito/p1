"""The nudges that rest on series and declared coverage: goal progress,
drift against your own baseline, follow-ups the imported records show no
reply to, and a person whose own rhythm has lapsed. Each says what the
records hold, never what you did, and goes quiet instead of guessing when
the coverage is not there."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from lifecrm import nudges as mod
from lifecrm.domain import entities, entries, measurements
from lifecrm.domain import observations as obs
from lifecrm.domain.util import canonical_json

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)
OWNER, JAKE, MAYA, SAM, MOM = ("+15125550100", "+15125550142", "+15125550187",
                               "+15125550155", "+12105550133")
PRIYA = "priya@acme-robotics.example"


def _d(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def _rec(n, stream, day, hhmm, parts=(), **kw):
    rec = {"stream": stream, "external_id": f"m-{n}", "journal_date": day,
           "started_at": f"{day}T{hhmm}:00Z", "direction": None,
           "participants": [{"kind": k, "value": v, "role": r} for k, v, r in parts]}
    rec.update(kw)
    return rec


def _apply(conn, records, coverage=(), connector="more_fixture", account=OWNER):
    scan = {"connector": connector, "account": account, "source_file": "fixture",
            "source_hash": connector.ljust(64, "x"), "records": records,
            "handles": [{"kind": "phone", "value": OWNER, "display_name": "you (this phone)"},
                        {"kind": "phone", "value": JAKE, "display_name": "Jake Morales"},
                        {"kind": "phone", "value": MAYA, "display_name": "Maya Chen"},
                        {"kind": "phone", "value": SAM, "display_name": "Sam Okafor"},
                        {"kind": "phone", "value": MOM, "display_name": "Linda Rivera"},
                        {"kind": "email", "value": PRIYA, "display_name": "Priya Natarajan"}],
            "coverage": list(coverage)}
    return obs.apply_plan(conn, scan, obs.build_plan(conn, scan))


def _people(conn):
    ids = {}
    for name, kind, value in (("Alex Rivera", "phone", OWNER), ("Jake Morales", "phone", JAKE),
                              ("Maya Chen", "phone", MAYA), ("Sam Okafor", "phone", SAM),
                              ("Linda Rivera", "phone", MOM),
                              ("Priya Natarajan", "email", PRIYA)):
        ids[name] = entities.create(conn, "person", name)["id"]
        obs.link_handle(conn, kind, value, ids[name])
    return ids


def _in(n, who, day, hhmm, body, stream="imessage", extra_parts=()):
    return _rec(n, stream, day, hhmm, [(("email" if "@" in who else "phone"), who, "sender"),
                                       ("phone", OWNER, "recipient"), *extra_parts],
                direction="in", body=body)


def _out(n, who, day, hhmm, body, stream="imessage"):
    return _rec(n, stream, day, hhmm, [("phone", OWNER, "sender"),
                                       (("email" if "@" in who else "phone"), who, "recipient")],
                direction="out", body=body)


def _build(conn, cfg):
    return mod.build(conn, cfg, today=TODAY, now=NOW)


# ---------------------------------------------------------------- follow-ups

@pytest.fixture
def world(conn, cfg):
    records = [
        _in(1, MAYA, _d(-5), "23:00", "Dinner Saturday? Uchi?"),               # no reply
        _in(2, JAKE, _d(-6), "17:00", "Veracruz tonight?"),
        _out(3, JAKE, _d(-5), "09:00", "always"),                                # replied
        _in(4, SAM, _d(-4), "12:00", "Climbing Saturday?", extra_parts=[("phone", JAKE,
                                                                           "recipient")]),
        _in(5, PRIYA, _d(-7), "08:47", "Can you own the board deck?", stream="email"),
        _rec(6, "calendar_event", _d(-1), "15:00", [("email", PRIYA, "organizer"),
                                                     ("phone", OWNER, "participant")],
             title="1:1 Alex / Priya", ended_at=f"{_d(-1)}T15:30:00Z",
             value_json=canonical_json({"calendar": "work"})),            # met since
        _in(7, MOM, _d(-9), "18:30", "When are you two coming to San Antonio?",
            stream="whatsapp"),                                              # called since
        _in(8, JAKE, _d(-1), "20:00", "Rematch?"),                           # too fresh
        # the newest text in the store, so Maya's reply window has closed
        _in(9, SAM, _d(0), "07:40", "New gym Saturday?", extra_parts=[("phone", JAKE,
                                                                        "recipient")]),
        # a line you sent in a group Maya is in is not a reply to her text
        _rec(10, "imessage", _d(-2), "12:00", [("phone", OWNER, "sender"),
                                               ("phone", MAYA, "recipient"),
                                               ("phone", JAKE, "recipient")],
             direction="out", body="booking the lakeside place"),
    ]
    _apply(conn, records)
    ids = _people(conn)
    call = entries.create(conn, journal_date=_d(-3), kind="event", title="Called Mom",
                          primary_category="social")
    entries.attach_entity(conn, call["id"], ids["Linda Rivera"], "participant")
    conn.commit()
    return ids


def test_follow_ups_name_the_one_unanswered_message_and_its_wording(conn, cfg, world):
    pack = _build(conn, cfg)
    fu = [n for n in pack["nudges"] if n["kind"] == "follow_up"]
    assert [n["title"] for n in fu] == [
        f"No reply found in imported records: Maya Chen's text from {_d(-5)}"]
    n = fu[0]
    assert n["detail"].startswith('"Dinner Saturday? Uchi?" — nothing from you to them since')
    assert f"newest imported text is {_d(0)}" in n["detail"] and n["tier"] == "observed"
    assert n["key"].startswith("followup:") and pack["index"][n["ids"][0]]["stream"] == "imessage"
    assert n["entity_id"] == world["Maya Chen"]
    # a reply in kind, a meeting, a journaled call and a fresh message are not follow-ups
    assert not any("Jake" in t or "Priya" in t or "Linda" in t or "Sam" in t
                   for t in (n["title"] for n in fu))
    assert pack["nudges"][0]["kind"] == "follow_up"          # nothing is due: it leads


def test_a_follow_up_is_not_judged_when_the_channel_stops_too_early(conn, cfg):
    _apply(conn, [_in(1, MAYA, _d(-5), "23:00", "Dinner Saturday?"),
                  _in(2, JAKE, _d(-4), "17:00", "yo")])          # newest text: 4 days ago
    _people(conn)
    conn.commit()
    pack = _build(conn, cfg)
    assert not [n for n in pack["nudges"] if n["kind"] == "follow_up"]
    assert any("not judged" in note and "import again first" in note for note in pack["notes"])


# ---------------------------------------------------------------- lapsed

def test_a_lapsed_rhythm_needs_a_history_and_a_gap_twice_the_usual(conn, cfg):
    weekly = [(TODAY - timedelta(days=35 + 7 * k)).isoformat() for k in range(12)]  # 12 calls
    records = [_rec(10 + k, "ios_call", d, "21:00", [("phone", OWNER, "sender"),
                                                     ("phone", SAM, "recipient")],
                    direction="out", value=300, unit="s") for k, d in enumerate(weekly)]
    records += [_rec(50 + k, "ios_call", (TODAY - timedelta(days=9 * k + 2)).isoformat(),
                     "21:00", [("phone", OWNER, "sender"), ("phone", JAKE, "recipient")],
                     direction="out", value=120, unit="s") for k in range(8)]      # Jake: current
    _apply(conn, records)
    ids = _people(conn)
    conn.commit()
    pack = _build(conn, cfg)
    lapsed = [n for n in pack["nudges"] if n["kind"] == "lapsed"]
    assert len(lapsed) == 1
    n = lapsed[0]
    assert n["title"] == ("Sam Okafor: no recorded contact in 35 days; the usual gap was about "
                          f"7 days over 12 contact days since {weekly[-1]}")
    assert n["key"] == f"lapsed:{ids['Sam Okafor']}" and n["date"] == weekly[0]
    assert pack["index"][n["ids"][0]]["stream"] == "ios_call"
    assert "(observed, not confirmed)" in n["detail"]


# ---------------------------------------------------------------- drift

def test_drift_compares_the_last_week_with_the_month_before_only_with_coverage(conn, cfg):
    base = [TODAY - timedelta(days=k) for k in range(8, 37)]          # the 30 days before
    for d in base[::3]:                                               # 10 baseline nights
        measurements.add(conn, journal_date=d.isoformat(), metric="sleep_hours",
                         numeric_value=7.3, unit="h")
    for k in (1, 3, 5):                                               # 3 recent nights
        measurements.add(conn, journal_date=_d(-k), metric="sleep_hours", numeric_value=6.2,
                         unit="h")
    for d in base[::3]:                                               # mood: no change
        measurements.add(conn, journal_date=d.isoformat(), metric="mood", numeric_value=7)
    for k in (1, 3, 5):
        measurements.add(conn, journal_date=_d(-k), metric="mood", numeric_value=7)
    # workouts: nine sessions in the baseline month, none since; the export is complete
    sessions = [_d(-k) for k in (8, 11, 15, 18, 22, 25, 29, 32, 36)]
    _apply(conn, [_rec(100 + i, "workout", d, "11:30", title="Lift", value=70.0, unit="min",
                       ended_at=f"{d}T12:40:00Z", origin_app="strong")
                  for i, d in enumerate(sessions)],
           coverage=[{"stream": "workout", "start": _d(-36), "end": _d(0),
                      "basis": "full-history export"}], connector="fitness_fixture",
           account="strong")
    # spending: three baseline purchases, one small recent one; statements declared
    _apply(conn, [_rec(200, "txn", _d(-31), "17:00", title="SHOP", value=-120.0, unit="USD"),
                  _rec(201, "txn", _d(-17), "17:00", title="SHOP", value=-150.0, unit="USD"),
                  _rec(202, "txn", _d(-10), "17:00", title="SHOP", value=-90.0, unit="USD"),
                  _rec(203, "txn", _d(-3), "17:00", title="CAFE", value=-10.0, unit="USD")],
           coverage=[{"stream": "txn", "start": _d(-36), "end": _d(0),
                      "basis": "OFX statement range"}], connector="finance_fixture",
           account="bank:1")
    _people(conn)
    conn.commit()
    pack = _build(conn, cfg)
    drift = {n["key"]: n for n in pack["nudges"] if n["kind"] == "drift"}
    assert set(drift) == {"drift:sleep_hours", "drift:workouts", "drift:spending"}
    sleep = drift["drift:sleep_hours"]
    assert sleep["title"] == ("sleep (journal) down: last 7 days averaged 6.2 h vs 7.3 h over "
                              "the 30 before")
    assert sleep["detail"].startswith("3 of 7 recent days known, 10 of 30 baseline")
    assert "not a verdict" in sleep["detail"] and sleep["tier"] == "confirmed"
    assert len(sleep["ids"]) == 2 and all(i.startswith("M") for i in sleep["ids"])
    recent = pack["index"][sleep["ids"][0]]
    assert recent["type"] == "rows" and recent["count"] == 3 and recent["table"] == "measurements"
    workouts = drift["drift:workouts"]
    assert workouts["title"] == ("workouts down: 0 sessions in the last 7 days vs 2.1 sessions a "
                                 "week over the 30 before")
    assert workouts["detail"].startswith("7 of 7 recent days known, 30 of 30 baseline")
    assert workouts["ids"] and all(i.startswith("O") for i in workouts["ids"])
    spending = drift["drift:spending"]
    assert spending["title"] == ("spending down: 10 USD in the last 7 days vs 84 USD a week "
                                 "over the 30 before")
    # mood did not move; steps and energy were never recorded: notes, not nudges
    assert "drift:mood" not in drift
    assert any(n.startswith("steps: insufficient coverage") for n in pack["notes"])
    assert any(n.startswith("energy: insufficient coverage") for n in pack["notes"])
    # every number in a drift title is in its cited lines
    r = mod.check(f"Sleep averaged 6.2 h over the last 7 days against 7.3 h before "
                  f"[{sleep['id']}].", pack)
    assert not r["unsupported"] and not r["unknown"]


# ---------------------------------------------------------------- goals

def test_goal_nudges_describe_progress_and_cite_the_plan_and_the_rows(conn, cfg):
    plan = entries.create(conn, journal_date=_d(-9), kind="plan",
                          title="Get back to three gym sessions a week", primary_category="health",
                          data={"goal": {"metric": "workouts", "target": 3, "per": "week"}})
    _apply(conn, [_rec(300, "workout", _d(-1), "11:30", title="Lift", value=70.0, unit="min",
                       ended_at=f"{_d(-1)}T12:40:00Z", origin_app="strong")],
           coverage=[{"stream": "workout", "start": _d(-9), "end": _d(0),
                      "basis": "full-history export"}], connector="fitness_fixture",
           account="strong")
    _people(conn)
    conn.commit()
    pack = _build(conn, cfg)
    [g] = [n for n in pack["nudges"] if n["kind"] == "goal"]
    assert g["key"] == f"goal:{plan['uuid']}" and g["tier"] == "confirmed"
    assert g["title"].startswith("Get back to three gym sessions a week — workouts >= 3 sessions "
                                 "per week: 1 of 3 so far this week (2026-W43)")
    assert "last week (2026-W42) missed" in g["title"]
    assert pack["index"][g["ids"][0]]["kind"] == "plan"
    assert any(pack["index"][i]["type"] == "rows" for i in g["ids"][1:])
    assert "(observed, not confirmed)" in g["detail"]
    r = mod.check(f"You have 1 of 3 gym sessions so far this week [{g['id']}].", pack)
    assert not r["unsupported"]
    # the kinds keep their order: goal before drift, drift before birthdays
    kinds = [n["kind"] for n in pack["nudges"]]
    assert kinds == sorted(kinds, key=mod.KIND_ORDER.index)

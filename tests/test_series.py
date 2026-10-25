"""The daily series registry: values per day, the days each series KNOWS,
and the rows behind every value. Missing data stays unknown; a known zero
exists only where an import declared the day complete."""

from __future__ import annotations

import pytest

from lifecrm import series
from lifecrm.domain import entries, health, measurements
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError, canonical_json

OWNER = "+15125550100"
JAKE = "+15125550142"
MAYA = "+15125550187"
D = {n: f"2026-10-{n:02d}" for n in range(1, 32)}


def _scan(records, coverage=(), connector="series_fixture", account=OWNER):
    return {"connector": connector, "account": account, "source_file": "fixture",
            "source_hash": "s" * 64, "records": records,
            "handles": [{"kind": "phone", "value": OWNER, "display_name": "you (this phone)"},
                        {"kind": "phone", "value": JAKE, "display_name": "Jake Morales"},
                        {"kind": "phone", "value": MAYA, "display_name": "Maya Chen"}],
            "coverage": list(coverage)}


def _rec(n, stream, day, hhmm, parts=(), **kw):
    rec = {"stream": stream, "external_id": f"s-{n}", "journal_date": day,
           "started_at": f"{day}T{hhmm}:00Z", "direction": None,
           "participants": [{"kind": k, "value": v, "role": r} for k, v, r in parts]}
    rec.update(kw)
    return rec


def _apply(conn, scan):
    return obs.apply_plan(conn, scan, obs.build_plan(conn, scan))


def _link(conn, people):
    ids = {}
    for name, value in people.items():
        ids[name] = entries_person(conn, name)
        obs.link_handle(conn, "phone", value, ids[name])
    return ids


def entries_person(conn, name):
    from lifecrm.domain import entities
    return entities.create(conn, "person", name)["id"]


# ---------------------------------------------------------------- measured quantities

def test_measured_series_know_only_the_days_with_a_record(conn, cfg):
    m1 = measurements.add(conn, journal_date=D[1], metric="mood", numeric_value=7)
    measurements.add(conn, journal_date=D[3], metric="mood", numeric_value=5)
    health.insert(conn, {"metric": "steps", "journal_date": D[2], "started_at": f"{D[2]}T10:00:00Z",
                         "value": 8000, "unit": "count", "external_id": "st-1",
                         "source_file": "x.zip"})
    health.insert(conn, {"metric": "sleep_session", "journal_date": D[2],
                         "started_at": f"{D[1]}T23:00:00Z", "ended_at": f"{D[2]}T06:30:00Z",
                         "value": 450, "unit": "min", "external_id": "sl-1",
                         "source_file": "x.zip"})
    conn.commit()
    mood = series.daily(conn, cfg, "mood", D[1], D[5])
    assert mood.values == {D[1]: 7.0, D[3]: 5.0} and mood.known == {D[1], D[3]}
    assert mood.refs[D[1]] == [("measurements", m1["uuid"])]
    assert mood.tier == "confirmed" and mood.agg == "avg"
    assert mood.period(D[1], D[5]) == {"value": 6.0, "known": 2, "days": 5, "complete": False}

    steps = series.daily(conn, cfg, "steps", D[1], D[5])
    assert steps.values == {D[2]: 8000.0} and steps.known == {D[2]}     # never zero-filled
    assert steps.period(D[1], D[5])["value"] == 8000.0

    sleep = series.daily(conn, cfg, "sleep", D[1], D[5])
    assert sleep.values == {D[2]: 7.5} and sleep.unit == "h" and sleep.tier == "observed"
    assert series.daily(conn, cfg, "weight", D[1], D[5]).period(D[1], D[5])["value"] is None


# ---------------------------------------------------------------- workouts

def test_workouts_merge_sessions_and_know_zeros_only_inside_declared_coverage(conn, cfg):
    cov = [{"stream": "workout", "start": D[1], "end": D[6], "basis": "full-history export"}]
    _apply(conn, _scan([
        _rec(1, "workout", D[2], "06:30", title="Push day", value=70.0, unit="min",
             ended_at=f"{D[2]}T07:40:00Z", origin_app="strong"),
        _rec(2, "workout", D[5], "18:00", title="Run", value=25.0, unit="min",
             origin_app="strava"),
    ], cov))
    # the watch saw the same push day (overlapping) and a separate evening run
    health.insert(conn, {"metric": "exercise_session", "journal_date": D[2],
                         "started_at": f"{D[2]}T06:35:00Z", "ended_at": f"{D[2]}T07:30:00Z",
                         "value": 55, "unit": "min", "external_id": "ex-1", "source_file": "w.zip"})
    health.insert(conn, {"metric": "exercise_session", "journal_date": D[2],
                         "started_at": f"{D[2]}T19:00:00Z", "ended_at": f"{D[2]}T19:30:00Z",
                         "value": 30, "unit": "min", "external_id": "ex-2", "source_file": "w.zip"})
    # the journal: an untimed exercise entry that day (attaches), and one alone on the 8th
    entries.create(conn, journal_date=D[2], kind="event", title="Lift with Sam",
                   primary_category="exercise")
    alone = entries.create(conn, journal_date=D[8], kind="event", title="Evening climb",
                           primary_category="exercise")
    # a plan ABOUT exercise is not a session, whatever its categories say
    entries.create(conn, journal_date=D[9], kind="plan", title="Three sessions a week",
                   primary_category="health", secondary_categories=["exercise"],
                   data={"goal": {"metric": "workouts", "target": 3, "per": "week"}})
    conn.commit()

    w = series.daily(conn, cfg, "workouts", D[1], D[10])
    assert w.values[D[2]] == 2.0                      # push day (3 rows) + the evening run
    assert len(w.refs[D[2]]) == 4
    assert w.values[D[5]] == 1.0
    assert w.values[D[8]] == 1.0 and w.refs[D[8]] == [("entries", alone["uuid"])]
    assert {d for d in w.known if w.values[d] == 0.0} == {D[1], D[3], D[4], D[6]}
    assert D[7] not in w.known and D[9] not in w.known   # outside the declaration: unknown
    assert D[9] not in w.values                           # the plan is not a session
    assert w.period(D[1], D[6]) == {"value": 3.0, "known": 6, "days": 6, "complete": True}
    assert w.period(D[1], D[10])["complete"] is False
    assert w.sources["declared_days"] == 6


# ---------------------------------------------------------------- spending, texts, contacts

def test_spending_applies_the_finance_rule_and_declared_statement_days(conn, cfg):
    cov = [{"stream": "txn", "start": D[1], "end": D[4], "basis": "OFX statement range"}]
    _apply(conn, _scan([
        _rec(1, "txn", D[2], "17:00", title="VERACRUZ", value=-34.5, unit="USD"),
        _rec(2, "txn", D[2], "19:00", title="H-E-B", value=-20.0, unit="USD"),
        _rec(3, "txn", D[3], "09:00", title="PAYROLL", value=4200.0, unit="USD"),
        _rec(4, "txn", D[6], "12:00", title="Venmo transfer", value=-100.0, unit="USD",
             origin_app="venmo", value_json=canonical_json({"transfer": True})),
        _rec(5, "txn", D[6], "13:00", title="Cafe", value=-4.0, unit="EUR"),
    ], cov))
    conn.commit()
    sp = series.daily(conn, cfg, "spending", D[1], D[8])
    assert sp.unit == "USD" and sp.sources["other_currencies"] == ["EUR"]
    assert sp.values[D[2]] == 54.5 and sp.values[D[3]] == 0.0      # income is not spending
    assert sp.values[D[1]] == 0.0 and sp.values[D[4]] == 0.0       # declared statement days
    assert D[6] not in sp.known and D[5] not in sp.known         # the transfer is left out
    assert len(sp.refs[D[2]]) == 2


def test_texts_and_contacts_take_zeros_from_complete_texts_only(conn, cfg):
    me_in, me_out = ("phone", OWNER, "recipient"), ("phone", OWNER, "sender")
    cov = [{"stream": "imessage", "start": D[1], "end": D[5], "basis": "a whole store"}]
    _apply(conn, _scan([
        _rec(1, "imessage", D[2], "17:00", [("phone", JAKE, "sender"), me_in],
             direction="in", body="tacos?"),
        _rec(2, "imessage", D[2], "17:05", [me_out, ("phone", JAKE, "recipient")],
             direction="out", body="always"),
        # a group text Maya sent is contact with Maya only, not with Jake
        _rec(3, "imessage", D[4], "20:00", [("phone", MAYA, "sender"), me_in,
                                            ("phone", JAKE, "recipient")],
             direction="in", body="castle this time"),
        _rec(4, "ios_call", D[7], "21:00", [me_out, ("phone", JAKE, "recipient")],
             direction="out", value=300, unit="s"),
    ], cov))
    ids = _link(conn, {"Jake Morales": JAKE, "Maya Chen": MAYA})
    owner_id = entries_person(conn, "Alex Rivera")
    obs.link_handle(conn, "phone", OWNER, owner_id)
    # a confirmed entry naming Jake as a participant is contact too
    dinner = entries.create(conn, journal_date=D[9], kind="event", title="Dinner with Jake",
                            primary_category="social")
    entries.attach_entity(conn, dinner["id"], ids["Jake Morales"], "participant")
    conn.commit()

    texts = series.daily(conn, cfg, "texts", D[1], D[8])
    assert texts.values[D[2]] == 2.0 and texts.values[D[4]] == 1.0
    assert {d for d in texts.known if texts.values[d] == 0.0} == {D[1], D[3], D[5]}
    assert D[6] not in texts.known

    people = series.daily(conn, cfg, "contacts", D[1], D[10])
    assert people.values[D[2]] == 1.0 and people.values[D[4]] == 1.0    # Jake; Maya
    assert people.values[D[7]] == 1.0                                   # the call counts
    assert people.values[D[9]] == 1.0 and people.refs[D[9]] == [("entries", dinner["uuid"])]
    assert people.values[D[1]] == 0.0 and D[6] not in people.known
    assert "no recorded texts" in people.sources["zero_means"]

    jake = series.daily(conn, cfg, f"contact:{ids['Jake Morales']}", D[1], D[10])
    assert jake.values[D[2]] == 1.0 and jake.values[D[4]] == 0.0 and jake.values[D[7]] == 1.0
    assert jake.values[D[9]] == 1.0 and D[8] not in jake.known
    # three contact days (the 2nd, 7th, 9th) and four declared zeros (1st, 3rd-5th)
    assert jake.period(D[1], D[10]) == {"value": 3.0, "known": 7, "days": 10, "complete": False}


# ---------------------------------------------------------------- journal categories, nutrients

def test_categories_have_positives_only_and_nutrients_read_the_diary(conn, cfg):
    entries.create(conn, journal_date=D[1], kind="event", title="Tacos", primary_category="food")
    entries.create(conn, journal_date=D[1], kind="event", title="Lunch", primary_category="food")
    entries.create(conn, journal_date=D[2], kind="task", title="Call the bank",
                   primary_category="chores")
    entries.create(conn, journal_date=D[3], kind="reflection", title="Weekly review",
                   primary_category="food", data={"derived_from": "lifecrm review"})
    _apply(conn, _scan([_rec(1, "nutrition_day", D[1], "23:00", value=2000.0, unit="kcal",
                             value_json=canonical_json({"completed": True, "nutrients": {
                                 "vitamin_d": [4.0, "µg"], "magnesium": [300.0, "mg"]}}))]))
    conn.commit()
    food = series.daily(conn, cfg, "cat:food", D[1], D[5])
    assert food.values == {D[1]: 2.0} and food.known == {D[1]}      # the logged 2nd is unknown
    assert food.tier == "confirmed" and food.label == "food entries"
    vit = series.daily(conn, cfg, "nutrient:vitamin_d", D[1], D[5])
    assert vit.values == {D[1]: 4.0} and vit.unit == "µg" and vit.label == "Vitamin D"


def test_unknown_keys_are_refused_and_the_registry_is_coherent(conn, cfg):
    for bad in ("sleepiness", "contact:", "contact:jake", "nutrient:unobtainium"):
        with pytest.raises(DomainError):
            series.daily(conn, cfg, bad, D[1], D[2])
    assert all(m["agg"] in ("avg", "sum", "count") for m in series.KEYS.values())
    assert series.info("cat:food")["agg"] == "count"
    assert series.available(conn, cfg, D[1], D[5]) == []
    measurements.add(conn, journal_date=D[1], metric="energy", numeric_value=6)
    conn.commit()
    assert series.available(conn, cfg, D[1], D[5]) == ["energy"]

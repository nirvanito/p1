"""Goals: a plan with a measurable target, validated in the domain layer on
every write path, and decided by how its metric adds up — a count or sum can
be reached early and projected, a level is decided only when the period is
over with enough known days, and an unknown day is never a zero."""

from __future__ import annotations

import json
from datetime import date

import pytest
from conftest import cli_project, golden_draft
from typer.testing import CliRunner

from lifecrm import contract, db as db_mod, goals, review
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.domain import corrections, entities, entries, health, measurements
from lifecrm.domain import goals as rules
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()
TODAY = date(2026, 10, 21)                 # a Wednesday; W43 runs 10-19..10-25
OWNER = "+15125550100"


def _plan(conn, day, title, goal, kind="plan"):
    return entries.create(conn, journal_date=day, kind=kind, title=title,
                          primary_category="health", data={"goal": goal})


def _workouts(conn, days, cover):
    scan = {"connector": "fitness_fixture", "account": "strong", "source_file": "strong.csv",
            "source_hash": "w" * 64, "handles": [],
            "records": [{"stream": "workout", "external_id": f"w-{d}", "journal_date": d,
                         "started_at": f"{d}T11:30:00Z", "ended_at": f"{d}T12:40:00Z",
                         "direction": None, "title": "Lift", "value": 70.0, "unit": "min",
                         "origin_app": "strong", "participants": []} for d in days],
            "coverage": [{"stream": "workout", "start": cover[0], "end": cover[1],
                          "basis": "full-history export"}]}
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))


# ---------------------------------------------------------------- validation

def test_a_goal_is_normalized_and_bad_shapes_are_single_line_errors():
    good = rules.validate_goal({"metric": "workouts", "target": 3, "per": "week"})
    assert good == {"metric": "workouts", "target": 3.0, "per": "week", "op": ">=",
                    "min_known": 5}
    assert rules.aggregation(good) == "count"
    assert rules.aggregation({"metric": "steps", "per": "day"}) == "sum"
    assert rules.aggregation({"metric": "steps", "per": "week"}) == "avg"
    assert rules.aggregation({"metric": "sleep", "per": "week"}) == "avg"
    zero = rules.validate_goal({"metric": "spending", "target": 0, "per": "week", "op": "<="})
    assert zero["target"] == 0.0 and zero["op"] == "<="
    for bad, msg in (
        ("3 a week", "must be an object"),
        ({"metric": "pushups", "target": 3, "per": "week"}, "metric 'pushups'"),
        ({"metric": "workouts", "target": True, "per": "week"}, "target must be a number"),
        ({"metric": "workouts", "target": float("nan"), "per": "week"}, "target must be"),
        ({"metric": "workouts", "target": -1, "per": "week"}, "0 or more"),
        ({"metric": "workouts", "target": 3, "per": "fortnight"}, "per must be"),
        ({"metric": "workouts", "target": 3, "per": "week", "op": "="}, "op must be"),
        ({"metric": "workouts", "target": 3, "per": "week", "start": "soon"}, "start must"),
        ({"metric": "contact", "target": 1, "per": "week"}, "entity"),
        ({"metric": "workouts", "target": 3, "per": "week", "entity": "Mom"}, "only goes"),
        ({"metric": "workouts", "target": 3, "per": "week", "currency": "USD"}, "currency"),
        ({"metric": "spending", "target": 3, "per": "week", "currency": "dollars"}, "3-letter"),
        ({"metric": "workouts", "target": 3, "per": "week", "min_known": 0}, "min_known"),
        ({"metric": "workouts", "target": 3, "per": "week", "colour": "red"}, "unknown field"),
    ):
        with pytest.raises(DomainError, match=msg):
            rules.validate_goal(bad)


def test_every_write_path_refuses_a_goal_outside_a_plan(conn, cfg):
    goal = {"metric": "workouts", "target": 3, "per": "week"}
    with pytest.raises(DomainError, match="needs kind 'plan'"):
        _plan(conn, "2026-10-13", "Gym", goal, kind="task")
    plan = _plan(conn, "2026-10-13", "Gym", goal)
    # a kind-only edit of a goal-bearing plan is refused
    with pytest.raises(DomainError, match="remove data.goal first"):
        corrections.edit_entry(conn, plan["uuid"], {"kind": "task"})
    with pytest.raises(DomainError, match="target must be"):
        corrections.edit_entry(conn, plan["uuid"], {"data": {"goal": {**goal, "target": "3"}}})
    corrections.edit_entry(conn, plan["uuid"], {"data": None})
    corrections.edit_entry(conn, plan["uuid"], {"kind": "task"})          # now fine
    snap = entries.snapshot(conn, plan["id"])
    assert snap["kind"] == "task" and snap["data"] is None

    draft = golden_draft()
    e = draft["entries"][0]
    e["kind"], e["data"] = "task", {"goal": goal}
    errs = contract.validate_draft(conn, draft, None)
    assert any("entries[0].data.goal needs kind 'plan'" in x for x in errs)
    e["kind"], e["data"] = "plan", {"goal": {**goal, "per": "fortnight"}}
    errs = contract.validate_draft(conn, draft, None)
    assert any("entries[0].data.goal.per must be" in x for x in errs)
    e["data"] = {"goal": goal}
    assert not [x for x in contract.validate_draft(conn, draft, None) if "goal" in x]


# ---------------------------------------------------------------- progress

def test_a_weekly_count_goal_is_met_missed_or_undecided_by_coverage(conn, cfg):
    plan = _plan(conn, "2026-10-06", "Get back to three gym sessions a week",
                 {"metric": "workouts", "target": 3, "per": "week"})
    # W41 (10-06..10-12): three sessions, W42: one, W43 so far: one (Tuesday)
    _workouts(conn, ["2026-10-06", "2026-10-08", "2026-10-10", "2026-10-13", "2026-10-20"],
              ("2026-10-06", "2026-10-21"))
    conn.commit()
    [g] = goals.progress(conn, cfg, TODAY)
    assert g["uuid"] == plan["uuid"] and g["error"] is None and g["agg"] == "count"
    assert g["series"]["key"] == "workouts" and g["start"] == "2026-10-06"
    cur = g["current"]
    assert (cur["label"], cur["value"], cur["met"]) == ("2026-W43", 1.0, None)
    assert cur["known"] == 3 and cur["days_elapsed"] == 3 and cur["days"] == 7
    assert cur["projected"] == 2.33                     # 1 in 3 of 7 days
    prev = g["previous"]
    assert (prev["label"], prev["value"], prev["met"], prev["complete"]) == \
        ("2026-W42", 1.0, False, True)                  # every day known: a real miss
    assert g["streak"] == {"count": 0, "ended_by": "missed"}
    assert len(g["refs"]) == 1                          # the one session this week
    line = goals.describe(g)
    assert line.startswith("Get back to three gym sessions a week — workouts >= 3 sessions per week: "
                           "1 of 3 so far this week (2026-W43), on pace for 2.33")
    assert "last week (2026-W42) missed" in line


def test_a_count_goal_is_undecided_when_the_days_were_not_declared(conn, cfg):
    _plan(conn, "2026-10-06", "Gym", {"metric": "workouts", "target": 3, "per": "week"})
    _workouts(conn, ["2026-10-06", "2026-10-08", "2026-10-10", "2026-10-13"],
              ("2026-10-06", "2026-10-12"))            # the export stops after W41
    conn.commit()
    [g] = goals.progress(conn, cfg, TODAY)
    assert g["previous"]["label"] == "2026-W42" and g["previous"]["met"] is None
    assert g["previous"]["known"] == 2    # the declared Monday and the day with a session
    assert g["streak"] == {"count": 0, "ended_by": "coverage"}
    assert "undecided" in goals.describe(g)


def test_a_count_goal_is_met_as_soon_as_it_is_reached_and_streaks_count(conn, cfg):
    _plan(conn, "2026-09-28", "Gym", {"metric": "workouts", "target": 2, "per": "week"})
    _workouts(conn, ["2026-09-29", "2026-09-30", "2026-10-07", "2026-10-09",
                     "2026-10-14", "2026-10-15", "2026-10-19", "2026-10-20"],
              ("2026-09-28", "2026-10-10"))             # W43 is reached with 5 unknown days
    conn.commit()
    [g] = goals.progress(conn, cfg, TODAY)
    assert g["current"]["met"] is True and g["current"]["known"] == 2
    assert g["previous"]["met"] is True                 # W42: reached, coverage irrelevant
    assert g["streak"] == {"count": 3, "ended_by": None}   # every week since the start


def test_a_level_goal_reports_its_mean_and_is_decided_only_with_enough_known_days(conn, cfg):
    _plan(conn, "2026-10-13", "Sleep seven hours", {"metric": "sleep_hours", "target": 7,
                                                    "per": "week"})
    for d, h in (("2026-10-13", 7.5), ("2026-10-14", 6.5), ("2026-10-15", 7.0),
                 ("2026-10-16", 8.0), ("2026-10-17", 7.2), ("2026-10-19", 6.0),
                 ("2026-10-20", 6.5)):
        measurements.add(conn, journal_date=d, metric="sleep_hours", numeric_value=h, unit="h")
    conn.commit()
    [g] = goals.progress(conn, cfg, TODAY)
    cur, prev = g["current"], g["previous"]
    assert g["agg"] == "avg" and cur["observed_mean"] == 6.25
    assert cur["above_target"] is False and cur["met"] is None and "projected" not in cur
    assert prev["observed_mean"] == 7.24 and prev["known"] == 5 and prev["met"] is True
    assert "averaging 6.25 h over 2 known day(s) this week (2026-W43), below target" in \
        goals.describe(g)
    # three known nights are too few to decide a week
    measurements.retract(conn, next(m["uuid"] for m in conn.execute(
        "SELECT uuid FROM measurements WHERE journal_date='2026-10-17'")))
    measurements.retract(conn, next(m["uuid"] for m in conn.execute(
        "SELECT uuid FROM measurements WHERE journal_date='2026-10-16'")))
    conn.commit()
    [g] = goals.progress(conn, cfg, TODAY)
    assert g["previous"]["known"] == 3 and g["previous"]["met"] is None


def test_a_budget_fails_as_soon_as_it_is_exceeded(conn, cfg):
    _plan(conn, "2026-10-13", "Spend under 100 a week", {"metric": "spending", "target": 100,
                                                          "per": "week", "op": "<="})
    scan = {"connector": "finance_fixture", "account": "bank:1234", "source_file": "s.ofx",
            "source_hash": "f" * 64, "handles": [],
            "records": [{"stream": "txn", "external_id": f"t-{i}", "journal_date": d,
                         "started_at": f"{d}T17:00:00Z", "direction": None, "title": "SHOP",
                         "value": v, "unit": "USD", "participants": []}
                        for i, (d, v) in enumerate((("2026-10-14", -70.0), ("2026-10-16", -50.0),
                                                    ("2026-10-20", -30.0)))],
            "coverage": [{"stream": "txn", "start": "2026-10-13", "end": "2026-10-19",
                          "basis": "OFX statement range"}]}
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    [g] = goals.progress(conn, cfg, TODAY)
    assert g["previous"]["value"] == 120.0 and g["previous"]["met"] is False
    assert g["current"]["value"] == 30.0 and g["current"]["met"] is None
    assert g["series"]["unit"] == "USD"


def test_a_contact_goal_names_one_person_or_says_why_not(conn, cfg):
    mom = entities.create(conn, "person", "Linda Rivera", aliases=["Mom"])["id"]
    entities.create(conn, "person", "Sam Okafor")
    entities.create(conn, "person", "Sam Rivera")
    _plan(conn, "2026-10-13", "Call Mom weekly", {"metric": "contact", "entity": "Mom",
                                                  "target": 1, "per": "week"})
    _plan(conn, "2026-10-13", "See Sam", {"metric": "contact", "entity": "Sam",
                                          "target": 1, "per": "week"})
    call = entries.create(conn, journal_date="2026-10-20", kind="event", title="Called Mom",
                          primary_category="social")
    entries.attach_entity(conn, call["id"], mom, "participant")
    conn.commit()
    by_title = {g["title"]: g for g in goals.progress(conn, cfg, TODAY)}
    ok = by_title["Call Mom weekly"]
    assert ok["series"]["key"] == f"contact:{mom}" and ok["current"]["met"] is True
    assert ok["refs"] == [("entries", call["uuid"])]
    assert "does not name exactly one person" in by_title["See Sam"]["error"]
    assert "never guessed" in goals.describe(by_title["See Sam"])


def test_a_daily_goal_counts_the_week_s_days(conn, cfg):
    _plan(conn, "2026-10-13", "8k steps", {"metric": "steps", "target": 8000, "per": "day"})
    for d, v in (("2026-10-19", 9000), ("2026-10-20", 7000), ("2026-10-21", 8500)):
        health.insert(conn, {"metric": "steps", "journal_date": d, "started_at": f"{d}T10:00:00Z",
                             "value": v, "unit": "count", "external_id": f"s-{d}",
                             "source_file": "x.zip"})
    conn.commit()
    [g] = goals.progress(conn, cfg, TODAY)
    assert g["agg"] == "sum" and g["current"]["label"] == "2026-10-21"
    assert g["current"]["met"] is True and g["previous"]["met"] is False
    assert g["week"] == {"days_met": 2, "days_known": 3, "start": "2026-10-19"}


# ---------------------------------------------------------------- CLI, review pack

def test_entry_edit_takes_data_and_the_domain_rule_holds(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    plan = entries.create(conn, journal_date="2026-10-13", kind="plan", title="Gym",
                          primary_category="health")
    task = entries.create(conn, journal_date="2026-10-13", kind="task", title="Passport",
                          primary_category="chores")
    conn.commit()
    conn.close()
    goal = '{"goal": {"metric": "workouts", "target": 3, "per": "week"}}'
    r = runner.invoke(app, ["entry", "edit", plan["uuid"], "--data", goal])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["entry", "edit", task["uuid"], "--data", goal])
    assert r.exit_code == 1 and "needs kind 'plan'" in r.output
    r = runner.invoke(app, ["entry", "edit", plan["uuid"], "--data", "not json"])
    assert r.exit_code == 1 and "not valid JSON" in r.output
    r = runner.invoke(app, ["entry", "edit", plan["uuid"], "--data", "[1]"])
    assert r.exit_code == 1 and "JSON object" in r.output
    r = runner.invoke(app, ["entry", "edit", plan["uuid"], "--data", "{}"])
    assert r.exit_code == 0
    conn = db_mod.connect(cfg.db_path, readonly=True)
    try:
        assert conn.execute("SELECT data FROM entries WHERE uuid=?",
                            (plan["uuid"],)).fetchone()["data"] is None
    finally:
        conn.close()


def test_the_review_pack_has_a_goals_section_in_every_lens(conn, cfg):
    _plan(conn, "2026-10-06", "Three gym sessions a week",
          {"metric": "workouts", "target": 3, "per": "week"})
    _workouts(conn, ["2026-10-06", "2026-10-08", "2026-10-10", "2026-10-13"],
              ("2026-10-06", "2026-10-19"))
    conn.commit()
    assert all("goals" in sections for sections in review.LENS_SECTIONS.values())
    period = review.resolve_period(cfg, week="2026-W42", today=TODAY)
    pack = review.build_pack(conn, cfg, period, lens="week")
    g = pack["goals"]
    assert g["decided_as_of"] == "2026-10-19" and len(g["items"]) == 1
    item = g["items"][0]
    assert item["id"].startswith("E") and pack["index"][item["id"]]["kind"] == "plan"
    assert [p["label"] for p in item["periods"]] == ["2026-W42"]
    assert item["periods"][0]["met"] is False and item["periods"][0]["value"] == 1.0
    assert item["line"] == ("Three gym sessions a week — workouts >= 3 sessions per week: "
                            "2026-W42: 1 of 3 (missed)")
    assert review.index_line(pack["index"][item["id"]]).startswith("entry ")
    md = review.render_markdown(pack)
    assert "## Goals (plans with a target; decided as of 2026-10-19)" in md
    assert f"- [{item['id']}] Three gym sessions a week" in md
    empty = review.build_pack(conn, cfg, review.resolve_period(cfg, week="2026-W30",
                                                              today=TODAY), lens="finance")
    assert empty["goals"]["items"] == [] and "- (no open goals)" in review.render_markdown(empty)

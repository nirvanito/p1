"""The weekly review pack: deterministic, cited, read-only.

The load-bearing claims: the period is the last full week unless told
otherwise, every id the pack hands out resolves to a real row, prior-period
deltas are computed against an equally long period, a lens narrows what is
shown, an empty week is a valid pack rather than an error, a saved brief is
never re-read as evidence, and building a pack writes nothing at all.
"""

import hashlib
import json
from datetime import date

import pytest
from conftest import cli_project, golden_draft
from typer.testing import CliRunner

from lifecrm import db as db_mod
from lifecrm import review
from lifecrm.cli import app
from lifecrm.config import load_config
from lifecrm.domain import (corrections, days, entities, entries, health,
                            measurements, observations as obs, reconcile)
from lifecrm.domain.util import DomainError, canonical_json
from lifecrm.doctor import run_checks
from lifecrm.importer import import_text
from test_reconcile import _ingest

runner = CliRunner()

TODAY = date(2026, 8, 5)            # a Wednesday: last full week is 2026-W31
WEEK = "2026-W31"                   # 2026-07-27 (Mon) .. 2026-08-02 (Sun)
OWNER = "+15125550100"
JAKE = "+15125550142"
MAYA = "+15125550187"
PRIYA = "priya@acme-robotics.example"


# ---------------------------------------------------------------- seeding

def _entry(conn, day, kind, title, primary, **kw):
    return entries.create(conn, journal_date=day, kind=kind, title=title,
                          primary_category=primary, **kw)


def _health(conn, metric, day, value, unit, n):
    health.insert(conn, {"metric": metric, "journal_date": day,
                         "started_at": f"{day}T12:00:00Z", "value": value,
                         "unit": unit, "external_id": f"{metric}-{n:03d}",
                         "source_file": "health_export.zip"})


def _msg(n, day, hhmm, stream, direction, parts, **kw):
    rec = {"stream": stream, "external_id": f"x-{n:03d}", "journal_date": day,
           "started_at": f"{day}T{hhmm}:00Z", "direction": direction,
           "participants": [{"kind": k, "value": v, "role": r} for k, v, r in parts]}
    rec.update(kw)
    return rec


def _scan():
    me_out = ("phone", OWNER, "sender")
    me_in = ("phone", OWNER, "recipient")
    claude_out = ("app_account", "claude:acct1", "sender")
    claude_in = ("app_account", "claude:acct1", "recipient")
    records = [
        _msg(1, "2026-07-28", "17:00", "imessage", "in",
             [("phone", JAKE, "sender"), me_in], body="Veracruz tonight?"),
        _msg(2, "2026-07-28", "17:05", "imessage", "out",
             [me_out, ("phone", JAKE, "recipient")], body="always"),
        # In a group, an incoming message is contact with its sender only.
        _msg(3, "2026-07-29", "20:00", "imessage", "in",
             [("phone", MAYA, "sender"), me_in, ("phone", JAKE, "recipient")],
             title="Lake crew", body="Castle this time"),
        _msg(4, "2026-07-30", "20:00", "imessage", "out",
             [me_out, ("phone", JAKE, "recipient"), ("phone", MAYA, "recipient")],
             title="Lake crew", body="Booking it"),
        _msg(5, "2026-07-22", "17:00", "imessage", "in",
             [("phone", JAKE, "sender"), me_in], body="prior week hello"),
        _msg(6, "2026-07-31", "23:00", "ios_call", "out",
             [me_out, ("phone", JAKE, "recipient")], value=300, unit="s"),
        _msg(7, "2026-07-29", "15:00", "calendar_event", None,
             [("email", PRIYA, "organizer")], title="1:1 Alex / Priya",
             ended_at="2026-07-29T15:30:00Z"),
        _msg(8, "2026-08-01", "05:00", "calendar_event", None, [],
             title="Launch week", ended_at="2026-08-02T05:00:00Z"),
        _msg(9, "2026-07-28", "22:00", "claude_chat", "out", [claude_out],
             title="Board deck structure", body="How should I structure it?",
             value_json=canonical_json({"conversation_id": "c-1", "turn": 0})),
        _msg(10, "2026-07-28", "22:01", "claude_chat", "in", [claude_in],
             title="Board deck structure", body="Open with a customer story.",
             value_json=canonical_json({"conversation_id": "c-1", "turn": 1})),
        _msg(11, "2026-08-02", "19:00", "claude_chat", "out", [claude_out],
             title="Return trip to Lake Bled", body="Five day itinerary?",
             value_json=canonical_json({"conversation_id": "c-2", "turn": 0})),
        _msg(12, "2026-07-30", "12:00", "note", None, [],
             title="Gift ideas for Maya", body="- ceramics class voucher"),
        _msg(13, "2026-07-28", "19:30", "txn", None, [],
             title="VERACRUZ ALL NATURAL", value=-34.50, unit="USD"),
        _msg(14, "2026-07-31", "19:30", "txn", None, [],
             title="VERACRUZ ALL NATURAL", value=-29.75, unit="USD"),
        _msg(15, "2026-07-30", "18:00", "txn", None, [],
             title="H-E-B #742 AUSTIN TX", value=-64.12, unit="USD"),
        _msg(16, "2026-07-31", "09:00", "txn", None, [],
             title="ACME ROBOTICS PAYROLL", value=4200.0, unit="USD"),
        _msg(17, "2026-07-22", "18:00", "txn", None, [],
             title="H-E-B #742 AUSTIN TX", value=-50.0, unit="USD"),
        # A bank that says the direction instead of signing the amount.
        _msg(18, "2026-07-29", "12:30", "txn", "out", [],
             title="CHIPOTLE 2231", value=12.0, unit="USD"),
    ]
    handles = [
        {"kind": "phone", "value": OWNER, "display_name": "you (this phone)"},
        {"kind": "phone", "value": JAKE, "display_name": "Jake Morales"},
        {"kind": "phone", "value": MAYA, "display_name": "Maya Chen"},
        {"kind": "email", "value": PRIYA, "display_name": "Priya Natarajan"},
        {"kind": "app_account", "value": "claude:acct1",
         "display_name": "Claude account"},
    ]
    return {"connector": "review_fixture", "account": OWNER,
            "source_file": "fixture", "source_hash": "f" * 64,
            "records": records, "handles": handles}


def seed(conn, cfg):
    """A week (2026-W31) with every kind of evidence, and a prior week to
    compare it with — all through the domain write paths."""
    reconcile.apply_diff(conn, _ingest(conn, cfg, golden_draft()))   # 2026-07-27
    jake = conn.execute("SELECT id FROM entities WHERE name='Jake'").fetchone()["id"]

    _entry(conn, "2026-07-28", "task", "Send the report follow-up to Dana",
           "work", due_at="2026-07-30")
    passport = _entry(conn, "2026-07-20", "task", "Renew passport", "chores")
    corrections.close_entry(conn, passport["uuid"], "done",
                            closed_at="2026-07-29T15:00:00Z")
    _entry(conn, "2026-07-15", "plan", "Visit Mom in San Antonio", "travel")
    old = _entry(conn, "2026-07-10", "task", "Long-finished chore", "chores")
    corrections.close_entry(conn, old["uuid"], "done", closed_at="2026-07-12")
    _entry(conn, "2026-07-29", "decision", "Lead the deck with the customer story",
           "work")
    groceries = _entry(conn, "2026-07-30", "event", "Groceries at H-E-B", "purchase",
                       secondary_categories=["food"])
    measurements.add(conn, entry_id=groceries["id"], metric="money",
                     numeric_value=64, unit="USD")
    dinner = _entry(conn, "2026-07-31", "event", "Dinner with Jake at Veracruz",
                    "social", secondary_categories=["food"])
    entries.attach_entity(conn, dinner["id"], jake, "participant")
    measurements.add(conn, journal_date="2026-07-29", metric="mood", numeric_value=9)

    # The prior week, for deltas.
    days.set_summary(conn, "2026-07-22", "A flat week.")
    measurements.add(conn, journal_date="2026-07-22", metric="mood", numeric_value=5)
    measurements.add(conn, journal_date="2026-07-22", metric="energy", numeric_value=4)

    # Someone not heard from in two months.
    linda = entities.create(conn, "person", "Linda Rivera")
    call = _entry(conn, "2026-06-01", "event", "Called Linda", "social")
    entries.attach_entity(conn, call["id"], linda["id"], "participant")

    n = 0
    period_sleep = [390, 360, 420, 450, 370, 480, 400]     # 6.5 h 6 h ... 6.67 h
    for i, minutes in enumerate(period_sleep):
        n += 1
        _health(conn, "sleep_session", f"2026-07-{27 + i:02d}" if i < 5
                else f"2026-08-{i - 4:02d}", minutes, "min", n)
    for i in range(7):
        n += 1
        _health(conn, "sleep_session", f"2026-07-{20 + i:02d}", 450, "min", n)
    for day_, steps in (("2026-07-27", 8000), ("2026-07-28", 10000),
                        ("2026-07-22", 6000)):
        n += 1
        _health(conn, "steps", day_, steps, "count", n)
    for day_, minutes in (("2026-07-27", 90), ("2026-07-30", 45),
                          ("2026-07-20", 60), ("2026-07-22", 60), ("2026-07-24", 60)):
        n += 1
        _health(conn, "exercise_session", day_, minutes, "min", n)
    for day_, kg in (("2026-07-27", 84.9), ("2026-08-01", 84.4)):
        n += 1
        _health(conn, "weight", day_, kg, "kg", n)
    for day_, bpm in (("2026-07-28", 62), ("2026-07-21", 58)):
        n += 1
        _health(conn, "resting_heart_rate", day_, bpm, "bpm", n)

    scan = _scan()
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    obs.link_handle(conn, "phone", JAKE, jake)
    conn.commit()


def pack_for(conn, cfg, lens="week", **kw):
    period = review.resolve_period(cfg, today=TODAY, **kw)
    return review.build_pack(conn, cfg, period, lens=lens)


# ---------------------------------------------------------------- period

@pytest.mark.parametrize("today, start, end, slug", [
    (date(2026, 8, 5), "2026-07-27", "2026-08-02", "2026-W31"),    # Wednesday
    (date(2026, 8, 3), "2026-07-27", "2026-08-02", "2026-W31"),    # Monday
    (date(2026, 8, 9), "2026-07-27", "2026-08-02", "2026-W31"),    # Sunday: week in progress
    (date(2026, 1, 1), "2025-12-22", "2025-12-28", "2025-W52"),    # across a year
])
def test_default_period_is_the_last_full_week(cfg, today, start, end, slug):
    p = review.resolve_period(cfg, today=today)
    assert (p["start"], p["end"], p["slug"], p["days"]) == (start, end, slug, 7)
    assert p["prior"]["end"] < p["start"] and p["prior"]["days"] == 7


def test_week_flag_and_custom_periods_carry_an_equally_long_prior(cfg):
    p = review.resolve_period(cfg, week="2026-w31", today=TODAY)
    assert (p["start"], p["end"], p["label"]) == ("2026-07-27", "2026-08-02",
                                                  "week 2026-W31")
    p = review.resolve_period(cfg, start="2026-07-01", end="2026-07-10", today=TODAY)
    assert p["prior"] == {"start": "2026-06-21", "end": "2026-06-30", "days": 10}
    assert p["slug"] == "2026-07-01_2026-07-10"
    p = review.resolve_period(cfg, days=3, today=TODAY)
    assert (p["start"], p["end"], p["label"]) == ("2026-08-03", "2026-08-05",
                                                  "last 3 days")
    p = review.resolve_period(cfg, start="2026-08-01", today=TODAY)
    assert p["end"] == "2026-08-05"


@pytest.mark.parametrize("kw, fragment", [
    ({"week": "2026-31"}, "not an ISO week"),
    ({"week": "2025-W53"}, "2025 has no week 53"),
    ({"week": "2026-W31", "days": 7}, "give one of"),
    ({"start": "2026-07-10", "end": "2026-07-01"}, "is before --start"),
    ({"end": "2026-07-01"}, "--end needs --start"),
    ({"start": "July 1"}, "is not a date"),
    ({"days": 0}, "--days must be"),
])
def test_bad_period_arguments_fail_with_one_line(cfg, kw, fragment):
    with pytest.raises(DomainError) as e:
        review.resolve_period(cfg, today=TODAY, **kw)
    assert fragment in str(e.value) and "\n" not in str(e.value)


# ---------------------------------------------------------------- contents

def test_week_pack_has_its_sections_and_no_advisor_ones(conn, cfg):
    seed(conn, cfg)
    pack = pack_for(conn, cfg)
    for name in review.LENS_SECTIONS["week"]:
        assert pack[name] is not None, name
    for name in ("finance", "career", "relationships"):
        assert name not in pack
    assert pack["empty"] is False
    assert pack["coverage"]["days_with_entries"] == 5
    assert pack["entries"]["by_category"]["food"] == 3
    assert pack["entries"]["by_kind"]["event"] == 7


def _walk_ids(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "id" and isinstance(v, str):
                found.add(v)
            elif k in ("entries", "events") and isinstance(v, list) \
                    and all(isinstance(x, str) for x in v):
                found.update(v)
            else:
                _walk_ids(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_ids(v, found)


def test_every_id_in_the_pack_resolves_to_a_real_row(conn, cfg):
    seed(conn, cfg)
    pack = pack_for(conn, cfg, lens="all")
    found: set[str] = set()
    _walk_ids({k: v for k, v in pack.items() if k != "index"}, found)
    assert found and found <= set(pack["index"])
    assert {c[0] for c in pack["index"]} == {"D", "E", "M", "H", "O"}
    for cid, item in pack["index"].items():
        assert item["id"] == cid
        if item["type"] == "entry":
            assert conn.execute("SELECT 1 FROM entries WHERE uuid=?",
                                (item["uuid"],)).fetchone(), cid
        elif item["type"] == "observation":
            assert conn.execute("SELECT 1 FROM observations WHERE uuid=?",
                                (item["uuid"],)).fetchone(), cid
        elif item["type"] == "observation_count":
            for u in [item.get("first_uuid"), item.get("last_uuid"),
                      *item.get("uuids", [])]:
                if u:
                    assert conn.execute("SELECT 1 FROM observations WHERE uuid=?",
                                        (u,)).fetchone(), cid
            for imp in item.get("imports", []):
                assert conn.execute("SELECT 1 FROM imports WHERE uuid=?",
                                    (imp,)).fetchone(), cid
        assert review.index_line(item)


def test_days_carry_summary_mood_energy_and_sleep_as_said(conn, cfg):
    seed(conn, cfg)
    days_ = {d["date"]: d for d in pack_for(conn, cfg)["days"]}
    assert set(days_) == {"2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30",
                          "2026-07-31"}
    monday = days_["2026-07-27"]
    assert (monday["mood"], monday["energy"], monday["sleep_hours"]) == (7, 6, 6.5)
    assert monday["summary"].startswith("Shipped the quarterly report")
    assert monday["entries"] == 6 and monday["id"].startswith("D")


def test_loops_opened_closed_open_and_overdue(conn, cfg):
    seed(conn, cfg)
    loops = pack_for(conn, cfg)["loops"]
    titles = {k: [e["title"] for e in v] for k, v in loops.items()
              if isinstance(v, list)}
    assert titles["opened"] == ["Send the report follow-up to Dana"]
    assert titles["closed"] == ["Renew passport"]
    assert loops["closed"][0]["closed_on"] == "2026-07-29"
    assert set(titles["open"]) == {"Send the report follow-up to Dana",
                                   "Visit Mom in San Antonio"}
    assert titles["overdue"] == ["Send the report follow-up to Dana"]
    assert "Long-finished chore" not in sum(titles.values(), [])


def test_a_task_due_on_the_last_day_is_overdue_only_once_the_day_has_passed(conn, cfg):
    _entry(conn, "2026-08-03", "task", "Due today", "chores", due_at="2026-08-05")
    conn.commit()
    running = pack_for(conn, cfg, days=3)          # 08-03 .. 08-05, today included
    assert running["loops"]["overdue"] == []
    elapsed = review.build_pack(conn, cfg, review.resolve_period(
        cfg, start="2026-08-03", end="2026-08-05", today=date(2026, 8, 7)))
    assert [e["title"] for e in elapsed["loops"]["overdue"]] == ["Due today"]


def test_journal_metrics_compare_with_the_prior_week(conn, cfg):
    seed(conn, cfg)
    metrics = {m["metric"]: m for m in pack_for(conn, cfg)["journal_metrics"]["metrics"]}
    mood = metrics["mood"]
    assert (mood["mean"], mood["days"]) == (8, 2)
    assert mood["prior"] == {"days": 1, "mean": 5} and mood["delta"] == 3
    assert metrics["energy"]["prior"]["mean"] == 4
    assert metrics["sleep_hours"]["prior"] is None
    assert metrics["mood"]["id"].startswith("M")


def test_health_trends_count_short_nights_sessions_and_weight_change(conn, cfg):
    seed(conn, cfg)
    h = {m["metric"]: m for m in pack_for(conn, cfg)["health"]["metrics"]}
    sleep = h["sleep_session"]
    assert sleep["days"] == 7 and sleep["short_nights"] == 2
    assert sleep["short_nights_prior"] == 0
    assert sleep["prior"]["mean"] == 7.5 and sleep["delta"] < 0
    assert sleep["source_files"] == ["health_export.zip"]
    assert sleep["tier"] == "observed" and sleep["id"].startswith("H")
    assert h["steps"]["mean"] == 9000 and h["steps"]["prior"]["mean"] == 6000
    assert (h["exercise_session"]["sessions"],
            h["exercise_session"]["sessions_prior"]) == (2, 3)
    assert h["weight"]["change_in_period"] == -0.5
    assert h["resting_heart_rate"]["delta"] == 4


def test_observation_counts_per_stream_with_the_prior_week(conn, cfg):
    seed(conn, cfg)
    s = pack_for(conn, cfg)["observations"]
    counts = {o["stream"]: (o["count"], o["prior_count"]) for o in s["streams"]}
    assert counts["imessage"] == (4, 1)
    assert counts["txn"] == (5, 1)
    assert counts["claude_chat"] == (3, 0)
    assert s["total"] == 16 and s["prior_total"] == 2


def test_top_people_resolve_linked_handles_and_keep_unresolved_names(conn, cfg):
    seed(conn, cfg)
    people = pack_for(conn, cfg)["observations"]["top_people"]
    names = [p["name"] for p in people]
    assert names[:2] == ["Jake", "Maya Chen"]
    assert "you (this phone)" not in names and "Claude account" not in names
    jake = people[0]
    assert jake["resolved"] and jake["entity_id"] is not None
    assert len(jake["entries"]) == 2 and jake["saw"] == 2
    # 2 direct messages + the group message he was sent; the group message
    # Maya sent him and you is contact with Maya, not with Jake.
    assert (jake["messages"]["count"], jake["messages"]["in"],
            jake["messages"]["out"]) == (3, 1, 2)
    assert jake["calls"]["count"] == 1 and jake["calls"]["minutes"] == 5
    maya = people[1]
    assert not maya["resolved"] and maya["handle"] == f"phone:{MAYA}"
    assert maya["messages"]["count"] == 2


def test_relationships_lists_who_went_quiet_with_a_citable_last_contact(conn, cfg):
    seed(conn, cfg)
    pack = pack_for(conn, cfg, lens="relationships")
    rel = pack["relationships"]
    quiet = {q["name"]: q for q in rel["quiet"]}
    assert set(quiet) == {"Linda Rivera"}
    linda = quiet["Linda Rivera"]
    assert (linda["last_contact"], linda["days_since"], linda["via"]) == \
        ("2026-06-01", 62, "entry")
    assert pack["index"][linda["id"]]["title"] == "Called Linda"
    priya = next(p for p in rel["people"] if p["name"] == "Priya Natarajan")
    assert priya["meetings"]["count"] == 1 and not priya["resolved"]
    assert review.build_pack(conn, cfg, review.resolve_period(
        cfg, week=WEEK), lens="relationships",
        quiet_weeks=10)["relationships"]["quiet"] == []


def test_a_handle_linked_to_a_merged_entity_still_counts_for_the_survivor(conn, cfg):
    seed(conn, cfg)
    jake = conn.execute("SELECT id FROM entities WHERE name='Jake'").fetchone()["id"]
    morales = entities.create(conn, "person", "Jake Morales")
    entities.merge(conn, jake, morales["id"])
    conn.commit()
    top = pack_for(conn, cfg)["observations"]["top_people"][0]
    assert top["name"] == "Jake Morales" and top["messages"]["count"] == 3


def test_finance_totals_by_the_banks_own_merchant_text(conn, cfg):
    seed(conn, cfg)
    fin = pack_for(conn, cfg, lens="finance")["finance"]
    assert fin["totals"] == [{"currency": "USD", "spent": 140.37, "received": 4200.0,
                              "count": 5, "prior_spent": 50.0, "delta_spent": 90.37}]
    assert [m["merchant"] for m in fin["merchants"]] == [
        "VERACRUZ ALL NATURAL", "H-E-B #742 AUSTIN TX", "CHIPOTLE 2231",
        "ACME ROBOTICS PAYROLL"]
    assert fin["merchants"][0]["spent"] == 64.25 and fin["merchants"][0]["count"] == 2
    assert [e["title"] for e in fin["journal_spending"]] == ["Groceries at H-E-B"]
    assert fin["journal_spending"][0]["money"] == [{"amount": 64, "unit": "USD"}]


def test_career_lists_work_decisions_and_meetings_by_attendee(conn, cfg):
    seed(conn, cfg)
    car = pack_for(conn, cfg, lens="career")["career"]
    assert {e["title"] for e in car["work_entries"]} == {
        "Shipped quarterly report draft to Dana",
        "Send the report follow-up to Dana",
        "Lead the deck with the customer story"}
    assert [e["title"] for e in car["decisions"]] == [
        "Lead the deck with the customer story"]
    m = car["meetings"]
    assert m["total"] == 2 and m["hours"] == 0.5     # the all-day block is untimed
    assert [(a["name"], a["count"], a["minutes"]) for a in m["by_attendee"]] == \
        [("Priya Natarajan", 1, 30)]


def test_a_room_or_a_card_on_a_contact_stream_is_never_counted_as_a_person(conn, cfg):
    scan = {"connector": "review_fixture", "account": OWNER, "source_file": "rooms",
            "source_hash": "e" * 64, "handles": [
                {"kind": "phone", "value": OWNER, "display_name": "you"},
                {"kind": "email", "value": PRIYA, "display_name": "Priya Natarajan"},
                {"kind": "place_id", "value": "room:4-north",
                 "display_name": "Room 4 North"},
                {"kind": "card", "value": "visa:4242", "display_name": "Visa 4242"}],
            "records": [
                _msg(1, "2026-07-29", "15:00", "calendar_event", None,
                     [("email", PRIYA, "organizer"),
                      ("place_id", "room:4-north", "participant")],
                     title="Planning", ended_at="2026-07-29T16:00:00Z"),
                _msg(2, "2026-07-30", "09:00", "ios_call", "out",
                     [("phone", OWNER, "sender"), ("card", "visa:4242", "recipient")],
                     value=60, unit="s")]}
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    pack = pack_for(conn, cfg, lens="all")
    names = {p["name"] for p in pack["relationships"]["people"]}
    assert names == {"Priya Natarajan"}
    assert [a["name"] for a in pack["career"]["meetings"]["by_attendee"]] == [
        "Priya Natarajan"]


def test_ai_chats_group_by_conversation_and_notes_list_titles(conn, cfg):
    seed(conn, cfg)
    t = pack_for(conn, cfg)["titles"]
    assert [(c["title"], c["turns"]) for c in t["ai_chats"]] == [
        ("Board deck structure", 2), ("Return trip to Lake Bled", 1)]
    assert [n["title"] for n in t["notes"]] == ["Gift ideas for Maya"]


# ---------------------------------------------------------------- lenses

def test_health_lens_keeps_only_entries_about_health(conn, cfg):
    seed(conn, cfg)
    pack = pack_for(conn, cfg, lens="health")
    assert set(pack["sections"]) == set(review.LENS_SECTIONS["health"])
    cats = review.LENS_CATEGORIES["health"]
    assert pack["entries"]["items"]
    for e in pack["entries"]["items"]:
        assert set(e["categories"]) & cats, e["title"]
    assert "observations" not in pack and "finance" not in pack
    assert pack["loops"]["open"] == []              # no health loops in the seed
    assert any("lens health" in n for n in pack["notes"])


def test_relationships_lens_keeps_entries_that_name_a_person(conn, cfg):
    seed(conn, cfg)
    titles = {e["title"] for e in pack_for(conn, cfg, lens="relationships")
              ["entries"]["items"]}
    assert "Shipped quarterly report draft to Dana" in titles    # names Dana
    assert "Dinner with Jake at Veracruz" in titles              # social
    assert "Breakfast: scrambled eggs and toast" not in titles


def test_all_lens_holds_every_section(conn, cfg):
    seed(conn, cfg)
    pack = pack_for(conn, cfg, lens="all")
    assert all(pack[name] is not None for name in review.SECTIONS)


def test_unknown_lens_is_refused(conn, cfg):
    with pytest.raises(DomainError, match="must be one of"):
        review.build_pack(conn, cfg, review.resolve_period(cfg, today=TODAY),
                          lens="astrology")


# ---------------------------------------------------------------- edge cases

def test_an_empty_week_is_a_valid_pack_that_says_so(conn, cfg):
    pack = pack_for(conn, cfg, lens="all")
    assert pack["empty"] is True
    assert pack["days"] == [] and pack["entries"]["total"] == 0
    assert pack["health"]["metrics"] == [] and pack["index"] == {}
    assert any("nothing was recorded" in n for n in pack["notes"])
    text = review.render_markdown(pack)
    assert "nothing was recorded" in text and "(none)" in text


def test_a_quiet_week_still_shows_loops_left_open_before_it(conn, cfg):
    _entry(conn, "2026-07-01", "task", "Fix the bike", "chores", due_at="2026-07-15")
    conn.commit()
    pack = pack_for(conn, cfg)
    assert pack["empty"] is True
    assert [e["title"] for e in pack["loops"]["overdue"]] == ["Fix the bike"]


def test_a_pre_observed_tier_database_gets_the_confirmed_sections(cfg, tmp_path):
    from conftest import init_v1_db
    mig = tmp_path / "mig"
    mig.mkdir()
    for f in sorted(db_mod.MIGRATIONS_DIR.glob("000[23]_*.sql")):
        (mig / f.name).write_bytes(f.read_bytes())
    conn = init_v1_db(cfg.db_path)
    db_mod.migrate(conn, migrations_dir=mig)
    assert db_mod.schema_version(conn) == 3
    _entry(conn, "2026-07-28", "event", "Walked the dog", "exercise")
    conn.commit()
    pack = pack_for(conn, cfg, lens="all")
    conn.close()
    assert pack["observed_tier"] is False
    assert pack["entries"]["total"] == 1
    assert pack["observations"] is None and pack["titles"] is None
    assert pack["finance"]["merchants"] == []
    assert any("schema below v4" in n for n in pack["notes"])


def test_the_pack_is_deterministic(conn, cfg):
    seed(conn, cfg)
    a = json.dumps(pack_for(conn, cfg, lens="all"), sort_keys=True)
    b = json.dumps(pack_for(conn, cfg, lens="all"), sort_keys=True)
    ro = db_mod.connect(cfg.db_path, readonly=True)
    try:
        c = json.dumps(pack_for(ro, cfg, lens="all"), sort_keys=True)
    finally:
        ro.close()
    assert a == b == c
    assert review.render_markdown(json.loads(a)) == review.render_markdown(json.loads(c))


def test_building_a_pack_never_writes_the_database(conn, cfg):
    seed(conn, cfg)
    before = conn.total_changes
    audit = conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"]
    for lens in review.LENSES:
        pack_for(conn, cfg, lens=lens)
    assert conn.total_changes == before
    assert conn.execute("SELECT COUNT(*) c FROM audit_log").fetchone()["c"] == audit
    assert not conn.in_transaction


# ---------------------------------------------------------------- briefs

BRIEF = ("A strong Monday: you shipped the report [E2] and went bouldering "
         "with Jake [E4, H1]. Mood ran higher than last week [M1].")


def test_check_citations_finds_unknown_and_missing_ids(conn, cfg):
    seed(conn, cfg)
    pack = pack_for(conn, cfg)
    assert review.check_citations(BRIEF, pack) == {
        "cited": ["E2", "E4", "H1", "M1"], "unknown": [], "uncited": False}
    bad = review.check_citations("Sleep was perfect [H99][E1].", pack)
    assert bad["unknown"] == ["H99"] and bad["cited"] == ["E1"]
    assert review.check_citations("No ids at all.", pack)["uncited"] is True


def test_the_sources_footer_spells_out_every_citation_durably(conn, cfg):
    seed(conn, cfg)
    pack = pack_for(conn, cfg)
    footer = review.sources_footer(pack, ["E2", "H1", "O1"])
    e2 = pack["index"]["E2"]
    assert e2["uuid"] in footer and e2["title"] in footer
    assert "health_records sleep_session" in footer
    assert "health_export.zip" in footer
    assert footer.count("\n[") == 3


def test_a_saved_brief_goes_through_the_gate_and_is_never_reread_as_evidence(conn, cfg):
    """The /review-week save path, step for step: import the sourced brief
    as a manual source, extract one reflection entry citing it, apply —
    and doctor stays green."""
    from test_doctor import _seed
    _seed(conn, cfg)                                   # the golden 2026-07-27
    pack = pack_for(conn, cfg)
    brief = ("You shipped the report [E2] and went bouldering with Jake "
             "[E4]; mood was a 7 [D1].")
    cited = review.check_citations(brief, pack)
    assert cited["unknown"] == [] and not cited["uncited"]
    text = brief + "\n\n" + review.sources_footer(pack, cited["cited"])
    src = import_text(conn, cfg, text, journal_date=pack["period"]["end"],
                      source_type="manual")
    draft = {
        "contract_version": 2, "journal_date": pack["period"]["end"],
        "sources": [{"source_id": src["source_id"]}],
        "entries": [{
            "kind": "reflection", "categories": {"primary": "reflection"},
            "title": f"Weekly review {WEEK}",
            "body": "Shipped the report, climbed with Jake, mood up on last week.",
            "data": {"derived_from": review.BRIEF_MARK, "lens": "week",
                     "period": WEEK},
            "provenance": {"source_id": src["source_id"], "char_start": 0,
                           "char_end": len(brief)}}]}
    diff = _ingest(conn, cfg, draft)
    assert [i["action"] for i in diff["items"]] == ["add"]
    reconcile.apply_diff(conn, diff)
    conn.commit()
    assert run_checks(conn, cfg)["failures"] == 0

    again = pack_for(conn, cfg)
    assert [b["title"] for b in again["briefs"]] == [f"Weekly review {WEEK}"]
    assert again["briefs"][0]["lens"] == "week"
    assert again["entries"]["total"] == pack["entries"]["total"]
    assert again["coverage"]["days_with_entries"] == pack["coverage"]["days_with_entries"]
    assert all(e["kind"] != "reflection" for e in again["entries"]["items"])


@pytest.mark.parametrize("skill", ["review-week", "advise"])
def test_each_skill_s_tracked_mirror_is_identical(skill):
    from conftest import PROJECT_ROOT
    claude = PROJECT_ROOT / ".claude" / "skills" / skill / "SKILL.md"
    agents = PROJECT_ROOT / ".agents" / "skills" / skill / "SKILL.md"
    assert claude.read_bytes() == agents.read_bytes()
    text = claude.read_text(encoding="utf-8")
    assert text.startswith(f"---\nname: {skill}\n")
    # The save path the skill prescribes is the one the gate test exercises.
    assert f'"derived_from": "{review.BRIEF_MARK}"' in text
    assert "never apply without" in text.lower()


# ---------------------------------------------------------------- the persona

def test_the_demo_persona_s_short_sleep_week_shows_in_the_health_trends(conn, cfg):
    """persona.health_series dips in the days before the anchor — exactly
    what the health advisor is meant to notice."""
    from lifecrm.demo import persona
    anchor = date(2026, 10, 21)
    for n, d in enumerate(persona.health_series(anchor)):
        day_ = d.date.isoformat()
        _health(conn, "sleep_session", day_, round(d.sleep_hours * 60), "min", 3 * n)
        _health(conn, "steps", day_, d.steps, "count", 3 * n + 1)
        _health(conn, "resting_heart_rate", day_, d.resting_hr, "bpm", 3 * n + 2)
    conn.commit()
    period = review.resolve_period(cfg, today=anchor)     # offsets -9 .. -3
    h = {m["metric"]: m for m in review.build_pack(
        conn, cfg, period, lens="health")["health"]["metrics"]}
    assert h["sleep_session"]["short_nights"] >= 4
    assert h["sleep_session"]["short_nights_prior"] == 0
    assert h["sleep_session"]["delta"] < 0
    assert h["resting_heart_rate"]["delta"] > 0


# ---------------------------------------------------------------- CLI

def _cli_seeded(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    cfg = load_config(root)
    conn = db_mod.connect(cfg.db_path)
    try:
        seed(conn, cfg)
    finally:
        conn.close()
    return root, cfg


def _db_hash(cfg):
    return hashlib.sha256(cfg.db_path.read_bytes()).hexdigest()


def test_cli_writes_the_pack_files_and_prints_their_paths(tmp_path, monkeypatch):
    root, cfg = _cli_seeded(tmp_path, monkeypatch)
    before = _db_hash(cfg)
    r = runner.invoke(app, ["review", "--week", WEEK])
    assert r.exit_code == 0, r.output
    md = cfg.staging_dir / f"review-week-{WEEK}.md"
    js = cfg.staging_dir / f"review-week-{WEEK}.json"
    assert str(md) in r.output and md.exists() and js.exists()
    assert "loops: 1 opened, 1 closed, 2 open, 1 overdue" in r.output
    text = md.read_text(encoding="utf-8")
    assert text.startswith(f"# Review pack — week {WEEK}")
    assert "[H1] sleep" in text and "Top people by contact:" in text
    assert json.loads(js.read_text(encoding="utf-8"))["period"]["slug"] == WEEK
    assert _db_hash(cfg) == before


def test_cli_write_only_prints_just_the_paths(tmp_path, monkeypatch):
    root, cfg = _cli_seeded(tmp_path, monkeypatch)
    r = runner.invoke(app, ["review", "--lens", "all", "--week", WEEK, "--write-only"])
    assert r.exit_code == 0, r.output
    lines = r.output.strip().splitlines()
    assert lines == [str(cfg.staging_dir / f"review-all-{WEEK}.md"),
                     str(cfg.staging_dir / f"review-all-{WEEK}.json")]
    text = (cfg.staging_dir / f"review-all-{WEEK}.md").read_text(encoding="utf-8")
    for heading in ("## Relationships", "## Career", "## Money"):
        assert heading in text


def test_cli_json_prints_the_pack_and_writes_nothing(tmp_path, monkeypatch):
    root, cfg = _cli_seeded(tmp_path, monkeypatch)
    r = runner.invoke(app, ["review", "--lens", "finance", "--week", WEEK, "--json"])
    assert r.exit_code == 0, r.output
    pack = json.loads(r.output)
    assert pack["lens"] == "finance" and pack["finance"]["totals"][0]["spent"] == 140.37
    assert not list(cfg.staging_dir.glob("review-*"))


def test_cli_check_verifies_a_brief_and_writes_it_with_sources(tmp_path, monkeypatch):
    root, cfg = _cli_seeded(tmp_path, monkeypatch)
    brief = tmp_path / "brief.md"
    brief.write_text("﻿" + BRIEF, encoding="utf-8")       # a BOM is tolerated
    out = tmp_path / "brief-sourced.txt"
    r = runner.invoke(app, ["review", "--week", WEEK, "--check", str(brief),
                            "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "cites 4 pack item(s): E2, E4, H1, M1" in r.output
    assert f"--type manual --date 2026-08-02" in r.output
    saved = out.read_text(encoding="utf-8")
    assert saved.startswith(BRIEF) and "Sources (lifecrm review pack" in saved
    r = runner.invoke(app, ["import", str(out), "--type", "manual",
                            "--date", "2026-08-02"])
    assert r.exit_code == 0, r.output

    brief.write_text("Sleep was flawless [H42].", encoding="utf-8")
    r = runner.invoke(app, ["review", "--week", WEEK, "--check", str(brief)])
    assert r.exit_code == 1 and "H42" in r.output
    brief.write_text("No citations here.", encoding="utf-8")
    r = runner.invoke(app, ["review", "--week", WEEK, "--check", str(brief), "--json"])
    assert r.exit_code == 1 and json.loads(r.output)["uncited"] is True


@pytest.mark.parametrize("args, fragment", [
    (["--lens", "astrology"], "must be one of"),
    (["--week", "2026-W31", "--days", "7"], "give one of"),
    (["--json", "--write-only"], "pick one"),
    (["--out", "x.txt"], "--out goes with --check"),
    (["--check", "missing-brief.md"], "file not found"),
])
def test_cli_refuses_bad_arguments_with_one_line(tmp_path, monkeypatch, args, fragment):
    _cli_seeded(tmp_path, monkeypatch)
    r = runner.invoke(app, ["review", *args])
    assert r.exit_code == 1 and fragment in r.output

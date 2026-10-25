"""On this day, journal prompts, highlights, meeting prep, gift ideas and
anniversaries — the read-side features that ride the nudges pack.

The load-bearing claims: each section is built from the records alone and
cites every item to a row the pack's index names; a brief written from the
pack passes `nudges.check` (years ago included); a prompt is left out when
the day's journal already names its person; the highlight rotation is fixed
and comes round; prep belongs to the people on an agenda event and never the
owner; a gift idea rides the birthday nudge; an anniversary is a confirmed
nudge; the data shapes are refused on every write path.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import golden_draft
from feature_seed import (JAKE, OWNER, PRIYA, TODAY, apply, entry, people, rec, sms,
                          summary, txn)

from lifecrm import contract, highlights, memories, nudges, prompts
from lifecrm.domain import corrections, entries
from lifecrm.domain.util import DomainError
from lifecrm.web import api

NOW_DAY = TODAY.isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


@pytest.fixture
def world(conn, cfg):
    who = people(conn)
    jake, linda, priya = who["Jake Morales"], who["Linda Rivera"], who["Priya Natarajan"]
    # on this day: 2024 and 2023 hold something on Oct 21; 2025 a meeting with Jake
    summary(conn, "2024-10-21", "Hiked Enchanted Rock at sunrise.")
    hike = entry(conn, "2024-10-21", "event", "Hiked Enchanted Rock", "exercise")
    apply(conn, [rec("photo", "2023-10-21", f"15:{i:02d}", [],
                     title=f"IMG_{i}.jpg", value_json={"lat": 46.36, "lon": 14.09})
                 for i in range(3)]
          + [rec("place_visit", "2023-10-21", "14:00", [], title="Lake Bled",
                 value_json={"lat": 46.3625, "lon": 14.0938, "place_id": "pid-bled"})]
          + [rec("calendar_event", "2025-10-21", "18:00",
                 [("phone", JAKE, "participant"), ("phone", OWNER, "participant")],
                 title="Dinner with Jake", ended_at="2025-10-21T20:00:00Z",
                 value_json={"calendar": "personal", "all_day": False})],
          source="otd")
    # yesterday's moments
    apply(conn, [rec("place_visit", YESTERDAY, "13:00", [], title="Barton Springs",
                     value_json={"lat": 30.2640, "lon": -97.7713, "place_id": "pid-barton"}),
                 rec("android_call", YESTERDAY, "19:00", [("phone", JAKE, "participant")],
                     direction="out", value=1500.0, unit="s"),
                 rec("calendar_event", YESTERDAY, "16:00",
                     [("email", PRIYA, "organizer"), ("phone", OWNER, "participant")],
                     title="1:1 Alex / Priya", ended_at=f"{YESTERDAY}T16:30:00Z",
                     value_json={"calendar": "work", "all_day": False}),
                 rec("workout", YESTERDAY, "07:00", [], title="Morning Run", value=32.0,
                     unit="min", origin_app="strava")]
          + [rec("photo", YESTERDAY, f"13:{i:02d}", [], title=f"P{i}.jpg",
                 value_json={"lat": 30.264, "lon": -97.771}) for i in range(9)],
          source="yday")
    # the agenda: coffee with Jake today; his texts; a loop and a gift idea
    apply(conn, [rec("calendar_event", NOW_DAY, "15:00",
                     [("phone", JAKE, "participant"), ("phone", OWNER, "participant")],
                     title="Coffee with Jake", ended_at=f"{NOW_DAY}T16:00:00Z",
                     value_json={"calendar": "personal", "all_day": False}),
                 sms("2026-10-15", "18:00", JAKE, "see you Thursday?"),
                 sms("2026-10-16", "09:00", JAKE, "yes! 3pm works", direction="out")],
          source="agenda")
    drill = entry(conn, "2026-10-10", "task", "Return Jake's drill", "chores",
                  mentions=[jake], due_at="2026-10-25")
    gift = entry(conn, "2026-10-12", "idea", "A Kindle Paperwhite for Jake", "purchase",
                 mentions=[jake], role="subject", data={"gift": True})
    anniv = entry(conn, "2026-09-01", "fact", "Mom and Dad's wedding anniversary", "social",
                  mentions=[linda], role="subject",
                  data={"anniversary": {"date": "1988-10-24", "what": "wedding"}})
    # highlights
    apply(conn, [rec("highlight", "2026-09-0" + str(i), "21:00", [],
                     title="The Overstory (Richard Powers)", body=f"highlight number {i}",
                     value_json={"kind": "highlight", "location": f"{i * 10}-{i * 10 + 2}"})
                 for i in range(1, 6)], source="kindle")
    conn.commit()
    return {"jake": jake, "linda": linda, "priya": priya, "hike": hike, "drill": drill,
            "gift": gift, "anniv": anniv}


def _pack(conn, cfg):
    return nudges.build(conn, cfg, today=TODAY,
                        now=__import__("datetime").datetime(2026, 10, 21, 17, 0))


# ---------------------------------------------------------------- on this day

def test_target_dates_and_the_vault_links_handle_february_29():
    assert memories.target_dates(date(2026, 3, 1), 2023) == \
        [date(2025, 3, 1), date(2024, 3, 1), date(2023, 3, 1)]
    # a common year's Feb 28 brings back the leap years' Feb 29 too
    assert memories.target_dates(date(2025, 2, 28), 2023) == \
        [date(2024, 2, 28), date(2024, 2, 29), date(2023, 2, 28)]
    assert memories.target_dates(date(2028, 2, 29), 2023) == [date(2024, 2, 29)]
    days = ["2023-02-28", "2024-02-28", "2024-02-29", "2025-02-28", "2025-03-01"]
    assert memories.same_day_other_years("2024-02-29", days) == ["2025-02-28", "2023-02-28"]
    assert memories.same_day_other_years("2025-02-28", days) == ["2024-02-29", "2024-02-28",
                                                                 "2023-02-28"]
    assert memories.same_day_other_years("2025-03-01", days) == []


def test_on_this_day_brings_back_both_tiers_newest_year_first(conn, cfg, world):
    otd = memories.on_this_day(conn, cfg, TODAY)
    assert [y["year"] for y in otd["years"]] == [2025, 2024, 2023]
    y2025, y2024, y2023 = otd["years"]
    assert y2024["summary"] == "Hiked Enchanted Rock at sunrise."
    assert [e["title"] for e in y2024["entries"]] == ["Hiked Enchanted Rock"]
    assert y2024["years_ago"] == 2 and y2023["years_ago"] == 3
    texts = [r["text"] for r in y2023["records"]]
    assert texts[0] == "3 photos" and "at Lake Bled" in texts
    assert y2025["records"][0]["text"] == "Dinner with Jake with Jake Morales"


def test_the_pack_cites_on_this_day_and_a_brief_with_years_ago_checks(conn, cfg, world):
    pack = _pack(conn, cfg)
    otd = pack["on_this_day"]
    assert otd["enabled"] and [y["year"] for y in otd["years"]] == [2025, 2024, 2023]
    items = otd["years"][1]["items"]
    assert items[0]["id"].startswith("D") and items[1]["id"].startswith("E")
    lines = nudges.lines(pack)
    photo = next(i for i in otd["years"][2]["items"] if i["text"] == "3 photos")
    assert lines[photo["id"]].startswith("observations: photos taken on 2023-10-21 · 3")
    brief = (f"Two years ago today you hiked Enchanted Rock [{items[1]['id']}], and on "
             f"2023-10-21 you took 3 photos [{photo['id']}].")
    result = nudges.check(brief, pack)
    assert not result["unknown"] and not result["unsupported"], result
    rendered = "\n".join(nudges.render_lines(pack))
    assert "On this day (10-21 in earlier years):" in rendered
    assert "2024-10-21 (2 years ago):" in rendered


def test_the_day_view_carries_on_this_day_for_any_date(conn, cfg, world):
    d = api.day(conn, cfg, "2026-10-21")
    assert [y["year"] for y in d["on_this_day"]["years"]] == [2025, 2024, 2023]
    assert api.day(conn, cfg, "2026-10-22")["on_this_day"]["years"] == []


# ---------------------------------------------------------------- prompts

def test_prompts_name_yesterdays_moments_and_skip_what_the_journal_covers(conn, cfg, world):
    data = prompts.moments(conn, cfg, YESTERDAY)
    kinds = [p["kind"] for p in data["prompts"]]
    assert kinds == ["first_visit", "meeting", "long_call", "photos", "workout"]
    texts = {p["kind"]: p["text"] for p in data["prompts"]}
    assert texts["first_visit"] == "a first recorded visit to Barton Springs"
    assert texts["meeting"] == "1:1 Alex / Priya with Priya Natarajan"
    assert texts["long_call"] == "a 25-minute call with Jake Morales"
    assert texts["photos"] == "9 photos taken"
    assert texts["workout"] == "a workout: Morning Run"
    # the journal names Jake for that day: the call is covered, never prompted
    entry(conn, YESTERDAY, "event", "Long call with Jake", mentions=[world["jake"]])
    entry(conn, YESTERDAY, "event", "Ran 5k", "exercise")
    conn.commit()
    again = prompts.moments(conn, cfg, YESTERDAY)
    assert [p["kind"] for p in again["prompts"]] == ["first_visit", "meeting", "photos"]
    assert again["covered"] == 2
    # a second visit is not a first one
    assert not any(p["kind"] == "first_visit"
                   for p in prompts.moments(conn, cfg, NOW_DAY)["prompts"])


def test_an_unusual_spending_day_is_a_prompt(conn, cfg, world):
    base = [txn((TODAY - timedelta(days=d)).isoformat(), "H-E-B #42", 30.0)
            for d in range(10, 40, 3)]
    apply(conn, base + [txn(NOW_DAY, "BEST BUY 0042", 480.0)], source="bank")
    conn.commit()
    data = prompts.moments(conn, cfg, NOW_DAY)
    spend = [p for p in data["prompts"] if p["kind"] == "spend"]
    assert spend and spend[0]["text"].startswith("spending of 480.00 USD across 1 transaction(s)")
    assert spend[0]["record"]["median"] == 30.0


def test_the_pack_holds_yesterdays_prompts_cited(conn, cfg, world):
    pack = _pack(conn, cfg)
    pr = pack["prompts"]
    assert pr["enabled"] and pr["date"] == YESTERDAY
    assert all(it["id"] in pack["index"] for it in pr["items"])
    photos = next(it for it in pr["items"] if it["kind"] == "photos")
    assert nudges.lines(pack)[photos["id"]].startswith(f"observations: photos on {YESTERDAY} · 9")


# ---------------------------------------------------------------- highlights

def test_the_highlight_rotation_is_fixed_and_comes_round(conn, cfg, world):
    seen = []
    for i in range(5):
        day = date(2026, 1, 1) + timedelta(days=i)
        got = highlights.for_day(conn, day, per_day=1)
        assert got == highlights.for_day(conn, day, per_day=1)      # same day, same pick
        seen.append(got["items"][0]["uuid"])
    assert len(set(seen)) == 5                  # five days, five different highlights
    six = highlights.for_day(conn, date(2026, 1, 6), per_day=1)["items"][0]["uuid"]
    assert six == seen[0]                       # and round again
    pack = _pack(conn, cfg)
    assert len(pack["highlights"]["items"]) == 3 and pack["highlights"]["total"] == 5


# ---------------------------------------------------------------- meeting prep

def test_prep_sits_under_the_agenda_event_for_each_person_on_it(conn, cfg, world):
    pack = _pack(conn, cfg)
    coffee = next(a for a in pack["agenda"] if a["title"] == "Coffee with Jake")
    assert len(coffee["prep"]) == 1
    prep = pack["index"][coffee["prep"][0]]
    assert prep["type"] == "prep" and prep["name"] == "Jake Morales"
    text = "\n".join(prep["lines"])
    assert 'last message from them 2026-10-15: "see you Thursday?"' in text
    assert 'last message from you 2026-10-16: "yes! 3pm works"' in text
    assert "open task: Return Jake's drill, due 2026-10-25" in text
    assert "birthday in 9 day(s), 2026-10-30, turning 36" in text
    assert "gift idea you noted 2026-10-12: A Kindle Paperwhite for Jake" in text
    # every line rests on ids the pack holds
    for cid in prep["rests_on"]:
        assert cid in pack["index"]
    brief = (f"Before coffee, note Jake texted on 2026-10-15 [{coffee['prep'][0]}] and his "
             f"birthday is in 9 days [{coffee['prep'][0]}].")
    assert not nudges.check(brief, pack)["unsupported"]
    rendered = "\n".join(nudges.render_lines(pack))
    assert "before you meet Jake Morales:" in rendered


def test_the_owner_never_gets_a_prep_and_a_cancelled_event_none(conn, cfg, world):
    apply(conn, [rec("calendar_event", NOW_DAY, "20:00",
                     [("phone", JAKE, "participant")], title="Cancelled thing",
                     ended_at=f"{NOW_DAY}T21:00:00Z",
                     value_json={"calendar": "personal", "status": "cancelled"})],
          source="cancel")
    conn.commit()
    pack = _pack(conn, cfg)
    names = {pack["index"][p]["name"] for a in pack["agenda"] for p in a["prep"]}
    assert names == {"Jake Morales"}
    cancelled = next(a for a in pack["agenda"] if a["title"] == "Cancelled thing")
    assert cancelled["prep"] == []


# ---------------------------------------------------------------- gifts, anniversaries

def test_gift_ideas_ride_the_birthday_nudge(conn, cfg, world):
    pack = _pack(conn, cfg)
    bday = next(n for n in pack["nudges"] if n["kind"] == "birthday")
    assert "gift ideas you noted: A Kindle Paperwhite for Jake (2026-10-12)" in bday["detail"]
    assert any(pack["index"][i].get("uuid") == world["gift"]["uuid"] for i in bday["ids"])
    # given or closed ideas leave the birthday; given ones stay on the person page
    corrections.close_entry(conn, world["gift"]["uuid"], "done")
    conn.commit()
    pack = _pack(conn, cfg)
    bday = next(n for n in pack["nudges"] if n["kind"] == "birthday")
    assert "gift ideas" not in bday["detail"]


def test_an_anniversary_is_a_confirmed_nudge(conn, cfg, world):
    pack = _pack(conn, cfg)
    ann = next(n for n in pack["nudges"] if n["kind"] == "anniversary")
    assert ann["title"] == "Wedding anniversary with Linda Rivera is in 3 days (2026-10-24), 38 years"
    assert ann["tier"] == "confirmed" and ann["key"] == f"anniversary:{world['anniv']['uuid']}:2026"
    order = [n["kind"] for n in pack["nudges"]]
    assert order.index("birthday") < order.index("anniversary")


def test_anniversaries_reach_the_reminders_calendar(conn, cfg, world):
    from lifecrm import reminders
    ann = [c for c in reminders.components(conn, cfg, today=TODAY)
           if c["kind"] == "anniversary"]
    assert [c["day"].isoformat() for c in ann] == ["2026-10-24", "2027-10-24"]
    assert ann[0]["summary"] == "Wedding anniversary with Linda Rivera, 38 years"
    assert ann[1]["summary"].endswith("39 years")
    assert ann[0]["key"] == f"anniversary:{world['anniv']['uuid']}:2026"
    cfg.extra["features"] = {"gift_ideas": False}
    assert not [c for c in reminders.components(conn, cfg, today=TODAY)
                if c["kind"] == "anniversary"]


def test_gift_and_anniversary_shapes_are_refused_on_every_write_path(conn, cfg):
    with pytest.raises(DomainError, match="data.gift.status must be one of"):
        entries.create(conn, journal_date="2026-10-01", kind="idea", title="x",
                       primary_category="other", data={"gift": {"status": "maybe"}})
    with pytest.raises(DomainError, match="data.anniversary.date must be"):
        entries.create(conn, journal_date="2026-10-01", kind="fact", title="x",
                       primary_category="other", data={"anniversary": {"date": "June 8"}})
    ok = entries.create(conn, journal_date="2026-10-01", kind="fact", title="x",
                        primary_category="other",
                        data={"anniversary": {"date": "--02-29", "what": "first date"}})
    with pytest.raises(DomainError, match="data.gift"):
        corrections.edit_entry(conn, ok["uuid"], {"data": {"gift": "yes"}})
    draft = golden_draft()
    draft["entries"][0]["data"] = {"anniversary": {"date": "nope"}}
    errs = contract.validate_draft(conn, draft)
    assert any("data.anniversary.date" in e for e in errs), errs

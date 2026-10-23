"""The web UI's reads for the newer sources: email (and every messaging app)
as contact on People, the recent emails, a person's LinkedIn position, the
day view's activity (workouts, rides, food log, listening, searches), the
Health page's workouts per week and nutrient summary, and who-lines for
email and payments. Reads only; the page renders all of it as text."""

import re

from lifecrm import recall
from lifecrm.config import PROJECT_ROOT
from lifecrm.web import api
from test_more_fronts_review import ME_EM, _apply, _rec, seed

PAGE = (PROJECT_ROOT / "lifecrm" / "web" / "index.html").read_text(encoding="utf-8")


def seed_more(conn):
    ids = seed(conn)
    # A Sunday with no daily nutrition summary: the food log is summed.
    _apply(conn, "rides", None, [
        _rec(60, "ride", "2026-07-26", "01:10", title="Uber to Uchi",
             body="from Home to Uchi", value=16.87, unit="usd", origin_app="uber",
             detail={"product": "UberX"}),
        _rec(61, "food_order", "2026-07-26", "02:40", title="Chipotle",
             body="1 × Burrito Bowl", value=18.95, unit="usd", origin_app="ubereats"),
    ])
    _apply(conn, "nutrition-log", None, [
        _rec(62, "food_log", "2026-07-26", "13:00", title="Turkey Chili",
             body="1 bowl · Lunch", value=600.0, unit="kcal", origin_app="cronometer",
             detail={"nutrients": {"energy": [600.0, "kcal"], "protein": [40.0, "g"]}}),
        _rec(63, "food_log", "2026-07-26", "19:00", title="Salmon",
             body="1 fillet · Dinner", value=900.0, unit="kcal", origin_app="cronometer",
             detail={"nutrients": {"energy": [900.0, "kcal"], "protein": [55.5, "g"]}}),
    ])
    _apply(conn, "google_activity", None, [
        _rec(64, "web_search", "2026-07-26", "15:00", title="uchi menu <b>omakase</b>",
             origin_app="google_search"),
        _rec(65, "web_search", "2026-07-26", "15:05", title="uchi parking",
             origin_app="google_search"),
        _rec(66, "music_play", "2026-07-26", "16:00", title="Be Sweet",
             body="Japanese Breakfast", value=10.0, unit="min", origin_app="spotify",
             detail={"plays": 3, "kind": "music"}),
    ])
    conn.commit()
    return ids


def test_email_counts_as_contact_on_people_but_a_cc_does_not(conn, cfg):
    seed_more(conn)
    people = {p["name"]: p for p in api.people(conn, cfg)["people"]}
    # Rosa wrote in June and was only copied in July
    assert people["Rosa Treviño"]["last_contact"] == {"date": "2026-06-20",
                                                     "tier": "observed"}
    assert people["Jake Morales"]["last_contact"]["date"] == "2026-07-28"
    assert people["Priya Natarajan"]["last_contact"]["date"] == "2026-07-31"
    assert "Alex Rivera" not in people                 # the owner


def test_people_lists_recent_emails_with_who_wrote_to_whom(conn, cfg):
    seed_more(conn)
    emails = api.people(conn, cfg)["recent_emails"]
    assert [e["title"] for e in emails[:3]] == ["Dinner Sunday", "Deck v3",
                                                "Bled photos"]
    # no direction recorded: the headers' sender and recipients, you included
    assert emails[0]["who"] == "Linda Rivera → you, Sam Okafor"
    assert emails[1]["who"] == "you → Priya Natarajan"
    assert emails[2]["who"] == "Jake Morales → you"
    assert all(e["tier"] == "observed" and e["import"]["uuid"] for e in emails)


def test_person_shows_linkedin_position_and_their_emails(conn, cfg):
    ids = seed_more(conn)
    priya = api.person(conn, cfg, ids["priya"])
    assert (priya["linkedin"]["position"], priya["linkedin"]["company"],
            priya["linkedin"]["connected_on"]) == ("VP Product", "Acme Robotics",
                                                    "2026-07-30")
    assert [e["title"] for e in priya["emails"]] == ["Deck v3"]
    assert priya["last_out"]["stream"] == "linkedin_message"
    jake = api.person(conn, cfg, ids["jake"])
    assert jake["linkedin"] is None
    assert [e["title"] for e in jake["emails"]] == ["Bled photos", "Party"]
    assert jake["last_in"]["stream"] in ("email", "instagram_dm")


def test_day_view_adds_up_workouts_food_and_listening(conn, cfg):
    seed_more(conn)
    a = api.day(conn, cfg, "2026-07-29")["activity"]
    assert (a["workouts"]["count"], a["workouts"]["minutes"]) == (1, 60.0)
    assert a["food"]["basis"] == "nutrition_day"
    assert {t["key"]: t["value"] for t in a["food"]["totals"]} == {"energy": 2200.0,
                                                                    "fiber": 30.0}
    sunday = api.day(conn, cfg, "2026-07-26")["activity"]
    assert sunday["food"]["basis"] == "food_log" and sunday["food"]["foods"] == 2
    assert {t["key"]: t["value"] for t in sunday["food"]["totals"]} == {
        "energy": 1500.0, "protein": 95.5}
    assert (sunday["rides"]["count"], sunday["rides"]["total"],
            sunday["rides"]["unit"]) == (1, 16.87, "usd")
    assert sunday["orders"]["items"][0]["title"] == "Chipotle"
    assert sunday["searches"] == {"count": 2}
    assert (sunday["listening"]["minutes"], sunday["listening"]["tracks"],
            sunday["listening"]["plays"]) == (10.0, 1, 3)
    assert "workouts" not in sunday
    assert api.day(conn, cfg, "2026-05-01")["activity"] == {}


def test_health_page_has_workouts_per_week_and_the_nutrient_summary(conn, cfg):
    seed_more(conn)
    # the week of daily summaries (the Sunday before has only a food log)
    h = api.health(conn, cfg, days=7, end="2026-08-02")
    w = h["workouts"]
    assert len(w["weeks"]) == 8 and w["weeks"][-1]["week"] == "2026-07-27"
    assert (w["weeks"][-1]["sessions"], w["weeks"][-1]["minutes"]) == (2, 105.0)
    assert (w["sessions"], w["minutes"]) == (4, 205.0)
    n = h["nutrition"]
    assert "not medical advice" in n["disclaimer"]
    assert [x["key"] for x in n["low"]] == ["vitamin_d"]
    assert [x["key"] for x in n["high"]] == ["sodium"]
    assert conn.execute("SELECT stream FROM observations WHERE uuid=?",
                        (n["low"][0]["latest_uuid"],)).fetchone()["stream"] == \
        "nutrition_day"
    empty = api.health(conn, cfg, days=14, end="2026-06-01")
    assert empty["nutrition"] is None and empty["workouts"]["sessions"] == 0


def test_who_lines_for_email_to_yourself_and_payments(conn, cfg):
    seed_more(conn)
    _apply(conn, "email-self", ME_EM[1], [
        _rec(70, "email", "2026-07-30", direction="self", title="Passport scan",
             who=((ME_EM, "sender"), (ME_EM, "recipient")))])
    rows = conn.execute(api._OBS_SELECT + "WHERE o.stream IN ('email', 'txn') "
                        "AND o.status='active' ORDER BY o.external_id").fetchall()
    who = {i["title"]: i["who"] for i in api._obs_items(conn, cfg, rows)}
    assert who["Passport scan"] == "you → you"
    assert who["Jake Morales: Bled lakeside place"] == "you → Jake Morales"
    assert who["Jake Morales: tacos"] == "Jake Morales → you"


def test_the_page_labels_every_stream_and_never_renders_imported_html():
    labels = set(re.findall(r"\b([a-z_]+):\s*\"", PAGE.split("const STREAMS = {")[1]
                            .split("};")[0]))
    streams = {s for group in recall.STREAM_GROUPS.values() for s in group}
    assert streams <= labels, streams - labels
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML", PAGE)
    for name in ("activitySection", "workoutsCard", "nutritionCard"):
        assert f"function {name}(" in PAGE

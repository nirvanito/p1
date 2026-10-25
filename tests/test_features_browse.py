"""Search, link memory, conversations, weekday profiles, LinkedIn job changes,
the year in review — and the switches that hide each of them.

The load-bearing claims: search ANDs the words across both tiers, newest
first, with facets that keep their siblings; a link is found by its site or
its page whatever tracking it carried, with who sent it; a conversation is
the one-to-one thread across apps, group texts left out; a weekday is
compared only with enough known days; a changed position between two
LinkedIn exports is a nudge for a linked person; the year lens holds what
only a year shows; unchecking a feature removes its output everywhere.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from feature_seed import (JAKE, MOM, OWNER, apply, entry, mood, people, rec, sms)

from lifecrm import features, jobchanges, links, nudges, recall, review, search, weekday
from lifecrm.domain.util import DomainError
from lifecrm.web import api

TODAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(conn, cfg):
    who = people(conn)
    jake, linda = who["Jake Morales"], who["Linda Rivera"]
    apply(conn, [
        sms("2026-10-01", "10:00", JAKE, "check this https://www.nytimes.com/2026/10/01/"
                                         "lake.html?utm_source=share&smid=x lake trip?"),
        sms("2026-10-02", "11:00", JAKE, "loved it", direction="out"),
        sms("2026-10-05", "12:00", MOM, "the lake photos are great"),
        rec("email", "2026-10-03", "09:00", [("phone", JAKE, "sender")], direction="in",
            title="Lake house booking", body="see nytimes.com/travel too"),
        # a group text: Jake and Mom both on it — not a conversation with Jake
        rec("android_mms", "2026-10-06", "13:00",
            [("phone", JAKE, "sender"), ("phone", OWNER, "recipient"),
             ("phone", MOM, "recipient")], direction="in", body="family lake weekend?"),
        rec("android_call", "2026-10-07", "18:00", [("phone", JAKE, "participant")],
            direction="out", value=600.0, unit="s"),
        rec("browser_visit", "2026-10-01", "21:00", [], title="Lake piece - NYT",
            body="https://nytimes.com/2026/10/01/lake.html",
            value_json={"domain": "nytimes.com"}),
    ], source="msgs")
    entry(conn, "2026-10-02", "event", "Lake weekend plans with Jake", mentions=[jake])
    entry(conn, "2024-06-01", "event", "First lake swim", "exercise")
    conn.commit()
    return {"jake": jake, "linda": linda}


# ---------------------------------------------------------------- search

def test_search_ands_the_words_across_both_tiers_with_facets(conn, cfg, world):
    r = search.search(conn, cfg, "lake")
    assert r["total"] >= 7
    assert r["results"][0]["date"] >= r["results"][-1]["date"]       # newest first
    assert r["facets"]["tier"] == {"observed": r["total"] - 2, "confirmed": 2}
    assert r["facets"]["year"] == {"2026": r["total"] - 1, "2024": 1}
    both = search.search(conn, cfg, "lake weekend")
    assert {x["title"] or x["kind"] for x in both["results"]} >= {"Lake weekend plans with Jake"}
    assert all("weekend" in (x["snippet"] or "").lower() or "weekend" in (x["title"] or "").lower()
               for x in both["results"])
    only = search.search(conn, cfg, "lake", tier="confirmed")
    assert only["total"] == 2 and only["facets"]["tier"]["observed"] > 0   # siblings stay
    jake = search.search(conn, cfg, "lake", person=world["jake"])
    assert jake["total"] >= 3 and all("Jake Morales" in x["people"] for x in jake["results"])
    assert any(p["name"] == "Jake Morales" for p in r["facets"]["person"])
    assert search.search(conn, cfg, 'OR "') ["total"] == 0                 # never FTS syntax
    with pytest.raises(ValueError):
        search.search(conn, cfg, "lake", tier="maybe")


# ---------------------------------------------------------------- links

def test_links_match_a_site_or_a_page_whatever_tracking_it_carried(conn, cfg, world):
    assert links.normalize("https://www.NYTimes.com/a/b/?utm_source=x&id=3#top") == \
        "nytimes.com/a/b?id=3"
    assert links.normalize("http://example.com:abc/x") is None
    assert links.normalize("example.com:8080/x") == "example.com:8080/x"
    assert links.urls_in("mail me at jake@acme.com or see acme.com/jobs") == ["acme.com/jobs"]
    site = links.sightings(conn, cfg, "nytimes.com")
    assert site["target"]["site"] and site["total"] == 3
    streams = [s["stream"] for s in site["sightings"]]
    assert streams == ["android_sms", "browser_visit", "email"]
    assert site["sightings"][0]["from"] == "Jake Morales"
    page = links.sightings(conn, cfg, "https://nytimes.com/2026/10/01/lake.html")
    assert page["total"] == 2 and not page["target"]["site"]       # sms and browser, not email
    sent = links.from_person(conn, cfg, world["jake"])
    assert sent and sent[0]["site"] == "nytimes.com"
    r = recall.answer(conn, cfg, "who sent me nytimes.com links", use_llm=False, today=TODAY)
    check = recall.check_citations(r["answer"], r["evidence"])
    assert not check["unsupported"] and "3 record(s) link to the site nytimes.com" in r["answer"]


# ---------------------------------------------------------------- conversation

def test_a_conversation_is_the_one_to_one_thread_across_apps(conn, cfg, world):
    c = api.conversation(conn, cfg, world["jake"])
    bodies = [x["text"] or x["title"] for x in c["items"]]
    assert c["streams"] == ["android_call", "android_sms", "email"]
    assert "family lake weekend?" not in bodies                 # the group text is left out
    assert [x["date"] for x in c["items"]] == sorted(x["date"] for x in c["items"])
    page = api.conversation(conn, cfg, world["jake"], limit=2)
    assert len(page["items"]) == 2 and page["older"]
    older = api.conversation(conn, cfg, world["jake"], before=page["older"], limit=10)
    assert {x["uuid"] for x in older["items"]}.isdisjoint({x["uuid"] for x in page["items"]})
    with pytest.raises(api.NotFound):
        api.conversation(conn, cfg, 9999)


# ---------------------------------------------------------------- weekdays

def test_weekdays_compare_only_with_enough_known_days(conn, cfg):
    start = date(2026, 7, 6)                                  # a Monday
    for i in range(70):
        d = start + timedelta(days=i)
        mood(conn, d.isoformat(), 4 if d.weekday() == 2 else 8 if d.weekday() == 5 else 6)
    conn.commit()
    p = weekday.profile(conn, cfg, "mood", "2026-07-06", "2026-09-13")
    assert p["low"]["weekday"] == "Wednesday" and p["high"]["weekday"] == "Saturday"
    assert p["weekdays"][2] == {"weekday": "Wednesday", "known": 10, "mean": 4.0}
    assert "lowest Wednesday 4/10, highest Saturday 8/10" in weekday.describe(p)
    thin = weekday.profile(conn, cfg, "mood", "2026-07-06", "2026-07-12")
    assert thin["low"] is None
    r = recall.answer(conn, cfg, "which day of the week is my mood lowest?", use_llm=False,
                      today=date(2026, 9, 13))
    check = recall.check_citations(r["answer"], r["evidence"])
    assert not check["unsupported"], (r["answer"], check)
    assert "averaged lowest on Wednesdays (4/10" in r["answer"]


# ---------------------------------------------------------------- job changes

def _connection(position: str, company: str, exported: str) -> dict:
    return rec("linkedin_connection", "2025-03-01", "12:00",
               [("app_account", "linkedin:in/priya-n", "participant")],
               external_id="linkedin:in/priya-n", title="Priya Natarajan",
               body=f"{position} at {company}",
               value_json={"position": position, "company": company,
                           "url": "https://www.linkedin.com/in/priya-n"},
               last_modified_at=exported)


def test_a_changed_position_between_two_exports_nudges(conn, cfg):
    from lifecrm.domain import entities
    from lifecrm.domain import observations as obs
    handles = [{"kind": "app_account", "value": "linkedin:in/priya-n",
                "display_name": "Priya Natarajan"}]
    apply(conn, [_connection("Engineer", "Acme Robotics", "2026-06-01T00:00:00Z")],
          connector="linkedin", account="linkedin:in/alex", source="Connections-1.csv",
          handles=handles)
    priya = entities.create(conn, "person", "Priya Natarajan")["id"]
    obs.link_handle(conn, "app_account", "linkedin:in/priya-n", priya)
    apply(conn, [_connection("Head of Product", "Globex", "2026-10-15T00:00:00Z")],
          connector="linkedin", account="linkedin:in/alex", source="Connections-2.csv",
          handles=handles)
    conn.commit()
    [c] = jobchanges.changes(conn, cfg)
    assert c["from"] == {"position": "Engineer", "company": "Acme Robotics"}
    assert c["to"] == {"position": "Head of Product", "company": "Globex"}
    assert c["name"] == "Priya Natarajan" and c["entity_id"] == priya
    pack = nudges.build(conn, cfg, today=date.fromisoformat(c["seen_on"]), now=NOW)
    job = next(n for n in pack["nudges"] if n["kind"] == "job_change")
    assert job["title"].startswith("Priya Natarajan: Head of Product at Globex in the LinkedIn "
                                   "export imported")
    assert job["key"] == f"job:{priya}:{c['audit_id']}"
    assert jobchanges.for_person(conn, cfg, priya)[0]["to"]["company"] == "Globex"
    assert api.person(conn, cfg, priya)["job_changes"][0]["to"]["company"] == "Globex"


# ---------------------------------------------------------------- the year

def test_the_year_lens(conn, cfg, world):
    apply(conn, [rec("book", "2026-03-04", "12:00", [], title="The Overstory",
                     body="Richard Powers", value=5.0, unit="stars",
                     value_json={"author": "Richard Powers", "exclusive_shelf": "read"}),
                 rec("music_play", "2026-02-01", "20:00", [], title="Song A", body="Big Thief",
                     value=42.0, unit="min"),
                 rec("music_play", "2026-02-02", "20:00", [], title="Song B", body="Big Thief",
                     value=30.0, unit="min"),
                 rec("music_play", "2026-02-03", "20:00", [], title="Song C", body="Wilco",
                     value=12.0, unit="min")], source="media")
    for d, v in (("2026-01-05", 6), ("2026-01-06", 8), ("2026-02-01", 7)):
        mood(conn, d, v)
    conn.commit()
    period = review.resolve_period(cfg, year=2026, today=TODAY)
    assert period["kind"] == "year" and period["label"] == "the year 2026 so far"
    pack = review.build_pack(conn, cfg, period, lens="year")
    y = pack["year"]
    assert [p["name"] for p in y["new_people"]] == ["Jake Morales", "Linda Rivera"]
    assert y["books"][0]["title"] == "The Overstory" and y["books"][0]["rating"] == 5.0
    assert [(a["artist"], a["minutes"]) for a in y["listening"]] == [("Big Thief", 72.0),
                                                                     ("Wilco", 12.0)]
    assert y["mood_by_month"][0] == {"month": "2026-01", "mean": 7.0, "known": 2,
                                     "ids": y["mood_by_month"][0]["ids"]}
    md = review.render_markdown(pack)
    assert "## The year" in md and "Most listened to:" in md and "Mood pixels: 2026-01 " in md
    assert all(cid in pack["index"] for p in y["new_people"] for cid in [p["id"]] if cid)
    # the review command takes --year and picks the lens
    from typer.testing import CliRunner
    assert review.LENSES[-1] == "year"
    cfg.extra["features"] = {"year_review": False}
    with pytest.raises(DomainError, match="features check year_review"):
        review.build_pack(conn, cfg, period, lens="year")


# ---------------------------------------------------------------- the switches

def test_unchecking_removes_each_feature_s_output(conn, cfg, world):
    on = nudges.build(conn, cfg, today=TODAY, now=NOW)
    assert on["on_this_day"]["enabled"] and on["features"]["on_this_day"]
    cfg.extra["features"] = {k: False for k in features.KEYS}
    off = nudges.build(conn, cfg, today=TODAY, now=NOW)
    for key in ("on_this_day", "prompts", "highlights"):
        assert off[key] == {"enabled": False}
    assert all(not a.get("prep") for a in off["agenda"])
    kinds = {n["kind"] for n in off["nudges"]}
    assert kinds.isdisjoint({"experiment", "anniversary", "price_change", "new_recurring",
                             "job_change"})
    period = review.resolve_period(cfg, week="2026-W41", today=TODAY)
    for lens in review.LENSES:
        if lens == "year":
            continue
        pack = review.build_pack(conn, cfg, period, lens=lens)
        assert set(pack["sections"]).isdisjoint(set(review.SECTION_FEATURES))
    assert api.search(conn, cfg, "lake")["enabled"] is False
    assert api.trips(conn, cfg)["enabled"] is False
    assert api.conversation(conn, cfg, world["jake"])["enabled"] is False
    d = api.day(conn, cfg, "2026-10-01")
    assert d["on_this_day"] is None and d["prompts"] is None and d["trip"] is None
    r = recall.answer(conn, cfg, "who sent me nytimes.com links", use_llm=False, today=TODAY)
    assert not any(m["metric"] in ("places", "subscriptions", "weekday", "links")
                   for m in r["evidence"]["metrics"])
    from lifecrm import export
    notes = export.build_vault(conn, tz_name=cfg.timezone, cfg=cfg)
    assert "Trips.md" not in notes
    assert all("On this day:" not in t for t in notes.values())

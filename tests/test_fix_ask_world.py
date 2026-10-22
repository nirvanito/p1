"""Questions about the persona's life that ask answered wrongly, now right.

The world is test_ask_demo's — the persona journal plus every observed
source, handles linked — with what it lacked for these questions: upcoming
calendar events, Timeline visits to a place whose place_id the owner linked,
and a restaurant's phone number linked to the restaurant. Each test names
the record that must answer, and the one that used to answer instead.
"""

from datetime import timedelta

import pytest

from lifecrm import db as db_mod, recall
from lifecrm.demo import persona
from lifecrm.domain import entities, observations as obs
from lifecrm.domain.util import canonical_json, now_utc
from test_ask_demo import ANCHOR, _apply, _rec, _scan, _utc, build_world, make_cfg

ME_PHONE, ME_EMAIL = persona.OWNER.phone, persona.OWNER.email
ZILKER_PLACE = "ChIJzilker-test"
VERACRUZ_PHONE = "+15125550177"


def day(offset: int) -> str:
    return (ANCHOR + timedelta(days=offset)).isoformat()


def extras(conn, cfg, ids):
    def event(uid, off, title, *attendees):
        start = persona.local_dt(ANCHOR, off, "18:00")
        parts = [{"kind": "email", "value": ME_EMAIL, "role": "organizer"}]
        parts += [{"kind": "email", "value": persona.person(k).email, "role": "participant"}
                  for k in attendees]
        return _rec(cfg, "calendar_event", uid, _utc(start),
                    ended_at=_utc(start + timedelta(hours=1)), title=title,
                    participants=parts)
    upcoming = [event("up-1", 2, "Team offsite"),                 # Friday
                event("up-2", 3, "Climbing gym w/ Sam", "sam"),    # Saturday
                event("up-3", 10, "Halloween party")]              # next week
    _apply(conn, _scan("calendar", upcoming,
                       [{"kind": "email", "value": ME_EMAIL, "display_name": "Alex Rivera"},
                        {"kind": "email", "value": persona.person("sam").email,
                         "display_name": "Sam Okafor"}],
                       account=ME_EMAIL, source_file="upcoming.ics"))

    zilker = persona.place("zilker")
    visit = _rec(cfg, "place_visit", "visit-zilker",
                 _utc(persona.local_dt(ANCHOR, -3, "17:30")),
                 title=f"Place at {zilker.lat}, {zilker.lon}",
                 value_json=canonical_json({"lat": zilker.lat, "lon": zilker.lon,
                                            "place_id": ZILKER_PLACE}),
                 participants=[{"kind": "place_id", "value": ZILKER_PLACE,
                                "role": "location"}])
    _apply(conn, _scan("location", [visit],
                       [{"kind": "place_id", "value": ZILKER_PLACE, "display_name": None}],
                       source_file="Timeline.json"))
    obs.link_handle(conn, "place_id", ZILKER_PLACE, ids["zilker"])

    # The restaurant called (an order ready): a call, never a visit.
    call = _rec(cfg, "android_call", "call-veracruz",
                _utc(persona.local_dt(ANCHOR, -1, "12:00")), direction="in",
                value=40, unit="s",
                participants=[{"kind": "phone", "value": VERACRUZ_PHONE, "role": "sender"},
                              {"kind": "phone", "value": ME_PHONE, "role": "recipient"}])
    _apply(conn, _scan("calls", [call],
                       [{"kind": "phone", "value": VERACRUZ_PHONE, "display_name": None}],
                       account=ME_PHONE, source_file="calls-2.xml"))
    obs.link_handle(conn, "phone", VERACRUZ_PHONE, ids["veracruz"])
    conn.commit()


@pytest.fixture(scope="module", params=persona.COMBOS)
def world(request, tmp_path_factory):
    root = tmp_path_factory.mktemp(f"fixworld-{request.param}")
    cfg = make_cfg(root)
    db_mod.init_db(cfg.db_path, now_utc())
    conn = db_mod.connect(cfg.db_path)
    info = build_world(conn, cfg, root, combo=request.param)
    extras(conn, cfg, info["ids"])
    yield {**info, "conn": conn, "cfg": cfg, "combo": request.param}
    conn.close()


def ask(world, question):
    bundle = recall.build_evidence(world["conn"], world["cfg"], question, today=ANCHOR)
    return bundle, recall.deterministic_answer(bundle)


def item(bundle, cid):
    return next(x for key in ("metrics", "entries", "passages", "observations")
                for x in bundle.get(key, []) if x["id"] == cid)


def spend(bundle):
    (m,) = [m for m in bundle["metrics"] if m["metric"] == "txn_spend"]
    return m


def entity(bundle, name):
    return next(e for e in bundle["entities"] if e["name"] == name)


def debits(lo: int, hi: int):
    """The persona's money out between two anchor offsets, inclusive."""
    return [t for t in persona.TRANSACTIONS
            if t.offset is not None and lo <= t.offset <= hi and t.amount < 0]


QUESTIONS = (
    "how much did I spend in September", "how much did I spend at Uchi in October",
    "what did Maya text me in October", "what did I ask Claude in October",
    "how much did I spend at H-E-B", "how much did I spend at HEB",
    "how much did I spend at Zilker Park",
    "when did Maya last text me", "when was the last time Maya texted me",
    "when did Sam last text me", "when did Jake last call me",
    "when did I last call Jake", "when did I last call Mom",
    "when did I last see Dr. Ellis", "how much did I spend over the past year",
    "what are my open tasks with Priya", "what do I need to do this week",
    "what are my plans this weekend", "what are my open tasks this month",
    "what's Maya's phone number", "what did I do yesterday",
    "what happened on Monday", "when was I last at Zilker Park",
    "when was I last at Veracruz", "what's on my calendar this week",
    "what's on my calendar next week", "how much did I spend this year",
)


def test_every_answer_here_passes_its_own_citation_check(world):
    for q in QUESTIONS:
        bundle, text = ask(world, q)
        check = recall.check_citations(text, bundle)
        assert check["unknown"] == [] and check["unsupported"] == [], (q, check, text)
        assert "Nothing in the database" not in text, (q, text)


# ---------------------------------------------------------------- 1. a window's words

def test_a_month_named_is_the_window_never_a_word_to_find(world):
    sept, text = ask(world, "how much did I spend in September")
    last, _ = ask(world, "how much did I spend last month")
    assert sept["window"]["label"] == "September 2026"
    assert spend(sept)["total"] == spend(last)["total"] > 0
    assert not spend(sept)["terms"]
    assert text.startswith("You spent")


def test_a_merchant_and_a_month_find_that_merchant_in_that_month(world):
    bundle, _ = ask(world, "how much did I spend at Uchi in October")
    m = spend(bundle)
    assert (m["total"], m["spent_count"]) == (212.4, 1)
    assert item(bundle, m["transactions"][0])["title"] == "UCHI AUSTIN"


def test_texts_and_chats_in_a_named_month(world):
    bundle, _ = ask(world, "what did Maya text me in October")
    maya = entity(bundle, "Maya Chen")
    hits = [item(bundle, i) for i in maya["hits"]]
    assert hits and all(h["date"].startswith("2026-10") for h in hits)
    assert all(any(p["entity"] == "Maya Chen" and p["role"] == "sender"
                   for p in h["participants"]) for h in hits)

    bundle, text = ask(world, "what did I ask Claude in October")
    hits = [item(bundle, i) for i in bundle["obs_hits"]]
    assert hits and {h["stream"] for h in hits} == {"claude_chat"}
    assert all(h["date"].startswith("2026-10") for h in hits)
    assert "you asked Claude" in text


# ---------------------------------------------------------------- 2. a named merchant

def test_a_hyphenated_merchant_is_found_by_any_of_its_names(world):
    expected = round(-sum(t.amount for t in persona.TRANSACTIONS if "H-E-B" in t.merchant), 2)
    for q in ("how much did I spend at H-E-B", "how much did I spend at HEB"):
        bundle, text = ask(world, q)
        m = spend(bundle)
        assert (m["total"], m["spent_count"]) == (expected, 3), q
        assert all("H-E-B" in item(bundle, i)["title"] for i in m["transactions"]), q
        assert "matching" in text


def test_a_named_merchant_with_no_transactions_is_no_data_not_every_purchase(world):
    bundle, text = ask(world, "how much did I spend at Zilker Park")
    assert not [m for m in bundle["metrics"] if m["metric"] == "txn_spend"]
    assert 'transactions matching "Zilker Park" (all records)' in bundle["no_data"]
    assert "You spent" not in text


# ---------------------------------------------------------------- 3. who texted whom

@pytest.mark.parametrize("question, who, offset, body", [
    ("when did Maya last text me", "Maya Chen", -5, "Go to sleep"),
    ("when was the last time Maya texted me", "Maya Chen", -5, "Go to sleep"),
    ("when did Sam last text me", "Sam Okafor", -2, "Back at it?"),
])
def test_a_group_message_someone_else_sent_is_not_them_texting_you(world, question, who,
                                                                  offset, body):
    bundle, text = ask(world, question)
    lc = entity(bundle, who)["last_contact"]
    latest = item(bundle, lc["latest"])
    assert latest["kind"] == "text" and latest["date"] == day(offset)
    assert latest["snippet"].startswith(body)
    assert any(p["entity"] == who and p["role"] == "sender" for p in latest["participants"])
    assert "Booking the lakeside place" not in text


# ---------------------------------------------------------------- 4. a channel named

def test_a_call_question_is_answered_by_a_call(world):
    for q in ("when did Jake last call me", "when did I last call Jake"):
        bundle, text = ask(world, q)
        lc = entity(bundle, "Jake Morales")["last_contact"]
        latest = item(bundle, lc["latest"])
        assert latest["kind"] == "call" and latest["date"] == day(-20), q
        assert all(i.startswith("O") for i in lc["items"]), q
        assert text.startswith("Last contact with Jake Morales: " + day(-20)), q


def test_a_journal_entry_that_says_it_was_a_call_still_counts(world):
    bundle, _ = ask(world, "when did I last call Mom")
    lc = entity(bundle, "Linda Rivera")["last_contact"]
    titles = {item(bundle, i).get("title") for i in lc["items"]}
    assert "Called Mom on the drive home" in titles
    assert item(bundle, lc["latest"])["kind"] == "call"


def test_when_i_last_texted_someone_is_my_text(world):
    bundle, _ = ask(world, "when did I last text Jake")
    assert bundle["intent"]["direction"] == "out"
    lc = entity(bundle, "Jake Morales")["last_contact"]
    latest = item(bundle, lc["latest"])
    assert (latest["kind"], latest["direction"], latest["snippet"]) == \
        ("text", "out", "see you there")


# ---------------------------------------------------------------- 5. Dr.

def test_a_sentence_about_dr_ellis_is_checked_whole(world, monkeypatch):
    bundle, text = ask(world, "when did I last see Dr. Ellis")
    assert recall.check_citations(text, bundle)["unsupported"] == []
    lc = entity(bundle, "Dr. Rebecca Ellis")["last_contact"]
    said = (f"On {lc['date']} you saw Dr. Ellis for a dentist check-up "
            f"[{lc['latest']}].")
    assert recall.check_citations(said, bundle)["unsupported"] == []

    monkeypatch.setattr("lifecrm.llm.status", lambda cfg: {
        "available": True, "reason": "", "model": "fake:8b", "models": ["fake:8b"]})
    monkeypatch.setattr("lifecrm.llm.answer",
                        lambda cfg, evidence, question, model=None: said)
    result = recall.answer(world["conn"], world["cfg"], "when did I last see Dr. Ellis",
                           today=ANCHOR)
    assert result["answer_source"] == "model:fake:8b" and result["model_rejected"] is None


# ---------------------------------------------------------------- 6. the past year

def test_the_past_year_is_the_last_twelve_months(world):
    bundle, text = ask(world, "how much did I spend over the past year")
    assert bundle["window"]["label"] == "past year"
    assert bundle["window"]["start"] == (ANCHOR - timedelta(days=364)).isoformat()
    paid = debits(-364, 0)
    m = spend(bundle)
    assert (m["total"], m["spent_count"]) == (round(-sum(t.amount for t in paid), 2),
                                              len(paid))


# ---------------------------------------------------------------- 7. open loops, narrowed

OPEN = {"Get back to three gym sessions a week", "Book flights to Ljubljana",
        "Visit Mom in San Antonio", "Return trip to Lake Bled next summer"}


def test_a_person_with_no_open_loops_is_not_no_loops_at_all(world):
    bundle, text = ask(world, "what are my open tasks with Priya")
    assert bundle["loops"] == [] and bundle["loops_open"] == len(OPEN)
    assert text.startswith(f"No open tasks or plans mention Priya Natarajan "
                           f"({len(OPEN)} open in total).")
    assert "You have no open tasks" not in text


@pytest.mark.parametrize("question", ["what do I need to do this week",
                                      "what are my open tasks this month",
                                      "what are my plans this weekend"])
def test_a_window_keeps_every_loop_still_open_then(world, question):
    bundle, _ = ask(world, question)
    assert {item(bundle, i)["title"] for i in bundle["loops"]} == OPEN


def test_plans_this_weekend_include_the_calendar_then(world):
    bundle, text = ask(world, "what are my plans this weekend")
    titles = [item(bundle, i)["title"] for i in bundle["obs_hits"]]
    assert titles == ["Climbing gym w/ Sam"]
    assert "Climbing gym w/ Sam" in text


# ---------------------------------------------------------------- 8. a card's numbers

def test_a_contact_cards_phone_and_email_are_evidence(world):
    maya = persona.person("maya")
    bundle, text = ask(world, "what's Maya's phone number")
    card = entity(bundle, "Maya Chen")["cards"][0]
    assert maya.phone in text and maya.email in text
    assert maya.phone in recall.render_evidence(bundle)
    said = f"Maya's number is {maya.phone} [{card}]."
    assert recall.check_citations(said, bundle)["unsupported"] == []


# ---------------------------------------------------------------- 9. a recap

@pytest.mark.parametrize("question, offset", [("what did I do yesterday", -1),
                                              ("what happened on Monday", -2)])
def test_a_named_day_alone_lists_what_was_recorded_then(world, question, offset):
    bundle, text = ask(world, question)
    uuids = {r["uuid"] for r in world["conn"].execute(
        "SELECT uuid FROM entries WHERE status='active' AND journal_date=?", (day(offset),))}
    assert uuids
    cited = {item(bundle, i)["uuid"] for i in recall._cited(text) if i.startswith("E")}
    assert cited == uuids
    assert bundle["has_evidence"] and bundle["recap"]


# ---------------------------------------------------------------- 10. a place's visits

def test_a_linked_place_visit_answers_when_i_was_last_there(world):
    bundle, text = ask(world, "when was I last at Zilker Park")
    zilker = entity(bundle, "Zilker Park")
    assert zilker["handles_linked"] == 1
    latest = item(bundle, zilker["last_contact"]["latest"])
    assert (latest["stream"], latest["date"]) == ("place_visit", day(-3))
    assert text.startswith(f"Last visit to Zilker Park on record: {day(-3)}, "
                           f"place visit at Zilker Park")


def test_a_places_linked_phone_number_makes_no_call_a_visit(world):
    bundle, _ = ask(world, "when was I last at Veracruz")
    veracruz = entity(bundle, "Veracruz All Natural")
    assert veracruz["handles_linked"] == 1
    kinds = {item(bundle, i)["kind"] for i in (veracruz["last_contact"] or {}).get("items", [])}
    assert "call" not in kinds


# ---------------------------------------------------------------- 11. the rest of this week

def test_the_calendar_this_week_includes_what_is_still_to_come(world):
    bundle, text = ask(world, "what's on my calendar this week")
    assert bundle["window"]["end"] == day(4)                  # through Sunday
    titles = [item(bundle, i)["title"] for i in bundle["obs_hits"]]
    assert titles[:2] == ["Team offsite", "Climbing gym w/ Sam"]   # soonest first
    assert "Team offsite" in text

    bundle, text = ask(world, "what's on my calendar next week")
    assert [item(bundle, i)["title"] for i in bundle["obs_hits"]] == ["Halloween party"]


# ---------------------------------------------------------------- 13. deposits are not purchases

def test_what_was_spent_counts_only_money_out(world):
    bundle, text = ask(world, "how much did I spend this year")
    m = spend(bundle)
    paid = debits(-(ANCHOR - ANCHOR.replace(month=1, day=1)).days, 0)
    credits = [t for t in persona.TRANSACTIONS if t.offset is not None and t.amount > 0]
    assert (m["spent_count"], m["received_count"], m["count"]) == \
        (len(paid), len(credits), len(paid) + len(credits))
    assert f"across {len(paid)} transaction(s)" in text
    assert f"in {len(credits)} transaction(s)" in text


# ---------------------------------------------------------------- 12. the owner, by any import

def owner_world(conn, cfg, *, link_owner: bool):
    """An iPhone-style calendar export: its account is 'calendar:Home', and
    the owner's own address is on the event as organizer and attendee."""
    ellis = persona.person("ellis")
    ids = {"ellis": entities.create(conn, "person", ellis.name,
                                    aliases=list(ellis.aliases))["id"]}
    text = _rec(cfg, "imessage", "msg-1", _utc(persona.local_dt(ANCHOR, -3, "09:00")),
                direction="out", body="Running five minutes late",
                participants=[{"kind": "phone", "value": ME_PHONE, "role": "sender"},
                              {"kind": "phone", "value": ellis.phone, "role": "recipient"}])
    _apply(conn, _scan("mac_messages", [text],
                       [{"kind": "phone", "value": ME_PHONE, "display_name": "Alex Rivera"},
                        {"kind": "phone", "value": ellis.phone, "display_name": None}],
                       account=ME_PHONE, source_file="chat.db"))
    start = persona.local_dt(ANCHOR, -2, "10:30")
    event = _rec(cfg, "calendar_event", "dentist", _utc(start),
                 ended_at=_utc(start + timedelta(minutes=45)), title="Dentist: Dr. Ellis",
                 participants=[{"kind": "email", "value": ME_EMAIL, "role": "organizer"},
                               {"kind": "email", "value": ME_EMAIL, "role": "participant"},
                               {"kind": "email", "value": ellis.email,
                                "role": "participant"}])
    _apply(conn, _scan("calendar", [event],
                       [{"kind": "email", "value": ME_EMAIL, "display_name": "Alex Rivera"},
                        {"kind": "email", "value": ellis.email, "display_name": None}],
                       account="calendar:Home", source_file="Home.ics"))
    if link_owner:
        alex = entities.create(conn, "person", "Alex Rivera")["id"]
        obs.link_handle(conn, "phone", ME_PHONE, alex)
        obs.link_handle(conn, "email", ME_EMAIL, alex)
    else:
        # the owner's address is another import's account
        _apply(conn, _scan("ai_chat", [], [], account=ME_EMAIL, source_file="chatgpt.zip"))
    obs.link_handle(conn, "phone", ellis.phone, ids["ellis"])
    obs.link_handle(conn, "email", ellis.email, ids["ellis"])
    conn.commit()


@pytest.mark.parametrize("link_owner", [True, False])
def test_the_owner_on_an_event_is_you_whatever_the_calendars_account(conn, cfg,
                                                                     link_owner):
    owner_world(conn, cfg, link_owner=link_owner)
    bundle = recall.build_evidence(conn, cfg, "when did I last see Dr. Ellis", today=ANCHOR)
    text = recall.deterministic_answer(bundle)
    event = next(o for o in bundle["observations"] if o["stream"] == "calendar_event")
    owners = [p for p in event["participants"] if p["handle"] == f"email:{ME_EMAIL}"]
    assert owners and all(p["is_owner"] for p in owners)
    assert 'calendar "Dentist: Dr. Ellis" with Dr. Rebecca Ellis [' in text
    assert "Alex Rivera" not in text and ME_EMAIL not in text

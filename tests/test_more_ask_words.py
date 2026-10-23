"""Which words choose which of the newer record kinds — and which do not.

Read by rule from the question alone (an empty database with a few named
people), so each ambiguity the connector builders flagged has its own line:
'book' only as a noun, 'read' never when a person reads, 'Strong' only as the
app, 'listen' only when nothing stronger chose, 'quote' never as a price,
'sent' for neither mail nor texts, "Jake's email" as an address.
"""

from datetime import date

import pytest

from lifecrm import recall
from lifecrm.domain import entities

TODAY = date(2026, 10, 21)


@pytest.fixture
def people(conn):
    for name, aliases in (("Jake Morales", ["Jake"]), ("Priya Natarajan", ["Priya"]),
                          ("Maya Chen", ["Maya"])):
        entities.create(conn, "person", name, aliases=aliases)
    conn.commit()
    return conn


def intent(conn, cfg, question):
    return recall.build_evidence(conn, cfg, question, today=TODAY)["intent"]


@pytest.mark.parametrize("question, streams", [
    ("what did Priya email me", ["email"]),
    ("what's in my inbox from Priya", ["email"]),
    ("what did I search for about Bled", ["web_search"]),
    ("what did I google", ["web_search"]),
    ("what did I look up about knee surgery", ["web_search"]),
    ("what did I watch on YouTube", ["video_watch"]),
    ("what's my bench PR", ["workout"]),
    ("what did I log in Strong this week", ["workout"]),
    ("when did I last work out", ["workout"]),
    ("what podcast did I listen to about Slovenia", ["music_play"]),
    ("how long did I listen to Japanese Breakfast", ["music_play"]),
    ("what did I highlight in The Overstory", ["highlight"]),
    ("what books did I read this year", ["book"]),
    ("what have I read", ["book"]),
    ("what's a good book about trees", ["book"]),
    ("what's on my reading list", ["book"]),
    ("when did I last take an Uber", ["ride"]),
    ("what did I order on Uber Eats", ["food_order"]),
    ("what did I eat yesterday", ["food_log"]),
    ("what did I send on Instagram", ["instagram_dm"]),
    ("what did Maya say on telegram", ["telegram"]),
    ("who do I know at Acme", ["linkedin_connection", "contact_card"]),
])
def test_words_that_choose_a_kind(people, cfg, question, streams):
    assert intent(people, cfg, question)["streams"] == streams


@pytest.mark.parametrize("question", [
    "did I book flights to Ljubljana",         # 'book' the verb
    "did Jake read my message",                # a person reading is a text
    "what did I read about may",               # 'read' beside another topic
    "strong coffee or tea",                    # 'strong' that is not the app
    "how much was the price quote",            # a price, not a passage
    "did Jake send the video",                 # a person named: a word
    "what did Jake say about the gym",
    "what did I eat at Veracruz with Jake",
    "a bike ride with Maya",
    "what did I send Priya",                   # 'sent' is mail and texts both
])
def test_words_that_do_not(people, cfg, question):
    got = intent(people, cfg, question)["streams"]
    assert not set(got) & (recall.ACTIVITY_STREAMS | {"email"}), (question, got)


def test_the_words_stay_a_search_when_they_do_not_choose(people, cfg):
    assert intent(people, cfg, "what did I read about may")["topic_terms"] == ["read", "may"]
    assert "video" in intent(people, cfg, "did Jake send the video")["topic_terms"]
    assert "breakfast" in intent(people, cfg,
                                 "how long did I listen to Japanese Breakfast")["topic_terms"]


def test_jakes_email_is_his_address(people, cfg):
    got = intent(people, cfg, "what's Jake's email")
    assert "email" not in got["streams"] and got["profile"]
    got = intent(people, cfg, "what is Priya's email address")
    assert "email" not in got["streams"] and got["profile"]
    got = intent(people, cfg, "what advice did Priya give me over email")
    assert got["streams"] == ["email"] and not got["profile"]
    assert got["direction"] == "in" and got["senders"]


def test_email_direction(people, cfg):
    assert intent(people, cfg, "what did I email Jake")["direction"] == "out"
    assert intent(people, cfg, "what did Priya email me")["direction"] == "in"
    got = intent(people, cfg, "did Priya email me this week")
    assert got["contact"] and got["streams"] == ["email"]


@pytest.mark.parametrize("question, apps, money", [
    ("how much did I spend on Venmo", ["venmo"], True),
    ("how much did I venmo Jake", ["venmo"], True),
    ("what did Maya paypal me", ["paypal"], True),
    ("what do I owe Sam", [], True),
    ("did Maya pay me back", [], True),
])
def test_payment_words_make_a_money_question(people, cfg, question, apps, money):
    got = intent(people, cfg, question)
    assert (got["apps"], got["money"]) == (apps, money)
    assert not got["unused"], got["unused"]


def test_activity_verbs_are_how_not_what(people, cfg):
    assert intent(people, cfg, "when did I last take an Uber")["topic_terms"] == []
    assert intent(people, cfg, "Uber to the airport")["topic_terms"] == ["airport"]
    got = intent(people, cfg, "when did I last run")
    assert got["streams"] == ["workout"] and got["topic_terms"] == ["run"] and got["last_of"]
    assert intent(people, cfg, "when did I last go running")["topic_terms"] == ["run"]
    got = intent(people, cfg, "what's my best squat")
    assert got["best"] and got["topic_terms"] == ["squat"]


@pytest.mark.parametrize("question, keys, flags", [
    ("what am I low on", [], True),
    ("what nutrients am I deficient in", [], True),
    ("how much protein did I eat this week", ["protein"], False),
    ("am I getting enough iron", ["iron"], False),
    ("how much vitamin D am I getting", ["vitamin_d"], False),
    ("am I eating too much salt", ["sodium"], True),
])
def test_nutrient_questions(people, cfg, question, keys, flags):
    got = intent(people, cfg, question)["nutrition"]
    assert got == {"keys": keys, "flags": flags}


@pytest.mark.parametrize("question", [
    "what did ChatGPT say about protein",      # a chat about protein
    "what's my body fat",
    "how much did I spend on protein powder",  # money
])
def test_not_nutrient_questions(people, cfg, question):
    assert intent(people, cfg, question)["nutrition"] is None


def test_work_and_who_i_know_at(people, cfg):
    got = intent(people, cfg, "where does Priya work")
    assert got["work"] and got["profile"] and got["topic_terms"] == []
    assert intent(people, cfg, "what's Jake's job")["work"]
    assert not intent(people, cfg, "where is Jake from")["work"]
    got = intent(people, cfg, "who do I know at Acme Robotics")
    assert got["know_at"] and got["topic_terms"] == ["acme", "robotics"]


def test_an_empty_database_says_what_is_not_imported(people, cfg):
    bundle = recall.build_evidence(people, cfg, "what did I highlight in The Overstory",
                                   today=TODAY)
    assert "highlight records (highlight): none imported" in bundle["no_data"]
    bundle = recall.build_evidence(people, cfg, "what am I low on", today=TODAY)
    assert any(x.startswith("nutrition day records") for x in bundle["no_data"])
    text = recall.deterministic_answer(bundle)
    assert recall.check_citations(text, bundle)["unsupported"] == []


def test_evidence_times_with_pm_support_the_same_time_quoted(cfg):
    """An invitation's '2pm - 3:30pm' quoted back is the evidence's own time."""
    bundle = {"window": {"label": "x", "start": "2026-10-01", "end": "2026-10-02"},
              "days_with_entries_in_window": 0, "metrics": [], "entries": [],
              "passages": [], "entities": [], "no_data": [], "has_evidence": True,
              "observations": [{
                  "id": "O1", "date": "2026-09-22", "local_time": None, "kind": "email",
                  "stream": "email", "who": None, "title": "Board meeting 3:30pm",
                  "detail": {}, "value": None, "unit": None, "snippet": None,
                  "participants": []}]}
    check = recall.check_citations('The invite says "Board meeting 3:30pm" [O1].', bundle)
    assert check["unsupported"] == []
    check = recall.check_citations("It starts at 15:30 [O1].", bundle)
    assert check["unsupported"] == []
    check = recall.check_citations("It starts at 4:30pm [O1].", bundle)
    assert check["unsupported"]

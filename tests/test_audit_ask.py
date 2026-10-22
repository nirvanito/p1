"""Audit round: questions ask still read wrongly, now right.

Each group failed before its fix. Calendar words other than 'calendar' and
'meeting' ('schedule', 'events', 'coming up') stopped a week at today and
found nothing; 'the rest of the week' was no window at all, and 'past 12
months' five days shorter than 'past year'. 'What tasks are due this week'
searched for the words "tasks" and "due". 'AT&T', 'H&M' and '76' fell apart
into nothing and the answer was everyone's total. 'Did Jake call me this
week' answered with his contact card, and 'ring', 'FaceTime' and 'phone me'
were no calls at all. 'What did I log yesterday' searched for "log";
'the day before yesterday' was yesterday; '10/20', 'the 20th' and 'over the
weekend' were no windows; and the time words a window did not read
('weekend', 'next', 'morning', the second month of 'September and October')
were required search terms. 'Laura J. Kim' ended a sentence at 'J.', so the
answer about her failed its own citation check.
"""

from datetime import date, timedelta

import pytest

from lifecrm import recall
from lifecrm.domain import entities, entries, observations as obs
from test_ask_demo import _apply, _rec, _scan
from test_fix_ask_world import ask, entity, item, world  # noqa: F401  (fixture)

TODAY = date(2026, 10, 21)            # a Wednesday
ME = "+15125550100"


def window(cfg, question, **kw):
    return recall.parse_window(question, cfg, today=TODAY, **kw)


def build(conn, cfg, question):
    return recall.build_evidence(conn, cfg, question, today=TODAY)


def answer(conn, cfg, question):
    bundle = build(conn, cfg, question)
    return bundle, recall.deterministic_answer(bundle)


def utc(d: date, hhmm: str = "12:00") -> str:
    # 12:00 UTC is 07:00 in Chicago: the same journal day.
    return f"{d.isoformat()}T{hhmm}:00Z"


# ---------------------------------------------------------------- periods

@pytest.mark.parametrize("question, start, end", [
    ("how much did I spend in the past 12 months", "2025-10-22", "2026-10-21"),
    ("how much did I spend over the past year", "2025-10-22", "2026-10-21"),
    ("steps in the last 3 months", "2026-07-22", "2026-10-21"),
    ("mood over the past 2 years", "2024-10-22", "2026-10-21"),
    ("weight in the past 6 weeks", "2026-09-10", "2026-10-21"),
])
def test_past_months_and_years_count_calendar_months(cfg, question, start, end):
    w = window(cfg, question)
    assert (w["start"], w["end"]) == (start, end)


def test_twelve_months_is_a_year_even_from_the_end_of_a_month(cfg):
    for today in (date(2026, 3, 31), date(2028, 2, 29), date(2026, 10, 21)):
        a = recall.parse_window("past 12 months", cfg, today=today)
        b = recall.parse_window("past year", cfg, today=today)
        assert (a["start"], a["end"]) == (b["start"], b["end"])
    # March 31 less one month is the end of February, not March 3
    assert recall.parse_window("past month", cfg, today=date(2026, 3, 31))["start"] \
        == "2026-03-01"


@pytest.mark.parametrize("question, start, end, label", [
    ("what's on my calendar for the rest of the week", "2026-10-21", "2026-10-25",
     "rest of this week"),
    ("anything on for the rest of this month", "2026-10-21", "2026-10-31",
     "rest of this month"),
    ("plans for the rest of the year", "2026-10-21", "2026-12-31", "rest of this year"),
    ("what's left for the rest of the day", "2026-10-21", "2026-10-21", "today"),
])
def test_the_rest_of_a_period_runs_from_today_to_its_end(cfg, question, start, end,
                                                         label):
    w = window(cfg, question)
    assert (w["start"], w["end"], w["label"]) == (start, end, label)


@pytest.mark.parametrize("question", [
    "what's on my schedule this week", "what do I have scheduled this week",
    "what events do I have this week", "what's coming up this week",
    "what's on my agenda this week", "anything upcoming this week",
    "any appointments coming up this week",
])
def test_calendar_words_reach_the_end_of_the_week(world, question):
    bundle, text = ask(world, question)
    assert "calendar_event" in bundle["intent"]["streams"]
    assert bundle["window"]["end"] == "2026-10-25"
    titles = {item(bundle, i)["title"] for i in bundle["obs_hits"]}
    assert {"Team offsite", "Climbing gym w/ Sam"} <= titles, (question, text)
    assert bundle["intent"]["topic_terms"] == []
    assert "Nothing in the database" not in text


def test_the_rest_of_the_week_on_the_calendar_is_what_is_still_to_come(world):
    bundle, text = ask(world, "what's on my calendar for the rest of the week")
    assert (bundle["window"]["start"], bundle["window"]["end"]) == \
        ("2026-10-21", "2026-10-25")
    titles = [item(bundle, i)["title"] for i in bundle["obs_hits"]]
    assert titles[:2] == ["Team offsite", "Climbing gym w/ Sam"]
    assert "Halloween party" not in titles


@pytest.mark.parametrize("question", ["what's upcoming", "what's next on my calendar",
                                      "what's coming up"])
def test_a_forward_question_with_no_time_looks_ahead_not_back(world, question):
    bundle, text = ask(world, question)
    w = bundle["window"]
    assert w["explicit"] and w["start"] == "2026-10-21"
    assert w["end"] == (TODAY + timedelta(days=89)).isoformat()
    titles = [item(bundle, i)["title"] for i in bundle["obs_hits"]]
    # soonest first, the party ten days out included, nothing past
    assert titles[:3] == ["Team offsite", "Climbing gym w/ Sam", "Halloween party"]
    assert all(item(bundle, i)["date"] >= "2026-10-21" for i in bundle["obs_hits"])
    assert recall.check_citations(text, bundle)["unsupported"] == []


# ---------------------------------------------------------------- open tasks and due dates

@pytest.fixture
def tasks(conn):
    def task(title, day, due=None, kind="task"):
        return entries.create(conn, journal_date=day, kind=kind, title=title,
                              primary_category="chores", due_at=due)["uuid"]
    out = {"overdue": task("Renew passport", "2026-10-01", "2026-10-16"),
           "due": task("Book flights to Ljubljana", "2026-10-05", "2026-10-24"),
           "later": task("Visit Mom in San Antonio", "2026-10-06", "2026-11-15"),
           "undated": task("Get back to three gym sessions", "2026-10-17")}
    conn.commit()
    return out


def _loop_titles(bundle):
    return [item(bundle, i)["title"] for i in bundle["loops"]]


@pytest.mark.parametrize("question", ["what tasks are due this week",
                                      "what's due this week", "which tasks are due this week",
                                      "what is due this week", "anything due this week"])
def test_what_is_due_this_week_is_what_has_a_due_date_by_sunday(conn, cfg, tasks,
                                                                question):
    bundle, text = answer(conn, cfg, question)
    assert bundle["intent"]["loops"] and bundle["intent"]["due"]
    assert "due" not in bundle["intent"]["topic_terms"] and bundle["match"] is None
    assert bundle["window"]["end"] == "2026-10-25"
    assert _loop_titles(bundle) == ["Renew passport", "Book flights to Ljubljana"]
    assert text.startswith("Open tasks and plans due by 2026-10-25: "
                           "2026-10-01 Renew passport (task, due 2026-10-16)")
    assert "Visit Mom" not in text and "gym" not in text
    assert recall.check_citations(text, bundle)["unsupported"] == []


def test_nothing_due_says_so_and_how_many_are_open(conn, cfg):
    entries.create(conn, journal_date="2026-10-17", kind="task", title="Call the bank",
                   primary_category="chores")
    conn.commit()
    bundle, text = answer(conn, cfg, "what tasks are due this week")
    assert bundle["loops"] == [] and bundle["loops_open"] == 1
    assert text == "No open tasks or plans are due for this week (1 open in total)."


@pytest.mark.parametrize("question", ["what tasks are open", "what tasks do I have",
                                      "any tasks this week", "show me my tasks",
                                      "which tasks are still open"])
def test_a_task_question_without_open_or_my_reaches_the_open_loops(conn, cfg, tasks,
                                                                   question):
    bundle, text = answer(conn, cfg, question)
    assert bundle["intent"]["loops"] and not bundle["intent"]["due"]
    assert bundle["intent"]["topic_terms"] == []
    assert "Renew passport" in _loop_titles(bundle)
    assert text.startswith("Open tasks and plans: ")


def test_due_with_no_window_is_every_loop_with_a_due_date(conn, cfg, tasks):
    bundle, _ = answer(conn, cfg, "what tasks are due")
    assert _loop_titles(bundle) == ["Renew passport", "Book flights to Ljubljana",
                                    "Visit Mom in San Antonio"]


# ---------------------------------------------------------------- merchants

PURCHASES = (("AT&T *BILL PAYMENT", -85.00, -2), ("H&M 0123", -42.50, -3),
             ("H-E-B #742", -64.12, -4), ("76 - 1234 FUEL", -38.20, -5),
             ("UCHI", -212.40, -6), ("MOVEMENT GYM", -79.00, -9))


@pytest.fixture
def statement(conn, cfg):
    records = [_rec(cfg, "txn", f"stmt-{n}", utc(TODAY + timedelta(days=off)),
                    title=title, value=amount, unit="USD")
               for n, (title, amount, off) in enumerate(PURCHASES)]
    _apply(conn, _scan("finance", records, [], account="discover-0042",
                       source_file="Discover-Statement-20261015.csv"))
    conn.commit()


def _spent(bundle):
    found = [m for m in bundle["metrics"] if m["metric"] == "txn_spend"]
    return (found[0]["total"], found[0]["spent_count"]) if found else None


@pytest.mark.parametrize("question, title, amount", [
    ("how much did I spend at AT&T", "AT&T *BILL PAYMENT", 85.00),
    ("how much did I spend at H&M", "H&M 0123", 42.50),
    ("how much did I spend at 76", "76 - 1234 FUEL", 38.20),
    ("what did I buy from H&M", "H&M 0123", 42.50),
    ("how much did I spend at AT&T this month", "AT&T *BILL PAYMENT", 85.00),
    ("how much did I spend at the 76 last week", "76 - 1234 FUEL", 38.20),
])
def test_a_short_or_ampersanded_merchant_stays_the_merchant(conn, cfg, statement,
                                                            question, title, amount):
    bundle, text = answer(conn, cfg, question)
    assert _spent(bundle) == (amount, 1), (question, text)
    assert title in text
    assert recall.check_citations(text, bundle)["unsupported"] == []


@pytest.mark.parametrize("question", ["how much did I spend at Zara",
                                      "how much did I spend at Best Buy",
                                      "how much did I spend at 7-Eleven last week"])
def test_a_merchant_that_matches_nothing_is_no_data_never_the_total(conn, cfg,
                                                                    statement, question):
    bundle, text = answer(conn, cfg, question)
    assert _spent(bundle) is None, text
    assert any(x.startswith("transactions matching") for x in bundle["no_data"])
    assert "521.22" not in text and "You spent" not in text


def test_no_merchant_slot_is_still_the_whole_total(conn, cfg, statement):
    for question in ("how much did I spend", "how much did I spend at all this month"):
        bundle, _ = answer(conn, cfg, question)
        assert _spent(bundle) == (521.22, 6), question


def test_an_ampersand_joins_a_word_and_is_one_phrase():
    words = recall._words("spend at AT&T and H&M")
    assert [w["term"] for w in words] == ["spend", "at", "at&t", "and", "h&m"]
    assert words[2]["key"] == "at t"
    assert recall._fts_expr(["at&t", "uchi"], " ", prefix=True) == '"at&t" "uchi"*'


# ---------------------------------------------------------------- calls and texts

@pytest.mark.parametrize("question", ["When did Jake call?", "when did Jake call me",
                                      "When did Jake last ring me?",
                                      "When did I last FaceTime Jake?",
                                      "When did Jake last phone me?",
                                      "did I phone Jake recently"])
def test_a_call_question_leads_with_the_call(world, question):
    bundle, text = ask(world, question)
    assert set(recall.CALL_STREAMS) <= set(bundle["intent"]["streams"]), question
    jake = entity(bundle, "Jake Morales")
    latest = item(bundle, jake["last_contact"]["latest"])
    assert latest["stream"] in recall.CALL_STREAMS
    assert text.startswith(f"Last contact with Jake Morales: {latest['date']}, "
                           f"Jake Morales called you"), (question, text)
    assert "contact card" not in text
    assert recall.check_citations(text, bundle)["unsupported"] == []


def test_did_jake_call_this_week_says_nothing_is_recorded(world):
    bundle, text = ask(world, "Did Jake call me this week?")
    assert bundle["intent"]["contact"]
    assert text.startswith("Nothing recorded shows a call with Jake Morales for this week.")
    assert "contact card" not in text and "Nickname" not in text


def test_what_jake_texted_when_he_texted_nothing_then_says_so(world):
    bundle, text = ask(world, "what did Jake text me over the weekend")
    assert bundle["window"]["label"] == "last weekend"
    assert text.startswith("Nothing recorded shows a text with Jake Morales for "
                           "last weekend."), text


@pytest.mark.parametrize("question", ["what's Jake's phone number",
                                      "what is Jake's phone", "Jake's phone #"])
def test_a_phone_as_a_thing_someone_has_is_still_their_card(world, question):
    bundle, text = ask(world, question)
    assert bundle["intent"]["profile"]
    assert not set(recall.CALL_STREAMS) & set(bundle["intent"]["streams"])
    (card,) = entity(bundle, "Jake Morales")["cards"]
    assert text.startswith(f"Jake Morales's contact card: phone ") and f"[{card}]" in text


def test_a_ring_that_is_not_a_call_stays_a_word(conn, cfg):
    intent = build(conn, cfg, "where did I buy the ring")["intent"]
    assert intent["streams"] == [] and "ring" in intent["topic_terms"]


# ---------------------------------------------------------------- recaps

@pytest.fixture
def journal(conn):
    def entry(day, title):
        entries.create(conn, journal_date=day, kind="event", title=title,
                       primary_category="social")
    entry("2026-10-17", "Farmers market with Maya")
    entry("2026-10-18", "Long run on the trail")
    entry("2026-10-19", "Launch planning")
    entry("2026-10-20", "Tacos at Veracruz")
    entry("2026-08-31", "End of summer party")
    conn.commit()


@pytest.mark.parametrize("question, days", [
    ("what did I log yesterday", ["2026-10-20"]),
    ("what's in my journal for yesterday", ["2026-10-20"]),
    ("what are my entries for yesterday", ["2026-10-20"]),
    ("what did I record yesterday", ["2026-10-20"]),
    ("what's in my diary from yesterday", ["2026-10-20"]),
    ("what did I write yesterday", ["2026-10-20"]),
    ("what did I do the day before yesterday", ["2026-10-19"]),
    ("what did I do on 10/20", ["2026-10-20"]),
    ("what did I do on 10/20/2026", ["2026-10-20"]),
    ("what did I log on 8/31/26", ["2026-08-31"]),
    ("what did I do on the 20th", ["2026-10-20"]),
    ("what did I do on the 31st", ["2026-08-31"]),
    ("what did I do on the 20th of October", ["2026-10-20"]),
    ("what did I do on 20 October", ["2026-10-20"]),
    ("what did I do over the weekend", ["2026-10-17", "2026-10-18"]),
    ("what did I do this past weekend", ["2026-10-17", "2026-10-18"]),
    ("how was the weekend", ["2026-10-17", "2026-10-18"]),
    ("what did I do yesterday morning", ["2026-10-20"]),
])
def test_a_recap_lists_that_time_whatever_it_is_called(conn, cfg, journal, question,
                                                      days):
    bundle, text = answer(conn, cfg, question)
    assert bundle["recap"], (question, bundle["intent"]["topic_terms"], bundle["match"])
    assert sorted({e["date"] for e in bundle["entries"]}) == days
    assert text.startswith("Journal entries for "), text
    assert recall.check_citations(text, bundle)["unsupported"] == []


def test_a_fraction_is_not_a_date_and_an_impossible_one_is_said(cfg):
    assert window(cfg, "calories in 1/2 cup of rice")["explicit"] is False
    w = window(cfg, "what did I do on 13/45")
    assert w["explicit"] is False and "13/45 is not a real date" in w["ignored"]
    assert window(cfg, "between 10/1 and 10/15")["label"] == "2026-10-01 to 2026-10-15"


# ---------------------------------------------------------------- time words

@pytest.mark.parametrize("question, start, end", [
    ("how much did I spend in September and October", "2026-09-01", "2026-10-31"),
    ("how much did I spend from September to October", "2026-09-01", "2026-10-31"),
    ("how much did I spend September through October", "2026-09-01", "2026-10-31"),
    ("steps Oct 1 - 15", "2026-10-01", "2026-10-15"),
    ("how much did I spend in the last week of September", "2026-09-24", "2026-09-30"),
    ("how much did I spend in the first week of October", "2026-10-01", "2026-10-07"),
    ("how much did I spend at the end of September", "2026-09-21", "2026-09-30"),
    ("how much did I spend at the start of October", "2026-10-01", "2026-10-10"),
    ("mood at the end of last month", "2026-09-21", "2026-09-30"),
    ("how much did I spend over the weekend", "2026-10-17", "2026-10-18"),
    ("how much did I spend this past weekend", "2026-10-17", "2026-10-18"),
    ("what did I spend on the weekend", "2026-10-17", "2026-10-18"),
    ("what's on my calendar for the weekend", "2026-10-24", "2026-10-25"),
    ("what's on my calendar the coming weekend", "2026-10-24", "2026-10-25"),
    ("what did I do the weekend before last", "2026-10-10", "2026-10-11"),
    ("steps in the past fortnight", "2026-10-08", "2026-10-21"),
    ("how much did I spend earlier this month", "2026-10-01", "2026-10-21"),
])
def test_time_phrases_read_as_their_window(cfg, question, start, end):
    w = window(cfg, question)
    assert (w["start"], w["end"]) == (start, end), w


@pytest.mark.parametrize("question, terms", [
    ("how much did I spend in September and October", []),
    ("how much did I spend from September to October", []),
    ("how much did I spend in the last week of September", []),
    ("how much did I spend over the weekend", []),
    ("how much did I spend this past weekend", []),
    ("how much did I spend on the weekend", []),
    ("what's next on my calendar", []),
    ("what did I do yesterday morning", []),
    ("anything about the lake on Saturday night", ["lake"]),
    ("how much did I spend at the end of September", []),
    ("how much did I spend at the start of October", []),
    ("how much did I spend earlier this month", []),
    ("what's on my calendar later this week", []),
    ("anything upcoming", []),
    ("what's on my calendar the coming weekend", []),
    ("notes from the morning about the lake", ["lake"]),
    ("what did I say about the deck in September and October", ["deck"]),
])
def test_time_words_are_never_search_terms(conn, cfg, question, terms):
    bundle = build(conn, cfg, question)
    assert bundle["intent"]["topic_terms"] == terms
    for word in ("september", "october", "weekend", "next", "morning", "night", "end",
                 "start", "earlier", "later", "upcoming", "coming"):
        assert f'"{word}"' not in (bundle["match"] or ""), (question, bundle["match"])


def test_a_place_named_with_a_time_word_keeps_its_name(conn, cfg):
    entities.create(conn, "place", "Sunday Market")
    conn.commit()
    bundle = build(conn, cfg, "what did I buy at Sunday Market")
    assert bundle["window"]["explicit"] is False          # not last Sunday
    assert [e["name"] for e in bundle["entities"]] == ["Sunday Market"]


# ---------------------------------------------------------------- sentences

@pytest.mark.parametrize("text", [
    "On 2026-10-09 Laura J. Kim texted you [O1].",
    "On 2026-10-10 you saw Dr. R. Ellis [O3].",
    "On 2026-10-10 you saw Dr. Ellis, D.D.S. at Ellis Dental [O3].",
    "Priya Natarajan, Ph.D. presented on 2026-10-02 [E1].",
    "You flew back to the U.S. on 2026-07-02 [E4].",
])
def test_initials_and_dotted_abbreviations_do_not_end_a_sentence(text):
    assert recall._sentences(text) == [text]


@pytest.mark.parametrize("question", ["anything about march", "who has a march birthday"])
def test_a_highlighted_search_hit_keeps_its_dates_checkable(world, question):
    """'«March» 14' in a search snippet is March 14 in the evidence too: the
    answer quoting the card's birthday passes its own check."""
    bundle, text = ask(world, question)
    assert "March 14" in text, text
    check = recall.check_citations(text, bundle)
    assert check["unknown"] == [] and check["unsupported"] == [], check


def test_a_sentence_ending_after_a_word_still_splits():
    assert recall._sentences("You texted Laura J. Kim [O1]. Then you called Mom [O2].") \
        == ["You texted Laura J. Kim [O1].", "Then you called Mom [O2]."]


def test_the_answer_about_someone_with_a_middle_initial_passes_its_own_check(conn, cfg):
    laura = entities.create(conn, "person", "Laura J. Kim")["id"]
    phone = "+12105550131"
    text_in = _rec(cfg, "android_sms", "laura-1", utc(TODAY - timedelta(days=12), "23:15"),
                   direction="in", title="Rivera Family",
                   body="See you all at Tía Rosa's on Sunday",
                   participants=[{"kind": "phone", "value": phone, "role": "sender"},
                                 {"kind": "phone", "value": ME, "role": "recipient"}])
    _apply(conn, _scan("android_sms", [text_in],
                       [{"kind": "phone", "value": phone, "display_name": None},
                        {"kind": "phone", "value": ME, "display_name": None}],
                       account=ME, source_file="sms.xml"))
    obs.link_handle(conn, "phone", phone, laura)
    conn.commit()
    bundle, text = answer(conn, cfg, "when did I last talk to Laura J. Kim")
    assert text.startswith("Last contact with Laura J. Kim: 2026-10-09, Laura J. Kim "
                           "texted you"), text
    check = recall.check_citations(text, bundle)
    assert check["unknown"] == [] and check["unsupported"] == []
    model = "On 2026-10-09 Laura J. Kim texted you in Rivera Family [O1]."
    assert recall.check_citations(model, bundle)["unsupported"] == []

"""Time words, sentence ends and hyphenated names — the parsing half of ask.

Each test here failed before its fix: a window's own words leaked into the
search terms ('September' had to appear in a transaction for September to
be searched), 'past year' meant last calendar year, 'this week' stopped at
today even for a calendar, 'Dr.' ended a sentence, and 'H-E-B' fell apart
into three letters nothing could match.
"""

from datetime import date

import pytest

from lifecrm import recall

TODAY = date(2026, 10, 21)            # a Wednesday


def window(cfg, question, **kw):
    return recall.parse_window(question, cfg, today=TODAY, **kw)


def topic(conn, cfg, question):
    return recall.build_evidence(conn, cfg, question, today=TODAY)["intent"]["topic_terms"]


# ---------------------------------------------------------------- the window's words are not the topic

@pytest.mark.parametrize("question, terms", [
    ("how much did I spend in September", []),
    ("how much did I spend during September", []),
    ("how much did I spend last weekend", []),
    ("how much did I spend on Saturday", []),
    ("how much did I spend since September", []),
    ("how much did I spend in the past two weeks", []),
    ("how much did I spend in October", []),
    ("how much did I spend at Uchi in October", ["uchi"]),
    ("how much did I spend between June and August on groceries", ["groceries"]),
    ("what's on my calendar next week", []),
    ("anything about the lake yesterday", ["lake"]),
    ("anything about the lake on 2026-10-04", ["lake"]),
    ("what did I do this evening", []),
    ("what happened since 2026-09", []),
])
def test_the_words_a_window_was_read_from_are_never_search_terms(conn, cfg, question,
                                                                 terms):
    assert topic(conn, cfg, question) == terms


def test_a_word_that_was_not_read_as_a_time_stays_a_term(conn, cfg):
    """Only what the window used goes: 'may' that is not the month, or a
    person called June, is still searched for."""
    assert topic(conn, cfg, "what did I read about may") == ["read", "may"]
    w = window(cfg, "notes about the June launch", names=frozenset({"june"}))
    assert w["explicit"] is False
    assert recall.without_window_words("notes about the June launch", w) == \
        "notes about the June launch"


def test_the_window_lists_the_spans_it_used(cfg):
    q = "How much did I spend at Uchi in early October"
    w = window(cfg, q)
    assert w["label"] == "October 2026"
    assert recall.without_window_words(q, w).split() == \
        ["How", "much", "did", "I", "spend", "at", "Uchi", "in"]
    assert "spans" not in window(cfg, "how much did I spend at Uchi")


# ---------------------------------------------------------------- past week / month / year roll

@pytest.mark.parametrize("question, start, end, label", [
    ("how much did I spend over the past year", "2025-10-22", "2026-10-21", "past year"),
    ("steps over the past month", "2026-09-22", "2026-10-21", "past month"),
    ("mood over the past week", "2026-10-15", "2026-10-21", "past week"),
    # 'last' and 'previous' months and years stay calendar ones
    ("how did I sleep last year", "2025-01-01", "2025-12-31", "last year"),
    ("weight the previous month", "2026-09-01", "2026-09-30", "last month"),
    ("since the past month", "2026-09-22", "2026-10-21", "since past month"),
])
def test_past_is_a_rolling_span_ending_today(cfg, question, start, end, label):
    w = window(cfg, question)
    assert (w["start"], w["end"], w["label"]) == (start, end, label)


# ---------------------------------------------------------------- this week, looking forward

def test_this_week_month_and_year_know_where_they_end(cfg):
    week = window(cfg, "what's on my calendar this week")
    assert (week["start"], week["end"], week["through"]) == \
        ("2026-10-19", "2026-10-21", "2026-10-25")
    month = window(cfg, "what's on my calendar this month")
    assert (month["end"], month["through"]) == ("2026-10-21", "2026-10-31")
    assert window(cfg, "plans this year")["through"] == "2026-12-31"
    # on the last day of the period there is nothing further to reach
    sunday = recall.parse_window("this week", cfg, today=date(2026, 10, 25))
    assert "through" not in sunday
    assert "through" not in window(cfg, "last week")


# ---------------------------------------------------------------- sentences and words

@pytest.mark.parametrize("text", [
    "On 2026-10-10 you saw Dr. Ellis for a dentist check-up [O3].",
    "Mr. and Mrs. Rivera came on 2026-10-09 [O4].",
    "Martin Luther King Jr. Day is on 2027-01-18 [O2].",
    "Dinner was at 7 p.m. on 2026-10-12 [O1].",
])
def test_an_abbreviation_does_not_end_a_sentence(text):
    assert recall._sentences(text) == [text]


def test_a_real_sentence_end_still_splits():
    assert recall._sentences("You saw Dr. Ellis [O3]. Then you called Mom [O2].") == \
        ["You saw Dr. Ellis [O3].", "Then you called Mom [O2]."]
    assert recall._sentences("It was late. St. Patrick's was busy.") == \
        ["It was late.", "St. Patrick's was busy."]


def test_a_hyphenated_name_is_one_word_and_one_phrase():
    words = recall._words("how much did I spend at H-E-B")
    assert words[-1]["term"] == "h-e-b" and words[-1]["key"] == "h e b"
    assert recall._fts_expr(["h-e-b", "veracruz"], " ", prefix=True) == \
        '"h-e-b" "veracruz"*'
    assert recall._terms(recall._words("my to-do list on 2026-10-04"),
                         recall.LOOP_WORDS) == []

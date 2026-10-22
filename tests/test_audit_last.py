"""Audit, last round: general guards instead of one more rule per phrase.

Each group failed before its fix.

- A money question answered with everyone's total when it named a merchant
  ask could not read: '76' or '7-11' without 'at' ('how much did I spend on
  76', '76 spending', 'how much did I pay 76'), a stream's name ('on
  ChatGPT'), a person ('how much did I pay Jake'). The guard: every word of
  a money question is accounted for, or the answer is no data for it.
- A time phrase the window parser had no rule for ('so far this month',
  'the past few days', 'the week before last', 'the second half of
  September', 'Q3', 'in the fall') left words that became required search
  terms. Every time word is now vocabulary, and the cheap phrasings windows.
- 'José Á. García' ended a sentence at 'Á.', and so did 'Mt.' and 'Co.':
  the answer about him failed its own citation check.
- 'Overdue' listed everything with a due date; 'what plans do I have this
  week' searched for "plans"; 'what's due by Saturday' asked on a Thursday
  read last Saturday.
- The calendar import told an owner to link their own address, which on an
  iPhone Home calendar never says who they are; [owner] handles does.
"""

import re
import tomllib
from datetime import date, timedelta

import pytest

from lifecrm import recall
from lifecrm.connectors import calendar as cal
from lifecrm.doctor import run_checks
from lifecrm.domain import entities, entries, observations as obs
from lifecrm.web import api
from test_ask_demo import _apply, _rec, _scan
from test_audit_ask_owner import ME_EMAIL, with_owner
from test_calendar import ev, ics, write
from test_fix_ask_world import ask, world  # noqa: F401  (fixture)

TODAY = date(2026, 10, 22)            # a Thursday
ME = "+15125550100"


def window(cfg, question, **kw):
    return recall.parse_window(question, cfg, today=TODAY, **kw)


def answer(conn, cfg, question):
    bundle = recall.build_evidence(conn, cfg, question, today=TODAY)
    return bundle, recall.deterministic_answer(bundle)


def item(bundle, cid):
    return next(x for key in ("metrics", "entries", "passages", "observations")
                for x in bundle.get(key, []) if x["id"] == cid)


def utc(d: date, hhmm: str = "18:00") -> str:
    return f"{d.isoformat()}T{hhmm}:00Z"


def own_check(text, bundle):
    check = recall.check_citations(text, bundle)
    assert check["unknown"] == [] and check["unsupported"] == [], (check, text)


# ---------------------------------------------------------------- 1. merchants

PURCHASES = (("AT&T *BILL PAYMENT", -85.00, -2), ("H&M 0123", -42.50, -3),
             ("76 - 1234 FUEL", -38.20, -5), ("7-11 #33012", -18.40, -6),
             ("UCHI", -212.40, -7), ("OPENAI *CHATGPT SUBSCR", -20.00, -8),
             ("ZELLE TO JAKE MORALES", -50.00, -9))
EVERYONE = round(-sum(amount for _, amount, _ in PURCHASES), 2)        # 466.50


@pytest.fixture
def statement(conn, cfg):
    records = [_rec(cfg, "txn", f"stmt-{n}", utc(TODAY + timedelta(days=off)),
                    title=title, value=amount, unit="USD")
               for n, (title, amount, off) in enumerate(PURCHASES)]
    _apply(conn, _scan("finance", records, [], account="discover-0042",
                       source_file="Discover-Statement-20261015.csv"))
    entities.create(conn, "person", "Jake Morales")
    entities.create(conn, "person", "Sam Okafor")         # no payments
    conn.commit()


def _spent(bundle):
    found = [m for m in bundle["metrics"] if m["metric"] == "txn_spend"]
    return (found[0]["total"], found[0]["spent_count"]) if found else None


@pytest.mark.parametrize("question, title, amount", [
    ("how much did I spend on 76", "76 - 1234 FUEL", 38.20),
    ("76 spending", "76 - 1234 FUEL", 38.20),
    ("how much did I pay 76", "76 - 1234 FUEL", 38.20),
    ("how much did 76 charge me", "76 - 1234 FUEL", 38.20),
    ("how much did I spend on 76 this month", "76 - 1234 FUEL", 38.20),
    ("how much did I spend on 7-11", "7-11 #33012", 18.40),
    ("7-11 spending", "7-11 #33012", 18.40),
    ("how much did I spend on ChatGPT", "OPENAI *CHATGPT SUBSCR", 20.00),
    ("how much did I pay Jake", "ZELLE TO JAKE MORALES", 50.00),
    ("how much did I spend with Jake at Uchi", "UCHI", 212.40),
])
def test_a_merchant_named_without_at_is_that_merchant(conn, cfg, statement, question,
                                                      title, amount):
    bundle, text = answer(conn, cfg, question)
    assert _spent(bundle) == (amount, 1), (question, text)
    assert title in text
    own_check(text, bundle)


@pytest.mark.parametrize("question", [
    "how much did I spend on X", "how much did I spend on Zorblax",
    "Zorblax spending", "how much did I pay Zorblax", "how much did Zorblax charge me",
    "how much did I spend on 99", "how much did I spend on 4-20 last week",
    "how much did I spend with Sam",
])
def test_a_name_nothing_can_use_is_no_data_never_everyones_total(conn, cfg, statement,
                                                                 question):
    """The guard: whatever the money question names — a word, a number, a
    person with no payments — is searched for or reported missing."""
    bundle, text = answer(conn, cfg, question)
    assert _spent(bundle) is None, (question, text)
    assert any(x.startswith("transactions matching") for x in bundle["no_data"]), \
        bundle["no_data"]
    assert f"{EVERYONE:,.2f}" not in text and "You spent" not in text


def test_the_guard_names_the_word_it_could_not_use(conn, cfg, statement):
    bundle, text = answer(conn, cfg, "how much did I spend on X")
    assert bundle["intent"]["unused"] == ["X"]
    assert 'transactions matching "X" (all records)' in bundle["no_data"]
    assert text.startswith('No data for: transactions matching "X" (all records)')


@pytest.mark.parametrize("question", [
    "how much did I spend", "how much did I spend this month",
    "how much did I spend over $50", "did I spend more than 100 dollars this month",
    "how much did I spend in the last 30 days", "how much did I spend in 2026",
    "how much did I spend so far this month", "how much did I spend in the past few days",
])
def test_amounts_times_and_the_frame_are_not_merchants(conn, cfg, statement, question):
    bundle, _ = answer(conn, cfg, question)
    assert bundle["intent"]["unused"] == [] and bundle["intent"]["topic_terms"] == [], \
        bundle["intent"]
    assert _spent(bundle) is not None


def test_numbers_a_merchant_can_never_be():
    words = recall._words("spent $50 on 76 in 2025, 3 days, 10% of 1,200 and 7-11")
    text = "spent $50 on 76 in 2025, 3 days, 10% of 1,200 and 7-11"
    assert recall._numeric_merchants(words, text) == {"76", "7-11"}


# ---------------------------------------------------------------- 2. time words

@pytest.mark.parametrize("question, start, end", [
    ("how much did I spend so far this month", "2026-10-01", "2026-10-22"),
    ("how much did I spend this month so far", "2026-10-01", "2026-10-22"),
    ("how much did I spend month to date", "2026-10-01", "2026-10-22"),
    ("how much did I spend YTD", "2026-01-01", "2026-10-22"),
    ("how much did I spend in the past few days", "2026-10-20", "2026-10-22"),
    ("how much did I spend in the last couple of weeks", "2026-10-09", "2026-10-22"),
    ("how much did I spend over the last several days", "2026-10-16", "2026-10-22"),
    ("how much did I spend the week before last", "2026-10-08", "2026-10-14"),
    ("what did Jake text me the night before last", "2026-10-21", "2026-10-21"),
    ("what's on my calendar for the next two weeks", "2026-10-22", "2026-11-04"),
    ("what's on my calendar in the next few days", "2026-10-22", "2026-10-24"),
    ("what's on my calendar the week after next", "2026-11-02", "2026-11-08"),
    ("how much did I spend in the second half of September", "2026-09-16", "2026-09-30"),
    ("how much did I spend in the first half of October", "2026-10-01", "2026-10-15"),
    ("how much did I spend towards the end of September", "2026-09-21", "2026-09-30"),
    ("how much did I spend near the start of October", "2026-10-01", "2026-10-10"),
    ("what did I do in the first few days of October", "2026-10-01", "2026-10-03"),
    ("how much did I spend last quarter", "2026-07-01", "2026-09-30"),
    ("how much did I spend this quarter", "2026-10-01", "2026-10-22"),
    ("how much did I spend in Q3", "2026-07-01", "2026-09-30"),
    ("how much did I spend in Q1 2025", "2025-01-01", "2025-03-31"),
    ("how much did I spend in the third quarter", "2026-07-01", "2026-09-30"),
    ("how much did I spend in the fall", "2026-09-01", "2026-10-22"),
    ("how much did I spend last winter", "2025-12-01", "2026-02-28"),
    ("how much did I spend this summer", "2026-06-01", "2026-08-31"),
    ("what did I do the other day", "2026-10-15", "2026-10-21"),
    ("what did I do a couple of days ago", "2026-10-18", "2026-10-21"),
    ("what did I do the weekend of October 10", "2026-10-10", "2026-10-11"),
    ("what did I do the week of October 5", "2026-10-05", "2026-10-11"),
    ("how much did I spend in September or October", "2026-09-01", "2026-10-31"),
    ("how much did I spend the month before last", "2026-08-01", "2026-08-31"),
])
def test_time_phrases_read_as_their_window(cfg, question, start, end):
    w = window(cfg, question)
    assert w["explicit"] and (w["start"], w["end"]) == (start, end), w


@pytest.mark.parametrize("question, start", [
    ("what's on my calendar for November", "2026-11-01"),
    ("what's on my calendar in January", "2027-01-01"),
    ("what's on my calendar in September", "2026-09-01"),
    ("what was on my calendar in November", "2025-11-01"),
    ("how much did I spend in November", "2025-11-01"),
])
def test_a_month_named_by_a_forward_question_is_the_nearest_one(cfg, question, start):
    assert window(cfg, question)["start"] == start


@pytest.mark.parametrize("question", ["what did I write about summer camp",
                                      "when did I fall"])
def test_a_season_word_alone_is_no_window(cfg, question):
    assert window(cfg, question)["explicit"] is False


_TIME_PHRASES = (
    "so far this month", "this month so far", "month to date", "year to date",
    "in the past few days", "in the last couple of weeks", "over the past couple of weeks",
    "over the last several days", "the week before last", "the night before last",
    "for the next two weeks", "the week after next", "in the next few days",
    "the weekend after next", "over the next 3 days", "in the second half of September",
    "in the first half of October", "towards the end of September",
    "near the end of September", "this quarter", "last quarter", "in Q3",
    "in the third quarter", "in Q2 2026", "in the fall", "this summer", "last winter",
    "a couple of days ago", "a few weeks ago", "in the first few days of October",
    "the other day", "the weekend of October 10", "the week of October 5",
    "in the past two weekends", "the weekend before", "in September or October",
    "in September, October", "the month before last", "so far this year",
    "in the autumn", "during the first half of the year",
)
# Read as no window at all (the default span), but still no search term.
_NO_WINDOW = {"in the past two weekends"}


@pytest.mark.parametrize("phrase", _TIME_PHRASES)
@pytest.mark.parametrize("lead", ["how much did I spend", "what did I do",
                                  "what's on my calendar"])
def test_no_time_word_is_ever_a_required_search_term(world, lead, phrase):
    """Fuzz-style: whatever the window parser makes of a time phrase, none
    of its words is ever a term the answer must contain."""
    bundle, text = ask(world, f"{lead} {phrase}")
    said = set(re.findall(r"[\w-]+", phrase.lower()))
    assert bundle["intent"]["topic_terms"] == [], (phrase, bundle["intent"]["topic_terms"])
    searched = set(re.findall(r'"([^"]+)"', bundle["match"] or ""))
    assert not searched & said, (phrase, bundle["match"])
    assert searched <= {"calendar"}, (phrase, bundle["match"])
    assert bundle["intent"]["unused"] == []
    assert bundle["window"]["explicit"] or phrase in _NO_WINDOW, (phrase, bundle["window"])
    own_check(text, bundle)


# ---------------------------------------------------------------- 3. sentences

@pytest.mark.parametrize("text", [
    "On 2026-10-09 José Á. García texted you [O1].",
    "On 2026-10-10 you saw Dr. Á. Ellis [O3].",
    "On 2026-10-10 Zoë Ö. Müller texted you [O1].",
    "On 2026-10-10 Paweł Ł. Nowak called you [O1].",
    "On 2026-10-10 you saw Dr. Ellis near Mt. Bonnell [O3].",
    "On 2026-10-10 you paid Acme Co. for the deck [O3].",
    "On 2026-10-10 you saw Dr. Ellis on Congress Ave. in Austin [O3].",
    "On 2026-10-10 you saw Dr. Ellis, DDS. at Ellis Dental [O3].",
    "On 2026-10-10 you bought milk, eggs etc. at H-E-B [O3].",
])
def test_initials_in_any_alphabet_and_abbreviations_do_not_end_a_sentence(text):
    assert recall._sentences(text) == [text]


def test_a_sentence_after_an_abbreviation_still_splits():
    assert recall._sentences("You paid Acme Co. [O1]. Then you called Mom [O2].") \
        == ["You paid Acme Co. [O1].", "Then you called Mom [O2]."]
    assert recall._sentences("You saw Á. García [O1]. Then you ran [M1].") \
        == ["You saw Á. García [O1].", "Then you ran [M1]."]


def test_the_answer_about_someone_with_an_accented_initial_passes_its_own_check(conn,
                                                                                 cfg):
    jose = entities.create(conn, "person", "José Á. García")["id"]
    phone = "+12105550131"
    text_in = _rec(cfg, "android_sms", "jose-1", utc(date(2026, 10, 10), "04:15"),
                   direction="in", title="Rivera Family", body="Qué bonita familia",
                   participants=[{"kind": "phone", "value": phone, "role": "sender"},
                                 {"kind": "phone", "value": ME, "role": "recipient"}])
    _apply(conn, _scan("android_sms", [text_in],
                       [{"kind": "phone", "value": phone, "display_name": None},
                        {"kind": "phone", "value": ME, "display_name": None}],
                       account=ME, source_file="sms.xml"))
    obs.link_handle(conn, "phone", phone, jose)
    conn.commit()
    bundle, text = answer(conn, cfg, "when did I last talk to José Á. García")
    assert text.startswith("Last contact with José Á. García: 2026-10-09, José Á. García "
                           "texted you"), text
    own_check(text, bundle)
    model = "On 2026-10-09 José Á. García texted you in Rivera Family [O1]."
    assert recall.check_citations(model, bundle)["unsupported"] == []


# ---------------------------------------------------------------- 4. tasks

@pytest.fixture
def tasks(conn):
    def loop(title, day, due=None, kind="task"):
        return entries.create(conn, journal_date=day, kind=kind, title=title,
                              primary_category="chores", due_at=due)["uuid"]
    out = {"overdue": loop("Renew passport", "2026-10-01", "2026-10-16"),
           "saturday": loop("Book flights to Ljubljana", "2026-10-05", "2026-10-24"),
           "later": loop("Visit Mom in San Antonio", "2026-10-06", "2026-11-15",
                         kind="plan"),
           "undated": loop("Plan the Bled hike", "2026-10-17", kind="plan")}
    conn.commit()
    return out


def _loop_titles(bundle):
    return [item(bundle, i)["title"] for i in bundle["loops"]]


@pytest.mark.parametrize("question", ["what's overdue", "what tasks are overdue",
                                      "anything overdue", "which tasks are overdue this week",
                                      "what's overdue this week", "is anything past due"])
def test_overdue_is_due_before_today(conn, cfg, tasks, question):
    bundle, text = answer(conn, cfg, question)
    assert bundle["intent"]["loops"] and bundle["intent"]["overdue"], bundle["intent"]
    assert _loop_titles(bundle) == ["Renew passport"], (question, text)
    assert text.startswith("Overdue tasks and plans: 2026-10-01 Renew passport "
                           "(task, due 2026-10-16)"), text
    own_check(text, bundle)


def test_nothing_overdue_says_so(conn, cfg):
    entries.create(conn, journal_date="2026-10-17", kind="task", title="Call the bank",
                   primary_category="chores", due_at="2026-10-30")
    conn.commit()
    bundle, text = answer(conn, cfg, "what's overdue")
    assert bundle["loops"] == [] and bundle["loops_open"] == 1
    assert text == "No open tasks or plans are overdue (1 open in total)."


@pytest.mark.parametrize("question", ["what plans do I have this week",
                                      "which plans are open", "any plans this week"])
def test_plans_reach_the_open_loops(conn, cfg, tasks, question):
    bundle, text = answer(conn, cfg, question)
    assert bundle["intent"]["loops"], bundle["intent"]
    assert bundle["intent"]["topic_terms"] == [] and bundle["match"] is None
    assert "Plan the Bled hike" in _loop_titles(bundle)
    if "week" in question:
        assert bundle["window"]["end"] == "2026-10-25"
    assert text.startswith("Open tasks and plans: ")


@pytest.mark.parametrize("question, day", [
    ("what's due by Saturday", "2026-10-24"),
    ("what tasks are due on Saturday", "2026-10-24"),
    ("what's due Friday", "2026-10-23"),
    ("what's due Thursday", "2026-10-22"),
])
def test_a_weekday_in_a_due_question_is_the_coming_one(conn, cfg, tasks, question, day):
    bundle, text = answer(conn, cfg, question)
    assert (bundle["window"]["start"], bundle["window"]["end"]) == (day, day)
    titles = _loop_titles(bundle)
    assert titles[0] == "Renew passport"
    assert ("Book flights to Ljubljana" in titles) == (day >= "2026-10-24"), titles
    own_check(text, bundle)


def test_a_weekday_in_a_past_question_is_still_the_last_one(cfg):
    assert window(cfg, "what did I do on Saturday")["start"] == "2026-10-17"
    assert window(cfg, "what was due last Saturday")["start"] == "2026-10-17"


# ---------------------------------------------------------------- 5. who you are

def _home_ics(tmp_path):
    return write(tmp_path / "in", ics(
        ev("dentist", ":20261015T150000Z", f"ORGANIZER;CN=Alex Rivera:mailto:{ME_EMAIL}",
           "ATTENDEE;CN=Dr. Rebecca Ellis:mailto:frontdesk@ellisdental.example",
           summary="Dentist: Dr. Ellis"),
        ev("lunch", ":20261016T170000Z", f"ORGANIZER;CN=Alex Rivera:mailto:{ME_EMAIL}",
           "ATTENDEE;CN=Sam Okafor:mailto:sam.okafor@example.com",
           summary="Lunch with Sam")))


def test_the_calendar_report_prints_the_owner_lines_that_work(tmp_path, conn, cfg):
    scan = cal.scan(_home_ics(tmp_path), cfg)
    assert scan["account"] == "calendar:Home"
    lines = cal.report_lines(scan, obs.build_plan(conn, scan))
    at = next(i for i, ln in enumerate(lines) if "your own address is likely" in ln)
    assert "config.toml" in lines[at] and "People" in lines[at]
    snippet = "\n".join(ln.strip() for ln in lines[at + 1:at + 3])
    assert snippet == f'[owner]\nhandles = ["email:{ME_EMAIL}"]'

    # Following it: apply, link everyone (the old advice), add the printed table.
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    for name, handle in (("Alex Rivera", ME_EMAIL),
                         ("Dr. Rebecca Ellis", "frontdesk@ellisdental.example"),
                         ("Sam Okafor", "sam.okafor@example.com")):
        obs.link_handle(conn, "email", handle, entities.create(conn, "person", name)["id"])
    conn.commit()
    assert "Alex Rivera" in [p["name"] for p in api.people(conn, cfg)["people"]]
    mine = with_owner(cfg, **tomllib.loads(snippet)["owner"])
    assert "Alex Rivera" not in [p["name"] for p in api.people(conn, mine)["people"]]


def test_the_report_says_nothing_more_once_owner_lists_the_address(tmp_path, conn, cfg):
    mine = with_owner(cfg, handles=[f"email:{ME_EMAIL}"])
    scan = cal.scan(_home_ics(tmp_path), mine)
    text = "\n".join(cal.report_lines(scan, obs.build_plan(conn, scan)))
    assert "your own address" not in text

    other = with_owner(cfg, handles=["phone:+15125550100"])
    scan = cal.scan(_home_ics(tmp_path / "b"), other)
    text = "\n".join(cal.report_lines(scan, obs.build_plan(conn, scan)))
    assert f'add "email:{ME_EMAIL}" to [owner] handles in config.toml' in text


def _check(report, name):
    return next((c for c in report["checks"] if c["check"] == name), None)


def test_doctor_warns_while_nothing_says_who_you_are(tmp_path, conn, cfg):
    assert _check(run_checks(conn, cfg), "owner_unknown") is None     # nobody imported yet
    scan = cal.scan(_home_ics(tmp_path), cfg)
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    warn = _check(run_checks(conn, cfg), "owner_unknown")
    assert warn["status"] == "warn" and "[owner] handles" in warn["detail"]
    mine = with_owner(cfg, handles=[f"email:{ME_EMAIL}"])
    assert _check(run_checks(conn, mine), "owner_unknown")["status"] == "ok"

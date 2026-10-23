"""The dashboards' cards for the newer sources: email, workouts, listening,
searches and the food diary — seeded by test_dashboard's _seed (which every
card already runs against in every filter form) and read back here card by
card. The stream lists the SQL spells out are held to recall's shared lists,
so a messaging app added there cannot be silently missing here."""

import re

from lifecrm import recall
from test_dashboard import FILTERED, _chart, _seed, run


def _rows(conn, key, options=None):
    return run(conn, _chart(key), options or {})[1]


def _streams_in(query: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", query))


def test_text_cards_cover_every_messaging_app_recall_knows():
    for key in ("observed.messages-count", "observed.texts-per-day",
                "observed.top-people"):
        assert set(recall.TEXT_STREAMS) <= _streams_in(_chart(key)["query"]), key


def test_people_cards_count_contact_by_recall_s_contact_groups():
    contact = {s for g in recall.CONTACT_GROUPS for s in recall.STREAM_GROUPS[g]}
    for key in ("people.people-in-touch", "people.observed-contacts",
                "people.contacts-per-week", "people.contacts-by-tier",
                "people.last-contact"):
        assert contact <= _streams_in(_chart(key)["query"]), key


def test_emails_per_week_split_by_direction(conn):
    _seed(conn)
    assert _rows(conn, "observed.emails-per-week") == [
        {"week": "2026-07-20", "week_end": "2026-07-27", "mail": "received",
         "emails": 2},
        {"week": "2026-07-20", "week_end": "2026-07-27", "mail": "sent", "emails": 1},
        {"week": "2026-07-20", "week_end": "2026-07-27", "mail": "to yourself",
         "emails": 1}]
    assert _rows(conn, "observed.emails-per-week", {"date_end": "2026-07-21"}) == [
        {"week": "2026-07-20", "week_end": "2026-07-27", "mail": "received",
         "emails": 1}]


def test_logged_workouts_per_week_in_minutes_by_app(conn):
    _seed(conn)
    assert _rows(conn, "health.logged-workouts-per-week") == [
        {"week": "2026-07-20", "week_end": "2026-07-27", "app": "strava",
         "sessions": 1, "minutes": 30.0},
        {"week": "2026-07-20", "week_end": "2026-07-27", "app": "strong",
         "sessions": 1, "minutes": 60.0},
        {"week": "2026-06-08", "week_end": "2026-06-15", "app": "strong",
         "sessions": 1, "minutes": 45.0}]
    assert len(_rows(conn, "health.logged-workouts-per-week", FILTERED)) == 2


def test_listening_and_searches_per_week(conn):
    _seed(conn)
    assert _rows(conn, "observed.listening-per-week") == [
        {"week": "2026-07-20", "week_end": "2026-07-27", "kind": "music",
         "minutes": 30.5, "tracks": 1},
        {"week": "2026-07-20", "week_end": "2026-07-27", "kind": "podcast",
         "minutes": 44.0, "tracks": 1}]
    assert _rows(conn, "observed.searches-per-week") == [
        {"week": "2026-07-27", "week_end": "2026-08-03", "product": "youtube",
         "searches": 1},
        {"week": "2026-07-20", "week_end": "2026-07-27", "product": "google_search",
         "searches": 1}]
    assert _rows(conn, "observed.searches-per-week", {"date_end": "2026-07-26"}) == [
        {"week": "2026-07-20", "week_end": "2026-07-27", "product": "google_search",
         "searches": 1}]


def test_food_diary_takes_the_daily_summary_else_sums_the_food_log(conn):
    _seed(conn)
    # 07-20 has a Cronometer daily summary (its oatmeal is not added again);
    # 07-21 has only food-log rows, summed
    assert _rows(conn, "health.diary-kcal-per-day") == [
        {"day": "2026-07-21", "kcal": 1500.0, "basis": "food log summed"},
        {"day": "2026-07-20", "kcal": 2400.0, "basis": "daily summary"}]
    assert _rows(conn, "health.diary-protein-per-day") == [
        {"day": "2026-07-21", "protein": 95.0},
        {"day": "2026-07-20", "protein": 150.0}]
    assert _rows(conn, "health.diary-kcal-per-day", {"date_start": "2026-07-21"}) == [
        {"day": "2026-07-21", "kcal": 1500.0, "basis": "food log summed"}]


def test_an_email_someone_else_sent_does_not_make_a_cc_a_contact(conn):
    _seed(conn)
    # Jake was only copied on Priya's 07-23 mail: his contact days stay the
    # texts, the call, the dinner invite and the two emails he exchanged.
    weeks = _rows(conn, "people.contacts-per-week", {"date_start": "2026-07-23",
                                                     "date_end": "2026-07-23"})
    assert all(r["person"] != "Jake Morales" for r in weeks)

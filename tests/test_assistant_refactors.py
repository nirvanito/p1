"""The groundwork refactors under the assistant layer keep today's behaviour:
the derived-entry filter (an entry without a generated marker is evidence),
the citation checker over any evidence index, the metric series' record
references, and the shared iCalendar writer the demo renders through."""

from __future__ import annotations

from lifecrm import ics, recall, review
from lifecrm.demo.sources import calendar as demo_calendar
from lifecrm.domain import entries, health, measurements

DAY = "2026-10-20"
NEXT = "2026-10-21"


def _entry(conn, title, data=None, kind="event"):
    return entries.create(conn, journal_date=DAY, kind=kind, title=title,
                          primary_category="other", data=data)


# ---------------------------------------------------------------- derived filter

def test_not_derived_keeps_every_entry_without_a_generated_marker(conn):
    kept = [
        _entry(conn, "plain"),
        _entry(conn, "empty data", {}),
        _entry(conn, "a goal", kind="plan",
               data={"goal": {"metric": "workouts", "target": 3, "per": "week"}}),
        _entry(conn, "someone else's marker", {"derived_from": "my own notebook"}),
    ]
    dropped = [
        _entry(conn, "review brief", kind="reflection",
               data={"derived_from": review.BRIEF_MARK, "lens": "week"}),
        _entry(conn, "morning brief", kind="reflection",
               data={"derived_from": "lifecrm morning"}),
    ]
    # Invalid JSON cannot arrive through the domain layer; a corrupted row is
    # still evidence rather than silently dropped.
    conn.execute("UPDATE entries SET data='{not json' WHERE uuid=?", (kept[0]["uuid"],))
    conn.commit()

    titles = {r["title"] for r in conn.execute(
        f"SELECT title FROM entries WHERE {review.NOT_DERIVED}")}
    assert titles == {"plain", "empty data", "a goal", "someone else's marker"}

    def data_of(e):
        return conn.execute("SELECT data FROM entries WHERE uuid=?",
                            (e["uuid"],)).fetchone()["data"]
    assert all(review.is_derived(data_of(e)) is False for e in kept)
    assert all(review.is_derived(data_of(e)) is True for e in dropped)
    assert review.is_derived(None) is False and review.is_derived("[1, 2]") is False


# ---------------------------------------------------------------- check_text

LINES = {"N1": "[N1] due: Book flights to Ljubljana — due 2026-10-24 (in 3 days)",
         "N2": "[N2] birthday: Sam Okafor turns 34 on 2026-10-25 (observed, not confirmed)"}


def test_check_text_accepts_numbers_and_dates_the_cited_lines_state():
    r = recall.check_text("Book the flights by 2026-10-24, 3 days out [N1]. "
                          "Sam turns 34 on 2026-10-25 [N2].", LINES)
    assert r == {"cited": ["N1", "N2"], "unknown": [], "uncited": False,
                 "unsupported": []}


def test_check_text_rejects_an_invented_id_and_an_unsupported_number():
    r = recall.check_text("Flights are due in 5 days [N1]. Also [N9].", LINES)
    assert r["unknown"] == ["N9"]
    assert r["unsupported"] == ["Flights are due in 5 days [N1]."]
    assert recall.check_text("Nothing cited here.", LINES)["uncited"] is True


def test_check_text_never_takes_a_number_from_a_uuid():
    # the line's uuid contains "-4a…": its digits are an address, not a fact
    lines = {"N1": "nudge due · due:7e975b84-2ccc-4a66-aec6-9b55a2ee9aaf · Overdue by 3 "
                   "days: Renew passport · import 2bb06f1fa76b4fc9afebaf2bf0b041d7"}
    r = recall.check_text("The passport is 4 days overdue [N1].", lines)
    assert r["unsupported"] == ["The passport is 4 days overdue [N1]."]
    assert not recall.check_text("The passport is 3 days overdue [N1].", lines)["unsupported"]


def test_check_text_context_lends_its_facts_only_to_the_id_it_names():
    ctx = ["TODAY: 2026-10-21", "N2 card last updated 2026-09-30"]
    ok = recall.check_text("Today is 2026-10-21 and Sam's card dates from "
                           "2026-09-30 [N2].", LINES, ctx)
    assert not ok["unsupported"]
    wrong_id = recall.check_text("Sam's card dates from 2026-09-30 [N1].", LINES, ctx)
    assert wrong_id["unsupported"]


# ---------------------------------------------------------------- metric series refs

def _steps(day, value, ext, app):
    return {"metric": "steps", "journal_date": day, "started_at": f"{day}T10:00:00Z",
            "value": value, "unit": "count", "external_id": ext,
            "source_file": "export.zip", "origin_app": app}


def test_metric_series_refs_cites_the_rows_behind_each_day(conn):
    m1 = measurements.add(conn, journal_date=DAY, metric="mood", numeric_value=7)
    m2 = measurements.add(conn, journal_date=NEXT, metric="mood", numeric_value=5)
    fit = health.insert(conn, _steps(DAY, 4000, "fit-1", "Fit"))
    sam1 = health.insert(conn, _steps(DAY, 6000, "sam-1", "Samsung Health"))
    sam2 = health.insert(conn, _steps(DAY, 1000, "sam-2", "Samsung Health"))
    conn.commit()
    window = {"start": DAY, "end": NEXT}

    plain = recall._metric_series(conn, table="measurements", metric="mood",
                                  spec=recall.JOURNAL_METRICS["mood"], window=window)
    assert "uuids" not in plain["series"][0] and plain["series"][0]["records"] == 1
    refs = recall._metric_series(conn, table="measurements", metric="mood",
                                 spec=recall.JOURNAL_METRICS["mood"], window=window,
                                 refs=True)
    assert [p["uuids"] for p in refs["series"]] == [[m1["uuid"]], [m2["uuid"]]]

    steps = recall._metric_series(conn, table="health_records", metric="steps",
                                  spec=recall.HEALTH_METRICS["steps"], window=window,
                                  refs=True)
    day = steps["series"][0]
    # The summed day took the app with the larger total; only its rows are cited.
    assert day["value"] == 7000 and day["origin"] == "Samsung Health"
    assert day["uuids"] == sorted([sam1, sam2]) and fit not in day["uuids"]


# ---------------------------------------------------------------- ics writer

def test_ics_folds_at_75_octets_without_splitting_a_character():
    line = "DESCRIPTION:" + "é" * 60          # two octets each
    out = ics.render([line])
    first = out.split(b"\r\n")[0]
    assert len(first) <= 75 and out.endswith(b"\r\n")
    assert out.decode("utf-8").replace("\r\n ", "") == line + "\r\n"
    assert ics.esc("a;b,c\\d\ne") == "a\\;b\\,c\\\\d\\ne"


def test_the_demo_calendar_renders_through_the_shared_writer():
    assert demo_calendar._fold is ics.fold
    assert demo_calendar._ics is ics.render
    assert demo_calendar._esc is ics.esc

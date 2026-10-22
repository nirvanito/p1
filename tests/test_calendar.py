"""Calendar connector: .ics exports from Google, Apple and Outlook.

The connector is a pure reader, so most of this exercises scan() against
iCalendar text written here, shaped like what each app exports — including
the parts that are easy to get wrong: dating an event in its own zone, the
all-day exclusive end, repeating events across a daylight-saving change,
overrides that must keep their slot's id, rooms that are not people, and a
mirror that must never mistake "outside today's window" or "could not read
it" for "deleted".
"""

import json
import os
import zipfile
from datetime import date

import pytest
from conftest import cli_project
from test_doctor import _seed
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import calendar as cal
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()
TODAY = date(2026, 10, 21)


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    """The expansion window hangs from today; pin it so counts are exact."""
    monkeypatch.setattr(cal, "_today", lambda cfg: TODAY)


@pytest.fixture
def vouched(cfg):
    """The owner vouches that Home and Work are the only calendars of those
    names they export — what a single calendar known only by its name needs
    before its deletions are mirrored (tests/test_fix_connectors_calendar.py)."""
    cfg.connectors = {"calendar": {"mirror_calendars": ["Home", "Work"]}}
    return cfg


def ics(*events, calname="Home", prodid="-//Apple Inc.//macOS 15.1//EN", extra=()):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{prodid}"]
    if calname:
        lines.append(f"X-WR-CALNAME:{calname}")
    lines += list(extra)
    for ev in events:
        lines += ["BEGIN:VEVENT", *ev, "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def ev(uid, start, *more, summary="Event"):
    return [f"UID:{uid}", f"DTSTART{start}", f"SUMMARY:{summary}",
            "LAST-MODIFIED:20261001T120000Z", *more]


def write(tmp_path, data, name="Home.ics"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / name
    p.write_bytes(data)
    return p


def scan_of(tmp_path, cfg, *events, **kw):
    return cal.scan(write(tmp_path, ics(*events, **kw)), cfg)


def _by_id(scan):
    return {r["external_id"]: r for r in scan["records"]}


def _detail(rec):
    return json.loads(rec["value_json"])


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _ids(conn):
    return sorted(r["external_id"] for r in conn.execute(
        "SELECT external_id FROM observations WHERE stream='calendar_event' "
        "AND status='active'"))


# ---------------------------------------------------------------- scan shape

def test_a_single_ics_is_one_calendar_named_after_itself(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev("a1", ":20261020T150000Z", "DTEND:20261020T160000Z"))
    assert scan["connector"] == "calendar"
    assert scan["account"] == "calendar:Home"
    assert scan["target_kind"] == "file"
    rec = scan["records"][0]
    assert rec["stream"] == "calendar_event"
    assert rec["external_id"] == "a1@20261020T150000Z"
    assert rec["origin_app"] == "apple_calendar"
    assert rec["direction"] is None
    assert scan["source_hash"]


def test_without_a_calendar_name_the_file_name_is_used(tmp_path, cfg):
    scan = cal.scan(write(tmp_path, ics(ev("a1", ":20261020T150000Z"), calname=None),
                          name="Work.ics"), cfg)
    assert scan["account"] == "calendar:Work"
    assert _detail(scan["records"][0])["calendar"] == "Work"


def test_title_body_location_and_value_json(tmp_path, cfg):
    rec = scan_of(tmp_path, cfg, ev(
        "a1", ":20261020T150000Z", "DTEND:20261020T163000Z",
        r"DESCRIPTION:Bring the deck\nand the\, um\; notes",
        r"LOCATION:Veracruz All Natural\n1704 E Cesar Chavez St\, Austin\, TX",
        "STATUS:CONFIRMED", "URL:https://example.com/e/1",
        summary=r"Dinner\, then drinks"))["records"][0]
    assert rec["title"] == "Dinner, then drinks"
    assert rec["body"] == "Bring the deck\nand the, um; notes"
    assert rec["ended_at"] == "2026-10-20T16:30:00Z"
    d = _detail(rec)
    assert d == {"calendar": "Home", "all_day": False, "recurring": False,
                 "location": "Veracruz All Natural, 1704 E Cesar Chavez St, Austin, TX",
                 "status": "confirmed", "url": "https://example.com/e/1"}


def test_an_event_with_no_summary_still_has_a_title(tmp_path, cfg):
    data = ics(["UID:a1", "DTSTART:20261020T150000Z"])
    assert cal.scan(write(tmp_path, data), cfg)["records"][0]["title"] == "(no title)"


def test_google_meet_boilerplate_and_html_are_stripped_from_the_body(tmp_path, cfg):
    rule = "-::~:~::~:~:~:~:~:~:~:~:~:~:~:~::~:~::-"
    desc = (r"<b>Agenda</b><br>- the deck<br><a href=\"https://docs.example/d\">doc</a>"
            r"\n\n" + rule + r"\nJoin with Google Meet: https://meet.google.com/abc"
            r"\nPlease do not edit this section.\n" + rule)
    rec = scan_of(tmp_path, cfg, ev("g1", ":20261020T150000Z", f"DESCRIPTION:{desc}",
                                    "X-GOOGLE-CONFERENCE:https://meet.google.com/abc"),
                  prodid="-//Google Inc//Google Calendar 70.9054//EN")["records"][0]
    assert rec["body"] == "Agenda\n- the deck\ndoc (https://docs.example/d)"
    assert "meet.google.com" not in rec["body"]
    assert _detail(rec)["url"] == "https://meet.google.com/abc"
    assert rec["origin_app"] == "google_calendar"


def test_utf8_bom_export_is_accepted(tmp_path, cfg):
    """PowerShell and Notepad write BOMs; every reader inherits the lesson."""
    p = write(tmp_path, b"\xef\xbb\xbf" + ics(ev("a1", ":20261020T150000Z")))
    assert len(cal.scan(p, cfg)["records"]) == 1


# ---------------------------------------------------------------- time

def test_an_event_with_a_zone_is_dated_and_offset_in_that_zone(tmp_path, cfg):
    """08:00 in Ljubljana is 01:00 in Chicago — before the rollover there —
    but the breakfast belongs to its own morning, not the night before."""
    rec = scan_of(tmp_path, cfg, ev("b1", ";TZID=Europe/Ljubljana:20220928T080000",
                                    "DTEND;TZID=Europe/Ljubljana:20220928T090000"))["records"][0]
    assert rec["started_at"] == "2022-09-28T06:00:00Z"
    assert rec["ended_at"] == "2022-09-28T07:00:00Z"
    assert rec["journal_date"] == "2022-09-28"
    assert rec["utc_offset_sec"] == 7200


def test_a_utc_event_is_dated_in_the_home_zone_with_rollover(tmp_path, cfg):
    """06:30Z is 01:30 in America/Chicago, before the 05:00 rollover."""
    rec = scan_of(tmp_path, cfg, ev("u1", ":20261021T063000Z"))["records"][0]
    assert rec["journal_date"] == "2026-10-20"
    assert rec["utc_offset_sec"] is None
    assert rec["ended_at"] == rec["started_at"]          # no DTEND, no DURATION


def test_a_floating_time_is_read_in_the_home_timezone(tmp_path, cfg):
    rec = scan_of(tmp_path, cfg, ev("f1", ":20261020T100000"))["records"][0]
    assert rec["started_at"] == "2026-10-20T15:00:00Z"
    assert rec["utc_offset_sec"] == -18000


def test_an_all_day_event_keeps_its_date_and_ends_at_the_next_midnight(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg,
                   ev("d1", ";VALUE=DATE:20261020", "DTEND;VALUE=DATE:20261021"),
                   ev("d2", ";VALUE=DATE:20261102", "DTEND;VALUE=DATE:20261107"),
                   ev("d3", ";VALUE=DATE:20261110"))
    one, five, bare = (_by_id(scan)[k] for k in ("d1@20261020", "d2@20261102", "d3@20261110"))
    assert one["journal_date"] == "2026-10-20"        # no rollover shift for a date
    assert one["started_at"] == "2026-10-20T05:00:00Z"
    assert one["ended_at"] == "2026-10-21T05:00:00Z"
    assert five["ended_at"] == "2026-11-07T06:00:00Z"   # across the DST change
    assert bare["ended_at"] == "2026-11-11T06:00:00Z"   # a date alone lasts one day
    assert _detail(one)["all_day"] is True
    assert one["utc_offset_sec"] is None


def test_duration_sets_the_end(tmp_path, cfg):
    rec = scan_of(tmp_path, cfg, ev("p1", ":20261020T150000Z", "DURATION:PT1H30M"))["records"][0]
    assert rec["ended_at"] == "2026-10-20T16:30:00Z"


def test_an_outlook_windows_zone_name_is_mapped_and_reported(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev("o1", ";TZID=Central Standard Time:20261020T100000"),
                   prodid="-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN")
    rec = scan["records"][0]
    assert rec["started_at"] == "2026-10-20T15:00:00Z"
    assert rec["origin_app"] == "outlook"
    assert any("Central Standard Time" in u["what"] and "America/Chicago" in u["what"]
               for u in scan["unmapped"])


@pytest.mark.parametrize("tzid", ["UTC", "Etc/UTC", "(UTC) Coordinated Universal Time"])
def test_a_utc_tzid_is_dated_in_the_home_zone_like_a_z_time(tmp_path, cfg, tzid):
    """Outlook writes TZID=UTC where Google writes 'Z'. 01:00 UTC is 20:00 the
    evening before in Chicago — the event's own zone is unknown either way."""
    rec = scan_of(tmp_path, cfg, ev("z1", f";TZID={tzid}:20261021T010000"),
                  prodid="-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN")["records"][0]
    assert rec["started_at"] == "2026-10-21T01:00:00Z"
    assert rec["journal_date"] == "2026-10-20"
    assert rec["utc_offset_sec"] is None


def test_last_modified_wins_over_dtstamp_which_wins_over_created(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg,
                   ["UID:m1", "DTSTART:20261020T150000Z", "SUMMARY:x",
                    "DTSTAMP:20261021T120000Z", "LAST-MODIFIED:20261001T080000Z"],
                   ["UID:m2", "DTSTART:20261020T150000Z", "SUMMARY:x",
                    "DTSTAMP:20261021T120000Z", "CREATED:20260901T080000Z"],
                   ["UID:m3", "DTSTART:20261020T150000Z", "SUMMARY:x",
                    "CREATED:20260901T080000Z"])
    got = {r["external_id"][:2]: r["last_modified_at"] for r in scan["records"]}
    assert got == {"m1": "2026-10-01T08:00:00Z", "m2": "2026-10-21T12:00:00Z",
                   "m3": "2026-09-01T08:00:00Z"}


# ---------------------------------------------------------------- repeating events

def test_a_weekly_series_is_one_row_per_occurrence_through_the_horizon(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev("w1", ";TZID=America/Chicago:20260901T100000",
                                     "DTEND;TZID=America/Chicago:20260901T103000",
                                     "RRULE:FREQ=WEEKLY;BYDAY=TU"))
    starts = [r["started_at"][:10] for r in scan["records"]]
    assert starts[0] == "2026-09-01"
    assert starts[-1] == "2027-01-19"                  # today + 90 days, a Tuesday
    assert len(starts) == 21
    d = _detail(scan["records"][0])
    assert d["recurring"] is True and d["rrule"] == "FREQ=WEEKLY;BYDAY=TU"
    assert scan["facts"]["series"] == 1 and scan["facts"]["occurrences"] == 21


def test_occurrences_keep_their_wall_clock_time_across_daylight_saving(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev("w1", ";TZID=America/Chicago:20261027T100000",
                                     "RRULE:FREQ=WEEKLY;COUNT=2"))
    assert [r["started_at"] for r in scan["records"]] == [
        "2026-10-27T15:00:00Z", "2026-11-03T16:00:00Z"]
    assert [r["utc_offset_sec"] for r in scan["records"]] == [-18000, -21600]


def test_count_interval_and_utc_until(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg,
                   ev("c1", ";TZID=America/Chicago:20261001T090000",
                      "RRULE:FREQ=DAILY;INTERVAL=2;COUNT=3"),
                   ev("u1", ";TZID=America/Chicago:20261001T090000",
                      "RRULE:FREQ=WEEKLY;UNTIL=20261015T140000Z"))
    ids = sorted(_by_id(scan))
    assert [i for i in ids if i.startswith("c1")] == [
        "c1@20261001T140000Z", "c1@20261003T140000Z", "c1@20261005T140000Z"]
    assert [i for i in ids if i.startswith("u1")] == [
        "u1@20261001T140000Z", "u1@20261008T140000Z", "u1@20261015T140000Z"]


def test_exdate_removes_an_occurrence(tmp_path, cfg, vouched):
    scan = scan_of(tmp_path, cfg, ev("x1", ";TZID=America/Chicago:20261001T090000",
                                     "RRULE:FREQ=WEEKLY;COUNT=4",
                                     "EXDATE;TZID=America/Chicago:20261008T090000"))
    assert sorted(_by_id(scan)) == ["x1@20261001T140000Z", "x1@20261015T140000Z",
                                    "x1@20261022T140000Z"]
    assert "1 × occurrence removed by EXDATE" in scan["skipped"]
    assert "x1@20261008T140000Z" not in scan["present_ids"]


def test_rdate_adds_an_occurrence(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev("r1", ";VALUE=DATE:20261001",
                                     "RDATE;VALUE=DATE:20261003,20261009"))
    assert sorted(_by_id(scan)) == ["r1@20261001", "r1@20261003", "r1@20261009"]


def test_an_override_replaces_its_occurrence_and_keeps_its_id(tmp_path, cfg):
    """The 1:1 moved to 11:30 and retitled: still the same slot, so a
    re-import updates the row rather than adding a second one."""
    scan = scan_of(tmp_path, cfg,
                   ev("s1", ";TZID=America/Chicago:20261001T100000",
                      "DTEND;TZID=America/Chicago:20261001T103000",
                      "RRULE:FREQ=WEEKLY;COUNT=3", summary="1:1"),
                   ev("s1", ";TZID=America/Chicago:20261008T113000",
                      "DTEND;TZID=America/Chicago:20261008T120000",
                      "RECURRENCE-ID;TZID=America/Chicago:20261008T100000",
                      summary="1:1 (moved)"))
    recs = _by_id(scan)
    assert sorted(recs) == ["s1@20261001T150000Z", "s1@20261008T150000Z",
                            "s1@20261015T150000Z"]
    moved = recs["s1@20261008T150000Z"]
    assert moved["title"] == "1:1 (moved)"
    assert moved["started_at"] == "2026-10-08T16:30:00Z"
    assert _detail(moved)["original_start"] == "2026-10-08T15:00:00Z"
    assert _detail(moved)["recurring"] is True
    assert "original_start" not in _detail(recs["s1@20261015T150000Z"])


def test_a_cancelled_override_removes_its_occurrence(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg,
                   ev("s1", ";TZID=America/Chicago:20261001T100000", "RRULE:FREQ=WEEKLY;COUNT=3"),
                   ev("s1", ";TZID=America/Chicago:20261008T100000",
                      "RECURRENCE-ID;TZID=America/Chicago:20261008T100000", "STATUS:CANCELLED"))
    assert sorted(_by_id(scan)) == ["s1@20261001T150000Z", "s1@20261015T150000Z"]
    assert "1 × cancelled occurrence" in scan["skipped"]


def test_an_override_without_its_series_stands_alone(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev("o1", ":20261008T160000Z",
                                     "RECURRENCE-ID:20261008T150000Z", summary="one instance"))
    rec = _by_id(scan)["o1@20261008T150000Z"]
    assert rec["title"] == "one instance" and rec["started_at"] == "2026-10-08T16:00:00Z"


def test_an_unsupported_rule_keeps_the_first_occurrence_and_stops_mirroring(tmp_path, cfg, conn):
    scan = scan_of(tmp_path, cfg, ev("h1", ":20261020T150000Z", "RRULE:FREQ=HOURLY;COUNT=5"))
    assert sorted(_by_id(scan)) == ["h1@20261020T150000Z"]
    assert any("FREQ=HOURLY" in u["what"] and "first occurrence kept" in u["what"]
               for u in scan["unmapped"])
    assert "present_ids" not in scan
    plan = obs.build_plan(conn, scan)
    text = "\n".join(cal.report_lines(scan, plan))
    assert "not mirroring deletions this run" in text
    assert "a repeat rule it cannot expand" in text


def test_a_series_keeps_its_newest_thousand_occurrences(tmp_path, cfg, vouched):
    scan = scan_of(tmp_path, cfg, ev("d1", ";VALUE=DATE:20220101", "RRULE:FREQ=DAILY"))
    days = sorted(r["journal_date"] for r in scan["records"])
    assert len(days) == 1000
    assert days[-1] == "2027-01-19"
    assert any("older than the newest 1000" in s for s in scan["skipped"])
    assert "d1@20220101" in scan["present_ids"]        # still in the calendar


def test_the_horizon_is_configurable(tmp_path, cfg):
    cfg.connectors = {"calendar": {"horizon_days": 0}}
    scan = scan_of(tmp_path, cfg, ev("w1", ";VALUE=DATE:20261001", "RRULE:FREQ=WEEKLY"))
    assert max(r["journal_date"] for r in scan["records"]) == "2026-10-15"
    cfg.connectors = {"calendar": {"horizon_days": "soon"}}
    with pytest.raises(DomainError) as e:
        scan_of(tmp_path / "b", cfg, ev("w1", ";VALUE=DATE:20261001"))
    assert "horizon_days" in str(e.value)


# ---------------------------------------------------------------- status and other components

def test_a_cancelled_event_is_skipped_counted_and_not_present(tmp_path, cfg, vouched):
    scan = scan_of(tmp_path, cfg, ev("c1", ":20261020T150000Z", "STATUS:CANCELLED"),
                   ev("k1", ":20261020T170000Z"))
    assert sorted(_by_id(scan)) == ["k1@20261020T170000Z"]
    assert "1 × cancelled event" in scan["skipped"]
    assert scan["present_ids"] == {"k1@20261020T170000Z"}


def test_tasks_and_journal_entries_are_counted_not_imported(tmp_path, cfg):
    data = ics(ev("e1", ":20261020T150000Z")).replace(
        b"END:VCALENDAR", b"BEGIN:VTODO\r\nUID:t1\r\nSUMMARY:file taxes\r\nEND:VTODO\r\n"
                          b"END:VCALENDAR")
    scan = cal.scan(write(tmp_path, data), cfg)
    assert len(scan["records"]) == 1
    assert any("VTODO" in s for s in scan["skipped"])


def test_an_event_without_a_start_is_reported_and_stops_mirroring(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ["UID:n1", "SUMMARY:no start"], ev("k1", ":20261020T170000Z"))
    assert sorted(_by_id(scan)) == ["k1@20261020T170000Z"]
    assert any("no readable start" in u["what"] for u in scan["unmapped"])
    assert "present_ids" not in scan


def test_an_event_without_a_uid_gets_a_stable_one(tmp_path, cfg):
    a = scan_of(tmp_path / "a", cfg, ["DTSTART:20261020T150000Z", "SUMMARY:x"])
    b = scan_of(tmp_path / "b", cfg, ["DTSTART:20261020T150000Z", "SUMMARY:x"])
    assert a["records"][0]["external_id"] == b["records"][0]["external_id"]
    assert a["records"][0]["external_id"].startswith("nouid-")


# ---------------------------------------------------------------- participants

def test_organizer_and_attendees_are_email_handles_named_by_their_invites(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev(
        "p1", ":20261020T150000Z",
        "ORGANIZER;CN=Priya Natarajan:mailto:Priya@Acme-Robotics.example",
        "ATTENDEE;CUTYPE=INDIVIDUAL;PARTSTAT=ACCEPTED;CN=Jake Morales:mailto:jake.morales@example.com",
        "ATTENDEE;PARTSTAT=DECLINED;CN=alex.rivera@example.com:mailto:alex.rivera@example.com",
        'ATTENDEE;CN="Morales, Jake";PARTSTAT=ACCEPTED:mailto:jake.morales@example.com'))
    rec = scan["records"][0]
    assert rec["participants"] == [
        {"kind": "email", "value": "priya@acme-robotics.example", "role": "organizer"},
        {"kind": "email", "value": "jake.morales@example.com", "role": "participant"},
        {"kind": "email", "value": "alex.rivera@example.com", "role": "participant"}]
    names = {h["value"]: h["display_name"] for h in scan["handles"]}
    assert names == {"priya@acme-robotics.example": "Priya Natarajan",
                     "jake.morales@example.com": "Jake Morales",
                     "alex.rivera@example.com": None}      # a CN that is just the address
    assert _detail(rec)["responses"] == {"jake.morales@example.com": "accepted",
                                         "alex.rivera@example.com": "declined"}


def test_rooms_and_resources_are_not_people(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev(
        "r1", ":20261020T150000Z",
        "ATTENDEE;CUTYPE=RESOURCE;CN=Boardroom (12):mailto:c_1@resource.calendar.google.com",
        "ATTENDEE;CUTYPE=ROOM;CN=Room 3:mailto:room3@acme.example",
        "ATTENDEE;CN=Team:mailto:abc@group.calendar.google.com",
        "ATTENDEE;CN=Jake:mailto:jake.morales@example.com"))
    assert [p["value"] for p in scan["records"][0]["participants"]] == ["jake.morales@example.com"]
    assert "3 × room, resource or calendar address among the attendees" in scan["skipped"]


def test_an_apple_attendee_written_as_a_uuid_uses_its_email_parameter(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg, ev(
        "a1", ":20261020T150000Z",
        'ATTENDEE;CN="Linda Rivera";EMAIL="linda.rivera@example.com";PARTSTAT=NEEDS-ACTION:'
        "urn:uuid:7c0e3f8a-0000-4000-8000-000000000001",
        "ATTENDEE;CN=Nobody:urn:uuid:7c0e3f8a-0000-4000-8000-000000000002"))
    assert scan["records"][0]["participants"] == [
        {"kind": "email", "value": "linda.rivera@example.com", "role": "participant"}]
    assert any(u["what"] == "attendee with no email address" for u in scan["unmapped"])


def test_a_location_is_a_place_handle_but_a_meeting_link_is_not(tmp_path, cfg):
    scan = scan_of(tmp_path, cfg,
                   ev("l1", ":20261020T150000Z",
                      r"LOCATION:Veracruz All Natural\n1704 E Cesar Chavez St\, Austin\, TX",
                      'X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="1704 E Cesar Chavez St, '
                      'Austin, TX";X-APPLE-RADIUS=70;X-TITLE="Veracruz All Natural":geo:30.2585,-97.7124'),
                   ev("l2", ":20261021T150000Z", "LOCATION:https://zoom.us/j/123"),
                   ev("l3", ":20261022T150000Z", "LOCATION:Microsoft Teams Meeting"))
    recs = _by_id(scan)
    place = "calendar:veracruz all natural, 1704 e cesar chavez st, austin, tx"
    assert recs["l1@20261020T150000Z"]["participants"] == [
        {"kind": "place_id", "value": place, "role": "location"}]
    assert _detail(recs["l1@20261020T150000Z"])["geo"] == [30.2585, -97.7124]
    assert recs["l2@20261021T150000Z"]["participants"] == []
    assert recs["l3@20261022T150000Z"]["participants"] == []
    assert _detail(recs["l2@20261021T150000Z"])["location"] == "https://zoom.us/j/123"
    assert scan["handles"] == [{"kind": "place_id", "value": place,
                                "display_name": "Veracruz All Natural"}]


def test_every_participant_handle_is_declared(tmp_path, cfg):
    """observation_participants has a foreign key to entity_handles: a
    participant missing from scan['handles'] fails the whole apply."""
    scan = scan_of(tmp_path, cfg, ev(
        "p1", ";TZID=America/Chicago:20261001T100000", "RRULE:FREQ=WEEKLY;COUNT=3",
        "ORGANIZER:mailto:priya@acme-robotics.example", "LOCATION:Uchi",
        "ATTENDEE:tel:+1 (512) 555-0142"))
    declared = {(h["kind"], h["value"]) for h in scan["handles"]}
    used = {(p["kind"], p["value"]) for r in scan["records"] for p in r["participants"]}
    assert used == declared
    assert ("phone", "+15125550142") in used


# ---------------------------------------------------------------- targets

def _takeout(tmp_path, name="takeout-20261021T120000Z-001.zip", primary=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    z = tmp_path / name
    with zipfile.ZipFile(z, "w") as zf:
        if primary:
            zf.writestr("Takeout/Calendar/me@example.com.ics",
                        ics(ev("t1", ":20261020T150000Z"), calname="me@example.com",
                            prodid="-//Google Inc//Google Calendar 70.9054//EN"))
        zf.writestr("Takeout/Calendar/Family.ics",
                    ics(ev("t2", ";VALUE=DATE:20261130", "RRULE:FREQ=YEARLY"),
                        calname="Family"))
        zf.writestr("Takeout/Calendar/Holidays in United States.ics",
                    ics(ev("t3", ";VALUE=DATE:20261126", summary="Thanksgiving Day"),
                        calname="Holidays in United States"))
        zf.writestr("Takeout/archive_browser.html", "<html></html>")
    return z


def test_a_takeout_zip_is_one_account_named_by_its_primary_calendar(tmp_path, cfg):
    scan = cal.scan(_takeout(tmp_path), cfg)
    assert scan["target_kind"] == "takeout"
    assert scan["account"] == "me@example.com"
    assert scan["facts"]["account_from"] == "the calendar named by an email address"
    assert [c["name"] for c in scan["facts"]["calendars"]] == [
        "Family", "Holidays in United States", "me@example.com"]
    assert {_detail(r)["calendar"] for r in scan["records"]} == {
        "me@example.com", "Family", "Holidays in United States"}
    assert scan["mirror_streams"] == ["calendar_event"]


def test_google_calendars_export_zip_is_named_by_its_file_name(tmp_path, cfg):
    z = tmp_path / "Me@Example.com.ical.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Work.ics", ics(ev("w1", ":20261020T150000Z"), calname="Work"))
        zf.writestr("Gym.ics", ics(ev("g1", ":20261021T150000Z"), calname="Gym"))
    scan = cal.scan(z, cfg)
    assert scan["target_kind"] == "zip"
    assert scan["account"] == "me@example.com"


def test_several_calendars_with_no_owner_do_not_mirror(tmp_path, cfg, conn):
    folder = tmp_path / "exports"
    write(folder, ics(ev("h1", ":20261020T150000Z"), calname="Home"), "Home.ics")
    write(folder / "sub", ics(ev("w1", ":20261021T150000Z"), calname="Work"), "Work.ics")
    scan = cal.scan(folder, cfg)
    assert scan["target_kind"] == "folder"
    assert len(scan["records"]) == 2                    # the folder is searched recursively
    assert scan["account"] is None
    plan = obs.build_plan(conn, scan)
    assert "does not say which account" in plan["mirror_note"]
    text = "\n".join(cal.report_lines(scan, plan))
    assert "[connectors.calendar] account" in text


def test_a_configured_account_names_a_multi_calendar_export(tmp_path, cfg):
    cfg.connectors = {"calendar": {"account": "alex@icloud.example"}}
    scan = cal.scan(_takeout(tmp_path, primary=False), cfg)
    assert scan["account"] == "alex@icloud.example"


def test_a_zip_without_calendars_lists_its_members(tmp_path, cfg):
    z = tmp_path / "takeout.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/Keep/note.json", "{}")
    with pytest.raises(DomainError) as e:
        cal.scan(z, cfg)
    assert "contains no .ics calendar" in str(e.value) and "Takeout/Keep/note.json" in str(e.value)


def test_a_folder_without_calendars_says_what_it_has(tmp_path, cfg):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "readme.txt").write_text("hi")
    with pytest.raises(DomainError) as e:
        cal.scan(tmp_path / "x", cfg)
    assert "holds no .ics calendar files (has: readme.txt)" in str(e.value)


def test_a_truncated_export_is_refused_not_read_partially(tmp_path, cfg):
    data = ics(ev("a1", ":20261020T150000Z"), ev("a2", ":20261021T150000Z"))
    p = write(tmp_path, data[:data.rindex(b"END:VEVENT")])
    with pytest.raises(DomainError) as e:
        cal.scan(p, cfg)
    assert "truncated" in str(e.value)


def test_a_calendar_archive_is_refused_with_the_export_to_use_instead(tmp_path, cfg):
    """Calendar.app's File > Export > Calendar Archive… writes an .icbu
    bundle — a folder on disk, with no .ics the reader could use."""
    archive = tmp_path / "Calendars 2026-10-21.icbu"
    archive.mkdir()
    (archive / "Info.plist").write_text("<plist/>")
    with pytest.raises(DomainError) as e:
        cal.scan(archive, cfg)
    assert "Calendar Archive" in str(e.value) and "File > Export > Export" in str(e.value)


def test_scan_is_deterministic(tmp_path, cfg):
    a = cal.scan(_takeout(tmp_path / "a"), cfg)
    b = cal.scan(_takeout(tmp_path / "b"), cfg)
    assert a["records"] == b["records"]
    assert a["handles"] == b["handles"]
    assert a["present_ids"] == b["present_ids"]


# ---------------------------------------------------------------- report

def test_report_lines_show_counts_span_handles_and_skips(tmp_path, cfg, conn):
    scan = scan_of(tmp_path, cfg,
                   ev("w1", ";TZID=America/Chicago:20261001T100000", "RRULE:FREQ=WEEKLY;COUNT=3",
                      "ORGANIZER;CN=Priya:mailto:priya@acme-robotics.example",
                      "ATTENDEE;CN=Alex:mailto:alex.rivera@example.com", "LOCATION:Uchi"),
                   ev("c1", ":20261020T150000Z", "STATUS:CANCELLED"))
    text = "\n".join(cal.report_lines(scan, obs.build_plan(conn, scan)))
    assert "calendar: Home.ics (calendar file, 1 calendar(s): Home (3))" in text
    assert "account: calendar:Home" in text
    assert "calendar_event: 3 new, 0 changed | 2026-10-01..2026-10-15" in text
    assert "1 series, 3 occurrence(s)" in text
    assert "handles: 2 people (2 named by their invites), 1 place(s)" in text
    assert "your own address is likely priya@acme-robotics.example" in text
    assert "skipped: 1 × cancelled event" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reapply_is_identical_and_touches_only_the_observed_tier(tmp_path, cfg, conn):
    scan = cal.scan(_takeout(tmp_path), cfg)
    _, result = _apply(conn, scan)
    assert result["added"] == len(scan["records"])
    plan, again = _apply(conn, cal.scan(_takeout(tmp_path / "again"), cfg))
    assert again["added"] == 0 and again["identical"] == len(scan["records"])
    assert plan["removed"] == []
    for table in ("entries", "measurements", "mentions", "entity_aliases",
                  "entry_sources", "extraction_runs"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0, table


def test_an_edited_event_with_a_newer_modified_time_updates(tmp_path, cfg, conn):
    _apply(conn, scan_of(tmp_path / "a", cfg, ev("e1", ":20261020T150000Z", summary="Lunch")))
    newer = ics(["UID:e1", "DTSTART:20261020T150000Z", "SUMMARY:Lunch with Priya",
                 "LAST-MODIFIED:20261015T120000Z"])
    plan, result = _apply(conn, cal.scan(write(tmp_path / "b", newer), cfg))
    assert result["updated"] == 1
    assert conn.execute("SELECT title FROM observations").fetchone()["title"] == "Lunch with Priya"


def test_doctor_stays_green_after_a_calendar_import(tmp_path, cfg, conn):
    _seed(conn, cfg)
    _apply(conn, cal.scan(_takeout(tmp_path), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- mirroring

WEEKLY = ev("w1", ";TZID=America/Chicago:20261001T100000", "RRULE:FREQ=WEEKLY;COUNT=4",
            summary="1:1")
LUNCH = ev("l1", ":20261020T170000Z", summary="Lunch")


def test_an_event_deleted_from_the_calendar_is_removed(tmp_path, cfg, conn, vouched):
    _apply(conn, scan_of(tmp_path / "a", cfg, WEEKLY, LUNCH))
    assert "l1@20261020T170000Z" in _ids(conn)
    plan, result = _apply(conn, scan_of(tmp_path / "b", cfg, WEEKLY))
    assert [r["external_id"] for r in plan["removed"]] == ["l1@20261020T170000Z"]
    assert result["removed"] == 1
    assert len(_ids(conn)) == 4


def test_an_exdated_occurrence_is_removed_but_the_series_stays(tmp_path, cfg, conn, vouched):
    _apply(conn, scan_of(tmp_path / "a", cfg, WEEKLY))
    later = ev("w1", ";TZID=America/Chicago:20261001T100000", "RRULE:FREQ=WEEKLY;COUNT=4",
               "EXDATE;TZID=America/Chicago:20261015T100000", summary="1:1")
    plan, _ = _apply(conn, scan_of(tmp_path / "b", cfg, later))
    assert [r["external_id"] for r in plan["removed"]] == ["w1@20261015T150000Z"]
    assert len(_ids(conn)) == 3


def test_occurrences_beyond_todays_window_are_not_treated_as_deleted(tmp_path, cfg, conn,
                                                                     monkeypatch, vouched):
    """An earlier run whose window reached further (a clock set ahead, a
    longer horizon) archived occurrences today's window does not reach.
    They are still in the calendar, and must stay."""
    endless = ev("e1", ";VALUE=DATE:20261001", "RRULE:FREQ=WEEKLY")
    monkeypatch.setattr(cal, "_today", lambda cfg: date(2027, 3, 1))
    _apply(conn, scan_of(tmp_path / "a", cfg, endless))
    far = max(_ids(conn))
    monkeypatch.setattr(cal, "_today", lambda cfg: TODAY)
    scan = scan_of(tmp_path / "b", cfg, endless)
    assert far not in {r["external_id"] for r in scan["records"]}
    assert far in scan["present_ids"]
    assert obs.build_plan(conn, scan)["removed"] == []


def test_one_calendars_export_never_removes_another_calendars_events(tmp_path, cfg, conn,
                                                                     vouched):
    _apply(conn, scan_of(tmp_path / "home", cfg, LUNCH, calname="Home"))
    scan = scan_of(tmp_path / "work", cfg, WEEKLY, calname="Work")
    assert scan["account"] == "calendar:Work"
    plan, _ = _apply(conn, scan)
    assert plan["removed"] == []
    assert "l1@20261020T170000Z" in _ids(conn)


def test_an_excluded_calendar_is_not_imported_and_not_removed(tmp_path, cfg, conn):
    _apply(conn, cal.scan(_takeout(tmp_path / "a"), cfg))
    cfg.connectors = {"calendar": {"exclude_calendars": ["holidays in united states"]}}
    scan = cal.scan(_takeout(tmp_path / "b"), cfg)
    assert all(_detail(r)["calendar"] != "Holidays in United States" for r in scan["records"])
    assert "1 × event in an excluded calendar (Holidays in United States)" in scan["skipped"]
    assert obs.build_plan(conn, scan)["removed"] == []


def test_a_calendar_left_out_of_a_later_export_is_named_in_a_warning(tmp_path, cfg, conn):
    """Takeout lets you untick a calendar; the next export then reads as every
    one of its events deleted. The removal stands (the dry run is the gate),
    but the report names the calendar the export does not hold."""
    _apply(conn, cal.scan(_takeout(tmp_path / "a"), cfg))
    z = tmp_path / "b" / "takeout-20261022T120000Z-001.zip"
    z.parent.mkdir()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Takeout/Calendar/me@example.com.ics",
                    ics(ev("t1", ":20261020T150000Z"), calname="me@example.com"))
        zf.writestr("Takeout/Calendar/Holidays in United States.ics",
                    ics(ev("t3", ";VALUE=DATE:20261126", summary="Thanksgiving Day"),
                        calname="Holidays in United States"))
    scan = cal.scan(z, cfg)
    plan = obs.build_plan(conn, scan)
    assert {_detail(r)["calendar"] for r in plan["removed"]} == {"Family"}
    text = "\n".join(cal.report_lines(scan, plan))
    assert "WARNING: Family (" in text and "not in this export at all" in text


def test_an_event_deleted_from_a_calendar_still_exported_draws_no_warning(tmp_path, cfg,
                                                                          conn, vouched):
    _apply(conn, scan_of(tmp_path / "a", cfg, WEEKLY, LUNCH))
    scan = scan_of(tmp_path / "b", cfg, WEEKLY)
    text = "\n".join(cal.report_lines(scan, obs.build_plan(conn, scan)))
    assert "remove: 1 no longer in the calendar" in text and "WARNING" not in text


def test_removing_most_of_the_archive_at_once_is_refused(tmp_path, cfg, conn, monkeypatch,
                                                         vouched):
    monkeypatch.setattr(obs, "MASS_REMOVAL_MIN", 2)
    many = [ev(f"m{i}", f":202610{10 + i}T150000Z") for i in range(5)]
    _apply(conn, scan_of(tmp_path / "a", cfg, *many))
    scan = scan_of(tmp_path / "b", cfg, many[0])
    plan = obs.build_plan(conn, scan)
    assert len(plan["removed"]) == 4
    assert obs.mass_removal(plan)
    with pytest.raises(DomainError):
        obs.apply_plan(conn, scan, plan)
    conn.rollback()


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch, extra=""):
    root = cli_project(tmp_path, monkeypatch)
    if extra:
        with open(root / "config.toml", "a", encoding="utf-8") as f:
            f.write(extra)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["observe", "calendar", str(_takeout(tmp_path / "exp"))])
    assert r.exit_code == 0, r.output
    assert "calendar_event: " in r.output and "dry run — nothing written" in r.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0


def test_cli_apply_then_reimport_is_a_no_op(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    export = _takeout(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "calendar", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    added = int(r.output.split("applied: ")[1].split(" added")[0])
    assert added > 0
    again = runner.invoke(app, ["observe", "calendar", str(export), "--apply"])
    assert again.exit_code == 0, again.output
    assert "0 added" in again.output and f"{added} identical skipped" in again.output
    dry = runner.invoke(app, ["observe", "calendar", str(export)])
    assert "already imported" in dry.output


def test_cli_without_a_path_uses_the_newest_takeout_in_downloads(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    downloads = tmp_path / "Downloads"
    old = _takeout(downloads, name="takeout-20250101T000000Z-001.zip")
    new = _takeout(downloads, name="takeout-20261021T120000Z-001.zip")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    (downloads / "takeout-photos.zip").write_bytes(b"not a zip")
    monkeypatch.setattr(cal, "downloads_dirs", lambda: [downloads])
    assert cal.default_target(_cfg_for(tmp_path)) == new
    r = runner.invoke(app, ["observe", "calendar"])
    assert r.exit_code == 0, r.output
    assert new.name in r.output


def _cfg_for(root):
    from lifecrm.config import load_config
    return load_config(root)


def test_default_target_prefers_the_configured_path(tmp_path, monkeypatch):
    export = write(tmp_path / "exp", ics(ev("a1", ":20261020T150000Z")))
    root = _cli_setup(tmp_path, monkeypatch,
                      f"[connectors.calendar]\npath = '{export}'\n")
    monkeypatch.setattr(cal, "downloads_dirs", lambda: [])
    assert cal.default_target(_cfg_for(root)) == export
    r = runner.invoke(app, ["observe", "calendar"])
    assert r.exit_code == 0, r.output
    assert "calendar: Home.ics" in r.output


def test_default_target_says_where_it_looked(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(cal, "downloads_dirs", lambda: [tmp_path / "Downloads"])
    with pytest.raises(DomainError) as e:
        cal.default_target(cfg)
    assert "no calendar export found" in str(e.value) and "Downloads" in str(e.value)
    cfg.connectors = {"calendar": {"path": str(tmp_path / "missing.ics")}}
    with pytest.raises(DomainError) as e:
        cal.default_target(cfg)
    assert "[connectors.calendar] path" in str(e.value)


def test_cli_connectors_lists_the_calendar_stream(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0, r.output
    assert "calendar" in r.output and "calendar_event" in r.output

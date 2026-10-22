"""iCalendar reading (connectors/_ics.py): content lines, structure, time
zones and the RRULE expander, tested on their own.

The expander cases are the RFC 5545 examples and the rules real calendars
actually write — Google's "last Friday of the month", Outlook's "last weekday"
via BYSETPOS, the holiday rules — plus the edges that silently produce wrong
dates when got wrong: the 31st in a short month, Feb 29, DTSTART that does
not match its own rule, and a rule that would never end.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from lifecrm.connectors import _ics
from lifecrm.domain.util import DomainError


def _expand(text, start, horizon=date(2030, 1, 1)):
    rule = _ics.parse_rule(text, start)
    return list(_ics.expand(rule, start, horizon, rule.until))


# ---------------------------------------------------------------- content lines

def test_a_quoted_parameter_may_hold_colons_commas_and_semicolons():
    p = _ics.parse_line('X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS="1704 E Cesar '
                        'Chavez St, Austin; TX";X-TITLE="Veracruz: All Natural":geo:30.25,-97.71')
    assert p.name == "X-APPLE-STRUCTURED-LOCATION"
    assert p.param("x-address") == "1704 E Cesar Chavez St, Austin; TX"
    assert p.param("X-TITLE") == "Veracruz: All Natural"
    assert p.value == "geo:30.25,-97.71"


def test_parameter_lists_and_rfc6868_escapes():
    p = _ics.parse_line('ATTENDEE;MEMBER="mailto:a@x.com","mailto:b@x.com";'
                        "CN=Jake ^'JM^' Morales^nEl Paso:mailto:jake@x.com")
    assert p.params["MEMBER"] == ["mailto:a@x.com", "mailto:b@x.com"]
    assert p.param("CN") == 'Jake "JM" Morales\nEl Paso'
    assert p.value == "mailto:jake@x.com"


def test_a_line_with_no_value_is_malformed():
    assert _ics.parse_line("SUMMARY") is None
    assert _ics.parse_line('ATTENDEE;CN="unterminated:mailto:x@y.com') is None


def test_text_escapes_are_undone():
    assert _ics.unescape(r"Dinner\, then drinks\; maybe\nor not \\ ok\N") == \
        "Dinner, then drinks; maybe\nor not \\ ok\n"


def test_unfolding_happens_on_bytes_so_a_split_character_survives():
    raw = "SUMMARY:Family dinner at Tía Rosa's".encode()
    cut = raw.index("í".encode()) + 1                 # between the two bytes of í
    data = (b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n" + raw[:cut] + b"\r\n " + raw[cut:]
            + b"\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    roots, malformed = _ics.parse(data, "x.ics")
    assert malformed == 0
    assert roots[0].kids("VEVENT")[0].text("SUMMARY") == "Family dinner at Tía Rosa's"


def test_bare_lf_and_tab_folds_and_a_bom_are_tolerated():
    data = b"\xef\xbb\xbfBEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:a long\n\t title\nEND:VEVENT\nEND:VCALENDAR\n"
    roots, _ = _ics.parse(data, "x.ics")
    assert roots[0].kids("VEVENT")[0].text("SUMMARY") == "a long title"


def test_nested_components_build_a_tree():
    data = (b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nBEGIN:VALARM\r\nTRIGGER:-PT15M\r\n"
            b"END:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    roots, _ = _ics.parse(data, "x.ics")
    event = roots[0].kids("VEVENT")[0]
    assert event.first("UID").value == "1"
    assert event.kids("VALARM")[0].first("TRIGGER").value == "-PT15M"
    assert event.first("TRIGGER") is None              # the alarm's, not the event's


def test_unbalanced_end_is_refused_naming_the_line():
    data = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"
    with pytest.raises(DomainError) as e:
        _ics.parse(data, "Home.ics")
    assert "Home.ics line 3: END:VTODO does not close BEGIN:VEVENT" in str(e.value)


def test_a_file_cut_off_mid_event_is_refused_as_truncated():
    data = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nSUMMARY:half"
    with pytest.raises(DomainError) as e:
        _ics.parse(data, "Home.ics")
    assert "ends inside BEGIN:VEVENT" in str(e.value) and "truncated" in str(e.value)


def test_a_file_that_is_not_icalendar_says_what_it_starts_with():
    with pytest.raises(DomainError) as e:
        _ics.parse(b"BEGIN:VCARD\r\nFN:Jake\r\nEND:VCARD\r\n", "contacts.ics")
    assert "no BEGIN:VCALENDAR" in str(e.value) and "BEGIN:VCARD" in str(e.value)


def test_invalid_utf8_names_the_line():
    with pytest.raises(DomainError) as e:
        _ics.parse(b"BEGIN:VCALENDAR\r\nSUMMARY:caf\xe9\r\nEND:VCALENDAR\r\n", "x.ics")
    assert "x.ics line 2 is not valid UTF-8" in str(e.value)


# ---------------------------------------------------------------- values

def test_date_and_date_time_forms():
    zones = _ics.Zones([])
    assert _ics.parse_value(_ics.parse_line("DTSTART;VALUE=DATE:20261020"), zones) \
        == date(2026, 10, 20)
    utc = _ics.parse_value(_ics.parse_line("DTSTART:20261020T150000Z"), zones)
    assert utc == datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    local = _ics.parse_value(_ics.parse_line("DTSTART;TZID=America/Chicago:20261020T100000"),
                             zones)
    assert local.utcoffset() == timedelta(hours=-5)
    floating = _ics.parse_value(_ics.parse_line("DTSTART:20261020T100000"), zones)
    assert floating.tzinfo is None


def test_exdate_lists_several_values():
    values = _ics.parse_values(_ics.parse_line(
        "EXDATE;TZID=America/Chicago:20261020T100000,20261027T100000"), _ics.Zones([]))
    assert [v.day for v in values] == [20, 27]


@pytest.mark.parametrize("text,expected", [
    ("PT1H30M", timedelta(hours=1, minutes=30)),
    ("P1D", timedelta(days=1)),
    ("P2W", timedelta(weeks=2)),
    ("-PT15M", -timedelta(minutes=15)),
    ("P0DT0H30M0S", timedelta(minutes=30)),
])
def test_durations(text, expected):
    assert _ics.parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["P", "PT", "1H", "P1H", "PT1H2"])
def test_bad_durations_are_refused(bad):
    with pytest.raises(ValueError):
        _ics.parse_duration(bad)


# ---------------------------------------------------------------- time zones

def test_every_windows_zone_maps_to_a_real_zone():
    """Outlook writes these; a typo here would silently date every one of
    that user's events in the home zone."""
    for windows, iana in _ics.WINDOWS_ZONES.items():
        assert _ics.zone_for(windows) is not None, (windows, iana)


@pytest.mark.parametrize("tzid,key", [
    ("America/Chicago", "America/Chicago"),
    ("Central Standard Time", "America/Chicago"),
    ("W. Europe Standard Time", "Europe/Berlin"),
    ("(UTC-06:00) Central Time (US & Canada)", "America/Chicago"),
    ("(UTC+01:00) Belgrade, Bratislava, Budapest, Ljubljana, Prague", "Europe/Budapest"),
    ("/mozilla.org/20050126_1/America/New_York", "America/New_York"),
    ("/freeassociation.sourceforge.net/Tzfile/Europe/Ljubljana", "Europe/Ljubljana"),
    ('"Europe/London"', "Europe/London"),
])
def test_tzids_resolve_to_zoneinfo(tzid, key):
    assert _ics.zone_for(tzid).key == key


def test_an_unknown_tzid_resolves_to_nothing():
    assert _ics.zone_for("Customized Time Zone") is None
    assert _ics.zone_for("") is None


def test_an_unknown_tzid_falls_back_to_its_vtimezone_and_says_so():
    data = (b"BEGIN:VCALENDAR\r\nBEGIN:VTIMEZONE\r\nTZID:Customized Time Zone\r\n"
            b"BEGIN:STANDARD\r\nDTSTART:16010101T020000\r\nTZOFFSETFROM:-0500\r\n"
            b"TZOFFSETTO:-0600\r\nEND:STANDARD\r\nBEGIN:DAYLIGHT\r\n"
            b"DTSTART:16010101T020000\r\nTZOFFSETFROM:-0600\r\nTZOFFSETTO:-0500\r\n"
            b"END:DAYLIGHT\r\nEND:VTIMEZONE\r\nEND:VCALENDAR\r\n")
    roots, _ = _ics.parse(data, "x.ics")
    zones = _ics.Zones(roots[0].kids("VTIMEZONE"))
    tz = zones.get("Customized Time Zone")
    assert tz.utcoffset(None) == timedelta(hours=-6)
    assert "daylight saving ignored" in zones.note("Customized Time Zone")
    assert zones.get("Nowhere/Special") is None
    assert "home timezone" in zones.note("Nowhere/Special")


def test_a_windows_name_records_how_it_was_read():
    zones = _ics.Zones([])
    zones.get("Central Standard Time")
    assert zones.note("Central Standard Time") == "read as America/Chicago"
    zones.get("America/Chicago")
    assert zones.note("America/Chicago") is None


# ---------------------------------------------------------------- recurrence

def test_thanksgiving_fourth_thursday_of_november():
    assert _expand("FREQ=YEARLY;BYMONTH=11;BYDAY=4TH;COUNT=4", date(2024, 11, 28)) == [
        date(2024, 11, 28), date(2025, 11, 27), date(2026, 11, 26), date(2027, 11, 25)]


def test_last_friday_of_the_month_keeps_its_time():
    got = _expand("FREQ=MONTHLY;BYDAY=-1FR;COUNT=4", datetime(2026, 1, 30, 9))
    assert got == [datetime(2026, 1, 30, 9), datetime(2026, 2, 27, 9),
                   datetime(2026, 3, 27, 9), datetime(2026, 4, 24, 9)]


def test_every_weekday():
    got = _expand("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;COUNT=7", datetime(2026, 10, 22, 9))
    assert [d.day for d in got] == [22, 23, 26, 27, 28, 29, 30]


def test_every_other_week_on_tuesday_and_thursday_with_sunday_week_start():
    got = _expand("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;WKST=SU;COUNT=6",
                  datetime(2026, 10, 20, 9))
    assert [(d.month, d.day) for d in got] == [(10, 20), (10, 22), (11, 3), (11, 5),
                                               (11, 17), (11, 19)]


def test_bysetpos_last_weekday_of_the_month():
    got = _expand("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=4", date(2026, 1, 30))
    assert got == [date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 31),
                   date(2026, 4, 30)]


def test_monthly_on_the_31st_skips_short_months():
    assert _expand("FREQ=MONTHLY;COUNT=4", date(2026, 1, 31)) == [
        date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31), date(2026, 7, 31)]


def test_negative_monthday_is_the_last_day():
    assert _expand("FREQ=MONTHLY;BYMONTHDAY=-1;COUNT=3", date(2026, 1, 31)) == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]


def test_a_feb_29_birthday_only_happens_in_leap_years():
    assert _expand("FREQ=YEARLY;COUNT=3", date(2024, 2, 29), horizon=date(2033, 1, 1)) == [
        date(2024, 2, 29), date(2028, 2, 29), date(2032, 2, 29)]


def test_rfc_example_twentieth_monday_of_the_year():
    assert _expand("FREQ=YEARLY;BYDAY=20MO;COUNT=3", date(1997, 5, 19)) == [
        date(1997, 5, 19), date(1998, 5, 18), date(1999, 5, 17)]


def test_daily_until_a_date_is_inclusive():
    got = _expand("FREQ=DAILY;UNTIL=20261023", datetime(2026, 10, 20, 9))
    assert [d.day for d in got] == [20, 21, 22, 23]


def test_daily_with_a_weekday_limit():
    got = _expand("FREQ=DAILY;BYDAY=SA,SU;COUNT=4", date(2026, 10, 24))
    assert got == [date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 31),
                   date(2026, 11, 1)]


def test_dtstart_counts_as_the_first_occurrence_even_off_rule():
    """RFC 5545 3.8.5.3: DTSTART always counts, synchronized or not."""
    got = _expand("FREQ=WEEKLY;BYDAY=MO;COUNT=3", date(2026, 10, 21))   # a Wednesday
    assert got == [date(2026, 10, 21), date(2026, 10, 26), date(2026, 11, 2)]


def test_expansion_stops_at_the_horizon():
    got = _expand("FREQ=WEEKLY", date(2026, 10, 1), horizon=date(2026, 10, 31))
    assert got[-1] == date(2026, 10, 29) and len(got) == 5


def test_byhour_restating_dtstart_is_harmless():
    got = _expand("FREQ=DAILY;BYHOUR=9;BYMINUTE=0;COUNT=2", datetime(2026, 10, 20, 9))
    assert len(got) == 2


@pytest.mark.parametrize("rule,part", [
    ("FREQ=HOURLY", "FREQ=HOURLY"),
    ("FREQ=WEEKLY;BYDAY=1MO", "BYDAY=1MO with FREQ=WEEKLY"),
    ("FREQ=YEARLY;BYWEEKNO=20", "BYWEEKNO=20"),
    ("FREQ=YEARLY;BYYEARDAY=100", "BYYEARDAY=100"),
    ("FREQ=DAILY;BYHOUR=9,17", "BYHOUR=9,17"),
    ("FREQ=WEEKLY;BYMONTHDAY=1", "BYMONTHDAY with FREQ=WEEKLY"),
    ("FREQ=MONTHLY;INTERVAL=x", "INTERVAL=X is malformed"),
    ("INTERVAL=2", "no FREQ"),
])
def test_unsupported_rules_name_the_part(rule, part):
    with pytest.raises(_ics.Unsupported) as e:
        _ics.parse_rule(rule, datetime(2026, 1, 1, 9))
    assert part in str(e.value)


def test_a_rule_too_long_to_reach_the_horizon_is_unsupported():
    rule = _ics.parse_rule("FREQ=DAILY", date(2026, 1, 1))
    with pytest.raises(_ics.Unsupported):
        list(_ics.expand(rule, date(2026, 1, 1), date(2027, 1, 1), max_periods=100))

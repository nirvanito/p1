"""Calendar: two calendars with the same name never remove each other's events.

A one-calendar export's mirroring account was `calendar:<its name>`, before
any config was consulted. On a Mac with iCloud and an Exchange account, both
default calendars are called "Calendar": importing one export removed every
event of the other (no warning — the missing-calendar check compares names —
and no refusal under the mass-removal threshold), and the next import of the
other removed them back. A name now mirrors only when it cannot be shared (a
calendar named by its owner's address) or the owner lists it in
[connectors.calendar] mirror_calendars.
"""

import pytest

from lifecrm.connectors import calendar as cal
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

APPLE = "-//Apple Inc.//macOS 15.1//EN"


def _ics(name: str, uids: list[str]) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{APPLE}", f"X-WR-CALNAME:{name}"]
    for i, uid in enumerate(uids):
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTART:202610{10 + i}T150000Z",
                  f"DTEND:202610{10 + i}T160000Z", f"SUMMARY:{uid} event",
                  "LAST-MODIFIED:20261001T120000Z", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _export(folder, name: str, uids: list[str], file_name: str | None = None):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / (file_name or f"{name}.ics")
    p.write_bytes(_ics(name, uids))
    return p


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _active(conn) -> list[str]:
    return sorted(r["external_id"].split("@")[0] for r in conn.execute(
        "SELECT external_id FROM observations WHERE stream='calendar_event' "
        "AND status='active'"))


def test_two_calendars_with_the_same_name_never_remove_each_others_events(tmp_path, cfg,
                                                                          conn):
    local = _export(tmp_path / "cal-local", "Calendar", ["local-1", "local-2", "local-3"])
    work = _export(tmp_path / "cal-work", "Calendar", ["work-1", "work-2"])
    _apply(conn, cal.scan(local, cfg))

    scan = cal.scan(work, cfg)
    assert scan["account"] == "calendar:Calendar"
    assert "present_ids" not in scan
    plan, result = _apply(conn, scan)
    assert plan["removed"] == [] and "removed" not in result
    assert _active(conn) == ["local-1", "local-2", "local-3", "work-1", "work-2"]

    # and back again
    plan, _ = _apply(conn, cal.scan(local, cfg))
    assert plan["removed"] == []
    assert len(_active(conn)) == 5


def test_the_report_says_why_and_how_to_turn_mirroring_on(tmp_path, cfg, conn):
    scan = cal.scan(_export(tmp_path, "Calendar", ["a"]), cfg)
    text = "\n".join(cal.report_lines(scan, obs.build_plan(conn, scan)))
    assert "account: calendar:Calendar (from a single calendar's name)" in text
    assert "not mirroring deletions: 'Calendar' is known only by its name" in text
    assert "[connectors.calendar] mirror_calendars" in text


def test_a_name_the_owner_vouches_for_mirrors(tmp_path, cfg, conn):
    cfg.connectors = {"calendar": {"mirror_calendars": ["home"]}}     # any case
    _apply(conn, cal.scan(_export(tmp_path / "a", "Home", ["h1", "h2"]), cfg))
    scan = cal.scan(_export(tmp_path / "b", "Home", ["h1"]), cfg)
    assert scan["facts"]["account_from"] == \
        "a single calendar, [connectors.calendar] mirror_calendars"
    plan, result = _apply(conn, scan)
    assert [r["external_id"].split("@")[0] for r in plan["removed"]] == ["h2"]
    assert result["removed"] == 1
    text = "\n".join(cal.report_lines(scan, plan))
    assert "not mirroring deletions" not in text


def test_a_calendar_named_by_its_owners_address_mirrors_without_config(tmp_path, cfg, conn):
    """Google names a primary calendar after the account's address: two
    different calendars cannot share that name."""
    _apply(conn, cal.scan(_export(tmp_path / "a", "alex.rivera@example.com", ["g1", "g2"]),
                          cfg))
    scan = cal.scan(_export(tmp_path / "b", "alex.rivera@example.com", ["g1"]), cfg)
    assert "present_ids" in scan
    plan, _ = _apply(conn, scan)
    assert [r["external_id"].split("@")[0] for r in plan["removed"]] == ["g2"]


def test_a_vouched_name_does_not_vouch_for_another(tmp_path, cfg):
    cfg.connectors = {"calendar": {"mirror_calendars": ["Home"]}}
    assert "present_ids" not in cal.scan(_export(tmp_path, "Calendar", ["a"]), cfg)


def test_mirror_calendars_must_be_a_list_of_names(tmp_path, cfg):
    cfg.connectors = {"calendar": {"mirror_calendars": [1, 2]}}
    with pytest.raises(DomainError, match="mirror_calendars must be a list of calendar names"):
        cal.scan(_export(tmp_path, "Home", ["a"]), cfg)

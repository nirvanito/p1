"""Demo calendar exports (lifecrm/demo/sources/calendar.py) read back by the
calendar connector.

The point of a sample is that the persona survives the round trip through
each app's real format: Jake's address with his name on it, the dinner at
Veracruz, the Lake Bled trip four years back, the weekly 1:1 with its one
edited occurrence. Then the same things every import must do: apply cleanly
to a fresh database, re-apply as all-identical, leave doctor green.
"""

import json
import zipfile
from datetime import date, timedelta

import pytest

from lifecrm.connectors import calendar as cal
from lifecrm.demo import persona as P
from lifecrm.demo.sources import calendar as gen
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

ANCHOR = date(2026, 10, 21)
COMBOS = ("android", "iphone")


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    monkeypatch.setattr(cal, "_today", lambda cfg: ANCHOR)


def _sample(tmp_path, combo):
    return gen.write_sample(tmp_path / combo, ANCHOR, combo)


def _titled(scan, title):
    return sorted((r for r in scan["records"] if r["title"] == title),
                  key=lambda r: r["started_at"])


def _detail(rec):
    return json.loads(rec["value_json"])


# ---------------------------------------------------------------- the files

def test_android_is_a_google_takeout_with_one_ics_per_calendar(tmp_path):
    path = _sample(tmp_path, "android")
    assert path.name == f"takeout-{ANCHOR:%Y%m%d}T120000Z-001.zip"
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        primary = zf.read(f"Takeout/Calendar/{P.OWNER.email}.ics").decode()
    assert names == {"Takeout/archive_browser.html",
                     "Takeout/Calendar/Family.ics",
                     "Takeout/Calendar/Holidays in United States.ics",
                     f"Takeout/Calendar/{P.OWNER.email}.ics"}
    assert "PRODID:-//Google Inc//Google Calendar 70.9054//EN" in primary
    assert "\r\n " in primary                       # long lines are folded
    assert all(len(line.encode()) <= 75 for line in primary.split("\r\n"))


def test_iphone_is_an_apple_calendar_export_of_one_calendar(tmp_path):
    path = _sample(tmp_path, "iphone")
    assert path.name == "Home.ics"
    text = path.read_text(encoding="utf-8")
    assert "PRODID:-//Apple Inc.//macOS" in text and "X-WR-CALNAME:Home" in text
    assert "BEGIN:VTIMEZONE" in text and "X-APPLE-STRUCTURED-LOCATION" in text


@pytest.mark.parametrize("combo", COMBOS)
def test_a_sample_is_deterministic_for_its_anchor(tmp_path, combo):
    a = _sample(tmp_path / "a", combo).read_bytes()
    b = _sample(tmp_path / "b", combo).read_bytes()
    assert a == b


def test_an_unknown_combo_is_refused(tmp_path):
    with pytest.raises(ValueError) as e:
        gen.write_sample(tmp_path, ANCHOR, "blackberry")
    assert "android, iphone" in str(e.value)
    assert list(tmp_path.iterdir()) == []            # nothing written


# ---------------------------------------------------------------- the persona survives

@pytest.mark.parametrize("combo", COMBOS)
def test_jake_comes_through_as_his_address_with_his_name(tmp_path, cfg, combo):
    scan = cal.scan(_sample(tmp_path, combo), cfg)
    jake = P.person("jake")
    handles = {(h["kind"], h["value"]): h["display_name"] for h in scan["handles"]}
    assert handles[("email", jake.email)] == jake.name
    assert ("email", P.OWNER.email) in handles      # the owner's own address is kept
    assert all(h["kind"] in ("email", "place_id") for h in scan["handles"])


@pytest.mark.parametrize("combo", COMBOS)
def test_dinner_at_veracruz_with_jake(tmp_path, cfg, combo):
    scan = cal.scan(_sample(tmp_path, combo), cfg)
    [dinner] = _titled(scan, "Dinner w/ Jake @ Veracruz")
    assert dinner["journal_date"] == P.day(ANCHOR, -38).isoformat()
    assert dinner["started_at"] == P.utc_iso(P.local_dt(ANCHOR, -38, "19:30"))
    assert "Veracruz All Natural" in _detail(dinner)["location"]
    roles = {(p["kind"], p["value"], p["role"]) for p in dinner["participants"]}
    assert ("email", P.person("jake").email, "participant") in roles
    assert any(k == "place_id" and "veracruz" in v and r == "location" for k, v, r in roles)


@pytest.mark.parametrize("combo", COMBOS)
def test_the_lake_bled_trip_four_years_back(tmp_path, cfg, combo):
    scan = cal.scan(_sample(tmp_path, combo), cfg)
    trip_days = P.bled_trip_days(ANCHOR)
    [trip] = _titled(scan, "Lake Bled trip")
    assert trip["journal_date"] == trip_days[0].isoformat()
    assert trip_days[0].year == ANCHOR.year - 4
    assert _detail(trip)["all_day"] is True
    assert trip["ended_at"][:10] == (trip_days[-1] + timedelta(days=1)).isoformat()
    [row] = _titled(scan, "Rowboat to Bled island")
    assert row["started_at"] == P.utc_iso(P.trip_dt(2, "15:00", ANCHOR))
    assert row["journal_date"] == trip_days[2].isoformat()
    assert any(p["value"] == P.person("jake").email for p in row["participants"])


@pytest.mark.parametrize("combo", COMBOS)
def test_the_weekly_one_on_one_has_seven_occurrences_and_one_edit(tmp_path, cfg, combo):
    scan = cal.scan(_sample(tmp_path, combo), cfg)
    spec = next(e for e in P.CALENDAR if e.uid == "demo-1on1-priya")
    occurrences = _titled(scan, spec.title)
    assert len(occurrences) == spec.weekly_count
    assert [r["journal_date"] for r in occurrences] == [
        P.day(ANCHOR, spec.offset + 7 * i).isoformat() for i in range(spec.weekly_count)]
    assert len({r["external_id"].split("@20")[0] for r in occurrences}) == 1   # one UID
    edited = occurrences[2]
    assert "customer story" in edited["body"]
    assert all("meet.google.com" not in (r["body"] or "") for r in occurrences)
    assert {p["value"] for p in occurrences[0]["participants"] if p["role"] == "organizer"} \
        == {P.person("priya").email}


@pytest.mark.parametrize("combo", COMBOS)
def test_birthdays_repeat_yearly_up_to_the_horizon(tmp_path, cfg, combo):
    scan = cal.scan(_sample(tmp_path, combo), cfg)
    maya = _titled(scan, "Maya's birthday")
    assert maya[-1]["journal_date"] == P.birthday_date(P.person("maya"), ANCHOR).isoformat()
    assert len(maya) == 4
    assert _detail(maya[0])["recurring"] is True


def test_the_android_sample_mirrors_the_google_account(tmp_path, cfg):
    scan = cal.scan(_sample(tmp_path, "android"), cfg)
    assert scan["account"] == P.OWNER.email
    assert "present_ids" in scan
    assert any(c["name"] == "Holidays in United States" for c in scan["facts"]["calendars"])
    assert any("room, resource" in s for s in scan["skipped"])


def test_the_iphone_sample_mirrors_its_one_calendar_once_its_name_is_vouched_for(tmp_path,
                                                                                 cfg):
    path = _sample(tmp_path, "iphone")
    scan = cal.scan(path, cfg)
    assert scan["account"] == "calendar:Home"
    assert "present_ids" not in scan            # a name alone could be shared
    mom = P.person("mom")
    assert any(p["value"] == mom.email for r in scan["records"]
               for p in r["participants"])       # written as urn:uuid + EMAIL
    cfg.connectors = {"calendar": {"mirror_calendars": ["Home"]}}
    assert "present_ids" in cal.scan(path, cfg)


# ---------------------------------------------------------------- the pipeline

@pytest.mark.parametrize("combo", COMBOS)
def test_a_sample_applies_reapplies_identically_and_leaves_doctor_green(tmp_path, cfg,
                                                                        conn, combo):
    path = _sample(tmp_path, combo)
    scan = cal.scan(path, cfg)
    plan = obs.build_plan(conn, scan)
    assert plan["invalid"] == [] and plan["conflicts"] == []
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    assert result["added"] == len(scan["records"])

    again = obs.build_plan(conn, cal.scan(path, cfg))
    assert again["new"] == [] and again["updates"] == [] and again["removed"] == []
    assert again["identical"] == len(scan["records"])

    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
    for table in ("entries", "measurements", "mentions", "entity_aliases"):
        assert conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] == 0


def test_samples_never_raise_a_domain_error_on_their_own_dry_run(tmp_path, cfg, conn):
    for combo in COMBOS:
        try:
            scan = cal.scan(_sample(tmp_path, combo), cfg)
        except DomainError as e:                   # pragma: no cover - the failure itself
            pytest.fail(f"{combo}: {e}")
        lines = cal.report_lines(scan, obs.build_plan(conn, scan))
        assert any(line.startswith("  calendar_event: ") for line in lines)
        assert not any("not understood" in line for line in lines), lines

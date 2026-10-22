"""Location connector: Google Maps Timeline visits into the observed tier.

scan() is a pure reader, so most of this runs it against exports built here
in each real shape — Android's Timeline.json (and the demo's rendering of the
persona in it), the iPhone Google Maps list, and legacy Takeout month files —
including what is easy to get wrong: a visit's own zone deciding its day,
'+00:00' being a zone rather than "unknown", the same visit in both formats
being one record, and a later export winning while an older one cannot.
"""

import json
import zipfile
from datetime import date, datetime, timezone

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import location
from lifecrm.demo import persona
from lifecrm.demo.sources import location as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()
ANCHOR = date(2026, 10, 21)


def _utc(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _visit(start, end, place_id, latlng, semantic="UNKNOWN", prob=0.8, level=0):
    return {"startTime": start, "endTime": end,
            "visit": {"hierarchyLevel": level, "probability": 0.9,
                      "topCandidate": {"placeId": place_id, "semanticType": semantic,
                                       "probability": prob,
                                       "placeLocation": {"latLng": latlng}}}}


def device_export(folder, segments, **extra):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "Timeline.json"
    p.write_text(json.dumps({"semanticSegments": segments, **extra}), encoding="utf-8")
    return p


VERACRUZ_PV = {"placeVisit": {
    "location": {"latitudeE7": 302585000, "longitudeE7": -977124000,
                 "placeId": "ChIJveracruz", "name": "Veracruz All Natural",
                 "address": "1704 E Cesar Chavez St, Austin, TX 78702, USA",
                 "locationConfidence": 87.5},
    "duration": {"startTimestamp": "2022-09-02T00:25:00.512Z",
                 "endTimestamp": "2022-09-02T02:05:00Z"},
    "editConfirmationStatus": "CONFIRMED",
    "childVisits": [{"location": {"latitudeE7": 302586000, "longitudeE7": -977125000,
                                  "placeId": "ChIJpatio", "name": "Veracruz patio"},
                     "duration": {"startTimestamp": "2022-09-02T00:30:00Z",
                                  "endTimestamp": "2022-09-02T01:00:00Z"}}]}}
HOME_PV = {"placeVisit": {
    "location": {"latitudeE7": 302612000, "longitudeE7": -977187000,
                 "placeId": "ChIJhome", "semanticType": "TYPE_HOME",
                 "address": "12 Example Ln\nAustin, TX"},
    "duration": {"startTimestampMs": "1662090000000", "endTimestampMs": "1662120000000"}}}
DRIVE = {"activitySegment": {"duration": {"startTimestamp": "2022-09-02T02:05:00Z",
                                          "endTimestamp": "2022-09-02T02:20:00Z"},
                             "activityType": "IN_PASSENGER_VEHICLE"}}


def takeout(root, objects=(VERACRUZ_PV, HOME_PV, DRIVE)):
    """The Takeout layout: Location History (Timeline)/Semantic Location
    History/<year>/<YEAR_MONTH>.json beside Records.json and Settings.json."""
    lh = root / "Takeout" / "Location History (Timeline)"
    month = lh / "Semantic Location History" / "2022" / "2022_SEPTEMBER.json"
    month.parent.mkdir(parents=True, exist_ok=True)
    month.write_text(json.dumps({"timelineObjects": list(objects)}), encoding="utf-8")
    (lh / "Records.json").write_text(json.dumps({"locations": []}), encoding="utf-8")
    (lh / "Settings.json").write_text("{}", encoding="utf-8")
    return root / "Takeout"


def _by_place(scan):
    out = {}
    for r in scan["records"]:
        out.setdefault(json.loads(r["value_json"]).get("place_id"), []).append(r)
    return out


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


@pytest.fixture
def demo(tmp_path, cfg):
    return location.scan(sample.write_sample(tmp_path / "demo", ANCHOR, "android"), cfg)


# ---------------------------------------------------------------- the demo export

def test_the_demo_timeline_holds_the_persona_at_veracruz(demo):
    visits = _by_place(demo)[sample.PLACE_IDS["veracruz"]]
    first = min(visits, key=lambda r: r["started_at"])
    assert first["started_at"] == _utc(persona.local_dt(ANCHOR, -38, "19:25"))
    assert first["journal_date"] == persona.day(ANCHOR, -38).isoformat()
    assert first["value"] == 100 and first["unit"] == "min"
    detail = json.loads(first["value_json"])
    place = persona.place("veracruz")
    assert (detail["lat"], detail["lon"]) == (place.lat, place.lon)
    assert first["participants"] == [{"kind": "place_id", "role": "location",
                                      "value": sample.PLACE_IDS["veracruz"]}]
    assert {r["journal_date"] for r in visits} == {
        persona.day(ANCHOR, off).isoformat() for off in (-38, -20, -1)}


def test_the_bled_trip_four_years_back_is_dated_in_slovenia(demo):
    trip = persona.bled_trip_days(ANCHOR)
    assert trip[0].year == ANCHOR.year - 4
    visits = sorted(_by_place(demo)[sample.PLACE_IDS["bled"]], key=lambda r: r["started_at"])
    zone_offset = int(persona.trip_dt(1, "09:40", ANCHOR).utcoffset().total_seconds())
    assert [r["journal_date"] for r in visits] == [trip[1].isoformat(), trip[2].isoformat()]
    assert all(r["utc_offset_sec"] == zone_offset for r in visits)
    assert visits[0]["started_at"] == _utc(persona.trip_dt(1, "09:40", ANCHOR))


def test_home_and_work_take_googles_label_and_other_places_their_position(demo):
    places = _by_place(demo)
    assert {r["title"] for r in places[sample.PLACE_IDS["home"]]} == {"Home"}
    assert {r["title"] for r in places[sample.PLACE_IDS["acme"]]} == {"Work"}
    assert {r["title"] for r in places[sample.PLACE_IDS["veracruz"]]} == {
        "Place at 30.2585, -97.7124"}
    assert json.loads(places[sample.PLACE_IDS["acme"]][0]["value_json"])[
        "semantic_type"] == "WORK"
    names = {h["value"]: h["display_name"] for h in demo["handles"]}
    assert names[sample.PLACE_IDS["home"]] == "Home"
    assert names[sample.PLACE_IDS["veracruz"]] is None      # the export names no place
    assert all(h["kind"] == "place_id" for h in demo["handles"])


def test_travel_route_points_and_raw_signals_are_counted_not_imported(demo):
    joined = " | ".join(demo["skipped"])
    for what in ("activity segment", "timelinePath segment", "raw signal",
                 "timelineMemory segment"):
        assert what in joined
    assert all(r["stream"] == "place_visit" for r in demo["records"])


def test_the_iphone_has_no_location_sample(tmp_path):
    with pytest.raises(ValueError, match="no export"):
        sample.write_sample(tmp_path, ANCHOR, "iphone")


def test_the_demo_sample_is_deterministic(tmp_path, cfg):
    a = location.scan(sample.write_sample(tmp_path / "a", ANCHOR, "android"), cfg)
    b = location.scan(sample.write_sample(tmp_path / "b", ANCHOR, "android"), cfg)
    assert a["records"] == b["records"]
    assert a["source_hash"] == b["source_hash"]


def test_the_demo_applies_once_then_is_identical_and_doctor_stays_green(
        tmp_path, cfg, conn):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entities",
              "entry_sources", "extraction_runs")
    before = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    path = sample.write_sample(tmp_path / "demo", ANCHOR, "android")
    _, first = _apply(conn, location.scan(path, cfg))
    assert first["added"] > 30
    plan, again = _apply(conn, location.scan(path, cfg))
    assert again["added"] == 0 and plan["identical"] == first["added"]
    after = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"] for t in tables}
    assert after == before
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


# ---------------------------------------------------------------- formats

def test_the_iphone_google_maps_list_export_reads_the_same(tmp_path, cfg):
    """Google Maps on an iPhone writes a bare list, 'geo:' positions,
    'placeID', and numbers as strings."""
    p = tmp_path / "location-history.json"
    p.write_text(json.dumps([
        {"startTime": "2026-03-10T18:30:00.000-05:00", "endTime": "2026-03-10T20:00:00.000-05:00",
         "visit": {"hierarchyLevel": "0", "probability": "0.95",
                   "topCandidate": {"placeID": "ChIJabc", "semanticType": "Inferred Home",
                                    "probability": "0.785181",
                                    "placeLocation": "geo:30.261200,-97.718700"}}},
        {"startTime": "2026-03-10T18:00:00.000-05:00", "endTime": "2026-03-10T18:30:00.000-05:00",
         "activity": {"start": "geo:30.2,-97.7", "end": "geo:30.26,-97.71",
                      "topCandidate": {"type": "walking", "probability": "0.0"}}},
    ]), encoding="utf-8")
    scan = location.scan(p, cfg)
    assert scan["facts"]["format"] == "device_list"
    (rec,) = scan["records"]
    assert rec["title"] == "Home"
    assert rec["started_at"] == "2026-03-10T23:30:00Z"
    assert rec["utc_offset_sec"] == -18000
    assert rec["journal_date"] == "2026-03-10"
    detail = json.loads(rec["value_json"])
    assert detail == {"lat": 30.2612, "lon": -97.7187, "place_id": "ChIJabc",
                      "probability": 0.7852, "semantic_type": "INFERRED_HOME"}
    assert "1 × activity segment (travel between places)" in scan["skipped"]


def test_takeout_month_files_bring_names_addresses_and_e7_positions(tmp_path, cfg):
    scan = location.scan(takeout(tmp_path), cfg)
    assert scan["facts"]["format"] == "takeout"
    recs = _by_place(scan)
    (veracruz,) = recs["ChIJveracruz"]
    assert veracruz["title"] == "Veracruz All Natural"
    assert veracruz["body"] == "1704 E Cesar Chavez St, Austin, TX 78702, USA"
    assert veracruz["started_at"] == "2022-09-02T00:25:00Z"       # sub-second dropped
    assert veracruz["utc_offset_sec"] is None
    assert veracruz["journal_date"] == "2022-09-01"               # 19:25 in Austin
    detail = json.loads(veracruz["value_json"])
    assert (detail["lat"], detail["lon"]) == (30.2585, -97.7124)
    assert detail["probability"] == 0.875                         # percent -> 0..1
    assert detail["confirmed"] is True
    (patio,) = recs["ChIJpatio"]                                  # a child visit
    assert json.loads(patio["value_json"])["hierarchy_level"] == 1
    (home,) = recs["ChIJhome"]                                    # epoch-ms timestamps
    assert home["started_at"] == "2022-09-02T03:40:00Z"
    assert home["title"] == "Home"                                # the label beats a street
    assert home["body"] == "12 Example Ln\nAustin, TX"
    assert json.loads(home["value_json"])["semantic_type"] == "HOME"
    names = {h["value"]: h["display_name"] for h in scan["handles"]}
    assert names["ChIJveracruz"] == "Veracruz All Natural"
    assert names["ChIJhome"] == "Home"
    assert "1 × activity segment (travel between places)" in scan["skipped"]
    unread = " | ".join(u["what"] for u in scan["unmapped"])
    assert "Records.json (raw location pings, not visits)" in unread
    assert "Settings.json" in unread


def test_a_takeout_zip_reads_like_the_folder(tmp_path, cfg):
    folder_scan = location.scan(takeout(tmp_path / "t"), cfg)
    z = tmp_path / "takeout-20240501.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for p in sorted((tmp_path / "t").rglob("*.json")):
            zf.write(p, p.relative_to(tmp_path / "t").as_posix())
    zip_scan = location.scan(z, cfg)
    assert zip_scan["records"] == folder_scan["records"]
    assert zip_scan["target_kind"] == "zip" and zip_scan["source_hash"]


def test_one_visit_has_the_same_identity_in_both_formats(tmp_path, cfg):
    device = device_export(tmp_path / "d", [_visit(
        "2022-09-01T19:25:00.000-05:00", "2022-09-01T21:05:00.000-05:00",
        "ChIJveracruz", "30.2585°, -97.7124°")])
    a = location.scan(device, cfg)["records"][0]["external_id"]
    b = _by_place(location.scan(takeout(tmp_path / "t"), cfg))["ChIJveracruz"][0]["external_id"]
    assert a == b == "ChIJveracruz@2022-09-02T00:25:00Z"


def test_a_visit_naming_no_place_is_keyed_on_its_position(tmp_path, cfg):
    p = device_export(tmp_path, [{
        "startTime": "2026-03-10T12:00:00.000-05:00", "endTime": "2026-03-10T13:00:00.000-05:00",
        "visit": {"topCandidate": {"placeLocation": {"latLng": "30.1°, -97.2°"}}}}])
    (rec,) = location.scan(p, cfg)["records"]
    assert rec["external_id"] == "geo:30.10000,-97.20000@2026-03-10T17:00:00Z"
    assert rec["participants"] == []


# ---------------------------------------------------------------- time

def test_a_visit_before_the_rollover_hour_belongs_to_the_previous_day(tmp_path, cfg):
    p = device_export(tmp_path, [_visit(
        "2026-03-11T01:30:00.000-05:00", "2026-03-11T02:30:00.000-05:00",
        "ChIJlate", "30.2°, -97.7°")])
    (rec,) = location.scan(p, cfg)["records"]
    assert rec["started_at"] == "2026-03-11T06:30:00Z"
    assert rec["journal_date"] == "2026-03-10"


def test_a_written_zero_offset_is_a_zone_not_unknown(tmp_path, cfg):
    """London in winter writes +00:00. 07:30 there is the London morning — in
    the home zone (Chicago) it would be 01:30, the night before."""
    p = device_export(tmp_path, [_visit(
        "2026-01-15T07:30:00.000+00:00", "2026-01-15T09:00:00.000+00:00",
        "ChIJlondon", "51.5°, -0.12°")])
    (rec,) = location.scan(p, cfg)["records"]
    assert rec["utc_offset_sec"] == 0
    assert rec["journal_date"] == "2026-01-15"


def test_an_impossible_visit_is_skipped_and_counted(tmp_path, cfg):
    p = device_export(tmp_path, [
        _visit("2026-03-10T12:00:00.000-05:00", "2026-03-10T11:00:00.000-05:00",
               "ChIJback", "30.2°, -97.7°"),
        _visit(None, "2026-03-10T11:00:00.000-05:00", "ChIJnostart", "30.2°, -97.7°"),
        {"startTime": "2026-03-10T12:00:00.000-05:00", "endTime": "2026-03-10T13:00:00.000-05:00",
         "visit": {"topCandidate": {}}},
    ])
    scan = location.scan(p, cfg)
    assert scan["records"] == []
    joined = " | ".join(scan["skipped"])
    assert "ends before it starts" in joined
    assert "no usable start time" in joined
    assert "naming no place and no position" in joined


# ---------------------------------------------------------------- re-importing

def _two_exports(tmp_path):
    """The same evening exported twice: first while still at the restaurant
    (the visit's end is the export's moment), then the next morning."""
    early = device_export(tmp_path / "early", [
        _visit("2026-03-10T08:00:00.000-05:00", "2026-03-10T17:00:00.000-05:00",
               "ChIJoffice", "30.2672°, -97.7431°", "WORK"),
        _visit("2026-03-10T19:00:00.000-05:00", "2026-03-10T19:40:00.000-05:00",
               "ChIJveracruz", "30.2585°, -97.7124°")])
    late = device_export(tmp_path / "late", [
        _visit("2026-03-10T08:00:00.000-05:00", "2026-03-10T17:00:00.000-05:00",
               "ChIJoffice", "30.2672°, -97.7431°", "WORK"),
        _visit("2026-03-10T19:00:00.000-05:00", "2026-03-10T20:45:00.000-05:00",
               "ChIJveracruz", "30.2585°, -97.7124°"),
        _visit("2026-03-10T21:00:00.000-05:00", "2026-03-11T07:00:00.000-05:00",
               "ChIJhome", "30.2612°, -97.7187°", "HOME")])
    return early, late


def test_a_later_export_finishes_the_visit_that_was_still_going_on(tmp_path, cfg, conn):
    early, late = _two_exports(tmp_path)
    _apply(conn, location.scan(early, cfg))
    scan = location.scan(late, cfg)
    plan, result = _apply(conn, scan)
    assert [rec["external_id"] for _, rec, _ in plan["updates"]] == [
        "ChIJveracruz@2026-03-11T00:00:00Z"]
    assert len(plan["refreshed"]) == 1 and len(plan["new"]) == 1
    row = conn.execute("SELECT ended_at, value FROM observations "
                       "WHERE external_id='ChIJveracruz@2026-03-11T00:00:00Z'").fetchone()
    assert (row["ended_at"], row["value"]) == ("2026-03-11T01:45:00Z", 105)
    text = "\n".join(location.report_lines(scan, plan))
    assert "place_visit: 1 new, 1 changed" in text
    assert "1 unchanged visit(s) re-dated to this export" in text


def test_an_older_export_cannot_overwrite_a_newer_one(tmp_path, cfg, conn):
    early, late = _two_exports(tmp_path)
    _apply(conn, location.scan(late, cfg))
    scan = location.scan(early, cfg)
    plan, result = _apply(conn, scan)
    assert len(plan["conflicts"]) == 1 and result["updated"] == 0
    assert conn.execute("SELECT ended_at FROM observations WHERE external_id="
                        "'ChIJveracruz@2026-03-11T00:00:00Z'").fetchone()[0] \
        == "2026-03-11T01:45:00Z"
    assert "kept existing: 1 visit(s)" in "\n".join(location.report_lines(scan, plan))


def test_a_visit_missing_from_a_later_export_is_remembered(tmp_path, cfg, conn):
    """Timeline's auto-delete is not the owner deleting a visit."""
    early, late = _two_exports(tmp_path)
    _apply(conn, location.scan(late, cfg))
    shorter = device_export(tmp_path / "shorter", [_visit(
        "2026-03-12T08:00:00.000-05:00", "2026-03-12T17:00:00.000-05:00",
        "ChIJoffice", "30.2672°, -97.7431°", "WORK")])
    plan, _ = _apply(conn, location.scan(shorter, cfg))
    assert plan["removed"] == []
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE status='active'"
                        ).fetchone()[0] == 4


# ---------------------------------------------------------------- bad input

def test_a_json_that_is_not_a_timeline_says_what_it_holds(tmp_path, cfg):
    p = tmp_path / "Timeline.json"
    p.write_text(json.dumps({"foo": 1, "bar": []}), encoding="utf-8")
    with pytest.raises(DomainError, match="semanticSegments.*it has: bar, foo"):
        location.scan(p, cfg)


def test_records_json_alone_points_at_the_semantic_history(tmp_path, cfg):
    p = tmp_path / "Records.json"
    p.write_text(json.dumps({"locations": [{"latitudeE7": 1, "longitudeE7": 2}]}),
                 encoding="utf-8")
    with pytest.raises(DomainError, match="raw location pings.*Semantic Location History"):
        location.scan(p, cfg)


def test_a_folder_without_visits_lists_what_it_has(tmp_path, cfg):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "notes.txt").write_text("hi", encoding="utf-8")
    with pytest.raises(DomainError, match="no Timeline.json.*notes.txt"):
        location.scan(tmp_path / "x", cfg)


def test_two_device_exports_in_one_folder_are_refused(tmp_path, cfg):
    device_export(tmp_path / "a", [])
    device_export(tmp_path / "a" / "older", [])
    with pytest.raises(DomainError, match="2 device exports.*pass the one"):
        location.scan(tmp_path / "a", cfg)


def test_a_file_that_is_not_json_is_refused(tmp_path, cfg):
    p = tmp_path / "Timeline.kml"
    p.write_text("<kml/>", encoding="utf-8")
    with pytest.raises(DomainError, match="not a .json file"):
        location.scan(p, cfg)


def test_malformed_json_reports_position(tmp_path, cfg):
    p = tmp_path / "Timeline.json"
    p.write_text('{"semanticSegments": [', encoding="utf-8")
    with pytest.raises(DomainError, match="not valid JSON: line 1"):
        location.scan(p, cfg)


def test_utf8_bom_export_is_accepted(tmp_path, cfg):
    p = device_export(tmp_path, [_visit(
        "2026-03-10T12:00:00.000-05:00", "2026-03-10T13:00:00.000-05:00",
        "ChIJbom", "30.2°, -97.7°")])
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    assert len(location.scan(p, cfg)["records"]) == 1


# ---------------------------------------------------------------- report

def test_report_names_the_stream_span_and_the_place_handles(demo, conn):
    text = "\n".join(location.report_lines(demo, obs.build_plan(conn, demo)))
    trip = persona.bled_trip_days(ANCHOR)
    assert f"place_visit: {len(demo['records'])} new, 0 changed" in text
    assert f"{trip[0].isoformat()}..{persona.day(ANCHOR, -1).isoformat()}" in text
    assert "Google Maps Timeline, on-device export" in text
    assert "handles link place_id:<id>" in text
    assert "skipped: " in text


# ---------------------------------------------------------------- default target

def test_default_target_is_the_configured_path(tmp_path, cfg):
    p = device_export(tmp_path / "exp", [])
    cfg.connectors = {"location": {"path": str(p)}}
    assert location.default_target(cfg) == p
    cfg.connectors = {"location": {"path": str(tmp_path / "missing.json")}}
    with pytest.raises(DomainError, match="path does not exist"):
        location.default_target(cfg)


def test_default_target_finds_the_one_export_in_downloads(tmp_path, cfg, monkeypatch):
    from lifecrm import platforms
    monkeypatch.setattr(platforms, "home", lambda: tmp_path)
    monkeypatch.setattr(platforms, "onedrive_candidates", lambda: [tmp_path / "OneDrive"])
    with pytest.raises(DomainError, match="no Timeline export found"):
        location.default_target(cfg)
    p = device_export(tmp_path / "Downloads", [])
    assert location.default_target(cfg) == p
    device_export(tmp_path / "OneDrive", [])
    with pytest.raises(DomainError, match="several Timeline exports"):
        location.default_target(cfg)


# ---------------------------------------------------------------- CLI

def test_cli_dry_run_then_apply_then_identical(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    path = sample.write_sample(tmp_path / "exp", ANCHOR, "android")
    dry = runner.invoke(app, ["observe", "location", str(path)])
    assert dry.exit_code == 0, dry.output
    assert "dry run — nothing written" in dry.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0
    applied = runner.invoke(app, ["observe", "location", str(path), "--apply"])
    assert applied.exit_code == 0, applied.output
    added = int(applied.output.split("applied: ")[1].split(" added")[0])
    assert added > 30
    again = runner.invoke(app, ["observe", "location", str(path), "--apply"])
    assert "0 added" in again.output and f"{added} identical skipped" in again.output
    note = runner.invoke(app, ["observe", "location", str(path)])
    assert "already imported" in note.output


def test_sample_timestamps_carry_the_android_offset_fields(tmp_path):
    data = json.loads(sample.write_sample(tmp_path, ANCHOR, "android")
                      .read_text(encoding="utf-8"))
    seg = next(s for s in data["semanticSegments"] if "visit" in s)
    start = datetime.fromisoformat(seg["startTime"])
    assert seg["startTimeTimezoneUtcOffsetMinutes"] == start.utcoffset().total_seconds() / 60
    assert seg["visit"]["topCandidate"]["placeLocation"]["latLng"].endswith("°")

"""GPS track connector: a phone's own GPS log (GPSLogger for Android) ->
`place_visit`, the records Google Timeline gives, so every reader of
place_visit works unchanged.

The load-bearing claims: stays are found from raw points — jitter stays one
visit, a short excursion or a GPS jump does not split it, a poor fix is
dropped, a gap counts only when the points either side agree, a night that
crosses two daily files is one visit, and the rollover dates a 1 a.m. visit
to the day before; the record has location.py's shape; re-reading a file
that has grown updates the open visit instead of adding one; the folder is
found in OneDrive with no config; and the daily collect job holds the first
import for the owner's yes.

Every fixture is written here, in GPSLogger's formats as its source writes
them (GPX 1.0, CSV, GeoJSON).
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from conftest import cli_project
from test_collect import home  # noqa: F401  (autouse: a home of its own)
from typer.testing import CliRunner

from lifecrm import collect, places
from lifecrm.cli import app
from lifecrm.connectors import base, gps_track
from lifecrm.connectors import location
from lifecrm.demo.sources import location as location_sample
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

# Geohash cell centres (Austin), so a stay's centre is never near a cell edge.
HOME = (30.266647, -97.743301)
CAFE = (30.277634, -97.743301)          # 1.2 km north
FRIEND = (30.2845, -97.759781)
CDT = timezone(timedelta(hours=-5))     # Chicago in October; cfg's home zone
CREATOR = "GPSLogger 137 - http://gpslogger.mendhak.com/"


def local(day, hm):
    h, m = (int(x) for x in hm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=CDT)


def jitter(rng, at, metres=20):
    lat, lon = at
    return (lat + rng.uniform(-metres, metres) / 110_574,
            lon + rng.uniform(-metres, metres) / 96_160)


def stay(rng, at, start, end, step=60, **extra):
    out, t = [], start
    while t <= end:
        out.append({"t": t, "pos": jitter(rng, at), **extra})
        t += timedelta(seconds=step)
    return out


def walk(a, b, start, n):
    """n points strictly between a and b, one a minute — ~150 m apart for
    the café walk, so no walking point is inside either radius."""
    return [{"t": start + timedelta(minutes=i),
             "pos": (a[0] + (b[0] - a[0]) * (i + 1) / (n + 1),
                     a[1] + (b[1] - a[1]) * (i + 1) / (n + 1))} for i in range(n)]


def day_one(rng):
    """Oct 24 (local): home, a GPS jump, a poor fix, the walk, the café with
    a silent stretch either side agrees on, a gap that does not agree, then
    home into the night."""
    pts = stay(rng, HOME, local(24, "08:00"), local(24, "09:30"))
    pts.append({"t": local(24, "08:45") + timedelta(seconds=30),
                "pos": (HOME[0] + 0.0072, HOME[1]), "hdop": 12.0})        # 800 m, poor
    pts.append({"t": local(24, "08:50") + timedelta(seconds=30),
                "pos": (HOME[0] + 0.0027, HOME[1])})                      # 300 m jump
    pts += walk(HOME, CAFE, local(24, "09:31"), 7)
    pts += stay(rng, CAFE, local(24, "09:38"), local(24, "10:00"))
    pts += stay(rng, CAFE, local(24, "12:30"), local(24, "13:00"))         # agrees: bridged
    pts += stay(rng, HOME, local(24, "15:00"), local(24, "23:59"))         # 2 h unknown before
    return sorted(pts, key=lambda p: p["t"])


def day_two(rng, until="07:00"):
    """Oct 25 (local): home past midnight, a drive to a friend's at 1 a.m.
    (before the 05:00 rollover: still the 24th's journal day), home again —
    still there when the file ends."""
    pts = stay(rng, HOME, local(25, "00:00"), local(25, "01:00"))
    pts += walk(HOME, FRIEND, local(25, "01:01"), 3)
    pts += stay(rng, FRIEND, local(25, "01:05"), local(25, "03:00"))
    pts += walk(FRIEND, HOME, local(25, "03:01"), 3)
    pts += stay(rng, HOME, local(25, "03:05"), local(25, until))
    return sorted(pts, key=lambda p: p["t"])


def _z(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_gpx(path, pts, creator=CREATOR):
    """GPX 1.0 the way GPSLogger's Gpx10FileLogger writes it."""
    rows = []
    for p in pts:
        lat, lon = p["pos"]
        extra = f"<hdop>{p['hdop']}</hdop>" if "hdop" in p else ""
        rows.append(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}"><ele>150.0</ele>'
                    f"<time>{_z(p['t'])}</time><src>gps</src><sat>9</sat>{extra}</trkpt>\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8" ?>'
        f'<gpx version="1.0" creator="{creator}" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns="http://www.topografix.com/GPX/1/0" '
        'xsi:schemaLocation="http://www.topografix.com/GPX/1/0 '
        'http://www.topografix.com/GPX/1/0/gpx.xsd">'
        f"<time>{_z(pts[0]['t'])}</time><trk><name>{path.stem}</name><trkseg>\n"
        + "".join(rows) + "</trkseg></trk></gpx>", encoding="utf-8")
    return path


CSV_HEADER = ("time,lat,lon,elevation,accuracy,bearing,speed,satellites,provider,hdop,"
              "vdop,pdop,geoidheight,ageofdgpsdata,dgpsid,activity,battery,annotation,"
              "timestamp_ms,time_offset,distance,starttimestamp_ms,profile_name,"
              "battery_charging")


def write_csv(path, pts, delim=",", decimal="."):
    """GPSLogger's CSVFileLogger columns, accuracy in metres, time_offset in
    the phone's zone."""
    def num(v):
        return str(v).replace(".", decimal)
    lines = [CSV_HEADER.replace(",", delim)]
    for p in pts:
        lat, lon = p["pos"]
        acc = p.get("acc", 8.0)
        ms = int(p["t"].timestamp() * 1000)
        offset = p["t"].astimezone(CDT).isoformat(timespec="milliseconds")
        lines.append(delim.join([_z(p["t"]), num(round(lat, 7)), num(round(lon, 7)),
                                 num(150.0), num(acc), "", "", "9", "gps", "", "", "", "",
                                 "", "", "", "80", "", str(ms), offset, "", "", "Default",
                                 "false"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_geojson(path, pts):
    feats = []
    for p in pts:
        lat, lon = p["pos"]
        props = {"time": _z(p["t"]), "provider": "gps",
                 "time_long": int(p["t"].timestamp() * 1000), "accuracy": p.get("acc", 8.0)}
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}),
                    encoding="utf-8")
    return path


def two_days(folder, until="07:00", seed=7):
    rng = random.Random(seed)
    write_gpx(folder / "20261024.gpx", day_one(rng))
    write_gpx(folder / "20261025.gpx", day_two(rng, until))
    return folder


def _visits(scan):
    return [(r["started_at"], r["ended_at"]) for r in scan["records"]]


def _utc(day, hm):
    return local(day, hm).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


EXPECTED = [("24", "08:00", "24", "09:30"),      # the jump and the poor fix: no split
            ("24", "09:38", "24", "13:00"),      # 10:00..12:30 silent, both sides agree
            ("24", "15:00", "25", "01:00"),      # one night across two daily files
            ("25", "01:05", "25", "03:00"),      # the friend's, before the rollover
            ("25", "03:05", "25", "07:00")]      # still there when the file ends


# ---------------------------------------------------------------- stays

def test_two_days_of_points_become_five_visits(tmp_path, cfg):
    scan = gps_track.scan(two_days(tmp_path / "GPSLogger"), cfg)
    assert _visits(scan) == [(_utc(int(a), b), _utc(int(c), d)) for a, b, c, d in EXPECTED]
    home_cell = f"gps:{gps_track.geohash(*HOME)}"
    cells = [json.loads(r["value_json"])["place_id"] for r in scan["records"]]
    assert cells == [home_cell, f"gps:{gps_track.geohash(*CAFE)}", home_cell,
                     f"gps:{gps_track.geohash(*FRIEND)}", home_cell]
    first = scan["records"][0]
    assert first["value"] == 90 and first["unit"] == "min"
    assert first["title"].startswith("Place at 30.26") and first["body"] is None
    d = json.loads(first["value_json"])
    assert gps_track.metres(d["lat"], d["lon"], *HOME) < 15        # the centre, not a fix
    assert first["participants"] == [{"kind": "place_id", "value": home_cell,
                                      "role": "location"}]
    assert first["origin_app"] == "gpslogger" and first["utc_offset_sec"] is None
    assert {h["value"] for h in scan["handles"]} == set(cells)
    assert all(h["display_name"] is None for h in scan["handles"])  # no name invented
    assert scan["facts"]["open_since"] == _utc(25, "03:05")
    assert any("HDOP above 5" in s for s in scan["skipped"])
    assert scan["facts"]["files_read"] == 2 and scan["facts"]["formats"] == {"GPX": 2}


def test_journal_dates_follow_the_rollover(tmp_path, cfg):
    """cfg: America/Chicago, rollover 05:00. The friend's at 01:05 on the
    25th, read from 20261025.gpx, is still the 24th's journal day."""
    scan = gps_track.scan(two_days(tmp_path / "GPSLogger"), cfg)
    assert [r["journal_date"] for r in scan["records"]] == ["2026-10-24"] * 5
    rng = random.Random(3)
    morning = stay(rng, CAFE, local(25, "06:00"), local(25, "07:00"))
    write_gpx(tmp_path / "late" / "20261025.gpx", morning)
    (late,) = gps_track.scan(tmp_path / "late", cfg)["records"]
    assert late["journal_date"] == "2026-10-25"


def test_a_long_gap_is_a_stay_only_when_both_sides_agree(tmp_path, cfg):
    rng = random.Random(1)
    agree = stay(rng, HOME, local(24, "21:00"), local(24, "21:02")) + \
        stay(rng, HOME, local(25, "07:00"), local(25, "07:02"))          # a silent night
    write_gpx(tmp_path / "a" / "night.gpx", agree)
    (night,) = gps_track.scan(tmp_path / "a", cfg)["records"]
    assert (night["started_at"], night["ended_at"]) == (_utc(24, "21:00"), _utc(25, "07:02"))

    differ = stay(rng, HOME, local(24, "21:00"), local(24, "21:02")) + \
        stay(rng, CAFE, local(25, "07:00"), local(25, "07:02"))
    write_gpx(tmp_path / "b" / "night.gpx", differ)
    assert gps_track.scan(tmp_path / "b", cfg)["records"] == []        # unknown, not a stay

    too_long = stay(rng, HOME, local(24, "08:00"), local(24, "08:20")) + \
        stay(rng, HOME, local(25, "09:00"), local(25, "09:20"))          # 24 h of silence
    write_gpx(tmp_path / "c" / "days.gpx", too_long)
    assert _visits(gps_track.scan(tmp_path / "c", cfg)) == [
        (_utc(24, "08:00"), _utc(24, "08:20")), (_utc(25, "09:00"), _utc(25, "09:20"))]


def test_short_runs_and_long_excursions(tmp_path, cfg):
    rng = random.Random(2)
    brief = stay(rng, CAFE, local(24, "12:00"), local(24, "12:08"))       # 8 min: no visit
    write_gpx(tmp_path / "a" / "x.gpx", brief)
    assert gps_track.scan(tmp_path / "a", cfg)["records"] == []
    # away 25 minutes (a run round the block, logged), then back: two visits
    out = stay(rng, HOME, local(24, "08:00"), local(24, "09:00"))
    out += [{"t": local(24, "09:00") + timedelta(minutes=i + 1),
             "pos": (HOME[0] + 0.002 * (1 + i % 5), HOME[1])} for i in range(24)]
    out += stay(rng, HOME, local(24, "09:25"), local(24, "10:00"))
    write_gpx(tmp_path / "b" / "x.gpx", out)
    assert _visits(gps_track.scan(tmp_path / "b", cfg)) == [
        (_utc(24, "08:00"), _utc(24, "09:00")), (_utc(24, "09:25"), _utc(24, "10:00"))]


# ---------------------------------------------------------------- formats

def test_csv_and_geojson_read_the_same_and_say_the_zone(tmp_path, cfg):
    rng = random.Random(7)
    pts = day_one(rng)
    for p in pts:
        p.pop("hdop", None)
    pts.append({"t": local(24, "08:40") + timedelta(seconds=15),
                "pos": (HOME[0] + 0.009, HOME[1]), "acc": 500.0})          # 1 km, 500 m
    expect = [(_utc(24, "08:00"), _utc(24, "09:30")), (_utc(24, "09:38"), _utc(24, "13:00")),
              (_utc(24, "15:00"), _utc(24, "23:59"))]
    csv_scan = gps_track.scan(write_csv(tmp_path / "c" / "20261024.csv", pts), cfg)
    assert _visits(csv_scan) == expect
    assert {r["utc_offset_sec"] for r in csv_scan["records"]} == {-5 * 3600}
    assert any("less accurate than 75 m" in s for s in csv_scan["skipped"])
    semi = gps_track.scan(write_csv(tmp_path / "s" / "20261024.csv", pts, delim=";",
                                    decimal=","), cfg)
    assert _visits(semi) == expect                                       # 30,2666 read right
    geo = gps_track.scan(write_geojson(tmp_path / "g" / "20261024.geojson", pts), cfg)
    assert _visits(geo) == expect and geo["records"][0]["origin_app"] == "gpslogger"


def test_one_day_in_two_formats_is_one_set_of_fixes(tmp_path, cfg):
    rng = random.Random(7)
    pts = day_one(rng)
    folder = tmp_path / "GPSLogger"
    write_gpx(folder / "20261024.gpx", pts)
    alone = gps_track.scan(folder, cfg)
    write_csv(folder / "20261024.csv", [p for p in pts if "hdop" not in p])
    (folder / "20261024.kml").write_text("<kml/>", encoding="utf-8")
    both = gps_track.scan(folder, cfg)
    assert _visits(both) == _visits(alone)
    assert [r["external_id"] for r in both["records"]] == \
        [r["external_id"] for r in alone["records"]]
    assert {r["utc_offset_sec"] for r in both["records"]} == {-5 * 3600}   # CSV's zone kept
    assert both["facts"]["formats"] == {"CSV": 1, "GPX": 1}
    assert any("20261024.kml (KML" in u["what"] for u in both["unmapped"])


def test_a_gpx_cut_off_mid_write_keeps_what_it_holds(tmp_path, cfg):
    rng = random.Random(4)
    path = write_gpx(tmp_path / "t" / "20261024.gpx",
                     stay(rng, HOME, local(24, "08:00"), local(24, "09:00")))
    text = path.read_text(encoding="utf-8")
    path.write_text(text[:len(text) * 2 // 3], encoding="utf-8")
    scan = gps_track.scan(tmp_path / "t", cfg)
    assert len(scan["records"]) == 1 and scan["records"][0]["value"] >= 30
    assert any("ends early" in s for s in scan["skipped"])


def test_targets_that_are_not_tracks_say_so(tmp_path, cfg):
    (tmp_path / "e").mkdir()
    (tmp_path / "e" / "notes.txt").write_text("x", encoding="utf-8")
    with pytest.raises(DomainError, match="holds no .gpx, .csv or .geojson"):
        gps_track.scan(tmp_path / "e", cfg)
    bank = tmp_path / "bank.csv"
    bank.write_text("date,amount\n2026-10-01,5\n", encoding="utf-8")
    assert gps_track.scan(bank, cfg)["unmapped"][0]["what"].startswith("bank.csv (no lat/lon")
    with pytest.raises(DomainError, match="not a .gpx, .csv, .geojson or .zip"):
        gps_track.scan(tmp_path / "e" / "notes.txt", cfg)


# ---------------------------------------------------------------- the record

def test_the_record_has_location_s_shape(tmp_path, cfg):
    theirs = location.scan(location_sample.write_sample(
        tmp_path / "demo", datetime(2026, 10, 21).date(), "android"), cfg)["records"][0]
    ours = gps_track.scan(two_days(tmp_path / "GPSLogger"), cfg)["records"][0]
    assert set(ours) == set(theirs)
    documented = {"lat", "lon", "place_id", "semantic_type", "address", "probability",
                  "hierarchy_level", "confirmed"}                # location.py's value_json
    assert set(json.loads(ours["value_json"])) == {"lat", "lon", "place_id"} <= documented
    assert ours["stream"] == theirs["stream"] == "place_visit"
    assert ours["external_id"] == f"gps@{ours['started_at']}"
    assert obs.validate(ours) is None


def test_places_reads_the_visits_unchanged(tmp_path, cfg, conn):
    _apply(conn, gps_track.scan(two_days(tmp_path / "GPSLogger"), cfg))
    pts = places.points(conn, cfg)
    assert len(pts) == 5 and {p.stream for p in pts} == {"place_visit"}
    assert gps_track.metres(pts[1].lat, pts[1].lon, *CAFE) < 15


def test_freshness_waits_for_an_import_of_its_own(tmp_path, cfg, conn):
    """place_visit is Timeline's stream too: an old Timeline export alone is
    not a GPS feed that stopped."""
    from datetime import date

    from lifecrm.doctor import run_checks

    def checks():
        out = {c["check"] for c in run_checks(conn, cfg)["checks"]}
        conn.rollback()
        return out
    sample = location_sample.write_sample(tmp_path / "tl", date(2025, 3, 1), "android")
    _apply(conn, location.scan(sample, cfg))
    assert "gps_track_data_age" not in checks()
    _apply(conn, gps_track.scan(two_days(tmp_path / "GPSLogger"), cfg))
    assert "gps_track_data_age" in checks()


# ---------------------------------------------------------------- re-reading

def test_a_grown_file_updates_the_open_visit_and_adds_nothing_twice(tmp_path, cfg, conn):
    folder = two_days(tmp_path / "GPSLogger", until="05:00")
    first = gps_track.scan(folder, cfg)
    assert first["records"][-1]["ended_at"] == _utc(25, "05:00")        # open, end so far
    _, done = _apply(conn, first)
    assert done["added"] == 5

    same = obs.build_plan(conn, gps_track.scan(folder, cfg))
    assert same["new"] == [] and same["updates"] == [] and same["identical"] == 5

    two_days(folder, until="07:00")                                    # the file grew
    plan, again = _apply(conn, gps_track.scan(folder, cfg))
    assert again["added"] == 0 and again["updated"] == 1 and again["conflicts"] == 0
    (row, rec, changes), = plan["updates"]
    assert row["started_at"] == _utc(25, "03:05") and rec["ended_at"] == _utc(25, "07:00")
    assert "ended_at" in changes and "value" in changes
    assert again["refreshed"] == 4                       # unchanged, re-dated to the newest fix
    rows = conn.execute("SELECT COUNT(*) FROM observations WHERE stream='place_visit'"
                        ).fetchone()[0]
    assert rows == 5

    # an older reading (yesterday's copy) overwrites nothing
    old = gps_track.scan(two_days(tmp_path / "old", until="05:00"), cfg)
    stale = obs.build_plan(conn, old)
    assert stale["updates"] == [] and len(stale["conflicts"]) == 1


def test_mid_excursion_reading_keeps_the_same_visit(tmp_path, cfg):
    """Read while the phone is away from the café for 4 minutes (GPS jump
    or stepping out), then again once it is back: the same visit, longer."""
    rng = random.Random(5)
    base_pts = stay(rng, CAFE, local(24, "12:00"), local(24, "12:30"))
    away = [{"t": local(24, "12:31") + timedelta(minutes=i),
             "pos": (CAFE[0] + 0.003, CAFE[1])} for i in range(4)]
    back = stay(rng, CAFE, local(24, "12:35"), local(24, "13:00"))
    write_gpx(tmp_path / "a" / "x.gpx", base_pts + away)
    write_gpx(tmp_path / "b" / "x.gpx", base_pts + away + back)
    (mid,) = gps_track.scan(tmp_path / "a", cfg)["records"]
    (end,) = gps_track.scan(tmp_path / "b", cfg)["records"]
    assert mid["external_id"] == end["external_id"]
    assert (mid["ended_at"], end["ended_at"]) == (_utc(24, "12:30"), _utc(24, "13:00"))


# ---------------------------------------------------------------- finding the folder

def test_default_target_finds_the_onedrive_folder(cfg, home):  # noqa: F811
    with pytest.raises(DomainError, match="no GPS track folder found"):
        gps_track.default_target(cfg)
    folder = home / "OneDrive" / "GPSLogger"
    folder.mkdir(parents=True)
    with pytest.raises(DomainError, match="holds no track files yet"):
        gps_track.default_target(cfg)
    two_days(folder)
    assert gps_track.default_target(cfg) == folder
    lower = two_days(home / "OneDrive" / "Apps" / "gpslogger")           # one folder down
    with pytest.raises(base.Ambiguous, match="several GPS track folders"):
        gps_track.default_target(cfg)
    cfg.connectors = {"gps_track": {"path": str(lower)}}
    assert gps_track.default_target(cfg) == lower
    cfg.connectors = {"gps_track": {"path": str(home / "nowhere")}}
    with pytest.raises(DomainError, match=r"\[connectors.gps_track\] path does not exist"):
        gps_track.default_target(cfg)


def test_one_folder_down_alone_is_found(cfg, home):  # noqa: F811
    folder = two_days(home / "OneDrive" / "Apps" / "GPSLogger")
    assert gps_track.default_target(cfg) == folder


# ---------------------------------------------------------------- collect

def test_collect_holds_the_first_import_then_takes_over(conn, cfg, home):  # noqa: F811
    folder = two_days(home / "OneDrive" / "GPSLogger", until="05:00")
    by = {r["source"]: r for r in collect.run(conn, cfg)["sources"]}
    assert (by["gps_track"]["outcome"], by["gps_track"]["hold"]) == ("held", "first_import")
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0

    done = collect.approve(conn, cfg, "gps_track", apply=True)
    assert done["applied"] and done["applied_counts"]["added"] == 5
    assert done["doc"] == "docs/gps_track.md" and done["checklist"]

    cfg.extra = {**(cfg.extra or {}), "collect": {"sources": ["gps_track"]}}
    two_days(folder, until="07:00")
    (grown,) = collect.run(conn, cfg)["sources"]
    assert grown["outcome"] == "applied" and grown["counts"]["updated"] == 1
    (same,) = collect.run(conn, cfg)["sources"]
    assert same["outcome"] == "unchanged"


# ---------------------------------------------------------------- CLI

def test_observe_dry_run_through_the_cli(tmp_path, monkeypatch):
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    folder = two_days(tmp_path / "GPSLogger")
    dry = runner.invoke(app, ["observe", "gps_track", str(folder)])
    assert dry.exit_code == 0, dry.output
    assert "gps track: GPSLogger (folder, 2 file(s) read: 2 GPX" in dry.output
    assert "place_visit: 5 new, 0 changed | 2026-10-24..2026-10-24" in dry.output
    assert "still open: the newest visit" in dry.output
    assert "dry run — nothing written" in dry.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0


# ---------------------------------------------------------------- helpers

def test_geohash_and_distance():
    assert gps_track.geohash(57.64911, 10.40744, 11) == "u4pruydqqvj"   # the reference value
    assert 1210 < gps_track.metres(*HOME, *CAFE) < 1230
    assert gps_track.metres(0.0, 179.9995, 0.0, -179.9995) < 120         # across 180°

"""The fitness demo sample: the persona's training, through the real connector.

Both combos write a Strong CSV and a Strava archive, and both must tell the
persona's story — squats on day -40, bench with Sam at 185 lb on day -12,
the 5k at Zilker in 27 minutes, the 7.2 km run, and the hike above Lake Bled
four years back — on the days persona._WORKOUTS (and so the watch) has them.
"""

import json
import zipfile
from datetime import date

import pytest

from lifecrm.connectors import fitness
from lifecrm.demo import persona
from lifecrm.demo.sources import fitness as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

ANCHOR = date(2026, 10, 21)


def _scans(tmp_path, cfg, combo, anchor=ANCHOR):
    strong, strava = sample.write_samples(tmp_path / combo, anchor, combo)
    return fitness.scan(strong, cfg), fitness.scan(strava, cfg)


def _day(offset, anchor=ANCHOR):
    return persona.day(anchor, offset).isoformat()


def _exercise(rec, name):
    return next(e for e in json.loads(rec["value_json"])["exercises"] if e["name"] == name)


def test_the_sample_matches_the_personas_workouts():
    strength = {o for o, w in persona._WORKOUTS.items() if w[0] == "strength"}
    runs = {o for o, w in persona._WORKOUTS.items() if w[0] == "running"}
    assert set(sample.STRENGTH) == strength and set(sample.RUNS) == runs
    for offset, run in sample.RUNS.items():
        _, _, minutes, km = persona._WORKOUTS[offset]
        assert run["moving"] == minutes * 60 and round(run["meters"] / 1000, 1) == km


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_strong_holds_the_strength_days_on_their_days(tmp_path, cfg, combo):
    strong, _ = _scans(tmp_path, cfg, combo)
    got = {r["journal_date"]: r["value"] for r in strong["records"]}
    want = {_day(o): float(w[2]) for o, w in persona._WORKOUTS.items() if w[0] == "strength"}
    assert got == want
    assert all(r["origin_app"] == "strong" for r in strong["records"])
    assert strong["files"][0]["assumed"] == ["weight"]          # lb: the file never says


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_bench_with_sam_three_sets_of_five_at_185_on_day_minus_12(tmp_path, cfg, combo):
    strong, _ = _scans(tmp_path, cfg, combo)
    bench_day = next(r for r in strong["records"] if r["journal_date"] == _day(-12))
    bench = _exercise(bench_day, "Bench Press (Barbell)")
    work = [s for s in bench["sets"] if s["type"] != "warmup"]
    assert [(s["weight"], s["unit"], s["reps"]) for s in work] == [(185, "lb", 5)] * 3
    assert bench["top_set"] == {"weight": 185, "unit": "lb", "reps": 5, "weight_kg": 83.9}
    assert "Bench Press (Barbell) 3×5 @ 185 lb" in bench_day["body"]
    assert "with Sam" in bench_day["body"]
    assert bench_day["started_at"] == persona.utc_iso(persona.local_dt(ANCHOR, -12, "06:30"))
    legs = next(r for r in strong["records"] if r["journal_date"] == _day(-40))
    assert "Squat (Barbell) 5×5 @ 225 lb" in legs["body"]


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_dry_run_shows_the_bench_and_squat_bests(tmp_path, cfg, conn, combo):
    strong, _ = _scans(tmp_path, cfg, combo)
    report = "\n".join(fitness.report_lines(strong, obs.build_plan(conn, strong)))
    assert f"new best: Bench Press (Barbell) 185 lb × 5 on {_day(-12)}, up from 180 lb × 5 " \
           f"on {_day(-24)}" in report
    assert f"new best: Squat (Barbell) 230 lb × 5 on {_day(-19)}, up from 225 lb × 5 " \
           f"on {_day(-40)}" in report
    assert "workout: 6 new" in report and "read weights as lb" in report


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_strava_has_the_runs_and_the_bled_hike(tmp_path, cfg, combo):
    _, strava = _scans(tmp_path, cfg, combo)
    assert strava["account"] == f"strava:{sample.ATHLETE_ID}"
    by_day = {r["journal_date"]: r for r in strava["records"]}
    zilker = by_day[_day(-27)]
    assert zilker["title"] == "Zilker Park 5k" and zilker["value"] == 27.0
    assert json.loads(zilker["value_json"])["distance_km"] == 5.0
    assert zilker["started_at"] == persona.utc_iso(persona.local_dt(ANCHOR, -27, "18:15"))
    long_run = by_day[_day(-15)]
    assert json.loads(long_run["value_json"])["distance_km"] == 7.2
    assert long_run["value"] == 41.0
    assert json.loads(by_day[_day(-33)]["value_json"])["distance_km"] == 5.61
    trip = [d.isoformat() for d in persona.bled_trip_days(ANCHOR)]
    hikes = [r for r in strava["records"] if r["journal_date"] in trip]
    assert len(hikes) == 1 and "Lake Bled" in hikes[0]["title"]
    assert ANCHOR.year - int(hikes[0]["journal_date"][:4]) == 4
    assert json.loads(hikes[0]["value_json"])["activity_type"] == "Hike"
    assert all(r["media_path"].startswith("activities/") and r["media_path"].endswith(".gpx")
               for r in strava["records"])
    assert strava["files"][0]["assumed"] == []                  # meters column read


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_applies_cleanly_and_reimports_as_identical(tmp_path, cfg, conn, combo):
    for scan in _scans(tmp_path, cfg, combo):
        plan = obs.build_plan(conn, scan)
        assert plan["invalid"] == [] and plan["dup_incoming"] == 0
        assert obs.apply_plan(conn, scan, plan)["added"] == len(scan["records"])
        conn.commit()
    for scan in _scans(tmp_path / "again", cfg, combo):
        again = obs.build_plan(conn, scan)
        assert again["new"] == [] and again["identical"] == len(scan["records"])
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE stream='workout'"
                        ).fetchone()["c"] == 10
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_is_deterministic(tmp_path, combo):
    a = sample.write_samples(tmp_path / "a", ANCHOR, combo)
    b = sample.write_samples(tmp_path / "b", ANCHOR, combo)
    assert [p.name for p in a] == ["strong.csv", f"export_{sample.ATHLETE_ID}.zip"]
    assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]


def test_the_combos_differ_only_in_the_phone_that_recorded_the_tracks(tmp_path):
    android = sample.write_samples(tmp_path / "android", ANCHOR, "android")
    iphone = sample.write_samples(tmp_path / "iphone", ANCHOR, "iphone")
    assert android[0].read_bytes() == iphone[0].read_bytes()
    with zipfile.ZipFile(android[1]) as za, zipfile.ZipFile(iphone[1]) as zi:
        assert za.namelist() == zi.namelist()
        assert za.read("activities.csv") == zi.read("activities.csv")
        gpx = next(n for n in za.namelist() if n.endswith(".gpx"))
        assert b'creator="StravaGPX Android"' in za.read(gpx)
        assert b'creator="StravaGPX iPhone"' in zi.read(gpx)


def test_the_sample_follows_the_anchor(tmp_path, cfg):
    later = date(2027, 3, 2)
    strong, strava = _scans(tmp_path, cfg, "android", anchor=later)
    assert max(r["journal_date"] for r in strong["records"]) == _day(-2, later)
    assert max(r["journal_date"] for r in strava["records"]) == _day(-15, later)


def test_an_unknown_combo_is_refused(tmp_path):
    with pytest.raises(ValueError) as e:
        sample.write_samples(tmp_path, ANCHOR, "blackberry")
    assert "blackberry" in str(e.value)

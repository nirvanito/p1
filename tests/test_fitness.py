"""Fitness connector: Strong, Hevy and Strava exports.

The connector is a pure reader, so most of this runs scan() against small
exports shaped like each app's real download — Strong's one-row-per-set CSV
with the workout repeated on every row (and its ';'-and-decimal-comma
variant), Hevy's workouts.csv, Strava's activities.csv with its doubled
columns. The rest is what a workout importer can get quietly wrong: a unit
the file never states, a UTC date read as local, sets counted as workouts,
and a later export that must win without a partial one deleting anything.
"""

import json
import zipfile

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import platforms
from lifecrm.cli import app
from lifecrm.connectors import base, fitness
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

STRONG_HEADER = ("Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,"
                 "Distance,Seconds,Notes,Workout Notes,RPE\n")
PUSH = "2026-10-09 06:30:00,Push Day,1h 10m"
STRONG = STRONG_HEADER + (
    f"{PUSH},Bench Press (Barbell),W,135.0,5,0,0,,Bench day with Sam,\n"
    f"{PUSH},Bench Press (Barbell),1,185.0,5,0,0,felt solid,Bench day with Sam,8\n"
    f"{PUSH},Bench Press (Barbell),2,185.0,5,0,0,,Bench day with Sam,8.5\n"
    f"{PUSH},Bench Press (Barbell),Rest Timer,0,0,0,90,,Bench day with Sam,\n"
    f"{PUSH},Bench Press (Barbell),3,185.0,4,0,0,,Bench day with Sam,9\n"
    f"{PUSH},Pull Up,1,0,8,0,0,,Bench day with Sam,\n"
    f"{PUSH},Pull Up,2,0,6,0,0,,Bench day with Sam,\n"
    f"{PUSH},Plank,1,0,0,0,60,,Bench day with Sam,\n"
    "2026-10-07 18:00:00,Cardio,25m,Running (Treadmill),1,0,0,2.5,1500,,,\n"
    "2026-10-07 18:00:00,Cardio,25m,Running (Treadmill),2,0,0,0,0,,,\n")

STRONG_EU = (
    "sep=;\n"
    "Date;Workout Name;Exercise Name;Set Order;Weight;Weight Unit;Reps;RPE;Distance;"
    "Distance Unit;Seconds;Notes;Workout Notes;Workout Duration\n"
    "2026-10-09 06:30:00;Oberkörper;Bankdrücken (Langhantel);1;82,5;kg;5;;;;;;;1h 10m\n"
    "2026-10-09 06:30:00;Oberkörper;Bankdrücken (Langhantel);2;82,5;kg;5;;;;;;;1h 10m\n"
    "2026-10-09 06:30:00;Oberkörper;Rudern;1;60;kg;8;;1,2;km;;;;1h 10m\n")

HEVY = (
    '"title","start_time","end_time","description","exercise_title","superset_id",'
    '"exercise_notes","set_index","set_type","weight_kg","reps","distance_km",'
    '"duration_seconds","rpe"\n'
    '"Upper A","9 Oct 2026, 06:30","9 Oct 2026, 07:41","Bench day with Sam",'
    '"Bench Press (Barbell)","","Pause at the chest","0","warmup","60","5","","",""\n'
    '"Upper A","9 Oct 2026, 06:30","9 Oct 2026, 07:41","Bench day with Sam",'
    '"Bench Press (Barbell)","","Pause at the chest","1","normal","84","5","","","8"\n'
    '"Upper A","9 Oct 2026, 06:30","9 Oct 2026, 07:41","Bench day with Sam",'
    '"Bench Press (Barbell)","","Pause at the chest","2","failure","84","4","","","10"\n'
    '"Upper A","9 Oct 2026, 06:30","9 Oct 2026, 07:41","Bench day with Sam",'
    '"Bent Over Row (Barbell)","0","","0","normal","70","8","","",""\n'
    '"Upper A","9 Oct 2026, 06:30","9 Oct 2026, 07:41","Bench day with Sam",'
    '"Face Pull","0","","0","dropset","20","15","","",""\n'
    '"Upper A","9 Oct 2026, 06:30","9 Oct 2026, 07:41","Bench day with Sam",'
    '"Treadmill","","","0","normal","","","1.6","600",""\n'
    '"Lower B","11 Oct 2026, 17:05","11 Oct 2026, 18:02","",'
    '"Squat (Barbell)","","","0","normal","100","5","","",""\n')

STRAVA = (
    "Activity ID,Activity Date,Activity Name,Activity Type,Activity Description,"
    "Elapsed Time,Distance,Max Heart Rate,Relative Effort,Commute,Activity Private Note,"
    "Activity Gear,Filename,Elapsed Time,Moving Time,Distance,Elevation Gain,"
    "Max Heart Rate,Average Heart Rate,Calories\n"
    '14032001395,"Sep 24, 2026, 11:15:00 PM",Zilker Park 5k,Run,"After work, felt quick",'
    "1695,3.11,176.0,51,false,,Nike Pegasus 40,activities/14032001395.gpx,1695.0,1620.0,"
    "5003.1,32.0,176.0,158.0,283.0\n"
    '8208000690,"Sep 29, 2022, 11:30:00 AM",Hike to Ojstrica,Hike,,9600,4.23,162.0,,'
    "false,steep last bit,,activities/8208000690.fit.gz,9600.0,7500.0,6812.0,395.0,"
    "162.0,128.0,640.0\n")

STRAVA_OLD = (
    "Activity ID,Activity Date,Activity Name,Activity Type,Activity Description,"
    "Elapsed Time,Distance,Commute,Activity Gear,Filename\n"
    "1234567890,2019-03-10 14:12:31,Morning Run,Run,,1800,5.0,false,,"
    "activities/1234567890.gpx\n")


def export(folder, name, text, bom=False, encoding="utf-8"):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode(encoding))
    return p


def strava_zip(folder, name="export_48213377.zip", activities=STRAVA, prefix=""):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(prefix + "activities.csv", activities)
        zf.writestr(prefix + "activities/14032001395.gpx", "<gpx/>")
        zf.writestr(prefix + "activities/8208000690.fit.gz", b"\x1f\x8b binary")
        zf.writestr(prefix + "profile.csv", "Athlete ID,Email Address\n48213377,a@example.com\n")
        zf.writestr(prefix + "media/1.jpg", b"\xff\xd8")
        zf.writestr("__MACOSX/._activities.csv", "junk")
    return p


def _with(cfg, **settings):
    cfg.connectors = {"fitness": settings}
    return cfg


def _by_title(scan, title):
    return next(r for r in scan["records"] if r["title"] == title)


def _detail(rec):
    return json.loads(rec["value_json"])


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _report(conn, scan):
    return "\n".join(fitness.report_lines(scan, obs.build_plan(conn, scan)))


# ---------------------------------------------------------------- Strong

def test_strong_rows_become_one_workout_per_session_not_per_set(tmp_path, cfg):
    scan = fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg)
    assert [r["title"] for r in scan["records"]] == ["Cardio", "Push Day"]
    push = _by_title(scan, "Push Day")
    assert push["stream"] == "workout" and push["origin_app"] == "strong"
    assert push["value"] == 70.0 and push["unit"] == "min"
    assert push["started_at"] == "2026-10-09T11:30:00Z"       # 06:30 in Austin (CDT)
    assert push["ended_at"] == "2026-10-09T12:40:00Z"
    assert push["journal_date"] == "2026-10-09"
    assert push["direction"] is None and push["media_path"] is None
    assert push["external_id"] == "strong:2026-10-09T06:30:00:push day"
    assert push["body"] == ("Bench Press (Barbell) 185×5, 185×5, 185×4 lb (+1 warm-up); "
                            "Pull Up 8, 6 reps; Plank 1×1:00\nBench day with Sam")


def test_strong_sets_keep_weight_unit_reps_rpe_and_type(tmp_path, cfg):
    detail = _detail(_by_title(fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg),
                               "Push Day"))
    bench, pull, plank = detail["exercises"]
    assert bench["name"] == "Bench Press (Barbell)" and bench["notes"] == "felt solid"
    assert bench["sets"] == [
        {"weight": 135, "unit": "lb", "reps": 5, "type": "warmup"},
        {"weight": 185, "unit": "lb", "reps": 5, "rpe": 8, "type": "normal"},
        {"weight": 185, "unit": "lb", "reps": 5, "rpe": 8.5, "type": "normal"},
        {"weight": 185, "unit": "lb", "reps": 4, "rpe": 9, "type": "normal"}]
    assert bench["top_set"] == {"weight": 185, "unit": "lb", "reps": 5, "weight_kg": 83.9}
    assert pull["sets"] == [{"reps": 8, "type": "normal"}, {"reps": 6, "type": "normal"}]
    assert "top_set" not in pull                                # bodyweight: no load
    assert plank["sets"] == [{"seconds": 60, "type": "normal"}]
    # working sets only: 185 lb × 14 reps
    assert detail["total_volume_kg"] == round(185 * 0.45359237 * 14, 1)
    assert detail["app"] == "strong" and detail["notes"] == "Bench day with Sam"
    assert detail["unit_source"] == "setting"


def test_strong_cardio_distance_is_in_the_configured_unit(tmp_path, cfg):
    cardio = _by_title(fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg), "Cardio")
    detail = _detail(cardio)
    assert detail["exercises"][0]["sets"] == [
        {"distance": 2.5, "distance_unit": "mi", "seconds": 1500, "type": "normal"}]
    assert detail["distance_km"] == 4.02
    assert cardio["body"] == "Running (Treadmill) 2.5 mi in 25:00"
    assert cardio["value"] == 25.0


def test_the_weight_unit_setting_says_what_strong_does_not(tmp_path, cfg):
    _with(cfg, weight_unit="kg")
    scan = fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg)
    detail = _detail(_by_title(scan, "Push Day"))
    assert detail["exercises"][0]["sets"][1]["unit"] == "kg"
    assert detail["exercises"][0]["top_set"]["weight_kg"] == 185
    # distance follows the weight unit unless it is set too
    assert _detail(_by_title(scan, "Cardio"))["exercises"][0]["sets"][0][
        "distance_unit"] == "km"
    _with(cfg, weight_unit="lbs", distance_unit="km")
    scan = fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg)
    assert _detail(_by_title(scan, "Cardio"))["distance_km"] == 2.5
    assert scan["facts"]["weight_unit"] == "lb"


@pytest.mark.parametrize("settings,needle", [
    ({"weight_unit": "stone"}, "weight_unit must be 'lb' or 'kg', got 'stone'"),
    ({"distance_unit": "furlong"}, "distance_unit must be 'mi' or 'km', got 'furlong'"),
])
def test_a_bad_unit_setting_fails_naming_it(tmp_path, cfg, settings, needle):
    _with(cfg, **settings)
    with pytest.raises(DomainError) as e:
        fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg)
    assert needle in str(e.value) and "[connectors.fitness]" in str(e.value)
    assert "\n" not in str(e.value)


def test_the_semicolon_variant_reads_decimal_commas_and_its_unit_columns(tmp_path, cfg):
    scan = fitness.scan(export(tmp_path, "strong.csv", STRONG_EU), cfg)
    (rec,) = scan["records"]
    assert rec["title"] == "Oberkörper" and rec["value"] == 70.0
    detail = _detail(rec)
    bench, row = detail["exercises"]
    assert bench["sets"][0] == {"weight": 82.5, "unit": "kg", "reps": 5, "type": "normal"}
    assert row["sets"][0]["distance"] == 1.2 and row["sets"][0]["distance_unit"] == "km"
    assert "unit_source" not in detail                         # the file said kg
    assert scan["files"][0]["assumed"] == [] and scan["files"][0]["delimiter"] == ";"
    assert rec["body"].startswith("Bankdrücken (Langhantel) 2×5 @ 82.5 kg; Rudern")


def test_units_in_the_header_are_read_from_the_header(tmp_path, cfg):
    text = ("Workout #,Date,Workout Name,Duration (sec),Exercise Name,Set Order,"
            "Weight (kg),Reps,RPE,Distance (meters),Seconds,Notes,Workout Notes\n"
            "12,2026-10-09 06:30:00,Push,4200,Bench Press (Barbell),1,84,5,,0,0,,\n"
            "12,2026-10-09 06:30:00,Push,4200,Rowing (Machine),1,0,0,,2000,480,,\n")
    scan = fitness.scan(export(tmp_path, "strong.csv", text), cfg)
    (rec,) = scan["records"]
    assert rec["value"] == 70.0
    detail = _detail(rec)
    assert detail["exercises"][0]["sets"][0]["unit"] == "kg"
    assert detail["exercises"][1]["sets"][0]["distance_unit"] == "m"
    assert detail["distance_km"] == 2.0
    assert scan["files"][0]["columns_not_read"] == ["workout #"]


def test_a_duration_with_no_unit_is_reported_not_guessed(tmp_path, cfg):
    text = STRONG_HEADER + "2026-10-09 06:30:00,Push,4200,Bench Press (Barbell),1,185,5,0,0,,,\n"
    scan = fitness.scan(export(tmp_path, "strong.csv", text), cfg)
    (rec,) = scan["records"]
    assert rec["value"] is None and rec["ended_at"] is None and rec["unit"] is None
    (odd,) = scan["unmapped"]
    assert odd["what"].startswith("workout duration with no unit")
    assert odd["example"] == "strong.csv:2 ('4200')"


def test_rest_timers_empty_sets_and_bad_numbers_are_counted_not_hidden(tmp_path, cfg):
    text = STRONG + (
        "2026-10-07 18:00:00,Cardio,25m,Running (Treadmill),3,0,0,two,0,,,\n"
        "10/07/2026 18:00,Cardio,25m,Running (Treadmill),4,0,0,1,0,,,\n"
        "2026-10-07 18:00:00,Cardio,25m,Running (Treadmill),X,0,0,1,0,,,\n")
    scan = fitness.scan(export(tmp_path, "strong.csv", text), cfg)
    assert scan["skipped"] == ["1 × rest timer row (time between sets, not a set)",
                               "1 × set with no weight, reps, distance or time"]
    odd = {u["what"]: u for u in scan["unmapped"]}
    assert odd["row with a date that does not parse"]["example"] == \
        "strong.csv:13 ('10/07/2026 18:00')"
    assert odd["set with a distance that is not a number (set not imported)"]["count"] == 1
    assert "set order this reader does not know ('X') — kept as the set's type" in odd
    sets = _detail(_by_title(scan, "Cardio"))["exercises"][0]["sets"]
    assert [s["type"] for s in sets] == ["normal", "x"]


def test_a_utf8_bom_export_is_accepted(tmp_path, cfg):
    scan = fitness.scan(export(tmp_path, "strong.csv", STRONG, bom=True), cfg)
    assert len(scan["records"]) == 2


def test_a_windows_1252_export_is_accepted(tmp_path, cfg):
    text = STRONG_HEADER + "2026-10-09 06:30:00,Día de pierna,1h,Sentadilla,1,100,5,0,0,,,\n"
    scan = fitness.scan(export(tmp_path, "strong.csv", text, encoding="cp1252"), cfg)
    assert scan["records"][0]["title"] == "Día de pierna"


# ---------------------------------------------------------------- Hevy

def test_hevy_workouts_carry_their_units_set_types_supersets_and_end(tmp_path, cfg):
    scan = fitness.scan(export(tmp_path, "workouts.csv", HEVY), cfg)
    assert [r["title"] for r in scan["records"]] == ["Upper A", "Lower B"]
    upper = _by_title(scan, "Upper A")
    assert upper["origin_app"] == "hevy"
    assert upper["started_at"] == "2026-10-09T11:30:00Z"
    assert upper["ended_at"] == "2026-10-09T12:41:00Z" and upper["value"] == 71.0
    assert upper["external_id"] == "hevy:2026-10-09T06:30:00:upper a"
    detail = _detail(upper)
    bench, row, face, tread = detail["exercises"]
    assert [s["type"] for s in bench["sets"]] == ["warmup", "normal", "failure"]
    assert bench["sets"][1] == {"weight": 84, "unit": "kg", "reps": 5, "rpe": 8,
                                "type": "normal"}
    assert bench["notes"] == "Pause at the chest" and "superset" not in bench
    assert row["superset"] == "0" and face["superset"] == "0"
    assert face["sets"][0]["type"] == "dropset"
    assert tread["sets"] == [{"distance": 1.6, "distance_unit": "km", "seconds": 600,
                              "type": "normal"}]
    assert detail["notes"] == "Bench day with Sam"
    assert detail["total_volume_kg"] == 84 * 9 + 70 * 8 + 20 * 15
    assert "unit_source" not in detail
    assert scan["files"][0]["assumed"] == []


def test_hevy_in_pounds_and_miles(tmp_path, cfg):
    text = HEVY.replace('"weight_kg"', '"weight_lbs"').replace('"distance_km"',
                                                               '"distance_miles"')
    detail = _detail(_by_title(fitness.scan(export(tmp_path, "workouts.csv", text), cfg),
                               "Upper A"))
    assert detail["exercises"][0]["sets"][1]["unit"] == "lb"
    assert detail["exercises"][3]["sets"][0]["distance_unit"] == "mi"
    assert detail["distance_km"] == round(1.6 * 1.609344, 2)


# ---------------------------------------------------------------- Strava

def test_strava_activities_read_the_meters_column_and_utc_dates(tmp_path, cfg):
    scan = fitness.scan(export(tmp_path, "activities.csv", STRAVA), cfg)
    run = _by_title(scan, "Zilker Park 5k")
    assert run["external_id"] == "strava:14032001395" and run["origin_app"] == "strava"
    assert run["started_at"] == "2026-09-24T23:15:00Z"         # UTC in the file
    assert run["journal_date"] == "2026-09-24"                  # 18:15 in Austin
    assert run["ended_at"] == "2026-09-24T23:43:15Z"            # + elapsed time
    assert run["value"] == 27.0 and run["unit"] == "min"        # moving time
    assert run["media_path"] == "activities/14032001395.gpx"
    assert run["body"] == ("Run 5.00 km in 27:00 (5:24 /km), +32 m, avg HR 158 / max 176, "
                           "283 kcal\nAfter work, felt quick")
    detail = _detail(run)
    assert detail == {"app": "strava", "activity_id": "14032001395",
                      "activity_type": "Run", "distance_km": 5.0, "elevation_m": 32,
                      "avg_hr": 158, "max_hr": 176, "calories": 283, "moving_s": 1620,
                      "elapsed_s": 1695, "relative_effort": 51,
                      "gear": "Nike Pegasus 40", "notes": "After work, felt quick"}
    hike = _by_title(scan, "Hike to Ojstrica")
    assert _detail(hike)["private_note"] == "steep last bit"
    assert hike["media_path"] == "activities/8208000690.fit.gz"
    assert scan["files"][0]["assumed"] == []


def test_strava_export_zip_reads_activities_csv_only(tmp_path, cfg):
    scan = fitness.scan(strava_zip(tmp_path), cfg)
    assert len(scan["records"]) == 2
    assert scan["account"] == "strava:48213377"
    assert scan["skipped"] == [
        "2 × activity file (GPS track) — never parsed; each activity keeps its path "
        "as media_path",
        "1 × file that is not a CSV export (e.g. media/1.jpg)",
        "1 × CSV that is not a workout export (e.g. profile.csv)"]
    assert scan["files"][0]["tracks"] == 2


def test_an_unzipped_strava_folder_is_read_the_same(tmp_path, cfg):
    folder = tmp_path / "export_48213377"
    with zipfile.ZipFile(strava_zip(tmp_path / "z")) as zf:
        zf.extractall(folder)
    scan = fitness.scan(folder, cfg)
    assert [r["external_id"] for r in scan["records"]] == \
        [r["external_id"] for r in fitness.scan(strava_zip(tmp_path / "z2"), cfg)["records"]]
    assert scan["account"] == "strava:48213377"


def test_an_older_strava_export_with_one_distance_column_says_which_unit(tmp_path, cfg, conn):
    scan = fitness.scan(export(tmp_path, "activities.csv", STRAVA_OLD), cfg)
    (rec,) = scan["records"]
    assert _detail(rec)["distance_km"] == round(5.0 * 1.609344, 2)
    assert _detail(rec)["unit_source"] == "setting"
    assert rec["value"] == 30.0                                 # no moving time: elapsed
    text = _report(conn, scan)
    assert "distance read as mi ([connectors.fitness] distance_unit" in text
    _with(cfg, distance_unit="km")
    (rec,) = fitness.scan(export(tmp_path, "activities.csv", STRAVA_OLD), cfg)["records"]
    assert _detail(rec)["distance_km"] == 5.0


def test_strava_rows_without_an_id_or_date_are_reported(tmp_path, cfg):
    text = STRAVA + (',"Sep 25, 2026, 1:00:00 PM",No id,Run,,60,,,,,,,,,,,,,,\n'
                     '99,"someday",Bad date,Run,,60,,,,,,,,,,,,,,\n'
                     '98,"Sep 25, 2026, 1:00:00 PM",Bad number,Run,,sixty,,,,,,,,,,,,,,\n')
    scan = fitness.scan(export(tmp_path, "activities.csv", text), cfg)
    assert len(scan["records"]) == 3
    odd = {u["what"]: u for u in scan["unmapped"]}
    assert odd["activity row with no Activity ID"]["count"] == 1
    assert odd["activity with an Activity Date that does not parse"]["example"] == \
        "activities.csv:5 ('someday')"
    # one bad cell costs that figure, not the activity
    assert odd["activity 'elapsed time' that is not a number (left empty)"]["example"] == \
        "activities.csv:6 ('sixty')"
    kept = _by_title(scan, "Bad number")
    assert kept["value"] is None and "elapsed_s" not in _detail(kept)


# ---------------------------------------------------------------- time

def test_the_rollover_hour_puts_a_late_night_session_on_the_day_before(tmp_path, cfg):
    text = STRONG_HEADER + "2026-10-10 01:30:00,Night lift,45m,Squat (Barbell),1,225,5,0,0,,,\n"
    (rec,) = fitness.scan(export(tmp_path, "strong.csv", text), cfg)["records"]
    assert rec["started_at"] == "2026-10-10T06:30:00Z"
    assert rec["journal_date"] == "2026-10-09"
    strava = STRAVA_OLD.replace("2019-03-10 14:12:31", '"Oct 10, 2026, 3:00:00 AM"')
    (run,) = fitness.scan(export(tmp_path / "s", "activities.csv", strava), cfg)["records"]
    assert run["started_at"] == "2026-10-10T03:00:00Z"
    assert run["journal_date"] == "2026-10-09"                  # 22:00 the evening before


def test_wall_clock_times_follow_daylight_saving(tmp_path, cfg):
    text = STRONG_HEADER + (
        "2026-07-01 06:30:00,Summer,1h,Squat (Barbell),1,225,5,0,0,,,\n"
        "2026-12-01 06:30:00,Winter,1h,Squat (Barbell),1,225,5,0,0,,,\n")
    scan = fitness.scan(export(tmp_path, "strong.csv", text), cfg)
    assert _by_title(scan, "Summer")["started_at"] == "2026-07-01T11:30:00Z"
    assert _by_title(scan, "Winter")["started_at"] == "2026-12-01T12:30:00Z"


def test_identity_is_the_files_own_time_so_a_new_timezone_keeps_it(tmp_path, cfg):
    p = export(tmp_path, "strong.csv", STRONG)
    home = fitness.scan(p, cfg)["records"]
    cfg.timezone = "Europe/London"
    away = fitness.scan(p, cfg)["records"]
    assert [r["external_id"] for r in home] == [r["external_id"] for r in away]
    assert [r["started_at"] for r in home] != [r["started_at"] for r in away]


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-09 06:30:00", (2026, 10, 9, 6, 30, 0)),
    ("2026-10-09T06:30", (2026, 10, 9, 6, 30, 0)),
    ("9 Oct 2026, 06:30", (2026, 10, 9, 6, 30, 0)),
    ("24 Sep 2026, 23:15:00", (2026, 9, 24, 23, 15, 0)),
    ("Oct 9, 2026, 6:30:00 AM", (2026, 10, 9, 6, 30, 0)),
    ("Sep 24, 2026, 11:15:00 PM", (2026, 9, 24, 23, 15, 0)),
    ("Sept 24, 2026, 12:05:00 AM", (2026, 9, 24, 0, 5, 0)),
])
def test_times_in_the_forms_the_apps_write(raw, expected):
    dt = fitness.parse_time(raw)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == expected
    assert dt.tzinfo is None


def test_a_time_that_states_its_zone_keeps_it():
    assert fitness.parse_time("2026-10-09 06:30:00Z").utcoffset().total_seconds() == 0
    assert fitness.parse_time("2026-10-09T06:30:00+02:00").utcoffset().total_seconds() == 7200


@pytest.mark.parametrize("raw", ["10/09/2026 06:30", "Foo 9, 2026, 6:30:00 AM",
                                 "2026-10-09 13:30:00 PM", "2026-10-09", "yesterday"])
def test_times_that_would_need_a_guess_are_refused(raw):
    with pytest.raises(ValueError):
        fitness.parse_time(raw)


@pytest.mark.parametrize("raw,bare,expected", [
    ("1h 15m", None, 4500), ("55m", None, 3300), ("1h", None, 3600),
    ("45m 30s", None, 2730), ("1:05:00", None, 3900), ("", None, None),
    ("3600", "s", 3600), ("75", "min", 4500), ("1 hour 5 minutes", None, 3900),
])
def test_durations_in_the_forms_the_apps_write(raw, bare, expected):
    assert fitness.parse_duration(raw, bare) == expected


@pytest.mark.parametrize("raw", ["3600", "soon", "1h banana"])
def test_durations_that_do_not_say_their_unit_raise(raw):
    with pytest.raises(ValueError):
        fitness.parse_duration(raw)


@pytest.mark.parametrize("raw,comma,expected", [
    ("185.0", False, 185.0), ("82,5", False, 82.5), ("1,234.5", False, 1234.5),
    ("1.234,5", False, 1234.5), ("1,234", False, 1234.0), ("1,234", True, 1.234),
    ("", False, None), (" 60 ", False, 60.0),
])
def test_numbers_in_the_forms_the_apps_write(raw, comma, expected):
    assert fitness._num(raw, comma) == expected


@pytest.mark.parametrize("raw", ["two", "nan", "1e400", "5 kg"])
def test_text_that_is_not_a_number_raises(raw):
    with pytest.raises(ValueError):
        fitness._num(raw)


# ---------------------------------------------------------------- handles

def test_no_export_names_a_person_so_no_handles_and_no_participants(tmp_path, cfg):
    for p in (export(tmp_path / "a", "strong.csv", STRONG),
              export(tmp_path / "b", "workouts.csv", HEVY), strava_zip(tmp_path / "c")):
        scan = fitness.scan(p, cfg)
        assert scan["handles"] == []
        assert all(r["participants"] == [] for r in scan["records"])
    # Sam is in the workout's own notes: text, searchable, never a participant
    push = _by_title(fitness.scan(tmp_path / "a" / "strong.csv", cfg), "Push Day")
    assert "Sam" in push["body"]


# ---------------------------------------------------------------- determinism & bad input

def test_scan_is_deterministic(tmp_path, cfg):
    folder = tmp_path / "exports"
    export(folder, "strong.csv", STRONG)
    export(folder, "workouts.csv", HEVY)
    a, b = fitness.scan(folder, cfg), fitness.scan(folder, cfg)
    assert a["records"] == b["records"] and len(a["records"]) == 4
    assert a["source_hash"] == b["source_hash"] and a["source_hash"]
    keys = [(r["stream"], r["started_at"], r["external_id"]) for r in a["records"]]
    assert keys == sorted(keys)
    z1, z2 = fitness.scan(strava_zip(tmp_path / "z1"), cfg), fitness.scan(
        strava_zip(tmp_path / "z2"), cfg)
    assert z1["source_hash"] == z2["source_hash"] and z1["records"] == z2["records"]
    single = export(tmp_path / "one", "strong.csv", STRONG)
    assert fitness.scan(single, cfg)["source_hash"] == base.file_hash(single)


def test_a_folder_reads_every_export_and_counts_what_it_is_not(tmp_path, cfg):
    folder = tmp_path / "exports"
    export(folder, "strong.csv", STRONG)
    export(folder / "hevy", "workouts.csv", HEVY)
    export(folder, "notes.txt", "hello")
    export(folder, "statement.csv", "Date,Description,Amount\n10/01/2026,X,1.00\n")
    export(folder / ".hidden", "strong.csv", STRONG)
    strava_zip(folder)
    scan = fitness.scan(folder, cfg)
    assert sorted(f["app"] for f in scan["files"]) == ["hevy", "strong"]
    assert "1 × file that is not a CSV export (e.g. notes.txt)" in scan["skipped"]
    assert "1 × zip in the folder, not opened — import it on its own " \
           "(export_48213377.zip)" in scan["skipped"]
    assert "1 × CSV that is not a workout export (e.g. statement.csv)" in scan["skipped"]


def test_an_unknown_csv_says_what_it_saw(tmp_path, cfg):
    p = export(tmp_path, "workouts.csv", "Date,Description,Amount\n10/01/2026,X,1.00\n")
    with pytest.raises(DomainError) as e:
        fitness.scan(p, cfg)
    msg = str(e.value)
    assert "not a workout export" in msg and "first row: date, description, amount" in msg
    assert "strong.csv" in msg and "\n" not in msg


@pytest.mark.parametrize("name,raw,needle", [
    ("strong.csv", b"", "strong.csv is empty"),
    ("strong.csv", b"\xef\xbb\xbf  \r\n", "strong.csv is empty"),
    ("strong.csv", STRONG_HEADER.encode(), "Strong CSV with no workout in it (a header "
                                           "and no rows)"),
    ("strong.csv", (STRONG_HEADER + "someday,Push,1h,Bench,1,1,1,0,0,,,\n").encode(),
     "1 row(s), none usable — 1 × row with a date that does not parse"),
    ("strong.csv", b"\x00\x01\x02\xff\xfe\x00binary", "is binary"),
    ("123.gpx", b"<?xml version='1.0'?><gpx></gpx>", "a single activity file is never read"),
])
def test_bad_input_fails_with_one_line_naming_what_it_saw(tmp_path, cfg, name, raw, needle):
    p = tmp_path / name
    p.write_bytes(raw)
    with pytest.raises(DomainError) as e:
        fitness.scan(p, cfg)
    assert needle in str(e.value) and "\n" not in str(e.value)


def test_a_folder_with_no_export_fails_listing_what_it_has(tmp_path, cfg):
    folder = tmp_path / "empty"
    export(folder, "readme.txt", "nothing")
    with pytest.raises(DomainError) as e:
        fitness.scan(folder, cfg)
    assert "holds no Strong, Hevy or Strava export (CSVs: no CSV at all" in str(e.value)


# ---------------------------------------------------------------- personal bests

def test_personal_bests_compare_workouts_in_the_export_by_heaviest_working_set(tmp_path, cfg):
    text = STRONG_HEADER + (
        "2026-09-01 06:30:00,Push,1h,Bench Press (Barbell),1,175,5,0,0,,,\n"
        "2026-09-08 06:30:00,Push,1h,Bench Press (Barbell),W,300,1,0,0,,,\n"
        "2026-09-08 06:30:00,Push,1h,Bench Press (Barbell),1,170,8,0,0,,,\n"
        "2026-09-15 06:30:00,Push,1h,Bench Press (Barbell),1,185,5,0,0,,,\n"
        "2026-09-22 06:30:00,Push,1h,Bench Press (Barbell),1,185,3,0,0,,,\n"
        "2026-09-22 06:30:00,Push,1h,Squat (Barbell),1,225,5,0,0,,,\n")
    scan = fitness.scan(export(tmp_path, "strong.csv", text), cfg)
    # The 300 lb warm-up never counts, 170 × 8 is lighter, 185 × 3 ties on
    # weight with fewer reps, and a lone squat has nothing to beat.
    assert fitness.personal_bests(scan["records"]) == [
        "new best: Bench Press (Barbell) 185 lb × 5 on 2026-09-15, up from 175 lb × 5 "
        "on 2026-09-01"]


def test_personal_bests_compare_across_units(tmp_path, cfg):
    hevy_kg = HEVY
    lbs = HEVY.replace('"weight_kg"', '"weight_lbs"').replace("9 Oct 2026", "2 Oct 2026") \
        .replace("11 Oct 2026", "4 Oct 2026")
    folder = tmp_path / "both"
    export(folder / "old", "workouts.csv", lbs)       # 84 lb bench
    export(folder / "new", "workouts.csv", hevy_kg)   # 84 kg bench
    scan = fitness.scan(folder, cfg)
    assert "new best: Bench Press (Barbell) 84 kg × 5 on 2026-10-09, up from 84 lb × 5 " \
           "on 2026-10-02" in fitness.personal_bests(scan["records"])


# ---------------------------------------------------------------- report

def test_report_lines_show_counts_span_sample_bests_and_the_unit_assumption(tmp_path, cfg, conn):
    text = STRONG + "2026-09-30 06:30:00,Push Day,1h,Bench Press (Barbell),1,180,5,0,0,,,\n"
    scan = fitness.scan(export(tmp_path, "strong.csv", text), cfg)
    report = _report(conn, scan)
    assert report.startswith("fitness: strong.csv (Strong CSV ×1)")
    assert "file strong.csv: Strong CSV, 3 workout(s) 2026-09-30..2026-10-09" in report
    assert ("read weights as lb and distances as mi ([connectors.fitness] weight_unit / "
            "distance_unit — the file does not say") in report
    assert "workout: 3 new, 0 changed | 2026-09-30..2026-10-09" in report
    assert "e.g. 2026-10-09 06:30 Push Day, 70 min — Bench Press (Barbell)" in report
    assert "new best: Bench Press (Barbell) 185 lb × 5 on 2026-10-09, up from 180 lb × 5 " \
           "on 2026-09-30" in report
    assert "skipped: 1 × rest timer row" in report


def test_report_for_hevy_and_strava_names_no_assumption(tmp_path, cfg, conn):
    report = _report(conn, fitness.scan(export(tmp_path, "workouts.csv", HEVY), cfg))
    assert "workout: 2 new" in report and "does not say" not in report
    report = _report(conn, fitness.scan(strava_zip(tmp_path), cfg))
    assert "file activities.csv: Strava activities.csv, 2 workout(s) 2022-09-29..2026-09-24, " \
           "2 activity file(s) referenced, never parsed" in report
    assert "totals across these: 2.5 h, 11.8 km" in report


def test_report_after_a_reimport_says_nothing_is_new(tmp_path, cfg, conn):
    scan = fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg)
    _apply(conn, scan)
    report = _report(conn, fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg))
    assert "nothing new — the export matches the database" in report
    assert "2 identical (skipped)" in report


# ---------------------------------------------------------------- apply

def test_apply_then_reimport_is_all_identical(tmp_path, cfg, conn):
    folder = tmp_path / "exports"
    export(folder, "strong.csv", STRONG)
    export(folder, "workouts.csv", HEVY)
    strava_zip(tmp_path / "s")
    _, first = _apply(conn, fitness.scan(folder, cfg))
    assert first["added"] == 4
    _, strava = _apply(conn, fitness.scan(tmp_path / "s" / "export_48213377.zip", cfg))
    assert strava["added"] == 2
    plan, again = _apply(conn, fitness.scan(folder, cfg))
    assert again["added"] == 0 and again["identical"] == 4
    assert plan["conflicts"] == [] and plan["updates"] == [] and plan["invalid"] == []


def test_a_later_export_wins_and_an_older_one_overwrites_nothing(tmp_path, cfg, conn):
    old = STRONG_HEADER + "2026-10-07 06:30:00,Push,1h,Bench Press (Barbell),1,180,5,0,0,,,\n"
    new = STRONG_HEADER + (
        "2026-10-07 06:30:00,Push,1h,Bench Press (Barbell),1,185,5,0,0,,,\n"   # corrected
        "2026-10-09 06:30:00,Push,1h,Bench Press (Barbell),1,185,5,0,0,,,\n")
    _apply(conn, fitness.scan(export(tmp_path / "a", "strong.csv", old), cfg))
    plan, result = _apply(conn, fitness.scan(export(tmp_path / "b", "strong.csv", new), cfg))
    assert result["added"] == 1 and result["updated"] == 1
    plan = obs.build_plan(conn, fitness.scan(tmp_path / "a" / "strong.csv", cfg))
    assert len(plan["conflicts"]) == 1 and plan["updates"] == []
    body = conn.execute("SELECT body FROM observations WHERE external_id LIKE "
                        "'strong:2026-10-07%'").fetchone()["body"]
    assert "185 lb" in body
    assert "kept existing: 1 workout(s)" in "\n".join(fitness.report_lines(
        fitness.scan(tmp_path / "a" / "strong.csv", cfg), plan))


def test_two_exports_of_one_workout_in_a_folder_keep_the_newest_copy(tmp_path, cfg):
    folder = tmp_path / "exports"
    export(folder / "2026-10-07", "strong.csv", STRONG_HEADER
           + "2026-10-07 06:30:00,Push,1h,Bench Press (Barbell),1,180,5,0,0,,,\n")
    export(folder / "2026-10-09", "strong.csv", STRONG_HEADER
           + "2026-10-07 06:30:00,Push,1h,Bench Press (Barbell),1,185,5,0,0,,,\n"
           + "2026-10-09 06:30:00,Push,1h,Bench Press (Barbell),1,185,5,0,0,,,\n")
    scan = fitness.scan(folder, cfg)
    assert len(scan["records"]) == 2
    assert "185 lb" in scan["records"][0]["body"]
    assert "1 × workout in more than one export (the newest export's copy kept)" \
        in scan["skipped"]


def test_nothing_is_ever_removed_by_a_later_partial_export(tmp_path, cfg, conn):
    _apply(conn, fitness.scan(export(tmp_path / "a", "strong.csv", STRONG), cfg))
    later = fitness.scan(export(tmp_path / "b", "strong.csv", STRONG_HEADER
                                + "2026-10-12 06:30:00,Legs,1h,Squat (Barbell),1,225,5,0,0,,,\n"),
                         cfg)
    assert "present_ids" not in later and "mirror_streams" not in later
    plan, _ = _apply(conn, later)
    assert plan["removed"] == []
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == 3


def test_the_importer_writes_none_of_the_confirmed_tables(tmp_path, cfg, conn):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entity_links",
              "entities", "entry_sources", "extraction_runs", "entity_handles")

    def counts():
        return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                for t in tables}
    before = counts()
    _apply(conn, fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg))
    _apply(conn, fitness.scan(strava_zip(tmp_path / "s"), cfg))
    assert counts() == before


def test_doctor_is_green_after_an_import(tmp_path, cfg, conn):
    _apply(conn, fitness.scan(export(tmp_path, "strong.csv", STRONG), cfg))
    _apply(conn, fitness.scan(strava_zip(tmp_path / "s"), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]
    assert not any(c["check"] == "fitness_data_age" for c in report["checks"])


def test_ask_can_find_the_bench_pr_in_value_json(tmp_path, cfg, conn):
    """The query docs/fitness.md gives for "what's my bench PR"."""
    text = STRONG + "2026-09-30 06:30:00,Push Day,1h,Bench Press (Barbell),1,190,3,0,0,,,\n"
    _apply(conn, fitness.scan(export(tmp_path, "strong.csv", text), cfg))
    row = conn.execute(
        "SELECT o.id, o.journal_date, json_extract(e.value, '$.name') name, "
        "json_extract(e.value, '$.top_set.weight') weight, "
        "json_extract(e.value, '$.top_set.unit') unit, "
        "json_extract(e.value, '$.top_set.reps') reps "
        "FROM observations o, json_each(o.value_json, '$.exercises') e "
        "WHERE o.stream = 'workout' AND o.status = 'active' "
        "AND lower(json_extract(e.value, '$.name')) LIKE '%bench%' "
        "AND json_extract(e.value, '$.top_set.weight_kg') IS NOT NULL "
        "ORDER BY json_extract(e.value, '$.top_set.weight_kg') DESC, "
        "json_extract(e.value, '$.top_set.reps') DESC, o.started_at LIMIT 1").fetchone()
    assert (row["journal_date"], row["weight"], row["unit"], row["reps"]) == \
        ("2026-09-30", 190, "lb", 3)


# ---------------------------------------------------------------- default target

def _no_cloud(monkeypatch, home):
    monkeypatch.setattr(platforms, "home", lambda: home)
    monkeypatch.setattr(platforms, "onedrive_candidates", lambda: [])
    monkeypatch.setattr(platforms, "is_mac", lambda: False)


def test_default_target_is_the_configured_path(tmp_path, cfg):
    folder = tmp_path / "training"
    folder.mkdir()
    _with(cfg, path=str(folder))
    assert fitness.default_target(cfg) == folder
    _with(cfg, path="training")                       # relative to the project root
    assert fitness.default_target(cfg) == cfg.root / "training"
    _with(cfg, path=str(tmp_path / "missing"))
    with pytest.raises(DomainError, match="does not exist"):
        fitness.default_target(cfg)


def test_default_target_finds_the_one_export_in_downloads(tmp_path, cfg, monkeypatch):
    _no_cloud(monkeypatch, tmp_path / "home")
    downloads = tmp_path / "home" / "Downloads"
    with pytest.raises(DomainError) as e:
        fitness.default_target(cfg)
    assert "no workout export found" in str(e.value) and "Downloads" in str(e.value)
    strong = export(downloads, "strong.csv", STRONG)
    assert fitness.default_target(cfg) == strong
    strava_zip(downloads)
    with pytest.raises(DomainError) as e:
        fitness.default_target(cfg)
    assert "found several workout exports" in str(e.value)
    assert "export_48213377.zip" in str(e.value)


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch, extra=""):
    root = cli_project(tmp_path, monkeypatch)
    if extra:
        with open(root / "config.toml", "a", encoding="utf-8") as f:
            f.write(extra)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def _count(where=""):
    r = runner.invoke(app, ["sql", f"SELECT COUNT(*) c FROM observations {where}", "--json"])
    return json.loads(r.output)[0]["c"]


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    p = export(tmp_path / "exp", "strong.csv", STRONG)
    r = runner.invoke(app, ["observe", "fitness", str(p)])
    assert r.exit_code == 0, r.output
    assert "workout: 2 new" in r.output
    assert "dry run — nothing written" in r.output
    assert _count() == 0


def test_cli_apply_then_reimport_is_a_no_op(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    p = strava_zip(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "fitness", str(p), "--apply"])
    assert r.exit_code == 0, r.output
    assert "2 added" in r.output
    again = runner.invoke(app, ["observe", "fitness", str(p), "--apply"])
    assert "0 added" in again.output and "2 identical skipped" in again.output
    assert _count("WHERE status='active' AND stream='workout'") == 2
    note = runner.invoke(app, ["observe", "fitness", str(p)])
    assert "already imported" in note.output


def test_cli_reads_the_configured_path_and_unit(tmp_path, monkeypatch):
    p = export(tmp_path / "exp", "strong.csv", STRONG)
    _cli_setup(tmp_path, monkeypatch,
               f"[connectors.fitness]\npath = '{p.as_posix()}'\nweight_unit = \"kg\"\n")
    r = runner.invoke(app, ["observe", "fitness", "--apply"])
    assert r.exit_code == 0, r.output
    assert "read weights as kg" in r.output and "2 added" in r.output
    out = runner.invoke(app, ["sql", "SELECT value_json FROM observations WHERE "
                                     "title='Push Day'", "--json"])
    detail = json.loads(json.loads(out.output)[0]["value_json"])
    assert detail["exercises"][0]["top_set"]["unit"] == "kg"


def test_cli_bad_export_fails_with_its_one_line(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    p = export(tmp_path / "exp", "strong.csv", "Date,Description,Amount\n1/1/2026,X,1\n")
    r = runner.invoke(app, ["observe", "fitness", str(p)])
    assert r.exit_code == 1
    assert "not a workout export this connector knows" in r.output

"""Nutrition connector: Cronometer (and MyFitnessPal) exports.

The connector is a pure reader, so most of this runs scan() against small
exports shaped like the apps' real downloads — Cronometer's servings.csv
and dailysummary.csv, MyFitnessPal's Nutrition Summary — and checks what a
food diary import can get quietly wrong: a unit that is converted when it
must not be (vitamin A in IU), a blank read as zero, a 00:40 snack moved to
the previous day, a half-logged day that can never be completed by a later
export, and a column the table does not know silently dropped.
"""

import json
import zipfile
from datetime import datetime, timezone

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import base, nutrition
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError

runner = CliRunner()

SERVINGS = (
    "Day,Time,Group,Food Name,Amount,Energy (kcal),Caffeine (mg),Vitamin D (IU),"
    "Vitamin A (IU),Fluoride (µg),Magnesium (mg),Sodium (mg),Fiber (g),"
    "Protein (g),Beta-carotene (µg),Category\n"
    '2026-10-20,8:05 AM,Breakfast,"Greek Yogurt, Plain, Nonfat",1.50 container,'
    "150.00,,0.00,,,28.50,91.50,0.00,25.95,,Dairy and Egg Products\n"
    '2026-10-20,8:05 AM,Breakfast,"Coffee, Brewed",1.00 cup,2.40,95.00,,,,7.10,'
    "4.70,,0.30,,Beverages\n"
    '2026-10-20,19:40,Dinner,"Salmon, Atlantic, Farmed, Baked",5.00 oz,291.00,,'
    "745.00,150.00,40.00,43.00,86.00,,31.40,12.00,Finfish and Shellfish Products\n"
    "2026-10-20,,Snacks,Black Beans,0.50 cup,109.00,,,,,42.00,230.00,8.30,7.20,,"
    "Legumes and Legume Products\n"
    "2026-10-21,00:40,Snacks,Tortilla Chips,1.00 oz,140.00,,,,,16.00,120.00,1.50,"
    "2.00,,Snacks\n")

DAILY = (
    "Date,Energy (kcal),Magnesium (mg),Sodium (mg),Vitamin D (IU),Fiber (g),"
    "Protein (g),Completed\n"
    "2026-10-20,552.40,120.60,412.20,745.00,8.30,64.85,true\n"
    "2026-10-21,140.00,16.00,120.00,,1.50,2.00,false\n")

MFP = (
    "Date,Meal,Time,Calories,Fat (g),Saturated Fat,Polyunsaturated Fat,"
    "Monounsaturated Fat,Trans Fat,Cholesterol,Sodium (mg),Potassium,"
    "Carbohydrates (g),Fiber,Sugar,Protein (g),Vitamin A,Vitamin C,Calcium,Iron,Note\n"
    "2026-10-20,Breakfast,8:05 AM,450.0,12.0,4.0,2.0,5.0,0.0,210.0,520.0,610.0,"
    "48.0,6.0,14.0,32.0,10,25,30,15,\n"
    "2026-10-20,Dinner,,780.5,30.0,9.0,6.0,12.0,0.5,95.0,1850.0,900.0,70.0,9.0,"
    "6.0,45.0,20,40,15,20,tacos with Jake\n")

EXERCISES = ("Day,Time,Group,Exercise,Minutes,Calories Burned\n"
             "2026-10-20,6:30 AM,,Weight Training,60.00,450.00\n"
             "2026-10-21,6:30 AM,,Running,30.00,320.00\n")
BIOMETRICS = ("Day,Time,Group,Metric,Unit,Amount\n"
              "2026-10-20,7:10 AM,,Weight,kg,78.40\n")
NOTES = "Day,Group,Note\n2026-10-20,,Felt great after the lift\n"


def export(folder, name, text, encoding="utf-8", bom=False):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode(encoding))
    return p


def _detail(rec):
    return json.loads(rec["value_json"])


def _food(scan, title):
    return next(r for r in scan["records"]
                if r["stream"] == "food_log" and r["title"] == title)


def _day(scan, day):
    return next(r for r in scan["records"]
                if r["stream"] == "nutrition_day" and r["journal_date"] == day)


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


# ---------------------------------------------------------------- servings

def test_a_serving_maps_to_one_food_log_record(tmp_path, cfg):
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS), cfg)
    assert scan["connector"] == "nutrition"
    assert [r["stream"] for r in scan["records"]] == ["food_log"] * 5
    yogurt = _food(scan, "Greek Yogurt, Plain, Nonfat")
    assert yogurt["body"] == "1.50 container · Breakfast"
    assert yogurt["value"] == 150.0 and yogurt["unit"] == "kcal"
    assert yogurt["origin_app"] == "cronometer"
    assert yogurt["external_id"].startswith("cronometer:serving:")
    assert yogurt["direction"] is None and yogurt["participants"] == []
    detail = _detail(yogurt)
    assert detail["meal"] == "Breakfast" and detail["amount"] == "1.50 container"
    assert detail["category"] == "Dairy and Egg Products"
    assert detail["time"] == "08:05"
    assert detail["nutrients"]["magnesium"] == [28.5, "mg"]
    assert detail["nutrients"]["protein"] == [25.95, "g"]
    assert detail["nutrients"]["fiber"] == [0.0, "g"]          # a written zero stays


def test_blank_cells_are_unknown_not_zero(tmp_path, cfg):
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS), cfg)
    coffee = _detail(_food(scan, "Coffee, Brewed"))["nutrients"]
    assert "fiber" not in coffee and "vitamin_d" not in coffee
    assert coffee["caffeine"] == [95.0, "mg"]


def test_a_daily_summary_maps_to_one_nutrition_day(tmp_path, cfg):
    scan = nutrition.scan(export(tmp_path, "dailysummary.csv", DAILY), cfg)
    day = _day(scan, "2026-10-20")
    assert day["external_id"] == "cronometer:day:2026-10-20"
    assert day["title"] == "Nutrition 2026-10-20"
    assert day["value"] == 552.4 and day["unit"] == "kcal"
    detail = _detail(day)
    assert detail["completed"] is True
    assert detail["nutrients"]["vitamin_d"] == [18.625, "µg"]
    assert _detail(_day(scan, "2026-10-21"))["completed"] is False


# ---------------------------------------------------------------- nutrient keys

@pytest.mark.parametrize("header, key, unit, factor", [
    ("Energy (kcal)", "energy", "kcal", 1.0),
    ("Energy (kJ)", "energy", "kcal", 1 / 4.184),
    ("B1 (Thiamine) (mg)", "thiamin", "mg", 1.0),
    ("B12 (Cobalamin) (µg)", "vitamin_b12", "µg", 1.0),
    ("Vitamin D (IU)", "vitamin_d", "µg", 0.025),
    ("Vitamin D (mcg)", "vitamin_d", "µg", 1.0),
    ("Fluoride (µg)", "fluoride", "mg", 0.001),
    ("Folate (μg)", "folate", "µg", 1.0),
    ("Trans-Fats (g)", "trans_fat", "g", 1.0),
    ("Carbs (g)", "carbohydrate", "g", 1.0),
])
def test_known_columns_get_one_key_and_one_unit(header, key, unit, factor):
    col = nutrition.nutrient_column(header)
    assert (col.key, col.unit, col.known) == (key, unit, True)
    assert col.factor == pytest.approx(factor)


def test_vitamin_a_and_e_in_iu_stay_in_iu_under_their_own_key():
    """The IU factor depends on the form (retinol or carotene, natural or
    synthetic tocopherol), which the export does not say — so no factor."""
    for header, key in (("Vitamin A (IU)", "vitamin_a_iu"), ("Vitamin E (IU)", "vitamin_e_iu")):
        col = nutrition.nutrient_column(header)
        assert (col.key, col.unit, col.factor) == (key, "IU", 1.0)


def test_unknown_and_unitless_columns_are_kept_as_is():
    odd = nutrition.nutrient_column("Oxalate (mg)")
    assert (odd.key, odd.unit, odd.known) == ("Oxalate (mg)", "mg", False)
    bare = nutrition.nutrient_column("Vitamin C", nutrition.MFP_UNITS)
    assert (bare.key, bare.unit, bare.known) == ("Vitamin C", None, False)
    pct = nutrition.nutrient_column("Iron (%DV)")
    assert (pct.key, pct.unit, pct.known) == ("Iron (%DV)", "%DV", False)


def test_conversions_and_kept_columns_are_applied_and_reported(tmp_path, cfg, conn):
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS), cfg)
    salmon = _detail(_food(scan, "Salmon, Atlantic, Farmed, Baked"))["nutrients"]
    assert salmon["vitamin_d"] == [18.625, "µg"]           # 745 IU / 40
    assert salmon["vitamin_a_iu"] == [150.0, "IU"]
    assert salmon["fluoride"] == [0.04, "mg"]                    # 40 µg
    assert salmon["beta_carotene"] == [12.0, "µg"]
    facts = scan["facts"]
    assert facts["kept_as_is"] == []
    assert "Vitamin A (IU) -> vitamin_a_iu" in facts["in_iu"]
    text = "\n".join(nutrition.report_lines(scan, obs.build_plan(conn, scan)))
    assert "converted: Vitamin D (IU) -> vitamin_d in µg" in text
    assert "kept in IU (no single conversion to µg/mg): Vitamin A (IU) -> vitamin_a_iu" in text


def test_a_column_the_table_does_not_know_is_kept_under_its_header(tmp_path, cfg, conn):
    text = SERVINGS.replace("Beta-carotene (µg)", "Phytosterol Mix (mg)")
    scan = nutrition.scan(export(tmp_path, "servings.csv", text), cfg)
    salmon = _detail(_food(scan, "Salmon, Atlantic, Farmed, Baked"))["nutrients"]
    assert salmon["Phytosterol Mix (mg)"] == [12.0, "mg"]
    assert scan["facts"]["kept_as_is"] == ["Phytosterol Mix (mg)"]
    report = "\n".join(nutrition.report_lines(scan, obs.build_plan(conn, scan)))
    assert "kept as-is: 1 column(s) not in the nutrient table, under their own " \
           "header — Phytosterol Mix (mg)" in report


def test_two_columns_for_one_key_keep_the_second_under_its_header(tmp_path, cfg):
    text = SERVINGS.replace("Beta-carotene (µg)", "Vitamin D (µg)")
    scan = nutrition.scan(export(tmp_path, "servings.csv", text), cfg)
    salmon = _detail(_food(scan, "Salmon, Atlantic, Farmed, Baked"))["nutrients"]
    assert salmon["vitamin_d"] == [18.625, "µg"]           # the first column
    assert salmon["Vitamin D (µg)"] == [12.0, "µg"]


def test_a_cell_that_is_not_a_number_is_kept_as_text_and_counted(tmp_path, cfg):
    text = SERVINGS.replace("43.00,86.00", "n/a,86.00")
    scan = nutrition.scan(export(tmp_path, "servings.csv", text), cfg)
    salmon = _detail(_food(scan, "Salmon, Atlantic, Farmed, Baked"))
    assert "magnesium" not in salmon["nutrients"]
    assert salmon["other"] == {"Magnesium (mg)": "n/a"}
    (u,) = scan["unmapped"]
    assert u["count"] == 1 and "not a number" in u["what"]
    assert "Magnesium (mg)=n/a" in u["example"]


# ---------------------------------------------------------------- time

def test_times_are_read_in_the_home_timezone(tmp_path, cfg):
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS), cfg)
    assert _food(scan, "Coffee, Brewed")["started_at"] == "2026-10-20T13:05:00Z"    # CDT
    assert _food(scan, "Salmon, Atlantic, Farmed, Baked")["started_at"] == \
        "2026-10-21T00:40:00Z"
    winter = SERVINGS.replace("2026-10-20,8:05 AM", "2026-12-01,8:05 AM")
    w = nutrition.scan(export(tmp_path / "w", "servings.csv", winter), cfg)
    assert _food(w, "Coffee, Brewed")["started_at"] == "2026-12-01T14:05:00Z"       # CST


def test_a_serving_without_a_time_sits_at_local_noon_and_says_so(tmp_path, cfg):
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS), cfg)
    beans = _food(scan, "Black Beans")
    assert beans["started_at"] == "2026-10-20T17:00:00Z"
    assert "time" not in _detail(beans)


def test_the_diary_day_is_kept_and_the_rollover_not_applied_again(tmp_path, cfg):
    """00:40 is before the 05:00 rollover, but Cronometer filed the chips under
    the 21st — and that day's summary counts them. The diary's day stands."""
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS), cfg)
    chips = _food(scan, "Tortilla Chips")
    assert chips["started_at"] == "2026-10-21T05:40:00Z"
    assert base.journal_date_for_iso(chips["started_at"], cfg) == "2026-10-20"
    assert chips["journal_date"] == "2026-10-21"


@pytest.mark.parametrize("raw, expected", [
    ("08:05", (8, 5)), ("8:05 AM", (8, 5)), ("12:10 AM", (0, 10)), ("12:10 PM", (12, 10)),
    ("7:40 pm", (19, 40)), ("19:40:30", (19, 40)), ("8:05 p.m.", (20, 5)),
])
def test_times_in_the_shapes_the_apps_write(raw, expected):
    t = nutrition.parse_time(raw)
    assert (t.hour, t.minute) == expected


def test_a_time_that_does_not_parse_is_placed_at_noon_and_counted(tmp_path, cfg):
    text = SERVINGS.replace("2026-10-20,19:40,Dinner", "2026-10-20,dinnertime,Dinner")
    scan = nutrition.scan(export(tmp_path, "servings.csv", text), cfg)
    assert _food(scan, "Salmon, Atlantic, Farmed, Baked")["started_at"] == \
        "2026-10-20T17:00:00Z"
    assert any("time that does not parse" in u["what"] for u in scan["unmapped"])


def test_rows_without_a_day_or_a_food_are_reported_not_imported(tmp_path, cfg):
    text = SERVINGS + "not-a-day,8:00 AM,Breakfast,Toast,1 slice,80.00,,,,,,,,,,\n" \
                      "2026-10-20,8:00 AM,Breakfast,,1 slice,80.00,,,,,,,,,,\n"
    scan = nutrition.scan(export(tmp_path, "servings.csv", text), cfg)
    assert len(scan["records"]) == 5
    whats = {u["what"]: u["count"] for u in scan["unmapped"]}
    assert whats == {"serving with no readable day": 1, "serving with no food name": 1}


# ---------------------------------------------------------------- identity

def test_overlapping_exports_give_the_same_rows_the_same_ids(tmp_path, cfg):
    a = nutrition.scan(export(tmp_path / "a", "servings.csv", SERVINGS), cfg)
    rows = SERVINGS.splitlines(keepends=True)
    b = nutrition.scan(export(tmp_path / "b", "servings.csv",
                              rows[0] + "".join(reversed(rows[2:]))), cfg)
    ids_a = {r["title"]: r["external_id"] for r in a["records"]}
    ids_b = {r["title"]: r["external_id"] for r in b["records"]}
    assert all(ids_b[t] == ids_a[t] for t in ids_b) and len(ids_b) == 4


def test_the_same_food_logged_twice_stays_two_servings(tmp_path, cfg):
    row = SERVINGS.splitlines(keepends=True)[2]
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS + row), cfg)
    coffees = [r for r in scan["records"] if r["title"] == "Coffee, Brewed"]
    assert len(coffees) == 2 and coffees[0]["external_id"] != coffees[1]["external_id"]


# ---------------------------------------------------------------- MyFitnessPal

def test_a_myfitnesspal_meal_row_is_a_meal_total(tmp_path, cfg):
    scan = nutrition.scan(export(tmp_path, "Nutrition-Summary-2026-10-20-to-2026-10-20.csv",
                                 MFP), cfg)
    assert [r["stream"] for r in scan["records"]] == ["food_log", "food_log"]
    breakfast = _food(scan, "Breakfast (meal total)")
    assert breakfast["origin_app"] == "myfitnesspal"
    assert breakfast["external_id"] == "myfitnesspal:meal:2026-10-20:breakfast:0"
    assert breakfast["value"] == 450.0 and breakfast["started_at"] == "2026-10-20T13:05:00Z"
    detail = _detail(breakfast)
    assert detail["aggregate"] == "meal" and detail["meal"] == "Breakfast"
    n = detail["nutrients"]
    assert n["sodium"] == [520.0, "mg"] and n["potassium"] == [610.0, "mg"]
    assert n["saturated_fat"] == [4.0, "g"] and n["cholesterol"] == [210.0, "mg"]
    # MyFitnessPal's micronutrient columns name no unit: kept as-is, never guessed
    assert n["Vitamin C"] == [25.0, None] and n["Iron"] == [15.0, None]
    dinner = _food(scan, "Dinner (meal total)")
    assert dinner["body"] == "MyFitnessPal meal total · tacos with Jake"
    assert scan["handles"] == []


def test_a_myfitnesspal_zip_reads_the_nutrition_summary_and_skips_the_rest(tmp_path, cfg):
    z = tmp_path / "File-Export-2026-10-20-to-2026-10-20.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Nutrition-Summary-2026-10-20-to-2026-10-20.csv", MFP)
        zf.writestr("Exercise-Summary-2026-10-20-to-2026-10-20.csv",
                    "Date,Exercise,Type,Exercise Calories,Exercise Minutes,Sets,"
                    "Reps Per Set,Kilograms,Steps,Note\n2026-10-20,Running,Cardio,320,30,,,,,\n")
        zf.writestr("Measurement-Summary-2026-10-20-to-2026-10-20.csv",
                    "Date,Weight\n2026-10-20,172.8\n")
    scan = nutrition.scan(z, cfg)
    assert len(scan["records"]) == 2
    assert any("MyFitnessPal exercise summary row" in s for s in scan["skipped"])
    assert any("MyFitnessPal measurement summary row" in s for s in scan["skipped"])


# ---------------------------------------------------------------- targets

def test_a_folder_reads_both_files_and_counts_what_is_not_food(tmp_path, cfg):
    folder = tmp_path / "cronometer"
    export(folder, "servings.csv", SERVINGS)
    export(folder, "dailysummary.csv", DAILY)
    export(folder, "exercises.csv", EXERCISES)
    export(folder, "biometrics.csv", BIOMETRICS)
    export(folder, "notes.csv", NOTES)
    export(folder, "readme.txt", "hello")
    scan = nutrition.scan(folder, cfg)
    streams = {r["stream"] for r in scan["records"]}
    assert streams == {"food_log", "nutrition_day"} and len(scan["records"]) == 7
    assert sorted(scan["skipped"]) == [
        "1 × Cronometer biometrics row (biometrics.csv) — not food, not imported",
        "1 × Cronometer notes row (notes.csv) — not food, not imported",
        "1 × file that is not a .csv export (e.g. readme.txt)",
        "2 × Cronometer exercise log row (exercises.csv) — not food, not imported"]
    assert scan["target_kind"] == "folder"


def test_two_exports_of_one_kind_are_refused(tmp_path, cfg):
    folder = tmp_path / "downloads"
    export(folder, "servings.csv", SERVINGS)
    export(folder, "servings (1).csv", SERVINGS)
    with pytest.raises(DomainError) as e:
        nutrition.scan(folder, cfg)
    msg = str(e.value)
    assert "2 Cronometer servings files" in msg and "one at a time" in msg
    assert "\n" not in msg


def test_a_file_that_is_not_food_says_what_it_is(tmp_path, cfg):
    with pytest.raises(DomainError) as e:
        nutrition.scan(export(tmp_path, "exercises.csv", EXERCISES), cfg)
    assert "is a Cronometer exercise log, not food" in str(e.value)


def test_an_unknown_header_says_what_it_saw(tmp_path, cfg):
    with pytest.raises(DomainError) as e:
        nutrition.scan(export(tmp_path, "x.csv", "Posting Date,Description,Amount\n"
                                                 "10/20/2026,VERACRUZ,-29.75\n"), cfg)
    msg = str(e.value)
    assert "not a Cronometer or MyFitnessPal export" in msg
    assert "first row: Posting Date, Description, Amount" in msg and "\n" not in msg


def test_an_empty_file_says_so(tmp_path, cfg):
    with pytest.raises(DomainError) as e:
        nutrition.scan(export(tmp_path, "servings.csv", "\n\n"), cfg)
    assert "servings.csv is empty" in str(e.value)


def test_a_folder_with_no_food_export_names_what_it_holds(tmp_path, cfg):
    folder = tmp_path / "f"
    export(folder, "exercises.csv", EXERCISES)
    with pytest.raises(DomainError) as e:
        nutrition.scan(folder, cfg)
    assert "holds no food export — has: exercises.csv (Cronometer exercise log)" \
        in str(e.value)


def test_utf8_bom_export_is_accepted(tmp_path, cfg):
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS, bom=True), cfg)
    assert len(scan["records"]) == 5
    assert _detail(_food(scan, "Coffee, Brewed"))["nutrients"]["energy"] == [2.4, "kcal"]


def test_a_windows_1252_export_is_accepted(tmp_path, cfg):
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS, encoding="cp1252"),
                          cfg)
    assert scan["files"][0]["encoding"] == "windows-1252"
    assert _detail(_food(scan, "Salmon, Atlantic, Farmed, Baked"))["nutrients"][
        "fluoride"] == [0.04, "mg"]


def test_scan_is_deterministic(tmp_path, cfg):
    folder = tmp_path / "c"
    export(folder, "servings.csv", SERVINGS)
    export(folder, "dailysummary.csv", DAILY)
    a, b = nutrition.scan(folder, cfg), nutrition.scan(folder, cfg)
    assert a == b and a["source_hash"]
    one = export(tmp_path, "servings.csv", SERVINGS)
    assert nutrition.scan(one, cfg)["source_hash"] == base.file_hash(one)


def test_default_target_is_the_configured_path(tmp_path, cfg):
    folder = tmp_path / "food"
    export(folder, "servings.csv", SERVINGS)
    cfg.connectors = {"nutrition": {"path": str(folder)}}
    assert nutrition.default_target(cfg) == folder
    cfg.connectors = {"nutrition": {"path": str(tmp_path / "missing")}}
    with pytest.raises(DomainError) as e:
        nutrition.default_target(cfg)
    assert "path does not exist" in str(e.value)
    cfg.connectors = {}
    with pytest.raises(DomainError) as e:
        nutrition.default_target(cfg)
    assert "nutrition needs a path" in str(e.value)


def test_the_connector_is_discovered():
    assert base.registry()["nutrition"] is nutrition
    assert nutrition.streams == ("food_log", "nutrition_day")


# ---------------------------------------------------------------- report

def test_report_lines_show_counts_spans_and_a_sample(tmp_path, cfg, conn):
    folder = tmp_path / "c"
    export(folder, "servings.csv", SERVINGS)
    export(folder, "dailysummary.csv", DAILY)
    scan = nutrition.scan(folder, cfg)
    text = "\n".join(nutrition.report_lines(scan, obs.build_plan(conn, scan)))
    assert "food_log: 5 new, 0 changed | 2026-10-20..2026-10-21" in text
    assert "e.g. 2026-10-21 00:40 Tortilla Chips, 1.00 oz · Snacks, 140 kcal" in text
    assert "nutrition_day: 2 new, 0 changed | 2026-10-20..2026-10-21" in text
    assert "e.g. 2026-10-21, 140 kcal, not marked completed" in text
    assert "file servings.csv: Cronometer servings, 5 row(s) 2026-10-20..2026-10-21" in text
    assert "then: lifecrm nutrients" in text


def test_report_after_a_reimport_says_nothing_is_new(tmp_path, cfg, conn):
    scan = nutrition.scan(export(tmp_path, "servings.csv", SERVINGS), cfg)
    _apply(conn, scan)
    text = "\n".join(nutrition.report_lines(scan, obs.build_plan(conn, scan)))
    assert "nothing new — the export matches the database" in text
    assert "5 identical (skipped)" in text


# ---------------------------------------------------------------- apply

def test_apply_then_reimport_is_all_identical(tmp_path, cfg, conn):
    folder = tmp_path / "c"
    export(folder, "servings.csv", SERVINGS)
    export(folder, "dailysummary.csv", DAILY)
    _, first = _apply(conn, nutrition.scan(folder, cfg))
    assert first["added"] == 7
    plan, again = _apply(conn, nutrition.scan(folder, cfg))
    assert again["added"] == 0 and again["identical"] == 7
    assert plan["conflicts"] == [] and plan["updates"] == []


def test_the_importer_writes_none_of_the_confirmed_tables(tmp_path, cfg, conn):
    tables = ("entries", "measurements", "mentions", "entity_aliases", "entity_links",
              "entities", "entry_sources", "health_records")

    def counts():
        return {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                for t in tables}
    before = counts()
    folder = tmp_path / "c"
    export(folder, "servings.csv", SERVINGS)
    export(folder, "dailysummary.csv", DAILY)
    _apply(conn, nutrition.scan(folder, cfg))
    assert counts() == before
    assert conn.execute("SELECT COUNT(*) c FROM entity_handles").fetchone()["c"] == 0


def test_doctor_is_green_after_an_import(tmp_path, cfg, conn):
    _apply(conn, nutrition.scan(export(tmp_path, "dailysummary.csv", DAILY), cfg))
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


def test_a_later_export_completes_a_half_logged_day(tmp_path, cfg, conn):
    """Today's summary is imported at noon; tomorrow's export holds the whole
    day. The later export is newer, so it updates the row (audited)."""
    _apply(conn, nutrition.scan(export(tmp_path / "a", "dailysummary.csv", DAILY), cfg))
    later = DAILY.replace("2026-10-21,140.00,16.00,120.00,,1.50,2.00,false",
                          "2026-10-21,2410.00,265.00,4220.00,80.00,24.00,140.00,true") \
        + "2026-10-22,300.00,40.00,250.00,,3.00,25.00,false\n"
    plan, result = _apply(conn, nutrition.scan(export(tmp_path / "b", "dailysummary.csv",
                                                      later), cfg))
    assert result["added"] == 1 and result["updated"] == 1 and plan["conflicts"] == []
    row = conn.execute("SELECT value, value_json FROM observations "
                       "WHERE external_id='cronometer:day:2026-10-21'").fetchone()
    assert row["value"] == 2410.0 and json.loads(row["value_json"])["completed"] is True


def test_an_older_export_overwrites_nothing(tmp_path, cfg, conn):
    newer = DAILY + "2026-10-22,300.00,40.00,250.00,,3.00,25.00,false\n"
    _apply(conn, nutrition.scan(export(tmp_path / "a", "dailysummary.csv", newer), cfg))
    edited = DAILY.replace("552.40", "600.00")
    plan, result = _apply(conn, nutrition.scan(export(tmp_path / "b", "dailysummary.csv",
                                                      edited), cfg))
    assert result["updated"] == 0 and len(plan["conflicts"]) == 1
    text = "\n".join(nutrition.report_lines(
        nutrition.scan(tmp_path / "b" / "dailysummary.csv", cfg), plan))
    assert "kept existing: 1 record(s)" in text


def test_nothing_is_ever_removed_by_a_later_export(tmp_path, cfg, conn):
    """Exports are date ranges: last month's days are absent from this month's."""
    _apply(conn, nutrition.scan(export(tmp_path / "a", "dailysummary.csv", DAILY), cfg))
    later = nutrition.scan(export(tmp_path / "b", "dailysummary.csv",
                                  DAILY.splitlines()[0] + "\n"
                                  "2026-10-25,2100.00,250.00,2000.00,,25.00,120.00,true\n"),
                           cfg)
    assert "present_ids" not in later and "mirror_streams" not in later
    plan, _ = _apply(conn, later)
    assert plan["removed"] == []
    assert conn.execute("SELECT COUNT(*) c FROM observations WHERE status='active'"
                        ).fetchone()["c"] == 3


# ---------------------------------------------------------------- staleness

def _status(report, check):
    return next((c["status"] for c in report["checks"] if c["check"] == check), None)


def test_doctor_warns_when_the_newest_food_record_is_old(tmp_path, cfg, conn):
    _apply(conn, nutrition.scan(export(tmp_path, "dailysummary.csv",
                                       DAILY.replace("2026-10", "2024-10")), cfg))
    report = run_checks(conn, cfg)
    assert _status(report, "nutrition_data_age") == "warn"
    assert report["failures"] == 0


def test_doctor_is_quiet_when_the_diary_is_current(tmp_path, cfg, conn):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = DAILY.splitlines()[0] + f"\n{today},2000.00,250.00,2000.00,,25.00,120.00,true\n"
    _apply(conn, nutrition.scan(export(tmp_path, "dailysummary.csv", text), cfg))
    assert _status(run_checks(conn, cfg), "nutrition_data_age") == "ok"


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
    p = export(tmp_path / "exp", "servings.csv", SERVINGS)
    r = runner.invoke(app, ["observe", "nutrition", str(p)])
    assert r.exit_code == 0, r.output
    assert "food_log: 5 new" in r.output
    assert "dry run — nothing written" in r.output
    assert _count() == 0


def test_cli_apply_then_reimport_is_a_no_op(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    p = export(tmp_path / "exp", "dailysummary.csv", DAILY)
    r = runner.invoke(app, ["observe", "nutrition", str(p), "--apply"])
    assert r.exit_code == 0, r.output
    assert "2 added" in r.output
    again = runner.invoke(app, ["observe", "nutrition", str(p), "--apply"])
    assert "0 added" in again.output and "2 identical skipped" in again.output
    assert _count("WHERE status='active'") == 2


def test_cli_reads_the_configured_path(tmp_path, monkeypatch):
    folder = tmp_path / "cronometer"
    export(folder, "servings.csv", SERVINGS)
    export(folder, "dailysummary.csv", DAILY)
    _cli_setup(tmp_path, monkeypatch,
               f"[connectors.nutrition]\npath = '{folder.as_posix()}'\n")
    r = runner.invoke(app, ["observe", "nutrition", "--apply"])
    assert r.exit_code == 0, r.output
    assert "7 added" in r.output


def test_cli_without_a_path_or_setting_says_how_to_give_one(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["observe", "nutrition"])
    assert r.exit_code == 1
    assert "nutrition needs a path" in r.output

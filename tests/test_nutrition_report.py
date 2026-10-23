"""`lifecrm nutrients`: logged intake set beside the reference intakes.

The report is arithmetic over cited rows, so these tests pin down the parts
that would mislead if they were wrong: which reference value applies to
whom, where the 70% and 100% lines fall, that an upper level written for
supplements is never held against food, that every average names the rows
it came from, which days are left out (a half-logged one, today), and that
nothing in the text ever advises.
"""

import json
import re
from datetime import date

import pytest
from conftest import cli_project
from typer.testing import CliRunner

from lifecrm import db as db_mod, nutrients
from lifecrm.cli import app
from lifecrm.connectors import nutrition
from lifecrm.domain import observations as obs
from lifecrm.domain.util import DomainError, canonical_json

runner = CliRunner()

TODAY = date(2026, 10, 21)
DAILY_HEADER = ("Date,Energy (kcal),Magnesium (mg),Vitamin D (IU),Fiber (g),"
                "Sodium (mg),Iron (mg),Protein (g),Niacin (mg),Completed\n")
SERVINGS_HEADER = ("Day,Time,Group,Food Name,Amount,Energy (kcal),Magnesium (mg),"
                   "Vitamin D (IU),Fiber (g),Sodium (mg)\n")


def _daily(rows) -> str:
    """rows: (date, kcal, magnesium, vitamin D IU, fiber, sodium, iron, protein,
    niacin, completed)."""
    return DAILY_HEADER + "".join(",".join("" if v is None else str(v) for v in r) + "\n"
                                  for r in rows)


def _import(conn, cfg, folder, name, text):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_text(text, encoding="utf-8")
    scan = nutrition.scan(p, cfg)
    plan = obs.build_plan(conn, scan)
    obs.apply_plan(conn, scan, plan)
    conn.commit()
    return scan


def _report(conn, cfg, days=30, start=None, end=None, **prof):
    window = nutrients.resolve_window(cfg, days=None if start else days, start=start,
                                      end=end, today=TODAY)
    return nutrients.build_report(conn, cfg, window, prof=nutrients.profile(cfg, **prof))


def _item(report, key):
    return next(x for x in report["nutrients"] if x["key"] == key)


def _ids(conn, stream, *dates):
    marks = ",".join("?" for _ in dates)
    return sorted(r["id"] for r in conn.execute(
        f"SELECT id FROM observations WHERE stream=? AND journal_date IN ({marks})",
        (stream, *dates)))


# Two complete days that land in every band: magnesium low, fiber borderline,
# protein meets, iron high (above its 45 mg UL), sodium over on one day only.
TWO_DAYS = _daily([
    ("2026-10-19", 2200, 250, 160, 28, 2600, 50, 120, 20, "true"),
    ("2026-10-20", 2300, 254, 200, 30, 1900, 52, 130, 22, "true"),
])


# ---------------------------------------------------------------- references

@pytest.mark.parametrize("key, sex, age, value, upper", [
    ("magnesium", "male", 25, 400, None),
    ("magnesium", "male", 40, 420, None),
    ("magnesium", "female", 25, 310, None),
    ("magnesium", "female", 40, 320, None),
    ("magnesium", None, None, 420, None),
    ("iron", "female", 30, 18, 45),
    ("iron", "female", 55, 8, 45),
    ("iron", "male", 30, 8, 45),
    ("iron", None, None, 18, 45),
    ("calcium", "female", 60, 1200, 2000),
    ("calcium", "male", 60, 1000, 2000),
    ("calcium", "male", 75, 1200, 2000),
    ("calcium", None, None, 1000, 2500),
    ("vitamin_d", "male", 40, 15, 100),
    ("vitamin_d", "female", 75, 20, 100),
    ("fiber", "male", 40, 38, None),
    ("fiber", "male", 60, 30, None),
    ("fiber", "female", 40, 25, None),
    ("fiber", "female", 60, 21, None),
    ("phosphorus", "male", 75, 700, 3000),
    ("sodium", "female", 30, 1500, 2300),
])
def test_reference_values_follow_sex_and_age(cfg, key, sex, age, value, upper):
    ref = nutrients.reference_for(key, nutrients.profile(cfg, sex=sex, age=age))
    assert ref["value"] == value
    if upper is None:
        assert not ref["upper_compared"]
    else:
        assert ref["upper"] == upper


def test_sodiums_upper_reference_is_the_cdrr_and_magnesiums_ul_is_never_compared(cfg):
    prof = nutrients.profile(cfg)
    sodium = nutrients.reference_for("sodium", prof)
    assert (sodium["kind"], sodium["upper_kind"], sodium["upper_compared"]) == \
        ("AI", "CDRR", True)
    for key in ("magnesium", "niacin", "folate", "vitamin_e", "vitamin_a"):
        ref = nutrients.reference_for(key, prof)
        assert ref["upper"] is not None and ref["upper_compared"] is False, key
        assert "only" in ref["upper_note"]


def test_every_reference_names_its_source():
    for ref in nutrients.REFERENCES:
        assert ref.source and ref.kind in ("RDA", "AI") and ref.bands[0][0] == 19
        assert ("IOM" in ref.source or "NASEM" in ref.source), ref.key


def test_without_sex_or_age_the_higher_value_is_used_and_said(cfg):
    prof = nutrients.profile(cfg)
    assert prof["sex"] is None and prof["age"] is None
    assert prof["label"] == "adult (higher of female and male), ages 19-50"
    assert len(prof["notes"]) == 2
    assert "higher of the female and male values" in prof["notes"][0]


def test_the_profile_comes_from_config_and_flags_override_it(cfg):
    cfg.extra = {"nutrition": {"sex": "Female", "age": 34}}
    prof = nutrients.profile(cfg)
    assert (prof["sex"], prof["age"], prof["notes"]) == ("female", 34, [])
    assert prof["label"] == "female, ages 31-50 (age 34)"
    over = nutrients.profile(cfg, sex="male", age="72")
    assert (over["sex"], over["age"]) == ("male", 72)
    assert over["label"] == "male, ages 71+ (age 72)"


@pytest.mark.parametrize("settings, needle", [
    ({"sex": "other"}, '[nutrition] sex must be "female" or "male"'),
    ({"age": 17}, "covers adults, 19 and over"),
    ({"age": "thirty"}, "must be a whole number of years"),
    ({"age": True}, "must be a whole number of years"),
])
def test_a_profile_the_table_cannot_use_fails_in_one_line(cfg, settings, needle):
    cfg.extra = {"nutrition": settings}
    with pytest.raises(DomainError) as e:
        nutrients.profile(cfg)
    assert needle in str(e.value) and "\n" not in str(e.value)


def test_a_bad_flag_names_the_flag(cfg):
    with pytest.raises(DomainError) as e:
        nutrients.profile(cfg, sex="m")
    assert str(e.value).startswith("--sex must be")


# ---------------------------------------------------------------- thresholds

@pytest.mark.parametrize("percent, expected", [
    (0.0, "low"), (69.94, "low"), (69.96, "borderline"), (70.0, "borderline"),
    (99.94, "borderline"), (99.96, "meets"), (100.0, "meets"), (250.0, "meets"),
])
def test_the_70_and_100_percent_lines(percent, expected):
    assert nutrients.band(percent) == expected


def test_every_band_from_daily_summaries(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", TWO_DAYS)
    rep = _report(conn, cfg)
    assert rep["coverage"]["days_counted"] == 2
    assert rep["coverage"]["from_daily_summary"] == 2
    mg = _item(rep, "magnesium")
    assert mg["average"] == 252.0 and mg["percent"] == 60.0 and mg["status"] == "low"
    fiber = _item(rep, "fiber")
    assert fiber["percent"] == 76.3 and fiber["status"] == "borderline"
    assert _item(rep, "protein")["status"] == "meets"
    iron = _item(rep, "iron")
    assert iron["status"] == "high" and len(iron["days_over"]) == 2
    assert rep["low"] == ["vitamin_d", "magnesium"]
    assert rep["borderline"] == ["fiber"]
    assert rep["high"] == ["iron"]
    vd = _item(rep, "vitamin_d")
    assert vd["unit"] == "µg" and vd["average"] == 4.5          # (160+200)/2/40


def test_an_upper_level_set_for_supplements_is_never_held_against_food(tmp_path, cfg, conn):
    """Magnesium's 350 mg UL and niacin's 35 mg are for supplements; a diary
    of food above them is not 'high'."""
    _import(conn, cfg, tmp_path, "dailysummary.csv", _daily([
        ("2026-10-20", 2400, 600, 400, 40, 2000, 20, 140, 60, "true")]))
    rep = _report(conn, cfg)
    for key in ("magnesium", "niacin"):
        item = _item(rep, key)
        assert item["status"] == "meets" and item["days_over"] == [], key
    assert rep["high"] == [] and rep["over_on_some_days"] == []


def test_days_over_an_upper_level_are_listed_with_their_rows(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", TWO_DAYS)
    rep = _report(conn, cfg)
    sodium = _item(rep, "sodium")
    assert sodium["status"] == "meets"         # 2,250 on average: over the AI, under the CDRR
    assert sodium["average"] == 2250.0
    assert [o["date"] for o in sodium["days_over"]] == ["2026-10-19"]
    assert sodium["days_over"][0]["ids"] == _ids(conn, "nutrition_day", "2026-10-19")
    assert rep["over_on_some_days"] == ["iron", "sodium"]
    text = "\n".join(nutrients.render_lines(rep))
    assert "Sodium: above the CDRR 2,300 mg on 1 of 2 days: 2026-10-19 2,600 mg" in text


# ---------------------------------------------------------------- citations

def test_every_average_cites_the_rows_behind_it(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", TWO_DAYS)
    rep = _report(conn, cfg)
    ids = _ids(conn, "nutrition_day", "2026-10-19", "2026-10-20")
    for item in rep["nutrients"]:
        assert item["cites"]["nutrition_day"] == ids
        assert item["cites"]["dates"] == ["2026-10-19", "2026-10-20"]
    text = "\n".join(nutrients.render_lines(rep))
    line = next(x for x in text.splitlines() if x.startswith("  Magnesium:"))
    assert line.endswith(f"[nutrition_day #{ids[0]}-{ids[1]}]")
    assert "60.0% of the RDA 420 mg" in line


def test_the_foods_behind_a_low_nutrient_are_named(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", _daily([
        ("2026-10-20", 2000, 200, 100, 20, 1800, 20, 120, 20, "true")]))
    _import(conn, cfg, tmp_path, "servings.csv", SERVINGS_HEADER +
            "2026-10-20,12:35,Lunch,Black Beans,1.00 cup,218,84,,16.6,460\n"
            "2026-10-20,19:40,Dinner,Black Beans,0.50 cup,109,42,,8.3,230\n"
            "2026-10-20,8:05,Breakfast,Greek Yogurt,1.00 container,100,19,,0,61\n"
            "2026-10-20,19:40,Dinner,White Rice,2.00 cup,410,38,,1.2,4\n")
    rep = _report(conn, cfg)
    sources = _item(rep, "magnesium")["sources"]
    assert [s["food"] for s in sources] == ["Black Beans", "White Rice", "Greek Yogurt"]
    assert sources[0]["share_percent"] == 69 and sources[0]["servings"] == 2
    beans = sorted(r["id"] for r in conn.execute(
        "SELECT id FROM observations WHERE title='Black Beans'"))
    assert sources[0]["food_log"] == beans
    text = "\n".join(nutrients.render_lines(rep))
    assert "most from: Black Beans 69%, White Rice 21%, Greek Yogurt 10%" in text


def test_a_day_without_a_summary_is_summed_from_its_servings(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "servings.csv", SERVINGS_HEADER +
            "2026-10-20,12:35,Lunch,Black Beans,1.00 cup,218,84,,16.6,460\n"
            "2026-10-20,19:40,Dinner,Salmon,5.00 oz,291,43,745,,86\n")
    rep = _report(conn, cfg)
    cov = rep["coverage"]
    assert (cov["days_counted"], cov["from_daily_summary"], cov["from_food_log"]) == (1, 0, 1)
    mg = _item(rep, "magnesium")
    assert mg["average"] == 127.0
    assert mg["cites"]["food_log"] == _ids(conn, "food_log", "2026-10-20")
    assert mg["cites"]["nutrition_day"] == []
    assert _item(rep, "vitamin_d")["average"] == 18.62
    text = "\n".join(nutrients.render_lines(rep))
    assert "0 from Cronometer daily summaries, 1 summed from food_log" in text
    assert "[food_log sums #" in text


# ---------------------------------------------------------------- which days

def test_a_day_not_marked_completed_is_skipped_when_completed_is_in_use(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", _daily([
        ("2026-10-19", 2200, 250, 160, 28, 2100, 20, 120, 20, "true"),
        ("2026-10-20", 900, 80, 0, 8, 700, 5, 40, 6, "false")]))
    rep = _report(conn, cfg)
    assert rep["coverage"]["days_counted"] == 1
    assert rep["coverage"]["skipped"] == [
        {"date": "2026-10-20", "why": "not marked completed in Cronometer"}]
    assert _item(rep, "magnesium")["average"] == 250.0


def test_today_is_skipped_unless_marked_completed(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", _daily([
        ("2026-10-20", 2200, 250, 160, 28, 2100, 20, 120, 20, "true"),
        ("2026-10-21", 600, 60, 0, 5, 500, 4, 30, 5, "")]))
    rep = _report(conn, cfg)
    assert rep["coverage"]["skipped"] == [
        {"date": "2026-10-21", "why": "today — still being logged"}]
    # The next day's export holds the finished day (and is newer, so it wins).
    _import(conn, cfg, tmp_path / "later", "dailysummary.csv", _daily([
        ("2026-10-21", 2300, 260, 160, 28, 2100, 20, 120, 20, "true"),
        ("2026-10-22", 500, 50, 0, 4, 400, 3, 30, 4, "false")]))
    assert _report(conn, cfg)["coverage"]["days_counted"] == 2


def test_when_no_day_is_marked_completed_every_day_counts_and_says_so(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", _daily([
        ("2026-10-19", 2200, 250, 160, 28, 2100, 20, 120, 20, "false"),
        ("2026-10-20", 2300, 254, 200, 30, 1900, 20, 130, 22, "false")]))
    rep = _report(conn, cfg)
    assert rep["coverage"]["days_counted"] == 2 and rep["coverage"]["skipped"] == []
    assert any("no day in this window is marked completed" in n for n in rep["notes"])


def test_a_nutrient_missing_on_some_days_counts_as_zero_and_says_so(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", _daily([
        ("2026-10-19", 2200, 250, None, 28, 2100, 20, 120, 20, "true"),
        ("2026-10-20", 2300, 254, 400, 30, 1900, 20, 130, 22, "true")]))
    rep = _report(conn, cfg)
    vd = _item(rep, "vitamin_d")
    assert vd["days_with_value"] == 1 and vd["average"] == 5.0
    text = "\n".join(nutrients.render_lines(rep))
    assert "Vitamin D: 5.00 µg/day — 33.3% of the RDA 15.0 µg (on 1 of 2 days)" in text


def test_a_nutrient_the_export_never_reports_is_not_called_low(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", TWO_DAYS)
    rep = _report(conn, cfg)
    assert "iodine" in rep["not_in_data"] and "potassium" in rep["not_in_data"]
    assert "iodine" not in rep["low"]


def test_a_value_stored_in_another_unit_is_not_compared(tmp_path, cfg, conn):
    rec = {"stream": "nutrition_day", "external_id": "test:day:2026-10-20",
           "journal_date": "2026-10-20", "started_at": "2026-10-20T17:00:00Z",
           "title": "Nutrition 2026-10-20", "value": 2000.0, "unit": "kcal",
           "value_json": canonical_json({"completed": True, "nutrients": {
               "vitamin_d": [400.0, "IU"], "energy": [2000.0, "kcal"]}}),
           "participants": []}
    scan = {"connector": "test", "records": [rec], "handles": []}
    obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()
    rep = _report(conn, cfg)
    (nc,) = rep["not_compared"]
    assert nc["key"] == "vitamin_d" and "stored in IU" in nc["why"]
    assert rep["low"] == []


def test_meal_totals_name_no_food_sources(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "Nutrition-Summary.csv",
            "Date,Meal,Calories,Sodium (mg),Fiber,Protein (g)\n"
            "2026-10-20,Breakfast,500,400,4,30\n2026-10-20,Dinner,900,1200,6,50\n")
    rep = _report(conn, cfg)
    fiber = _item(rep, "fiber")
    assert fiber["average"] == 10.0 and fiber["status"] == "low"
    assert fiber["sources"] == []
    assert any("meal totals, not foods" in n for n in rep["notes"])


def test_an_empty_window_says_there_is_nothing_to_compare(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", TWO_DAYS)
    rep = _report(conn, cfg, start="2026-01-01", end="2026-01-31")
    assert rep["coverage"]["days_counted"] == 0 and rep["nutrients"] == []
    assert rep["low"] == [] and rep["high"] == []
    lines = nutrients.render_lines(rep)
    assert lines[0] == nutrients.DISCLAIMER
    assert any("no nutrition data in this window" in x for x in lines)


# ---------------------------------------------------------------- window

def test_the_window_defaults_to_the_last_30_days(cfg):
    w = nutrients.resolve_window(cfg, today=TODAY)
    assert (w["start"], w["end"], w["days"], w["label"]) == \
        ("2026-09-22", "2026-10-21", 30, "last 30 days")
    w = nutrients.resolve_window(cfg, days=7, today=TODAY)
    assert w["start"] == "2026-10-15"
    w = nutrients.resolve_window(cfg, start="2026-10-01", today=TODAY)
    assert (w["end"], w["days"]) == ("2026-10-21", 21)


@pytest.mark.parametrize("kw, needle", [
    ({"days": 0}, "--days must be 1-"),
    ({"days": 7, "start": "2026-10-01"}, "not both"),
    ({"end": "2026-10-01"}, "--end needs --start"),
    ({"start": "2026-10-10", "end": "2026-10-01"}, "is before --start"),
    ({"start": "October"}, "is not a date"),
])
def test_a_bad_window_fails_in_one_line(cfg, kw, needle):
    with pytest.raises(DomainError) as e:
        nutrients.resolve_window(cfg, today=TODAY, **kw)
    assert needle in str(e.value)


# ---------------------------------------------------------------- the contract

def test_the_report_is_deterministic_and_needs_only_a_read_only_connection(
        tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", TWO_DAYS)
    conn.close()
    ro = db_mod.connect(cfg.db_path, readonly=True)
    try:
        a, b = _report(ro, cfg), _report(ro, cfg)
    finally:
        ro.close()
    assert a == b
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_the_text_compares_and_never_advises(tmp_path, cfg, conn):
    _import(conn, cfg, tmp_path, "dailysummary.csv", TWO_DAYS)
    lines = nutrients.render_lines(_report(conn, cfg))
    assert lines[0] == nutrients.DISCLAIMER
    assert "not medical advice" in lines[0]
    text = "\n".join(lines[1:]).lower().replace("office of dietary supplements", "")
    for word in ("supplement", "you should", "eat more", "take", "try", "dose",
                 "deficient", "deficiency in"):
        assert not re.search(rf"\b{word}\b", text), word


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch, extra=""):
    root = cli_project(tmp_path, monkeypatch)
    if extra:
        with open(root / "config.toml", "a", encoding="utf-8") as f:
            f.write(extra)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def _observe(tmp_path, text):
    p = tmp_path / "exp" / "dailysummary.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    r = runner.invoke(app, ["observe", "nutrition", str(p), "--apply"])
    assert r.exit_code == 0, r.output


def test_cli_prints_the_comparison(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch, '[nutrition]\nsex = "male"\nage = 32\n')
    _observe(tmp_path, TWO_DAYS)
    r = runner.invoke(app, ["nutrients", "--start", "2026-10-19", "--end", "2026-10-20"])
    assert r.exit_code == 0, r.output
    lines = r.output.splitlines()
    assert lines[0] == nutrients.DISCLAIMER
    assert "reference intakes for: male, ages 31-50 (age 32)" in r.output
    assert "  Magnesium: 252 mg/day — 60.0% of the RDA 420 mg" in r.output


def test_cli_json_is_the_report(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    _observe(tmp_path, TWO_DAYS)
    r = runner.invoke(app, ["nutrients", "--start", "2026-10-19", "--end", "2026-10-20",
                            "--sex", "female", "--age", "40", "--json"])
    assert r.exit_code == 0, r.output
    rep = json.loads(r.output)
    assert rep["profile"]["sex"] == "female" and rep["disclaimer"] == nutrients.DISCLAIMER
    assert _item(rep, "magnesium")["reference"]["value"] == 320
    assert rep["low"] == ["vitamin_d"] and "magnesium" in rep["borderline"]


def test_cli_refuses_a_bad_profile_and_a_bad_window(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["nutrients", "--sex", "x"])
    assert r.exit_code == 1 and '--sex must be "female" or "male"' in r.output
    r = runner.invoke(app, ["nutrients", "--days", "7", "--start", "2026-10-01"])
    assert r.exit_code == 1 and "not both" in r.output


def test_cli_on_an_empty_database_says_how_to_import(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["nutrients"])
    assert r.exit_code == 0, r.output
    assert "lifecrm observe nutrition" in r.output

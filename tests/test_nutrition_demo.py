"""The nutrition demo sample: the persona's food diary, through the real
connector and the real report.

Both combos export the same Cronometer CSVs (one account, either app), and
they must tell the persona's story — the Chipotle bowl, the Veracruz tacos,
the Uchi omakase, the meal prep after the ChatGPT chat — add up to the
energy the health apps hold, and leave the pattern `lifecrm nutrients` is
there to find: vitamin D and magnesium low, fiber borderline, sodium over
its reference on the taco nights only.
"""

import json
from datetime import date

import pytest

from lifecrm import nutrients
from lifecrm.connectors import nutrition
from lifecrm.demo import persona
from lifecrm.demo.sources import nutrition as sample
from lifecrm.doctor import run_checks
from lifecrm.domain import observations as obs

ANCHOR = date(2026, 10, 21)
TACO_NIGHTS = (-38, -20, -1)


def _paths(tmp_path, combo, anchor=ANCHOR):
    return sample.write_samples(tmp_path / combo, anchor, combo)


def _scans(tmp_path, cfg, combo, anchor=ANCHOR):
    return [nutrition.scan(p, cfg) for p in _paths(tmp_path, combo, anchor)]


def _foods(scan, off):
    day = persona.day(ANCHOR, off).isoformat()
    return [(json.loads(r["value_json"])["meal"], r["title"]) for r in scan["records"]
            if r["journal_date"] == day]


def _load(tmp_path, cfg, conn, combo):
    for scan in _scans(tmp_path, cfg, combo):
        obs.apply_plan(conn, scan, obs.build_plan(conn, scan))
    conn.commit()


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_both_combos_export_cronometers_two_csvs(tmp_path, cfg, combo):
    paths = _paths(tmp_path, combo)
    assert [p.name for p in paths] == ["servings.csv", "dailysummary.csv"]
    servings, summary = _scans(tmp_path / "s", cfg, combo)
    assert {r["stream"] for r in servings["records"]} == {"food_log"}
    assert {r["stream"] for r in summary["records"]} == {"nutrition_day"}
    assert len(summary["records"]) == persona.WINDOW_DAYS
    assert servings["unmapped"] == [] and summary["unmapped"] == []
    assert servings["facts"]["kept_as_is"] == []


def test_the_journals_meals_are_in_the_diary(tmp_path, cfg):
    servings, _ = _scans(tmp_path, cfg, "android")
    assert ("Lunch", "Chipotle, Chicken Burrito Bowl") in _foods(servings, -40)
    assert ("Dinner", "Fish Taco") in _foods(servings, -38)
    assert ("Dinner", "Margarita") in _foods(servings, -20)
    assert ("Dinner", "Salmon Nigiri") in _foods(servings, -9)          # Uchi
    assert ("Dinner", "Fish Taco") in _foods(servings, -1)


def test_the_meal_prep_starts_after_the_chatgpt_chat(tmp_path, cfg):
    chat = next(c for c in persona.AI_CHATS if c.title == "High-protein meal prep")
    servings, _ = _scans(tmp_path, cfg, "android")
    chili = sorted({r["journal_date"] for r in servings["records"]
                    if r["title"] == "Turkey Chili, Homemade"})
    assert chili and min(chili) > persona.day(ANCHOR, chat.offset).isoformat()
    for off in range(chat.offset + 1, 1):
        titles = {t for _, t in _foods(servings, off)}
        assert "Greek Yogurt, Plain, Nonfat" in titles, off
        assert "Instant Oatmeal, Plain, Fortified" in titles, off


def test_a_days_energy_is_what_the_health_apps_hold(tmp_path, cfg):
    _, summary = _scans(tmp_path, cfg, "android")
    health = {h.date.isoformat(): h.kcal_in for h in persona.health_series(ANCHOR)}
    same = [r for r in summary["records"] if r["value"] == health[r["journal_date"]]]
    assert len(same) >= persona.WINDOW_DAYS - 4          # the big dinners may run over
    short = next(r for r in summary["records"]
                 if r["journal_date"] == persona.day(ANCHOR, sample.INCOMPLETE_DAY).isoformat())
    assert short["value"] < health[short["journal_date"]]       # dinner never logged
    assert json.loads(short["value_json"])["completed"] is False


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_report_finds_the_deliberate_pattern(tmp_path, cfg, conn, combo):
    _load(tmp_path, cfg, conn, combo)
    window = nutrients.resolve_window(cfg, today=ANCHOR)
    rep = nutrients.build_report(conn, cfg, window)
    assert rep["low"] == ["vitamin_d", "magnesium"]
    assert rep["borderline"] == ["fiber"]
    assert rep["high"] == [] and rep["over_on_some_days"] == ["sodium"]
    over = [o["date"] for o in next(x for x in rep["nutrients"]
                                    if x["key"] == "sodium")["days_over"]]
    in_window = [persona.day(ANCHOR, o).isoformat() for o in TACO_NIGHTS
                 if o > -nutrients.DEFAULT_DAYS]
    assert over == in_window
    skipped = {s["date"] for s in rep["coverage"]["skipped"]}
    assert skipped == {persona.day(ANCHOR, sample.INCOMPLETE_DAY).isoformat(),
                       ANCHOR.isoformat()}
    vit_d = next(x for x in rep["nutrients"] if x["key"] == "vitamin_d")
    assert vit_d["sources"][0]["food"] == "Salmon, Atlantic, Farmed, Baked"


def test_the_pattern_holds_for_the_personas_own_reference_values(tmp_path, cfg, conn):
    """Alex is a man of 32 (born 1994; his mother calls him mijo)."""
    _load(tmp_path, cfg, conn, "android")
    window = nutrients.resolve_window(cfg, today=ANCHOR)
    rep = nutrients.build_report(conn, cfg, window,
                                 prof=nutrients.profile(cfg, sex="male", age=32))
    assert rep["low"] == ["vitamin_d", "magnesium"] and rep["borderline"] == ["fiber"]
    assert rep["profile"]["label"] == "male, ages 31-50 (age 32)"
    assert rep["profile"]["notes"] == []


def test_the_whole_window_shows_every_taco_night_and_the_chipotle_bowl(tmp_path, cfg, conn):
    _load(tmp_path, cfg, conn, "iphone")
    window = nutrients.resolve_window(cfg, days=persona.WINDOW_DAYS, today=ANCHOR)
    rep = nutrients.build_report(conn, cfg, window)
    sodium = next(x for x in rep["nutrients"] if x["key"] == "sodium")
    assert [o["date"] for o in sodium["days_over"]] == \
        [persona.day(ANCHOR, o).isoformat() for o in (-40, *TACO_NIGHTS)]
    src = [s["food"] for s in sodium["sources_on_days_over"]]
    assert src[0] == "Fish Taco" and "Chipotle, Chicken Burrito Bowl" in src


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_applies_cleanly_and_reimports_as_identical(tmp_path, cfg, conn, combo):
    first = []
    for scan in _scans(tmp_path, cfg, combo):
        plan = obs.build_plan(conn, scan)
        assert plan["invalid"] == [] and plan["dup_incoming"] == 0
        first.append(obs.apply_plan(conn, scan, plan)["added"])
        conn.commit()
    assert first[1] == persona.WINDOW_DAYS
    for scan, n in zip(_scans(tmp_path / "again", cfg, combo), first):
        again = obs.build_plan(conn, scan)
        assert again["identical"] == n and again["new"] == []
    report = run_checks(conn, cfg)
    assert report["failures"] == 0, [c for c in report["checks"] if c["status"] == "fail"]


@pytest.mark.parametrize("combo", persona.COMBOS)
def test_the_sample_is_deterministic(tmp_path, combo):
    a = sample.write_samples(tmp_path / "a", ANCHOR, combo)
    b = sample.write_samples(tmp_path / "b", ANCHOR, combo)
    assert [p.read_bytes() for p in a] == [p.read_bytes() for p in b]
    other = "iphone" if combo == "android" else "android"
    c = sample.write_samples(tmp_path / "c", ANCHOR, other)
    assert [p.read_bytes() for p in a] == [p.read_bytes() for p in c]   # one account


def test_the_sample_follows_the_anchor(tmp_path, cfg):
    later = date(2027, 3, 2)
    _, summary = _scans(tmp_path, cfg, "android", anchor=later)
    assert max(r["journal_date"] for r in summary["records"]) == later.isoformat()


def test_an_unknown_combo_is_refused(tmp_path):
    with pytest.raises(ValueError) as e:
        sample.write_samples(tmp_path, ANCHOR, "blackberry")
    assert "blackberry" in str(e.value)

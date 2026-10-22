"""Apple Health import: a synthetic "Export All Health Data" export.zip built
from XML strings shaped like the real export (internal DTD, ExportDate, Me,
Records with MetadataEntry children, Food Correlations whose members are also
top-level, old- and new-style Workouts, ActivitySummary, a decoy CDA document
and a route .gpx).

The contract under test: the same canonical metrics and units Health Connect
rows use; identity that survives an export rendered in another zone and
another unit preference (the key test); hourly buckets for high-frequency
types; sleep that never counts the phone's in-bed night on top of the watch's
stages; the shared dedupe / newer-wins / retract path; format auto-detection
and export discovery in the CLI.
"""

import json
import random
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from conftest import cli_project

from lifecrm import apple_health as ah
from lifecrm import health_import as hi
from lifecrm import platforms
from lifecrm.domain import health
from lifecrm.domain.util import DomainError

WATCH = "Alex’s Apple Watch"           # the device's own name, curly apostrophe
PHONE = "Alex’s iPhone"
FOOD_APP = "MyFitnessPal"
Q = "HKQuantityTypeIdentifier"
STAGE = "HKCategoryValueSleepAnalysis"
EXPORTED = "2026-07-28T23:15:22Z"

DTD = """<!DOCTYPE HealthData [
<!-- HealthKit Export Version: 14 -->
<!ELEMENT HealthData (ExportDate,Me,(Record|Correlation|Workout|ActivitySummary)*)>
<!ATTLIST HealthData
  locale CDATA #REQUIRED
>
<!ELEMENT Record ((MetadataEntry|HeartRateVariabilityMetadataList)*)>
<!ATTLIST Record
  type          CDATA #REQUIRED
  unit          CDATA #IMPLIED
  value         CDATA #IMPLIED
  sourceName    CDATA #REQUIRED
  sourceVersion CDATA #IMPLIED
  device        CDATA #IMPLIED
  creationDate  CDATA #IMPLIED
  startDate     CDATA #REQUIRED
  endDate       CDATA #REQUIRED
>
<!ELEMENT MetadataEntry EMPTY>
<!ATTLIST MetadataEntry
  key   CDATA #REQUIRED
  value CDATA #REQUIRED
>
]>"""


def _d(utc: str, offset: str) -> str:
    """An instant as the export renders it: wall clock in the zone the phone
    was in at export time ('2026-07-28 09:05:00 -0500')."""
    dt = datetime.fromisoformat(utc.replace("Z", "+00:00"))
    sign = -1 if offset[0] == "-" else 1
    local = dt + sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
    return local.strftime("%Y-%m-%d %H:%M:%S ") + offset


def _created(start: str, minutes: int) -> str:
    dt = datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Export:
    """Records are given in UTC; render() writes them as one export would."""

    def __init__(self):
        self.items = []          # (sort key, renderer(offset) -> str)

    def record(self, hk_type, source, start, end, value, unit=None, created=None,
               meta=None, indent=" "):
        created = created or _created(end, 2)

        def render(off):
            attrs = (f'type="{hk_type}" sourceName="{source}" sourceVersion="17.5" '
                     + (f'unit="{unit}" ' if unit else "")
                     + f'creationDate="{_d(created, off)}" startDate="{_d(start, off)}" '
                       f'endDate="{_d(end, off)}" value="{value}"')
            if not meta:
                return f"{indent}<Record {attrs}/>"
            inner = "".join(f'\n{indent}  <MetadataEntry key="{k}" value="{v}"/>'
                            for k, v in meta.items())
            return f"{indent}<Record {attrs}>{inner}\n{indent}</Record>"
        return render

    def add(self, *args, **kw):
        self.items.append(self.record(*args, **kw))

    def raw(self, render):
        self.items.append(render)

    def render(self, offset="-0500", dtd=DTD, exported=EXPORTED) -> str:
        body = "\n".join(r(offset) for r in self.items)
        return ('<?xml version="1.0" encoding="UTF-8"?>\n' + dtd + "\n"
                '<HealthData locale="en_US">\n'
                f' <ExportDate value="{_d(exported, offset)}"/>\n'
                ' <Me HKCharacteristicTypeIdentifierDateOfBirth="1994-04-02" '
                'HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexNotSet"/>\n'
                f"{body}\n</HealthData>\n")


def build(metric_units=False, newer_weight=False, drop_water=False,
          shuffle_seed=None) -> Export:
    """The fixture day, 2026-07-28 in Chicago (CDT, -0500). `metric_units`
    renders the same samples the way a phone set to kg/km/kcal/L would."""
    e = Export()
    lb, kg = 186.3, 186.3 * 0.45359237

    for start, end, n, src in (("2026-07-28T14:05:00Z", "2026-07-28T14:15:00Z", 400, PHONE),
                               ("2026-07-28T14:30:00Z", "2026-07-28T14:40:00Z", 600, PHONE),
                               ("2026-07-28T14:10:00Z", "2026-07-28T14:20:00Z", 350, WATCH),
                               ("2026-07-28T19:00:00Z", "2026-07-28T19:10:00Z", 1000, PHONE)):
        e.add(Q + "StepCount", src, start, end, n, "count")
    for start, end, km in (("2026-07-28T14:05:00Z", "2026-07-28T14:15:00Z", 0.3),
                           ("2026-07-28T14:30:00Z", "2026-07-28T14:40:00Z", 0.45)):
        e.add(Q + "DistanceWalkingRunning", PHONE, start, end,
              repr(km) if metric_units else repr(km / 1.609344),
              "km" if metric_units else "mi")
    for t, bpm in (("2026-07-28T14:00:00Z", 60), ("2026-07-28T14:10:00Z", 70),
                   ("2026-07-28T14:20:00Z", 80)):
        e.add(Q + "HeartRate", WATCH, t, t, bpm, "count/min",
              meta={"HKMetadataKeyHeartRateMotionContext": "0"})
    e.add(Q + "RestingHeartRate", WATCH, "2026-07-28T11:00:00Z",
          "2026-07-29T01:00:00Z", 58, "count/min")
    e.add(Q + "ActiveEnergyBurned", WATCH, "2026-07-28T14:00:00Z",
          "2026-07-28T14:10:00Z", 12.5, "kcal" if metric_units else "Cal")
    e.add(Q + "BasalEnergyBurned", WATCH, "2026-07-28T14:00:00Z",
          "2026-07-28T14:59:59Z", 70, "kcal" if metric_units else "Cal")

    # 04:30 local, before the 05:00 rollover: belongs to the previous day.
    # A re-entered weight: same instant, different value, newer creationDate.
    weigh = "2026-07-28T09:30:00Z"
    if newer_weight:
        e.add(Q + "BodyMass", "Health", weigh, weigh, 187.0, "lb",
              created="2026-07-28T10:31:00Z")
    else:
        e.add(Q + "BodyMass", "Health", weigh, weigh,
              repr(kg) if metric_units else lb, "kg" if metric_units else "lb",
              created="2026-07-28T09:31:00Z")
    e.add(Q + "BodyFatPercentage", "Health", "2026-07-28T09:31:00Z",
          "2026-07-28T09:31:00Z", 0.183, "%")
    if not drop_water:
        e.add(Q + "DietaryWater", WATCH, "2026-07-28T15:00:00Z", "2026-07-28T15:00:00Z",
              0.5 if metric_units else 500, "L" if metric_units else "mL")
    e.add(Q + "DietaryWater", WATCH, "2026-07-28T16:00:00Z", "2026-07-28T16:00:00Z",
          0.1, "gal_us")                                     # no conversion: skipped

    # Two foods logged at the same instant; the first was logged as a meal
    # (a Food correlation) whose members ALSO appear at top level.
    lunch = "2026-07-28T17:00:00Z"
    bowl = {"HKFoodType": "Chicken bowl"}
    a_kcal = 650 if metric_units else 2719.6               # kJ in the default units
    unit = "kcal" if metric_units else "kJ"
    members = [(Q + "DietaryEnergyConsumed", a_kcal, unit),
               (Q + "DietaryProtein", 42.5, "g"), (Q + "DietaryFatTotal", 20, "g"),
               (Q + "DietaryCarbohydrates", 55, "g")]
    for t, v, u in members:
        e.add(t, FOOD_APP, lunch, lunch, v, u, created="2026-07-28T17:05:00Z", meta=bowl)
    e.add(Q + "DietaryEnergyConsumed", FOOD_APP, lunch, lunch,
          300 if metric_units else 1255.2, unit, created="2026-07-28T17:06:00Z")
    nested = [e.record(t, FOOD_APP, lunch, lunch, v, u, created="2026-07-28T17:05:00Z",
                       meta=bowl, indent="  ") for t, v, u in members]
    e.raw(lambda off: (
        f' <Correlation type="HKCorrelationTypeIdentifierFood" sourceName="{FOOD_APP}" '
        f'creationDate="{_d("2026-07-28T17:05:00Z", off)}" startDate="{_d(lunch, off)}" '
        f'endDate="{_d(lunch, off)}">\n'
        '  <MetadataEntry key="HKFoodType" value="Chicken bowl"/>\n'
        + "\n".join(r(off) for r in nested) + "\n </Correlation>"))
    # A snack that exists ONLY inside its correlation: imported exactly once.
    snack = "2026-07-28T20:00:00Z"
    only = [e.record(Q + "DietaryEnergyConsumed", FOOD_APP, snack, snack,
                     120, "kcal", indent="  "),
            e.record(Q + "DietaryProtein", FOOD_APP, snack, snack, 10, "g", indent="  ")]
    e.raw(lambda off: (
        f' <Correlation type="HKCorrelationTypeIdentifierFood" sourceName="{FOOD_APP}" '
        f'creationDate="{_d(_created(snack, 2), off)}" startDate="{_d(snack, off)}" '
        f'endDate="{_d(snack, off)}">\n' + "\n".join(r(off) for r in only)
        + "\n </Correlation>"))

    # A night: the watch's stages, and the iPhone's Bedtime in-bed sample.
    chicago = {"HKTimeZone": "America/Chicago"}
    for start, end, stage in (("03:00", "03:30", "Awake"), ("03:30", "07:30", "AsleepCore"),
                              ("07:30", "09:00", "AsleepDeep"), ("09:00", "10:00", "AsleepREM")):
        e.add("HKCategoryTypeIdentifierSleepAnalysis", WATCH, f"2026-07-28T{start}:00Z",
              f"2026-07-28T{end}:00Z", STAGE + stage, created="2026-07-28T10:04:00Z",
              meta=chicago)
    e.add("HKCategoryTypeIdentifierSleepAnalysis", PHONE, "2026-07-28T02:50:00Z",
          "2026-07-28T10:05:00Z", STAGE + "InBed", created="2026-07-28T10:06:00Z")

    e.add(Q + "FlightsClimbed", PHONE, "2026-07-28T15:00:00Z", "2026-07-28T15:01:00Z", 2, "count")
    e.add(Q + "FlightsClimbed", PHONE, "2026-07-28T18:00:00Z", "2026-07-28T18:01:00Z", 1, "count")

    # Workouts: pre-iOS 16 totals on the element, and iOS 16+ statistics.
    walk_s, walk_e = "2026-07-28T22:00:00Z", "2026-07-28T22:45:00Z"
    e.raw(lambda off: (
        ' <Workout workoutActivityType="HKWorkoutActivityTypeWalking" duration="45" '
        'durationUnit="min" '
        + ('totalDistance="3.3796224" totalDistanceUnit="km" ' if metric_units
           else 'totalDistance="2.1" totalDistanceUnit="mi" ')
        + f'totalEnergyBurned="180" totalEnergyBurnedUnit="kcal" sourceName="{WATCH}" '
        f'creationDate="{_d(_created(walk_e, 1), off)}" startDate="{_d(walk_s, off)}" '
        f'endDate="{_d(walk_e, off)}">\n'
        f'  <WorkoutEvent type="HKWorkoutEventTypePause" date="{_d(walk_s, off)}"/>\n'
        ' </Workout>'))
    lift_s, lift_e = "2026-07-27T11:30:00Z", "2026-07-27T12:30:00Z"
    e.raw(lambda off: (
        ' <Workout workoutActivityType="HKWorkoutActivityTypeTraditionalStrengthTraining" '
        f'duration="60" durationUnit="min" sourceName="{WATCH}" '
        f'creationDate="{_d(_created(lift_e, 1), off)}" startDate="{_d(lift_s, off)}" '
        f'endDate="{_d(lift_e, off)}">\n'
        '  <MetadataEntry key="HKTimeZone" value="Europe/Ljubljana"/>\n'
        '  <MetadataEntry key="HKIndoorWorkout" value="1"/>\n'
        f'  <WorkoutStatistics type="{Q}ActiveEnergyBurned" startDate="{_d(lift_s, off)}" '
        f'endDate="{_d(lift_e, off)}" sum="{420 if metric_units else 420}" '
        f'unit="{"kcal" if metric_units else "Cal"}"/>\n'
        f'  <WorkoutStatistics type="{Q}HeartRate" startDate="{_d(lift_s, off)}" '
        f'endDate="{_d(lift_e, off)}" average="128" minimum="90" maximum="160" '
        'unit="count/min"/>\n'
        ' </Workout>'))
    e.raw(lambda off: (' <ActivitySummary dateComponents="2026-07-28" '
                       'activeEnergyBurned="512" activeEnergyBurnedGoal="600" '
                       'activeEnergyBurnedUnit="Cal" appleExerciseTime="45"/>'))
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(e.items)
    return e


def write_zip(path, xml: str, extra: dict | None = None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("apple_health_export/export.xml", xml.encode("utf-8"))
        zf.writestr("apple_health_export/export_cda.xml",
                    '<?xml version="1.0"?>\n<ClinicalDocument xmlns="urn:hl7-org:v3"/>\n')
        zf.writestr("apple_health_export/workout-routes/route_2026-07-28_5.00pm.gpx",
                    '<?xml version="1.0"?><gpx version="1.1"></gpx>')
        for name, text in (extra or {}).items():
            zf.writestr(name, text)
    return path


@pytest.fixture
def ah_zip(tmp_path):
    return write_zip(tmp_path / "export.zip", build().render())


def _by_metric(records):
    out = {}
    for r in records:
        out.setdefault(r["metric"], []).append(r)
    return out


def _import(conn, cfg, path):
    scan = ah.read_export(path, cfg)
    plan = ah.build_plan(conn, scan["records"])
    ah.apply_plan(conn, plan)
    conn.commit()
    return scan, plan


KEY = f"export.zip#{EXPORTED}"


# ---------------------------------------------------------------- reading

def test_every_mapped_type_lands_in_the_metric_and_unit_health_connect_uses(cfg, ah_zip):
    scan = ah.read_export(ah_zip, cfg)
    recs = _by_metric(scan["records"])

    assert scan["format"] == "apple_health"
    assert scan["source_file"] == KEY
    assert scan["inner_db"] == "apple_health_export/export.xml"
    assert sorted(recs) == ["body_fat", "distance", "exercise_session", "heart_rate",
                            "hydration", "nutrition", "resting_heart_rate",
                            "sleep_session", "steps", "total_calories", "weight"]
    assert len(scan["records"]) == 17

    w = recs["weight"][0]
    assert (w["value"], w["unit"]) == (84.5, "kg")                       # lb -> kg
    assert w["ended_at"] is None and w["origin_app"] == "Health"
    assert w["external_id"].startswith("ah1:")
    assert (recs["body_fat"][0]["value"], recs["body_fat"][0]["unit"]) == (18.3, "%")
    assert (recs["hydration"][0]["value"], recs["hydration"][0]["unit"]) == (500.0, "ml")
    d = recs["distance"][0]                                               # mi -> km
    assert (d["value"], d["unit"]) == (0.75, "km")
    assert json.loads(d["value_json"]) == {"activity": "walking_running", "samples": 2}
    assert recs["resting_heart_rate"][0]["value"] == 58.0
    assert recs["resting_heart_rate"][0]["unit"] == "bpm"

    kcal = recs["total_calories"][0]                     # active + basal, split kept
    assert (kcal["value"], kcal["unit"]) == (82.5, "kcal")
    assert json.loads(kcal["value_json"]) == {"active_kcal": 12.5, "basal_kcal": 70.0,
                                              "samples": 2}


def test_dates_come_from_the_instant_and_the_samples_own_zone(cfg, ah_zip):
    recs = _by_metric(ah.read_export(ah_zip, cfg)["records"])

    w = recs["weight"][0]
    assert w["started_at"] == "2026-07-28T09:30:00Z"
    assert w["journal_date"] == "2026-07-27"            # 04:30 local, before rollover
    assert w["utc_offset_sec"] is None                  # no HKTimeZone: not guessed

    lift = next(r for r in recs["exercise_session"]
                if "strength" in r["value_json"])
    assert lift["utc_offset_sec"] == 7200               # HKTimeZone Europe/Ljubljana
    assert lift["journal_date"] == "2026-07-27"

    watch = next(r for r in recs["sleep_session"] if r["origin_app"] == WATCH)
    assert watch["utc_offset_sec"] == -18000            # HKTimeZone America/Chicago
    assert watch["journal_date"] == "2026-07-28"        # the wake day
    assert watch["last_modified_at"] == "2026-07-28T10:04:00Z"   # creationDate


def test_phone_and_watch_steps_stay_separate_rows_and_the_report_says_so(cfg, conn, ah_zip):
    scan = ah.read_export(ah_zip, cfg)
    steps = sorted((r["origin_app"], r["value"], r["started_at"])
                   for r in scan["records"] if r["metric"] == "steps")
    assert steps == [(WATCH, 350.0, "2026-07-28T14:10:00Z"),
                     (PHONE, 1000.0, "2026-07-28T14:05:00Z"),     # one hourly bucket
                     (PHONE, 1000.0, "2026-07-28T19:00:00Z")]

    plan = ah.build_plan(conn, scan["records"])
    assert plan["origin_overlap"] == {"steps": {"origins": sorted([PHONE, WATCH]),
                                                "days": 1}}
    lines = "\n".join(ah.report_lines(scan, plan))
    assert "steps origins:" in lines and "would double-count" in lines
    assert f"{PHONE} (2)" in lines and f"{WATCH} (1)" in lines
    # the phone's in-bed night has no value and so is not a second sleep origin
    assert "sleep_session origins" not in lines


def test_heart_rate_is_one_row_per_hour_with_its_range(cfg, ah_zip):
    hr = _by_metric(ah.read_export(ah_zip, cfg)["records"])["heart_rate"]
    assert len(hr) == 1
    assert (hr[0]["value"], hr[0]["unit"]) == (70.0, "bpm")
    assert json.loads(hr[0]["value_json"]) == {"min": 60, "max": 80, "samples": 3}
    assert (hr[0]["started_at"], hr[0]["ended_at"]) == ("2026-07-28T14:00:00Z",
                                                        "2026-07-28T14:20:00Z")


def test_meals_fold_their_macros_and_a_correlated_copy_counts_once(cfg, ah_zip):
    scan = ah.read_export(ah_zip, cfg)
    food = sorted(_by_metric(scan["records"])["nutrition"], key=lambda r: r["value"])

    assert [r["value"] for r in food] == [120.0, 300.0, 650.0]       # kJ -> kcal
    assert len({r["external_id"] for r in food}) == 3       # same instant, distinct
    bowl = json.loads(food[2]["value_json"])
    assert bowl == {"protein_g": 42.5, "fat_g": 20.0, "carbs_g": 55.0,
                    "meal_name": "Chicken bowl"}
    assert food[1]["value_json"] is None                    # logged on its own
    assert json.loads(food[0]["value_json"]) == {"protein_g": 10.0}
    assert any("present only inside a Correlation" in n for n in scan["notes"])
    unmapped = {u["table"] for u in scan["unmapped"]}
    assert Q + "DietaryProtein" not in unmapped             # folded, not dropped


def test_a_night_on_phone_and_watch_counts_once(cfg, ah_zip):
    sleep = {r["origin_app"]: r for r in _by_metric(
        ah.read_export(ah_zip, cfg)["records"])["sleep_session"]}

    watch = sleep[WATCH]
    assert (watch["value"], watch["unit"]) == (390.0, "min")         # asleep only
    assert json.loads(watch["value_json"]) == {
        "stages": {"awake": 30.0, "light": 240.0, "deep": 90.0, "rem": 60.0}}
    phone = sleep[PHONE]
    assert phone["value"] is None and phone["unit"] is None          # in bed only
    assert json.loads(phone["value_json"]) == {"in_bed": 435.0}
    assert health.validate(phone) is None


def test_pre_ios16_and_newer_workouts_both_read(cfg, ah_zip):
    work = {json.loads(r["value_json"])["exercise_type"]: r for r in _by_metric(
        ah.read_export(ah_zip, cfg)["records"])["exercise_session"]}

    walk = work["walking"]
    assert (walk["value"], walk["unit"]) == (45.0, "min")
    assert json.loads(walk["value_json"]) == {"exercise_type": "walking",
                                              "distance_km": 3.38, "energy_kcal": 180.0}
    lift = work["traditional_strength_training"]
    assert lift["value"] == 60.0
    assert json.loads(lift["value_json"]) == {
        "exercise_type": "traditional_strength_training", "energy_kcal": 420.0,
        "avg_hr": 128.0}


def test_unmapped_types_and_unknown_units_are_reported_not_dropped(cfg, ah_zip):
    scan = ah.read_export(ah_zip, cfg)
    assert {u["table"]: u["rows"] for u in scan["unmapped"]} == {
        "ActivitySummary": 1, Q + "FlightsClimbed": 2}
    assert len(scan["skipped"]) == 1
    assert "hydration" in scan["skipped"][0] and "gal_us" in scan["skipped"][0]


def test_bucket_totals_do_not_depend_on_sample_order(cfg, tmp_path):
    a = ah.read_export(write_zip(tmp_path / "a.zip", build().render()), cfg)
    b = ah.read_export(write_zip(tmp_path / "b.zip", build(shuffle_seed=7).render()), cfg)
    strip = lambda recs: [{k: v for k, v in r.items() if k != "source_file"} for r in recs]
    assert strip(a["records"]) == strip(b["records"])


def test_the_reader_needs_no_dtd_survives_a_bom_and_names_the_real_line(cfg, tmp_path):
    broken_dtd = DTD.replace("<!ATTLIST HealthData\n  locale CDATA #REQUIRED\n>",
                             "<!ATTLIST HealthData locale CDATA #REQUIRED")
    xml = build().render(dtd=broken_dtd)
    p = tmp_path / "export.xml"
    p.write_bytes(b"\xef\xbb\xbf" + xml.encode("utf-8"))
    assert len(ah.read_export(p, cfg)["records"]) == 17

    lines = xml.splitlines()
    bad = next(i for i, line in enumerate(lines) if "<Record" in line)
    lines[bad] = lines[bad].replace("/>", ">")                # unclosed element
    p.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(DomainError) as err:
        ah.read_export(p, cfg)
    assert "export.xml line" in str(err.value)
    line_no = int(str(err.value).split("line ")[1].split()[0])
    assert line_no > bad                                       # past the DTD, real numbering


def test_a_clinical_document_or_a_foreign_zip_is_refused_by_name(cfg, tmp_path):
    cda = tmp_path / "export_cda.xml"
    cda.write_text('<?xml version="1.0"?><ClinicalDocument/>', encoding="utf-8")
    with pytest.raises(DomainError, match="root element is <ClinicalDocument>"):
        ah.read_export(cda, cfg)
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("photos/list.xml", "<Photos/>")
    with pytest.raises(DomainError, match="contains no Apple Health export.xml"):
        ah.read_export(z, cfg)


# ---------------------------------------------------------------- identity / apply

def test_an_export_from_another_zone_and_unit_preference_is_all_identical(cfg, conn, ah_zip,
                                                                         tmp_path):
    """The key property: the export renders dates in the phone's zone AT EXPORT
    TIME and values in its PREFERRED units. Taken again while travelling
    (-0700) with the phone switched to kg/km/kcal/L, nothing may re-key or
    differ — or every record would come back as a conflict."""
    first, _ = _import(conn, cfg, ah_zip)
    (tmp_path / "later").mkdir()
    later = ah.read_export(write_zip(
        tmp_path / "later" / "export.zip",
        build(metric_units=True).render(offset="-0700",
                                        exported="2026-08-02T18:00:00Z")), cfg)
    assert later["source_file"] == "export.zip#2026-08-02T18:00:00Z"

    plan = ah.build_plan(conn, later["records"])
    assert not plan["new"] and not plan["updates"] and not plan["conflicts"]
    assert plan["identical"] == len(first["records"]) == 17


def test_a_re_entered_sample_with_a_newer_creation_date_updates(cfg, conn, ah_zip, tmp_path):
    _import(conn, cfg, ah_zip)
    (tmp_path / "v2").mkdir()
    again = write_zip(tmp_path / "v2" / "export.zip",
                      build(newer_weight=True).render(exported="2026-07-29T12:00:00Z"))
    plan = ah.build_plan(conn, ah.read_export(again, cfg)["records"])

    assert len(plan["updates"]) == 1 and not plan["new"] and not plan["conflicts"]
    row, rec, changes = plan["updates"][0]
    assert rec["metric"] == "weight" and set(changes) == {"value"}
    ah.apply_plan(conn, plan)
    conn.commit()
    w = conn.execute("SELECT value, source_file FROM health_records "
                     "WHERE metric='weight'").fetchone()
    assert w["value"] == 84.82
    assert w["source_file"] == "export.zip#2026-07-29T12:00:00Z"


def test_rows_missing_from_a_later_full_export_are_reported_and_kept(cfg, conn, ah_zip,
                                                                    tmp_path):
    _import(conn, cfg, ah_zip)
    (tmp_path / "v2").mkdir()
    later = ah.read_export(write_zip(tmp_path / "v2" / "export.zip",
                                     build(drop_water=True).render()), cfg)
    plan = ah.build_plan(conn, later["records"])
    assert plan["absent"] == {"hydration": 1}
    assert any("not in this export" in line for line in ah.report_lines(later, plan))
    ah.apply_plan(conn, plan)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM health_records WHERE metric='hydration' "
                        "AND status='active'").fetchone()["c"] == 1


def test_retract_and_restore_by_the_export_key(cfg, conn, ah_zip):
    _import(conn, cfg, ah_zip)
    out = health.retract_source_file(conn, KEY)
    conn.commit()
    assert out["records"] == 17
    assert conn.execute("SELECT COUNT(*) c FROM health_records "
                        "WHERE status='active'").fetchone()["c"] == 0
    with pytest.raises(DomainError, match="export.zip#"):
        health.retract_source_file(conn, "export.zip")         # names the real key
    assert health.restore_source_file(conn, KEY)["records"] == 17


def test_doctor_stays_green_after_an_apple_import_and_its_retraction(conn, cfg, ah_zip):
    from test_doctor import _seed
    from lifecrm.doctor import run_checks
    _seed(conn, cfg)
    assert run_checks(conn, cfg)["failures"] == 0
    _import(conn, cfg, ah_zip)
    assert run_checks(conn, cfg)["failures"] == 0
    health.retract_source_file(conn, KEY)
    conn.commit()
    assert run_checks(conn, cfg)["failures"] == 0


# ---------------------------------------------------------------- detection

def test_format_is_detected_from_contents(tmp_path, ah_zip):
    from test_health_connect import make_hc_db
    db = tmp_path / "healthconnect.db"
    make_hc_db(db)
    hc_zip = tmp_path / "Health Connect.zip"
    with zipfile.ZipFile(hc_zip, "w") as zf:
        zf.write(db, "health_connect_export.db")
    folder = tmp_path / "apple_health_export"
    folder.mkdir()
    (folder / "export.xml").write_text(build().render(), encoding="utf-8")

    assert hi.detect_format(ah_zip) == hi.APPLE
    assert hi.detect_format(folder) == hi.APPLE
    assert hi.detect_format(folder / "export.xml") == hi.APPLE
    assert hi.detect_format(hc_zip) == hi.HEALTH_CONNECT
    assert hi.detect_format(db) == hi.HEALTH_CONNECT


def _no_onedrive(monkeypatch, home):
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(platforms.Path, "home", classmethod(lambda cls: home))


def test_find_exports_takes_the_newest_real_export_and_names_icloud_stubs(tmp_path,
                                                                         monkeypatch):
    home = tmp_path / "home"
    _no_onedrive(monkeypatch, home)
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    old = write_zip(downloads / "export.zip", build().render())
    new = write_zip(downloads / "export (1).zip", build().render())
    import os
    os.utime(old, (1_700_000_000, 1_700_000_000))
    with zipfile.ZipFile(downloads / "export_photos.zip", "w") as zf:
        zf.writestr("list.txt", "not health")                 # passed over
    icloud = platforms.icloud_drive()
    icloud.mkdir(parents=True)
    (icloud / ".export.zip.icloud").write_bytes(b"")

    found = hi.find_exports()
    assert found["found"] == [new, old]
    assert [p.name for p in found["placeholders"]] == [".export.zip.icloud"]
    assert downloads in found["looked"]

    found["found"] = []
    msg = hi.not_found_message(found)
    assert str(downloads) in msg and "not downloaded yet: export.zip" in msg


# ---------------------------------------------------------------- CLI

def test_cli_imports_a_zip_a_folder_or_the_xml_alike(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0

    z = write_zip(tmp_path / "export.zip", build().render())
    r = runner.invoke(app, ["health", "import", str(z)])
    assert r.exit_code == 0, r.output
    assert f"apple-health export: {KEY}" in r.output
    assert "sleep_session: 2 new" in r.output
    assert "steps origins:" in r.output
    assert "dry run — nothing written" in r.output

    folder = tmp_path / "unzipped" / "apple_health_export"
    folder.mkdir(parents=True)
    (folder / "export.xml").write_text(build().render(), encoding="utf-8")
    r = runner.invoke(app, ["health", "import", str(folder), "--apply"])
    assert r.exit_code == 0, r.output
    assert "applied: 17 added" in r.output

    r = runner.invoke(app, ["health", "import", str(folder / "export.xml"), "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["format"] == "apple_health" and out["identical"] == 17 and out["new"] == 0

    r = runner.invoke(app, ["health", "retract", "--source-file",
                            f"apple_health_export#{EXPORTED}"])
    assert r.exit_code == 0, r.output
    assert "retracted 17 health record(s)" in r.output
    assert runner.invoke(app, ["doctor"]).exit_code == 0


def test_cli_without_a_path_uses_what_it_finds_or_says_where_it_looked(tmp_path,
                                                                      monkeypatch):
    from typer.testing import CliRunner
    from lifecrm.cli import app
    runner = CliRunner()
    cli_project(tmp_path, monkeypatch)
    home = tmp_path / "home"
    _no_onedrive(monkeypatch, home)
    assert runner.invoke(app, ["init"]).exit_code == 0

    r = runner.invoke(app, ["health", "import"])
    assert r.exit_code == 1
    assert "none found" in r.output and "Downloads" in r.output

    (home / "Downloads").mkdir(parents=True)
    z = write_zip(home / "Downloads" / "export.zip", build().render())
    r = runner.invoke(app, ["health", "import"])
    assert r.exit_code == 0, r.output
    assert f"using {z}" in r.output and "dry run" in r.output


def test_a_re_zipped_export_one_folder_deeper_still_reads(cfg, tmp_path):
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Downloads/apple_health_export/export.xml",
                    build().render().encode("utf-8"))
        zf.writestr("Downloads/apple_health_export/export_cda.xml",
                    '<?xml version="1.0"?>\n<ClinicalDocument xmlns="urn:hl7-org:v3"/>\n')
    scan = ah.read_export(z, cfg)
    assert scan["inner_db"] == "Downloads/apple_health_export/export.xml"
    assert len(scan["records"]) == 17
    assert hi.detect_format(z) == hi.APPLE

# Nutrition (Cronometer, MyFitnessPal)

"If you know every single thing that you eat, it tells you exactly what
you're deficient in." A food diary already knows, food by food and with ~80
nutrients each; this page is about getting it out of the app and setting it
beside the reference intakes. The `nutrition` connector reads Cronometer's
export (and, more thinly, MyFitnessPal's), and `lifecrm nutrients` compares
the averages with the National Academies' Dietary Reference Intakes — offline,
deterministic, every number cited, no model, no account, no recurring cost.

**`lifecrm nutrients` is a comparison with reference values, not medical
advice or a diagnosis, and it recommends nothing** — no supplements, no
doses, no foods. See ["What the report is not"](#what-the-report-is-not).

Food records land in the **observed tier**: bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited.
Nothing here can touch `entries`, `measurements` or `mentions`, and nothing
here writes `health_records` (that tier is Health Connect's and Apple
Health's). See [docs/observed-tier.md](observed-tier.md).

## Getting the export

### Cronometer (recommended — both combos)

Cronometer has an Android and an iPhone app on one account, so the export is
the same file on either combo. The dependable place to take it is the
website, on the laptop:

1. cronometer.com → sign in → **Settings** (gear) → **Account** → **Export
   Data**.
2. Pick the date range (the last month is plenty for a 30-day report; the
   first time, take everything).
3. Download **Food & Recipe Entries** → `servings.csv` and **Daily
   Nutrition** → `dailysummary.csv`. Exercises, Biometrics and Notes can be
   downloaded too; the connector recognises and skips them.
4. Put the two CSVs in one folder (`~/Downloads/cronometer/` is fine) and
   import the **folder** — see below for why.

From the phone apps: More → Settings → Account → Export Data, which builds
the same CSVs and hands them to the share sheet (save to OneDrive / iCloud
Drive / Files). *Not verified against the current app versions* — if the menu
has moved, use the website.

Two Cronometer settings make the report better:

- **Timestamps** (Settings → Diary; *menu name unverified*): fills the export's
  Time column, so each serving has its real time instead of local noon.
- **Mark days complete** (the "Mark Day Complete" toggle at the bottom of a
  diary day): once you use it, the report leaves out days you did not mark —
  a day you stopped logging at lunch would otherwise read as a day you barely
  ate.

### MyFitnessPal (Premium only)

MyFitnessPal's data export needs a Premium subscription: myfitnesspal.com →
Settings → **Export Data** (*path unverified; it has moved between "Reports"
and "Settings"*). It arrives by email as a zip of `Nutrition-Summary-…csv`,
`Exercise-Summary-…csv` and `Measurement-Summary-…csv`; import the zip.

Its nutrition summary has **one row per meal, not per food**, and only the
label nutrients (calories, fat, sodium, potassium, carbohydrate, fiber,
sugar, protein, and Vitamin A / C / Calcium / Iron with no unit — the app has
shown those as % of daily value, so they are kept as-is and never compared).
The report can average a day from it, but cannot name food sources, and has
nothing to say about vitamin D or magnesium. Cronometer is the better source
for this question.

## Importing

Dry-run first — always:

```bash
.venv/Scripts/python -m lifecrm observe nutrition ~/Downloads/cronometer
```

```
nutrition: cronometer (Cronometer daily summary ×1, Cronometer servings ×1)
  file dailysummary.csv: Cronometer daily summary, 42 row(s) 2026-09-10..2026-10-21
  file servings.csv: Cronometer servings, 560 row(s) 2026-09-10..2026-10-21
  food_log: 560 new, 0 changed | 2026-09-10..2026-10-21 | e.g. 2026-10-21 19:40 Jasmine Rice, Enriched, Cooked, 2.97 cup · Dinner, 609 kcal
  nutrition_day: 42 new, 0 changed | 2026-09-10..2026-10-21 | e.g. 2026-10-21, 2,628 kcal, not marked completed
  nutrients: 63 column(s) read as stable keys (docs/nutrition.md)
  converted: Fluoride (µg) -> fluoride in mg
  converted: Vitamin D (IU) -> vitamin_d in µg
  then: lifecrm nutrients — average intake set beside the reference intakes
dry run — nothing written. Re-run with --apply to write.
```

Check the span (is it the range you exported?), the sample (is that a food
you logged, at that time?), and any `kept as-is` or `not understood` line.
Then write:

```bash
.venv/Scripts/python -m lifecrm observe nutrition ~/Downloads/cronometer --apply
```

The command prints an import uuid; `lifecrm imports retract <uuid>` undoes
the whole run.

Accepted targets: one export file (recognised by its header, not its name), a
folder of them (searched recursively), or a `.zip`. Two files of the same
kind in one target (`servings.csv` and `servings (1).csv`) are refused — each
is an export of its own date range; import them one at a time, oldest first.
**Import the folder with both files rather than each file on its own**: then
the daily summary takes the "as of" of the servings' timestamps (see
"Re-importing"), which is what lets an evening export update a morning one.

With the folder set once in `config.toml`, the path can be left off:

```toml
[connectors.nutrition]
path = "~/Downloads/cronometer"     # relative paths are under the project root
```

## What gets imported

Two streams.

**`food_log`** — one observation per serving:

| field | holds |
|---|---|
| `title` | the food name as Cronometer wrote it (`Greek Yogurt, Plain, Nonfat`) — for MyFitnessPal, `<Meal> (meal total)` |
| `body` | amount and meal: `1.50 container · Breakfast` (MyFitnessPal: `MyFitnessPal meal total`, plus its note) |
| `value` / `unit` | the serving's energy, `kcal` |
| `value_json` | `meal` (Cronometer's Group / MyFitnessPal's Meal), `amount`, `category` (Cronometer's food category), `time` (only when the export had one), `nutrients` (below), `other` (cells that were not numbers, as text), `aggregate: "meal"` for a MyFitnessPal row |
| `started_at` | Day + Time in the configured timezone; local noon when the export has no time |
| `journal_date` | the diary day the app filed it under |
| `origin_app` | `cronometer` / `myfitnesspal` |

**`nutrition_day`** — one observation per day of Cronometer's daily summary:

| field | holds |
|---|---|
| `title` | `Nutrition 2026-10-20` |
| `value` / `unit` | the day's energy, `kcal` |
| `value_json` | `nutrients`, `completed` (Cronometer's Completed flag: `true` / `false` / `null`) |
| `started_at` | local noon of the day — a placeholder, never an instant |

There are no participants and no handles: a food diary names no people.
Nothing is categorised, scored or interpreted at import — that is the
report's job, and the report is regenerable.

## Nutrient keys

`nutrients` maps a stable key to `[value, unit]`:
`{"magnesium": [312.4, "mg"], "vitamin_d": [4.1, "µg"]}`. Every known
column has one key and one canonical unit, so a nutrient reads the same
whichever app or year it came from. A blank cell means "not known for this
food" and is left out — never stored as zero.

| key | unit | Cronometer column | note |
|---|---|---|---|
| `energy` | kcal | Energy (kcal) | kJ converted (÷ 4.184); MyFitnessPal Calories |
| `protein`, `carbohydrate`, `net_carbs`, `fiber`, `soluble_fiber`, `insoluble_fiber`, `starch`, `sugars`, `added_sugars`, `sugar_alcohol`, `fructose`, `galactose`, `glucose`, `lactose`, `maltose`, `sucrose` | g | Protein, Carbs, Net Carbs, Fiber, Starch, Sugars, … | |
| `fat`, `saturated_fat`, `monounsaturated_fat`, `polyunsaturated_fat`, `trans_fat`, `omega_3`, `omega_6`, `ala`, `epa`, `dha` | g | Fat, Saturated, Monounsaturated, Polyunsaturated, Trans-Fats, Omega-3, Omega-6 | |
| `cholesterol` | mg | Cholesterol (mg) | |
| `alcohol`, `water` | g | Alcohol (g), Water (g) | |
| `caffeine` | mg | Caffeine (mg) | |
| `thiamin`, `riboflavin`, `niacin`, `pantothenic_acid`, `vitamin_b6` | mg | B1 (Thiamine), B2 (Riboflavin), B3 (Niacin), B5 (Pantothenic Acid), B6 (Pyridoxine) | |
| `vitamin_b12`, `biotin`, `folate`, `folic_acid` | µg | B12 (Cobalamin), Biotin, Folate | folate as Cronometer reports it (DFE) |
| `choline` | mg | Choline (mg) | |
| `vitamin_a`, `retinol`, `alpha_carotene`, `beta_carotene`, `beta_cryptoxanthin`, `lutein_zeaxanthin`, `lycopene` | µg | Vitamin A (µg), Retinol, Beta-carotene, … | vitamin A as RAE |
| `vitamin_a_iu`, `vitamin_e_iu` | IU | Vitamin A (IU), Vitamin E (IU) | **not converted**: the IU factor depends on the form (retinol vs carotene, natural vs synthetic tocopherol), which the export does not state; kept under its own key, never summed with µg / mg, never compared |
| `vitamin_c`, `vitamin_e` | mg | Vitamin C (mg), Vitamin E (mg) | |
| `vitamin_d` | µg | Vitamin D (IU) | **IU ÷ 40** (exact by definition); a µg column is taken as-is |
| `vitamin_k` | µg | Vitamin K (µg) | |
| `calcium`, `copper`, `iron`, `magnesium`, `manganese`, `phosphorus`, `potassium`, `sodium`, `zinc` | mg | Calcium (mg), Copper (mg), … | |
| `fluoride` | mg | Fluoride (µg) | **µg ÷ 1000** — mg is the unit its reference intake uses |
| `chromium`, `iodine`, `molybdenum`, `selenium` | µg | Chromium (µg), Iodine (µg), … | |
| `cystine`, `histidine`, `isoleucine`, `leucine`, `lysine`, `methionine`, `phenylalanine`, `threonine`, `tryptophan`, `tyrosine`, `valine` | g | the amino-acid columns | |

Unit spellings `µg`, `μg` (Greek mu), `mcg` and `ug` are all read as µg. A
column **not in this table** — a nutrient Cronometer adds next year — is
**kept as-is**: its header is its key (`"Oxalate (mg)": [12.0, "mg"]`), with
the unit it stated, and the dry run names it. So is a column whose unit does
not convert (`% DV`) and one that states no unit at all. Nothing is dropped.
The table lives in `lifecrm/connectors/nutrition.py` (`NUTRIENTS`).

## Dates and times

- `journal_date` is **the diary day the app filed the food under** — the
  day you chose — and the rollover hour is *not* applied again. A 00:40
  snack Cronometer files under Tuesday stays Tuesday, so a day's servings
  always add up to that day's summary.
- `started_at` is Day + Time in the configured `[time] timezone` (the export
  states no zone, so a meal logged while travelling is read in the home
  zone), and **local noon** when the export has no time; `value_json.time`
  is present only when the export had one.
- Times are read as `08:05`, `8:05 AM`, `20:05:30` or `8:05 p.m.`; one that
  does not parse is placed at noon and counted in the report.

## Re-importing, and what wins

- **Identity.** A serving is `cronometer:serving:` + sha1(day, time, group,
  food, amount, ordinal) — the ordinal keeps two identical coffees on one
  day as two servings, and overlapping exports give the same ids for the
  same rows, so the overlap is a no-op. A daily summary is
  `cronometer:day:<date>`; a MyFitnessPal meal
  `myfitnesspal:meal:<date>:<meal>:<ordinal>`.
- **Newer wins.** The exports carry no edit times, so every record's
  `last_modified_at` is the export's own "as of" — the latest instant it
  describes. A later export updates what changed (today's half-logged
  summary becomes tomorrow's export of the whole day, audited); an older
  export overwrites nothing, and a difference is reported as *kept
  existing*. Unchanged rows only have their "as of" moved forward.
- **Same-day re-exports.** Two exports taken on the same day without
  timestamps describe the same instant, so the second cannot update the
  first; the next day's export does. Importing the folder (both files in one
  run) with Cronometer's timestamps on avoids this: the summary then takes
  the latest serving's time as its "as of".
- **Edited servings.** A serving you change in Cronometer after importing it
  (another amount, another time) arrives as a new serving; the old one stays
  until its import is retracted. The daily summary — which the report
  prefers — is updated in place either way, so averages stay right; only the
  food-source shares can double count the edited food.

## Nothing is mirrored

An export is a date range you choose, not the diary's whole state: the days
absent from this month's file are in last month's. Nothing is ever removed
because a later export does not contain it — the same rule as finance and
location. `lifecrm imports retract <uuid>` undoes a run that should not have
happened.

## The nutrient report: `lifecrm nutrients`

```bash
.venv/Scripts/python -m lifecrm nutrients                       # the last 30 days
.venv/Scripts/python -m lifecrm nutrients --days 90
.venv/Scripts/python -m lifecrm nutrients --start 2026-09-01 --end 2026-09-30
.venv/Scripts/python -m lifecrm nutrients --sex female --age 34 # override the config
.venv/Scripts/python -m lifecrm nutrients --json                # the whole report
```

Say whose reference values to use, once:

```toml
[nutrition]
sex = "male"        # "female" | "male"
age = 32            # whole years, 19 or over — update it on the birthdays that change a band (31, 51, 71)
```

Without them, each reference is the **higher of the female and male
values**, for ages 19-50 — the cautious default — and the report says so
(iron's 18 mg is then the female 19-50 value, for instance).

What it does, in order:

1. **Days.** Every day in the window with nutrition data. A day with a
   Cronometer daily summary uses it; a day with servings but no summary is
   the sum of its servings (MyFitnessPal meal totals sum the same way).
   **Today is left out** unless marked completed — it is still being
   logged. Once any day in the window is marked completed, **days whose
   Completed flag is false are left out** too; if none is marked, every day
   counts and the report says that a half-logged day pulls the averages
   down. Days with no
   food logged are not counted (the average is per *logged* day).
2. **Averages.** Each nutrient's mean per counted day. A nutrient missing
   from one day's export counts as zero that day, and the line says so
   (`on 12 of 28 days`); a nutrient the export never reports (Cronometer
   leaves iodine blank for most foods) is listed as *not in the export* —
   never called low.
3. **Comparison.** Each average beside the RDA or AI for the profile:
   - **low** — below 70% of the reference;
   - **borderline** — 70% to 100%;
   - **meets** — 100% or more;
   - **high** — the *average* is above the upper level (UL; for sodium the
     CDRR). Percentages are rounded to a tenth before banding, as printed.
4. **Days over.** Every day a nutrient went over its upper level, with the
   day's value — the taco nights, in the demo.
5. **Sources.** For each low, borderline or high nutrient (and for the days
   over), the three foods that supplied most of it, from `food_log`, with
   their share — "most of your magnesium came from …". MyFitnessPal's meal
   totals name no foods.
6. **Citations.** Every line ends with the observation ids behind it:
   `[nutrition_day #573-578, #580-601]`, or `[food_log sums #…]` for a day
   summed from servings. `--json` carries the ids, dates and per-day values
   in full. Look one up with
   `lifecrm sql "SELECT * FROM observations WHERE id=573" --json`.

The demo persona (`lifecrm demo`), with `--sex male --age 32`:

```
This compares logged intake with the National Academies' Dietary Reference Intakes for healthy adults; it is not medical advice or a diagnosis, and it recommends nothing.
nutrients: 2026-09-22 to 2026-10-21 (last 30 days) — 28 of 30 day(s) counted: 28 from Cronometer daily summaries, 0 summed from food_log
  reference intakes for: male, ages 31-50 (age 32)
  skipped 2026-09-28: not marked completed in Cronometer
  skipped 2026-10-21: today — still being logged
low — below 70% of the reference intake:
  Vitamin D: 5.80 µg/day — 38.6% of the RDA 15.0 µg | most from: Salmon, Atlantic, Farmed, Baked 46%, Egg, Hard-Boiled 24%, Protein Shake, Chocolate 7% [nutrition_day #573-578, #580-601]
  Magnesium: 274 mg/day — 65.3% of the RDA 420 mg | most from: Turkey Chili, Homemade 10%, Jasmine Rice, Enriched, Cooked 9%, Instant Oatmeal, Plain, Fortified 9% [nutrition_day #573-578, #580-601]
borderline — 70-100%:
  Fiber: 29.9 g/day — 78.7% of the AI 38.0 g | most from: Turkey Chili, Homemade 24%, Black Beans, Canned, Drained 13%, Instant Oatmeal, Plain, Fortified 10% [nutrition_day #573-578, #580-601]
high — average above the upper level: none
above the upper level on some days:
  Sodium: above the CDRR 2,300 mg on 2 of 28 days: 2026-10-01 3,927 mg, 2026-10-20 4,227 mg | most from, those days: Fish Taco 31%, Salsa Roja 19%, Migas Taco 16% [#582, #601]
meets the reference (23): Vitamin A 112%, Calcium 113%, Choline 115%, Vitamin E 116%, Potassium 122%, Sodium 126%, …
no reference intake: alcohol, caffeine, cholesterol, energy, fat, monounsaturated_fat, net_carbs, polyunsaturated_fat, saturated_fat, sugars
not compared: omega_3 — the AI is for alpha-linolenic acid alone; the export's omega-3 adds EPA and DHA
not compared: water — the AI for water counts every drink and the water in food; diaries rarely log every glass
not in the export: biotin, chromium, fluoride, iodine, molybdenum
  note: an AI (adequate intake) is set where evidence was too thin for an RDA: intake below an AI says nothing about adequacy
  note: an RDA covers 97-98% of healthy people: intake below it is not a deficiency
sources: references from the National Academies' DRI reports via the NIH Office of Dietary Supplements — `--json` names each one
```

The command is read-only (a read-only connection; nothing is written,
nothing is logged), model-free, and deterministic: the same database and
arguments give the same report. With nothing imported it says how to import.

## Reference intakes and their sources

Adult values (19 and over; pregnancy and lactation are not covered),
embedded in `lifecrm/nutrients.py` (`REFERENCES`) with the report each comes
from. The National Academies (formerly the Institute of Medicine) set them;
the NIH Office of Dietary Supplements publishes the tables and a fact sheet
per nutrient (ods.od.nih.gov → Health Information → Nutrient Recommendations
and Databases). M / F = male / female.

| nutrient | kind | adult reference | upper level | source |
|---|---|---|---|---|
| protein | RDA | M 56 g, F 46 g | — | IOM 2005 (0.8 g/kg of a reference weight) |
| carbohydrate | RDA | 130 g | — | IOM 2005 |
| fiber | AI | 19-50: M 38 g, F 25 g; 51+: M 30, F 21 | — | IOM 2005 (14 g / 1,000 kcal) |
| vitamin A | RDA | M 900 µg RAE, F 700 | 3,000 µg — preformed vitamin A only: **not compared** | IOM 2001 |
| vitamin C | RDA | M 90 mg, F 75 | 2,000 mg | IOM 2000 |
| vitamin D | RDA | 15 µg (600 IU); 71+: 20 µg | 100 µg | IOM 2011 |
| vitamin E | RDA | 15 mg alpha-tocopherol | 1,000 mg — supplements only: **not compared** | IOM 2000 |
| vitamin K | AI | M 120 µg, F 90 | — | IOM 2001 |
| thiamin (B1) | RDA | M 1.2 mg, F 1.1 | — | IOM 1998 |
| riboflavin (B2) | RDA | M 1.3 mg, F 1.1 | — | IOM 1998 |
| niacin (B3) | RDA | M 16 mg NE, F 14 | 35 mg — supplements and fortified foods only: **not compared** | IOM 1998 |
| pantothenic acid (B5) | AI | 5 mg | — | IOM 1998 |
| vitamin B6 | RDA | 19-50: 1.3 mg; 51+: M 1.7, F 1.5 | 100 mg | IOM 1998 |
| biotin (B7) | AI | 30 µg | — | IOM 1998 |
| folate | RDA | 400 µg DFE | 1,000 µg — synthetic folic acid only: **not compared** | IOM 1998 |
| vitamin B12 | RDA | 2.4 µg | — | IOM 1998 |
| choline | AI | M 550 mg, F 425 | 3,500 mg | IOM 1998 |
| calcium | RDA | 19-50: 1,000 mg; 51-70: M 1,000, F 1,200; 71+: 1,200 | 19-50: 2,500 mg; 51+: 2,000 | IOM 2011 |
| chromium | AI | 19-50: M 35 µg, F 25; 51+: M 30, F 20 | — | IOM 2001 |
| copper | RDA | 0.9 mg (900 µg) | 10 mg | IOM 2001 |
| fluoride | AI | M 4 mg, F 3 | 10 mg | IOM 1997 |
| iodine | RDA | 150 µg | 1,100 µg | IOM 2001 |
| iron | RDA | M 8 mg; F 19-50: 18, 51+: 8 | 45 mg | IOM 2001 |
| magnesium | RDA | 19-30: M 400 mg, F 310; 31+: M 420, F 320 | 350 mg — supplements and medicines only, not food: **not compared** | IOM 1997 |
| manganese | AI | M 2.3 mg, F 1.8 | 11 mg | IOM 2001 |
| molybdenum | RDA | 45 µg | 2,000 µg | IOM 2001 |
| phosphorus | RDA | 700 mg | 19-70: 4,000 mg; 71+: 3,000 | IOM 1997 |
| potassium | AI | M 3,400 mg, F 2,600 | — | NASEM 2019 |
| selenium | RDA | 55 µg | 400 µg | IOM 2000 |
| sodium | AI | 1,500 mg | **CDRR 2,300 mg** — the 2019 report replaced sodium's UL with a Chronic Disease Risk Reduction intake | NASEM 2019 |
| zinc | RDA | M 11 mg, F 8 | 40 mg | IOM 2001 |

Reports: IOM 1997 *DRIs for Calcium, Phosphorus, Magnesium, Vitamin D, and
Fluoride*; IOM 1998 *… Thiamin, Riboflavin, Niacin, Vitamin B6, Folate,
Vitamin B12, Pantothenic Acid, Biotin, and Choline*; IOM 2000 *… Vitamin C,
Vitamin E, Selenium, and Carotenoids*; IOM 2001 *… Vitamin A, Vitamin K,
Arsenic, Boron, Chromium, Copper, Iodine, Iron, Manganese, Molybdenum,
Nickel, Silicon, Vanadium, and Zinc*; IOM 2005 *… Energy, Carbohydrate,
Fiber, Fat, Fatty Acids, Cholesterol, Protein, and Amino Acids*; IOM 2011
*… Calcium and Vitamin D*; NASEM 2019 *… Sodium and Potassium*.

Not compared, on purpose: **water** (its AI counts every drink and the water
in food; diaries rarely log every glass), **omega-3 / omega-6** (the AIs are
for alpha-linolenic and linoleic acid alone; the export's totals add EPA and
DHA), and anything without a DRI — energy, fat, saturated fat, sugars,
cholesterol, caffeine, alcohol — which the report lists as *no reference
intake*.

## What the report is not

- **Not medical advice, not a diagnosis.** It sets a logged average beside a
  population reference value. An RDA is set to cover 97-98% of healthy
  people, so intake below it is not a deficiency; an AI is set where the
  evidence was too thin for an RDA, so intake below an AI says nothing about
  adequacy. A deficiency is diagnosed by a clinician, usually with a blood
  test — never by a food diary.
- **It recommends nothing.** No supplements, no doses, no foods to eat or
  avoid. It names which logged foods supplied a nutrient, because that is a
  fact about the diary; what to do about it is the owner's (and their
  doctor's) call.
- **It says nothing about focus or energy.** Linking a nutrient to how you
  feel is interpretation. The `/advise health` skill may turn a low line
  into a question to ask a clinician, or a small experiment to try with
  one — never into a prescription.
- **It is only as good as the logging.** A forgotten snack, a recipe with
  blank micronutrients, a restaurant meal entered as calories only — each
  lowers the averages. The report says how many days it counted and which it
  left out; read that line first.

## For ask and the weekly review

`lifecrm/nutrients.py` imports nothing from `recall` or `review`, so either
can call it:

```python
from lifecrm import nutrients
window = nutrients.resolve_window(cfg, days=30)           # or start=/end=
report = nutrients.build_report(conn, cfg, window)        # read-only connection is enough
report["low"], report["borderline"], report["high"]       # keys, worst first
report["over_on_some_days"]                               # keys with days_over
item = next(x for x in report["nutrients"] if x["key"] == "magnesium")
item["cites"]["nutrition_day"], item["cites"]["food_log"] # observation ids
```

Wiring it into `ask` ("what am I low on", "am I deficient in anything",
"how much protein am I eating") and into the weekly review's health lens is
the integrator's job; each cited line maps to O# observation ids.

## Health Connect and Apple Health

Cronometer (and MyFitnessPal) can write food to Health Connect and Apple
Health. The health importers (`lifecrm health import`) keep a `nutrition`
metric in `health_records` with energy and the macros only (protein, fat,
carbs, sugar). Both platforms hold more — Health Connect's NutritionRecord
has columns for vitamins and minerals (vitamin D, magnesium, …), and Apple
Health has `HKQuantityTypeIdentifierDietary*` types for ~38 nutrients — so a
later change could let the report fall back to those when no Cronometer
export exists. Until then: import Cronometer here **or** let it sync to the
health app, not both, for intake questions — or the same meal is counted
in two places.

## Known limitations

- The export states no timezone; meals logged while travelling are read in
  the home zone (the diary day is still the one Cronometer filed them
  under).
- Same-day re-exports without timestamps cannot update each other (see
  "Re-importing"); the next day's export does.
- An edited serving is a new serving; food-source shares can double count it
  until the older import is retracted.
- MyFitnessPal: meal totals only, label nutrients only, Premium required,
  and its unit-less Vitamin A / C / Calcium / Iron are never compared.
- Cronometer's biometrics (weight, blood pressure) are skipped — the health
  importers own those metrics — and so are exercises and notes.
- The reference table covers adults, not pregnancy or lactation, and does
  not adjust for body size or activity.

## First real import — check these

The connector was written from Cronometer's documented export columns and
MyFitnessPal's commonly seen Nutrition Summary, and the demo renders them,
but neither has yet been run against a real export from this owner's
account. On the first real import, check in the dry run:

1. both files are recognised (`Cronometer servings`, `Cronometer daily
   summary`) and the span is the range you exported;
2. the sample's time is the time you logged it (Cronometer's Time format —
   `08:05` or `8:05 AM` — is unverified; both are read);
3. `converted: Vitamin D (IU) -> vitamin_d in µg` appears — or, if the export
   now writes `Vitamin D (µg)`, no conversion line for it;
4. no `kept as-is` columns you expected to be known (a renamed column is a
   one-line alias in `NUTRIENTS`);
5. `lifecrm nutrients` counts the days you expect, and the Completed flag
   is read (`marked completed` in the dry run's sample).

Anything off — say so; a fixed column is a one-line change.

## Commands

```bash
.venv/Scripts/python -m lifecrm connectors                          # what's available, last run
.venv/Scripts/python -m lifecrm observe nutrition <folder|file|zip>  # dry run
.venv/Scripts/python -m lifecrm observe nutrition <...> --apply
.venv/Scripts/python -m lifecrm observe nutrition                    # the [connectors.nutrition] path
.venv/Scripts/python -m lifecrm nutrients [--days N | --start D [--end D]] [--sex S --age N] [--json]
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
```

On the Mac the interpreter is `.venv/bin/python`; everything else is the same.

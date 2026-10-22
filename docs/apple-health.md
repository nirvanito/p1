# Health data from your iPhone (Apple Health)

The Health app is the iPhone's health hub: the iPhone itself, an Apple Watch,
and most fitness, sleep and food apps (MyFitnessPal, Strava, AutoSleep, smart
scales) write their samples into it. As on Android, the hub is the one
integration worth having — but Apple offers no API a Mac or PC can call.
What exists, and what this pipeline uses, is the Health app's own **Export
All Health Data**: a zip (`export.zip`) around one large XML file.

```
iPhone + Watch + apps -> Health app -> Export All Health Data -> export.zip
     -> AirDrop / iCloud Drive / OneDrive to the computer
     -> python -m lifecrm health import              (finds it; dry-run report)
     -> python -m lifecrm health import <file> --apply
```

The rows land in the same `health_records` table, under the same metric
names and units, as Health Connect's on the Android combo — so `status`,
`doctor`, the Health dashboard and `ask` work the same whichever phone the
data came from.

## Exporting on the iPhone

1. Open **Health**, tap your **profile picture** (top right).
2. Scroll down, tap **Export All Health Data**, confirm **Export**. It takes
   a minute or several — years of watch data make an export of hundreds of
   MB. Keep the app open until the share sheet appears.
3. Send it to the computer:
   - **Mac:** AirDrop it. It lands in `~/Downloads` as `export.zip` (Safari
     or Finder may unzip it into an `apple_health_export` folder — either
     works).
   - **Mac or Windows:** **Save to Files → iCloud Drive** (Finder shows it
     under iCloud Drive; on Windows, iCloud for Windows syncs it to
     `%USERPROFILE%\iCloudDrive`), or save it to the OneDrive app.

Every export is a full snapshot of everything Health holds, so there is no
need to keep old ones — the newest export is all you import.

## Importing

With no file named, `health import` looks in Downloads, iCloud Drive and
OneDrive for `export*.zip`, `apple_health_export*` and `Health Connect*.zip`,
takes the newest one whose contents really are a health export, and says
which it used — or, if it finds none, names every folder it looked in (and
any `export.zip` that is still only an iCloud placeholder, not downloaded).

```
# Mac
.venv/bin/python -m lifecrm health import
.venv/bin/python -m lifecrm health import ~/Downloads/export.zip
# Windows
.venv\Scripts\python.exe -m lifecrm health import "%USERPROFILE%\iCloudDrive\export.zip"
```

The same command reads a Health Connect export; the format is decided from
the file's contents, not its name.

Always dry-run first — that IS the review step for this pipeline. The report
looks like:

```
apple-health export: export.zip#2026-10-22T02:30:00Z (xml: apple_health_export/export.xml, 17 record types)
  sleep_session: 84 new, 0 changed | 2026-09-10..2026-10-21 | e.g. 2026-10-21 443.4 min (awake 12, deep 100, light 255.4, rem 88) from Alex’s Apple Watch
  steps: 588 new, 0 changed | 2026-09-10..2026-10-21 | e.g. 2026-10-21 total 11338 steps (14 record(s))
  weight: 14 new, 0 changed | 2026-09-12..2026-10-21 | e.g. 2026-10-21 78.2 kg from Health
  ...
  steps origins: Alex’s Apple Watch (640), Alex’s iPhone (812) — 37 day(s) recorded by more than one: daily totals would double-count
  unmapped types (present, not imported): ActivitySummary (42), FlightsClimbed (11), HeartRateVariabilitySDNN (42)
dry run — nothing written. Re-run with --apply to write.
```

**Read the samples** against what the Health app shows for that day. A unit
surprise (a weight of 186 "kg", a distance three times too long) or a
wrong-looking date means stop and report it — nothing has been written. When
the numbers look right, re-run with `--apply` (and the path it reported).

The first line's `export.zip#2026-10-22T02:30:00Z` is this export's **key**:
every Apple export is called `export.zip`, so the export's own timestamp is
appended to tell them apart. It is what `health retract` takes.

## Undoing an import

```
python -m lifecrm health retract --source-file "export.zip#2026-10-22T02:30:00Z"
python -m lifecrm health restore --source-file "export.zip#2026-10-22T02:30:00Z"
```

Same semantics as Health Connect (see `docs/health-connect.md`): retract
hides every record whose current values came from that export, keeps them on
disk, and `restore` puts them back; re-importing does not undo a retraction.
A wrong key is refused with the list of keys actually imported.

## What comes in

| Health app type                                   | metric              | stored as | notes |
|---------------------------------------------------|---------------------|-----------|-------|
| Sleep (InBed / Asleep / Core / Deep / REM / Awake) | sleep_session      | minutes asleep, `min` | one row per night per device; stages in detail (Core is stored as `light`, Health Connect's name) |
| Steps                                             | steps               | `count`   | one row per device per hour |
| Walking + running distance (also cycling, swimming, wheelchair) | distance | `km` | per device per hour; `activity` in detail |
| Heart rate                                        | heart_rate          | avg `bpm` | per device per hour; min/max/samples in detail (Health Connect's shape) |
| Resting heart rate                                | resting_heart_rate  | `bpm`     | |
| Active energy + resting (basal) energy            | total_calories      | `kcal`    | per device per hour, active + basal (Health Connect's TotalCaloriesBurned); the split is in detail |
| Weight                                            | weight              | `kg`      | from kg, lb, st or g |
| Body fat percentage                               | body_fat            | `%`       | HealthKit stores 0.183 for 18.3 % |
| Dietary energy                                    | nutrition           | `kcal`    | from Cal, kcal or kJ; a meal's protein / fat / carbs / sugar and its food name are folded into detail |
| Water                                             | hydration           | `ml`      | from mL, L, fl oz, cups |
| Workouts                                          | exercise_session    | duration `min` | `exercise_type` (e.g. `running`, `traditional_strength_training`), distance, energy and average heart rate in detail |

Everything else — flights climbed, HRV, respiratory rate, walking metrics,
audio exposure, Activity ring summaries, ECGs, workout routes, clinical
records — is listed in the report under *unmapped types* with counts, never
silently dropped. A value in a unit the importer does not know is skipped and
counted, never guessed.

**Why hourly rows.** The watch writes heart rate every few minutes and steps,
distance and energy in short slices — millions of samples over a few years.
Those are summed (averaged, for heart rate) into one row per device per
hour, which keeps the database small and the dashboards fast without losing
anything a daily question needs. Sums are exact, so re-importing never finds
a difference that isn't there.

**Dates.** The export writes every date in the zone the phone was in *when
you exported*, not where you were when the sample was recorded. So the
importer uses only the instant. A sample's journal date comes from its own
`HKTimeZone` (the watch stamps sleep and workouts with it) and otherwise
from `timezone` in config.toml, with the usual 05:00 rollover. Sleep is the
exception to the rollover: a night belongs to the **day you woke up**, as the
Health app shows it — waking at 04:30 is still that morning's sleep.

## iPhone + Apple Watch: two recorders

The iPhone and the watch **both** count steps and distance, and both may
estimate energy. The Health app hides this by picking one source per minute
(Data Sources & Access priority); the export does not — it contains both
devices' samples in full. The importer keeps each device's rows separately
(`origin_app` is the device name, e.g. `Alex’s Apple Watch`) and the dry-run
report says so:

```
  steps origins: Alex’s Apple Watch (640), Alex’s iPhone (812) — 37 day(s) recorded by more than one: daily totals would double-count
```

Sleep is handled already: the iPhone's Bedtime schedule writes "in bed" only,
which is stored with its in-bed minutes in detail and **no value**, so
nothing sums it next to the watch's night.

For steps, distance and energy, a day both devices recorded is never counted
twice: `ask`, the web UI, the Obsidian export and the Health dashboards add
up each device's rows and take the device with the larger total for that
day. The warning is still worth reading — that day's number is one device's
count, not both merged. Check what you have with:

```
python -m lifecrm sql "SELECT metric, origin_app, COUNT(*) n, MIN(journal_date) first, MAX(journal_date) last FROM health_records WHERE status='active' GROUP BY metric, origin_app ORDER BY metric, n DESC" --json
```

(The demo's sample export gives steps to the iPhone only, so its numbers are
right as shown; a real watch-wearer's export will show the warning.)

## Re-importing

Apple exports carry no record ids, so each row's identity is a hash of the
sample's type, the device name, and its start and end instants (the `ah1:`
prefix). That identity deliberately leaves out the value and the unit, so:

- exporting again — next week, while travelling, after switching Health to
  pounds or miles — finds everything already imported **identical**;
- a sample you deleted and re-entered at the same time comes back as an
  **update** (its creation date is newer), audit-logged;
- hourly rows that gained a late-synced sample update the same way;
- an hourly row that *lost* a sample (you deleted a workout) differs without
  being newer, so it is listed as a CONFLICT and the stored row is kept;
- a sample deleted on the phone is not removed here. Because every export is
  complete, the report counts stored Apple rows the new export no longer
  contains ("not in this export … left as they are").

## Limitations

- **Device names, not app ids.** The export names the recording device or
  app ("Alex’s Apple Watch", "MyFitnessPal", "Health"), not a bundle id, and
  that name is part of each row's identity. **Renaming the watch or phone
  re-keys everything it recorded**: the next import brings those rows in
  again as new under the new name, and the old ones stay. Avoid renaming
  devices; if it happens, report it before applying.
- Only samples with an `HKTimeZone` (typically watch sleep and workouts) keep
  their own zone; steps, heart rate and food use the configured home zone, so
  a morning walk abroad can land on the previous home-time day.
- `exercise_type` is Apple's activity name as text (`running`); Health
  Connect stores its numeric code. Both are in detail only.
- Activity ring summaries are not imported (they are Apple's own daily
  roll-ups of samples that are imported).
- An export is the whole history every time; big ones take a while to read
  (the file is streamed, so memory stays flat).

## First-real-import checklist (unproven until a real iPhone run)

The reader was written from the export's published structure and tested on
synthetic exports — Apple doesn't document it — so the first real export
must confirm:

- [ ] Export path and wording on your iOS version; how long it takes; the
      zip's size and the inner file names (`apple_health_export/export.xml`;
      some locales name it differently — the reader picks the XML whose root
      is `<HealthData>`)
- [ ] Dates: are they all rendered in the phone's *current* zone at export
      time? (Export once at home, then compare a known workout's time.)
- [ ] Which samples carry `HKTimeZone` (sleep? workouts? anything else?)
- [ ] Dry-run sample values match the Health app for the same day — sleep
      minutes, steps per device, weight, a workout's duration and distance
- [ ] Body fat: stored 0.183 → shown as 18.3 % (not 0.18 % or 1830 %)
- [ ] Food: are a meal's nutrient samples listed both inside its Food
      correlation and at top level? (Either way it counts once; the report's
      notes say which happened.)
- [ ] The `steps origins` warning: which devices write steps, distance and
      energy, and on how many days both do
- [ ] Re-export after switching a unit (Health → Weight → kg ↔ lb): the
      re-import is all identical, no conflicts
- [ ] Device names (`origin_app`) are what Settings shows, and stay stable
      across iOS updates
- [ ] What lands in "unmapped types" — candidates for the next mapper

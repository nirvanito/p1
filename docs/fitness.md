# Workouts: Strong, Hevy and Strava

The journal says "bench with Sam, three sets of five at one eighty-five";
the training app beside it holds every set of every session for years. The
`fitness` connector reads what those apps export for free — Strong's and
Hevy's CSVs, Strava's bulk archive — and turns each **workout** into one
record. It never logs in anywhere, never calls an API, and never sends
anything.

Workouts land in the **observed tier**: bulk records that never pass the
`/log` review gate, are never asserted as fact, and are always cited.
Nothing here can touch `entries`, `measurements` or `mentions`. See
[docs/observed-tier.md](observed-tier.md) for why.

## Getting the exports

Same on both combos: Strong, Hevy and Strava run on Android and iPhone and
export the same files. Get the file onto the laptop — on the iPhone, save it
to iCloud Drive (it appears on the Mac); on the Samsung, save it to Google
Drive or Downloads and copy it over, or mail it to yourself.

| app | where | file |
|---|---|---|
| Strong | app: Profile → ⚙ Settings → *Export Strong Data* → save from the share sheet | `strong.csv` — one row per set |
| Hevy | app: Profile → ⚙ Settings → *Export & Import Data* → *Export Workouts* (also on hevy.com → Settings) | `workouts.csv` — one row per set |
| Strava | **the website**, not the app: strava.com → Settings → My Account → *Download or Delete Your Account* → *Get Started* → *Request Your Archive*; the link arrives by email (it can take hours) | `export_<athlete id>.zip` — import the zip as it is |

The menu paths are written from the apps' documented export features and
have **not** been walked through on a current build of each app; if a
label differs, the file is what matters — the connector recognises it by
its header row, not its name.

## Importing

Dry-run first — always:

```bash
.venv/Scripts/python -m lifecrm observe fitness ~/Downloads/strong.csv
```

```
fitness: strong.csv (Strong CSV ×1)
  file strong.csv: Strong CSV, 6 workout(s) 2026-09-11..2026-10-19; read weights as lb ([connectors.fitness] weight_unit — the file does not say; check one set against the app)
  workout: 6 new, 0 changed | 2026-09-11..2026-10-19 | e.g. 2026-10-19 06:30 Full Body, 60 min — Squat (Barbell) 3×5 @ 205 lb (+1 warm-up); Bench Press (Barbell) 3×5 @ 175 lb; Pull Up 3×8
  totals across these: 6.4 h — compare with the app's own history
  new best: Overhead Press (Barbell) 100 lb × 8 on 2026-10-09, up from 100 lb × 6 on 2026-09-27
  new best: Bench Press (Barbell) 185 lb × 5 on 2026-10-09, up from 180 lb × 5 on 2026-09-27
  new best: Squat (Barbell) 230 lb × 5 on 2026-10-02, up from 225 lb × 5 on 2026-09-11
  new best: Leg Press 370 lb × 10 on 2026-10-02, up from 360 lb × 10 on 2026-09-11
dry run — nothing written. Re-run with --apply to write.
```

```bash
.venv/Scripts/python -m lifecrm observe fitness ~/Downloads/export_48213377.zip
```

```
fitness: export_48213377.zip (Strava activities.csv ×1)
  file activities.csv: Strava activities.csv, 4 workout(s) 2022-09-29..2026-10-06, 4 activity file(s) referenced, never parsed
  workout: 4 new, 0 changed | 2022-09-29..2026-10-06 | e.g. 2026-10-06 07:00 Morning Run, 41 min — Run 7.20 km in 41:00 (5:41 /km), +58 m, avg HR 151 / max 168, 430 kcal
  totals across these: 3.8 h, 24.6 km — compare with the app's own history
  skipped: 4 × activity file (GPS track) — never parsed; each activity keeps its path as media_path
  skipped: 1 × CSV that is not a workout export (e.g. profile.csv)
dry run — nothing written. Re-run with --apply to write.
```

Read three things: the **unit** (is 185 really pounds?), the **e.g.** line's
time (is that when the session started, in your time?), and the **totals**
against the app's own history. Then write:

```bash
.venv/Scripts/python -m lifecrm observe fitness ~/Downloads/strong.csv --apply
```

The command prints an import uuid; `lifecrm imports retract <uuid>` undoes
the whole run.

Accepted targets: one export file, Strava's zip or the folder it unzips to,
or any folder (searched recursively) or zip holding exports. Only `.csv`
files are opened, and each is recognised by its header; a CSV that is none
of the three (Strava's `profile.csv`, a bank statement) is counted and left
alone, and so is a zip inside a folder — import it on its own. With a path
set once, it can be left off:

```toml
[connectors.fitness]
path = "~/Downloads/strong.csv"   # relative paths are under the project root
weight_unit = "lb"                # or "kg": the unit Strong is set to
distance_unit = "mi"              # or "km"; defaults to mi with lb, km with kg
```

Without `path`, the connector looks for the one `strong.csv`, `workouts.csv`
or `export_*.zip` in Downloads (and OneDrive, and iCloud Drive on a Mac);
finding several, it lists them and asks for the one to import.

## Formats it knows

| app | header (recognised by) | notes |
|---|---|---|
| Strong | `Date,Workout Name,Duration,Exercise Name,Set Order,Weight,Reps,Distance,Seconds,Notes,Workout Notes,RPE` | `,` or `;` (then decimal commas); a `Weight Unit` / `Distance Unit` column, `Workout Duration`, or a unit in the header (`Weight (kg)`, `Duration (sec)`) are read too |
| Hevy | `title,start_time,end_time,description,exercise_title,superset_id,exercise_notes,set_index,set_type,weight_lbs\|weight_kg,reps,distance_miles\|distance_km,duration_seconds,rpe` | the unit is in the column name |
| Strava | `activities.csv`: `Activity ID,Activity Date,Activity Name,Activity Type,…` | read from the zip without unpacking it; the GPX/FIT/TCX tracks are never opened |

Times are read as ISO (`2026-10-09 06:30:00`, Strong), day first
(`9 Oct 2026, 06:30`, Hevy; Strava in British English) or month first
(`Oct 9, 2026, 6:30:00 AM`, Strava in US English). A slashed date
(`10/09/2026`) is refused rather than guessed day- or month-first, and so is
a month name in another language — the report shows the row.

## What gets imported

**One observation per workout** — not per set. A session is what anyone
asks about; ten years of lifting is ~1,500 workouts and ~40,000 sets, and
one row per set would bury the sessions. The sets are kept, structured,
inside the workout. Stream `workout`:

| field | holds |
|---|---|
| `title` | the workout's name (`Push Day`), Strava's activity name (`Zilker Park 5k`), else the activity type |
| `body` | a readable summary — `Bench Press (Barbell) 3×5 @ 185 lb; Overhead Press (Barbell) 3×8 @ 100 lb` or `Run 5.00 km in 27:00 (5:24 /km), +32 m, avg HR 158 / max 176, 283 kcal` — then the workout's own notes on the next line |
| `value` / `unit` | duration in minutes, `min` — Strava: moving time (its headline figure for a run), elapsed time when there is none |
| `started_at` / `ended_at` | the start; the end is start + duration (Hevy: its `end_time`; Strava: + elapsed time) |
| `media_path` | Strava only: the activity's track, relative to the export (`activities/14032001395.gpx`) |
| `origin_app` | `strong`, `hevy` or `strava` |
| `value_json` | see below |

`value_json`:

- `app`; `exercises` — `[{name, sets: [{weight, unit, reps, distance,
  distance_unit, seconds, rpe, type}], notes, superset, top_set}]`, set
  `type` being `normal`, `warmup`, `dropset` or `failure` (Strong's `W` /
  `D` / `F` set orders, Hevy's `set_type`);
- `top_set` — `{weight, unit, reps, weight_kg}`: the heaviest working set of
  that exercise in that workout (more reps breaks a tie; warm-ups never
  count);
- `total_volume_kg` (weight × reps over working sets), `distance_km`,
  `notes`, and `unit_source: "setting"` when a unit came from
  `[connectors.fitness]` rather than the file;
- Strava: `activity_id`, `activity_type`, `distance_km`, `elevation_m`,
  `avg_hr`, `max_hr`, `calories`, `moving_s`, `elapsed_s`,
  `relative_effort`, `gear`, `commute`, `private_note`.

`top_set` and `total_volume_kg` are arithmetic on the record itself — there
so a question is one query, never a judgement about which sets "really"
counted. Zero is how the apps write "not recorded" (Strong puts `0` in the
distance and seconds of every lifting set), so zeros are dropped; a set with
nothing left, and Strong's rest-timer rows, are skipped and counted.

## Units the file does not state

Strong writes weights and distances in whatever unit the app is set to and
**does not say which**. `weight_unit` (default `lb`, the US default) and
`distance_unit` (default `mi` with lb, `km` with kg) in
`[connectors.fitness]` say it; a unit column or a unit in the header always
wins. The report says whenever the setting was used, and every workout read
that way carries `unit_source: "setting"`. Check one set against the app on
the first import — a kg lifter read as lb has every PR 2.2× too light.

Hevy names its unit in the column. Strava's `activities.csv` carries **two
`Distance` columns**: the first in the athlete's display unit, the second in
meters — the meters one is read. An older export with only the first uses
`distance_unit`, and the report says so.

## Time, and the zone nobody wrote down

- **Strong and Hevy** write the phone's wall-clock time with no zone; it is
  read in the configured `[time] timezone`.
- **Strava's `Activity Date` is UTC.** `Sep 24, 2026, 11:15:00 PM` is 18:15
  in Austin.
- The journal day is the home timezone's, with the rollover hour applied —
  a 01:30 session counts toward the day before, like everything else.

None of the three files says where you were. A 9 am hike in Slovenia is
03:00 in Austin, so with a 05:00 rollover it lands on the day before; a
lift logged in Strong while travelling is read as home time. That is a
documented limitation of zone-less exports, not a bug.

## Re-importing, and what wins

- **Identity.** Strong / Hevy: `<app>:<start as the file wrote it>:<workout
  name>`, e.g. `strong:2026-10-09T06:30:00:push day`. The file's own
  wall-clock text, not the UTC instant derived from it, so changing the
  configured timezone never re-identifies a workout. Strava:
  `strava:<Activity ID>`.
- **Newer wins.** No export has a per-workout edit time, so every record is
  dated by its export's "as of" — the end of the newest workout in it. A
  later export therefore wins: a set corrected in the app, a Strava activity
  renamed, updates the stored row (audited). An older export overwrites
  nothing; a difference is a conflict and the stored workout is kept. One
  edited without any newer session logged since is a conflict too — log the
  next session, then export.
- **Overlap.** Each export is the whole history, so re-importing a newer one
  skips everything already stored. A folder holding an old and a new
  `strong.csv` counts each workout once, from the newer file, and says so.
- **Renamed workouts.** Renaming a Strong or Hevy workout after it was
  imported gives it a new id; the old row stays (below). Strava's ids
  survive renames.

## Deleted workouts: the archive remembers

The apps let a workout be deleted, and a CSV can be partial — an archive
requested mid-upload, a share sheet cut short. Nothing in the file tells a
deleted workout from a missing one, and losing a year of training to a
truncated export is the unrecoverable direction. So nothing is ever removed
because a later file does not hold it (there is no mirroring, unlike texts
and AI chats). `lifecrm imports retract <uuid>` undoes a run that should
not have happened.

## No people, no handles

No export names a training partner — not Strong's or Hevy's notes columns,
not Strava's `activities.csv` (group activities are not in it). Who you
trained with is the journal's to say ("bench with Sam"). A name typed into a
workout note stays text in the body: searchable, never a participant. This
connector creates no handles.

## Personal bests in the dry run

The report lists, per exercise, the latest workout whose heaviest working
set beat every **earlier workout in the same export**: `new best: Bench
Press (Barbell) 185 lb × 5 on 2026-10-09, up from 180 lb × 5 on 2026-09-27`.
Weights compare in kg, so a switch from lb to kg does not fake a PR. It is
computed from the scan alone — against nothing already stored — and nothing
about it is written anywhere.

## Asking about it

The stored rows answer "what's my bench PR" directly — the heaviest
`top_set` of any exercise whose name contains the word, earliest first on a
tie, cited by its row:

```sql
SELECT o.id, o.journal_date, json_extract(e.value, '$.name') AS exercise,
       json_extract(e.value, '$.top_set.weight') AS weight,
       json_extract(e.value, '$.top_set.unit') AS unit,
       json_extract(e.value, '$.top_set.reps') AS reps
FROM observations o, json_each(o.value_json, '$.exercises') e
WHERE o.stream = 'workout' AND o.status = 'active'
  AND lower(json_extract(e.value, '$.name')) LIKE '%bench%'
  AND json_extract(e.value, '$.top_set.weight_kg') IS NOT NULL
ORDER BY json_extract(e.value, '$.top_set.weight_kg') DESC,
         json_extract(e.value, '$.top_set.reps') DESC, o.started_at
LIMIT 1;
```

(`lifecrm sql "…" --json` runs it today.) `lifecrm ask` does not yet route
workout questions to this stream; the question words it would use are
*workout(s), gym, lift(ed), bench, squat, deadlift, run, ran, runs, strava,
strong, hevy, PR, personal best*.

## Known limitations

- **No zone in any file** — see "Time" above; travel sessions are dated by
  home time.
- **Strong's unit is assumed** from the setting unless the file has a unit
  column. One setting for the whole file: a history that switched from lb
  to kg in the app is read entirely in one unit.
- **A Strong duration written as a bare number** (no `h`/`m`, no
  `Duration (sec)` header) is left empty and reported, never guessed as
  seconds or minutes.
- **GPS tracks are never read**: no route, splits, laps or per-second heart
  rate — only what `activities.csv` summarises. Strava's comments, kudos,
  segments and other CSVs are not imported.
- **One session in two apps is two records** — a run in Strava and in
  Strong's cardio log, or a lift in Strong that the watch also recorded
  (Health Connect / Apple Health store that one as an `exercise_session`
  health record, the sensor tier).
- **Personal bests** in the report compare workouts within one export only.
- **Memory.** Each CSV is read whole; a decade of Strong is a few MB, and
  Strava's `activities.csv` is one row per activity. The tracks beside it
  are listed, never read, however many gigabytes they are.

## First real import — check these

The readers were written from the apps' documented and commonly seen export
layouts, and the demo renders them, but none has yet been run against a real
export from this owner's accounts. On the first real file of each app, check
in the dry run:

1. the file line names the right app;
2. **Strong:** the unit — open one workout in the app and compare its top
   set to the report's `e.g.` line; set `weight_unit = "kg"` if needed;
3. **Strong:** no `workout duration with no unit` / `does not parse` lines
   (if there are, the duration column has a form this reader does not know —
   say so);
4. **Hevy:** no `start_time that does not parse` lines;
5. **Strava:** the `e.g.` run starts at the local time the app shows. If it
   is off by exactly your UTC offset, `Activity Date` was local, not UTC —
   say so; it is a one-line change;
6. **Strava:** a run's distance matches the app (the meters column was read
   — no `distance read as mi` note);
7. the workout count matches the app's history.

## Commands

```bash
.venv/Scripts/python -m lifecrm connectors                          # what's available, last run
.venv/Scripts/python -m lifecrm observe fitness <file|zip|folder>   # dry run
.venv/Scripts/python -m lifecrm observe fitness <...> --apply
.venv/Scripts/python -m lifecrm observe fitness                     # the [connectors.fitness] path, or Downloads
.venv/Scripts/python -m lifecrm imports list
.venv/Scripts/python -m lifecrm imports retract <uuid>
```

On the Mac the interpreter is `.venv/bin/python`; everything else is the same.

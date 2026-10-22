# Health data from your Android phone (Health Connect)

Health Connect is Android's on-device health hub: Samsung Health, Fitbit,
Google Fit, Oura, sleep trackers, and most fitness apps write their data into
it. That makes it the one integration worth having — instead of chasing each
app's export, you export the hub.

There is no public web API and no plain "save to folder" export. What exists
(and what this pipeline uses) is Health Connect's built-in export: a zip
containing its raw SQLite database, written to a storage provider (Google
Drive, and possibly OneDrive — see the checklist) on a schedule or on demand.

```
apps -> Health Connect (on phone) -> export zip -> synced/downloaded to PC
     -> python -m lifecrm health import <zip>       (dry-run report)
     -> python -m lifecrm health import <zip> --apply
```

On an iPhone, the same `health import` reads Apple Health's *Export All
Health Data* file instead. It tells the two apart by the file's contents.
See [apple-health.md](apple-health.md).

## Phone-side setup (once)

1. Open **Health Connect** (Android 14+: Settings → search "Health Connect";
   Android 13: the Health Connect app).
2. Confirm your apps are connected and writing (App permissions — you should
   see Samsung Health / Fitbit / etc. with write access).
3. Find **Export/backup data** (menu names vary by Android version — record
   the exact path you find in the checklist below).
4. Choose **scheduled export** (daily is the minimum cadence) and pick a
   destination the PC can reach:
   - Google Drive → then sync that folder to the PC (Drive for desktop), or
   - OneDrive, if it appears as a destination — it lands next to the
     VoiceJournal flow you already have (verify: checklist).
   A manual one-off export to the same place works too and is the right way
   to do the first trial run.

## Importing

Always dry-run first — that IS the review step for this pipeline:

```
.venv\Scripts\python.exe -m lifecrm health import "path\to\export.zip"
```

The report shows, per metric: how many records are new/changed, the date
span, and a sample value (e.g. `weight: 1 new | 2026-07-27..2026-07-27 |
e.g. 2026-07-27 84.5 kg`). **Read the samples.** A unit surprise (a weight
of 84500, a distance of 5000) or a wrong-looking date means stop and report
it — nothing has been written. When the numbers look right:

```
.venv\Scripts\python.exe -m lifecrm health import "path\to\export.zip" --apply
```

Re-importing the same or overlapping exports is always safe: records carry
Health Connect's own record ids, so existing rows dedupe. Records the phone
edited later update only if the incoming copy is newer (audit-logged with
old and new values); a record that differs but is NOT newer is refused and
listed as a CONFLICT. Tables the importer doesn't understand are listed with
row counts — never silently dropped.

## Undoing an import

If an export lands with a wrong unit scale or wrong dates and you only notice
after `--apply`:

```
.venv\Scripts\python.exe -m lifecrm health retract --source-file "export.zip"
.venv\Scripts\python.exe -m lifecrm health restore --source-file "export.zip"
```

Retract sets `status='retracted'` on every record that export **last wrote**,
which hides them from doctor, dashboards, `status` and `ask` while keeping
every field on disk. That is the right key: a re-import overwrites
`source_file`, so it always names the export a row's current values came
from — naming the bad export catches both the rows it created and the rows it
overwrote, and leaves rows an earlier export still owns alone.

Two things to know:

- **Re-importing does not undo a retraction.** An unchanged record classifies
  as identical and is skipped whatever its status, so `health restore` is the
  way back.
- The name is the file name as `health import` reported it, not the full path.
  Get it wrong and the error lists the files actually imported.

## What comes in (v1)

| metric              | stored as                 | notes                                    |
|---------------------|---------------------------|------------------------------------------|
| sleep_session       | minutes asleep, `min`     | per-stage breakdown (light/deep/rem/awake) in detail |
| steps               | per-record count          | daily total = SUM per journal date       |
| weight              | `kg`                      | converted from Health Connect's grams    |
| heart_rate          | avg `bpm` per record      | min/max/sample-count in detail           |
| resting_heart_rate  | `bpm`                     |                                          |
| exercise_session    | duration `min`            | type id + title/notes in detail          |
| distance            | `km`                      | verify scale on first import (checklist) |
| total_calories      | `kcal`                    | converted from calories                  |
| nutrition           | `kcal` + macros in detail | protein/fat/carbs/sugar grams, meal      |
| body_fat            | `%`                       |                                          |
| hydration           | `ml`                      | verify scale on first import (checklist) |

Journal dates follow the same rule as everything else here: the record's own
timezone, with the 05:00 rollover — a 00:30 weigh-in belongs to yesterday,
and a sleep session belongs to the day you woke up.

What does NOT come in: gym set/rep/load detail (Health Connect doesn't carry
it — that's a future Strong-CSV/Hevy importer), and iPhone data (Apple
Health is a different pipeline; the friend's instance will use an
auto-export app POSTing to a small receiver, or the manual export.zip).

## First-real-import checklist (unproven until a real phone run)

The parser was verified against Android's published storage code (AOSP
HealthFitness, July 2026) and synthetic fixtures — but Google doesn't
document this database, so the first real export must confirm:

- [ ] Exact menu path and wording for scheduled export on your Android version
- [ ] Whether OneDrive is offered as an export destination (else: Drive + sync)
- [ ] The zip's filename pattern and the inner `.db` filename
- [ ] Dry-run: every mapped metric's sample value matches the app's own
      numbers (especially **distance** and **hydration** scale)
- [ ] Sleep totals within a few minutes of what the sleep app shows
- [ ] Second export a day later: previously imported records show as
      identical, not conflicts (record ids are stable across exports)
- [ ] What lands in "unmapped tables" — candidates for the next mapper
- [ ] Zip size and whether daily cadence actually fires without opening the app
- [ ] **One app per metric.** Samsung Health and Google Fit (or a watch app)
      can both write steps, sleep or calories into Health Connect; every
      day-total here — the steps and sleep dashboards, `lifecrm ask` — SUMs
      a day's records, so a second writer silently doubles those days. The
      dry-run prints `WARNING steps: written by 2 apps (...)` when the export
      shows it (`--json`: `origin_apps`), and after import doctor keeps
      warning (`health_multi_origin`). Check what actually landed:

      ```
      python -m lifecrm sql "SELECT metric, origin_app, COUNT(*) n, MIN(journal_date) first, MAX(journal_date) last FROM health_records WHERE status='active' GROUP BY metric, origin_app ORDER BY metric, n DESC" --json
      ```

      If two apps share a summed metric, fix it at the source before relying
      on the totals: in Health Connect let only one app write it (app
      permissions, or the data type's sources/priority list — the menu
      wording varies by Android version; record what yours says here).
      The importer has no per-app filter yet, and `health retract` works per
      export file, not per app — so the cleanest moment to settle this is
      before the first `--apply`. Averaged metrics (weight, heart rate, body
      fat) only get a `note` line: two writers mix readings rather than double
      them.

Once trusted, wrap the download-and-dry-run in the scheduled-task setup —
deliberately manual until then.

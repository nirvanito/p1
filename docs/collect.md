# The daily collection: `lifecrm collect`

Most sources already reach the computer by themselves: the phone's backup
app uploads to OneDrive every night, the health export lands in a cloud
folder, the browser keeps its own history file, the gallery syncs. What used
to be left was the last step: someone typing `observe … --apply` after
reading the dry run. `lifecrm collect` takes that step every morning for the
sources you list, and holds back anything that is not routine so you decide
it.

```
.venv\Scripts\python.exe -m lifecrm collect             # the sources in [collect] sources
.venv\Scripts\python.exe -m lifecrm collect --dry-run   # decide every source, write nothing
.venv\Scripts\python.exe -m lifecrm collect android_sms health   # just these, now
.venv\Scripts\python.exe -m lifecrm collect --status    # each source's last outcome
```

(macOS: `.venv/bin/python -m lifecrm …`.)

## Turning it on

1. Import each source **once by hand** first, as its doc says: the dry run,
   the first-real-import checklist at the end of `docs/<source>.md`, then
   `--apply`. The job never does a source's first import (below).
2. List the sources in `config.toml`:

   ```toml
   [collect]
   sources = ["android_sms", "health", "browser", "photos"]   # Windows + Samsung
   # health_dir = "~/OneDrive/Health Connect"   # if the phone's health export is in a subfolder
   ```

   On a Mac + iPhone: `["mac_messages", "ios_calls", "health", "browser",
   "photos", "notes", "contacts"]`. Any connector that finds its own data with
   no path given can be listed, plus `health`; `ai_chat` cannot (it needs the
   export's path every time). The `[connectors.*]` settings still decide where
   each one looks (an SMS backup folder, a browser, a calendar file).
3. Register the job: `scripts\install-tasks.ps1` (Windows) or
   `scripts/install-tasks.sh` (Mac) — re-running replaces the jobs and adds
   **LifeCRM collect** / `local.lifecrm.collect`, daily at **06:30**: before
   intake (07:00), the reminders calendar (07:15) and doctor (08:00), so all
   three see this morning's imports. It logs to `data\collect.log`; with no
   `[collect] sources` it logs one line and exits 0.

## What a run decides

Each source is read where its connector looks with no path given, and goes
through the very plan an `observe` (or `health import`) dry run shows. Then,
per source:

| outcome | when | what is written |
|---|---|---|
| `applied` | the plan is routine: new and updated records, refreshed ones, the few deletions a mirroring source made, the coverage a new file declares | the import, exactly as `--apply` writes it — one shared write path (`collect.apply_observed`), a snapshot first when anything is removed |
| `unchanged` | nothing the database does not already hold (the same backup as yesterday) | nothing |
| `held` | anything unusual — see below | nothing |
| `missing` | nothing found where the connector looks | nothing |
| `error` | the reader failed on what it found | nothing; the next source still runs |

**Held**, by rule:

- `first_import` — nothing was ever imported from this source (and account)
  before, or every import of it was retracted. The first import of every
  source is yours, with its doc's checklist: that is where a wrong unit, a
  wrong date or a misread file shows. A second phone or a new account starts
  over the same way.
- `mass_removal` — the removal `observe` would refuse: at least 5 records
  that are over 10 % of the account's archive, or two or more that are half
  of it. That looks like an incomplete backup more than like deletions. Run
  the dry run it names; if the deletions are real, `--apply
  --allow-mass-removal`.
- `invalid` — records the reader produced but cannot validate. Nothing of the
  file is written; the dry run shows them.

A **conflict** (a stored record the source now spells differently, with no
newer modification time) is never written by any import, so it holds nothing
back: the plan applies around it and the count is kept, as `observe` reports
it.

**A quiet day is a known zero.** A new full SMS backup with no new texts is
still applied: it declares the days it covers (`import_coverage`), and on
those days no text means none was sent. Re-reading yesterday's file declares
nothing new and is `unchanged`.

## Where you see it

Every outcome is a row in `collect_runs` (schema v8), written through
`lifecrm/domain/collect.py` and audited. Read from there:

- **/morning** and `lifecrm nudges`: a source whose newest run was held, found
  nothing or failed is a `collect` nudge, `collect:<source>:<why>`, resting on
  `C#` (that run's outcome). It goes by itself once a run is routine; snooze
  or dismiss it by its key like any nudge.
- **doctor**: `collect_waiting` lists those sources; `collect_last_run` warns
  when sources are configured but no run started in two days (the task was
  never registered, or stopped).
- **The Obsidian Home note** and the web UI's Today card show the same
  nudges. A collect row is bookkeeping, not content — the vault's as-of date
  ignores it — but it still re-renders the vault, so a hold shows on Home the
  morning it happens.
- `lifecrm collect --status`: each source's newest outcome, and when it last
  applied.

## Undo

An applied run is an ordinary import: `collect --status --json` and
`imports list` name it, `imports retract <uuid>` hides what it added, and a
removal's snapshot (`backups\pre-removal-*.db`, printed and kept in the run's
counts) is the undo for what it removed. Health imports retract by file:
`health retract --source-file "<name>"`.

## What it never does

- Write entries, measurements, mentions, aliases or entity links — the
  importer rule holds unattended too. Handles still wait for `handles link`.
- A source's first import, a refused removal, or anything else a dry run
  would make you stop and read.
- Go online. It reads what is already on this computer; the phone and the
  cloud drive bring it there.
- Run a model.

## Phone side, per setup

What makes each source arrive without you (the rest of each guide is the
same):

- **Windows + Samsung** (`docs/windows-android.md`): SMS Backup & Restore's
  scheduled daily backup to OneDrive (`android_sms`); Health Connect's
  scheduled export to OneDrive or Google Drive (`health`; set `health_dir`
  when it is a subfolder); Gallery › Sync with OneDrive (`photos`); Chrome
  sync brings the phone's history into the laptop's browser (`browser`).
- **Mac + iPhone** (`docs/mac.md`): Messages in iCloud fills the Mac's
  `chat.db` (`mac_messages`); with *Calls from iPhone* on, the Mac's call
  history holds the phone's calls (`ios_calls`, `docs/calls.md`); iCloud
  Contacts, Notes and Photos on the Mac (`contacts`, `notes`, `photos`).
  Apple Health has no scheduled export: export it from the Health app when
  you want it in, and the next morning's run picks up the newest one.
- **Windows + iPhone** (`docs/windows-iphone.md`): each Apple Devices backup
  feeds `ios_messages` — over Wi-Fi while the phone charges, if Apple Devices
  does that for you **(verify)**, else whenever the cable is in. There is no
  call history to collect on this setup.

Exports that can only be requested by hand (Google Takeout, Instagram,
WhatsApp chats, LinkedIn, bank statements) can still be listed: on the
morning after you download one into the place its connector looks, the job
imports it, under the same rules.

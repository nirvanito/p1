# The daily collection: `lifecrm collect`

Most sources already reach the computer by themselves: the phone's backup
app uploads to OneDrive every night, the health export lands in a cloud
folder, the browser keeps its own history file, the gallery syncs. The
collect job takes the last step every morning: it imports what is routine
and asks you about anything new. You configure nothing; all you ever do is
answer yes or no.

```
.venv\Scripts\python.exe -m lifecrm collect                    # every source on this computer
.venv\Scripts\python.exe -m lifecrm collect approve browser    # yes: shows what comes in, imports on yes
.venv\Scripts\python.exe -m lifecrm collect decline calendar   # never
.venv\Scripts\python.exe -m lifecrm collect setup              # the phone's side, as a checklist
.venv\Scripts\python.exe -m lifecrm collect status             # each source's last outcome
.venv\Scripts\python.exe -m lifecrm collect run android_sms    # just this one, now
.venv\Scripts\python.exe -m lifecrm collect --dry-run          # decide everything, write nothing
```

(macOS: `.venv/bin/python -m lifecrm …`.) In Claude Code, `/morning` asks
the questions and runs the answers for you.

## How it goes

1. **Get the phone's data onto this computer, once.** `collect setup` lists
   each source for your setup (Windows + Android, Windows + iPhone, Mac +
   iPhone; guessed from the computer, `--phone` to change it) as *done*,
   *ready* (on this computer, waiting for your yes), *to do* (with the exact
   steps, e.g. SMS Backup & Restore's nightly backup to OneDrive) or *no*
   (declined).
2. **Every morning at 06:30** the job looks wherever each source looks by
   itself. A source it cannot find is skipped without a word. A source found
   for the first time becomes a question in `/morning` (and in `lifecrm
   nudges`, the vault's Home and the web UI's Today card):

   > New on this computer: browser history. Import it?
   > 12,034 record(s) from 2023-03-02 to 2026-10-24, in …\Chrome\User Data\Default\History

3. **You answer.**
   - **yes** — `collect approve <source>` shows the very dry run `observe`
     would print, plus one summary line, and imports on your yes (`--yes`
     to skip the prompt; in Claude Code, /morning runs the preview, checks
     it against the question, and imports). From then on the job collects
     it every morning by itself.
   - **not now** — snooze the question: `nudges snooze <key> --days 7`.
   - **never** — `collect decline <source>`: the job stops looking at it.
     `collect approve` takes it back.
4. **After that, routine mornings need nothing from you.** New texts, new
   health records, new photos and pages come in; what the job held shows up
   as a question again.

Registering the job is `scripts\install-tasks.ps1` (Windows) or
`scripts/install-tasks.sh` (Mac): **LifeCRM collect** / `local.lifecrm.collect`,
daily at 06:30, before intake (07:00), the reminders calendar (07:15) and
doctor (08:00), so all three see this morning's imports. It logs to
`data\collect.log`.

## What a run decides

Each source goes through the very plan an `observe` (or `health import`)
dry run shows. Then, per source:

| outcome | when | what is written |
|---|---|---|
| `applied` | the plan is routine: new and updated records, refreshed ones, the few deletions a mirroring source made, the coverage a new file declares | the import, exactly as `--apply` writes it — one shared write path (`collect.apply_observed`), a snapshot first when anything is removed |
| `unchanged` | nothing the database does not already hold (the same backup as yesterday) | nothing |
| `held` | something only you decide — see below | nothing |
| `error` | the reader failed on a source you already collect | nothing; the next source still runs |
| `missing` | a source you *named* (`run`, or a `[collect] sources` list) found nothing | nothing |

**Held**, by rule:

- `first_import` — nothing was ever imported from this source (and account)
  before, or every import of it was retracted. That is where a wrong unit, a
  wrong date or a misread file shows, so the first import of every source is
  yours: the question says how many records and which days, and `approve`
  shows the dry run. A second phone or a new account is asked about the same
  way.
- `mass_removal` — the removal `observe` would refuse: at least 5 records
  that are over 10 % of the account's archive, or two or more that are half
  of it. That looks like an incomplete backup more than like deletions.
  `approve` shows what would go; yes imports it (a snapshot first).
- `invalid` — records the reader produced but cannot validate. Nothing of the
  file is written, and `approve` refuses too: that needs a fix to the
  reader, not a yes.

A source found for the first time that the reader cannot read, or that holds
no records, is not offered at all: there is nothing to ask.

**On a Mac, a source macOS refuses is never "not found".** Messages, calls,
Contacts, Notes, Photos, Mail and Safari sit behind Full Disk Access; until
it is granted, each one the job finds is an `error` that says so, and
/morning shows them as one item ("macOS is keeping the daily import out of
Messages on this Mac, notes. Give Full Disk Access?", key
`collect:macos:blocked`). `collect setup` marks them `!` with the two steps:
your terminal, and `/bin/bash` plus the venv's Python for the 06:30 job
(docs/mac.md, section 5). The same goes for **Downloads and iCloud Drive**,
where exports land: macOS can keep a scheduled job out of them without an
error anywhere, so the job lists each folder itself and, when it may not,
says so once as that folder's own row ("the Downloads folder"); `collect
decline downloads` silences it if you never save exports there.

**Several candidates are a question too.** Two iPhone backups on one Mac
(an iPad's, an old phone's), two camera folders, several downloaded
exports: the job never guesses between them, and says which it found. Set
the one to use in `config.toml` (`[connectors.ios_messages] path`,
`[connectors.photos] path`, …) or import it by hand.

**Messages that age out are kept.** A phone set to keep messages for 30
days or a year empties its oldest end by itself. For texts (the iPhone
backup, the Mac's Messages, SMS Backup & Restore) only a message missing
from *inside* the span the source still holds counts as deleted; older
ones aged out and stay, with the days they were in. A **conflict** (a
stored record the source now spells differently, with no newer modification
time) is never written by any import, so it holds nothing back: the plan
applies around it and the count is kept, as `observe` reports it.

**A quiet day is a known zero.** A new full SMS backup with no new texts is
still applied: it declares the days it covers (`import_coverage`), and on
those days no text means none was sent. Re-reading yesterday's file declares
nothing new and is `unchanged`.

## Settings (optional)

You need none of these. In `config.toml`:

```toml
[collect]
# sources = ["android_sms", "health"]        # only these, and nothing else is looked at
# health_dir = "D:/Exports/Health Connect"   # an extra folder for the phone's health export
```

Without `sources` every source that knows where its data lives is looked at
(all but `ai_chat`, which needs the export's path every time). Health
exports are found at the top of Downloads, iCloud Drive and OneDrive and one
folder down in OneDrive (Health Connect's scheduled export usually makes a
folder of its own); `health_dir` adds one more. The `[connectors.*]`
settings still decide where each connector looks.

## Where you see it

Every outcome is a row in `collect_runs` (schema v8), written through
`lifecrm/domain/collect.py` and audited; a skipped source leaves none. A
"never" is a dismissed nudge key, `collect:<source>:declined`, so it shows
in `nudges states` and `nudges restore` undoes it like any other.

- **/morning** and `lifecrm nudges`: each question, held removal or failure
  is a `collect` nudge, `collect:<source>:<why>`, resting on `C#` (that run's
  outcome). It goes by itself once a run is routine.
- **doctor**: `collect_waiting` lists what waits for you; `collect_last_run`
  warns when a job that ran before has not run in two days (or, with a
  `[collect] sources` list, never ran).
- **The Obsidian Home note** and the web UI's Today card show the same
  nudges. A collect row is bookkeeping, not content — the vault's as-of date
  ignores it — but it still re-renders the vault, so a question shows on
  Home the morning it is asked.

## Undo

An approved or collected import is an ordinary import: `collect status
--json` and `imports list` name it, `imports retract <uuid>` hides what it
added, and a removal's snapshot (`backups\pre-removal-*.db`, printed and
kept in the run's counts) is the undo for what it removed. Health imports
retract by file: `health retract --source-file "<name>"`. Retracting every
import of a source makes it a new source again: the next morning asks.

## What it never does

- Import a source you have not said yes to, or a removal observe would
  refuse.
- Write entries, measurements, mentions, aliases or entity links — the
  importer rule holds unattended too. Handles still wait for `handles link`.
- Go online. It reads what is already on this computer; the phone and the
  cloud drive bring it there.
- Run a model.

## Phone side, per setup

`collect setup` prints these with each one's state on this computer:

- **Windows + Samsung** (`docs/windows-android.md`): SMS Backup & Restore's
  scheduled daily backup to OneDrive (`android_sms`); Health Connect's
  scheduled export to OneDrive (`health`); Gallery › Sync with OneDrive
  (`photos`); Chrome sync brings the phone's history into the laptop's
  browser (`browser`).
- **Mac + iPhone** (`docs/mac.md`): Messages in iCloud fills the Mac's
  `chat.db` (`mac_messages`); with *Calls from iPhone* on, the Mac's call
  history holds the phone's calls (`ios_calls`, `docs/calls.md`); iCloud
  Contacts, Notes and Photos on the Mac (`contacts`, `notes`, `photos`).
  Apple Health has no scheduled export: export it from the Health app when
  you want it in, and the next morning's run picks up the newest one.
- **Windows + iPhone** (`docs/windows-iphone.md`): each Apple Devices backup
  feeds `ios_messages` — over Wi-Fi while the phone charges, if Apple Devices
  does that for you **(verify)**, else whenever the cable is in; the OneDrive
  app's Camera upload feeds `photos`. There is no call history to collect on
  this setup.

Exports that can only be requested by hand (Google Takeout, Instagram,
WhatsApp chats, LinkedIn, bank statements) are found the same way: the
morning after you download one into the place its connector looks, it is a
question.

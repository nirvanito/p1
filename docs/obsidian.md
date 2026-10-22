# Obsidian mirror

`lifecrm export --format obsidian` turns the database into an
[Obsidian](https://obsidian.md) vault you can browse on the laptop and the
phone: one note per journal day, one per person, place and thing, your open
loops, and an index — all cross-linked, so backlinks and the graph view work
with no plugins.

It is the markdown export grown up, and like every export it is **one-way**.

## Read-only by design

Nothing in the vault is ever read back into `lifecrm.db`. An edit made in
Obsidian flowing back into the database would be a second write path around
the review gate, and the architecture has exactly one (everything goes
through `python -m lifecrm ...`). To change a record, change it with the CLI
(`lifecrm entry edit`, `entry close`, `entities rename`, ...) or `/log`, then
re-export.

That makes the vault safe to point at a folder that also holds your own
notes, because the export only ever touches what it generated:

- Every generated note starts with the front-matter line
  `lifecrm: generated`, followed by a one-line comment (hidden in reading
  view) saying edits are not read back.
- `.lifecrm-manifest.json` at the vault root lists the generated notes.
- A re-export rewrites only generated notes whose content changed, deletes
  generated notes that no longer apply (a retracted import, a renamed person),
  and removes folders that deleting left empty.
- A file **without** the marker line is never written, moved or deleted —
  not even one sitting exactly where a generated note would go. The export
  reports it as `kept` and carries on.
- To keep a generated note as your own (to annotate a person's page, say),
  delete its `lifecrm: generated` line. From then on it is yours and the
  export leaves it alone; Jake's page simply stops being regenerated.

## Making the vault

Dry-run first, as with every import:

```
.venv\Scripts\python.exe -m lifecrm export --format obsidian --out "C:\Users\me\OneDrive\LifeCRM vault" --dry-run
```

```
obsidian vault C:\Users\me\OneDrive\LifeCRM vault: 61 generated note(s) (42 day(s), 17 entities) — 61 new, 0 changed, 0 unchanged, 0 removed
  + Index.md
  + Journal/2026/2026-09-10.md
  ...
dry run — nothing written. Re-run without --dry-run to write.
```

`+` is a new note, `~` a changed one, `-` one that would be removed, and
`kept` a file in the way that is not a generated note. Then run it again
without `--dry-run`. On a Mac the command is the same with
`.venv/bin/python` and a path like `~/Documents/LifeCRM vault`.

The export reads the database through a read-only connection, so it is safe
to run at any time, including while an intake or import is running.

## What is in it

```
Index.md                     recent days, people, places, ... and what was imported
Loops.md                     open tasks and plans, soonest due first
Journal/2026/2026-07-27.md   one note per day
People/Jake Morales.md       one note per entity, in a folder by type:
Places/Veracruz All Natural.md   People, Places, Organizations, Projects,
Activities/Bouldering.md         Activities, Food, Media, Topics
.lifecrm-manifest.json       the list of generated notes (hidden)
```

**A day note** has front matter for Dataview and the properties panel —
`date`, `mood`, `energy`, `sleep` (the journal's own numbers) and `tags`
(`journal` for a day you logged, `observed` for a day with imports, plus the
day's categories) — then:

- the day summary and the day's journal measurements;
- a **Health** line from the sensor tier: sleep hours, steps, workouts and
  weight, marked *observed* and naming the export file it came from;
- the entries, grouped by category, each with its kind, time of day, due
  date, closure, measurements and links to other entries; the people, places
  and things it mentions as `[[wikilinks]]`; and the transcript passage it
  was extracted from as a `> [!quote]` callout naming the source id (the
  same id `lifecrm ask` cites). The entry's uuid is in a hidden `%%` comment,
  ready for `lifecrm entry show`;
- an **Observed** section: how many rows each imported stream holds for the
  day, then up to ten notable items — texts and calls with people you have
  linked, calendar events, notes, place visits and AI-chat conversations —
  each ending in `observed` and the uuid of its `observations` row.

**An entity note** has the type, uuid and aliases in front matter (Obsidian
uses `aliases` for link suggestions), first and last seen, the latest
contact across both tiers (the newest journal entry and the newest imported
message or event, each cited), the handles linked to it (phone numbers,
email addresses), every journal day it appears in, and every day it appears
in imported data.

**Loops** lists open tasks and plans with their due dates and the day each
was logged. It deliberately uses plain bullets, not checkboxes: ticking a box
here would change nothing. Close a loop with
`lifecrm entry close <uuid> --closure done` and re-export.

The two tiers never blur: journal entries are what you reviewed and
confirmed; anything marked *observed* was bulk-imported and never reviewed,
and always carries the row it came from.

## File names

Links resolve by file name alone, wherever a note lives, so every generated
name is unique across the vault ignoring upper/lower case (Windows and macOS
both ignore it):

- Characters Windows or Obsidian links cannot hold (`< > : " / \ | ? * # ^ [ ]`)
  become `-`; trailing dots and spaces are dropped; `CON`, `NUL`, `COM1` and
  the other Windows device names get a `_`.
- Two entities that would share a name are told apart by type —
  `Jake (person)` and `jake (topic)` — then, if that still collides, by the
  start of their uuid. Links keep showing the real name
  (`[[Jake (person)|Jake]]`), and the real name is added to `aliases`.

The same database always produces byte-identical files (LF line endings,
stable ordering, no timestamps), so a re-export with nothing new writes
nothing and a synced vault does not churn.

## Opening the vault

**Windows or Mac:** install Obsidian, choose *Open folder as vault*, and pick
the `--out` folder. Opening it creates Obsidian's own `.obsidian` settings
folder, which the export never touches.

Use its own vault, or a subfolder of an existing one. In a shared vault two
things can collide with the export's names: a note of your own with the same
name as a generated one (`Index.md`, `Jake Morales.md`) makes a `[[link]]`
ambiguous, and the *Daily notes* core plugin names notes `YYYY-MM-DD` by
default, exactly like the journal notes — keep your daily notes in another
format or another vault.

**On the phone** there are two routes:

- **Obsidian Sync** (paid, end-to-end encrypted): open the vault on the
  computer, turn on Sync, then connect the phone's Obsidian to the same
  remote vault. Works the same on Android and iPhone.
- **A cloud drive:**
  - *iPhone + Mac* — export straight into iCloud Drive's Obsidian folder:
    `--out "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/LifeCRM"`.
    Obsidian on the iPhone lists vaults stored there.
  - *Android + Windows* — Obsidian on Android opens folders on the phone's
    own storage only, not OneDrive directly. Export into a OneDrive folder
    and mirror it to the phone with a sync app (FolderSync, Autosync for
    OneDrive) or with Syncthing between the laptop and the phone.

Treat the phone copy as read-only too: anything edited there is overwritten
by the next export. And remember what is in the vault — journal entries,
text snippets, phone numbers. Syncing it puts that on the sync provider.

Export from **one** computer. The manifest is a hidden file, and sync tools
(Obsidian Sync among them) generally skip hidden files; a second computer
exporting into the same synced vault would still never touch your own notes,
but without the first one's manifest it could not prune the notes that one
generated.

## Re-running it on a schedule

The export is idempotent and only writes what changed, so it is safe to run
after every intake or once a night.

**Windows** (Task Scheduler, as your own user; run from the project root):

```powershell
$root = (Get-Location).Path
$vault = "$env:USERPROFILE\OneDrive\LifeCRM vault"
$action = New-ScheduledTaskAction -Execute "$root\.venv\Scripts\python.exe" `
    -Argument "-X utf8 -m lifecrm export --format obsidian --out `"$vault`"" `
    -WorkingDirectory $root
Register-ScheduledTask -TaskName "LifeCRM obsidian" -Action $action `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 22:00) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) -Force
```

`-X utf8` is the same as `PYTHONUTF8=1`, which a scheduled task otherwise
lacks.

**Mac** (launchd): save as
`~/Library/LaunchAgents/local.lifecrm.obsidian.plist`, fix the two paths, and
load it with `launchctl load ~/Library/LaunchAgents/local.lifecrm.obsidian.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.lifecrm.obsidian</string>
  <key>WorkingDirectory</key><string>/Users/me/LifeCRM</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/me/LifeCRM/.venv/bin/python</string>
    <string>-X</string><string>utf8</string>
    <string>-m</string><string>lifecrm</string>
    <string>export</string><string>--format</string><string>obsidian</string>
    <string>--out</string>
    <string>/Users/me/Library/Mobile Documents/iCloud~md~obsidian/Documents/LifeCRM</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardErrorPath</key><string>/Users/me/LifeCRM/data/launchd-obsidian.log</string>
</dict>
</plist>
```

If the log shows "Operation not permitted", give the venv's python Full Disk
Access (System Settings › Privacy & Security), since iCloud Drive and
`~/Documents` are protected folders.

A failed export exits non-zero with one `error:` line — for example when a
note is locked by a sync client mid-write; the next run picks up where it
stopped.

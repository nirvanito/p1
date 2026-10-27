# Obsidian vault

`lifecrm export --format obsidian` (or `lifecrm obsidian refresh`) turns the
database into an [Obsidian](https://obsidian.md) vault you can browse on the
laptop and the phone. It has start pages (Home, People, Health, Sources), one
note per journal day, one per person, place and thing, your open loops, and
an index. Everything is cross-linked, so backlinks and the graph view work
with no plugins.

It can be your **main GUI** in place of the web UI: set where the vault
lives once, and every command that changes the database keeps it current
([below](#obsidian-as-your-main-gui)). It is the markdown export grown up,
and like every export it is **one-way**. Setting it up as the screen for a
collection that runs by itself, and acting from it day to day, is
[automatic-collection.md](automatic-collection.md), sections 4 and 5; this
page is the vault's own reference.

## Read-only by design

Nothing in the vault is ever read back into `lifecrm.db`. An edit made in
Obsidian flowing back into the database would be a second write path around
the review gate, and the architecture has exactly one (everything goes
through `python -m lifecrm ...`). To change a record, change it with the CLI
(`lifecrm entry edit`, `entry close`, `entities merge`, `entities alias-add`, ...) or `/log`, then
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

## Obsidian as your main GUI

Two front ends read the same database: the web UI (`lifecrm serve`,
[web.md](web.md)) and this vault. Choose the vault in `config.toml`:

```toml
[gui]
main = "obsidian"                     # what `lifecrm open` opens; "web" is the default

[obsidian]
vault = "~/OneDrive/LifeCRM vault"    # Mac: "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/LifeCRM"
# auto_refresh = true                 # the default once a vault is set
```

A relative `vault` is relative to the project root, like every path in
`config.toml`. Then:

```
lifecrm open                  # refresh the vault, open Obsidian on Home
lifecrm obsidian open         # the same, whatever [gui] main says
lifecrm obsidian refresh      # bring the vault up to date, open nothing (--dry-run to preview)
```

**It stays current by itself.** With a vault set, the CLI notes the audit
log's newest row before each command. If the command moved it, the CLI
re-exports the vault once the command is done. Every write the domain
layer makes logs one audit row, so this covers `/log`'s `apply`,
`observe ... --apply`, `health import --apply`, `entry close`,
`handles link`, `restore` and the rest. A read-only command never
triggers it, and neither does a question that `ask` or the web UI logs. The
refresh prints one line on stderr (`obsidian vault refreshed: 3 note(s)
changed in ...`) when anything changed, and nothing when nothing did. If it
fails — a sync client holding a note, the folder gone — it warns on stderr.
The command's own result and exit code stand, and `lifecrm obsidian refresh`
catches up. `auto_refresh = false` turns it off, and the environment variable
`LIFECRM_NO_VAULT_REFRESH=1` skips it for a script that runs many writes in a
row (the demo build does this, and refreshes once at the end).

The refresh is a re-export, the same one-way render as `export --format
obsidian`. The database is turned into notes again, and no note is ever
read back (see [Read-only by design](#read-only-by-design)).

**The start pages** do in the vault what the web UI's pages do in the
browser. They are built from the same reads (`lifecrm/web/api.py`), so the two
cannot disagree:

| note | what it shows |
|---|---|
| `Home.md` | Where to start. **Today** first: what the records say matters as of the database's last change — tasks and plans due within 7 days (overdue first), birthdays from linked contact cards within 14 days, nutrients below reference, imports gone stale, and the agenda for that day and the next — each line ending with the key `lifecrm nudges snooze` or `dismiss` takes ([assistant.md](assistant.md)); the vault is one way, so the "not now" goes through the CLI. Then the latest day (its summary, your journal's mood, energy and sleep, the health line, counts) and the three before it. Open loops, overdue first. The eight people most recently in touch, and who has been quiet for 30+ days. The last 7 days' sleep, steps and resting heart rate as small sparklines, your journal's mood and energy, the week's logged workouts, and any nutrient below 70% of its reference over 30 days (not medical advice). What the imports hold and what waits to be linked. |
| `People.md` | Everyone of type person, most recent contact first, by the rule `lifecrm ask` uses for "when did I last talk to X". A text on any app, an email that passed between you, a call that connected or a calendar event counts, as does a journal entry mentioning them. A contact card, a tapback, a missed call, a Cc or a meeting still ahead does not. You are not on the list. Then the people with no contact on record, and those not in touch for 30+ days. |
| `Health.md` | The last 30 days: each sensor metric (mean, min, max, trend and a sparkline, naming the export file), your journal's measures, logged workouts per week, and the `lifecrm nutrients` summary, which opens with its own not-medical-advice line. |
| `Sources.md` | Each connector's records per stream, its newest record, its last import and whether it went stale. The handles waiting to be linked (with the command that links one), health export files, the journal's own counts and drafts waiting for review, and the last ten import runs. |

They are dated **as of the database's last change**, the newest audit row,
never by the clock. "Overdue", "quiet for 30 days" and "the last 7 days" are
counted back from that day, which each page names. So the same database
renders the same pages on any day: a refresh that finds nothing new writes
nothing, and a synced vault does not churn overnight.

**What stays in the web UI.** Obsidian shows the data; it cannot answer a
question. Ask with `lifecrm ask "..."`, the web UI's Ask page or `lifecrm
voice`. Every page in the vault says so. The day digest and the start pages
carry the same `observations` uuids an answer cites, so a cited answer leads
back into the vault.

**On the phone**, Obsidian is a phone GUI with no server to keep running.
Sync the vault folder ([below](#opening-the-vault)) and the phone shows what
the laptop last refreshed.

## Making the vault

With `[obsidian] vault` set, `lifecrm obsidian refresh` makes it (and every
write after that keeps it current). Without it, name the folder each time.
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
Home.md                      the start page (see "Obsidian as your main GUI")
People.md                    everyone, most recent contact first
Health.md                    the last 30 days: sensor series, journal, workouts, nutrients
Sources.md                   connectors, handles waiting to be linked, import runs
Trips.md                     trips and countries (the places feature, with a gazetteer)
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

- "On this day:" links to the same date in other years that have a note
  (the `on_this_day` feature, [features.md](features.md));
- the day summary and the day's journal measurements;
- a **Health** line from the sensor tier: sleep hours, steps, workouts and
  weight, marked *observed* and naming the export file it came from. When two
  apps recorded the day (Samsung Health and Google Fit writing the same walk,
  an iPhone and a Watch counting the same steps) sleep, steps and workouts
  are the app with the larger total, never both added up — the numbers
  `lifecrm ask` and the web UI give;
- the entries, grouped by category, each with its kind, time of day, due
  date, closure, measurements and links to other entries; the people, places
  and things it mentions as `[[wikilinks]]`; and the transcript passage it
  was extracted from as a `> [!quote]` callout naming the source id (the
  same id `lifecrm ask` cites). The entry's uuid is in a hidden `%%` comment,
  ready for `lifecrm entry show`;
- an **Observed** section: how many rows each imported stream holds for the
  day, then up to ten notable items — at their time, calendar events, place
  visits, workouts (with the first line of their summary: `Bench Press
  (Barbell) 3×5 @ 185 lb; …`), rides and food orders (with the fare);
  texts (on any messaging app — iMessage, SMS, WhatsApp, Instagram,
  Messenger, Telegram, LinkedIn) and emails with people you have linked (an
  email is with its sender, or its recipients when you sent it — never with
  someone only copied on another person's mail), calls, notes, highlights
  grouped by book title and books rated or shelved, AI-chat conversations;
  then minutes listened and the number of searches, as counts only — each
  ending in `observed` and the uuid of an `observations` row behind it.

**An entity note** has the type, uuid and aliases in front matter (Obsidian
uses `aliases` for link suggestions), first and last seen, the latest
contact across both tiers (the newest journal entry and the newest imported
message or event, each cited), the handles linked to it (phone numbers,
email addresses), every journal day it appears in, and every day it appears
in imported data.

*Seen* (first and last seen, the latest contact, the Index's "last seen")
counts the journal days and the imported records that were an encounter:
for a person a text, an email (they sent it, or you sent it to them — a Cc
on someone else's mail is not one), a call that connected or a calendar
event — what `lifecrm ask` counts as contact (recall's `CONTACT_GROUPS`) —
and for a place or anything else any imported record. Never a contact card
(it is dated the day the contacts were
exported), a tapback, a missed call, or a record dated after the newest
import: a calendar export reaches months ahead, and next week's
meeting has not been seen yet. The newest import's time stands in for
"now", so the same database still renders the same vault on any day; those
records are still listed among the days the entity appears in imported
data.

**Imported text is made inert.** A text, a calendar invite's title, a note,
an AI chat's name or what a source calls a handle was written by someone
else — a stranger's invite reaches the day note with no linked handle at
all. So before it goes into a note, every character that could start
Markdown, Obsidian or HTML syntax gets a backslash, which Obsidian renders as
the character itself: `![](https://...)` cannot make Obsidian fetch an image
(and tell the sender the note was opened), `<img>` is not HTML, and
`[[links]]`, `#tags`, `%%comments%%`, `==highlights==` and `$math$` add
nothing to the vault or its graph. The words read the same. Handles are code
spans a backtick cannot close. Journal text you reviewed is written as it
is.

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
- `Index`, `Loops`, `Home`, `People`, `Health`, `Sources` and the journal
  dates are taken: a topic called "Health" becomes `Health (topic).md`, so
  `[[Health]]` always means the start page. That holds whether or not an
  export writes the start pages.

The same database always produces byte-identical files (LF line endings,
stable ordering, no timestamps), so a re-export with nothing new writes
nothing and a synced vault does not churn.

## Opening the vault

**Windows or Mac:** install Obsidian, then run `lifecrm obsidian open` (or
`lifecrm open` with `[gui] main = "obsidian"`). It refreshes the vault and
hands Obsidian an `obsidian://open?path=<vault>/Home.md` link, the same
as clicking one, which opens the vault on its Home note. Obsidian opens
links only into vaults it already knows. `obsidian open` reads Obsidian's
own vault list (`obsidian.json`: `%APPDATA%\obsidian\` on Windows,
`~/Library/Application Support/obsidian/` on a Mac — read, never written).
The first time, when the folder is not on that list, it opens Obsidian's
vault manager (`obsidian://choose-vault`) instead and says the one step to
take there: choose *Open folder as vault* and pick the vault folder. On
Windows that manager opens behind your other windows, as a second Obsidian
window on the taskbar. From then on it goes straight to Home. A vault kept
as a subfolder of a vault Obsidian already knows counts as known, since
Obsidian opens the note in the vault that holds it. Opening the folder creates Obsidian's own
`.obsidian` settings folder, which the export never touches.

To keep Home one click away inside Obsidian, bookmark it (the core
Bookmarks plugin: *Bookmark* in the note's menu). Obsidian also reopens the
notes that were open when you closed it.

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

With `[obsidian] vault` set (and `auto_refresh` left on) there is nothing to
schedule. Every write goes through the CLI, and the CLI refreshes the vault
after each one. A schedule is for `auto_refresh = false`, or for a vault you
export without naming it in `config.toml`. The export is idempotent and only
writes what changed, so it is safe to run after every intake or once a night.

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

## Checked with Obsidian itself

The tests replace the hand-off, so the suite never launches Obsidian. On
the Windows laptop, with Obsidian 1.14.5 (2026-10-24), the real hand-off was
checked:

- [x] Obsidian's vault list is `%APPDATA%\obsidian\obsidian.json`,
      `{"vaults": {<id>: {"path": ..., "ts": ..., "open": ...}}}`. That is
      the shape `obsidian open` reads.
- [x] On a folder Obsidian does not know, `obsidian://choose-vault` opens
      the vault manager. It opens as a second Obsidian window, **behind**
      the terminal, because Windows does not let a background app take the
      focus. Find it on the taskbar. Opening the folder from inside a vault
      that is already open (the vault name at the bottom left → *Manage
      vaults…* → *Open folder as vault*) works the same.
- [x] On a vault Obsidian knows, `lifecrm obsidian open` (`os.startfile`
      on `obsidian://open?path=…\Home.md`) opens it on `Home.md`. The
      window reads "Home - vault - Obsidian".

Still to check, on a Mac and on a synced vault:

- [ ] The same three steps on a Mac (`open` on the link, the vault list
      under `~/Library/Application Support/obsidian/`).
- [ ] The sparklines (`▁▂▃▄▅▆▇█` in a code span) line up in Obsidian's
      font on both machines.
- [ ] With the vault in OneDrive / iCloud Drive, a refresh while the sync
      client holds a note warns once and the next write's refresh catches
      up.

## The read-side features in the vault

While they are checked ([features.md](features.md)), four features reach the
vault, each a pure function of the database (and, for places, of the
gazetteer file), so the vault stays reproducible:

- **On this day** — the link row on each day note, and an *On this day*
  section on Home for the as-of date;
- **Places** — `Trips.md` (home, trips newest first, countries) and "(near
  Bled, Slovenia)" beside each place visit in a day note, from the local
  GeoNames file ([places.md](places.md)); without the file there is no
  Trips note;
- **Meeting prep, journal prompts, highlights** — on Home: "before you meet
  ..." under each agenda event, *Worth a word* for the day before the as-of
  date, *From your highlights*. Imported text in them is made inert like
  every other imported text here.

Unchecking a feature removes its part on the next refresh; a note the vault
no longer writes (Trips.md) is deleted only because the manifest owns it.


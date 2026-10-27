# p1 — Windows laptop + Android (Samsung)

> **This is the Windows + Android version (branch `windows-android`).** It is
> set up for a Windows laptop with a Samsung Galaxy, or another Android phone.
> The code is the same on every branch; the other setups have their own:
> `windows-iphone` (Windows laptop + iPhone) and `mac-iphone` (MacBook +
> iPhone). `main` holds all three.

## Quick start (Windows + Android)

1. **Install:** in PowerShell, from this folder, run
   `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`. It sets up
   Python 3.12, whisper and `config.toml`. The full walk-through is
   [docs/windows-android.md](docs/windows-android.md).
2. **See it working first:**
   `powershell -ExecutionPolicy Bypass -File scripts\demo.ps1` builds the
   Windows + Android demo and opens the web UI. Add `-Gui obsidian` to open
   it in Obsidian instead.
3. **The phone:** [docs/android.md](docs/android.md) covers Samsung Voice
   Recorder → Autosync for OneDrive → `OneDrive\VoiceJournal\inbox`, quick
   notes, call recordings (only the ones you pick), Health Connect, and SMS
   Backup & Restore.
4. **Background jobs:**
   `powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1`
   (intake, doctor, backup, weekly review).
5. **Every day:** run `/log` in Claude Code and review the diff. Ask with
   `/ask <question>`. Browse in the web UI (`lifecrm serve`) or Obsidian
   (`lifecrm open --gui obsidian`).

A personal life database that runs on your own computer. You talk to your
phone (one evening note, quick dictated thoughts during the day). Local
whisper transcribes it, and Claude Code drafts structured records from it
with `/log`: meals, workouts, work, people, places, mood, tasks, ideas.
Nothing lands until you have reviewed the diff, and every record points to
the sentence that produced it. Next to that confirmed journal, importers copy
what your apps already hold into the same SQLite file: texts, calls,
email, Instagram, Messenger and Telegram messages, contacts, LinkedIn,
calendar, notes, photo metadata, location visits, browser history, Google
searches and YouTube, bank statements, Venmo and PayPal, Uber rides,
workouts, food diaries, Spotify, Kindle highlights and Goodreads, health
data and AI chats. These imported records are
cited, never asserted as fact. You then ask questions on the command line,
on a web page on your phone, or by voice, and every fact in the answer
carries the id of the row it came from. There is no recurring cost: whisper
runs locally, extraction and written answers (`/log`, `/ask`) use an
existing Claude Code subscription, and everything else is local code.

```
phone ─ voice note / quick note ─> cloud folder (OneDrive or iCloud Drive)
  │
  ├─ lifecrm intake          local whisper for audio; text needs no model
  ├─ /log (Claude Code)      one draft per day (docs/contract.md)
  ├─ lifecrm ingest          validate + reconcile -> a reviewable diff
  ├─ you review              one keystroke, or corrections
  └─ lifecrm apply           CONFIRMED tier: entries, measurements, mentions,
                             entity relationships (audited, with provenance)

app exports    ─ lifecrm observe       ─> OBSERVED tier: observations
health exports ─ lifecrm health import ─> health_records

data/lifecrm.db ─> ask · serve (web UI) · Obsidian vault · voice · dashboard · review
                   export (jsonl / markdown / obsidian)
```

## Try the demo first

The demo builds a complete instance from synthetic data: one fictional
person's six weeks, rendered as each app's real export files and run through
the real importers. Then it opens the web UI. It never touches `data/`.

```
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1     # Windows
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1      #   -Combo iphone|windows-iphone, -Rebuild, -NoServe, -Port, -Gui obsidian

/bin/bash scripts/setup.sh                                     # Mac
/bin/bash scripts/demo.sh                                      #   --combo android|windows-iphone, --rebuild, --no-serve, --port, --gui obsidian
```

It builds `demo/<combo>/` (gitignored) and serves it at
`http://127.0.0.1:8765/`. The build includes an Obsidian vault at
`demo/<combo>/vault`; `-Gui obsidian` / `--gui obsidian` opens that in
Obsidian instead of the web UI. [docs/demo.md](docs/demo.md) lists what is
in it and what to try.

## Two front ends: the web UI or Obsidian

Pick one as your main GUI; both read the same database and neither writes
to it.

- **The web UI** (`lifecrm serve`, the default): Ask with cited answers,
  Today, People, Loops, Health and Sources pages, on the laptop or the phone
  ([docs/web.md](docs/web.md)).
- **Obsidian**: a read-only vault of the same data. Its start pages (Home,
  People, Health, Sources) show what the web pages show. Every journal day,
  person and place has its own note, and backlinks and the graph view work
  across them. Set `[obsidian] vault` in `config.toml` and every command that
  changes the database refreshes the vault by itself
  ([docs/obsidian.md](docs/obsidian.md)). Questions stay `lifecrm ask` (or
  the web UI, or voice): Obsidian shows the data, it does not answer.

The hands-off plan — every source's automatic route, the order to set it
up, what each morning asks of you afterwards, and Obsidian as the main
screen — is one page:
[docs/automatic-collection.md](docs/automatic-collection.md).

```
lifecrm open                       # the one [gui] main names: "web" (default) or "obsidian"
lifecrm open --gui obsidian        # this once
lifecrm obsidian open              # refresh the vault, open Obsidian on Home
lifecrm obsidian refresh [--dry-run]
```

## Three supported setups

| setup | full guide | phone capture |
|---|---|---|
| Windows laptop + Samsung Galaxy (Android) | [docs/windows-android.md](docs/windows-android.md) | [docs/android.md](docs/android.md): Samsung Voice Recorder + Autosync for OneDrive, Never sleeping apps, Health Connect export, SMS Backup & Restore, Contacts `.vcf`, Google Takeout |
| Windows laptop + iPhone | [docs/windows-iphone.md](docs/windows-iphone.md) | [docs/iphone.md](docs/iphone.md): the two Shortcuts saving to OneDrive; texts from an unencrypted Apple Devices backup, contacts and calendar from iCloud.com, photos from iCloud for Windows; no calls (Apple keeps them out of unencrypted backups), no Apple Notes |
| MacBook + iPhone | [docs/mac.md](docs/mac.md) | [docs/iphone.md](docs/iphone.md): two Apple Shortcuts ("Log My Day" for audio, "Quick Note" for dictated text) saving to iCloud Drive |

On GitHub each setup is also its own branch — `windows-android`,
`windows-iphone`, `mac-iphone` — with a quick start for it at the top of its
README; `main` holds all three. The code is the same on every branch.
[docs/new-machine.md](docs/new-machine.md) is the short version of the
setups, for anyone setting up their own copy.

**Commands.** `lifecrm …` below is short for `.venv\Scripts\python.exe -m
lifecrm …` on Windows and `.venv/bin/python -m lifecrm …` on a Mac, run from
the repo folder with `PYTHONUTF8=1`. Scripts are
`powershell -ExecutionPolicy Bypass -File scripts\<name>.ps1` on Windows and
`/bin/bash scripts/<name>.sh` on a Mac.

## Daily ritual

1. During the day, when a thought comes up, run **Quick Note** (iPhone) or
   double-press the side button (Samsung) and talk for ten seconds.
2. In the evening, record one longer note about the day.
3. At the computer, the scheduled intake has usually run already. Open
   Claude Code in the repo folder and type `/log`. All of today's notes are
   extracted together as one day. You review a diff (new entries, closed
   tasks, links to earlier threads, anything ambiguous) and confirm it.

No audio handy? Paste text straight into `/log`.

## Background jobs

`scripts/install-tasks.ps1` (Task Scheduler, tasks `LifeCRM *`) or
`scripts/install-tasks.sh` (launchd, `local.lifecrm.*`) registers seven jobs
for your user, with no admin rights needed:

| job | when | does | log |
|---|---|---|---|
| collect | daily 06:30 | `lifecrm collect --if-configured`: every source on this computer, found by itself — routine changes applied; a new source, a large removal or unreadable records become a yes/no question in /morning ([docs/collect.md](docs/collect.md)); one line and exit 0 when there is nothing to collect | `data/collect.log` |
| intake | daily 07:00 and at logon | both inboxes: text notes, then audio | `data/transcribe.log` |
| doctor | daily 08:00 | doctor against the live database | `data/doctor.log` (WARN/FAIL lines) |
| backup | daily 21:30 | snapshot with tiered retention (14 daily, 12 weekly, 24 monthly, yearly kept forever), local and the cloud copy (`cloud_backups_dir`: OneDrive or iCloud Drive) | — |
| verify-restore | Sundays 12:00 | `lifecrm verify-backup`: restores the newest snapshot into a scratch folder, runs doctor on it, and checks every table holds at least the rows live had when the snapshot was taken | `data/verify-restore.log` (Windows) |
| review | Mondays 07:30 | `lifecrm review --lens all --write-only`: the review pack for the week that just ended, waiting in `data/staging/` | `data/review.log` |
| reminders | daily 07:15 | `lifecrm reminders publish --if-configured`: republishes the reminders calendar so an overdue task moves to this morning's slot (docs/assistant.md); one line and exit 0 when no `[assistant] reminders_dir` is set | `data/reminders.log` |

The Windows tasks also run on battery. A failed run exits non-zero, so Task
Scheduler's *Last Run Result* shows `0x1` and launchd's `last exit code`
shows 1. On a Mac, a missing venv shows up in the job's log as
`FAILED (exit 127)`; whatever a script prints outside its log (all of
backup's and verify-restore's output) lands in `data/launchd-<job>.log`.
A file that intake fails on is named in the log
and stays in the inbox for the next run. `install-tasks.sh --print` shows the
plists without installing anything, and `uninstall-tasks.sh` removes them.

## Data sources

Every import is a **dry run by default**. It prints per-stream counts, date
spans and sample rows. Read them, then run the same command with `--apply`.
Re-running is always safe, because records dedupe on the source's own ids.
With no path, every connector except `ai_chat` looks in its usual place
(`finance`, `nutrition`, `payments` and `rides` need a `path` under their
`[connectors.<name>]` table). `lifecrm where` lists what this machine has,
with the command for each. Which of these arrive by themselves once set up,
which are a download you request now and then, and how the 06:30 job turns
either into a yes/no question: [docs/automatic-collection.md](docs/automatic-collection.md).

**No export at all — connect once** ([docs/online.md](docs/online.md)). These
live on a service's servers, so the 06:30 collect job downloads them itself
each morning; the first import still waits for your yes.

| source | connect once with | read by | guide |
|---|---|---|---|
| Google Calendar | its *Secret address in iCal format* | `calendar` | [online.md](docs/online.md#google-calendar--google_calendar--the-calendar-connector) |
| Gmail | a Google app password (2-Step Verification on) | `email` | [gmail.md](docs/gmail.md) |
| Google Contacts | your own Google Cloud OAuth client (ten minutes, once) | `contacts` | [google_contacts.md](docs/google_contacts.md) |
| Spotify listening | Spotify scrobbling to Last.fm + a free Last.fm API key | `lastfm` | [lastfm.md](docs/lastfm.md) |

```
lifecrm connect                              # which services there are, which are connected
lifecrm connect gmail you@gmail.com          # set one up (passwords asked for, hidden)
lifecrm fetch gmail                          # download now, import nothing; disconnect <service> forgets it
```

**Windows laptop + Samsung:** with the phone linked to Windows 11 (Link to
Windows), two things need nothing on the phone at all. Phone Link's own
cache on the laptop holds the phone's recent texts, calls and address book,
and the `phone_link` connector reads it ([phone_link.md](docs/phone_link.md)).
With the phone also shown in File Explorer, voice notes, GPS tracks and a
health export saved on the phone are read straight off it
([android.md](docs/android.md#no-sync-app-at-all-windows-11-optional)). The
sync route in the table works beside both.

| source | how it reaches the laptop | command | guide |
|---|---|---|---|
| Voice notes | Samsung Voice Recorder → Autosync → `OneDrive\VoiceJournal\inbox` | `lifecrm intake` (scheduled) | [android.md](docs/android.md) |
| Quick notes | the same path, or an Automate flow into `inbox-text` | `lifecrm intake` | [android.md](docs/android.md) |
| Call recordings (only the ones you pick) | Samsung Phone's `Recordings/Call` → the same inbox | held by intake; `lifecrm calls take <n>` | [android.md](docs/android.md#call-recordings-optional-and-only-the-ones-you-pick) |
| Health | Samsung Health → Health Connect → scheduled export `.zip`, to OneDrive or to a folder on the phone | `lifecrm health import [zip]` | [health-connect.md](docs/health-connect.md) |
| Texts, calls and contact names (nothing on the phone) | Phone Link's cache on the laptop, a window of about a month | `lifecrm observe phone_link` | [phone_link.md](docs/phone_link.md) |
| Texts and calls (the full history, deletions mirrored) | SMS Backup & Restore XML, scheduled to OneDrive or read off the phone | `lifecrm observe android_sms [folder]` | [android-texts.md](docs/android-texts.md) |
| Contacts | automatic: Phone Link's cache (above), or Google Contacts, connected once; or Contacts › Export → `.vcf` | `lifecrm observe contacts [file]` | [google_contacts.md](docs/google_contacts.md), [contacts.md](docs/contacts.md) |
| Calendar | automatic: Google Calendar's secret iCal link, connected once; or Google Takeout | `lifecrm observe calendar [export]` | [calendar.md](docs/calendar.md) |
| Notes | Google Keep Takeout, or a folder of `.md`/`.txt` (Obsidian, Samsung Notes text) | `lifecrm observe notes [path]` | [notes.md](docs/notes.md) |
| Photos (metadata only) | Gallery › Sync with OneDrive, or a Google Photos Takeout | `lifecrm observe photos [folder]` | [photos.md](docs/photos.md) |
| Location visits (automatic) | GPSLogger's daily track → Autosync → `OneDrive\GPSLogger` | `lifecrm observe gps_track [folder]` | [gps_track.md](docs/gps_track.md) |
| Location visits (Timeline) | Timeline › Export Timeline data → `Timeline.json` | `lifecrm observe location [file]` | [location.md](docs/location.md) |
| Browser history | the laptop's Chrome/Edge/Brave (phone Chrome syncs into it) | `lifecrm observe browser` | [browser.md](docs/browser.md) |
| WhatsApp | ⋮ › More › Export chat, Quick Share to the laptop | `lifecrm observe whatsapp <zip>`, after setting `[connectors.whatsapp] me` | [whatsapp.md](docs/whatsapp.md) |
| Bank and card | the bank's CSV or OFX/QFX download | `lifecrm observe finance <file or folder>` | [finance.md](docs/finance.md) |
| AI chats | Claude / ChatGPT data export | `lifecrm observe ai_chat <zip>` | [ai-chat.md](docs/ai-chat.md) |
| Email | automatic: Gmail, connected once; or Google Takeout › Mail → `takeout-….zip` in Downloads | `lifecrm observe email [zip]` | [gmail.md](docs/gmail.md), [email.md](docs/email.md) |
| Instagram and Messenger | Accounts Center › Download your information, **JSON** → `instagram-….zip`, `facebook-….zip` | `lifecrm observe meta_messages [zip or folder]` | [meta_messages.md](docs/meta_messages.md) |
| Telegram | Telegram Desktop on the laptop › Export chat history / Export Telegram data, machine-readable JSON | `lifecrm observe telegram [folder]` | [telegram.md](docs/telegram.md) |
| Google searches and YouTube | Google Takeout › My Activity + YouTube history | `lifecrm observe google_activity [zip]` | [google_activity.md](docs/google_activity.md) |
| Workouts | Strong / Hevy export from the app, Strava's archive from strava.com | `lifecrm observe fitness [file]` | [fitness.md](docs/fitness.md) |
| Spotify | automatic: scrobbling to Last.fm, connected once; or spotify.com › Account › Privacy settings › Download your data (earlier years) | `lifecrm observe lastfm` / `observe spotify [zip]` | [lastfm.md](docs/lastfm.md), [spotify.md](docs/spotify.md) |
| Kindle highlights, Goodreads | the Kindle by USB (`documents\My Clippings.txt`); Goodreads › Export Library | `lifecrm observe reading [file]` | [reading.md](docs/reading.md) |
| Food diary | cronometer.com › Settings › Account › Export Data → `servings.csv`, `dailysummary.csv` | `lifecrm observe nutrition <folder>`, then `lifecrm nutrients` | [nutrition.md](docs/nutrition.md) |
| LinkedIn | Settings & Privacy › Data privacy › Get a copy of your data | `lifecrm observe linkedin [zip]` | [linkedin.md](docs/linkedin.md) |
| Venmo and PayPal | venmo.com › Statements › Download CSV; paypal.com › Activity › Statements | `lifecrm observe payments <file or folder>` | [payments.md](docs/payments.md) |
| Uber rides and Eats | Uber's Privacy Center › Download your data → `Uber Data.zip` | `lifecrm observe rides <zip>` | [rides.md](docs/rides.md) |

**MacBook + iPhone:** first grant Full Disk Access to your terminal (System
Settings › Privacy & Security › Full Disk Access). Without it, macOS blocks
Messages, call history, Contacts, Photos, Notes, Mail, Safari and iPhone
backups, and the import says so in one line.

| source | where it lives | command | guide |
|---|---|---|---|
| Voice notes | Shortcut "Log My Day" → `iCloud Drive/VoiceJournal/inbox` | `lifecrm intake` (scheduled) | [iphone.md](docs/iphone.md) |
| Quick notes | Shortcut "Quick Note" → `inbox-text` | `lifecrm intake` | [iphone.md](docs/iphone.md) |
| Call recordings (only the ones you pick) | a recorded call's audio in Notes → Save to Files → the inbox | held by intake; `lifecrm calls take <n>` | [iphone.md](docs/iphone.md#call-recordings--only-the-ones-you-choose) |
| Health | Health › Export All Health Data → `export.zip` (AirDrop or Files) | `lifecrm health import [path]` | [apple-health.md](docs/apple-health.md) |
| Messages | the Mac's own `chat.db` (turn on Messages in iCloud and Text Message Forwarding) | `lifecrm observe mac_messages` | [texts.md](docs/texts.md) |
| Calls | the Mac's call history (FaceTime › Calls from iPhone) | `lifecrm observe ios_calls` | [calls.md](docs/calls.md) |
| Texts from a backup (alternative) | an unencrypted Finder backup of the iPhone | `lifecrm observe ios_messages` | [texts.md](docs/texts.md) |
| Contacts | the Mac's Contacts stores, iCloud included | `lifecrm observe contacts` | [contacts.md](docs/contacts.md) |
| Calendar | Google Calendar: automatic, its secret iCal link connected once; else Calendar › File › Export → `.ics` | `lifecrm observe calendar <ics>` | [calendar.md](docs/calendar.md) |
| Notes | Apple Notes' store | `lifecrm observe notes` | [notes.md](docs/notes.md) |
| Photos (metadata only) | the Photos library | `lifecrm observe photos` | [photos.md](docs/photos.md) |
| Browser history | Safari (the iPhone's Safari syncs into it), Chrome, Brave or Edge | `lifecrm observe browser` (`[connectors.browser] browser = "safari"`) | [browser.md](docs/browser.md) |
| WhatsApp | chat name › Export Chat, AirDrop to the Mac | `lifecrm observe whatsapp <zip>`, after setting `[connectors.whatsapp] me` | [whatsapp.md](docs/whatsapp.md) |
| Bank and card | Apple Card CSV, or the bank's CSV or OFX/QFX | `lifecrm observe finance <file or folder>` | [finance.md](docs/finance.md) |
| Location visits | only Google Maps Timeline on the iPhone. Apple's Significant Locations cannot be exported | `lifecrm observe location <file>` | [location.md](docs/location.md) |
| AI chats | Claude / ChatGPT data export | `lifecrm observe ai_chat <zip>` | [ai-chat.md](docs/ai-chat.md) |
| Email | Gmail: automatic, connected once; else Apple Mail's own store (`~/Library/Mail`), a Mailbox › Export Mailbox… folder, or a Gmail Takeout | `lifecrm observe email [path]` | [gmail.md](docs/gmail.md), [email.md](docs/email.md) |
| Instagram and Messenger | Accounts Center › Download your information, **JSON** → `instagram-….zip`, `facebook-….zip` | `lifecrm observe meta_messages [zip or folder]` | [meta_messages.md](docs/meta_messages.md) |
| Telegram | the Telegram Desktop app on the Mac › Export chat history / Export Telegram data, machine-readable JSON | `lifecrm observe telegram [folder]` | [telegram.md](docs/telegram.md) |
| Google searches and YouTube | Google Takeout › My Activity + YouTube history | `lifecrm observe google_activity [zip]` | [google_activity.md](docs/google_activity.md) |
| Workouts | Strong / Hevy export saved to iCloud Drive, Strava's archive from strava.com | `lifecrm observe fitness [file]` | [fitness.md](docs/fitness.md) |
| Spotify | automatic: scrobbling to Last.fm, connected once; or spotify.com › Account › Privacy settings › Download your data (earlier years) | `lifecrm observe lastfm` / `observe spotify [zip]` | [lastfm.md](docs/lastfm.md), [spotify.md](docs/spotify.md) |
| Kindle highlights, Goodreads | a Kindle at `/Volumes/Kindle` (USB-C models: copy the file off first); Goodreads › Export Library | `lifecrm observe reading [file]` | [reading.md](docs/reading.md) |
| Food diary | cronometer.com › Settings › Account › Export Data → `servings.csv`, `dailysummary.csv` | `lifecrm observe nutrition <folder>`, then `lifecrm nutrients` | [nutrition.md](docs/nutrition.md) |
| LinkedIn | Settings & Privacy › Data privacy › Get a copy of your data | `lifecrm observe linkedin [zip]` | [linkedin.md](docs/linkedin.md) |
| Venmo and PayPal | venmo.com › Statements › Download CSV; paypal.com › Activity › Statements | `lifecrm observe payments <file or folder>` | [payments.md](docs/payments.md) |
| Uber rides and Eats | Uber's Privacy Center › Download your data → `Uber Data.zip` | `lifecrm observe rides <zip>` | [rides.md](docs/rides.md) |

Use one route for texts per phone: the Mac's `chat.db` or an iPhone backup,
not both by turns. Each one mirrors its own source.

## Commands

`lifecrm --help` and `lifecrm <command> --help` are the full reference.

**Capture and review**

```
lifecrm status                               # pending notes, held calls, open loops, streak
lifecrm intake                               # both inboxes now (what the scheduled task runs); holds call recordings
lifecrm calls list                           # held call recordings; then `calls take <n>` or `calls skip <n>`
lifecrm import note.txt --date 2026-07-27    # a pasted transcript as a source
lifecrm transcribe --file "<inbox>/x.m4a" --date 2026-07-27   # one recording, re-dated
lifecrm filedate "Recording_20261021_003000.m4a"              # the day intake would give a name
lifecrm ingest <draft.json>                  # validate + reconcile -> a diff (what /log runs)
lifecrm apply <diff.json>                    # applying is the confirmation
lifecrm admin discard-run <run prefix>       # end a draft that will never be applied
```

**Fixing things** (every change is reversible and audited)

```
lifecrm entry show <uuid>                    # with its transcript passage and revisions
lifecrm entry edit <uuid> --title "..." --primary-category food
lifecrm entry retract <uuid>                 # and: entry restore
lifecrm entry merge <src-uuid> <dst-uuid>
lifecrm entry close <uuid> --closure done    # or dropped / obsolete; and: entry reopen
lifecrm link add <from-uuid> <to-uuid> --type resolves      # entry threads; link list / rm
lifecrm entities match "Jake"
lifecrm entities merge <src-id> <dst-id>
lifecrm entities alias-add <id> "Jaik"       # fix a mishearing for good
lifecrm entities link add <from-id> <to-id> --type from     # records "Jake from El Paso"
lifecrm entities show <id>                   # relationships listed both ways
```

`entities link` also has `list`, `rm` (retract), `restore`, `types` and
`type-add`. `categories` and `kinds` add reference rows without a migration.

**Ask, the web UI, voice**

```
lifecrm ask "how did I sleep last week"
lifecrm ask "when did I last talk to Mom"
lifecrm ask "what did Jake text me about the lake"
lifecrm ask "how much did I spend at Veracruz"
lifecrm ask "what are my open tasks"
lifecrm ask --history                        # recent questions; unanswered ones show what to import next
lifecrm ask "<question>" --check answer.md   # check a written answer's citations (what /ask runs)
lifecrm serve                                # web UI at http://127.0.0.1:8765/
lifecrm serve --host 0.0.0.0 --token random  # from the phone on the same Wi-Fi
lifecrm voice --text "how did I sleep last week"   # needs nothing installed
lifecrm voice                                # push-to-talk; extras in requirements-voice.txt
```

```
Sleep totalled 7.09 h per day over 5 day(s) (min 6.47 h 2026-07-27,
max 7.75 h 2026-07-26) [M1]. Related entries: 2026-07-27 Gym: push day [E1].
```

`M#` is a statistic, `E#` a confirmed entry, `P#` a transcript passage and
`O#` an imported record, always shown as "(observed, not confirmed)".
Retrieval is deterministic: rules parse the question and SQL answers it over
a read-only connection, so it works with no model installed. For a written
answer, type **`/ask <question>`** in Claude Code: Claude writes it from the
same evidence with your own Claude account, and `ask --check` checks it
sentence by sentence against what it cites before you see it
([docs/ask.md](docs/ask.md)). An offline alternative, a local Ollama model,
is off by default (`[ask] enabled = true` turns it on). Its answers get the
same check, and the deterministic answer replaces one whose numbers or
dates do not match.
`serve` is a viewer over a snapshot, with no capture and no edit button
([docs/web.md](docs/web.md)). `voice` answers aloud and turns "note …" into a
quick note for intake and `/log`. It never writes the database
([docs/voice.md](docs/voice.md)).

**Imports and handles**

```
lifecrm where                                # what this machine has that can be imported
lifecrm connectors                           # connectors, their last run, each one's own rows per stream
lifecrm observe <connector> [path]           # dry run
lifecrm observe <connector> [path] --apply
lifecrm imports list
lifecrm imports retract <uuid>               # undo one run; reversible
lifecrm imports restore <uuid>               # puts back exactly what retract hid
lifecrm handles list --suggest               # who is who: candidates from names and contact cards
lifecrm handles link phone:+15125550142 --entity <id>      # or --new "Jake Morales"
lifecrm handles ignore <kind:value>          # spam, delivery notices
```

Importers record *handles* (a phone number, an email, an account), never
people. Linking one handle attributes every observation that used it, past
and future, in one decision. Nothing is linked automatically.

**Health**

```
lifecrm health import [path]                 # Health Connect zip or Apple Health export; dry run
lifecrm health import [path] --apply
lifecrm health retract --source-file "<name as reported>"   # undo; and: health restore
lifecrm nutrients                            # what am I low on: 30 days of the food diary vs the reference intakes
lifecrm nutrients --days 14 --json           # or --start/--end; --sex/--age override [nutrition]
```

`nutrients` averages each nutrient per day from the `nutrition` connector's
records and sets it beside the adult Dietary Reference Intakes for
`[nutrition] sex` and `age`: low (under 70%), borderline, high (over an
upper level), with the foods behind each and the observation ids every
number came from. It is read-only and uses no model. It is a comparison
with reference values, not medical advice, and it recommends nothing
([docs/nutrition.md](docs/nutrition.md)).

**Weekly review and advisors**

```
lifecrm review                               # last full week's evidence pack -> data/staging/
lifecrm review --lens health --days 30       # lenses: week, health, career, relationships, finance, all
lifecrm review --check brief.md --out kept.txt   # verify a brief's citations
```

In Claude Code, `/review-week` and `/advise <lens>` write a short brief in
which every claim cites a pack id. The pack itself holds no interpretation.
A brief you keep becomes a `reflection` entry through the normal import →
ingest → review → apply gate ([docs/review.md](docs/review.md)).

**Features you can check and uncheck**

```
lifecrm features                             # the checklist of 14 read-side features: [x] on, [ ] off
lifecrm features uncheck subscriptions 2     # by key or number; check ... / --all / features pick
lifecrm onthisday                            # the same date in earlier years: entries, photos, places, people
lifecrm prompts                              # yesterday's moments worth a word in the journal (a prompt, never an entry)
lifecrm places fetch                         # once: GeoNames town names into data/geo/ (the only network call)
lifecrm places trips                         # trips away from home; `places countries`, `places where <date>`
lifecrm subscriptions                        # recurring charges, new ones, price rises
lifecrm experiments                          # plans with data.experiment: before vs during, co-occurrence not cause
lifecrm search lake bled                     # every match across journal and imports, with facets
lifecrm links nytimes.com                    # where a link or site turned up, and who sent it
lifecrm conversation Jake                    # one thread with a person across texts, chats, email, calls
lifecrm weekdays                             # each series by day of the week; `review --year 2025` for the year
```

Each one only reads and cites its rows; unchecking it hides it everywhere
(nudges and /morning, the review pack, `ask`, the web UI, the vault) and
deletes nothing ([docs/features.md](docs/features.md)).

**The assistant: what matters today**

```
lifecrm nudges                               # due tasks and plans, birthdays, nutrients below reference, stale imports, the agenda — cited
lifecrm nudges --json --out pack.json        # save the pack (what /morning writes from and checks against)
lifecrm nudges --check brief.md --pack pack.json   # verify a brief's ids, numbers and dates against that pack
lifecrm nudges snooze due:<uuid> --days 7    # "not now", by key; dismiss <key> for "never"; restore <key> undoes
lifecrm reminders publish                    # the reminders calendar (.ics) for the phone: due items and birthdays
lifecrm collect setup                        # the phone's side as a checklist: what is collected, ready, still to do (docs/collect.md)
lifecrm collect approve browser              # yes to a new source: the dry run, then the import (decline <source> = never)
lifecrm connect                              # the online services the job fetches by itself, once connected (docs/online.md)
lifecrm patterns                             # EXPERIMENTAL: which daily series go together, same day or next day (docs/patterns.md)
```

Every nudge is recomputed from the records and rests on ids you can check;
the only thing you tell it is "not now" or "never", by a durable key. Beyond
what is due: a message the imported records show no reply to, a person whose
own contact rhythm has lapsed, drift in the last week against the month
before (sleep, steps, workouts, mood, energy, spending), and progress on a
goal — a `plan` whose `data.goal` names a target ("three workouts a week",
`/log` proposes one when you say so). A missing day is unknown, never zero:
only a source whose format guarantees completeness (an OFX statement, a full
SMS backup, a whole Messages store, a full-history fitness export) declares
the days it covers, and everything else says "insufficient coverage" rather
than guess. In Claude Code, `/morning` writes a short cited brief from a
saved pack. The calendar is published to `[assistant] reminders_dir` (a
synced folder) and served by `lifecrm serve` at `/reminders.ics` for an
iPhone subscription ([docs/assistant.md](docs/assistant.md)).

**Dashboards**

```
lifecrm dashboard                            # http://127.0.0.1:8001/-/dashboards (Ctrl+C stops)
```

There are four dashboards. **Journal** shows days logged, entries per day,
categories, mood and energy, and open loops. **Health** shows sleep, steps,
weight, workouts, heart rate and food energy. **Observed** covers the
imported tier: texts per day by app, top people, calls, AI chats, calendar
events and money out per week, notes and photos per month, a map of photo
locations, the latest imports and handles waiting to be linked. **People**
shows who you were in touch with across both tiers, week by week, and each
person's last contact. Datasette sits underneath for ad-hoc browsing. The
server reads a snapshot taken at launch and never holds the live database
open, so rerun it to refresh. The definitions are code
([dashboards/dashboards.yml](dashboards/dashboards.yml)). The test suite runs
every chart query in every filter form against the current schema, and once
more through an in-process Datasette. The photo map's tiles need internet.

**Export**

```
lifecrm export --format markdown --out exports/markdown
lifecrm export --format jsonl --out exports/jsonl
lifecrm export --format obsidian --out "<vault folder>" [--dry-run]
```

The exports are deterministic: two exports of the same data diff cleanly.
The obsidian format is a read-only vault. It holds day notes (journal,
health line, a digest of observed records with their uuids), one note per
person, place and thing, `Loops.md`, `Index.md` and the start pages
(`Home.md`, `People.md`, `Health.md`, `Sources.md`). A re-export rewrites and
prunes only the notes that carry `lifecrm: generated`, so your own notes in
the vault are never touched ([docs/obsidian.md](docs/obsidian.md)). Reading
an export with no code at all: [docs/restore.md](docs/restore.md).

**Operations**

```
lifecrm doctor                               # every invariant the schema cannot enforce; exit 1 on failure
lifecrm backup                               # snapshot locally + to cloud_backups_dir
lifecrm verify-backup [snapshot]             # prove a snapshot restores, without touching live
lifecrm restore [snapshot]                   # list snapshots, or restore one (backs up current first)
lifecrm migrate                              # pending schema migrations, snapshot first
lifecrm configure [--force]                  # write config.toml for this machine
lifecrm where                                # read-only report of importable sources
lifecrm filedate <name>                      # check a recorder's file names before relying on them
lifecrm sql "SELECT ..." --json              # read-only queries
```

`configure` writes `config.toml`. It uses iCloud Drive's VoiceJournal on a
Mac and OneDrive's on Windows, sets the OS timezone as an IANA name and
creates the folders. Replacing an existing config needs `--force`, and the
old file is kept under `data/`. Every setting is in
[config.example.toml](config.example.toml).

## How it's built

- **SQLite is canonical** (`data/lifecrm.db`, WAL, schema v5). Schema
  changes are numbered migrations in `lifecrm/migrations/`, applied
  atomically by `lifecrm migrate` after a snapshot. Sources, transcripts and
  segments are immutable. Entries carry stable UUIDs and are corrected
  through revisions, retraction, closure and merges, never by overwriting.
- **Two trust tiers, one rule.** The confirmed tier (`entries`,
  `measurements`, `mentions`, `entity_links`) holds what you reviewed. The
  observed tier (`observations`, `health_records`) is bulk-imported at a
  scale nobody reviews. It is cited, never asserted, and an importer may not
  write entries, measurements, mentions, entity aliases or entity links.
  Connectors are pure readers ([docs/observed-tier.md](docs/observed-tier.md)).
- **The contract boundary** ([docs/contract.md](docs/contract.md), v2) runs
  transcripts → versioned JSON draft → deterministic validation → reviewable
  diff → transactional apply. A day made of several notes extracts as one
  draft. Re-processing produces a diff (add, update, possible duplicate,
  retract proposal, cross-day closure, unchanged). Confirmed records are
  never silently overwritten. Cross-day closures and retractions are always
  your explicit decision. Claude is just the current extractor.
- **Provenance.** Every entry names the source and the character span that
  supports it. Every observation names the import that brought it in and the
  export file it came from. Every `ask` fact carries an id you can check.
- **Mirroring, per connector.** Texts (`ios_messages`, `mac_messages`,
  `android_sms` from a full backup), AI chats, calendars (a
  one-calendar export once its name is listed in `mirror_calendars`, since
  two calendars can share a name), contacts, notes and a Mac Photos library
  *mirror* their source when it names an account: what the source no longer
  holds is removed on the next import. Removal is scoped to that account,
  preceded by a snapshot, and refused when it would remove a large share of
  the archive at once (`--allow-mass-removal` overrides). A record that a
  journal entry or a relationship cites is hidden instead of deleted.
  LinkedIn connections mirror the owner's profile (Connections.csv is the
  complete current list). WhatsApp exports, calls (iPhone and Android: each
  phone's call log ages old calls out), bank statements, location, browser
  history, photo folders and Takeouts *remember*, because a missing record
  there is not evidence of deletion. So do email, Instagram, Messenger,
  Telegram, LinkedIn messages, Google activity, workouts, Spotify, Kindle and
  Goodreads, the food diary, Venmo and PayPal, and Uber: each export is a
  window, a chosen range or a history the service ages out.
- **Health** comes from Health Connect or Apple Health into
  `health_records`, keyed on the source's own record ids (or a stable hash
  for Apple). It updates only from newer records and surfaces conflicts
  instead of overwriting. Sleep is dated by the day you woke up.
- **Loops, threads and relationships.** `task`/`plan` entries close (done,
  dropped or obsolete, which is different from retraction). `entry_links`
  (continues, resolves, part_of, caused, contradicts, relates) capture
  threads. `entity_links` (schema v5) are typed relationships between
  people, places and things, like *Jake from El Paso*, *Jake works_at Acme*
  or *Linda parent_of Alex*. They come only from a reviewed draft or
  `entities link add`, never inferred, and `ask` cites them through the
  entry that stated them.
- **Read-only front ends.** `serve` and `dashboard` read snapshots, and
  serve writes nothing to the database but the question log; started with
  a key, its Sources page also writes the feature switches in config.toml.
  `ask` retrieves over a read-only connection and then logs the question
  unless you pass `--no-log`.
  `review` never writes the database. `voice` drafts notes into the
  inbox and never applies them. The only path into the confirmed tier is
  `/log` → ingest → review → apply.
- **The archive outlives the code.** Exports are deterministic, and
  [docs/restore.md](docs/restore.md) explains how to read them with no
  tooling at all.
- **Privacy.** Personal data never enters git, and tests enforce that. The
  cloud folder holds audio and DB snapshots, which is cloud storage. Read
  [docs/privacy.md](docs/privacy.md).

Tests: `.venv\Scripts\python.exe -m pytest tests -q` (Mac:
`.venv/bin/python -m pytest tests -q`). Every concurrent pytest run needs its
own `--basetemp`. pytest deletes a basetemp at session start, so two runs
sharing one wipe each other's files mid-test.
`.github/workflows/ci.yml` (Windows and macOS runners) runs once the repo is
pushed to GitHub. Until then, the `.sh` scripts are tested under Git Bash on
Windows.

## Not yet verified on real devices

Every connector and every phone-side step is tested against synthetic files
in the real formats, not against the owner's real phones and exports. Each
guide ends with what to check on the first real run:

- Windows + Samsung: [windows-android.md](docs/windows-android.md#verification-checklist-once-on-the-real-devices),
  [android.md](docs/android.md#verify-it-on-the-real-phone-once). Recorder
  file names, Autosync, One UI menus, the Firewall prompt for phone access.
- Windows + iPhone: [windows-iphone.md](docs/windows-iphone.md#verification-checklist-once-on-the-real-devices).
  The Apple Devices backup folder, Shortcuts saving to OneDrive, the
  iCloud.com exports and iCloud for Windows' photo folder.
- Mac + iPhone: [mac.md](docs/mac.md#what-has-not-been-verified-on-a-real-mac),
  [iphone.md](docs/iphone.md#verification-checklist-run-on-the-real-phone).
  Nothing has run on a real Mac yet: launchd, Full Disk Access, iCloud
  latency, whisper speed on Apple Silicon.
- Imports: [Phone Link](docs/phone_link.md#verified-against-a-real-cache-2026-10)
  (texts and contacts were read from a real cache; calls and RCS rows were
  not in it), [texts](docs/texts.md#first-real-import-checklist-unproven-until-a-real-backup),
  [calls](docs/calls.md#first-real-import-checklist-unproven-until-a-real-store),
  [Android texts](docs/android-texts.md#first-real-import-checklist-unproven-until-a-real-backup),
  [Health Connect](docs/health-connect.md#first-real-import-checklist-unproven-until-a-real-phone-run),
  [Apple Health](docs/apple-health.md#first-real-import-checklist-unproven-until-a-real-iphone-run),
  [notes](docs/notes.md#real-device-checklist-unverified-until-run-on-real-exports),
  [photos](docs/photos.md#first-real-import-checklist-unproven-until-real-exports),
  [finance](docs/finance.md#first-real-import--check-these),
  and the known limitations in [contacts](docs/contacts.md#known-limitations),
  [calendar](docs/calendar.md#known-limitations),
  [location](docs/location.md#known-limitations),
  [browser](docs/browser.md#known-limitations) and
  [WhatsApp](docs/whatsapp.md#known-limitations). The newer sources say
  what is unverified in their own guides: [email](docs/email.md),
  [Instagram and Messenger](docs/meta_messages.md),
  [Telegram](docs/telegram.md), [Google activity](docs/google_activity.md),
  [workouts](docs/fitness.md), [Spotify](docs/spotify.md),
  [reading](docs/reading.md), [nutrition](docs/nutrition.md),
  [LinkedIn](docs/linkedin.md), [Venmo and PayPal](docs/payments.md) and
  [Uber](docs/rides.md).
- Front ends: [voice](docs/voice.md#not-yet-verified-on-real-hardware) (a
  real microphone, the wake word, speakers). For the web UI on a real phone,
  see [web.md](docs/web.md#on-your-phone). No real Ollama model (optional,
  off by default) has been tried with `ask`'s citation check, and `/ask` has
  not yet been run on your real data. `lifecrm obsidian open` has been checked
  with Obsidian on Windows, not yet on a Mac
  ([obsidian.md](docs/obsidian.md#checked-with-obsidian-itself)).

## Roadmap

The work order is [docs/next-brief.md](docs/next-brief.md). It is built
through Phase 5: integrity fixes, backups you can trust, every connector in
the source ranking, cross-tier `ask`, entity relationships, dashboards v2,
the weekly review and advisors, the read-only Obsidian mirror (now also a
main GUI, refreshed after every write), the web UI and the voice loop.
Still open:

- the real-device and first-real-import checklists above;
- ActivityWatch screen time;
- comparison questions ("this month vs last");
- owner actions: `gh auth login` and push (which also turns on CI), the
  folder rename, registering the scheduled tasks, and `lifecrm migrate` to
  bring the real database to schema v5.

Deliberately not built: a capture UI (the phone apps are the capture), live
federation of app data (everything is copied into `lifecrm.db`), and
bidirectional Obsidian sync, which would be a second write path. The vault's
refresh runs one way only: the database is rebuilt into notes, and no note
is ever read back.

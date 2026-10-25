# Windows laptop + Samsung phone — the whole setup

One page from nothing to a working journal on a **Windows 10/11 laptop** with
a **Samsung Galaxy** (One UI): install, configure, connect the phone, schedule
the background work, bring in each of the phone's data sources, and look at
the result from the laptop or the phone. The MacBook + iPhone equivalent is
[mac.md](mac.md).

Phone-side detail (which buttons, which apps, what to verify on the real
phone) lives in [android.md](android.md); this page links to its sections
instead of repeating them.

Time: about 30 minutes at the laptop and 20 on the phone, plus a one-time
~1.5 GB whisper model download.

**Commands.** Everything runs from the repo folder. `lifecrm …` below is
short for:

```
.venv\Scripts\python.exe -m lifecrm …
```

Set `PYTHONUTF8` once so names with accents and emoji print correctly in
every new terminal: `setx PYTHONUTF8 1` (the scheduled-task scripts set it
themselves).

## 0. What you need

- The laptop, with **git** (`winget install Git.Git`) and about 3 GB free.
- **Claude Code** with a Pro or Max login — extraction (`/log`) runs on that
  subscription; nothing here calls a metered API.
- **OneDrive** signed into one Microsoft account, on the laptop and on the
  phone (it is usually preinstalled on a Galaxy).
- On the phone: Samsung Voice Recorder (preinstalled), *Autosync for
  OneDrive*, and — for the data sources you want — *SMS Backup & Restore* and
  *Automate*. All have free tiers.

## 1. Install

```
git clone https://github.com/<owner>/lifecrm.git
cd lifecrm
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

`setup.ps1` finds or installs Python 3.12 (faster-whisper's dependencies
have no wheels for newer Pythons yet), creates `.venv`, installs the
requirements, runs `lifecrm configure` if there is no `config.toml` yet
(section 3), and creates an empty database. Re-running it is safe.

Keep the clone **outside** OneDrive (for example `C:\Users\<you>\lifecrm`):
the database must not be cloud-synced while it is open. Its snapshots go to
OneDrive on purpose, through `backup`.

## 2. The OneDrive folders

`lifecrm configure` (run by `setup.ps1`) creates these inside your OneDrive
folder; it never creates OneDrive itself, so sign in to OneDrive first. To
make them by hand, create in Explorer:

```
OneDrive\VoiceJournal\inbox         <- audio notes from the phone
OneDrive\VoiceJournal\inbox-text    <- typed/dictated text notes
OneDrive\VoiceJournal\archive       <- processed files move here
OneDrive\VoiceJournal\backups       <- database snapshots
```

Right-click `VoiceJournal` › **Always keep on this device**. Without it,
OneDrive may keep only online placeholders, and intake would have to
download each recording before reading it (it can, with retries — but a
laptop that is offline at 07:00 then has nothing to read).

A work or school account names the folder `OneDrive - <Organization>`; use
whichever OneDrive the phone signs into.

## 3. Configure

```
lifecrm configure
```

It writes `config.toml`: the four OneDrive folders and Windows' timezone as
an IANA name. Everything else is `config.example.toml`'s defaults, comments
included, such as the whisper model. `setup.ps1` already ran it once. To
start over, run `lifecrm configure --force`, which keeps the replaced file
under `data\`. `lifecrm configure --help` lists the options
(`--journal-root`, `--timezone`, `--no-mkdir`). Prefer to edit by hand? The
keys that matter:

```toml
[paths]
inbox_dir = '~\OneDrive\VoiceJournal\inbox'
inbox_text_dir = '~\OneDrive\VoiceJournal\inbox-text'
archive_dir = '~\OneDrive\VoiceJournal\archive'
cloud_backups_dir = '~\OneDrive\VoiceJournal\backups'

[time]
timezone = "America/Chicago"     # your IANA zone
day_rollover_hour = 5            # notes before 05:00 count as the previous day
filename_date = "auto"           # see android.md, "The time in the filename"

[whisper]
model = "distil-large-v3"        # "small" if transcription is too slow
```

(`~` is your user folder. Single quotes keep the backslashes literal.)

Then check what the pipeline will read and write, and that it is happy:

```
lifecrm where
lifecrm doctor
```

`where` prints the config file, each inbox (with how many notes are waiting),
the archive and cloud backups folders, the iPhone backup folders, and any
Apple Health, Health Connect, SMS Backup & Restore, Takeout, WhatsApp or .ics
export sitting in Downloads, OneDrive or the journal folder, each with the
command that reads it. `doctor` warns
about a folder that does not exist — the classic typo that otherwise looks
like a quiet day.

## 4. The phone

Follow [android.md](android.md):

1. **Voice notes** — Samsung Voice Recorder, the side-button shortcut, and
   an Autosync folder pair `Recordings/Voice Recorder` → OneDrive
   `VoiceJournal/inbox`.
2. **Never sleeping apps** — add Autosync (and every other sync app below),
   or One UI silently stops the uploads.
3. **Quick notes** — a ten-second voice note through the same path, or the
   optional Automate flow for typed text into `inbox-text`.

Then prove the timestamps survive the trip. Record a note, wait ten minutes,
let it sync, and on the laptop:

```
lifecrm filedate "$HOME\OneDrive\VoiceJournal\inbox\Voice 001.m4a"
```

It prints the journal day and recording time intake would assign. If the
time is the *upload* time rather than when you spoke, see android.md's
options (rename, or a recorder that writes the time into the name).

## 5. Background work: scheduled tasks

```
powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1
```

This registers, for your user (no admin):

| task | when | does | log |
|---|---|---|---|
| LifeCRM collect | daily 06:30 | the imports `[collect] sources` names (section 7): routine changes applied, anything unusual held for you | `data\collect.log` |
| LifeCRM intake | daily 07:00 and at logon | transcribes audio, imports text notes | `data\transcribe.log` |
| LifeCRM doctor | daily 08:00 | doctor against the live database | `data\doctor.log` (WARN/FAIL lines) |
| LifeCRM backup | daily 21:30 | tiered snapshots, local + OneDrive | — |
| LifeCRM verify-restore | Sundays 12:00 | `lifecrm verify-backup`: restores the newest snapshot into a scratch folder, runs doctor on it, checks every table against live | `data\verify-restore.log` |
| LifeCRM review | Mondays 07:30 | the review pack for the week that just ended, in `data\staging\`, for `/review-week` | `data\review.log` |
| LifeCRM reminders | daily 07:15 | republishes the reminders calendar (docs/assistant.md) | `data\reminders.log` |

On a laptop:

- **Battery.** The script registers every task to start on battery and to
  keep running when the charger is pulled. Windows' default for a scripted
  task is the opposite, and a laptop on battery would then skip (or kill)
  the run without a word. To check, open Task Scheduler › a LifeCRM task ›
  Properties › *Conditions*: both power boxes should be unticked.
- **Sleep.** A task due while the lid is closed runs when the laptop wakes
  ("run as soon as possible after a missed start"); intake also runs at every
  logon.
- **Failures are visible.** A failed run shows *Last Run Result* `0x1`, and
  intake writes what went wrong, with the file name, to
  `data\transcribe.log`. A file that fails stays in the inbox for the next
  run.

## 6. The daily loop

1. During the day: double-press the side button, talk ten seconds.
2. Evening: one longer note about the day.
3. At the laptop: intake has usually run already (or run `lifecrm intake`).
   Open Claude Code in the repo folder and type **`/log`**. It drafts the
   day as one extraction, you review the diff, confirm, and it applies.
   Call recordings in the inbox are held, not transcribed. `/log` lists
   them and asks which belong in the journal
   ([android.md](android.md#call-recordings-optional-and-only-the-ones-you-pick)).

```
lifecrm status        # pending notes, held calls, open loops, streak
lifecrm intake        # process both inboxes now (call recordings stay held)
lifecrm calls list    # held call recordings: `calls take <n>` / `calls skip <n>`
lifecrm doctor        # prove the database is intact
```

A note on the wrong day: `lifecrm transcribe --file "<path>" --date
2026-07-27` (`--date` requires `--file`), or tell `/log` the date.

## 7. Each data source

Everything below is the **observed tier**: bulk imports that never pass the
review gate. Every import is a **dry run by default** — read the report's
counts, date spans and sample lines; then run the same command with
`--apply`. Re-running is always safe (records dedupe on the source's own ids).

| source | phone side (android.md) | lands on the laptop | command |
|---|---|---|---|
| Voice notes | §1 Voice Recorder + Autosync | `VoiceJournal\inbox` | `lifecrm intake` (scheduled) |
| Text notes | §2 Automate + sync | `VoiceJournal\inbox-text` | `lifecrm intake` (scheduled) |
| Health | §3 Samsung Health → Health Connect → scheduled export | Drive/OneDrive `.zip` | `lifecrm health import ["<zip>"]` |
| Texts and calls | §4 SMS Backup & Restore, scheduled | `OneDrive\SMSBackupRestore` | `lifecrm observe android_sms ["<folder or sms-….xml>"]` ([android-texts.md](android-texts.md)) |
| Contacts | §5 Contacts › Export | a `.vcf` | `lifecrm observe contacts ["<Contacts.vcf>"]` |
| Calendar | §6 Google Takeout | `Takeout\Calendar` | `lifecrm observe calendar "<…\Takeout\Calendar>"` |
| Keep notes | §6 Google Takeout | `Takeout\Keep` | `lifecrm observe notes "<…\Takeout\Keep>"` |
| Photos (metadata) | §6 Takeout, or Gallery › Sync with OneDrive | `Takeout\Google Photos` / `OneDrive\Pictures\…` | `lifecrm observe photos "<folder>"` |
| Location visits | §6 Timeline › Export Timeline data | `Timeline.json` | `lifecrm observe location ["<Timeline.json>"]` ([location.md](location.md)) |
| Browser history | Chrome sync on the phone | the laptop's Chrome/Edge/Brave profile | `lifecrm observe browser` ([browser.md](browser.md)) |
| WhatsApp | ⋮ › More › Export chat › Quick Share | `Downloads\WhatsApp Chat with ….zip` | `lifecrm observe whatsapp ["<zip>"]`; set `[connectors.whatsapp] me` first ([whatsapp.md](whatsapp.md)) |
| Bank and card | the bank's website | a `.csv` / `.ofx` / `.qfx` | `lifecrm observe finance "<file or folder>"` ([finance.md](finance.md)) |
| AI chats | Claude / ChatGPT data export | a `.zip` | `lifecrm observe ai_chat "<zip>"` ([ai-chat.md](ai-chat.md)) |

**After a source's first import, the morning job can take over.** The
phone already sends texts and calls (SMS Backup & Restore's nightly
backup), health (Health Connect's scheduled export) and photos (Gallery's
OneDrive sync) to the laptop by itself, and the browser keeps its own
history; list those in `config.toml` and the 06:30 task imports them each
morning, holding anything unusual for you in /morning and doctor:

```toml
[collect]
sources = ["android_sms", "health", "browser", "photos"]
# health_dir = "~/OneDrive/Health Connect"   # if Health Connect exports into a subfolder
```

[collect.md](collect.md) has the rules. Every source's first import stays
yours: the job holds a source it has never imported.

A path in brackets is optional: with none, the connector looks where that
export usually lands. For health, texts and WhatsApp, `lifecrm where` shows
what it finds. Contacts takes the newest `.vcf` in Downloads, and location
takes a `Timeline.json` (or `location-history.json`) in Downloads or OneDrive.
`lifecrm connectors` lists the connectors this install has, with each one's
last run. Health has its own walkthrough: [health-connect.md](health-connect.md).

### The newer sources

Each of these is a download on the laptop (or a file the phone saves to
OneDrive), imported the same way: dry run, read it, `--apply`. `lifecrm
where` recognises every one of them in Downloads, OneDrive or the journal
folder by the name the app gives it, and prints the command.

**Email (Gmail).** takeout.google.com → *Deselect all* → **Mail** → export
once, `.zip` → download `takeout-<date>T<time>Z-001.zip` into Downloads and
leave it zipped. `lifecrm observe email` (no path) takes the newest Takeout
there that holds mail. Samsung Email exports nothing; export the account.
[email.md](email.md)

**Instagram and Messenger.** On the Samsung or at accountscenter.facebook.com:
Accounts Center → *Your information and permissions* → **Download your
information** → Messages, *All time*, **Format: JSON**. Download the
`instagram-….zip` / `facebook-….zip` on the laptop:
`lifecrm observe meta_messages` (Downloads) or pass the zip or a folder of
parts. [meta_messages.md](meta_messages.md)

**Telegram.** Only Telegram Desktop exports — install it on the laptop: a
chat's ⋮ → *Export chat history*, or *Settings › Advanced › Export Telegram
data*, **Machine-readable JSON**. It writes
`Downloads\Telegram Desktop\ChatExport_<date>\` or `DataExport_<date>\`:
`lifecrm observe telegram` reads that folder. [telegram.md](telegram.md)

**Google searches and YouTube.** The same Takeout page: **My Activity** and
*YouTube and YouTube Music › history*, both in JSON →
`lifecrm observe google_activity` (newest Takeout in Downloads with activity
in it). One Takeout can carry Mail, Calendar, Keep and activity together;
each connector reads its own part. [google_activity.md](google_activity.md)

**Workouts.** Strong: Settings › *Export Strong Data* → `strong.csv`. Hevy:
Settings › *Export & Import Data* → `workouts.csv`. Strava: strava.com ›
Settings › My Account › *Request Your Archive* → `export_<id>.zip`. Save the
phone's file to OneDrive (or mail it to yourself) and run
`lifecrm observe fitness ["<file>"]`. [fitness.md](fitness.md)

**Spotify.** spotify.com › Account › *Privacy settings* › **Download your
data** (account data, and the extended history) → `my_spotify_data.zip`;
put both downloads in one folder and pass it:
`lifecrm observe spotify "<folder>"`. [spotify.md](spotify.md)

**Kindle and Goodreads.** Plug the Kindle in by USB: `lifecrm observe
reading` finds `documents\My Clippings.txt` on any removable drive (a newer
USB-C Kindle shows up as a device, not a drive — copy the file out first).
Goodreads: *My Books › Import and export › Export Library* →
`goodreads_library_export.csv` in Downloads. [reading.md](reading.md)

**Food diary.** cronometer.com › Settings › Account › **Export Data** →
`servings.csv` and `dailysummary.csv` into one folder →
`lifecrm observe nutrition "<folder>"` (or set `[connectors.nutrition]
path`), then `lifecrm nutrients` for the cited low / borderline / high
report — set `[nutrition] sex` and `age` first. [nutrition.md](nutrition.md)

**LinkedIn.** linkedin.com › *Settings & Privacy › Data privacy* › **Get a
copy of your data** → `Basic_LinkedInDataExport_<date>.zip` in Downloads →
`lifecrm observe linkedin`. [linkedin.md](linkedin.md)

**Venmo and PayPal.** venmo.com › **Statements** › *Download CSV*;
paypal.com › Activity › Statements › **Activity report** (CSV). Keep them in
one folder (your bank statements' folder is fine) →
`lifecrm observe payments "<folder>"`. [payments.md](payments.md)

**Uber.** Uber's Privacy Center (myprivacy.uber.com, or the app's *Account
› Settings › Privacy*) › **Download your data** → `Uber Data.zip` →
`lifecrm observe rides "<zip>"`. [rides.md](rides.md)

**Who is who.** Importers record phone numbers and email addresses
(*handles*), never guess people. After contacts and texts are in:

```
lifecrm handles list --suggest            # candidates from your contact cards
lifecrm handles link phone:+15125550142 --entity <id>
```

Linking a handle attributes every text, call and invite that used it; it is
always your decision.

**Undo.** `lifecrm imports list` shows every run; `lifecrm imports retract
<uuid>` takes one back out (kept on disk, hidden everywhere), and `lifecrm
imports restore <uuid>` puts it back. Health:
`lifecrm health retract --source-file "<zip name>"` (and `health restore`).

## 8. Asking and looking (on the laptop)

```
lifecrm ask "how did I sleep last week"
lifecrm serve                     # the web UI: http://127.0.0.1:8765/  (Ctrl+C stops)
lifecrm dashboard                 # http://127.0.0.1:8001/-/dashboards  (Ctrl+C stops)
lifecrm voice --text "when did I last talk to Mom"   # voice.md for the microphone
```

`ask` answers from the database with citations, deterministically. For a
written answer, type `/ask <question>` in Claude Code. Claude writes it from
the same evidence and `ask --check` verifies every citation
([ask.md](ask.md)). No local model to install.
The dashboard serves a snapshot taken at start, never the live database;
rerun it to refresh.

**Obsidian instead of the web UI.** To browse in Obsidian, add this to
`config.toml`:

```toml
[gui]
main = "obsidian"
[obsidian]
vault = "~/OneDrive/LifeCRM vault"
```

Then run `lifecrm open` (or `lifecrm obsidian open`). The first time, choose
*Open folder as vault* in Obsidian's vault manager. After that, every write
(`/log`'s apply, an import) refreshes the vault by itself. On the Samsung,
Obsidian opens folders on the phone's own storage only, so mirror the
OneDrive folder there with Autosync for OneDrive or FolderSync
([obsidian.md](obsidian.md#obsidian-as-your-main-gui)). Questions stay
`lifecrm ask` or the web UI.

## 9. From the phone, over Wi-Fi

Both servers listen only on the laptop itself unless you say otherwise:

```
lifecrm serve --host 0.0.0.0 --token random   # the phone-friendly page, port 8765 (web.md)
lifecrm dashboard --host 0.0.0.0              # the charts, port 8001
```

1. **Windows Firewall.** The first time, Windows asks whether Python may
   accept connections. Allow it on **Private networks only**.
2. **Your Wi-Fi must be Private.** Settings › Network & internet › Wi-Fi ›
   your network › *Network profile type* › **Private**. On a Public profile
   the firewall blocks the phone even after you allowed Python.
3. **Find the laptop's address:** `ipconfig` › the Wi-Fi adapter's *IPv4
   Address*, e.g. `192.168.1.23`. On the phone (same Wi-Fi), open
   `http://192.168.1.23:8765/` for the web page (`serve` prints the address
   with its token when it starts) or `http://192.168.1.23:8001/-/dashboards`
   for the charts.
4. Samsung Internet or Chrome › menu › **Add page to › Home screen** makes it
   one tap next time.

**Missed the firewall prompt?** In an **administrator** PowerShell, allow
the ports on Private networks only:

```
New-NetFirewallRule -DisplayName "Life CRM (LAN)" -Direction Inbound -Protocol TCP -LocalPort 8765,8001 -Profile Private -Action Allow
Remove-NetFirewallRule -DisplayName "Life CRM (LAN)"     # to take it away again
```

**Refused it (clicked Cancel or "don't allow")?** Then Windows made inbound
**Block** rules for Python, and a Block rule wins over any Allow rule, so
the port rule above changes nothing until they are gone. The rules name the
base interpreter the venv was made from
(`...\Python\pythoncore-3.12-64\python.exe`, say), not
`.venv\Scripts\python.exe`. See them, then remove them, in an administrator
PowerShell, and add the port rule above:

```
Get-NetFirewallApplicationFilter | Where-Object Program -like '*\python*.exe' | Get-NetFirewallRule | Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' } | Format-Table DisplayName, Profile, Enabled
Get-NetFirewallApplicationFilter | Where-Object Program -like '*\python*.exe' | Get-NetFirewallRule | Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq 'Inbound' } | Remove-NetFirewallRule
```

The dashboard has **no login**, and the web page has only the `--token` key.
Use them only on your own Wi-Fi: never on a Public profile, never
port-forwarded. Guest networks and "AP isolation"
block phone-to-laptop traffic by design.

## 10. The demo — no phone needed

```
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1   # builds demo\android\, opens the web UI on :8765
lifecrm demo --combo android                                # or just build it
```

builds a separate, throwaway project root holding six weeks of a fictional
life (Alex Rivera in Austin: a best friend Jake who loves Veracruz, a Lake
Bled trip four years ago, a board deck due). The life is rendered as the
Samsung-side exports: the phone's notes, SMS Backup & Restore XML, a Health
Connect export, a contacts `.vcf`, a Takeout with calendar and Keep,
Samsung Gallery photos, `Timeline.json`, Chrome history, a WhatsApp export,
Chase CSVs and AI chat exports, and the newer sources: a Gmail Takeout, a
Takeout of My Activity and YouTube history, Instagram and Facebook exports,
a Telegram Desktop export, Strong and Strava, Spotify, Kindle clippings and
Goodreads, Cronometer, LinkedIn, Venmo statements and Uber. It imports them
through the same commands as above, and never touches your own data. It
prints the demo root; point any command at it with `--root`:

```
lifecrm --root "<demo root>" status
lifecrm --root "<demo root>" ask "where is Jake from"
lifecrm --root "<demo root>" dashboard --host 0.0.0.0
```

[demo.md](demo.md) has the rest; `lifecrm demo --help` has the options.

## 11. Backups

`backup` (scheduled daily) keeps tiered snapshots in `backups\` and a copy in
`OneDrive\VoiceJournal\backups`; `verify-restore` (weekly, `lifecrm
verify-backup`) proves the newest one restores. Restoring, and moving to a new laptop: [restore.md](restore.md)
and [new-machine.md](new-machine.md).

## Troubleshooting

| symptom | look at |
|---|---|
| Nothing arrives from the phone | Autosync in *Never sleeping apps*; Autosync's own log; OneDrive running and not paused on the laptop; `lifecrm doctor` (`configured_dirs`, `inbox_stuck`) |
| A note is on the wrong day | `lifecrm filedate "<file>"`; android.md "The time in the filename"; `transcribe --file … --date …` |
| Task Scheduler shows `0x1` | `data\transcribe.log` — the failing file is named; it stays in the inbox |
| `doctor` says a file type is never read | the phone saved a format intake does not take; the warning lists the ones it does |
| The phone cannot open the dashboard | Wi-Fi profile is Private; the Allow rule exists and no inbound Block rule for python.exe does (section 9); same network; the laptop's IP changed (`ipconfig`) |
| Transcription is slow | `[whisper] model = "small"` |

## Verification checklist (once, on the real devices)

- [ ] `setup.ps1` completes; `lifecrm doctor` reports 0 failures on the
      empty database.
- [ ] `lifecrm where` shows the four OneDrive folders you created.
- [ ] A voice note recorded on the Galaxy reaches `VoiceJournal\inbox`;
      `lifecrm filedate` on it shows when you spoke (note the sync latency).
- [ ] `lifecrm intake` transcribes it (note the whisper time for a 3-minute
      note); `/log` drafts, you review, it applies.
- [ ] After `install-tasks.ps1`: an unattended 07:00 run on battery leaves a
      `data\transcribe.log` entry and a snapshot appears after 21:30.
- [ ] Each data source's dry run shows sensible counts and samples before
      `--apply`.
- [ ] The phone opens the dashboard over Wi-Fi with the profile set to
      Private, and cannot once it is set to Public.
- [ ] The android.md checklist, on the phone.

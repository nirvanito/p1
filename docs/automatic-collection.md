# Hands-off: automatic collection, and Obsidian as the screen

**The goal.** After one afternoon of setup, your data arrives every morning
with no export to make, and you read all of it in Obsidian. What is left for
you each day is two things: say *yes* or *no* when the morning job asks about
something new, and review your own journal notes with `/log`. Everything else
runs on a schedule.

This page is the end-to-end plan: for every source, the one route that needs
no manual export (or the closest there is); the order to set things up; what
every morning looks like afterwards; and how to use the Obsidian vault as the
main screen. The button-by-button detail lives in the guide each line links
to — this page does not repeat it. The three complete setups are
[windows-android.md](windows-android.md), [windows-iphone.md](windows-iphone.md)
and [mac.md](mac.md); the rules of the morning job are in
[collect.md](collect.md), the online services in [online.md](online.md), the
vault in [obsidian.md](obsidian.md).

Steps marked **(verify)** in the linked guides come from the apps'
documentation, not from a test on a real phone; each guide's checklist says
what to confirm on the first real run.

**Commands.** `lifecrm …` is short for `.venv\Scripts\python.exe -m lifecrm …`
on Windows and `.venv/bin/python -m lifecrm …` on a Mac, run from the repo
folder with `PYTHONUTF8=1`.

## The picture

```
the phone's apps ──(a) shown in File Explorer (Windows 11 + Android): read directly ──┐
                 ──(b) a sync app or a Shortcut → OneDrive / iCloud Drive ─────────────┤
online services ── Google Calendar, Gmail, Google Contacts, Last.fm: fetched ──────────┤
this computer's own apps ── browser history; a Mac's Messages, calls, Contacts, Notes, Photos ┤
downloads you make by hand ── dropped in Downloads: found the next morning ────────────┘
                                                                                        │
  06:30  lifecrm collect    routine changes imported; anything new is a yes/no question │
  07:00  lifecrm intake     voice notes transcribed, quick notes read in                │
  07:15  lifecrm reminders  the reminders calendar republished                          │
  08:00  lifecrm doctor     every invariant checked; a source gone quiet is a warning   │
  21:30  lifecrm backup     snapshots, local and in the cloud folder                    ▼
                                                                                 data/lifecrm.db
                                                                                        │
  every write ──> the Obsidian vault is re-rendered: Home, People, Health, Sources, one note
                  per day and per person ──> Obsidian on the laptop, and synced, on the phone
```

Two tiers, one rule, unchanged by any of this: your journal (`/log`, reviewed
by you) is the confirmed tier; everything an importer brings in is the
observed tier — cited, never asserted as fact. The morning job never writes
an entry, never links a handle to a person, never runs a model
([collect.md](collect.md#what-it-never-does)).

## 1. Which sources arrive by themselves

Three groups. **A** needs no export ever once it is set up. **B** is a
download you request by hand now and then, which the job finds and offers
the next morning. **C** is a folder you name once, into which you drop
statements as they come.

### A. Automatic once set up — no export, ever

| source | the automatic route | set up once | after that | "gone quiet" warning |
|---|---|---|---|---|
| Voice notes, quick notes | the recorder's and the quick-note folder are read straight off the phone (Windows 11 + Android), or a sync app / Shortcut saves into `VoiceJournal/inbox` and `inbox-text` | [android.md §1–2](android.md#1-voice-notes), [iphone.md](iphone.md) | intake (07:00 and at logon) transcribes; you `/log` | `doctor`: a note stuck in the inbox |
| Texts and calls (Android) | SMS Backup & Restore's scheduled daily backup, read off the phone or from its OneDrive folder | [android-texts.md](android-texts.md#making-scheduled-backups-to-onedrive-on-the-phone) | each morning; texts mirror the phone, calls are remembered | 14 days |
| Texts (Mac + iPhone) | Messages in iCloud fills the Mac's own Messages store | [texts.md](texts.md#reading-a-macs-messages-directly-mac_messages), Full Disk Access | each morning | 14 days |
| Calls (Mac + iPhone) | *Calls from iPhone* fills the Mac's call history | [calls.md](calls.md) | each morning | 14 days |
| Texts (Windows + iPhone) | an unencrypted Apple Devices backup; over Wi-Fi while the phone charges if Apple Devices offers it **(verify)**, else the cable now and then | [texts.md](texts.md#making-the-backup-windows) | each new backup is collected the next morning | 14 days |
| Health (Android) | Health Connect's scheduled daily export into OneDrive, or into Google Drive synced to the laptop | [health-connect.md](health-connect.md#phone-side-setup-once) | each morning takes the newest export | 14 days |
| Places visited | GPSLogger writes one track file a day; read off the phone or synced to OneDrive › GPSLogger | [gps_track.md](gps_track.md) | visits each morning | 7 days |
| Browser history | the laptop's own Chrome / Edge / Brave / Safari history; the phone's browser, signed into the same account, syncs into it | [browser.md](browser.md) — nothing on the laptop | each morning | none, by design |
| Photos (metadata only, never the pictures) | Gallery › *Sync with OneDrive* or the OneDrive app's *Camera upload* (Android; iPhone + Windows), or the Mac's Photos library with iCloud Photos on | [photos.md](photos.md#where-your-photos-are) | each morning | none, by design |
| Calendar | Google Calendar's *Secret address in iCal format*, connected once | [online.md](online.md#google-calendar--google_calendar--the-calendar-connector) | downloaded each morning; a deleted event leaves the archive | — |
| Email | Gmail over IMAP with an app password, connected once; read-only | [gmail.md](gmail.md) | all new mail each morning; after the first yes, older mail 2,000 a morning until the oldest | — |
| Contacts | Google Contacts through an OAuth client of your own (ten minutes, once); or the Mac's Contacts with iCloud Contacts on | [google_contacts.md](google_contacts.md), [contacts.md](contacts.md) | the whole address book each morning; a deleted contact leaves the archive | — |
| Music | Spotify scrobbling to Last.fm, plus a free Last.fm API key, connected once | [lastfm.md](lastfm.md) | new scrobbles each morning; the history backfilled 10,000 a morning | 14 days |
| Notes (Mac) | Apple Notes' own store with iCloud Notes on | [notes.md](notes.md) | each morning | — |
| Notes (any setup) | a synced folder of your own `.md` / `.txt` notes, named once in `[connectors.notes] path` — never the Life CRM vault itself, whose notes are generated from the database; on a Mac the path replaces Apple Notes as what `notes` reads | [notes.md](notes.md) | each morning | — |

### B. Requested by hand, then found by itself

These services have no personal API and no scheduled export. You request
the download (a few times a year is plenty); the morning after it lands
where its connector looks, the job asks whether to import it, and your yes
imports it. Nothing to type but the yes.

| source | request it at | where to put it | connector |
|---|---|---|---|
| Google searches and YouTube | takeout.google.com › *My Activity*, *YouTube and YouTube Music › history*, JSON | the `takeout-….zip` in Downloads, zipped | `google_activity` ([google_activity.md](google_activity.md)) |
| Keep notes, mail before Gmail was connected, Google Photos history | the same Takeout, *Keep* / *Mail* / *Google Photos* | Downloads | `notes`, `email` (found there); photos by hand: `lifecrm observe photos "<zip>"` ([photos.md](photos.md)) |
| Instagram and Messenger | Accounts Center › *Download your information*, **JSON**, all time | `instagram-….zip` / `facebook-….zip` in Downloads | `meta_messages` ([meta_messages.md](meta_messages.md)) |
| Telegram | Telegram Desktop on the laptop › *Export Telegram data*, machine-readable JSON | its own `Downloads\Telegram Desktop\DataExport_…` | `telegram` ([telegram.md](telegram.md)) |
| WhatsApp | a chat › *Export chat*, Quick Share / AirDrop / Save to Files | Downloads | `whatsapp` ([whatsapp.md](whatsapp.md)); set `[connectors.whatsapp] me` first |
| LinkedIn | Settings & Privacy › Data privacy › *Get a copy of your data* | Downloads | `linkedin` ([linkedin.md](linkedin.md)) |
| Spotify (the years before scrobbling) | spotify.com › Account › Privacy settings › *Download your data* | `my_spotify_data.zip` in Downloads or OneDrive | `spotify` ([spotify.md](spotify.md)) |
| Workouts | Strong / Hevy › export from the app; Strava › *Request Your Archive* | Downloads, OneDrive or iCloud Drive | `fitness` ([fitness.md](fitness.md)) |
| Kindle highlights, Goodreads | plug the Kindle in; Goodreads › *Export Library* | the Kindle itself; `goodreads_library_export.csv` in Downloads | `reading` ([reading.md](reading.md)) |
| Location history (before GPSLogger) | Google Maps Timeline › *Export Timeline data* | `Timeline.json` in Downloads or OneDrive | `location` ([location.md](location.md)) |
| Health (iPhone) | Health › your picture › *Export All Health Data* | AirDrop to the Mac (Downloads), or Save to Files › OneDrive / iCloud Drive, top level | `health import` ([apple-health.md](apple-health.md)) |
| Contacts, calendar by hand (no Google) | a `.vcf` export; an `.ics` export | Downloads | `contacts`, `calendar` |

**Google Takeout can come on a schedule.** Takeout's delivery options include
*every 2 months for 1 year* and *Add to OneDrive*, which makes the
Takeout-only data (searches, YouTube, Keep) the next best thing to
automatic. The connectors look for Takeout zips in Downloads; if OneDrive
delivers them elsewhere **(verify)** the folder, point
`[connectors.google_activity] path` (and `notes`, `photos`) at it in
`config.toml`, or move each zip to Downloads.

**Health on an iPhone is the one source in A's spirit that stays in B.**
Apple Health has no scheduled export. Export it when you want newer data in
(monthly, say); the next morning takes the newest file. Everything else
about it is automatic.

### C. A folder you name once

Four connectors need a `path` in `config.toml`, because nothing on this
computer says where their files are. Name a folder once; every statement
you download into it is collected the next morning.

```toml
[connectors.finance]
path = "~/Documents/Statements"      # bank and card CSV / OFX / QFX  (docs/finance.md)
[connectors.payments]
path = "~/Documents/Statements"      # Venmo statements, PayPal activity CSV — the same folder is fine (docs/payments.md)
[connectors.nutrition]
path = "~/Downloads/cronometer"      # Cronometer's servings.csv + dailysummary.csv (docs/nutrition.md)
[connectors.rides]
path = "~/Downloads/Uber Data.zip"   # Uber's export; put each newer download at the same path (docs/rides.md)
```

`doctor` warns when the newest bank transaction (`finance`) or food-diary
day (`nutrition`) is over 45 days old. Banks have no personal API here, so
a statement download is the record; the alert and receipt emails Gmail
brings in every morning are the automatic trail in the meantime.

**The one exception.** `ai_chat` (Claude / ChatGPT exports) needs the
export's path every time and is the only connector the morning job never
looks for: `lifecrm observe ai_chat "<zip>"`, then `--apply`
([ai-chat.md](ai-chat.md)).

### What cannot be automated, and why

- **Apple Health** has no scheduled export (B, above).
- **Apple's own location history** (Significant Locations) cannot be
  exported at all; on an iPhone only Google Maps Timeline, or GPSLogger's
  equivalent, can reach here.
- **Spotify's API** returns only the last 50 plays, so listening comes
  through Last.fm scrobbling instead.
- **Banks and wallet apps**: no personal API, no export schedule.
- **Apple Notes on Windows** and **Samsung Notes** have no bulk export; a
  folder of text notes is the route (A, last row).
- **Instagram, WhatsApp, LinkedIn, Telegram** and anything Takeout-only:
  requested by hand (B).

## 2. Set it up, in order

One afternoon. Do the computer first, then the phone, then the online
services, then Obsidian; the first mornings then ask you about each source
once.

### 2.1 The computer

1. **Install and configure** — section 1–3 of your setup guide
   ([windows-android.md](windows-android.md#1-install),
   [windows-iphone.md](windows-iphone.md#1-install), [mac.md](mac.md#1-clone-it-somewhere-icloud-does-not-sync)),
   or the short version in [new-machine.md](new-machine.md). Keep the clone
   outside the cloud folder; keep the `VoiceJournal` folder *Always keep on
   this device* / *Keep Downloaded*.
2. **Register the jobs** — the whole schedule in the picture above, for your
   user, no admin:

   ```
   powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1     # Windows
   /bin/bash scripts/install-tasks.sh                                     # Mac
   ```

   On Windows the collect task also runs three minutes after you sign in,
   for the mornings the laptop was asleep at 06:30. On a Mac the jobs need
   the same Full Disk Access as the imports (2.2).
3. **See what is already here** — `lifecrm where` lists every source this
   computer holds, with the command for each; `lifecrm collect setup` is the
   same list as a checklist for your setup, each source *done*, *ready*
   (here, waiting for your yes), *to do* (with its steps) or *no* (declined).
   `--phone android | iphone-windows | iphone-mac` overrides the guess.

### 2.2 The phone, by setup

`lifecrm collect setup` prints these steps and keeps them current; here is
what they add up to.

**Windows 11 + Android.** The shortcut first: Quick settings › *Link to
Windows* on the phone, and on the laptop Settings › Bluetooth & devices ›
Mobile devices › *Show mobile device in File Explorer* **(verify)** the
wording ([android.md](android.md#no-sync-app-at-all-windows-11-optional)).
Then the laptop reads the phone's own folders over Wi-Fi, and three sources
need no sync app at all:

- **Voice notes and quick notes** — Samsung Voice Recorder saves to
  `Recordings/Voice Recorder` (and call recordings to `Recordings/Call`, held
  until you pick them); a quick-note app saves one `.txt` per note into a
  phone folder named `VoiceJournal-text`. Intake copies new files off the
  phone, once, never moving them ([android.md §1–2](android.md#1-voice-notes)).
- **Texts and calls** — SMS Backup & Restore: Messages and Call logs, *all*
  conversations, scheduled daily at night; leave the backup in its local
  folder ([android-texts.md](android-texts.md)).
- **Places** — GPSLogger: GPX, one file a day, saved to Internal storage ›
  GPSLogger, 60-second interval, distance filter 0, start on boot
  ([gps_track.md](gps_track.md#1-gpslogger-on-the-phone)).

Still through the cloud, because the phone writes them there:

- **Health** — Samsung Health › Health Connect, one app writing each metric,
  then Health Connect's *scheduled export* daily into OneDrive **(verify)**
  the destinations; to Google Drive, sync that folder to the laptop with
  Drive for desktop and add it as `[collect] health_dir`
  ([health-connect.md](health-connect.md#phone-side-setup-once)).
- **Photos** — Gallery › Settings › *Sync with OneDrive*, or the OneDrive
  app's Camera upload ([photos.md](photos.md#where-your-photos-are)).
- **Browser** — Chrome on the phone signed into the same Google account with
  Sync on; the laptop's Chrome then holds the phone's pages too.

Then **Never sleeping apps**: Settings › Battery › Background usage limits ›
add SMS Backup & Restore, GPSLogger (and Autosync, if you use it), and
Battery: *Unrestricted* for each ([android.md](android.md#keep-samsung-from-killing-the-sync)).
One UI stops a background app without a word, and a stopped app looks
like a quiet day.

Without Link to Windows — or beside it — the sync route is Autosync for
OneDrive: a folder pair `Recordings/Voice Recorder` → `VoiceJournal/inbox`
(the free tier's one pair), and paid pairs or FolderSync for `GPSLogger` and
`VoiceJournal-text`; SMS Backup & Restore uploads to OneDrive by itself. The
phone and laptop must be on the same Wi-Fi for the shortcut, so a phone that
is away reads as "not answering" and the morning run skips it quietly; the
sync route has no such condition. `[phone] read = false` turns the shortcut
off.

**Windows + iPhone.** The two Shortcuts, *Log My Day* and *Quick Note*,
saving to OneDrive's `VoiceJournal` ([iphone.md](iphone.md),
[windows-iphone.md §4](windows-iphone.md#4-the-iphone)). Texts come from an
**unencrypted** Apple Devices backup, each one collected the next morning
([windows-iphone.md §7](windows-iphone.md#the-iphone-backup-texts)); photos
from the OneDrive app's Camera upload; browsing from Edge or Chrome on the
iPhone signed into the laptop's account (Safari syncs nowhere on Windows).
Apple Health: *Export All Health Data* › Save to Files › OneDrive, top
level, when you want it in. Contacts and calendar: Google, connected once
(2.3), or iCloud.com exports by hand. There is no call history and no Notes
on this setup, by Apple's design.

**Mac + iPhone.** The two Shortcuts, saving to iCloud Drive's `VoiceJournal`
([mac.md §3](mac.md#3-the-iphone)). Then four switches and one permission:
*Messages in iCloud* on both devices; FaceTime › *Calls from iPhone* on the
Mac and Phone › *Calls on Other Devices* on the iPhone; iCloud Contacts,
Notes and Photos on in the Mac's apps; and **Full Disk Access** for your
terminal, plus `/bin/bash` and the venv's Python for the scheduled jobs
([mac.md §5](mac.md#5-full-disk-access)). Safari's history syncs from the
iPhone through iCloud. Apple Health: *Export All Health Data* › AirDrop to
the Mac when you want it in.

### 2.3 The online services

Four sources live only on a service's servers; for those there is no export
to make. Connect each once, in your own terminal, and the morning job
downloads what is new before it reads anything ([online.md](online.md)):

```
lifecrm connect                                   # the services, their steps, which are connected
lifecrm connect google_calendar                   # paste a calendar's "Secret address in iCal format"
lifecrm connect gmail you@gmail.com               # a Google app password (2-Step Verification on)
lifecrm connect google_contacts "<client>.json"   # your own Google Cloud OAuth client; the browser opens
lifecrm connect lastfm <username>                 # Spotify scrobbling on at last.fm; a free API key
lifecrm fetch gmail                               # download now, import nothing
```

What each one needs is typed by you, hidden, and kept in
`data\secrets.json` — never in `config.toml`, never printed, never in a
result. Fetching is read-only on every service (mail is not marked read;
nothing is changed), and it is the daily job's only network use.
`disconnect <service>` forgets the secret; what was imported stays.

Google Contacts is the one with a real setup (a Google Cloud project with
the People API, a consent screen published to *In production*, a Desktop
app client): [google_contacts.md](google_contacts.md) walks it step by step.

### 2.4 Obsidian

Section 4, below. Two lines in `config.toml` and one `lifecrm open`.

### 2.5 The first mornings

Every source's **first import is yours**: that is where a wrong unit, a
misread file or a second phone shows. The morning after a source arrives,
the job holds it and asks — in `/morning`, on the vault's Home note under
*Today*, in `lifecrm nudges`, and in `lifecrm collect status`:

> New on this computer: texts and calls. Import it?
> 12,034 record(s) from 2023-03-02 to 2026-10-24, in …

Answer each one once:

```
lifecrm collect approve android_sms      # yes: the dry run (counts, days, sample rows), then the import on your yes
lifecrm collect approve android_sms --yes
lifecrm nudges snooze collect:android_sms:first_import --days 7     # not now
lifecrm collect decline android_sms      # never; `collect approve` takes it back
```

In Claude Code, `/morning` asks the questions and runs the answers. Read the
dry run's sample rows against what the phone shows before the yes; a source
whose first-real-import checklist is still open is named in the summary.
From then on that source comes in every morning by itself.

Then give the imports their people, once per handle — a phone number, an
email address, an account — and every past and future record that used it
is attributed:

```
lifecrm handles list --suggest                          # candidates from your contact cards
lifecrm handles link phone:+15125550142 --entity <id>   # or --new "Jake Morales"
lifecrm handles ignore <kind:value>                     # spam, delivery notices
```

Nothing is linked automatically, and relationships are never inferred.

## 3. Every day afterwards

**Morning.** Open Obsidian on **Home** (or type `/morning` in Claude Code).
*Today* lists what matters as of the database's last change — due tasks and
plans, birthdays from linked contact cards, nutrients below reference,
imports gone stale, the daily job's questions and failures, the agenda — each
line ending with the key the CLI takes. Three answers cover it:

```
lifecrm collect approve <source>            # yes to a new source or a held removal
lifecrm nudges snooze <key> --days 7        # not now
lifecrm nudges dismiss <key>                # never (restore <key> undoes; collect decline <source> for a source)
```

**Evening, or the next day.** Record the day's note; intake has usually
transcribed it by the time you sit down. Type `/log`: all of the day's notes
are extracted as one draft, you review the diff, confirm, and it applies.
The vault refreshes by itself after the apply. This review is the one manual
step that matters, and it is meant to stay manual.

**Weekly.** The Monday job writes the week's review pack; `/review-week`
writes the brief from it, and `/advise <lens>` the advisor's view
([review.md](review.md)). Every quarter or so, request the group B downloads
(a calendar reminder is enough) and drop them in Downloads.

**Keep an eye on what went quiet.** A source that stops is the one failure
that looks like a quiet life. It is caught three ways: `doctor` (08:00,
`data\doctor.log`) warns `<connector>_data_age` once a connector's newest
record is older than its limit (section 1's last column), the vault's
**Sources** note marks the connector *stale*, and *Today* carries a
`stale:<connector>` nudge. The usual causes:

| quiet source | first look at |
|---|---|
| texts, places, voice notes (Android) | the app still in *Never sleeping apps* and *Unrestricted*; GPSLogger's notification still showing; the phone on the same Wi-Fi as the laptop (`lifecrm collect status` says whether it answers); OneDrive running and not paused |
| anything through the phone in File Explorer | the phone was away at 06:30 — the next run catches up, nothing is lost; the collect task also runs after logon |
| health (Android) | Health Connect's scheduled export still on; the export landed where `collect` looks (top of OneDrive, one folder down, or `[collect] health_dir`) |
| Gmail, Google Contacts, Last.fm | `lifecrm collect status`: a failed fetch is an **error**, never a silent skip; a revoked app password or an ended Google sign-in says "needs connecting again" — run the same `connect` command |
| texts, calls, Contacts, Notes, Photos (Mac) | Full Disk Access for the terminal *and* the jobs (`/bin/bash`, the venv's Python); /morning shows every refused source as one item |
| texts (Windows + iPhone) | a backup was made since (the cable, or Wi-Fi backup if your Apple Devices does it) |

Every outcome is a row: `lifecrm collect status` shows each source's last
run, `data\collect.log` the run itself. An approved or collected import is
an ordinary import — `lifecrm imports list`, `imports retract <uuid>` hides
what it added, `imports restore <uuid>` puts it back.

**On the phone.** The vault, synced (section 4); the reminders calendar
(`[assistant] reminders_dir`, a synced folder, or `lifecrm serve` at
`/reminders.ics`) for due items and birthdays ([assistant.md](assistant.md#the-reminders-calendar));
and the web UI over Wi-Fi for questions ([web.md](web.md#on-your-phone)).

## 4. Obsidian as the main screen: setup

The vault is a read-only rendering of the database — every day, person,
place and thing as a note, cross-linked, with four start pages — kept
current by the CLI after every write. Nothing in it is ever read back; a
change goes through the CLI or `/log`, and the vault follows
([obsidian.md](obsidian.md#read-only-by-design)).

**1. Two lines in `config.toml`:**

```toml
[gui]
main = "obsidian"                     # what `lifecrm open` opens; "web" is the default

[obsidian]
vault = "~/OneDrive/LifeCRM vault"    # Mac: "~/Library/Mobile Documents/iCloud~md~obsidian/Documents/LifeCRM"
# auto_refresh = true                 # the default once a vault is set
```

Where to put it: a folder your phone can reach (OneDrive on Windows;
Obsidian's own iCloud Drive folder on a Mac, which Obsidian on the iPhone
lists by itself), or `vault` at the repo root, which is gitignored, when the
laptop is the only screen. A relative path is relative to the project
folder. Its own folder, or a subfolder of a vault you already have, both
work; in a shared vault keep the *Daily notes* plugin's date format
different from `YYYY-MM-DD`, which the journal notes use.

**2. Preview, then make it:**

```
lifecrm obsidian refresh --dry-run    # + new, ~ changed, - removed, kept = a file of yours in the way
lifecrm obsidian refresh
```

**3. Open it.** Install Obsidian, then:

```
lifecrm open                          # refresh, then open Obsidian on Home
```

The first time, Obsidian does not know the folder yet, so its vault manager
opens instead (on Windows behind your other windows — find it on the
taskbar): choose **Open folder as vault** and pick the vault folder. From
then on `lifecrm open` lands on Home. Bookmark Home inside Obsidian (the
core Bookmarks plugin) to keep it one click away.

**4. It stays current by itself.** With `vault` set, every command that
changes the database — `/log`'s apply, the morning job's imports, `entry
close`, `handles link`, `restore` — re-renders the vault when it is done,
and prints one line (`obsidian vault refreshed: 3 note(s) changed`) when
anything changed. A read-only command never triggers it. If a refresh fails
(a sync client holding a note), the command's own result stands and the next
write's refresh catches up; `lifecrm obsidian refresh` does it now. The same
database always renders byte-identical files, so a synced vault does not
churn overnight.

**5. On the phone.**

- **Obsidian Sync** (paid): turn it on in the laptop's vault, connect the
  phone's Obsidian to the same remote vault. Android and iPhone alike.
- **iPhone + Mac:** the vault is already in Obsidian's iCloud folder (the
  path above); the iPhone's Obsidian lists it.
- **Android + Windows:** Obsidian on Android opens folders on the phone's
  own storage only, not OneDrive. Mirror the OneDrive vault folder to the
  phone with FolderSync or Autosync for OneDrive (download only), or with
  Syncthing between laptop and phone.

Treat the phone copy as read-only too: the next refresh overwrites an edit
made there. And the vault holds journal text, text snippets and phone
numbers — syncing it puts that on the sync provider
([privacy.md](privacy.md)).

**6. Your own notes beside it.** The export only touches files that carry
the front-matter line `lifecrm: generated`. A note of yours in the same
folder is never written, moved or deleted — the dry run reports it as
`kept`. To take over a generated note (to annotate a person's page), delete
its `lifecrm: generated` line; it is yours from then on and stops being
regenerated. Export from **one** computer: the manifest that lets a refresh
prune stale notes is a hidden file most sync tools skip.

## 5. Obsidian: reading, and acting on what you read

Every page is dated **as of the database's last change**, never by the
clock, so "overdue" and "the last 7 days" count back from the day the page
names.

| note | what it shows |
|---|---|
| **Home** | *Today* (section 3), the agenda with meeting prep under each event, the latest day and the three before it, open loops overdue first, the eight people most recently in touch and who has been quiet 30+ days, seven-day sparklines of sleep, steps and resting heart rate, the week's workouts, nutrients below reference, what the imports hold and what waits to be linked, *On this day*, yesterday's journal prompts, a few highlights |
| **People** | everyone, most recent contact first, by the rule `lifecrm ask` uses for "when did I last talk to X" (a text, an email between you, a connected call, a calendar event, a journal mention); then the people with no contact on record and those quiet 30+ days |
| **Health** | the last 30 days: each sensor metric with mean, min, max, trend and a sparkline, naming its export file; the journal's own measures; workouts per week; the `lifecrm nutrients` summary |
| **Sources** | each connector's records per stream, its newest record, its last import and whether it is **stale**; the handles waiting to be linked, with the command; health export files; the journal's counts and drafts waiting for review; the last ten import runs |
| **Trips** | home, trips newest first, countries — with the places feature and a local GeoNames file (`lifecrm places fetch`, once) |
| **Index**, **Loops** | recent days, people, places and what was imported; open tasks and plans, soonest due first |
| `Journal/<year>/<date>.md` | front matter (`date`, `mood`, `energy`, `sleep`, `tags`); *On this day* links; the summary; a **Health** line from the sensors naming its file; the entries by category with their transcript passage as a quote callout and their uuid in a hidden comment; an **Observed** section — counts per stream, then up to ten notable items (events, visits, workouts, texts and emails with linked people, calls, notes, highlights, AI chats), each ending in `observed` and the uuid behind it |
| `People/<name>.md`, `Places/…`, … | type, uuid and aliases in front matter; first and last seen; the latest contact across both tiers, cited; the handles linked to the person; every journal day and every imported day they appear in |

**Reading.** Backlinks and the graph view work with no plugins: a person's
note lists every day they appear in, a day note links every person in it.
Obsidian's search finds any text in any note. With the Dataview community
plugin, the day notes' front matter answers questions of its own:

```dataview
TABLE mood, energy, sleep FROM "Journal" WHERE mood SORT date DESC LIMIT 14
```

**Acting.** The vault is one way, so everything you decide goes through the
CLI, and the vault follows on the next refresh:

| you see | you run |
|---|---|
| a loop in **Loops** that is done | `lifecrm entry close <uuid> --closure done` (`dropped` / `obsolete`; `entry reopen`) — the uuid is in the entry's hidden comment |
| a nudge under *Today* you want gone for a week, or for good | `lifecrm nudges snooze <key> --days 7` / `nudges dismiss <key>`; `nudges restore <key>` |
| "New on this computer: …" under *Today* | `lifecrm collect approve <source>` / `collect decline <source>` |
| a handle under **Sources › Waiting to be linked** | `lifecrm handles link <kind:value> --entity <id>` or `--new "<name>"`; `handles ignore <kind:value>` |
| two notes for one person (a mishearing) | `lifecrm entities merge <src-id> <dst-id>`; `entities alias-add <id> "<spelling>"` for good |
| an entry with the wrong title or category | `lifecrm entry edit <uuid> --title "..." --primary-category <name>`; `entry retract <uuid>` / `entry restore` |
| a relationship worth recording ("Jake from El Paso") | `lifecrm entities link add <from-id> <to-id> --type from` |
| an import that should not have come in | `lifecrm imports retract <uuid>` (`imports restore` puts it back); health: `health retract --source-file "<name>"` |
| a connector marked **stale** on Sources | section 3's table |

**What stays outside Obsidian.** Questions: Obsidian shows the data and
cannot answer. `lifecrm ask "when did I last talk to Mom"` answers from the
database with ids, `/ask <question>` in Claude Code writes the prose and
checks every citation, the web UI and `lifecrm voice` do the same on the
phone or aloud. Every `O#` an answer cites is the uuid a day note carries,
so a cited answer leads back into the vault.

## 6. Troubleshooting

| symptom | look at |
|---|---|
| Home's *Today* still shows a question you answered | the approve or decline re-renders the vault when it runs; `lifecrm obsidian refresh --dry-run` shows whether anything is left to write, and `lifecrm nudges` is the live list |
| the vault did not refresh after a write | `[obsidian] vault` set and `auto_refresh` not `false`; `LIFECRM_NO_VAULT_REFRESH` not set in that shell; the warning line on stderr (a sync client holding a note); `lifecrm obsidian refresh` catches up |
| `lifecrm open` opens the vault manager every time | Obsidian only opens links into vaults it knows: choose *Open folder as vault* once on the exact folder; a vault inside a folder Obsidian already knows counts as known |
| a person's page stopped updating | its `lifecrm: generated` line was deleted — the page is yours now; restore the line to hand it back |
| the phone's Obsidian shows old data | the sync: Obsidian Sync paused, FolderSync's schedule, or the laptop had nothing to re-render (the pages are dated as of the last change, not the clock) |
| `collect status` says the phone is not answering | same Wi-Fi; Link to Windows signed in; *Show mobile device in File Explorer* on; one phone at a time |
| a source is `missing` or `error` every morning | `lifecrm collect status --json` and `data\collect.log` name the reason; a connector that found two candidates (two backups, two camera folders) wants the one to use in `[connectors.<name>] path` |
| a first import looks wrong in the dry run | do not approve; the summary names the connector's doc and its first-real-import checklist; `collect decline` if it is a source you never want |
| macOS: "macOS is keeping the daily import out of …" | Full Disk Access for `/bin/bash` and the venv's Python ([mac.md §5](mac.md#5-full-disk-access)), then let the next run try |
| Gmail / Google Contacts "needs connecting again" | the app password was revoked or Google ended the sign-in: the same `lifecrm connect …` once more |
| the whisper model download, or a slow first transcription | once, ~1.5 GB; `[whisper] model = "small"` if every day is slow |

## 7. What goes where

- **Secrets** (`data\secrets.json`): the iCal links, the Gmail app password,
  the Google refresh token, the Last.fm key — on this computer only, never
  in `config.toml`, never printed.
- **Fetched files** (`data\fetched\<service>\`) and everything else under
  `data\` and `backups\`: private, gitignored as a whole.
- **The cloud folder**: the phone's recordings and text notes, the
  database snapshots, and — if you sync it — the vault, which holds journal
  text, message snippets and phone numbers. [privacy.md](privacy.md) says
  what that means before you decide you are fine with it.
- **The services**: read-only; nothing is sent to any of them but the
  request for your own data, and the daily job's only network use is those
  fetches. The phone in File Explorer uploads nothing anywhere.

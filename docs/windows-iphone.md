# Windows laptop + iPhone — the whole setup

One page from nothing to a working journal on a **Windows 10/11 laptop** with
an **iPhone**: install, configure, connect the phone, schedule the
background work, bring in each of the phone's data sources, and look at the
result from the laptop or the phone. The other two setups have their own
pages: [windows-android.md](windows-android.md) (Windows + Samsung) and
[mac.md](mac.md) (MacBook + iPhone). The database, the commands and the
`/log` review loop are the same on all three.

What is different here, in one paragraph: the journal lives in **OneDrive**,
as on the Windows + Samsung setup, and the iPhone's Shortcuts save into it
through the OneDrive app. Everything else the iPhone holds reaches Windows
by one of four doors: an **unencrypted backup** made by the Apple Devices
app (texts), a file the phone **saves to OneDrive** (Apple Health, Apple
Card, WhatsApp), **iCloud.com** in the browser (contacts, calendar), and
**iCloud for Windows** (photos). Two things have no door at all on Windows:
the iPhone's **call history** and **Apple Notes**.

Phone-side detail (the two Shortcuts, their triggers, what to verify on the
real phone) lives in [iphone.md](iphone.md); this page links to it instead
of repeating it. Steps nobody has yet checked on a real Windows laptop with
a real iPhone are marked **(verify)**.

Time: about 30 minutes at the laptop and 15 on the phone, plus a one-time
~1.5 GB whisper model download and the first iPhone backup (as big as the
phone's own data).

**Commands.** Everything runs from the repo folder. `lifecrm …` below is
short for:

```
.venv\Scripts\python.exe -m lifecrm …
```

Set `PYTHONUTF8` once so names with accents and emoji print correctly in
every new terminal: `setx PYTHONUTF8 1` (the scheduled-task scripts set it
themselves).

## 0. What you need

- The laptop, with **git** (`winget install Git.Git`), about 3 GB free for
  the project and the model, and room for an iPhone backup.
- **Claude Code** with a Pro or Max login — extraction (`/log`) runs on that
  subscription; nothing here calls a metered API.
- **OneDrive** signed into one Microsoft account on the laptop, and the free
  **OneDrive** app on the iPhone signed into the same account.
- **Apple Devices** from the Microsoft Store (or iTunes, on older setups) and
  a cable for the iPhone — for the texts.
- **iCloud for Windows** from the Microsoft Store, signed into the iPhone's
  Apple Account — for the photos (optional).
- On the iPhone: **Shortcuts** (built in). For the data sources you want,
  the apps you already use (WhatsApp, Wallet, Health, Strong, ...).

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

Keep the clone **outside** OneDrive and outside iCloud Drive (for example
`C:\Users\<you>\lifecrm`): the database must not be cloud-synced while it is
open. Its snapshots go to OneDrive on purpose, through `backup`.

## 2. The OneDrive folders

`lifecrm configure` (run by `setup.ps1`) creates these inside your OneDrive
folder; it never creates OneDrive itself, so sign in to OneDrive first. To
make them by hand, create in Explorer:

```
OneDrive\VoiceJournal\inbox         <- audio notes from the iPhone (.m4a)
OneDrive\VoiceJournal\inbox-text    <- dictated text notes (.txt)
OneDrive\VoiceJournal\archive       <- processed files move here
OneDrive\VoiceJournal\backups       <- database snapshots
```

Right-click `VoiceJournal` › **Always keep on this device**. Without it,
OneDrive may keep only online placeholders, and intake would have to
download each recording before reading it (it can, with retries — but a
laptop that is offline at 07:00 then has nothing to read).

A work or school account names the folder `OneDrive - <Organization>`; use
whichever OneDrive the iPhone's OneDrive app signs into.

## 3. Configure

```
lifecrm configure
```

It writes `config.toml`: the four OneDrive folders and Windows' timezone as
an IANA name. Everything else is `config.example.toml`'s defaults, comments
included. To start over, `lifecrm configure --force` (the replaced file is
kept under `data\`); `--journal-root`, `--timezone` and `--no-mkdir` are the
other options. The keys that matter:

```toml
[paths]
inbox_dir = '~\OneDrive\VoiceJournal\inbox'
inbox_text_dir = '~\OneDrive\VoiceJournal\inbox-text'
archive_dir = '~\OneDrive\VoiceJournal\archive'
cloud_backups_dir = '~\OneDrive\VoiceJournal\backups'

[time]
timezone = "America/Chicago"     # your IANA zone
day_rollover_hour = 5            # notes before 05:00 count as the previous day
filename_date = "auto"           # the iPhone's file names carry the journal date

[owner]
name = "Your Name"
handles = ["phone:+15125550100", "email:you@icloud.com"]
```

(`~` is your user folder. Single quotes keep the backslashes literal.)

The Shortcuts in iphone.md name every note `<journal date> <clock time>
<utc offset>`, for example `2026-07-27 21.30.05 -0500.m4a`. With
`filename_date = "auto"` that shape — a dotted time and an offset — is read
as the **journal** date, already shifted by the Shortcut's "subtract 5
hours", so the rollover is not applied twice. Keep `day_rollover_hour` equal
to the hours the Shortcut subtracts. `lifecrm filedate "<file name>"` prints
the journal day and recording time intake will assign.

`[owner]` says which phone number and address are yours; email, payments and
LinkedIn use it to tell what you sent from what you received.

Then check what the pipeline will read and write, and that it is happy:

```
lifecrm where
lifecrm doctor
```

`where` prints the config file, each inbox (with how many notes are
waiting), the archive and cloud backups folders, the iPhone backups it
finds, and any export sitting in Downloads, OneDrive or the journal folder,
each with the command that reads it. `doctor` warns about a folder that
does not exist — the classic typo that otherwise looks like a quiet day.

## 4. The iPhone

Follow **[iphone.md](iphone.md)**: two Shortcuts, **Log My Day** (audio) and
**Quick Note** (dictated text). With a Windows laptop, save to **OneDrive**:

```
OneDrive/VoiceJournal/inbox         <- audio notes (.m4a)
OneDrive/VoiceJournal/inbox-text    <- dictated text notes (.txt)
```

Install the OneDrive app first, sign in and open it once, then make sure the
Files app lists it (Files › Browse › ⋯ › Edit: OneDrive on), so Shortcuts'
**Save File** can offer it as a location **(verify)**.

**The OneDrive caveat (verify).** On some iOS versions **Save File** only
pins a fixed destination inside iCloud Drive; for OneDrive it shows the file
picker every time. If yours does, leave *Ask Where to Save* on — two taps
per note, and OneDrive is remembered as the last place — or use the Voice
Memos fallback in iphone.md. Report which your iPhone does (iphone.md's
checklist).

**If you would rather avoid the picker:** save to **iCloud Drive** instead,
where Save File can always pin a folder, and let iCloud for Windows bring it
to the laptop. Turn on *iCloud Drive* in iCloud for Windows, then point the
config at its folder:

```
lifecrm configure --force --journal-root "~\iCloudDrive\VoiceJournal"
```

Right-click that `VoiceJournal` folder › **Always keep on this device**, as
for OneDrive. Where iCloud for Windows puts iCloud Drive
(`%USERPROFILE%\iCloudDrive`) and how quickly it syncs are **(verify)**;
OneDrive is the tested path.

Then prove the timestamps survive the trip. Run Quick Note, dictate a
sentence, wait for sync, and on the laptop:

```
lifecrm filedate "2026-07-27 21.30.05 -0500.txt"
lifecrm intake
lifecrm status
```

## 5. Background work: scheduled tasks

```
powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1
```

This registers, for your user (no admin):

| task | when | does | log |
|---|---|---|---|
| LifeCRM intake | daily 07:00 and at logon | transcribes audio, imports text notes | `data\transcribe.log` |
| LifeCRM doctor | daily 08:00 | doctor against the live database | `data\doctor.log` (WARN/FAIL lines) |
| LifeCRM backup | daily 21:30 | tiered snapshots, local + OneDrive | — |
| LifeCRM verify-restore | Sundays 12:00 | `lifecrm verify-backup`: restores the newest snapshot into a scratch folder, runs doctor on it, checks every table against live | `data\verify-restore.log` |
| LifeCRM review | Mondays 07:30 | the review pack for the week that just ended, in `data\staging\`, for `/review-week` | `data\review.log` |

Every task starts on battery and keeps running when the charger is pulled;
one due while the lid is closed runs when the laptop wakes; intake also runs
at every logon; a failed run shows *Last Run Result* `0x1` and names the
file in its log ([windows-android.md](windows-android.md#5-background-work-scheduled-tasks)
has the details, the same on this setup).

The iPhone's own data is not on a schedule: an import reads a backup or a
download you made, so it runs when you have one (section 7).

## 6. The daily loop

1. During the day: **Quick Note** from the Action Button, Back Tap or Siri,
   ten seconds of talking.
2. Evening: **Log My Day**, one longer note about the day.
3. At the laptop: intake has usually run already (or run `lifecrm intake`).
   Open Claude Code in the repo folder and type **`/log`**. It drafts the
   day as one extraction, you review the diff, confirm, and it applies.

```
lifecrm status        # pending notes, held calls, open loops, streak
lifecrm intake        # process both inboxes now (call recordings stay held)
lifecrm calls list    # held call recordings: `calls take <n>` / `calls skip <n>`
lifecrm doctor        # prove the database is intact
```

A call you recorded on the iPhone (iOS 18.1+) is saved in Notes; to journal
it, share its audio to `VoiceJournal/inbox`. A file named `Call…` is held
until you pick it ([iphone.md](iphone.md#call-recordings--only-the-ones-you-choose)).
A note on the wrong day: `lifecrm transcribe --file "<path>" --date
2026-07-27` (`--date` requires `--file`), or tell `/log` the date.

## 7. Each data source

Everything below is the **observed tier**: bulk imports that never pass the
review gate. Every import is a **dry run by default** — read the report's
counts, date spans and sample lines; then run the same command with
`--apply`. Re-running is always safe (records dedupe on the source's own ids).

### The iPhone backup (texts)

The iPhone has no export for its messages. On Windows the one way in is a
local backup, which stores the Messages database as a plain file when the
backup is **not encrypted**:

1. Open **Apple Devices**, connect the iPhone by cable, unlock it and tap
   *Trust*.
2. Under *Backups*: **Back up all of the data on your iPhone to this
   computer**, and make sure **Encrypt local backup is unticked**. An
   encrypted backup cannot be read without its password; the connector says
   so and stops.
3. **Back Up Now**. It is written to
   `%USERPROFILE%\Apple\MobileSync\Backup\<device id>\` (desktop iTunes:
   `%APPDATA%\Apple Computer\MobileSync\Backup`) — the folders the connector
   searches **(verify)**.

```
lifecrm observe ios_messages              # dry run: finds the one backup
lifecrm observe ios_messages --apply
```

With several backups (two phones, an old one) it lists them with device name
and date and asks for one; it never guesses. The report names the device
and the backup date, the account (your number), the span of messages ("check
the oldest against what the phone shows") and five raw numbers as they were
normalized, with the names the phone's own address book gives them.
[texts.md](texts.md) has the whole story: tapbacks, edits, unsends,
Recently Deleted, and why the archive mirrors the phone.

Each later backup adds what is new. With *Show this iPhone when on Wi-Fi*
on, Apple Devices can back up while the phone charges on the same Wi-Fi
**(verify)**; otherwise connect the cable now and then, and re-run the
import after each backup. With **Messages in iCloud** on and the phone set
to keep messages for a limited time, the backup holds only what the phone
still has — the dry run's span shows it.

**No calls.** Apple keeps the call history (`CallHistory.storedata`) only in
**encrypted** backups, and an encrypted backup cannot be read. So on this
setup there is no call history to import: `lifecrm observe ios_calls` on an
ordinary backup refuses it with one line pointing at [calls.md](calls.md),
rather than importing nothing. Whether current iOS really leaves it out is
on calls.md's checklist **(verify)** — if your backup turns out to have it,
the same command imports it. Calls you recorded are a separate matter
(section 6).

### Everything else

| source | on the iPhone / iCloud side | lands on the laptop | command |
|---|---|---|---|
| Voice notes | Log My Day (iphone.md) | `OneDrive\VoiceJournal\inbox` | `lifecrm intake` (scheduled) |
| Text notes | Quick Note (iphone.md) | `OneDrive\VoiceJournal\inbox-text` | `lifecrm intake` (scheduled) |
| Texts | Apple Devices backup (above) | `%USERPROFILE%\Apple\MobileSync\Backup\<id>` | `lifecrm observe ios_messages` |
| Health | Health › your picture › **Export All Health Data** › *Save to Files* › OneDrive (top level) | `OneDrive\export.zip` | `lifecrm health import` (finds the newest at the top of Downloads, iCloud Drive or OneDrive; or pass the path) ([apple-health.md](apple-health.md)) |
| Contacts | icloud.com › Contacts › select all (Ctrl+A) › **Export vCard…** **(verify)** | `Downloads\<name>.vcf` | `lifecrm observe contacts "<file>.vcf"` ([contacts.md](contacts.md)) |
| Calendar | icloud.com › Calendar › share the calendar as a **Public Calendar**, copy its `webcal://` link, open it as `https://` to download, turn sharing off again **(verify)** | `Downloads\Home.ics` | `lifecrm observe calendar "<file>.ics"` ([calendar.md](calendar.md)) |
| Photos (metadata) | iCloud for Windows › Photos › *iCloud Photos* | `%USERPROFILE%\Pictures\iCloud Photos\Photos` **(verify)** | `lifecrm observe photos "<folder>"` ([photos.md](photos.md)) |
| Browser history | Edge or Chrome on the laptop (and on the iPhone, signed in, so its pages sync) | the laptop's Edge/Chrome profile | `lifecrm observe browser` ([browser.md](browser.md)) |
| WhatsApp | chat name › **Export Chat** › *Save to Files* › OneDrive | `OneDrive\WhatsApp Chat - <name>.zip` | `lifecrm observe whatsapp "<zip>"`; set `[connectors.whatsapp] me` first ([whatsapp.md](whatsapp.md)) |
| Apple Card | Wallet › Apple Card › Card Balance › a monthly statement › **Export Transactions** › CSV › *Save to Files* › OneDrive | `Apple Card Transactions - <Month YYYY>.csv` | `lifecrm observe finance "<file or folder>"` ([finance.md](finance.md)) |
| Bank | the bank's website on the laptop | a `.csv` / `.ofx` / `.qfx` in Downloads | `lifecrm observe finance "<file or folder>"` |
| AI chats | Claude / ChatGPT data export | a `.zip` in Downloads | `lifecrm observe ai_chat "<zip>"` ([ai-chat.md](ai-chat.md)) |
| Location | only Google Maps with Timeline on, on the iPhone: › Your Timeline › ⋯ › Location & privacy settings › **Export Timeline data** | `location-history.json` | `lifecrm observe location "<file>"` ([location.md](location.md)) |

Per source, what to know on this setup:

- **Contacts: one route.** Import the iCloud vCard, not the backup's address
  book: the two hold the same cards under different ids, so importing both
  shows everyone twice (the backup's address book still names the texts'
  handles — that needs nothing from you). A vCard does not say whose address
  book it is, so name it once and the export mirrors your deletions:
  ```toml
  [connectors.contacts]
  account = "icloud:you@icloud.com"            # any stable label
  path = '~\Downloads\iCloud Contacts.vcf'     # the export to mirror
  ```
- **Calendar: say the name is yours.** iCloud's `Home.ics` is one calendar
  named *Home*, not an address, so for events you delete to leave the archive
  too, list it: `[connectors.calendar] mirror_calendars = ["Home"]`. Repeat
  the download for each calendar you want (calendar.md, *Deleted events*).
  With iCloud for Windows' *Calendars* in classic Outlook, Outlook's *File ›
  Save Calendar* `.ics` reads the same way.
- **Photos.** iCloud for Windows downloads HEIC photos as JPEG unless *Keep
  high efficiency original if available* is ticked **(verify)**; both read
  the same, and only metadata is read — time, zone, place, camera, never the
  pixels. Name the folder once, `[connectors.photos] path =
  '~\Pictures\iCloud Photos\Photos'`, and `observe photos` needs no path.
  Files iCloud has not downloaded are skipped and counted. The OneDrive app's
  *Camera upload* is the other route: it fills `OneDrive\Pictures\Camera
  Roll`, which `observe photos` finds with no path. Use one of the two.
- **Browser.** Safari on the iPhone syncs nowhere on Windows, so only what
  the laptop's browser holds comes in (plus the phone's pages, if you browse
  with Edge or Chrome on the iPhone signed into the same account). With both
  Edge and Chrome installed, `[connectors.browser] browser = "edge"` picks
  one.
- **Notes: none.** Apple Notes has no export on Windows (icloud.com shows
  notes but cannot export them), so there is nothing for `observe notes` to
  read. Notes you keep as `.md`/`.txt` files in a OneDrive folder (an
  Obsidian vault, say) import with `lifecrm observe notes "<folder>"`
  ([notes.md](notes.md)).
- **Location.** Apple's Significant Locations are encrypted on the phone and
  cannot be exported. Only Google Maps' Timeline, where you have it on,
  reaches here.

### The newer sources

Each of these is a download on the laptop, or a file the iPhone saves to
OneDrive, imported the same way: dry run, read it, `--apply`. `lifecrm
where` recognises every one of them in Downloads, OneDrive or the journal
folder by the name the app gives it, and prints the command.
[windows-android.md](windows-android.md#the-newer-sources) has the click
paths; on this setup:

| source | from | command |
|---|---|---|
| Email | Gmail: takeout.google.com › **Mail** › `takeout-….zip`. iCloud Mail has no export and there is no Apple Mail on Windows: add the account to **Thunderbird** (IMAP, folders kept for offline use) and pass its `ImapMail\<server>` folder, or a folder of `.eml` files. The iPhone's Mail app exports nothing | `lifecrm observe email ["<zip or folder>"]` ([email.md](email.md)) |
| Instagram, Messenger | Accounts Center › **Download your information** › Messages, JSON | `lifecrm observe meta_messages` ([meta_messages.md](meta_messages.md)) |
| Telegram | **Telegram Desktop** on the laptop › *Export Telegram data*, JSON (the iPhone app does not export) | `lifecrm observe telegram` ([telegram.md](telegram.md)) |
| Google searches, YouTube | takeout.google.com › **My Activity** + *YouTube and YouTube Music › history* | `lifecrm observe google_activity` ([google_activity.md](google_activity.md)) |
| Workouts | Strong / Hevy's export › *Save to Files* › OneDrive; Strava's `export_<id>.zip` from strava.com | `lifecrm observe fitness ["<file>"]` ([fitness.md](fitness.md)) |
| Spotify | spotify.com › **Download your data** (account data + extended history) | `lifecrm observe spotify "<folder>"` ([spotify.md](spotify.md)) |
| Kindle, Goodreads | the Kindle over USB (`documents\My Clippings.txt`); Goodreads' *Export Library* | `lifecrm observe reading` ([reading.md](reading.md)) |
| Food diary | cronometer.com › **Export Data** (`servings.csv`, `dailysummary.csv`) | `lifecrm observe nutrition "<folder>"`, then `lifecrm nutrients` ([nutrition.md](nutrition.md)) |
| LinkedIn | **Get a copy of your data** › `Basic_LinkedInDataExport_<date>.zip` | `lifecrm observe linkedin` ([linkedin.md](linkedin.md)) |
| Venmo, PayPal | venmo.com › **Statements**; paypal.com › **Activity report** | `lifecrm observe payments "<folder>"` ([payments.md](payments.md)) |
| Uber | Privacy Center › **Download your data** › `Uber Data.zip` | `lifecrm observe rides "<zip>"` ([rides.md](rides.md)) |

**Who is who.** Importers record phone numbers and email addresses
(*handles*), never guess people. After contacts and texts are in:

```
lifecrm handles list --suggest            # candidates from your contact cards
lifecrm handles link phone:+15125550142 --entity <id>
```

Your own number shows up as `you (this phone)`; link it to yourself once.
Linking a handle attributes every text, card and invite that used it; it is
always your decision.

**Undo.** `lifecrm imports list` shows every run; `lifecrm imports retract
<uuid>` takes one back out (kept on disk, hidden everywhere), and `lifecrm
imports restore <uuid>` puts it back. Health:
`lifecrm health retract --source-file "export.zip"` (and `health restore`).

## 8. Asking and looking (on the laptop)

```
lifecrm ask "how did I sleep last week"
lifecrm serve                     # the web UI: http://127.0.0.1:8765/  (Ctrl+C stops)
lifecrm dashboard                 # http://127.0.0.1:8001/-/dashboards  (Ctrl+C stops)
lifecrm voice --text "when did I last talk to Mom"   # voice.md for the microphone
```

`ask` answers from the database deterministically, every fact cited
([ask.md](ask.md)); for an answer in prose, ask in Claude Code with `/ask`.
The dashboard serves a snapshot taken at start, never the live database;
rerun it to refresh.

**Obsidian instead of the web UI.** Obsidian on the iPhone opens vaults kept
in iCloud Drive (or on the phone itself), not in OneDrive, so put the vault
where iCloud for Windows syncs Obsidian's iCloud folder **(verify)**:

```toml
[gui]
main = "obsidian"
[obsidian]
vault = '~\iCloudDrive\iCloud~md~obsidian\LifeCRM'
```

Then run `lifecrm open` (or `lifecrm obsidian open`). The first time, choose
*Open folder as vault* in Obsidian's vault manager. After that, every write
(`/log`'s apply, an import) refreshes the vault by itself, and the iPhone's
Obsidian shows what the laptop last wrote. It is a one-way mirror: nothing
written in the vault comes back ([obsidian.md](obsidian.md#obsidian-as-your-main-gui)).
Questions stay `lifecrm ask` or the web UI.

## 9. From the iPhone, over Wi-Fi

Both servers listen only on the laptop itself unless you say otherwise:

```
lifecrm serve --host 0.0.0.0 --token random   # the phone-friendly page, port 8765 (web.md)
lifecrm dashboard --host 0.0.0.0              # the charts, port 8001
```

1. **Windows Firewall.** The first time, Windows asks whether Python may
   accept connections. Allow it on **Private networks only**.
2. **Your Wi-Fi must be Private.** Settings › Network & internet › Wi-Fi ›
   your network › *Network profile type* › **Private**.
3. **Find the laptop's address:** `ipconfig` › the Wi-Fi adapter's *IPv4
   Address*, e.g. `192.168.1.23`. On the iPhone (same Wi-Fi), open
   `http://192.168.1.23:8765/` in Safari (`serve` prints the address with its
   token when it starts).
4. Safari › Share › **Add to Home Screen** makes it one tap next time.

A missed or refused firewall prompt, and the rules to fix it, are in
[windows-android.md](windows-android.md#9-from-the-phone-over-wi-fi) — the
laptop side is the same whatever the phone. The dashboard has **no login**,
and the web page has only the `--token` key: use them on your own Wi-Fi
only, never on a Public profile, never port-forwarded.

## 10. The demo — no phone needed

```
powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Combo windows-iphone   # builds demo\windows-iphone\, opens the web UI on :8765
lifecrm demo --combo windows-iphone                                               # or just build it
```

builds a separate, throwaway project root holding six weeks of a fictional
life (Alex Rivera in Austin: a best friend Jake who loves Veracruz, a Lake
Bled trip four years ago, a board deck due), rendered as this setup's
exports: the iPhone Shortcuts' notes in `OneDrive\VoiceJournal`, an
unencrypted Apple Devices backup (`sms.db` and the phone's address book), an
Apple Health `export.zip`, an iCloud.com vCard and `Home.ics`, an iCloud for
Windows photos folder (JPEGs with the iPhone's EXIF), Edge history, a
WhatsApp export in the iPhone's format, Apple Card CSVs and a credit union
`.qfx`, AI chat exports, a Gmail Takeout, and the newer sources. It has no
calls, no Apple Notes and no location history, for the reasons in section 7,
and says so in its `skipped` lines. It imports everything through the same
commands as above, never touches your own data, and ends with `doctor` at 0
failures and every sample question answered with citations. Point any
command at it with `--root`:

```
lifecrm --root "<demo root>" status
lifecrm --root "<demo root>" ask "where is Jake from"
lifecrm --root "<demo root>" serve --host 0.0.0.0 --token random
```

[demo.md](demo.md) has the rest; `lifecrm demo --help` has the options.

## 11. Backups

`backup` (scheduled daily) keeps tiered snapshots in `backups\` and a copy in
`OneDrive\VoiceJournal\backups`; `verify-restore` (weekly, `lifecrm
verify-backup`) proves the newest one restores. Restoring, and moving to a
new laptop: [restore.md](restore.md) and [new-machine.md](new-machine.md).
The iPhone backup is Apple Devices' own and is not part of these; it is an
input, read and never written.

## Troubleshooting

| symptom | look at |
|---|---|
| Nothing arrives from the iPhone | the OneDrive app signed in and allowed *Background App Refresh*; OneDrive running and not paused on the laptop; `lifecrm doctor` (`configured_dirs`, `inbox_stuck`) |
| Every Shortcut run asks where to save | the OneDrive caveat (section 4): keep *Ask Where to Save* on, or save to iCloud Drive instead |
| A note is on the wrong day | `lifecrm filedate "<file>"`; the Shortcut's "subtract 5 hours" must match `day_rollover_hour`; `transcribe --file … --date …` |
| `observe ios_messages`: *no iPhone backup found* | make one in Apple Devices first, or pass the `<device id>` folder that holds `Manifest.db` |
| `observe ios_messages`: *encrypted backup* | Apple Devices › untick **Encrypt local backup** (it asks for the password), back up again |
| `observe ios_messages`: *N iPhone backups found* | pass the one you mean; the list names each device and date |
| `observe ios_calls` refuses the backup | expected: an unencrypted backup has no call history (section 7) |
| Photos: many *not downloaded* | iCloud for Windows keeps photos online until opened; let it download them (or tick its download option) and re-run |
| Task Scheduler shows `0x1` | `data\transcribe.log` — the failing file is named; it stays in the inbox |
| The iPhone cannot open the web page | Wi-Fi profile is Private; the Allow rule exists and no inbound Block rule for python.exe does (windows-android.md, section 9); same network; the laptop's IP changed (`ipconfig`) |
| Transcription is slow | `[whisper] model = "small"` |

## Verification checklist (once, on the real devices)

- [ ] `setup.ps1` completes; `lifecrm doctor` reports 0 failures on the
      empty database.
- [ ] `lifecrm where` shows the four OneDrive folders.
- [ ] Quick Note's `.txt` reaches `VoiceJournal\inbox-text`; note whether
      Save File pinned the OneDrive folder or showed the picker, and the sync
      latency.
- [ ] `lifecrm filedate` on it shows the journal day and time you spoke;
      `lifecrm intake` imports it; Log My Day's `.m4a` transcribes (note the
      whisper time for a 3-minute note); `/log` drafts, you review, it
      applies.
- [ ] After `install-tasks.ps1`: an unattended 07:00 run on battery leaves a
      `data\transcribe.log` entry and a snapshot appears after 21:30.
- [ ] The Apple Devices backup lands in the folder section 7 names;
      `observe ios_messages` finds it with no path, names the device, and its
      span and normalized numbers look right.
- [ ] `observe ios_calls` on that backup: refused (expected) or read — note
      which on calls.md's checklist.
- [ ] The iCloud.com vCard export and the Public Calendar download work as
      described; note the menu names and the vCard's file name.
- [ ] iCloud for Windows' photos folder path, and whether it downloads HEIC
      or JPEG by default.
- [ ] Each data source's dry run shows sensible counts and samples before
      `--apply`.
- [ ] The iPhone opens the web page over Wi-Fi with the profile set to
      Private, and cannot once it is set to Public.
- [ ] The iphone.md checklist, on the phone.

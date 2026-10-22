# MacBook + iPhone — the complete guide

Everything to run Life CRM on a Mac with an iPhone as the phone, from an
empty Mac to a scheduled, self-checking setup with your texts, contacts,
photos, notes, calendar and health data alongside the voice journal. The
Windows laptop + Android combination is covered by
[windows-android.md](windows-android.md) and [android.md](android.md); the
database, the commands and the `/log` review loop are identical on both.

What changes on a Mac, in one paragraph: the voice journal lives in **iCloud
Drive** instead of OneDrive (the iPhone's Shortcuts can save there without a
file picker), the scripts are **bash** instead of PowerShell, the scheduler
is **launchd** instead of Task Scheduler, and macOS's privacy protection
(**Full Disk Access**) has to be granted before anything can read Messages,
Contacts, Photos, Notes or an iPhone backup.

Time: ~30 minutes at the Mac (most of it downloads), ~10 on the phone.

## 0. What you need

- A Mac. **Apple Silicon (M1 or later) is the tested target**; an Intel Mac
  probably works but see [the Intel caveat](#intel-macs).
- **Homebrew** (<https://brew.sh>) for Python 3.12:
  ```
  brew install python@3.12
  ```
  macOS's own `/usr/bin/python3` is 3.9 and cannot run this project (no
  `tomllib`). Python 3.12 exactly: faster-whisper's `ctranslate2` lags new
  Python releases.
- **git**: `xcode-select --install` installs it if `git --version` asks.
- **iCloud Drive** turned on, on the Mac and the iPhone, with the same Apple
  Account (Mac: System Settings › your name › iCloud › iCloud Drive).
- **Claude Code** with a Pro or Max login (<https://claude.com/claude-code>)
  for `/log` extraction.

## 1. Clone it somewhere iCloud does not sync

```
cd ~
git clone https://github.com/<owner>/lifecrm.git LifeCRM
cd ~/LifeCRM
```

**Not** under `~/Documents` or `~/Desktop`. With iCloud's *Desktop &
Documents Folders* option on (it often is), everything there is synced —
including `data/lifecrm.db` and its live write-ahead log, which iCloud can
upload mid-write or evict to make space. `setup.sh` warns if it finds itself
there. `~/LifeCRM` is safe.

## 2. Run setup

```
/bin/bash scripts/setup.sh
```

It finds Python 3.12 (`python3.12` on your PATH, then Homebrew's
`/opt/homebrew/bin` and `/usr/local/bin`, then `uv python find 3.12`),
creates `.venv`, installs the dependencies, writes `config.toml` with
`lifecrm configure` (below), and creates an empty database. Scripts are
always run as `/bin/bash scripts/<name>.sh`, so they need no execute bit.

### What `configure` chose

`lifecrm configure` proposes this Mac's own answers and prints each one:

```
wrote /Users/alex/LifeCRM/config.toml
  journal folder     /Users/alex/Library/Mobile Documents/com~apple~CloudDocs/VoiceJournal  (iCloud Drive — ...)
  inbox_dir          .../VoiceJournal/inbox
  inbox_text_dir     .../VoiceJournal/inbox-text
  archive_dir        .../VoiceJournal/archive
  cloud_backups_dir  .../VoiceJournal/backups
  timezone           America/Chicago  (from /etc/localtime)
created in .../VoiceJournal: inbox, inbox-text, archive, backups
```

`~/Library/Mobile Documents/com~apple~CloudDocs` is the folder Finder calls
**iCloud Drive**, so the journal shows up there as `VoiceJournal`, and on the
iPhone in Files › iCloud Drive › VoiceJournal. The timezone comes from the
Mac's own setting. Everything else in `config.toml` is
`config.example.toml`'s defaults, comments included — read the `[whisper]`
section once.

Options when the proposal is wrong: `--journal-root DIR` (another folder),
`--timezone Area/City`, `--no-mkdir`, and `--force` to replace an existing
`config.toml` (the old one is kept under `data/`). It never creates iCloud
Drive itself: if iCloud Drive is off, it writes the config, creates nothing,
and tells you to turn iCloud Drive on and re-run with `--force`.
(`cloud_backups_dir` takes any cloud folder; configs that still say
`onedrive_backups_dir`, its old name, keep working.)

### Keep the journal downloaded

With **Optimize Mac Storage** on, iCloud may evict files to save space. In
Finder, open iCloud Drive, right-click **VoiceJournal** › **Keep Downloaded**
(macOS 15 Sequoia and later; on older macOS, turn Optimize Mac Storage off in
System Settings › your name › iCloud › iCloud Drive). Intake copes either
way, but a downloaded folder is faster and never waits:

- newer macOS keeps an evicted file's name and marks it *dataless*; reading it
  downloads it, which intake does (with retries, like OneDrive placeholders);
- older macOS replaces it with a hidden stub `.<name>.icloud`. Intake never
  reads a stub: it asks iCloud for the file (`brctl download`), logs
  `waiting: <name> is still in iCloud`, and picks the file up on the next run.
  `lifecrm doctor` warns about stubs: *N note(s) still in iCloud — open the
  VoiceJournal folder in Finder or set it to Keep Downloaded*.

AppleDouble `._<name>` files (left by exFAT drives, network shares and
unzipped archives) and Finder's `Icon` file are ignored.

### See what this Mac has

```
.venv/bin/python -m lifecrm where
```

A read-only report: the journal folders (and how many notes are waiting),
iPhone backups, the Messages database, Contacts stores, the Photos library,
the Notes store, and any Health, Takeout, WhatsApp or calendar export sitting
in `~/Downloads` or iCloud Drive — each with whether it exists, whether macOS
lets you read it (`no access` = [Full Disk Access](#5-full-disk-access)), and
the exact command that imports it. `--json` gives the same for scripts.

## 3. The iPhone

Follow **[iphone.md](iphone.md)** — two Shortcuts, "Log My Day" (audio) and
"Quick Note" (dictated text). With a Mac, save to **iCloud Drive**:

```
iCloud Drive/VoiceJournal/inbox         <- audio notes (.m4a)
iCloud Drive/VoiceJournal/inbox-text    <- dictated text notes (.txt)
```

In each Shortcut's **Save File** action, turn **Ask Where to Save** off and
pick that folder. The OneDrive caveat in iphone.md (third-party providers
forcing the file picker) does not apply: Save File can always pin an iCloud
Drive destination. Nothing to install on the phone — iCloud Drive and
Shortcuts are built in.

**If your iOS only offers a path inside the Shortcuts folder** (older
versions take a subpath under iCloud Drive › Shortcuts rather than any
folder): save to `Shortcuts/VoiceJournal/inbox` and `.../inbox-text`, and on
the Mac point the config there — that folder lives in the Shortcuts app's own
iCloud container:

```
.venv/bin/python -m lifecrm configure --force --journal-root \
    "~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/VoiceJournal"
```

Which of the two your iPhone offers is one of the things to report back from
the checklist at the end of iphone.md.

## 4. First run, end to end

1. On the iPhone, run **Quick Note** and dictate a sentence. In Finder,
   confirm the `.txt` appears in iCloud Drive › VoiceJournal › inbox-text
   (note how long it took).
2. On the Mac, from `~/LifeCRM`:
   ```
   .venv/bin/python -m lifecrm intake
   .venv/bin/python -m lifecrm status
   ```
3. Open Claude Code in `~/LifeCRM`, type `/log`, review the diff, confirm.
4. `.venv/bin/python -m lifecrm doctor` — expect `0 failure(s)`.
5. Record a real **Log My Day** note and repeat; note the whisper wall-clock
   time for it (see [Transcription](#8-transcription-on-a-mac)).

## 5. Full Disk Access

macOS's privacy protection (TCC) blocks every program from your Messages,
Contacts, Photos and Notes databases and from iPhone backups until you allow
it. Python inherits the permission of the app that launched it — so grant it
to **your terminal**:

System Settings › Privacy & Security › **Full Disk Access** › **+** › choose
Terminal (Applications › Utilities) — or iTerm, or the app Claude Code runs
in, whichever you use — then quit and reopen that app.

What needs it:

| reads | without Full Disk Access |
|---|---|
| `~/Library/Messages/chat.db` (Messages) | `no access` in `where`; the connector fails with *Operation not permitted* or *unable to open database file* |
| `~/Library/Application Support/MobileSync/Backup` (iPhone backups) | same |
| `~/Library/Application Support/AddressBook` (Contacts) | same |
| `~/Pictures/Photos Library.photoslibrary` (Photos) | same |
| `~/Library/Group Containers/group.com.apple.notes` (Notes) | same |
| iCloud Drive, `~/Downloads` | a one-time "would like to access" prompt instead — allow it |

**Scheduled jobs (launchd) cannot show that prompt.** If a scheduled run's
log (`data/transcribe.log`, `data/launchd-intake.log`) says
`Operation not permitted` while the same command works in Terminal, the job
itself needs the permission: add `/bin/bash` to Full Disk Access (in the
file picker press ⌘⇧G and type `/bin/bash`), and the venv's real Python
binary, which this prints:

```
.venv/bin/python -c "import os, sys; print(os.path.realpath(sys.executable))"
```

(A Homebrew upgrade of Python changes that path; re-add it after one.)
Granting `/bin/bash` Full Disk Access applies to every bash script run by
launchd — the price of unattended access to iCloud Drive; if you would rather
not, skip the scheduled intake and run `intake` by hand. Whether launchd jobs
need this for iCloud Drive at all is **unverified** — please report what your
Mac does.

## 6. Scheduled jobs (launchd)

```
/bin/bash scripts/install-tasks.sh
```

Writes five LaunchAgents to `~/Library/LaunchAgents/` and loads them (as you,
no sudo):

| job | when | runs |
|---|---|---|
| `local.lifecrm.intake` | daily 07:00 **and at login** | `scripts/transcribe-inbox.sh` — both inboxes; transcripts ready before you sit down |
| `local.lifecrm.doctor` | daily 08:00 | `scripts/doctor.sh` — invariants on the live database |
| `local.lifecrm.backup` | daily 21:30 | `scripts/backup.sh` — snapshot, local + iCloud Drive, tiered retention |
| `local.lifecrm.verify-restore` | Sunday 12:00 | `scripts/verify-restore.sh` — `lifecrm verify-backup`: restores the newest snapshot into a scratch folder and checks it |
| `local.lifecrm.review` | Sunday 19:00 | `scripts/review.sh` — the review pack for the last full week, waiting in `data/staging/` for `/review-week` |

When a run fails, it says so — exactly as on Windows:

- `data/transcribe.log`, `data/doctor.log` and `data/review.log` carry every
  line, prefixed with the run's start time, ending in
  `FAILED (exit N) - see the lines above`.
  A recording intake chokes on is named there and stays in the inbox.
- `launchctl print gui/$(id -u)/local.lifecrm.intake` shows
  **last exit code** — the Mac's version of Task Scheduler's *Last Run
  Result*; `1` means look at the log.
- Anything that never reached Python (a missing venv) lands in
  `data/launchd-<job>.log`.

Run a job now: `launchctl kickstart gui/$(id -u)/local.lifecrm.backup`.
`--print` shows the plists without installing; re-running the script
replaces the jobs; `/bin/bash scripts/uninstall-tasks.sh` removes them.

launchd differs from Task Scheduler in two ways worth knowing: a job missed
while the Mac **slept** runs when it wakes, but one missed while it was
**shut down** does not (the intake job's at-login run covers the morning);
and there is no two-hour run limit.

## 7. Bringing in the rest of your data

Every import below is a **dry run by default**: it prints counts, date spans
and a sample record and writes nothing. Read the sample, then re-run with
`--apply`. Re-running is always safe — records dedupe on the source's own
ids. `where` lists which of these this Mac has; run them from `~/LifeCRM`
with `.venv/bin/python -m lifecrm ...`.

| source | where it lives | command |
|---|---|---|
| Messages (iMessage, SMS, RCS) | the Mac's own `~/Library/Messages/chat.db` (with *Messages in iCloud* on, the full history) | `observe mac_messages` |
| Texts from the phone (instead of `chat.db`) | an unencrypted **Finder** backup: Finder › your iPhone › General › *Back up all of the data on your iPhone to this Mac*, untick *Encrypt local backup*, **Back Up Now** | `observe ios_messages` — see [texts.md](texts.md) (an unencrypted backup is expected to lack call history) |
| Contacts | the Mac's Contacts stores (iCloud contacts included) | `observe contacts` |
| Photos (metadata only — time, place, camera; never the pixels) | `~/Pictures/Photos Library.photoslibrary` | `observe photos` |
| Apple Notes | `~/Library/Group Containers/group.com.apple.notes` | `observe notes` |
| Calendar | Calendar app › select a calendar › File › Export › Export… → an `.ics` | `observe calendar ~/Downloads/<name>.ics` |
| Apple Health | iPhone Health app › your picture › **Export All Health Data** › AirDrop to the Mac (→ `~/Downloads/export.zip`) or Save to Files › iCloud Drive | `health import` (finds the newest export) or `health import ~/Downloads/export.zip` — see [apple-health.md](apple-health.md) |
| Calls | the Mac's call history, with FaceTime › Settings › **Calls from iPhone** on | `observe ios_calls` — see [calls.md](calls.md) |
| Safari history | `~/Library/Safari/History.db` (the iPhone's Safari syncs into it) | `observe browser`, with `[connectors.browser] browser = "safari"` if Chrome is also installed — see [browser.md](browser.md) |
| WhatsApp | on the iPhone: chat name › **Export Chat** › AirDrop (→ `~/Downloads`) | `observe whatsapp` — set `[connectors.whatsapp] me` first; see [whatsapp.md](whatsapp.md) |
| Bank and card statements | Apple Card's monthly CSV, a bank's CSV or OFX/QFX | `observe finance <file or folder>` — see [finance.md](finance.md) |
| Location | only Google Maps Timeline on the iPhone; Apple's Significant Locations cannot be exported | `observe location <file>` — see [location.md](location.md) |
| AI chats (Claude, ChatGPT) | the vendor's export zip | `observe ai_chat <zip>` — see [ai-chat.md](ai-chat.md) |

Use **one** route for texts per account: the Mac's `chat.db` and an iPhone
backup are separate connectors, and each mirrors its own source — alternating
them for the same phone number makes each look like it deleted what only the
other holds.

Then name the people: `handles list --suggest` matches phone numbers and
addresses against your contacts' names and prints a `handles link` command
for each candidate. Nothing is ever linked automatically.

## 8. Transcription on a Mac

faster-whisper runs on Apple Silicon out of the box: `pip install -r
requirements.txt` gets native arm64 wheels for `ctranslate2` and PyAV (which
bundles FFmpeg — no `brew install ffmpeg`). It runs on the **CPU**; there is
no Metal/GPU backend, so keep `device = "cpu"` and `compute_type = "int8"`
(int8 is the fast path on ARM). The first transcription downloads the model
into `~/.cache/huggingface` — about 1.5 GB for `distil-large-v3`, ~500 MB
for `small`. If a long note takes too long for you, drop to `small` in
`config.toml`; report the wall-clock time you see either way.

<a id="intel-macs"></a>**Intel Macs:** unverified. If `pip` cannot find a
`ctranslate2` wheel for your Mac, transcription will not install; dictated
text notes (Quick Note) still work, since they never load whisper.

## 9. The demo

To see a fully populated system before your own data exists:

```
/bin/bash scripts/demo.sh            # builds demo/iphone/ and opens the web UI on :8765
.venv/bin/python -m lifecrm demo --combo iphone     # or just build it
```

builds a separate demo project from the built-in persona (Alex Rivera in
Austin — journal, texts, calls, contacts, photos, notes, calendar, Safari,
WhatsApp, Apple Card, AI chats, Apple Health) in each source's real export
format, imported through the same connectors your data uses. It prints the
demo root and the commands to try against it; [demo.md](demo.md) has the
rest.

## 10. Day to day

The daily ritual is the README's: Quick Note during the day, Log My Day in
the evening, `/log` at the Mac. Commands are the same on both systems; on a
Mac the prefix is `.venv/bin/python -m lifecrm` (Windows:
`.venv\Scripts\python.exe -m lifecrm`), run from the repo folder.

Updates: `git pull`; if it brings a new file under `lifecrm/migrations/`, run
`.venv/bin/python -m lifecrm migrate` (it snapshots first). Restoring onto
another machine: [restore.md](restore.md) — `/bin/bash scripts/restore.sh
<snapshot>` is the Mac form.

## Troubleshooting

- **`Operation not permitted`** in a log or a traceback-free error: [Full
  Disk Access](#5-full-disk-access).
- **A note never arrives:** `lifecrm where` shows the inbox counts;
  `lifecrm doctor` names stubs still in iCloud and files stuck over two days;
  `data/transcribe.log` names any file intake failed on.
- **`launchctl bootstrap` fails with `Input/output error`:** the old job was
  still unloading; run `install-tasks.sh` again.
- **`Python 3.12 not found`:** `brew install python@3.12`, then re-run
  `setup.sh`.
- **The database is under iCloud's control** (setup warned about
  `~/Documents`): move the folder to `~/LifeCRM` and re-run `setup.sh`; your
  data moves with it.

## What has not been verified on a real Mac

The scripts, plists and the iCloud handling have tests (they run under Git
Bash on Windows today, and `.github/workflows/ci.yml` runs the whole suite on
a macOS runner once the repo is pushed to GitHub), but these need a person
and a Mac:

1. Whether **Save File** lets you pin an arbitrary iCloud Drive folder or
   only a path under Shortcuts (section 3).
2. Whether **launchd jobs need Full Disk Access** to read iCloud Drive
   (section 5), and which binary macOS attributes the access to.
3. iCloud **sync latency** from the iPhone, and whether `brctl download`
   brings a stub down without Finder open.
4. **Whisper wall-clock** for a ~3 minute note on your chip.
5. **Keep Downloaded**'s exact menu name and macOS version.
6. Intel Mac wheel availability.

Report what you find — it replaces the guesses here.

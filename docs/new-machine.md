# Setting up Life CRM on a new machine

This walks you from nothing to your first applied day. It assumes you are
*not* the original author: fresh computer, fresh database — your journal,
your machine, your data. Nothing here touches anyone else's copy.

Two combinations are supported end to end, and the steps differ only where
the operating system does:

| | Windows laptop + Android (Samsung) | MacBook + iPhone |
|---|---|---|
| phone guide | [android.md](android.md) | [iphone.md](iphone.md) |
| journal folder | OneDrive › VoiceJournal | iCloud Drive › VoiceJournal |
| scripts | PowerShell (`scripts\*.ps1`) | bash (`/bin/bash scripts/*.sh`) |
| scheduler | Task Scheduler | launchd |
| run a command | `.venv\Scripts\python.exe -m lifecrm …` | `.venv/bin/python -m lifecrm …` |

A Mac has its own complete guide, [mac.md](mac.md), which also covers
importing Messages, Contacts, Photos, Notes and Apple Health; the Mac lines
below are the short version.

Time: ~20 minutes at the computer, plus ~10 on the phone.

## 0. Prerequisites

Both:

- A **GitHub account** only if you want to file issues: the repo is public,
  so cloning needs no invite.
- **Claude Code** with a Pro or Max login — install per
  <https://claude.com/claude-code>. Extraction (`/log`) runs on your
  subscription; a nightly run is small and fits comfortably in Pro limits.

Windows:

- **git** — check with `git --version`; if missing:
  `winget install Git.Git` (then open a new terminal).
- **OneDrive** signed into **your own** Microsoft account, on PC and phone.
  (Only your phone's recordings and your DB snapshots go there — see
  `docs/privacy.md` before you decide you're OK with that.)

Mac:

- **Homebrew** (<https://brew.sh>), then `brew install python@3.12`.
  (macOS's own python3 is too old.) `xcode-select --install` provides git.
- **iCloud Drive** on, on the Mac and the iPhone, same Apple Account.

## 1. Clone the repo

```
git clone https://github.com/<owner>/lifecrm.git
cd lifecrm
```

First clone pops a browser window to sign into GitHub (Git Credential
Manager on Windows); that's normal.

**Mac:** clone into your home folder (`~/LifeCRM`), **not** under
`~/Documents` or `~/Desktop` — iCloud's *Desktop & Documents* sync would sync
the live database.

## 2. Run setup

Windows:

```
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Mac:

```
/bin/bash scripts/setup.sh
```

Both find (Windows: or install) Python 3.12, create `.venv`, install
dependencies, write `config.toml` for this machine with
`lifecrm configure`, and initialize an empty database. They print what they
do; a fresh run takes a few minutes.

## 3. Check what `configure` chose

`lifecrm configure` (run by setup) proposed this machine's own answers and
printed them:

- the **journal folder** — `OneDrive\VoiceJournal` on Windows, iCloud Drive's
  `VoiceJournal` on a Mac — with its four subfolders created:
  `inbox` (audio), `inbox-text` (dictated notes), `archive`, `backups`;
- the **timezone**, read from the operating system and written as an IANA
  name (e.g. `America/Chicago`);
- everything else from `config.example.toml`.

If the cloud folder did not exist yet (OneDrive not signed in, iCloud Drive
off), it says so and creates nothing — fix that, then re-run
`python -m lifecrm configure --force`. Other folders or zone:
`--journal-root DIR`, `--timezone Area/City`. Or edit `config.toml` by hand:

- leave `day_rollover_hour = 5` (notes recorded before 05:00 file under the
  previous day);
- `model = "distil-large-v3"` — benchmarked against `small` on the original
  machine: same CPU speed, noticeably fewer name errors, and names are what
  the database is built from. Only drop to `small` if transcription feels
  too slow on your machine.

The first transcription downloads the whisper model (~1.5 GB, one time).

Keep the journal folder downloaded, so the sync client keeps real files
rather than placeholders (the code guards against placeholders anyway):

- Windows: in Explorer, right-click `VoiceJournal` → **Always keep on this
  device**.
- Mac: in Finder, right-click `VoiceJournal` → **Keep Downloaded**.

`python -m lifecrm where` lists the journal folders, how many notes are
waiting, and every other source this machine has that Life CRM can import.

## 4. Set up your phone

Follow **[docs/android.md](android.md)** or **[docs/iphone.md](iphone.md)**.
You're saving into your *own* cloud folder, so skip the "someone else's
journal" paragraph. Build both captures — the evening audio note and the
ten-second daytime quick note.

Optional but recommended, back at the computer, schedule the jobs:

Windows:

```
powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1
```

Mac:

```
/bin/bash scripts/install-tasks.sh
```

That schedules daily intake (07:00 and at logon, so transcripts are ready
before you sit down), a daily `doctor` against the live database (08:00),
daily tiered backups (21:30, local + cloud folder), a weekly
restore-verification (Sundays 12:00) and a weekly review pack for the week
just ended (Mondays 07:30), on both systems.

If a job fails it says so. On Windows, Task Scheduler's *Last Run Result*
shows `0x1`; on a Mac, `launchctl print gui/$(id -u)/local.lifecrm.intake`
shows *last exit code = 1*. Either way intake writes what went wrong —
including the file name — to `data/transcribe.log`. Run `lifecrm doctor` any
time: it flags a mistyped inbox path, a recording stuck in the inbox, a file
in a format intake can't read, and (Mac) notes still in iCloud.

A Mac also needs **Full Disk Access** for anything that reads Messages,
Contacts, Photos, Notes or an iPhone backup — see mac.md, section 5.

## 5. First run, end to end

1. On the phone: run the quick note, dictate one sentence. Confirm the
   `.txt` appears in `VoiceJournal/inbox-text`.
2. On the computer, after sync (Windows shown; on a Mac use
   `.venv/bin/python`):
   ```
   .venv\Scripts\python.exe -m lifecrm intake
   .venv\Scripts\python.exe -m lifecrm status
   ```
   Status should list one source under "sources awaiting extraction" with
   today's journal date (type `phone_transcript`); `pending text notes` reads
   0 because intake has already taken the file out of the inbox.
3. Open Claude Code in the repo folder and type `/log`. It drafts the
   extraction, you review the diff, confirm, and it applies.
4. Prove the database is intact:
   ```
   .venv\Scripts\python.exe -m lifecrm doctor
   ```
   Expect `0 failure(s)`.
5. Same evening or next: record a real evening note and repeat.

## 6. During the trial

- Run the **verification checklist** at the end of your phone guide once, on
  your real phone, and report the facts it asks for (for iPhone: whether
  Save File let you pin the folder, sync latency, whisper wall-clock).
- Keep a plain-text **friction log** next to the repo. Any time something
  takes more than two–three minutes or makes you not want to record — write
  it down in the moment. That file is the trial's most valuable output.
- Report problems and observations as **GitHub issues** on the repo
  (never paste transcript contents into an issue — describe, don't quote).

## 7. Getting updates

```
git pull
```

Your data is never in git — pulling only updates code and docs. If a pull
brings a new file under `lifecrm/migrations/`, run:

```
.venv\Scripts\python.exe -m lifecrm migrate      # Windows
.venv/bin/python -m lifecrm migrate              # Mac
```

(it snapshots the DB before touching anything). `lifecrm doctor` after a
migration is never a bad idea.

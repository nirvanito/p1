# Setting up Life CRM on a new machine

This walks you from nothing to your first applied day. It assumes you are
*not* the original author: fresh Windows PC, fresh database — your journal,
your machine, your data. Nothing here touches anyone else's copy.

Time: ~20 minutes at the computer, plus ~10 on the phone.

## 0. Prerequisites

- A **Windows PC** (the automation scripts are PowerShell).
- **git** — check with `git --version`; if missing:
  `winget install Git.Git` (then open a new terminal).
- A **GitHub account**, with the collaborator invite to the private repo
  accepted (check your email / github.com notifications).
- **Claude Code** with a Pro or Max login — install per
  <https://claude.com/claude-code>. Extraction (`/log`) runs on your
  subscription; a nightly run is small and fits comfortably in Pro limits.
- **OneDrive** signed into **your own** Microsoft account, on PC and phone.
  (Only your phone's recordings and your DB snapshots go there — see
  `docs/privacy.md` before you decide you're OK with that.)

## 1. Clone the repo

```
git clone https://github.com/<owner>/lifecrm.git
cd lifecrm
```

First clone pops a browser window to sign into GitHub (Git Credential
Manager); that's normal.

## 2. Run setup

```
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

This finds (or installs) Python 3.12, creates `.venv`, installs
dependencies, copies `config.example.toml` → `config.toml`, and initializes
an empty database. It prints what it's doing; a fresh run takes a few
minutes.

## 3. Create your OneDrive folders and edit config.toml

In OneDrive (Explorer is fine), create:

```
OneDrive\VoiceJournal\inbox
OneDrive\VoiceJournal\inbox-text
OneDrive\VoiceJournal\archive
OneDrive\VoiceJournal\backups
```

Right-click `VoiceJournal` → **Always keep on this device**.

Then open `config.toml` (in the repo root) and set:

- the four `[paths]` entries to those folders (replace `<you>` with your
  Windows username);
- `timezone` to your IANA zone (e.g. `America/Chicago` — list:
  <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>);
- leave `day_rollover_hour = 5` (notes recorded before 05:00 file under the
  previous day);
- `model = "distil-large-v3"` — benchmarked against `small` on the original
  machine: same CPU speed, noticeably fewer name errors, and names are what
  the database is built from. Only drop to `small` if transcription feels
  too slow on your machine.

The first transcription downloads the whisper model (~1.5 GB, one time).

## 4. Set up your phone

Follow **[docs/iphone.md](iphone.md)** (or `android.md`). You're saving into
your *own* OneDrive, so skip the "someone else's journal" paragraph. Build
both shortcuts — "Log My Day" (evening audio) and "Quick Note" (ten-second
daytime thoughts).

Optional but recommended, back at the PC:

```
powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1
```

That schedules daily intake (transcripts ready before you sit down), daily
tiered backups (local + OneDrive), and a weekly restore-verification.

## 5. First run, end to end

1. On the phone: run **Quick Note**, dictate one sentence. Confirm the
   `.txt` appears in `OneDrive\VoiceJournal\inbox-text`.
2. On the PC, after sync:
   ```
   .venv\Scripts\python.exe -m lifecrm intake
   .venv\Scripts\python.exe -m lifecrm status
   ```
   Status should show one pending note with today's date.
3. Open Claude Code in the repo folder and type `/log`. It drafts the
   extraction, you review the diff, confirm, and it applies.
4. Prove the database is intact:
   ```
   .venv\Scripts\python.exe -m lifecrm doctor
   ```
   Expect `0 failure(s)`.
5. Same evening or next: record a real **Log My Day** note and repeat.

## 6. During the trial

- Run the **verification checklist** at the end of iphone.md once, on your
  real phone, and report the three facts it asks for (whether Save File let
  you pin the OneDrive folder, sync latency, whisper wall-clock).
- Keep a plain-text **friction log** next to the repo. Any time something
  takes more than two–three minutes or makes you not want to record — write
  it down in the moment. That file is the trial's most valuable output.
- Report problems and observations as **GitHub issues** on the private repo
  (never paste transcript contents into an issue — describe, don't quote).

## 7. Getting updates

```
git pull
```

Your data is never in git — pulling only updates code and docs. If a pull
brings a new file under `lifecrm\migrations\`, run:

```
.venv\Scripts\python.exe -m lifecrm migrate
```

(it snapshots the DB before touching anything). `lifecrm doctor` after a
migration is never a bad idea.

# Life CRM

A personal life database. Every evening you record one voice note about your
day; the system transcribes it locally, Claude Code (via the `/log` skill)
extracts it into structured records — meals, workouts, work, people, places,
mood, ideas — you review a diff, confirm with one keystroke, and it lands in a
SQLite database with full provenance back to the exact transcript passage.

Zero recurring cost: whisper runs on this machine, extraction runs on your
existing Claude Code subscription, everything else is local code.

```
Android voice note --OneDrive--> inbox --faster-whisper--> transcript
    --/log (Claude Code, reviewed)--> draft JSON --ingest--> diff
    --your confirmation--> apply --> SQLite (+ audit, revisions, provenance)
```

The database outlives every tool on top of it: entries carry stable UUIDs,
every record traces to its source, corrections are revisions (never
overwrites), and plain SQL reads everything.

## Daily ritual

1. On the way home (or whenever): open your recorder app, talk through your
   day, stop. The file uploads to OneDrive by itself.
2. At the computer: open Claude Code in this folder and type `/log`.
3. Claude transcribes, extracts, and shows you a review: entry table, new
   entities, questions about anything ambiguous.
4. Say `y` (or give corrections). Done — usually under a minute.

No audio? Paste text into `/log` instead — dictated text from your phone
keyboard works exactly as well and skips transcription entirely.

## One-time setup

### This computer

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
```

Installs Python 3.12 (via the Python Install Manager), creates the venv,
installs dependencies, and initializes the database. Then check
`config.toml` — inbox paths, timezone, whisper model.

Pin the audio folder so OneDrive keeps real files, not placeholders:
right-click `OneDrive\VoiceJournal` in Explorer -> **Always keep on this
device**. (The code guards against placeholders anyway.)

Optional — transcribe automatically so the text is ready before you sit down:
Task Scheduler -> Create Basic Task -> "LifeCRM transcribe" -> Daily 7:00 AM
(add a second trigger "At log on") -> Start a program:

```
powershell -ExecutionPolicy Bypass -File "C:\Users\you\LifeCRM\scripts\transcribe-inbox.ps1"
```

### Your phone (Android)

1. Install a recorder app that can save to a chosen folder with a date
   filename — e.g. **Easy Voice Recorder**: set format m4a, filename template
   including the date (`2026-07-27 21.30.m4a`), save location a folder named
   `VoiceJournal`. Put its record widget on your home screen.
2. Install **Autosync for OneDrive (OneSync)**, free tier: one folder pair,
   phone `VoiceJournal` -> OneDrive `VoiceJournal/inbox`, sync method
   **Upload only**, instant upload on Wi-Fi.
3. Fallback that always works: record with anything, then Share -> OneDrive ->
   `VoiceJournal/inbox`.

Filenames without a date still work — the file's timestamp decides, and
recordings before 05:00 count as the previous day (configurable).

## Commands you'll actually use

```bash
.venv/Scripts/python -m lifecrm status                 # what's pending, streak
.venv/Scripts/python -m lifecrm transcribe             # process inbox audio now
.venv/Scripts/python -m lifecrm import note.txt --date 2026-07-27
.venv/Scripts/python -m lifecrm sql "SELECT ..." --json  # read-only queries
.venv/Scripts/python -m lifecrm backup                 # snapshot local + OneDrive
```

Fixing things (all reversible, all audited):

```bash
.venv/Scripts/python -m lifecrm entry show <uuid>      # incl. transcript passage
.venv/Scripts/python -m lifecrm entry edit <uuid> --title "..." --primary-category food
.venv/Scripts/python -m lifecrm entry retract <uuid>   # and: entry restore
.venv/Scripts/python -m lifecrm entry merge <src-uuid> <dst-uuid>
.venv/Scripts/python -m lifecrm entities match "Jake" # resolve names
.venv/Scripts/python -m lifecrm entities merge <src-id> <dst-id>
.venv/Scripts/python -m lifecrm entities alias-add <id> "Jaik"  # fix a mishearing forever
```

`lifecrm --help` shows everything else (categories/kinds management, migrate,
restore, admin).

## How it's built

- **SQLite is canonical** (`data\lifecrm.db`, WAL). Sources, transcripts, and
  segments are immutable; entries/measurements/entities are corrected via
  revisions, retraction, and merges — never destructive rewrites. Full-text
  search over entries and transcripts via FTS5.
- **Contract boundary** (`docs/contract.md`): transcript -> versioned JSON ->
  deterministic validation -> reviewable diff -> transactional apply. Claude
  is just the current extractor; the database never depends on it.
- **Reconciliation, not replacement**: re-processing a day produces a diff
  (add / update / possible duplicate / retract proposal / unchanged);
  user-confirmed records are never silently overwritten.
- **Provenance**: every entry links to its source, extraction run, and the
  character range (or audio segment) of the transcript that supports it.
- **Privacy**: personal data never enters git; OneDrive gets audio + DB
  snapshots (that's cloud storage — read `docs/privacy.md`).

Tests: `.venv/Scripts/python -m pytest tests -q` (104 tests).

## Roadmap (deliberately not built yet)

After a few weeks of real notes exist — and the schema has survived contact
with them — the next layers go on top: a local web dashboard (timeline, entity
pages, charts, review queue), an Obsidian-compatible markdown mirror with the
knowledge graph, JSONL snapshot exports, typed entity relationships
(Jake works_at Acme), and `/recall` + `/review-week` skills. The
`recall_log` table and `transcript_segments` are already in place as the
extension points.

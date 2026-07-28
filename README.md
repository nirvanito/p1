# Life CRM

A personal life database. You talk — one long note in the evening, quick
dictated thoughts during the day — and the system turns it into structured,
confirmed records: meals, workouts, work, people, places, mood, tasks, ideas,
with every record traceable to the exact sentence that produced it.

Zero recurring cost: whisper runs on this machine, extraction runs on your
existing Claude Code subscription (via the `/log` skill), everything else is
local code. This codebase is the *foundation* of the Life CRM — capture,
schema, reconciliation, provenance, export, and self-verification. The layers
on top (dashboard, recall, weekly reviews) come after real data exists.

```
iPhone / Android
  voice note  --OneDrive-->  VoiceJournal/inbox        (audio)
  quick note  --OneDrive-->  VoiceJournal/inbox-text   (dictated text)
        |
        v  lifecrm intake        (local faster-whisper for audio;
        |                         text imports directly - no model load)
        v  /log (Claude Code)    draft JSON per docs/contract.md
        v  lifecrm ingest        validate + reconcile -> reviewable diff
        v  your confirmation     one keystroke, or corrections
        v  lifecrm apply         transactional; audit + revisions + provenance
        v
   SQLite (data\lifecrm.db)  ->  lifecrm export (jsonl / markdown archives)
```

The database outlives every tool on top of it: stable UUIDs, immutable
sources, corrections as revisions (never overwrites), full-text search,
deterministic plain-text exports, and `lifecrm doctor` to prove it's intact.

## Daily ritual

1. During the day: any passing thought — run the **Quick Note** shortcut,
   dictate ten seconds, done.
2. Evening: run **Log My Day**, talk through the day, stop.
3. At the computer: open Claude Code here, type `/log`. All of today's notes
   are extracted together as one coherent day; you review a diff (new
   entries, closed tasks, links to earlier threads, anything ambiguous) and
   confirm.

No audio handy? Paste text straight into `/log`.

## One-time setup

### This computer

```bash
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
```

Then check `config.toml` (inbox paths, timezone, whisper model — the setup
benchmark picked `distil-large-v3`), and register the scheduled tasks so
nothing depends on remembering:

```bash
powershell -ExecutionPolicy Bypass -File scripts/install-tasks.ps1
```

That schedules: **intake** (daily 7:00 + at logon — transcripts are ready
before you sit down), **backup** (daily 21:30, tiered retention: 14 daily /
12 weekly / 24 monthly / yearly forever, local + OneDrive), and
**verify-restore** (weekly — restores the newest snapshot into a scratch
folder and runs `doctor` on it, because an untested backup is not a backup).

In Explorer, right-click `OneDrive\VoiceJournal` → **Always keep on this
device** so OneDrive keeps real files, not placeholders (the code guards
against placeholders anyway).

### Your phone

- **iPhone** — [docs/iphone.md](docs/iphone.md): two Apple Shortcuts ("Log My
  Day" for audio, "Quick Note" for dictated text), action by action, with
  triggers (Action Button, Back Tap, Siri) and an on-phone verification
  checklist. No extra apps beyond OneDrive.
- **Android** — [docs/android.md](docs/android.md): recorder app + Autosync
  for OneDrive, filename conventions, and the honest caveats.

Both guides are self-contained and shareable on their own.

## Commands you'll actually use

```bash
.venv/Scripts/python -m lifecrm status        # pending notes, open loops, streak
.venv/Scripts/python -m lifecrm intake        # process both inboxes now
.venv/Scripts/python -m lifecrm import note.txt --date 2026-07-27
.venv/Scripts/python -m lifecrm sql "SELECT ..." --json   # read-only queries
.venv/Scripts/python -m lifecrm doctor        # prove the database is intact
.venv/Scripts/python -m lifecrm backup        # snapshot local + OneDrive
.venv/Scripts/python -m lifecrm export --format markdown --out archive/
.venv/Scripts/python -m lifecrm export --format jsonl --out archive-data/
```

Loops and threads:

```bash
.venv/Scripts/python -m lifecrm entry close <uuid> --closure done   # or dropped/obsolete
.venv/Scripts/python -m lifecrm entry reopen <uuid>
.venv/Scripts/python -m lifecrm link add <from-uuid> <to-uuid> --type resolves
.venv/Scripts/python -m lifecrm link list <uuid>
```

Fixing things (all reversible, all audited):

```bash
.venv/Scripts/python -m lifecrm entry show <uuid>     # incl. transcript passage
.venv/Scripts/python -m lifecrm entry edit <uuid> --title "..." --primary-category food
.venv/Scripts/python -m lifecrm entry retract <uuid>  # and: entry restore
.venv/Scripts/python -m lifecrm entry merge <src-uuid> <dst-uuid>
.venv/Scripts/python -m lifecrm entities match "Jake"
.venv/Scripts/python -m lifecrm entities merge <src-id> <dst-id>
.venv/Scripts/python -m lifecrm entities alias-add <id> "Jaik"   # fix a mishearing forever
```

`lifecrm --help` shows the rest (categories/kinds management, migrate,
restore, admin).

## How it's built

- **SQLite is canonical** (`data\lifecrm.db`, WAL, schema v2 with a
  transactional migration runner in `lifecrm/migrations/`). Sources,
  transcripts, and segments are immutable; entries carry stable UUIDs and are
  corrected via revisions, retraction, closure, and merges — never
  destructive rewrites.
- **Contract boundary** (`docs/contract.md`, v2): transcripts → versioned
  JSON → deterministic validation → reviewable diff → transactional apply. A
  day made of several notes extracts as ONE draft; every entry names which
  source and which passage supports it. Claude is just the current extractor.
- **Reconciliation, not replacement**: re-processing produces a diff (add /
  update / possible duplicate / retract proposal / cross-day closure /
  unchanged); user-confirmed records are never silently overwritten, and
  closures of earlier days' tasks always require an explicit decision.
- **Loops close, threads connect**: `task`/`plan` entries carry
  `closed_at`/`closure` (done / dropped / obsolete — distinct from
  retraction, which means "this record was wrong"), and `entry_links`
  (continues / resolves / part_of / caused / contradicts / relates) capture
  the thread structure that is only knowable while the memory is fresh.
- **The archive outlives the code**: deterministic JSONL and markdown
  exports; `docs/restore.md` explains reading them with no tooling at all.
- **Privacy**: personal data never enters git (test-enforced); OneDrive gets
  audio + DB snapshots — that's cloud storage, read `docs/privacy.md`.

Tests: `.venv/Scripts/python -m pytest tests -q` (169 tests).

## Roadmap (deliberately not built yet)

This work completes the **foundation**, not the application. After real
notes accumulate — and the schema has survived contact with them — the next
layers go on top: a local dashboard (timeline, entity pages, review queue),
a live Obsidian mirror, typed entity↔entity relationships (Jake works_at
Acme), and `/recall` + `/review-week` skills. `recall_log`,
`transcript_segments`, and stable entry UUIDs are already in place as their
extension points.

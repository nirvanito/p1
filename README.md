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

Setting this up on a fresh machine that isn't the original author's (your
own copy, your own data)? Follow [docs/new-machine.md](docs/new-machine.md)
— it covers clone through first applied `/log`.

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

When a run fails, it says so: each task exits non-zero, so Task Scheduler's
*Last Run Result* shows `0x1`, and intake's output (with the name of the file
it choked on) is in `data\transcribe.log`. A file that fails stays in the
inbox for the next run. `lifecrm doctor` also warns about a typo'd inbox or
archive path, files waiting in an inbox for over two days, and files in a
format intake will never read.

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

### Health data (Android)

Sleep, steps, weight, workouts, heart rate, and nutrition come in through
Android's **Health Connect** hub — the apps you already use write into it,
and its export is one zip that
[docs/health-connect.md](docs/health-connect.md) walks through importing:
dry-run report first (counts, date spans, sample values), then `--apply`.
Deterministic parsing, dedupe on the source's own record ids, updates only
from newer records, conflicts surfaced instead of overwritten.

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
.venv/Scripts/python -m lifecrm health import hc-export.zip           # dry-run report
.venv/Scripts/python -m lifecrm health import hc-export.zip --apply   # then write
.venv/Scripts/python -m lifecrm health retract --source-file hc-export.zip  # undo it
.venv/Scripts/python -m lifecrm observe ios_messages     # texts: finds the iPhone backup; see docs/texts.md
.venv/Scripts/python -m lifecrm handles list --suggest    # who is who, from the phone's contacts

.venv/Scripts/python -m lifecrm dashboard      # charts at /-/dashboards (Ctrl+C stops)
.venv/Scripts/python -m lifecrm ask "how did I sleep last week"
```

AI chat history (Claude / ChatGPT exports — see [docs/ai-chat.md](docs/ai-chat.md)):

```bash
.venv/Scripts/python -m lifecrm observe ai_chat claude-export.zip           # dry run
.venv/Scripts/python -m lifecrm observe ai_chat claude-export.zip --apply
.venv/Scripts/python -m lifecrm handles list                                # who is who
.venv/Scripts/python -m lifecrm imports retract <uuid>                      # undo a run
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

## Dashboards

```bash
.venv/Scripts/python -m lifecrm dashboard
```

Two dashboards at `http://127.0.0.1:8001/-/dashboards` — **Journal** (days
logged, entries per day, categories, mood & energy, open loops) and
**Health** (sleep, steps, weight, workouts, heart rate, food energy) — plus
all of Datasette underneath for ad-hoc exploring of every table.

Definitions are code: [dashboards/dashboards.yml](dashboards/dashboards.yml),
reviewed and versioned like everything else, and the test suite executes
every chart query against the current schema so a migration can't silently
blank a chart. The server reads a **snapshot taken at launch** — the live
database is never held open (nothing can block backups or restores) — so
refreshing the data is just rerunning the command. Add `--host 0.0.0.0` to
open it from your phone on the same network.

## Asking questions

```bash
.venv/Scripts/python -m lifecrm ask "how did I sleep last week"
```

```
Sleep totalled 7.09 h per day over 5 day(s) (min 6.47 h 2026-07-27,
max 7.75 h 2026-07-26) [M1]. Related entries: 2026-07-27 Gym: push day [E1].
```

Every claim carries an evidence id, and the window it used is printed with
the answer. Retrieval is deterministic — rules parse the question, SQL
answers it over a read-only connection — so **it works with no AI installed
at all**. If you install a local model ([Ollama](docs/ask.md), one command),
it phrases the same evidence in prose and its citations are verified against
that evidence before you see them; a model that cites something nonexistent
gets flagged, not printed as fact.

Because the model never writes SQL or does arithmetic, a small 7-8B local
model is enough. Nothing is metered and nothing leaves the machine.
Questions the database *can't* answer are logged — `ask --history` shows
them, and that list is the best guide to what to import next.
[docs/ask.md](docs/ask.md) covers windows, metrics, and the `--json` tool
interface for hooking up a chat UI or voice front-end later.

## How it's built

- **SQLite is canonical** (`data\lifecrm.db`, WAL, schema v4 with a
  transactional migration runner in `lifecrm/migrations/`). Sources,
  transcripts, and segments are immutable; entries carry stable UUIDs and are
  corrected via revisions, retraction, closure, and merges — never
  destructive rewrites.
- **Two trust tiers, one rule**: the confirmed tier (`entries`,
  `measurements`, `mentions`) is everything you reviewed through `/log`; the
  observed tier (`observations`, `health_records`) is bulk-imported at a scale
  nobody reviews — cited, never asserted, and structurally unable to write to
  the confirmed tables. `lifecrm observe` imports it, `imports retract` undoes
  it, and `handles link` attributes a whole decade of messages in one decision.
  See [docs/observed-tier.md](docs/observed-tier.md).
- **Two metric layers, one rule**: `measurements` holds what the journal
  said; `health_records` holds what sensors recorded (Health Connect
  import), keyed on the source system's own record ids so re-imports dedupe,
  updates apply only from newer records (audit-logged), and disagreements
  surface as conflicts instead of overwrites.
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

Tests: `.venv/Scripts/python -m pytest tests -q`.

## Roadmap (deliberately not built yet)

This work completes the **foundation**, not the application. After real
notes accumulate — and the schema has survived contact with them — the next
layers go on top: richer dashboard views (timeline, entity pages, review
queue — the first Datasette layer shipped as `lifecrm dashboard`), a live
Obsidian mirror, typed entity↔entity relationships (Jake works_at Acme),
weekly review briefs drafted from the same evidence `ask` retrieves, and a
voice front-end over the `ask --json` interface. `transcript_segments` and
stable entry UUIDs are already in place as their extension points.

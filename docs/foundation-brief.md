# Foundation completion brief

Work order for finishing the Life CRM foundation. Read `CLAUDE.md`,
`README.md`, `docs/contract.md`, and `lifecrm/schema.sql` before starting.

**Context:** the substrate (schema, contract, reconciliation, provenance,
revisions, audit, FTS, 104 passing tests) is built. The database is empty —
zero days, zero entries, zero sources. Nothing has been logged yet. That makes
this the cheapest possible moment for schema changes and the most urgent
moment for making daily capture actually work.

**Definition of done:** the owner can capture from an iPhone in one tap, a day
made of several notes extracts as one coherent day, the schema can represent
an open loop and a thread, the archive is readable without this codebase, and
one command proves the database is healthy.

**Scope honesty:** F0–F6 completes the *foundation*, not the Life CRM. The
dashboard, `/recall`, `/review-week`, entity↔entity relationships, a live
Obsidian mirror, and passive data channels stay deferred per the README
roadmap. Do not build them, and do not describe this work as finishing the
application.

## Ground rules

- Run from the project root with `.venv\Scripts\python.exe`, `PYTHONUTF8=1`.
- All writes go through `python -m lifecrm ...` → `lifecrm/domain/`. Never
  write to `data\lifecrm.db` with direct SQL. Never hand-edit files in `data\`.
  (Direct SQL *inside a test fixture* is legitimate — it simulates the
  corruption the domain layer must never produce.)
- `data\`, `backups\`, `config.toml` are private and gitignored. Never commit
  them; never paste transcript contents anywhere.
- `.venv\Scripts\python.exe -m pytest tests -q` passes at every commit. The
  existing 104 are the floor.
- Commit per task. Say what and why.

**This brief is a statement of intent, not a specification to follow
literally.** Its first draft confidently asserted that multiple sources per day
already reconcile together (they do not), specified a Shortcut that stamps the
wrong clock time, and asked an agent to physically record a voice note on a
phone it cannot hold. Assume comparable errors remain. Where the code
contradicts the brief, the code wins — say so and proceed. Where a better
design exists, take it and justify the choice in the commit message. Open
questions are collected at the bottom; they are yours to decide.

Order: **F0 → F1 → F2 → F3 → F4 → F5 → F6.** F0 is a prerequisite for F2. F1
is what lets the owner start logging, so land it early even if the rest slips;
its full value arrives with F2's multi-source contract.

---

## F0 — Fix the migration runner before it has a real migration to corrupt

`lifecrm/migrations/` does not exist yet, so the runner at [db.py:83](../lifecrm/db.py)
has never applied a real migration. It is not safe.

`sqlite3.executescript()` commits any pending transaction and then performs no
transaction control of its own — each statement autocommits. The comment at
line 99 claiming it "opens its own implicit transaction" is false, and the
`conn.rollback()` at line 103 rolls back nothing. Reproduced:

```
script  = "CREATE TABLE good (id INTEGER); INSERT INTO nonexistent VALUES (1);"
result  = OperationalError: no such table: nonexistent
tables  = ['meta', 'good']     ← the first statement survived the rollback
version = 1                    ← schema_version says nothing happened
```

A half-applied migration with a version number claiming otherwise is the exact
failure this system's careful provenance is meant to make impossible.

The existing failure test cannot catch it: `0002_bad.sql` is
`CREATE TABLE broken (;`, which dies at parse before anything applies
([test_db.py:88](../tests/test_db.py)).

Required:

- Schema changes and the `meta.schema_version` update commit in **one explicit
  transaction**, or neither does. Fixing the comment is not fixing the bug.
- `meta.contract_version` updates as part of a migration that changes the
  contract — currently nothing ever updates it after `init`.
- Reject duplicate migration numbers and gaps in the sequence, with a clear
  error naming the offending files.
- A test whose migration creates a table and *then* fails, asserting the table
  does not survive. Keep the existing parse-failure test too; they cover
  different things.

Note the constraint that shapes the fix: DDL inside an explicit transaction is
fine in SQLite, but `executescript` is the wrong tool. Statement-splitting a
`.sql` file naively breaks on semicolons inside triggers and string literals —
`schema.sql` already contains trigger bodies with internal semicolons, so any
splitter you write must survive them, or the design should avoid splitting
altogether. Choose an approach and explain why it is safe.

---

## F1 — iPhone capture, and a mid-day text channel

The only documented capture path is Android-specific (README lines 58–71) and
has never run end to end. The owner is on iPhone. iOS has no Autosync
equivalent: the OneDrive app auto-uploads the camera roll only, and Voice
Memos syncs to iCloud, not OneDrive. The bridge has to be Apple Shortcuts.

The Python side is already format-agnostic — `AUDIO_EXTS`
([transcribe.py:30](../lifecrm/transcribe.py)) includes `.m4a`, which is what
iOS produces.

### F1a — "Log My Day" shortcut (audio)

Write the recipe into a new `docs/iphone.md`, action by action, naming each
Shortcuts action exactly as it appears in iOS.

**Two timestamps, never conflated.** This is the part the first draft got
wrong:

```
CapturedAt   = Current Date, unmodified          → the real clock time
JournalDate  = CapturedAt minus day_rollover_hour → the day it belongs to

filename = "<JournalDate as yyyy-MM-dd> <CapturedAt as HH.mm.ss>.m4a"
```

Deriving the clock time from the adjusted date stamps a 00:30 note as `19.30`.
Include the UTC offset in the filename if you can do it without making the name
unwieldy — the source row carries a timezone and `recorded_at` should be
unambiguous.

Then **Save File** → `OneDrive/VoiceJournal/inbox`, "Ask Where to Save" off,
"Overwrite If File Exists" off.

Why the rollover subtraction matters: `process_file`
([transcribe.py:155](../lifecrm/transcribe.py)) lets a filename date **win**
over the rollover calculation. Keep that precedence — it is the right call,
since the phone knows things the file mtime does not — and make the Shortcut
agree with it. Pin the precedence with a test in a new `tests/test_transcribe.py`
so a later refactor cannot silently invert it.

Extend `date_from_filename` (or add a sibling) to parse the **time** as well
and feed `recorded_at`. Today `recorded_at` comes from file mtime, which after
a OneDrive round trip may be the upload time rather than the recording time.

Document the triggers and which suits which hardware: Action Button (iPhone
15 Pro and later), Back Tap (Accessibility → Touch), Lock Screen control
(iOS 18+), Home Screen icon, Siri phrase. A time-of-day Personal Automation
around 21:00 is a useful nudge — state plainly in the doc that it opens the
shortcut rather than recording unattended, because iOS will not capture audio
from a background automation. Do not imply hands-free operation.

Fallback worth documenting: Voice Memos → Share → Save to Files →
`OneDrive/VoiceJournal/inbox`.

### F1b — "Quick Note" shortcut + text intake

One nightly note only captures what the owner still remembers at 9pm. iOS
on-device dictation is instant and good, and `phone_transcript` already exists
as a source type with nothing producing it.

Shortcut: **Dictate Text** → **Save File** to a new
`OneDrive/VoiceJournal/inbox-text` as `<JournalDate> <CapturedAt>.txt`, same
two-timestamp rule. One file per note — do not append to a shared daily file;
one-file-per-note removes the problem entirely.

Code:

- `inbox_text_dir` in `config.example.toml` and `lifecrm/config.py`.
- Text-inbox intake creating `phone_transcript` sources under the same rules
  the audio path uses: rollover-aware dates, filename precedence,
  `content_hash` dedupe (warn, never block), sentence-boundary segments with
  char offsets, archive after intake, per-file commit.
- Reuse `date_from_filename`, `sources.content_hash_bytes`,
  `sources.duplicate_warnings`, `archive_file`, `importer.import_text`.
- `lifecrm status` reports pending text files alongside `pending_audio`.
- The OneDrive placeholder guard (`is_placeholder` / `read_hydrated`) applies
  to text exactly as to audio.

Decide whether this belongs in `lifecrm transcribe` or a new `lifecrm intake`
that runs both, and justify it. Prefer `intake` if `transcribe` would load the
whisper model just to move a `.txt`.

### F1c — Fix archive-before-commit (both paths)

[transcribe.py:181](../lifecrm/transcribe.py) moves the file into the archive
*before* the source and transcript rows are created and committed. On a failure
the audio still exists, but it has silently left the inbox un-ingested and will
never be rescanned — the owner loses a day of their life and gets no error they
will ever see.

Make the sequence recoverable: commit first and move after, or move back on a
failed transaction. Either is acceptable; a crash between the two steps must
leave the system in a state the next run repairs by itself. Test it by forcing
a transaction failure and asserting the file is still in the inbox.

### F1d — Verification (split honestly)

You can build the intake, exercise it with synthetic files named exactly as the
Shortcut names them, and verify dates, times, dedupe, segments, and archiving.

You **cannot** verify the Shortcut itself — fixed-folder saving through the
OneDrive Files provider is precisely the behavior that must be tested on the
owner's physical phone. Do not claim to have done it.

Instead, produce a short numbered checklist at the end of `docs/iphone.md` for
the owner to run: record a note, confirm it lands in the inbox, run intake,
confirm the journal date and clock time, then `/log` end to end. Ask for the
sync latency and whisper wall-clock on a ~3 minute note so the README timings
are real rather than guessed.

---

## F2 — Schema v2 and contract v2

Four structural gaps. All cheapest at zero rows; two are unrecoverable if
deferred.

### F2a — One day, many sources

The first draft of this brief claimed multi-source days already reconcile
together. They do not. `extraction_runs.source_id` is singular
([schema.sql:77](../lifecrm/schema.sql)), the contract accepts exactly one
`source`, and reconcile threads a single `source_id` throughout
([reconcile.py:81](../lifecrm/domain/reconcile.py)). An evening note plus three
mid-day quick notes means four drafts, four diffs, four review cycles — and F1b
makes that the normal case, not the exception.

It is less broken than it looks, which is why the fix is contained:

- Existing entries load by `journal_date` ([reconcile.py:85](../lifecrm/domain/reconcile.py)),
  so cross-source duplicate detection already works.
- Retract proposals are same-source scoped ([reconcile.py:152](../lifecrm/domain/reconcile.py)),
  so nothing false-retracts across sources.
- `entry_sources` is keyed `(entry_id, source_id)` — the entry side is already
  multi-source.

Add:

```sql
CREATE TABLE extraction_run_sources (
  extraction_run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
  source_id         INTEGER NOT NULL REFERENCES sources(id),
  PRIMARY KEY (extraction_run_id, source_id)
);
```

The contract takes `sources: [...]`, and each entry's provenance must name
**which** source and which segments or char range support it — otherwise
provenance silently degrades from "the passage behind this record" to "one of
today's four notes," which is the one guarantee this system exists to keep.

Decide what happens to `extraction_runs.source_id`: keep it as a denormalized
primary-source pointer, or drop it and go through the join table everywhere.
Either is defensible; state which and why. `apply` and `status` both assume
singular today.

### F2b — Closable loops

`entries.status` is `active|superseded|retracted` ([schema.sql:102](../lifecrm/schema.sql))
— lifecycle states about whether a record is *correct*. Nothing records whether
a `task` or `plan` was ever *done*. Loops open and never close.

Add `closed_at TEXT` (nullable, ISO-8601 UTC) and
`closure TEXT CHECK (closure IN ('done','dropped','obsolete'))`, both set or
both null.

Do not overload `status`. Retracting means "this record was wrong." Dropping a
plan means "this really happened and I chose not to do it." Those must stay
distinguishable in 2040.

Then `lifecrm entry close <uuid> --closure done [--at ...]` and
`lifecrm entry reopen <uuid>`, routed through the domain layer with revision
and audit records like every other correction. Add open loops to
`lifecrm status` (kind in task/plan, active, `closed_at IS NULL`), ordered by
`due_at`, overdue marked.

### F2c — Closing across days, from the contract

`links` alone cannot express the most common real utterance: *"finished the
thing I mentioned Tuesday."* That is an update to an entry on a **different
day** than the one being extracted, and the contract has no operation for it.

Add a top-level operation:

```json
"entry_updates": [
  {"target_uuid": "...",
   "set": {"closure": "done", "closed_at": "2026-07-28T22:00:00Z"},
   "provenance": {"source_id": 17, "char_start": 120, "char_end": 180}}
]
```

Constraints: reconciles across dates; always a requires-decision diff item
(never auto-applied); preserves provenance for the update itself, so the
closure traces to the sentence that closed it; never silently modifies a
confirmed record. Which fields `set` may touch is your call — start narrow
(closure fields) and say what you deliberately excluded.

### F2d — Entry links

The only edge in the schema is entry→entity (`mentions`, five fixed roles). A
six-month project is N disconnected rows that happen to name the same entity.

```sql
CREATE TABLE entry_links (
  id         INTEGER PRIMARY KEY,
  from_uuid  TEXT NOT NULL REFERENCES entries(uuid) ON DELETE CASCADE,
  to_uuid    TEXT NOT NULL REFERENCES entries(uuid) ON DELETE CASCADE,
  type       TEXT NOT NULL CHECK (type IN
    ('continues','caused','resolves','contradicts','part_of','relates')),
  note       TEXT,
  actor      TEXT NOT NULL CHECK (actor IN ('user','extraction','system')),
  created_at TEXT NOT NULL,
  UNIQUE (from_uuid, to_uuid, type),
  CHECK (from_uuid <> to_uuid)
);
```

`ON DELETE CASCADE` is required, not decorative: `admin purge-day` hard-deletes
entries ([corrections.py:173](../lifecrm/domain/corrections.py)) and
`PRAGMA foreign_keys=ON` is set on every write connection, so a plain FK would
break purge. Note the deliberate asymmetry with `entry_revisions.entry_uuid`,
which carries *no* FK because revision history must outlive a purge — a link
to a deleted entry is meaningless, a revision of one is not. Document that
reasoning in the schema comment.

Direction must be documented and unambiguous: `child part_of parent`,
`new_event resolves old_task`, `later continues earlier`.

On the `type` enum — a lookup table like `categories`/`entry_kinds` is a
reasonable alternative, and link vocabulary will plausibly evolve. I chose
CHECK because the existing schema draws a consistent line: user vocabulary gets
a table (`categories`, `entry_kinds`, both CLI-extensible), structural
vocabulary gets a CHECK (`entities.type`, `mentions.role`, `entry_sources`
roles), and link types are structural — reconcile and future graph queries will
branch on them. Once F0 lands, extending a CHECK is a trustworthy migration.
It is a genuine coin flip. Overrule me if you disagree, and say why.

Add `lifecrm link add <from> <to> --type X`, `link list <uuid>`, `link rm`, all
audited; show incoming and outgoing links in `lifecrm entry show`.

**Why links now and entity↔entity relationships later:** entity edges (Jake
works_at Acme) are reconstructible at any time from a small set of stable rows.
Thread structure is not — "this continues that" is only knowable while the
memory is fresh, at write time. The cheap table now costs nothing and prevents
a permanent loss.

### F2e — Mechanics

- Bump `SCHEMA_VERSION` to 2 and `CONTRACT_VERSION` to 2 in `lifecrm/__init__.py`.
- Create `lifecrm/migrations/0002_*.sql` — the first real migration, running on
  the runner F0 just fixed.
- Update `schema.sql` to match, so fresh `init` and migrated databases agree.
- Test that they agree: init a v1 database from a pinned copy of the v1 schema,
  migrate it, and compare normalized `sqlite_master` against a fresh v2 init.
  Drift between those two paths is a bug that would otherwise surface years
  later, during a restore, on a bad day.
- Contract v2 in `docs/contract.md` with the version-history line that file's
  footer requires. Extraction-proposed changes to confirmed entries keep
  following the existing requires-decision path.

---

## F3 — Export: the archive must outlive the code

The README promises the database outlives every tool on top of it. Today
reading it requires this Python package. SQLite is a good format and a bad
guarantee.

`lifecrm export`:

- `--format jsonl --out <dir>` — one file per table, one JSON object per line,
  UUIDs rather than integer ids wherever a stable identity exists, plus a
  `manifest.json` with schema version, contract version, export timestamp, and
  per-table row counts.
- `--format markdown --out <dir>` — `YYYY/YYYY-MM-DD.md` per day: front-matter
  (date, summary, day measurements), entries grouped by category with kind,
  title, body, measurements, closure state, links. Entities as `[[Wiki Links]]`,
  one `_entities/<slug>.md` page each listing every day it appears.
  Human-readable first: this is the artifact that has to make sense to a person
  in 2046 with no tooling at all.

Both deterministic — stable ordering, no timestamps in content outside the
manifest — so consecutive exports diff cleanly.

Test: seed via the domain layer, export, re-parse the JSONL, assert row counts
and sampled field values match exactly. Add a section to `docs/restore.md` on
reading an export with no code.

Not the deferred Obsidian feature: one-way dump, no sync, no vault config, no
live updating. Keep it that way.

---

## F4 — `lifecrm doctor`

A system this careful about provenance needs a way to prove it is intact.
[schema.sql:118](../lifecrm/schema.sql) states outright that
exactly-one-primary-category is enforced in the domain layer with only a
partial database backstop — meaning a bug there is silent.

`lifecrm doctor [--json]`, non-zero exit on any failure:

- `PRAGMA integrity_check`, `PRAGMA foreign_key_check`.
- Exactly one primary category per entry.
- FTS in sync — compare `entries_fts` / `transcripts_fts` against their content
  tables rather than trusting the triggers.
- No entry without an `entry_sources` provenance row; no source without a
  transcript; no transcript without segments.
- No dangling `superseded_by`; no `mentions` on a merged entity; no measurement
  violating owner-XOR.
- v2 additions: `closed_at`/`closure` set together; `extraction_run_sources`
  consistent with `entry_sources` for the same run; links resolving to live
  entries.
- Schema version matches `SCHEMA_VERSION`; no pending migrations.
- Warnings, not failures: last backup age, orphaned drafts in `data\staging\`,
  extraction runs stuck in `draft`, overdue open loops.

Tests deliberately corrupt a temporary database — direct SQL is correct here —
and assert each check fires.

---

## F5 — Operations that don't depend on remembering

`scripts/transcribe-inbox.ps1` is scheduled; nothing else is. Backups happen
only when someone types the command, and `docs/restore.md` describes a path
that has never been executed.

- `scripts/backup.ps1` on a daily Task Scheduler trigger, documented in the
  README beside the existing transcribe task.
- Tiered retention, replacing `--keep 8` (roughly a week of history for the
  only copy of a decade of memory): **14 daily, 12 weekly, 24 monthly, yearly
  indefinitely.** These snapshots are small; this is cheap insurance. Implement
  it rather than proposing it.
- New `scripts/verify-restore.ps1`: restore the newest snapshot into a scratch
  directory, run `doctor` against it, compare row counts to live, report, clean
  up. Never touches `data\lifecrm.db`. Weekly trigger.
- Run it once for real. An untested backup is not a backup.
- Add `.claude/settings.local.json` to the repo `.gitignore`. It is currently
  excluded only by the owner's *global* gitignore
  (`~/.config/git/ignore`), so the exclusion does not travel with the repo to
  another machine.

---

## F6 — Documentation sync

Land last, once F0–F5 are real:

- **README** — iPhone flow replacing Android (keep Android as a secondary
  note), link `docs/iphone.md`, update the pipeline diagram for the text
  channel, add `export`/`doctor`/`link`/`entry close` to the command list,
  correct the test count, and state that F0–F6 completes the foundation rather
  than the application.
- **`docs/contract.md`** — v2 fields, `sources: [...]`, `entry_updates`,
  `links`, version history.
- **`.claude/skills/log/SKILL.md`** — extraction must handle several same-day
  sources as one draft, propose closures via `entry_updates` when a note
  resolves an earlier task, propose links when a note continues an earlier
  thread, and route all of it through the existing review-before-apply gate.
  This is the file that turns the schema into behavior; give it real thought
  rather than a mechanical field list.
- **`CLAUDE.md`** — new commands, the `lifecrm/migrations/` layout, and move
  anything now built out of the deferred list.

---

## Open questions — yours to decide

Decide these, implement your answer, and record the reasoning. Do not ask
first; do report what you chose.

1. `lifecrm intake` as a new command versus folding text into `transcribe`.
2. Whether `extraction_runs.source_id` survives F2a as a primary-source
   pointer or is replaced entirely by the join table.
3. `entry_links.type` as CHECK or lookup table — my reasoning above is a coin
   flip, not a mandate.
4. Which fields `entry_updates.set` may touch in v2.
5. How the migration runner executes a `.sql` file transactionally without a
   naive statement splitter that breaks on trigger bodies.
6. Whether `days` deserves anything beyond `summary` once several sources land
   on one day — you will see the shape of the data before anyone else does.

And the standing one: anything in this brief that turns out to be wrong once
you are in the code. There were three such errors in its first draft. Finding
the fourth is part of the job, not a deviation from it.

## Report back

What shipped; what you overruled and why; what the synthetic-file intake tests
proved and what remains unproven until the owner runs a real phone; the final
test count; and anything you scoped down. A scoped-down item reported plainly
is fine. A silently skipped one is not.

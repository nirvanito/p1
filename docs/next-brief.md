# Next brief — from foundation to Second Brain

Work order for everything after the foundation. Written 2026-08-25, after the
observed tier (schema v4 + `ai_chat` connector) was built and 286 tests pass.
Read `CLAUDE.md`, `docs/observed-tier.md`, and `docs/contract.md` before
starting any phase.

**How to use this file:** work top to bottom, one numbered task per session or
commit. Each task says what "done" looks like. Phases 4+ are gated on real
data existing — do not build ahead of the gate. Where this brief contradicts
the code, the code wins; say so and proceed. Its predecessor
(`docs/foundation-brief.md`) contained real errors found during
implementation; assume this one does too.

**Context that will not be in the code:**

- The database is nearly empty. Nothing has been logged; the pipeline is
  unproven on real content. That is why Phase 2 exists and why derived layers
  are gated.
- A six-subsystem adversarial audit ran 2026-08-24 (104 candidate findings, 99
  verified). Phases 1 and 3 are its surviving items. Evidence file:line refs
  were verified that day; re-verify before editing.
- `lifecrm/recall.py` and `lifecrm/llm.py` (F10) were NOT covered by that
  audit.
- The end state (see memory / README roadmap): all-app data in one SQLite hub
  → dashboards → local-LLM cited recall → voice. Source priority by richness:
  texts > AI chat > voice > notes > calendar > health > finance > photos >
  videos. Strategy: read from existing apps, never build capture (except
  voice/food/subjective state, which the voice channel already covers).

## Ground rules (unchanged from the foundation)

- Run from project root with `.venv\Scripts\python.exe`, `PYTHONUTF8=1`.
  Tests: `pytest tests -q --basetemp=<scratch>` — green at every commit.
- All writes through `python -m lifecrm ...` → `lifecrm/domain/`. Importers
  may never write `entries`/`measurements`/`mentions`/`entity_aliases`.
- Bulk data goes to the observed tier via a connector (pure reader —
  `connectors/base.py`); the /log review gate is only for confirmed records.
- Every new reader is BOM-tolerant (`utf-8-sig`). Every import is dry-run by
  default. Every destructive path has an undo or a snapshot.
- Schema changes = numbered migrations, `schema.sql` kept in parity
  (the parity test enforces it).
- `data\`, `backups\`, `config.toml` never enter git. No metered API calls.
- Commit per task, message says what and why.

---

## Phase 0 — Land what exists (first session)

0.1 **Commit the observed tier.** The v4 migration, `domain/observations.py`,
`connectors/`, CLI (`observe`/`connectors`/`imports`/`handles`), doctor +
export coverage, docs, 62 new tests — all sitting uncommitted in the working
tree right now. One commit ("F11: observed tier — schema v4 + ai_chat
connector" or similar). Done when: `git status` clean, 286 tests green.

0.2 **User actions, not code** (park these, remind at session start):
`gh auth login` → private repo → push; run the LifeCRM→SecondBrain rename
script; register scheduled tasks (after 1.5).

---

## Phase 1 — Integrity: shipped commands that lose data

All verified against the code 2026-08-24. Cheap now, expensive after real
data exists. Highest priority in the whole brief.

1.1 **`entry merge` destroys provenance.** `corrections.py:140-142` does
UPDATE OR IGNORE then DELETE on `entry_sources`; PK is (entry_id, source_id),
so when src and dst share a source (the normal case — one voice note, many
entries) the row and its `original_json` are destroyed, and doctor fails
`entries_have_provenance` forever after. Reproduced. Fix: copy-don't-move
(leave src's rows, INSERT what dst lacks). Fix the "Nothing is deleted"
docstring. Done when: a test merges two same-source entries with real
provenance and doctor stays green; `entry show <src>` still prints its
excerpt.

1.2 **Doctor-stays-green test.** One parametrized test: seed golden draft,
run every domain op in turn (edit/retract/restore/close/reopen/entry
merge/entity merge/alias/link add+rm/purge-day/health import/observe
apply+retract), assert `run_checks` reports 0 failures after each. Write as
part of 1.1 — it is the test that would have caught it.

1.3 **`apply` staleness guard.** Reproduced: ingest same draft twice, apply
diff1, apply diff2 → the day doubles (6→12 entries). Record a fingerprint of
the day's state in the diff at build time; refuse a stale diff with a
one-line "re-run ingest" error; rename applied diff files (`.applied`). The
re-ingest→apply loop must keep working.

1.4 **`health retract`.** `health_records.status='retracted'` exists in the
schema, doctor and dashboards filter on it, nothing writes it — a bad unit
conversion is currently unrecoverable short of restore. Add
`lifecrm health retract --source-file <name>` (mirror
`observations.retract_import`). Done when: an applied import is reversible
and a test reaches status='retracted'.

1.5 **Failure visibility bundle** (must precede real capture):
  - `intake`/`transcribe` exit 1 when any file failed; `transcribe-inbox.ps1`
    checks `$LASTEXITCODE` (copy the pattern from `verify-restore.ps1:31`).
  - Doctor warnings: configured inbox/archive dir missing on disk; inbox file
    older than ~2 days; unrecognized-extension files in the inbox (named).
  - `lifecrm import` reads `utf-8-sig` like every sibling reader.
  - Config errors (bad TOML, bad timezone, missing file, locked DB) become
    one `error:` line, not a traceback.
  Done when: a poison file makes the scheduled task report failure, and a
  typo'd inbox path is warned about by doctor.

1.6 **Small integrity fixes** (one commit): `add_provenance` INSERT OR
REPLACE clobbers original extraction provenance (`entries.py:172`) — make
non-clobbering; inline-text drafts validate offsets against the raw draft
string instead of the stored normalized transcript (`cli.py` inline_text —
pass `importer.normalize_text` output); `days.set_summary` is the only
unaudited domain mutation — add the audit row.

1.7 **Privacy bundle**: add `/archive/`, `/archive-data/`, `/exports/`,
`*.zip` to `.gitignore` (README's own export command currently writes into
unignored paths — verified with `git check-ignore`); extend the privacy probe
test to cover an export file, manifest, zip, config.toml,
settings.local.json; `dashboard --host` non-loopback prints a one-line
warning; document `data\dashboard\lifecrm.db` in privacy.md.

---

## Phase 2 — Prove it on real data (mostly user-driven; code assists)

Order matters: each step exercises what the next depends on.

2.1 **First real /log.** One real day, ≥2 sources, through the skill by
hand. First time the `sources:[{source_id}]` draft shape hits the real CLI.
Then add the missing CLI-level test for exactly that shape.

2.2 **Real AI chat export through `ai_chat`.** The connector has only seen
fixtures. Dry-run the real Claude export; read the date span and sample
lines; fix what breaks with a fixture reproducing it; then `--apply` and
`handles link` the account. Done when: real conversations are queryable via
`observations_fts` and doctor is green.

2.3 **First real phone audio** (user's friend, iPhone): checklist at the end
of `docs/iphone.md`. `transcribe_audio` has literally never executed —
every test monkeypatches it. Also add the cheap regression test: feed the
real function a fake segment iterator and pin the char-offset arithmetic.

2.4 **Register scheduled tasks** (`install-tasks.ps1`) — only after 1.5, so
failures are visible. Confirm an unattended run leaves a log and a snapshot.

2.5 **Real Health Connect export** — LAST, after 1.4 gives it an undo.
Follow the checklist in `docs/health-connect.md`; add the missing check:
`SELECT DISTINCT origin_app` per metric (two apps writing steps silently
doubles dashboard SUMs). Write findings back into the doc.

---

## Phase 3 — Correctness and trust (parallel with Phase 2)

3.1 **Contract/CLI gaps** (audit-verified): validate the `extraction` block
in `contract.py` (currently only in the CLI, so `--dry-run` lies); enforce or
re-document the same-day rule on `entries[].uuid` (doc and validator
disagree); add `admin discard-run` (doctor names a command that doesn't
exist); fix or drop the half-wired `transcribe --date`; duplicate detection
needs a corroborating signal ("Called Mom"/"Called Tom" = 0.900 similarity,
false positive) and the incumbent's body in the diff item.

3.2 **Tests that would have caught what shipped**: export coverage asserted
against `sqlite_master` (today it audits its own manifest); dashboard chart
queries executed in their `[[ filtered ]]` form (3 charts never run in CI);
v1-snapshot restore through the CLI landing at v4; `migrate` CLI test.

3.3 **Backups you can trust**: `verify-restore.ps1` gets a lower bound
(today an empty restored DB prints OK) and compares all tables from
sqlite_master, not 3 hardcoded; a scheduled doctor run against the LIVE db
(the only scheduled one audits the scratch copy); split local vs cloud
backup-age checks.

3.4 **Audit `recall.py` + `llm.py`** — the only modules never reviewed.
One pass: correctness of window parsing, SQL injection surface of the
retrieval layer, citation-check bypasses, behavior on a v4 database.
Extend `ask` retrieval to `observations` + `observations_fts` (cited as O#),
so chat history becomes askable — this is the first visible payoff of the
observed tier. Retrieval stays deterministic and read-only.

---

## Phase 4 — Connectors, one at a time (the long middle)

Pattern is fixed: pure-reader `scan()` in `lifecrm/connectors/<name>.py`,
register in `base.registry()`, fixtures + tests, doc page, dry-run first on
real data. Each is its own commit. Effort-adjusted order:

4.1 **Calendar** (`gcal`/ICS). Trivial parse (ICS or Google Takeout), huge
entity yield: every attendee is an `email` handle, every location a
candidate place. Streams: `calendar_event`. This is the connector that
bootstraps the people graph.

4.2 **Notes/docs** (Apple Notes export, markdown folders, Google Keep
Takeout). Stream `note`; `last_modified_at` from file mtime; body = text,
title = filename/heading.

4.3 **Finance** (bank/card CSV export first — free and universal; Actual
Budget's SQLite if adopted). Stream `txn`: `value`+`unit='usd'`, merchant in
`title`, `counterparty` handles (`card` kind exists for this). NO
auto-categorization — that is a derived layer.

4.4 **Texts — the crown jewel and the worst acquisition.** iPhone has no
API. Path: Apple Devices app makes a local unencrypted backup on Windows →
parse `sms.db` (Manifest.db → hashed file names → Messages SQLite) → streams
`imessage`/`sms`, `phone` handles per participant, group chats =
multiple participants. Decide deletion semantics before first import
(provisional: observations stay when messages are deleted — the archive
remembers). Budget real time for this one; it is a project, not a task.

4.5 **Photos — metadata only.** EXIF timestamp + GPS + camera into
`value_json`, path into `media_path`, never the pixels. Answers "the lake in
Slovenia 4 years ago" almost for free. Reverse-geocoding = derived layer,
not import time.

4.6 **When wanted, same pattern**: browser history (browserexport), Google
Takeout location, ActivityWatch screen time, WhatsApp export.

**After the first two backfills:** verify `status`/streak still count days
with *entries*, not days that merely exist (backfill creates thousands of
empty `days` rows); check dashboard queries stay fast; add
`observations` day-counts line to `lifecrm status`.

---

## Phase 5 — Derived layers (each gated on data thresholds)

5.1 **Handle-review ergonomics** (after texts land): `handles list` gains
`--suggest` using display_name ↔ existing entity/alias matches — suggestions
only, confirmation stays manual. Gate: >50 unresolved handles.

5.2 **`/review-week` skill.** Reads last 7 days (entries, closed/open loops,
day summaries, health trends, observation counts) → draft brief through the
existing staging→review→apply gate. Gate: 4 consecutive weeks of real
logging.

5.3 **Richer `ask` / `/recall`.** Cross-tier questions ("when did I last
talk to X" = mentions + observation participants), temporal entity answers.
recall_log's unanswered rows are the backlog that drives this. Gate: ~3
months of data.

5.4 **Dashboards v2.** Fix the 3 empty-state cards now (cheap); new views
only when you can name three questions the current 16 cards don't answer.

5.5 **Entity↔entity relationships** (schema-level: `entity_links`,
contract support, doctor checks — F2-sized). Gate: 50+ entities with repeat
mentions and an actual "who does X know" need.

5.6 **Advisors** (health/founder/finance lenses): scheduled reviews that
produce draft briefs through the review gate — /review-week generalized,
never always-on daemons, interpretations always regenerable + cited.

5.7 **Obsidian mirror** — read-only extension of the markdown export, only
after the export has actually been read a few times. Bidirectional = a
second write path = violates the architecture; requires an explicit
decision to even consider.

5.8 **Voice loop** (last, per roadmap): openWakeWord → faster-whisper →
`ask --json` → Piper TTS. Reads + draft creation only; never applies.

---

## Standing decisions already made (do not relitigate)

- Read from apps; never build capture UIs. Copy into `lifecrm.db`; never
  federate live.
- Two tiers, one rule. Importers emit handles, never names. Resolution is
  always a user decision.
- `health_records` stays separate from `observations`.
- Media stays outside the DB (`media_path` only).
- No LLM in any import path; LLM only phrases cited evidence in `ask`.
- Zero metered API calls; interactive Claude Code + local models only.

## Open decisions — decide when reached, record the answer here

1. Encryption at rest (before texts/finance land — the liability jumps an
   order of magnitude at 4.3/4.4). SQLCipher vs BitLocker-only vs deferred.
2. Deletion semantics per connector (provisional: archive remembers).
3. Export destination for `lifecrm export` (in-repo ignored dir vs outside).
4. Whether `health import` folds into `observe health` once a second
   metric-shaped connector exists.
5. `entries[].uuid` cross-day: forbid (doc) or surface target_date (code).

## Report back (every phase)

What shipped; what was overruled and why; what remains unproven until real
data or a real phone; test count; anything scoped down — plainly, never
silently.

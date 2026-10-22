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

**Status 2026-10-22.** Phases 1, 3 and 5 are built, and so is Phase 4
apart from ActivityWatch. The code-side parts of Phases 0 and 2 are done
too; what is left of those belongs to the owner. Schema v5, 2332 tests. The
owner asked to finish the whole Second Brain, ahead of the data gates, for
two setups end to end: Windows laptop + Samsung
([windows-android.md](windows-android.md)) and MacBook + iPhone
([mac.md](mac.md)). It also included a demo of both
([demo.md](demo.md)). Notes forwarded by the owner (the vision
doc, the source ranking, mac_messages_mcp, and "why not live access layers,
contacts"), became two Mac pieces. The first is `mac_messages`: `chat.db`
is read through a snapshot and copied into the DB, never federated. The
second is the `contacts` connector: Mac Contacts become handle-name
suggestions and are never auto-linked. What is still open is at the end of
this file.

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
  may never write `entries`/`measurements`/`mentions`/`entity_aliases`/
  `entity_links`.
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
*Done 2026-08-25 (0e1e697).*

0.2 **User actions, not code** (park these, remind at session start):
`gh auth login` → private repo → push; run the LifeCRM→SecondBrain rename
script; register scheduled tasks (after 1.5). *Still open 2026-10-22, plus
`lifecrm migrate` to bring the real database to schema v5; see "Still
open".*

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
excerpt. *Done 2026-08-25 (ef6a1eb), with 1.2.*

1.2 **Doctor-stays-green test.** One parametrized test: seed golden draft,
run every domain op in turn (edit/retract/restore/close/reopen/entry
merge/entity merge/alias/link add+rm/purge-day/health import/observe
apply+retract), assert `run_checks` reports 0 failures after each. Write as
part of 1.1 — it is the test that would have caught it.

1.3 **`apply` staleness guard.** Reproduced: ingest same draft twice, apply
diff1, apply diff2 → the day doubles (6→12 entries). Record a fingerprint of
the day's state in the diff at build time; refuse a stale diff with a
one-line "re-run ingest" error; rename applied diff files (`.applied`). The
re-ingest→apply loop must keep working. *Done 2026-08-25 (7c2baf0).*

1.4 **`health retract`.** `health_records.status='retracted'` exists in the
schema, doctor and dashboards filter on it, nothing writes it — a bad unit
conversion is currently unrecoverable short of restore. Add
`lifecrm health retract --source-file <name>` (mirror
`observations.retract_import`). Done when: an applied import is reversible
and a test reaches status='retracted'. *Done 2026-08-25 (eb45af9), with
`health restore`.*

1.5 **Failure visibility bundle** (must precede real capture):
  - `intake`/`transcribe` exit 1 when any file failed; `transcribe-inbox.ps1`
    checks `$LASTEXITCODE` (copy the pattern from `verify-restore.ps1:31`).
  - Doctor warnings: configured inbox/archive dir missing on disk; inbox file
    older than ~2 days; unrecognized-extension files in the inbox (named).
  - `lifecrm import` reads `utf-8-sig` like every sibling reader.
  - Config errors (bad TOML, bad timezone, missing file, locked DB) become
    one `error:` line, not a traceback.
  Done when: a poison file makes the scheduled task report failure, and a
  typo'd inbox path is warned about by doctor. *Done 2026-09-24 (ddb2a7d).*

1.6 **Small integrity fixes** (one commit): `add_provenance` INSERT OR
REPLACE clobbers original extraction provenance (`entries.py:172`) — make
non-clobbering; inline-text drafts validate offsets against the raw draft
string instead of the stored normalized transcript (`cli.py` inline_text —
pass `importer.normalize_text` output); `days.set_summary` is the only
unaudited domain mutation — add the audit row. *Done 2026-10-22 (a79020c):
add_provenance only fills gaps, inline text is normalized before offsets
are checked, set_summary is audited (and returns whether it changed).*

1.7 **Privacy bundle**: add `/archive/`, `/archive-data/`, `/exports/`,
`*.zip` to `.gitignore` (README's own export command currently writes into
unignored paths — verified with `git check-ignore`); extend the privacy probe
test to cover an export file, manifest, zip, config.toml,
settings.local.json; `dashboard --host` non-loopback prints a one-line
warning; document `data\dashboard\lifecrm.db` in privacy.md. *Done
2026-10-22 (a79020c); the probe lives in tests/test_integrity_privacy.py.*

---

## Phase 2 — Prove it on real data (mostly user-driven; code assists)

Order matters: each step exercises what the next depends on.

2.1 **First real /log.** One real day, ≥2 sources, through the skill by
hand. First time the `sources:[{source_id}]` draft shape hits the real CLI.
Then add the missing CLI-level test for exactly that shape. *Test done
2026-10-22 (a79020c); the real day is still the owner's to log.*

2.2 **Real AI chat export through `ai_chat`.** The connector has only seen
fixtures. Dry-run the real Claude export; read the date span and sample
lines; fix what breaks with a fixture reproducing it; then `--apply` and
`handles link` the account. Done when: real conversations are queryable via
`observations_fts` and doctor is green.

2.3 **First real phone audio** (user's friend, iPhone): checklist at the end
of `docs/iphone.md`. `transcribe_audio` has literally never executed —
every test monkeypatches it. Also add the cheap regression test: feed the
real function a fake segment iterator and pin the char-offset arithmetic.
*Regression test done 2026-10-22 (a79020c); the real phone run is still
open.*

2.4 **Register scheduled tasks** (`install-tasks.ps1`) — only after 1.5, so
failures are visible. Confirm an unattended run leaves a log and a snapshot.
*Owner action, still open. install-tasks now registers five jobs, including
the daily doctor and the weekly review, and the Windows tasks run on
battery (2026-10-22).*

2.5 **Real Health Connect export** — LAST, after 1.4 gives it an undo.
Follow the checklist in `docs/health-connect.md`; add the missing check:
`SELECT DISTINCT origin_app` per metric (two apps writing steps silently
doubles dashboard SUMs). Write findings back into the doc. *Check done
2026-10-22: the dry run lists origin apps, doctor warns `health_multi_origin`,
and the checklist has the per-origin query. The real export is still open.*

---

## Phase 3 — Correctness and trust (parallel with Phase 2)

3.1 **Contract/CLI gaps** (audit-verified): validate the `extraction` block
in `contract.py` (currently only in the CLI, so `--dry-run` lies); enforce or
re-document the same-day rule on `entries[].uuid` (doc and validator
disagree); add `admin discard-run` (doctor names a command that doesn't
exist); fix or drop the half-wired `transcribe --date`; duplicate detection
needs a corroborating signal ("Called Mom"/"Called Tom" = 0.900 similarity,
false positive) and the incumbent's body in the diff item. *Done 2026-10-22
(a79020c). The extraction block is validated in contract.py. The uuid
same-day rule is enforced (decision #5). `admin discard-run` exists.
`transcribe --date` works and requires `--file`. Possible duplicates need a
corroborating signal and show the incumbent's body, kind, categories and
entities.*

3.2 **Tests that would have caught what shipped**: export coverage asserted
against `sqlite_master` (today it audits its own manifest); dashboard chart
queries executed in their `[[ filtered ]]` form (3 charts never run in CI);
v1-snapshot restore through the CLI landing at v4; `migrate` CLI test.
*Done 2026-10-22 (4f1a10c, 8f2f849): dashboard queries run in their
filtered forms (filtered, from-only, blank From) and end to end through an
in-process Datasette. The imports-restore gap is closed too
(`imports restore`).*

3.3 **Backups you can trust**: `verify-restore.ps1` gets a lower bound
(today an empty restored DB prints OK) and compares all tables from
sqlite_master, not 3 hardcoded; a scheduled doctor run against the LIVE db
(the only scheduled one audits the scratch copy); split local vs cloud
backup-age checks. *Done 2026-10-22 (4f1a10c). `lifecrm verify-backup` is
shared by verify-restore.ps1/.sh: it runs doctor on the restored copy and
puts a lower bound on every table. Doctor runs daily against live.
`backup_age` covers local and `backup_age_cloud` the cloud copy
(`cloud_backups_dir`).*

3.4 **Audit `recall.py` + `llm.py`** — the only modules never reviewed.
One pass: correctness of window parsing, SQL injection surface of the
retrieval layer, citation-check bypasses, behavior on a v4 database.
Extend `ask` retrieval to `observations` + `observations_fts` (cited as O#),
so chat history becomes askable — this is the first visible payoff of the
observed tier. Retrieval stays deterministic and read-only. *Done
2026-10-22 (fcb9ebe). After the audit fixes, O# evidence covers the
observed tier. The citation check is per sentence, and an answer that fails
it is replaced by the deterministic one (`model_rejected`). Evidence is
fenced as data, and model requests never use a proxy.*

---

## Phase 4 — Connectors, one at a time (the long middle)

Pattern is fixed: pure-reader `scan()` in `lifecrm/connectors/<name>.py`,
register in `base.registry()`, fixtures + tests, doc page, dry-run first on
real data. Each is its own commit. Effort-adjusted order:

4.1 **Calendar** (`gcal`/ICS). Trivial parse (ICS or Google Takeout), huge
entity yield: every attendee is an `email` handle, every location a
candidate place. Streams: `calendar_event`. This is the connector that
bootstraps the people graph. *Built 2026-10-22 (e5175f0): `calendar` takes
.ics files, folders, Google Takeout and `<you>.ical.zip`. The RFC 5545
reader is stdlib only. Repeats expand to today + 90 days. It mirrors per
calendar or account. See docs/calendar.md; unverified against real exports.*

4.2 **Notes/docs** (Apple Notes export, markdown folders, Google Keep
Takeout). Stream `note`; `last_modified_at` from file mtime; body = text,
title = filename/heading. *Built 2026-10-22 (470cb32): `notes` reads .md/.txt
folders, a Keep Takeout and the Mac's NoteStore.sqlite (through
`_protobuf.py`). It mirrors per account. Still to check on real exports:
Keep's field names and file naming for untitled notes and name clashes,
Apple Notes columns on macOS 13–15, and the Full Disk Access message
(docs/notes.md).*

4.3 **Finance** (bank/card CSV export first — free and universal; Actual
Budget's SQLite if adopted). Stream `txn`: `value`+`unit='usd'`, merchant in
`title`, `counterparty` handles (`card` kind exists for this). NO
auto-categorization — that is a derived layer. *Built 2026-10-22 (7bc178f):
`finance` has CSV presets for eight banks, a `[connectors.finance]` column
mapping and OFX/QFX/QBO. Values are signed (negative = money out), the unit
is the ISO currency, and the only handles are `card` handles. It remembers
and never mirrors, because statements are windows. Unverified against the
owner's real downloads; docs/finance.md ends with the checklist.*

4.4 **Texts — the crown jewel and the worst acquisition.** *Built
2026-09-24, ahead of the gate, at the owner's request: `ios_messages`
connector + `connectors/typedstream.py`; see docs/texts.md. Unproven until a
real backup — its checklist is at the end of that doc.* iPhone has no
API. Path: Apple Devices app makes a local unencrypted backup on Windows →
parse `sms.db` (Manifest.db → hashed file names → Messages SQLite) → streams
`imessage`/`sms`, `phone` handles per participant, group chats =
multiple participants. Decide deletion semantics before first import
(provisional: observations stay when messages are deleted — the archive
remembers). Budget real time for this one; it is a project, not a task.
*Extended 2026-10-22 to both setups. `mac_messages` reads the Mac's own
`chat.db` under its own mirroring scope, and `ios_calls` reads call history
from a Mac or a backup (5b94184). `android_sms` reads SMS Backup & Restore
XML and mirrors only a type="full" file whose count matches (3df84a7). The
first-real-import checklists in docs/texts.md, docs/calls.md and
docs/android-texts.md are still open until real hardware.*

4.5 **Photos — metadata only.** EXIF timestamp + GPS + camera into
`value_json`, path into `media_path`, never the pixels. Answers "the lake in
Slovenia 4 years ago" almost for free. Reverse-geocoding = derived layer,
not import time. *Built 2026-10-22 (d454644): `photos` reads a folder, a
Google Photos Takeout or a Mac Photos library. Only the library mirrors.
Decided while building: Google Photos face labels stay in
value_json.people, never handles. Videos are included as kind 'video'.
There is no `stale_after_days`, because a month without photos is not a
broken pipeline.*

4.6 **When wanted, same pattern**: browser history (browserexport), Google
Takeout location, ActivityWatch screen time, WhatsApp export. *Built
2026-10-22. `browser` reads Chrome/Edge/Brave/Safari history, and
`location` reads Google Maps Timeline visits (bb87b13). Both remember.
`whatsapp` reads "Export chat" files and remembers too, because an export
is one capped chat (3ab716a). Contacts came alongside: `contacts` reads
vCard, Mac Contacts and iPhone address books as contact cards that feed
`handles list --suggest` (5ab1678). ActivityWatch screen time is not
built.*

**After the first two backfills:** verify `status`/streak still count days
with *entries*, not days that merely exist (backfill creates thousands of
empty `days` rows); check dashboard queries stay fast; add
`observations` day-counts line to `lifecrm status`. *2026-10-22: the streak
counts days with active entries (`cli._streak`). The slowest dashboard
cards, people and top people, took 0.25–0.65 s on 100k synthetic messages,
and `dashboard` now passes `sql_time_limit_ms 5000`. A larger history
should use the date filters. The `status` observations line is not built.*

---

## Phase 5 — Derived layers (each gated on data thresholds)

5.1 **Handle-review ergonomics** (after texts land): `handles list` gains
`--suggest` using display_name ↔ existing entity/alias matches — suggestions
only, confirmation stays manual. Gate: >50 unresolved handles. *Built
2026-09-24 with 4.4: `entities.match` gained order-free word matching (plus
accent folding), and texts supply display names from the phone's own address
book.*

5.2 **`/review-week` skill.** Reads last 7 days (entries, closed/open loops,
day summaries, health trends, observation counts) → draft brief through the
existing staging→review→apply gate. Gate: 4 consecutive weeks of real
logging. *Built 2026-10-22 (24a93c4), ahead of the gate, at the owner's
request. `lifecrm review` builds a read-only, model-free evidence pack (D/E/M/H/O
ids, prior-period deltas). The /review-week skill writes the cited brief,
and a kept brief is a `reflection` entry through the normal gate. The
weekly job (scripts/review.ps1 / review.sh, registered Mondays 07:30 by
install-tasks, log data/review.log) runs `review --lens all --write-only`.
See docs/review.md.*

5.3 **Richer `ask` / `/recall`.** Cross-tier questions ("when did I last
talk to X" = mentions + observation participants), temporal entity answers.
recall_log's unanswered rows are the backlog that drives this. Gate: ~3
months of data. *Built 2026-10-22 (fcb9ebe). Person questions cover last
contact across texts, calls, calendar and journal mentions, with contact
cards and facts. Also built: spend at a merchant, open tasks, and
relationships cited through the entry that stated them. Not built:
comparison questions ("this month vs last"), docs/ask.md "Limits".*

5.4 **Dashboards v2.** Fix the 3 empty-state cards now (cheap); new views
only when you can name three questions the current 16 cards don't answer.
*Built 2026-10-22 (8f2f849). The three empty-state cards are fixed. The
new observed and people dashboards are built. Day charts moved to utc time
units and newest-first ordering.*

5.5 **Entity↔entity relationships** (schema-level: `entity_links`,
contract support, doctor checks — F2-sized). Gate: 50+ entities with repeat
mentions and an actual "who does X know" need. *Built 2026-10-22 (schema
v5, d36d35b): entity_links + entity_link_types, contract v2 optional
`entity_links`, `entity_link` diff items, doctor checks, export,
`entities link ...`.*

5.6 **Advisors** (health/founder/finance lenses): scheduled reviews that
produce draft briefs through the review gate — /review-week generalized,
never always-on daemons, interpretations always regenerable + cited.
*Built 2026-10-22 (24a93c4). Lenses are health, career, relationships,
finance and all, and the /advise skill writes the brief. Suggestions are
framed as questions or small experiments, never as medical, legal, tax or
investment advice.*

5.7 **Obsidian mirror** — read-only extension of the markdown export, only
after the export has actually been read a few times. Bidirectional = a
second write path = violates the architecture; requires an explicit
decision to even consider. *Built 2026-10-22 (c60635c) as a read-only
extension of the markdown export (`export --format obsidian`). It owns its
notes through a marker and a manifest, it is deterministic, and it is
documented in docs/obsidian.md. Bidirectional sync remains forbidden.*

5.8 **Voice loop** (last, per roadmap): openWakeWord → faster-whisper →
`ask --json` → Piper TTS. Reads + draft creation only; never applies.
*Built 2026-10-22 (8b0e02b): `lifecrm voice`. It has push-to-talk
(sounddevice) and `--wake` (openWakeWord "hey jarvis", onnx), with
faster-whisper `[voice] model=small`. Questions go through recall.answer
(read-only, not logged). Notes go into inbox_text_dir under the Quick Note
name. TTS uses System.Speech, say, espeak or spd-say, or optionally Piper.
A model answer with invented or missing citations is replaced by the
deterministic one before it is spoken. See docs/voice.md.*

5.9 **Not in the original order, built 2026-10-22.**
- The read-only web UI `lifecrm serve` (0594347; docs/web.md).
- The Apple Health importer behind `health import` (b76ff7c).
- The Android capture fixes (903d605). Calendar-date file names now get the
  rollover, and `.3ga/.3gp/.webm/.caf` are read.
- The Mac port (0e9b7a8). It has bash scripts, launchd jobs,
  `configure`/`where` and iCloud placeholder handling.
- `lifecrm demo` for both setups (9d5d27d, 7c214dd).
- Health Connect sleep is dated by the wake day, like Apple Health
  (0d24569).

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
   **Decided 2026-09-24: not required.** Personal use on a personal machine,
   not a released product. Revisit only if that changes.
2. Deletion semantics per connector (provisional: archive remembers).
   **Decided 2026-09-24 for texts: the archive mirrors the phone** — deleted
   and unsent messages do not stay. Implemented as a shared capability in
   domain/observations.py (a connector declares `present_ids`; what its
   source no longer holds is removed, scoped per account, with a snapshot
   first, a mass-removal guard, and cited provenance hidden rather than
   deleted). **AI chats mirror too** (same day): a conversation deleted in the
   app is removed on the next import, scoped to its vendor and account. Future
   connectors: decide per connector when each lands. *Recorded 2026-10-22,
   per connector as each landed. These mirror when the source names an
   account: `mac_messages` (its own scope, so a phone backup and the Mac
   never remove each other's history), `android_sms` texts
   (only from a type="full" file whose count matches), `calendar` (a
   one-calendar export only when its name is an address or listed in
   `[connectors.calendar] mirror_calendars` — two calendars can share a
   name), `contacts` (a vCard only with `[connectors.contacts] account`),
   `notes` (Keep only with a known account) and a Mac Photos library. These
   remember: `whatsapp` (an export is one capped chat), `finance`
   (statements are windows), `android_sms` calls (Android's call log ages
   old calls out, so a full backup is not the whole history), `ios_calls`
   (the same reason: iOS keeps only its newest calls), `location`,
   `browser`, and photo folders and Takeouts.*
3. Export destination for `lifecrm export` (in-repo ignored dir vs outside).
   *Still open. `/archive/`, `/archive-data/` and `/exports/` are gitignored
   (1.7), so either choice is safe.*
4. Whether `health import` folds into `observe health` once a second
   metric-shaped connector exists. **Answered 2026-10-22, in practice: no.**
   The second metric-shaped source, Apple Health, became a second reader
   behind `health import`, picked by `health_import.reader_for` from the
   file itself. It is not under `lifecrm/connectors/`, so it never enters
   the observe registry or doctor's connector loop, and `health_records`
   stays separate (standing decision).
5. `entries[].uuid` cross-day: forbid (doc) or surface target_date (code).
   **Decided 2026-10-21: forbid.** `entries[].uuid` re-extracts an ACTIVE
   entry of the draft's own journal day, and each uuid at most once.
   `contract.validate_draft` refuses a cross-day, retracted/superseded or
   doubly-targeted uuid. Its error points at `entry_updates` (cross-day
   closures) or `lifecrm entry edit` (any other cross-day change), and
   `reconcile.build_diff` raises if one ever gets past the contract. Reason:
   a cross-day uuid was auto-edited without a decision, outside the day
   fingerprint that guards the review, and a retracted target failed the
   whole apply.

## Still open (2026-10-22)

- **Real devices and real exports.** Every connector and phone-side step
  has only met synthetic files in the real formats. These checklists are
  the work:
  - setups: windows-android.md, android.md, iphone.md and mac.md (launchd,
    Full Disk Access, iCloud latency, `brctl download`, whisper on Apple
    Silicon, Intel wheels);
  - imports: texts.md, calls.md, android-texts.md, health-connect.md,
    apple-health.md, notes.md, photos.md and finance.md, plus the
    limitations in contacts.md, calendar.md, location.md, browser.md and
    whatsapp.md;
  - front ends: voice.md (microphone, wake word, speakers) and web.md (a
    real phone over the LAN);
  - a real Ollama model through `ask`'s per-sentence citation check.
  2.1, 2.2, 2.3 and 2.5 are this same work for the first real day, the
  first real AI chat export, phone audio and Health Connect.
- **Not built:** ActivityWatch screen time (4.6), Samsung `.qta`
  recordings (not in `AUDIO_EXTS`; needs a real file first), comparison
  questions ("this month vs last", 5.3), and the observations line in
  `status`.
- **Owner actions:**
  - `gh auth login` → private repo → push. That also runs
    `.github/workflows/ci.yml` on Windows and macOS for the first time.
  - The LifeCRM → SecondBrain folder rename.
  - Registering the scheduled tasks (`install-tasks.ps1` / `.sh`).
  - Migrating the real database to schema v5 with `lifecrm migrate`, which
    snapshots first.

## Report back (every phase)

What shipped; what was overruled and why; what remains unproven until real
data or a real phone; test count; anything scoped down — plainly, never
silently.

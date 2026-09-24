# Life CRM

Personal life database: voice notes + dictated quick notes -> local whisper /
direct text intake -> reviewed extraction (via /log) -> SQLite with full
provenance, closable loops, and entry links.

## Rules

- **All writes go through `python -m lifecrm ...`** (which routes through
  `lifecrm/domain/`). Never modify `data\lifecrm.db` with direct SQL writes,
  never edit files under `data\` by hand.
- Ad-hoc reads: `python -m lifecrm sql "SELECT ..." --json` (read-only
  connection; writes are blocked at the connection level).
- The extraction contract is `docs/contract.md` (contract v2, schema v4). A
  day with several sources extracts as ONE draft. Drafts go to
  `data\staging\`, then `lifecrm ingest` -> review -> `lifecrm apply`.
- Never apply a diff the user hasn't reviewed. Never guess on ambiguous
  entity matches. Cross-day closures (`entry_updates`) and retract proposals
  are always explicit user decisions.
- `data\`, `backups\`, and `config.toml` are private and gitignored — never
  commit them, never paste transcript contents into commit messages.
- Schema changes are numbered migrations in `lifecrm/migrations/`
  (`0002_*.sql`, consecutive, no gaps) applied atomically by the runner in
  `lifecrm/db.py`; `lifecrm migrate` runs them with a pre-migration snapshot.
- This project uses interactive agent sessions only — no metered API calls,
  no headless metered workflows.

## Commands

- Run everything from the project root with `.venv\Scripts\python.exe`
  (Python 3.12 venv; system 3.14 lacks ctranslate2 wheels). Set `PYTHONUTF8=1`.
- Tests: `.venv\Scripts\python.exe -m pytest tests -q` (pass
  `--basetemp=<scratch dir>` if the default temp root is blocked).
- Full surface (`python -m lifecrm --help`): intake, transcribe, import,
  ingest, apply, status, doctor, export, backup, restore, migrate, sql,
  health (import — Health Connect zips, dry-run by default;
  retract/restore — undo an import by source file),
  dashboard (Datasette over a snapshot — never the live db),
  ask (cited answers; deterministic retrieval, optional local model),
  observe/connectors/imports/handles (observed tier — bulk imports that never
  pass the review gate; dry-run by default; connectors ai_chat, ios_messages;
  `handles list --suggest` offers entity candidates, never links),
  entities (match/list/show/merge/alias-add), entry (show/edit/retract/
  restore/merge/close/reopen), link (add/list/rm), categories, kinds, admin.

## Layout

- `lifecrm/domain/` — the single validated write layer (audit + revisions).
- `lifecrm/contract.py` — deterministic draft validation, single-line errors.
- `lifecrm/domain/reconcile.py` — draft(s) vs existing day -> reviewable
  diff; confirmed records are never silently overwritten.
- `lifecrm/doctor.py` — invariant verification (`lifecrm doctor`).
- `lifecrm/export.py` — deterministic jsonl/markdown archives.
- `lifecrm/health_connect.py` + `lifecrm/domain/health.py` — Health Connect
  export import into `health_records` (deterministic, external-id dedupe,
  newer-wins updates, conflicts surfaced; dry-run by default).
- `lifecrm/domain/observations.py` + `lifecrm/connectors/` — the OBSERVED
  tier (schema v4). Two tiers, one rule: `entries`/`measurements`/`mentions`
  are user-confirmed; `observations`/`health_records` are bulk-imported, never
  reviewed, and never asserted as fact without citing the row. **An importer
  may not write entries, measurements, mentions or entity_aliases.** Connectors
  are pure readers (`connectors/base.py` contract); dedupe, newer-wins
  classification, conflict surfacing, transactional apply, retraction and
  mirroring live once in `domain/observations.py` (a connector that declares
  `present_ids` has what its source no longer holds removed — texts do; the
  owner chose "mirror the phone" over "remember", 2026-09-24). Importers emit
  *handles*, never names —
  linking one handle attributes every observation that referenced it, with no
  backfill pass. See `docs/observed-tier.md`, `docs/ai-chat.md`, and
  `docs/texts.md` — iPhone backup -> iMessage/SMS/RCS, with
  `connectors/typedstream.py` decoding `attributedBody` (written from the
  format; crabstep/imessage-exporter are GPL-3.0 — never copy their code).
- `lifecrm/recall.py` + `lifecrm/llm.py` — `lifecrm ask`: rules parse the
  question (window + metrics), SQL answers it, every fact carries a citable
  id. The local model (Ollama over stdlib HTTP, optional) may only PHRASE
  that evidence — never supply facts — and its citations are verified
  against the bundle. Retrieval must stay read-only and model-free.
- `dashboards/dashboards.yml` — dashboards-as-code (datasette-dashboards
  0.8.x: charts MUST be explicit vega-lite specs — the bar/line/pie
  shorthands in the plugin's master README don't exist in 0.8.x and fail
  silently; tests enforce supported libraries and run every chart query).
- `docs/iphone.md` + `docs/android.md` — standalone phone capture guides.
- Deferred (do not build unless asked): live Obsidian mirror,
  entity<->entity relationships, /review-week, voice front-end (would call
  `ask --json`, not reimplement retrieval).

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
- The extraction contract is `docs/contract.md` (contract v2, schema v2). A
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
- This project uses interactive Claude Code only — no Anthropic API calls,
  no headless metered workflows.

## Commands

- Run everything from the project root with `.venv\Scripts\python.exe`
  (Python 3.12 venv; system 3.14 lacks ctranslate2 wheels). Set `PYTHONUTF8=1`.
- Tests: `.venv\Scripts\python.exe -m pytest tests -q` (pass
  `--basetemp=<scratch dir>` if the default temp root is blocked).
- Full surface (`python -m lifecrm --help`): intake, transcribe, import,
  ingest, apply, status, doctor, export, backup, restore, migrate, sql,
  health (import — Health Connect zips, dry-run by default),
  dashboard (Datasette over a snapshot — never the live db),
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
- `dashboards/dashboards.yml` — dashboards-as-code (datasette-dashboards
  0.8.x: charts MUST be explicit vega-lite specs — the bar/line/pie
  shorthands in the plugin's master README don't exist in 0.8.x and fail
  silently; tests enforce supported libraries and run every chart query).
- `docs/iphone.md` + `docs/android.md` — standalone phone capture guides.
- Deferred (do not build unless asked): live Obsidian mirror,
  entity<->entity relationships, /recall, /review-week.

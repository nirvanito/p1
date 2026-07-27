# Life CRM

Personal life database: nightly voice notes -> local whisper transcription ->
reviewed extraction (via /log) -> SQLite with full provenance.

## Rules

- **All writes go through `python -m lifecrm ...`** (which routes through
  `lifecrm/domain/`). Never modify `data\lifecrm.db` with direct SQL writes,
  never edit files under `data\` by hand.
- Ad-hoc reads: `python -m lifecrm sql "SELECT ..." --json` (read-only
  connection; writes are blocked at the connection level).
- The extraction contract is `docs/contract.md` (contract_version 1). Drafts
  go to `data\staging\`, then `lifecrm ingest` -> review -> `lifecrm apply`.
- Never apply a diff the user hasn't reviewed. Never guess on ambiguous
  entity matches.
- `data\`, `backups\`, and `config.toml` are private and gitignored — never
  commit them, never paste transcript contents into commit messages.
- This project uses interactive Claude Code only — no Anthropic API calls,
  no headless metered workflows.

## Commands

- Run everything from the project root with `.venv\Scripts\python.exe`
  (Python 3.12 venv; system 3.14 lacks ctranslate2 wheels). Set `PYTHONUTF8=1`.
- Tests: `.venv\Scripts\python.exe -m pytest tests -q` (pass
  `--basetemp=<scratch dir>` if the default temp root is blocked).
- `python -m lifecrm --help` lists the full surface: transcribe, import,
  ingest, apply, status, entities, entry, categories, kinds, sql, backup,
  restore, migrate, admin.

## Layout

- `lifecrm/domain/` — the single validated write layer (audit + revisions).
- `lifecrm/contract.py` — deterministic draft validation, single-line errors.
- `lifecrm/domain/reconcile.py` — draft vs existing day -> reviewable diff;
  confirmed records are never silently overwritten.
- Deferred (do not build unless asked): dashboard, Obsidian vault export,
  JSONL exports, entity relationships, /recall, /review-week.

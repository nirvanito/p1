# Privacy, encryption, and data portability

This system stores an intimate record of your life — and, since the observed
tier landed, other people's words too: imported text messages and AI-chat
transcripts sit in the same database as the journal. Treat the data folders
accordingly.

## Where your data lives

| Location | Contents | Private? |
|---|---|---|
| `data\lifecrm.db` | The database: entries, entities, transcripts, audit log — and the observed tier (texts, AI chats, call logs, every imported record) and health records | Yes — gitignored, local |
| `data\transcripts\`, `data\staging\` | Transcript text mirrors; drafts and diffs awaiting review (full journal text) | Yes — gitignored, local |
| `data\dashboard\lifecrm.db` | **A full copy of the database**, taken by `lifecrm dashboard` and served by Datasette. Rewritten on each run, never deleted | Yes — gitignored (`/data/`); delete it when you are done |
| `data\web\lifecrm.db` | The same kind of full copy, for the web view | Yes — gitignored (`/data/`); delete it when you are done |
| `backups\` | Local DB snapshots (`lifecrm-*.db`, `pre-migration-*.db`, `pre-restore-*.db`) | Yes — gitignored, local |
| `archive\`, `archive-data\`, `exports\` | `lifecrm export` output (markdown / jsonl) — the whole journal in plain text | Yes — gitignored; keep them out of synced folders unless you mean to |
| Phone/health export zips (`*.zip`), iPhone backups | Health Connect exports, unencrypted iPhone backups (every message on the phone) | Yes — `*.zip` is gitignored; iPhone backups live outside the project (`MobileSync\Backup`) |
| `OneDrive\VoiceJournal\inbox` + `archive\` (Windows) | Your voice recordings and dictated notes | **Cloud-synced** |
| `iCloud Drive/VoiceJournal/inbox` + `archive/` (Mac) | Your voice recordings and dictated notes | **Cloud-synced** |
| `OneDrive\VoiceJournal\backups\` or its iCloud equivalent | DB snapshots (full journal content, texts included) | **Cloud-synced** |
| The project folder itself (code, tests, docs) | No personal data | Committed to local git |

**This system is not entirely local.** With cloud sync on, your recordings and
database snapshots are stored on Microsoft's (OneDrive) or Apple's (iCloud)
servers under your account. That is the trade-off for phone auto-upload and
off-machine backup. If you are not comfortable with it:

- Point `inbox_dir`/`archive_dir`/`cloud_backups_dir` in `config.toml` at
  local folders and move audio over USB (`onedrive_backups_dir` is still
  accepted as the old name), or
- keep the cloud inbox but set `cloud_backups_dir` to a local path so
  snapshots never leave the machine (then arrange your own off-machine
  backup — a copy on an encrypted USB drive, for example).

On a Mac, do not put the project itself under `~/Documents` or `~/Desktop`
when iCloud "Desktop & Documents" sync is on: `data/lifecrm.db` would then be
cloud-synced (and evictable) behind your back. Keep it at `~/LifeCRM`.

## Encryption

**Decided 2026-09-24: encryption at rest is not required** (next-brief open
decision #1). This is personal use on a personal machine, not a released
product; revisit only if that changes. The database is a plain SQLite file.

What protects it instead is the machine:

- **Windows:** turn on BitLocker / Device encryption (Settings > Privacy &
  security > Device encryption).
- **Mac:** turn on FileVault (System Settings > Privacy & Security >
  FileVault).

Either way a stolen laptop does not mean a readable journal. Turn on
two-factor authentication for the Microsoft or Apple account — it guards the
cloud copies. On iCloud, Advanced Data Protection additionally end-to-end
encrypts iCloud Drive (and so the inbox and any snapshots kept there).

iPhone backups made for the texts connector must be **unencrypted** for the
importer to read them (see `docs/texts.md`); that backup is then as sensitive
as the phone itself, and FileVault/BitLocker is what protects it on disk.

## Serving the dashboard on your network

`lifecrm dashboard` binds to `127.0.0.1` by default: only this machine can
open it. `--host 0.0.0.0` makes it reachable from your phone on the same
Wi-Fi — and from **anyone else on that network**: there is no login, and the
snapshot behind it is the whole database. The command prints a one-line
warning and the LAN address to open on the phone when you do this. Use it on
a network you trust (home Wi-Fi, not a café), and stop it (Ctrl+C) when done.

## What git sees

The git repository holds code only. `.gitignore` excludes `data\` (database,
dashboard/web snapshots, staging, transcripts), `backups\`, `archive\`,
`archive-data\`, `exports\`, `demo\`, `*.db*`, `*.jsonl`, `*.zip`,
`config.toml` and `.claude/settings.local.json`. A test
(`tests/test_integrity_privacy.py`) asks `git check-ignore` about every one of
those paths — without creating any file — and fails the suite if one ever
becomes visible; it also checks that source files (`lifecrm/demo/`,
`config.example.toml`, ...) are NOT ignored. Never commit transcripts or
paste journal content into commit messages.

## Retention (optional)

Nothing is deleted automatically except old snapshots. If you want limits:

- Audio: delete old files from the inbox's `archive` folder — transcripts
  and entries survive in the database (their `sources.path` just goes stale).
- Transcripts: keeping them is strongly recommended (they are the provenance
  layer), but `data\transcripts\` text files are a convenience mirror — the
  canonical text is in the database.
- Snapshots: `lifecrm backup` applies tiered retention to `lifecrm-*.db` in
  each location — the newest per day, then 14 daily, 12 weekly, 24 monthly,
  and one per year kept indefinitely. `pre-migration-*` and `pre-restore-*`
  snapshots are never pruned; delete them by hand once you trust the result.
- Texts and AI chats mirror their source (decision #2): a message or
  conversation deleted on the phone or in the app is removed from the
  database on the next import (snapshot first).

## Moving everything to another machine

1. Copy the project folder (or `git clone` your repo) to the new machine.
2. Run the setup script (`scripts\setup.ps1` on Windows) — Python 3.12 +
   venv + dependencies.
3. Copy the entire `data\` folder from the old machine (or restore from a
   snapshot — see `docs\restore.md`).
4. Update paths in `config.toml` for the new machine's OneDrive or iCloud
   Drive location (`~` is expanded).
5. Verify: `python -m lifecrm status` and `python -m lifecrm doctor`.

Restoring from a snapshot without the original machine is covered in
`docs\restore.md`.

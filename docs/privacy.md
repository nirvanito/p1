# Privacy, encryption, and data portability

This system stores an intimate record of your life. Treat the data folders
accordingly.

## Where your data lives

| Location | Contents | Private? |
|---|---|---|
| `data\` | SQLite database (entries, entities, transcripts, audit), transcript text files, staging drafts | Yes — gitignored, local |
| `backups\` | Local DB snapshots | Yes — gitignored, local |
| `OneDrive\VoiceJournal\inbox` + `archive\` | Your voice recordings | **Cloud-synced** |
| `OneDrive\VoiceJournal\backups\` | DB snapshots (full journal content) | **Cloud-synced** |
| The project folder itself (code, tests, docs) | No personal data | Committed to local git |

**This system is not entirely local.** With OneDrive sync enabled, your audio
recordings and database snapshots are stored on Microsoft's servers under your
Microsoft account. That is the trade-off for phone auto-upload and off-machine
backup. If you are not comfortable with it:

- Point `inbox_dir`/`archive_dir`/`onedrive_backups_dir` in `config.toml` at
  local folders and move audio over USB, or
- keep OneDrive for the inbox but set `onedrive_backups_dir` to a local path
  so snapshots never leave the machine (then arrange your own off-machine
  backup — a copy on an encrypted USB drive, for example).

## Device encryption

Enable BitLocker (Settings > Privacy & security > Device encryption) so a
stolen laptop does not mean a readable journal. Check your OneDrive account
has two-factor authentication enabled — it guards the cloud copy.

## What git sees

The git repository holds code only. `.gitignore` excludes `data\`, `backups\`,
`*.db*`, `*.jsonl`, and `config.toml`, and a test
(`tests/test_cli.py::test_gitignore_keeps_private_data_out_of_git`) fails the
suite if that ever breaks. Never commit transcripts or paste journal content
into commit messages.

## Retention (optional)

Nothing is deleted automatically. If you want limits:

- Audio: delete old files from `OneDrive\VoiceJournal\archive\` — transcripts
  and entries survive in the database (their `sources.path` just goes stale).
- Transcripts: keeping them is strongly recommended (they are the provenance
  layer), but `data\transcripts\` text files are a convenience mirror — the
  canonical text is in the database.
- Snapshots: `lifecrm backup` already prunes to the newest 8 per location.

## Moving everything to another machine

1. Copy the project folder (or `git clone` your repo) to the new machine.
2. Run `scripts\setup.ps1` (installs Python 3.12 + venv + deps).
3. Copy the entire `data\` folder from the old machine (or restore from a
   snapshot — see `docs\restore.md`).
4. Update paths in `config.toml` for the new machine's OneDrive location.
5. Verify: `python -m lifecrm status` and `python -m lifecrm sql "SELECT COUNT(*) FROM entries"`.

Restoring from a snapshot without the original machine is covered in
`docs\restore.md`.

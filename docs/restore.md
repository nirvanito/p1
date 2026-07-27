# Restoring from a backup

Snapshots are complete SQLite databases produced by `lifecrm backup` (SQLite
backup API — consistent even though the live DB uses WAL). They live in:

- `backups\lifecrm-YYYYMMDD-HHMMSS.db` (local)
- `OneDrive\VoiceJournal\backups\lifecrm-YYYYMMDD-HHMMSS.db` (cloud)

## Same machine

```
python -m lifecrm restore                # list available snapshots
python -m lifecrm restore <snapshot.db>  # verify + restore
```

`restore` refuses corrupt or non-lifecrm files, snapshots the current database
to `backups\pre-restore-*.db` first (nothing is ever lost by restoring), swaps
the file in, and applies any pending schema migrations.

## A machine that has never run this project

You need two things: this code and one snapshot file. The original application
directory is NOT required.

1. Get the code: `git clone` your repo, or copy the project folder minus
   `data\` and `backups\`, onto the new machine.
2. Run `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`
   (installs Python 3.12, venv, dependencies; creates an empty database).
3. Edit `config.toml` — fix the OneDrive paths for this machine.
4. Restore the snapshot:
   `\.venv\Scripts\python.exe -m lifecrm restore <path-to-snapshot.db>`
5. Verify:
   ```
   .venv\Scripts\python.exe -m lifecrm status
   .venv\Scripts\python.exe -m lifecrm sql "SELECT COUNT(*) AS entries FROM entries"
   ```

Notes:

- The snapshot contains everything structured: entries, entities, transcripts,
  segments, provenance, audit history, revisions. Audio files are separate —
  they live in `OneDrive\VoiceJournal\archive\` and sync down with OneDrive.
- The `data\transcripts\*.txt` files are a convenience mirror; the canonical
  transcript text is inside the database and survives in any snapshot.
- If the snapshot came from an older schema version, `restore` migrates it
  automatically (taking a pre-migration snapshot first).

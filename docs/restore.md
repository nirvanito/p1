# Restoring from a backup

Snapshots are complete SQLite databases produced by `lifecrm backup` (SQLite
backup API — consistent even though the live DB uses WAL). File names carry
the UTC time the snapshot was taken. They live in:

- `backups/lifecrm-YYYYMMDD-HHMMSS.db` — local (or wherever
  `backups_dir` points)
- the cloud copy, when `[paths] cloud_backups_dir` is set in `config.toml`
  (older configs call it `onedrive_backups_dir`; both names work):
  - Windows + OneDrive: `C:\Users\<you>\OneDrive\VoiceJournal\backups\`
  - Mac + iCloud Drive:
    `~/Library/Mobile Documents/com~apple~CloudDocs/VoiceJournal/backups/`

Also in `backups/`: `pre-restore-*.db` (the database as it was before a
restore), `pre-migration-*.db` (before a schema upgrade), and
`pre-removal-*.db`, which `observe` takes before a mirroring import removes
anything (deleted on the phone, in the app, in the address book). That one is
the undo for the removal — `observe` prints it as `lifecrm restore
"<snapshot>"`, and `imports retract` names it too, since retracting an import
cannot bring back what it removed. `restore` with no argument lists only the
`lifecrm-*.db` snapshots; pass a `pre-*` file's path to restore it.
`backup` keeps every `pre-removal-*.db` from the 14 most recent days that
have one, whatever later backups that day do.

Commands below say `python`; use the project's own venv:
`.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on a Mac, with
`PYTHONUTF8=1` set.

## Proving a backup restores

```
python -m lifecrm verify-backup              # newest local snapshot
python -m lifecrm verify-backup <snap.db>    # any one — e.g. the cloud copy
python -m lifecrm verify-backup --json
```

It restores the snapshot into a scratch folder through the real `restore`
command, runs `doctor` on the result, and compares every table with the live
database — then deletes the scratch folder. The live database is only read.
A `FAIL` line and exit code 1 mean one of:

- **restore refused the snapshot** — corrupt, not a lifecrm database, or from
  newer code.
- **doctor on the restored copy** — the snapshot restores but breaks an
  invariant.
- **`<table>: restored copy has N row(s), but live had M stamped before the
  snapshot`** — rows that existed when the snapshot was taken are missing
  from it. Tables without a creation timestamp fail when live has rows and
  the copy has none.
- **`<table>: restored copy has MORE rows than live`** (entries, sources) —
  live lost rows since the snapshot (`admin purge-day`?) or the snapshot is
  from another database.

One false alarm is possible: a long import still running while the backup
ran stamps rows before the snapshot but commits them after. Re-run after the
next backup.

It runs on a schedule: `scripts\install-tasks.ps1` registers a weekly
`scripts\verify-restore.ps1` on Windows (log: `data\verify-restore.log`); on
a Mac the weekly launchd job runs `scripts/verify-restore.sh`. Both are thin
wrappers around `verify-backup` and exit with its code. A daily doctor run
on the live database (`scripts\doctor.ps1`, log: `data\doctor.log`) warns
when the newest local snapshot or the newest cloud copy is over a week old —
separately, so a dead cloud copy cannot hide behind a fresh local one.

## Same machine

```
python -m lifecrm restore                # list available snapshots
python -m lifecrm restore <snapshot.db>  # verify + restore
```

`restore` refuses corrupt or non-lifecrm files, snapshots the current database
to `backups/pre-restore-*.db` first (nothing is ever lost by restoring), swaps
the file in, and applies any pending schema migrations. Close anything that
has the database open first (a DB browser, the dashboard, a running
`observe`).

## A machine that has never run this project

You need two things: this code and one snapshot file. The original application
directory is NOT required, and a snapshot taken on Windows restores on a Mac
and the other way round — SQLite files are portable. (`sources.path` keeps
the original machine's file paths; they are provenance text, never reopened.)

### Windows

1. Get the code: `git clone` your repo, or copy the project folder minus
   `data\` and `backups\`, onto the new machine.
2. Run `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`
   (installs Python 3.12, venv, dependencies; creates an empty database).
3. Edit `config.toml` — fix the OneDrive paths for this machine.
4. Restore the snapshot:
   `.venv\Scripts\python.exe -m lifecrm restore <path-to-snapshot.db>`
5. Verify:
   ```
   .venv\Scripts\python.exe -m lifecrm verify-backup <path-to-snapshot.db>
   .venv\Scripts\python.exe -m lifecrm status
   .venv\Scripts\python.exe -m lifecrm sql "SELECT COUNT(*) AS entries FROM entries"
   ```
6. `powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1` to
   schedule intake, doctor, backup, verify-restore and review again.

### Mac

1. Get the code into a folder iCloud does not sync — `~/LifeCRM`, not
   `~/Documents` or `~/Desktop` when "Desktop & Documents Folders" is on: the
   live database must never be a cloud file.
2. Python 3.12 (macOS's own `python3` is too old):
   ```
   brew install python@3.12
   python3.12 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   cp config.example.toml config.toml
   ```
3. Edit `config.toml`. `~` works in paths, e.g.
   `cloud_backups_dir = '~/Library/Mobile Documents/com~apple~CloudDocs/VoiceJournal/backups'`.
4. If the snapshot is in iCloud Drive, make sure it is really on disk: in
   Finder, right-click it and choose **Download Now** (or copy it out of
   iCloud Drive first). A cloud placeholder is not a database.
5. Restore and verify:
   ```
   export PYTHONUTF8=1
   .venv/bin/python -m lifecrm restore <path-to-snapshot.db>
   .venv/bin/python -m lifecrm verify-backup <path-to-snapshot.db>
   .venv/bin/python -m lifecrm status
   ```

Notes:

- The snapshot contains everything structured: entries, entities, transcripts,
  segments, provenance, audit history, revisions, imported health records and
  the observed tier (texts, AI chats, ...). Audio files are separate — they
  live in the archive folder (`OneDrive\VoiceJournal\archive\` or the iCloud
  Drive equivalent) and sync down with it.
- The `data/transcripts/*.txt` files are a convenience mirror; the canonical
  transcript text is inside the database and survives in any snapshot.
- If the snapshot came from an older schema version, `restore` migrates it
  automatically (taking a pre-migration snapshot first).
- The cloud copy is the whole journal in someone else's storage — see
  `docs/privacy.md`.

## Reading an export with no code at all

`lifecrm export` produces archives that need nothing from this project:

**Markdown** (`--format markdown`): plain text. `2026/2026-07-27.md` is that
day — front-matter with date and mood/sleep numbers, then entries grouped by
category with their measurements, closure states, and links. `[[Name]]` marks
a person/place/thing; `_entities/<slug>.md` lists every day that entity
appears. Any text editor from any decade can read it; Obsidian renders the
links as a graph.

**Obsidian** (`--format obsidian`): the markdown export plus a digest of the
day's observed records, one note per person, place or thing, and loop and
index notes (`Loops.md`, `Index.md`). It is the same plain text, laid out
as a vault ([obsidian.md](obsidian.md)).

**JSONL** (`--format jsonl`): one file per database table, one JSON object
per line, cross-referenced by UUIDs and names rather than database ids.
`manifest.json` states the schema/contract version, export time, and
per-table row counts (verify your copy is complete by counting lines). The
key files and their join keys:

- `entries.jsonl` — the records; `uuid` is the identity everything else uses.
- `entry_categories.jsonl` — `entry_uuid` -> category name (+ `is_primary`).
- `mentions.jsonl` — `entry_uuid` <-> `entity_uuid` (see `entities.jsonl`).
- `measurements.jsonl` — owned by `entry_uuid` OR `journal_date`.
- `entry_sources.jsonl` — provenance: `entry_uuid` -> `source_uuid` with
  char offsets into `transcripts.jsonl` text (keyed `source_uuid` +
  `transcript_index`).
- `extraction_runs.jsonl` — each extraction run, with the sources it read
  listed in `source_uuids`.
- `entry_links.jsonl` — `from_uuid`/`to_uuid` thread structure.
- `entry_revisions.jsonl` + `audit_log.jsonl` — full correction history.

The imported tiers — records that came in by bulk import and were never
reviewed one by one, so they are what a source said, not confirmed fact:

- `health_records.jsonl` — sensor/app health data; `source_file` names the
  export it came from, `status: retracted` marks an undone import.
- `imports.jsonl` — one row per importer run: `connector`, `account`,
  `source_file`, `status`, `counts`. `status: retracted` marks a run undone
  with `lifecrm imports retract` (reversible with `lifecrm imports restore`);
  its observations keep their data with `status: retracted` too.
- `observations.jsonl` — texts, AI chats and other imported records;
  `import_uuid` -> `imports.jsonl`, and `stream` + `external_id` is the
  source system's own identity for the record.
- `observation_participants.jsonl` — `observation_uuid` <-> a handle
  (`handle_kind` + `handle_value`: a phone number, an email, an app account)
  with its role (sender, recipient, ...).
- `entity_handles.jsonl` — each handle -> `entity_uuid` (null: never
  resolved to a person), plus the name the source called it.
- `entry_observations.jsonl` — confirmed entries that cite an observation as
  their provenance: `entry_uuid` -> `observation_uuid`.

Ten lines of any scripting language — or a spreadsheet import — reconstructs
any view of it. Exports are deterministic, so two exports of the same data
are byte-identical and diff cleanly.

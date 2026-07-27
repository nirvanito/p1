---
name: log
description: Process today's voice journal into the life CRM — transcribe or import, extract to the contract, reconcile, review, and apply. Use when the user wants to log their day, process a voice note, or paste a journal transcript.
---

# /log — daily journal processing

Turn the newest transcript (or pasted text) into structured, confirmed records.
All commands run from the project root with the venv python:
`.venv\Scripts\python.exe -m lifecrm ...` (set `PYTHONUTF8=1`).

The data contract is `docs/contract.md`. You never write to the database
directly — only through `lifecrm ingest` + `lifecrm apply`.

## Step 1 — find the source

1. Run `python -m lifecrm transcribe` (picks up new audio from the OneDrive
   inbox; skip silently if it reports nothing).
2. Run `python -m lifecrm status --json`.
3. Choose the source:
   - `$ARGUMENTS` contains a date -> the unprocessed source for that date.
   - The user pasted transcript text -> save it to a temp file and run
     `python -m lifecrm import <file> --date <date>` (ask for the date only if
     it isn't today's entry; a note about "today" recorded before ~05:00 counts
     as the previous day — status output shows `today` already adjusted).
   - Otherwise -> the newest entry in `unprocessed_sources`.
   - Nothing unprocessed and nothing pasted -> tell the user and stop.

## Step 2 — read the transcript

Read the transcript text (path shown by import/transcribe under
`data\transcripts\YYYY\`, or `python -m lifecrm sql "SELECT text FROM transcripts WHERE source_id=<id>" --json`).

## Step 3 — draft the extraction

Draft a contract document (`docs/contract.md`, contract_version 1):

- One entry per distinct thing: meals, workouts, work items, social moments,
  errands, media, thoughts, ideas, decisions, plans.
- `kind` = what the record IS (event, observation, thought, idea, decision,
  task, plan, fact, reflection). `categories` = what it's ABOUT (primary +
  optional secondary). Future things go in `due_at`/`occurred_at` on a
  plan/task entry — never a future journal_date.
- Numbers become `measurements` (duration, distance, weight, reps, sets,
  calories, money, sleep_hours...). Day-level mood/energy/sleep go in
  `day.measurements`.
- `day.summary`: one or two sentences.
- `provenance` per entry: `{"char_start": N, "char_end": M}` covering the
  transcript passage that supports it (or `{"segments": [...]}` for audio).
- Anything uncertain goes in `ambiguities` and gets asked in Step 4.

**Entity resolution (mandatory, before writing the draft):** collect every
person/place/food/project/activity/media/organization/topic name, then run

    python -m lifecrm entities match "Name1" "Name2" ... --json

- `exact`/`alias` with ONE match -> `{"ref": "existing", "id": N, "role": ...}`.
- Multiple matches (ambiguous alias) or fuzzy `candidates` -> NEVER guess;
  ask the user in Step 4.
- `none` -> `{"ref": "new", "type": ..., "name": <canonical name>,
  "aliases": [<spoken variants>]}`. Prefer full names the user has used before
  ("Jake Reilly", not "Jake") as canonical; spoken variants become aliases.

## Step 4 — review (mandatory — never apply without it)

Write the draft to `data\staging\draft-<date>.json` and run:

    python -m lifecrm ingest data\staging\draft-<date>.json

Fix any validation errors (they are single-line and precise) and re-run.
Then present the resulting diff compactly:

- Headline: `4 food, 1 exercise, 2 work - mood 7 - sleep 6.5h`
- A table of new entries: kind, title, categories, entities, confidence.
- New entities separately: `NEW place "Movement Gym" (alias: the gym)`;
  matched ones as `"Jake" -> existing Jake Reilly (id 12)`.
- Every ambiguity and every diff item with `"accepted": null`
  (updates to confirmed entries, possible duplicates, retract proposals)
  as a direct question.

Ask for a single confirmation (y / corrections).

## Step 5 — apply

- Plain "y" -> `python -m lifecrm apply <diff-file>` (the diff path is printed
  by ingest).
- Corrections -> edit the draft JSON (or set `"accepted": true/false` on diff
  items the user decided), re-run ingest if the draft changed, re-confirm
  briefly, then apply.
- If the user corrects a misheard name, also run
  `python -m lifecrm entities alias-add <id> "<misspelling>"` so it resolves
  automatically forever.

## Step 6 — report

Report: records saved (from apply output), entities created, current streak
(`status --json`), and remind about `python -m lifecrm backup` if the last
backup is old. Post-apply fixes: `entry edit/retract/restore/merge`,
`entities merge` — never re-ingest to fix a typo.

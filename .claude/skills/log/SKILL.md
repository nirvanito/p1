---
name: log
description: Process the day's voice journal and quick notes into the life CRM — intake, extract all of today's sources as one draft, reconcile, review, and apply. Use when the user wants to log their day, process voice notes, or paste a journal transcript.
---

# /log — daily journal processing

Turn every unprocessed note for a day into structured, confirmed records.
All commands run from the project root with the venv python:
`.venv\Scripts\python.exe -m lifecrm ...` (set `PYTHONUTF8=1`).

The data contract is `docs/contract.md` (contract v2). You never write to the
database directly — only through `lifecrm ingest` + `lifecrm apply`.

## Step 1 — gather the day's sources

1. Run `python -m lifecrm intake` (processes dictated text notes and
   transcribes new audio; skip silently if both inboxes are empty).
2. Run `python -m lifecrm status --json`.
3. Group `unprocessed_sources` by `journal_date`. Choose the day:
   - `$ARGUMENTS` names a date -> that day's sources.
   - The user pasted text -> save to a temp file, run
     `python -m lifecrm import <file> --date <date>`, then proceed with that
     source included.
   - Otherwise -> the most recent date with unprocessed sources.
   - Nothing unprocessed and nothing pasted -> say so and stop.

**A day made of several notes is ONE draft.** Three quick notes plus an
evening note = one extraction, one review, one apply — never four cycles.

## Step 2 — read every transcript

Read each source's transcript (`data\transcripts\YYYY\` or
`python -m lifecrm sql "SELECT text FROM transcripts WHERE source_id=<id>" --json`).
Mid-day quick notes are often terse fragments; the evening note may revisit
the same event — expect overlap and prefer one entry per real-world event.

## Step 3 — draft one contract document

Build a v2 draft (`docs/contract.md`): `sources: [{"source_id": ...}, ...]`
listing every source for the day.

- One entry per distinct thing: meals, workouts, work items, social moments,
  errands, media, thoughts, ideas, decisions, tasks, plans.
- `kind` = what the record IS. `categories` = what it's ABOUT (primary +
  optional secondary). Future things go in `due_at`/`occurred_at` on a
  plan/task — never a future journal_date.
- Numbers become `measurements`; day-level mood/energy/sleep go in
  `day.measurements`. `day.summary`: one or two sentences.
- **Provenance per entry** — `{"source_id": N, "char_start": .., "char_end": ..}`
  (or `segments` for audio) covering the passage that supports it. Mandatory
  when the day has more than one source; do it always. If the evening note
  and a quick note both mention an event, cite the richer passage.

**Entity resolution (mandatory, before writing the draft):** collect every
name, then run

    python -m lifecrm entities match "Name1" "Name2" ... --json

- `exact`/`alias` with ONE match -> `{"ref": "existing", "id": N}`.
- Multiple matches or fuzzy `candidates` -> NEVER guess; ask in Step 4.
- `none` -> `{"ref": "new", ...}` with the fullest known form as the
  canonical name and spoken variants as aliases.

**Closures — listen for them.** "Finally renewed the passport", "gave up on
the garage project", "that idea's dead" usually close a task/plan/idea from
an EARLIER day. Find it:

    python -m lifecrm sql "SELECT uuid, title, journal_date FROM entries e JOIN entry_kinds k ON k.id=e.kind_id WHERE k.name IN ('task','plan') AND e.status='active' AND e.closed_at IS NULL" --json

(or check `open_loops` in status). Propose it as an `entry_updates` item —
closure fields only, provenance pointing at the sentence that closed it.
If today's note itself describes doing the thing, ALSO record the doing as a
normal `event` entry, linked with `resolves`.

**Links — capture threads while they're fresh.** When a note continues an
earlier storyline ("more progress on the deck"), contradicts one, or breaks
a project into parts, add a `links` item: `from_index`/`to_index` for
entries in this draft, `from_uuid`/`to_uuid` for earlier entries. Direction:
later `continues` earlier; new event `resolves` old task; child `part_of`
parent; cause `caused` effect; newer `contradicts` older. Don't force links
— a plain day has none.

**Relationships — only when the note states one.** "Jake grew up in El
Paso" -> an `entity_links` item: Jake `from` El Paso. Direction reads as a
sentence, FROM type TO (Jake `works_at` Acme, Priya `manages` Alex, Linda
`parent_of` Alex); types: `python -m lifecrm entities link types`. Each end
is `{"ref": "existing", "id": N}` or a `new` ref that also appears in an
entry's `entities` in this draft; `provenance` is the stating sentence's
char span; `entry_index` the entry it came with. Never inferred: sharing a
dinner is not `friend_of`, naming a company is not `works_at`. A
relationship to the user ("my partner Maya") needs the user's own person
entity — resolve it like any name; if there is none, ask before creating it.
Already-known relationships come back `unchanged` in the diff.

## Step 4 — review (mandatory — never apply without it)

Write the draft to `data\staging\draft-<date>.json` and run:

    python -m lifecrm ingest data\staging\draft-<date>.json

Fix any validation errors (single-line, precise) and re-run. Then present
the diff compactly:

- Headline: `4 food, 1 exercise, 2 work - mood 7 - sleep 6.5h - closes 1 task`
- Table of new entries: kind, title, categories, entities, which note it
  came from (source), confidence.
- New entities: `NEW place "Movement Gym" (alias: the gym)`; matches:
  `"Jake" -> existing Jake Reilly (id 12)`.
- Relationships (`entity_link` items, their `text`): `NEW Jake Morales from
  El Paso`; `unchanged` ones in one line.
- **Every item with `"accepted": null` is a question for the user**: updates
  to confirmed entries, possible duplicates, retract proposals, and every
  cross-day closure (`entry_update` items are always requires-decision).
  Present each as a one-line question with the old/new values.
- Ambiguities from Step 3.

Ask for a single confirmation (y / corrections).

## Step 5 — apply

- Plain "y" -> set `"accepted": true` on the items the user approved when
  answering the questions above (edit the diff file), then
  `python -m lifecrm apply <diff-file>`.
- Corrections -> edit the draft JSON, re-run ingest, re-confirm briefly.
- "has changed since this diff was built" -> the day moved underneath the
  review (an earlier diff got applied, or an entry was edited). Re-run
  ingest on the same draft and review the fresh diff — never hand-edit a
  diff to get around it. Applied diffs are renamed `.applied`.
- A misheard name -> `python -m lifecrm entities alias-add <id> "<misspelling>"`
  so it resolves automatically forever.

## Step 6 — report

Report: records saved, loops closed, links created, relationships created,
entities created, current streak, and any open loops now overdue
(`status`). If the last backup is old (doctor warns), suggest
`python -m lifecrm backup`.
Post-apply fixes go through `entry edit/retract/restore/merge/close/reopen`,
`link add/rm`, `entities merge`, `entities link add/rm` — never re-ingest to
fix a typo.

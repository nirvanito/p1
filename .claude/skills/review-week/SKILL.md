---
name: review-week
description: Weekly review of the life CRM — build the deterministic evidence pack for last week (or a named week or range), write a short brief where every claim cites a pack id (what happened, wins, open loops, patterns worth attention), and only if the user wants to keep it, save it as a reflection entry through the normal import -> ingest -> review -> apply gate. Use when the user asks for a weekly review, "how was my week", or a recap of a period.
---

# /review-week — the weekly brief

The database already knows what happened; your job is to say what it adds
up to, citing it. `lifecrm review` builds the evidence (deterministic SQL,
no model); you write the interpretation. An interpretation is never a fact:
it is regenerable from the pack, it cites the pack, and it is only stored if
the user asks — as a `reflection` entry through the same review gate as any
journal note. Contract: `docs/review.md`.

Run everything from the project root with the venv python and
`PYTHONUTF8=1` — Windows: `.venv\Scripts\python.exe -m lifecrm ...`,
macOS: `.venv/bin/python -m lifecrm ...`.

## Step 1 — build the pack

    python -m lifecrm review --json

That is the last full week, Monday to Sunday. `$ARGUMENTS` may name another
period — pass it through as `--week 2026-W42`, `--start 2026-10-01 --end
2026-10-14`, or `--days 14` — and use the **same flags in every later
command**, so the ids stay the same. "My year", "2025 in review": `--year
2025` (the `year` lens, picked by the flag; its `year` section adds people
first in touch, towns and countries first on record, books, listening and
mood by month — cite only ids as always; while the user has `year_review`
unchecked the command refuses with the line to check it, which you pass on).

The weekly scheduled job may already have written
`data\staging\review-all-<week>.md`; reading it is fine, but cite from the
`--json` pack you just built (same data, same ids, smaller lens).

- `empty: true` -> say nothing was recorded for the period, show `notes`,
  offer `/log`, and stop.
- `briefs` not empty -> a brief for this period was already saved. Say so
  and ask whether they want a fresh one before writing it.

## Step 2 — read it

- Ids: `D#` day, `E#` entry, `M#` journal metric, `H#` health metric,
  `O#` one observation or a count over observations. `index[id]` has the
  details (uuid, dates, source files) behind every id.
- `tier` matters. `confirmed` items (entries, journal measurements) are what
  the user reviewed. `observed` items (texts, calls, calendar, chats,
  transactions, sensor data) were imported in bulk and never reviewed —
  phrase them as what the data shows ("your messages show...", "the watch
  recorded..."), never as a confirmed event.
- `coverage` and `notes` say how complete the week is ("entries on 2 of 7
  days"). A thin week gets a short brief that says it is thin.
- People: `resolved: false` means a handle nobody has linked yet, shown
  under the name its source gave it. Never guess who it is.

## Step 3 — write the brief

Short: roughly 150-300 words, in second person, these headings:

- **What happened** — the shape of the week in a few sentences.
- **Wins** — closed loops, things finished, good days.
- **Open loops** — overdue first, then what is still open.
- **Patterns worth attention** — trends against the prior period and things
  that co-occur in the pack.
- Optionally **One question for next week**.

Rules (non-negotiable):

- Every factual sentence ends with the id(s) it rests on: `[E3]`,
  `[H1, M2]`. A sentence with no id may only be framing or a question.
- Copy numbers exactly as the pack states them (means, deltas, counts,
  totals). Do no arithmetic of your own beyond what the pack spells out.
- No fact, name, place or date that is not in the pack.
- Patterns are co-occurrence, not cause: "the short nights [H1] fell in the
  same days as the launch entries [E7]" is fine; "work stress ruined your
  sleep" is not.
- Never diagnose, never moralize.

Write the brief to a temporary file **outside** `data\` — e.g.
`$env:TEMP\review-brief-<period>.md` on Windows, `$TMPDIR/review-brief-<period>.md`
on macOS.

## Step 4 — check the citations

    python -m lifecrm review --check <brief file>   (plus the same period flags)

Exit 1 names any id the pack does not hold, or says the brief cites
nothing — fix the brief (remove the claim or cite the right id) and re-run
until it passes. Then show the user the brief.

## Step 5 — keep it? (only if the user says yes)

Ask once: "Keep this brief in the journal?" If not, you are done — nothing
was written, and the brief can be regenerated from the pack at any time.

If yes:

1. Write it out with its sources spelled out (pack ids mean nothing once the
   pack is gone; the footer pins each one to uuids, dates and source files):

       python -m lifecrm review --check <brief file> --out <sourced file>   (same period flags)

2. Import it as a source on the period's last day:

       python -m lifecrm import <sourced file> --type manual --date <period end> --json

   Note the `source_id`, then get the text length:

       python -m lifecrm sql "SELECT length(text) AS n FROM transcripts WHERE source_id=<id>" --json

3. Write `data\staging\draft-review-week-<period slug>.json` — one entry,
   nothing else:

   ```json
   {"contract_version": 2, "journal_date": "<period end>",
    "sources": [{"source_id": <id>}],
    "extraction": {"provider": "claude-code", "model": null, "interface": "claude_skill"},
    "entries": [{
      "kind": "reflection",
      "categories": {"primary": "reflection"},
      "title": "Weekly review <period label, e.g. 2026-W42>",
      "body": "<the brief's gist in one or two sentences>",
      "data": {"derived_from": "lifecrm review", "lens": "week", "period": "<period slug>"},
      "provenance": {"source_id": <id>, "char_start": 0, "char_end": <n>}
    }]}
   ```

   `data.derived_from` must be exactly `"lifecrm review"`: it is what keeps
   the brief out of later packs' evidence, so an interpretation is never
   re-read as a fact. No `day` block (never touch the day's summary or
   measurements), no entities, no measurements, no `entry_updates` — a brief
   records an interpretation, not new facts. If the brief surfaced a loop the
   user now wants closed, that is a separate decision (`entry close`, or the
   next `/log`).

4. `python -m lifecrm ingest data\staging\draft-review-week-<slug>.json`.
   Show the diff (it should be exactly one `add`: the reflection) and wait
   for the user's confirmation. **Never apply without it.**

5. On "y": `python -m lifecrm apply <diff file>`.

## Step 6 — report

Say where it landed (reflection entry on `<period end>`, source id), and
that next week's pack lists it under `briefs` rather than as evidence. If
`doctor` warns the last backup is old, suggest `python -m lifecrm backup`.

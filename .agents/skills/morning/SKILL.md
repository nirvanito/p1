---
name: morning
description: The morning brief — what matters today, written from the deterministic nudges pack (`lifecrm nudges`: what is due, whose birthday is near, nutrients below reference, stale imports, the agenda) in an advisor's voice, every sentence cited to a nudge id and checked against the saved pack before it is shown; suggestions as questions or small experiments, never a diagnosis. Optionally kept as a reflection entry through the normal review gate. Use when the user asks what matters today, what they should not forget, for a morning brief or a start-of-day check-in, or runs /morning.
---

# /morning — what matters today

The database already knows what is due, who has a birthday, what the food
diary shows and which import went quiet; your job is to say what it adds up
to this morning, citing it. `lifecrm nudges` builds the evidence (deterministic
SQL, no model); you write the few sentences. An interpretation is never a
fact: it is regenerable from the pack, it cites the pack, and it is only
stored if the user asks — as a `reflection` entry through the same review
gate as any journal note. Contract: `docs/assistant.md`.

Run everything from the project root with the venv python and
`PYTHONUTF8=1` — Windows: `.venv\Scripts\python.exe -m lifecrm ...`,
macOS: `.venv/bin/python -m lifecrm ...`.

## Step 1 — build and save the pack

    python -m lifecrm nudges --json --out <pack file>

The pack file lives **outside** `data\` — e.g. `$env:TEMP\nudges-<today>.json`
on Windows, `$TMPDIR/nudges-<today>.json` on macOS. Today is the clock's
journal date (timezone and the 05:00 rollover); `--today YYYY-MM-DD` stands
in for it when the user asks about another day — then pass the same flag to
every later command.

Save first, then read: the check in Step 4 and any `N#` you hand to a write
resolve against **this file**, never against a rebuild, because an import or
a closed task renumbers the next run.

- `nudges` and `agenda` both empty -> say nothing is due, no birthday is
  near and nothing has gone stale, show `notes`, and stop.
- `hidden` lists what the user snoozed or dismissed; mention the count, not
  the items, unless asked.

## Step 2 — read it

- Ids: `N#` a nudge (its `kind`, `key`, `title`, `detail`, and the `ids` it
  rests on), `E#` a confirmed entry, `O#` an observed row or a count over
  observed rows. `index[id]` has the details behind every id (uuids, dates,
  import ids).
- `tier` matters. `confirmed` nudges (a due task or plan) rest on what the
  user reviewed. `observed` nudges (a birthday from a contact card, a
  nutrient from the food diary, a stale import, the agenda) rest on bulk
  imports nobody reviewed — phrase them as what the records show ("your
  contact card for Sam gives...", "the food diary averages..."), never as a
  confirmed fact.
- Wording the pack chose is deliberate: "no reply found in imported records"
  is defensible, "you forgot to reply" is not. Keep that register.
- `notes` says what could not be computed (no `[nutrition]` profile, a bad
  `[owner]` table). A thin pack gets a short brief that says it is thin.

## Step 3 — write the brief

Short: at most 200 words, in second person, under one heading, **What
matters today**, optionally followed by **One small experiment** (two weeks,
measurable, framed as a question: "would logging sleep for two weeks show
whether the short nights cluster?").

Rules (non-negotiable):

- Every factual sentence ends with the id(s) it rests on: `[N1]`, `[N2,
  N3]`. A sentence with no id may only be framing or a question.
- Copy numbers and dates exactly as the pack states them (days overdue,
  due dates, ages, averages, percentages). No arithmetic of your own.
- No fact, name, place or date that is not in the pack.
- Never diagnose, never moralize, never medical, legal, tax or investment
  advice. A nutrient below reference is at most a question for a clinician;
  the pack's own disclaimer line says so and you keep its spirit.
- Observed items are what the records show, not what happened.

**Patterns (experimental, optional).** Only when the pack's `patterns`
block has `enabled: true` (the user set `[assistant] patterns = "on"`) and it
holds items: add one short paragraph, **Patterns (experimental)**, one
sentence per `P#`, copying its wording's register — "X tends to go with Y the
same day in this window" — citing `[P#]`, and ending with the words
"co-occurrence, not cause". Never turn a pattern into advice, a cause, or a
diagnosis; a health series is a comparison with the user's own record. When
the block is off or empty, say nothing about patterns at all.

Write the brief to a temporary file **outside** `data\` — e.g.
`$env:TEMP\morning-<today>.md` on Windows, `$TMPDIR/morning-<today>.md` on
macOS.

## Step 4 — check the citations

    python -m lifecrm nudges --check <brief file> --pack <pack file>

Exit 1 names any id the pack does not hold, says the brief cites nothing, or
lists each sentence whose numbers or dates are not in the lines it cites —
fix the brief (remove the claim or cite the right id) and re-run until it
passes. The check verifies ids and numbers; whether a sentence's claim
follows from them is your responsibility. Then show the user the brief with
a **Sources** list: each cited `N#` with the `E#`/`O#` ids it rests on.

If the user waves something off ("not this week", "never again"), do it by
key, never by number:

    python -m lifecrm nudges snooze <key> --days 7
    python -m lifecrm nudges dismiss <key>

`<key>` is the nudge's `key` field (`due:<uuid>`, `birthday:<id>:<year>`,
`nutrient:<name>`, `stale:<connector>`). An `N#` works only together with the
saved pack: `nudges snooze N2 --days 7 --pack <pack file>`.

## Step 5 — keep it? (only if the user says yes)

Ask once: "Keep this brief in the journal?" If not, you are done — nothing
was written, and the brief can be regenerated from the records at any time.

If yes, follow /review-week's Step 5 with these differences:

1. Import the brief as a source on today's date:

       python -m lifecrm import <brief file> --type manual --date <today> --json

   Note the `source_id`, then get the text length:

       python -m lifecrm sql "SELECT length(text) AS n FROM transcripts WHERE source_id=<id>" --json

2. Write `data\staging\draft-morning-<today>.json` — one entry, nothing else:

   ```json
   {"contract_version": 2, "journal_date": "<today>",
    "sources": [{"source_id": <id>}],
    "extraction": {"provider": "claude-code", "model": null, "interface": "claude_skill"},
    "entries": [{
      "kind": "reflection",
      "categories": {"primary": "reflection"},
      "title": "Morning brief <today>",
      "body": "<the brief's gist in one or two sentences>",
      "data": {"derived_from": "lifecrm morning", "pack_id": "<pack_id from the pack file>"},
      "provenance": {"source_id": <id>, "char_start": 0, "char_end": <n>}
    }]}
   ```

   `data.derived_from` must be exactly `"lifecrm morning"`: every generated
   interpretation carries a `derived_from` starting with `lifecrm `, which
   keeps it out of later packs' evidence, so an interpretation is never
   re-read as a fact. No `day` block, no entities, no measurements, no
   `entry_updates` — a brief records an interpretation, not new facts. If the
   brief surfaced a loop the user now wants closed, that is a separate
   decision (`entry close`, or the next `/log`).

3. `python -m lifecrm ingest data\staging\draft-morning-<today>.json`. Show
   the diff (it should be exactly one `add`: the reflection) and wait for the
   user's confirmation. **Never apply without it.**

4. On "y": `python -m lifecrm apply <diff file>`.

## Step 6 — report

Say where it landed (reflection entry on `<today>`, source id), and that the
Obsidian Home note and the web UI's Today card show the same nudges from the
same records. If `doctor` warns the last backup is old, suggest
`python -m lifecrm backup`.

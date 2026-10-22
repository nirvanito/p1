# Extraction contract — version 2

The provider-neutral boundary of the system:

    transcript(s) -> versioned extraction JSON -> validation/review -> persistence

Any extractor (the /log skill, a hand-written draft, a future tool) produces
this document. `lifecrm ingest` validates it deterministically — single-line
errors, safe to fix and retry — then reconciles it into a reviewable diff.
Nothing reaches the database except through `lifecrm apply` on that diff.

## Document shape

```json
{
  "contract_version": 2,
  "journal_date": "2026-07-27",
  "sources": [{"source_id": 3}, {"source_id": 5}],
  "extraction": {"provider": "claude-code", "model": null, "interface": "claude_skill"},
  "day": {
    "summary": "One or two sentences about the day.",
    "measurements": [
      {"metric": "mood", "numeric_value": 7},
      {"metric": "sleep_hours", "numeric_value": 6.5, "unit": "h"}
    ]
  },
  "entries": [
    {
      "kind": "event",
      "categories": {"primary": "exercise", "secondary": ["social"]},
      "title": "Bouldering at Movement with Jake",
      "body": "About 90 minutes; finally sent the blue V4.",
      "time_of_day": "evening",
      "occurred_at": null,
      "ended_at": null,
      "due_at": null,
      "data": null,
      "confidence": "high",
      "measurements": [
        {"metric": "duration", "numeric_value": 90, "unit": "min"}
      ],
      "entities": [
        {"ref": "existing", "id": 12, "role": "participant"},
        {"ref": "new", "type": "place", "name": "Movement Gym",
         "aliases": ["Movement", "the gym"], "role": "location"}
      ],
      "provenance": {"source_id": 3, "char_start": 210, "char_end": 330}
    }
  ],
  "entry_updates": [
    {
      "target_uuid": "b8e2...-uuid-of-tuesdays-task",
      "set": {"closure": "done", "closed_at": "2026-07-27T15:00:00Z"},
      "provenance": {"source_id": 5, "char_start": 0, "char_end": 45},
      "note": "\"finally renewed the passport\""
    }
  ],
  "links": [
    {"from_index": 0, "to_uuid": "uuid-of-an-earlier-entry", "type": "continues"},
    {"from_index": 2, "to_index": 0, "type": "part_of"}
  ],
  "ambiguities": [
    {"note": "'Sam' could be Sam Ortega (id 8) or someone new — asked the user."}
  ]
}
```

## Field rules

- **contract_version** — must be `2`.
- **journal_date** — `YYYY-MM-DD`, never in the future. The journal date is
  *when it was journaled about*; future events live in `due_at`/`occurred_at`
  of `plan`/`task` entries.
- **sources** — non-empty array. A day made of several notes (evening note
  plus mid-day quick notes) extracts as ONE draft listing every source:
  - `{"source_id": N}` — an existing source (created by `intake`/`transcribe`/
    `import`);
  - `{"text": "...", "type": "pasted|phone_transcript|manual",
    "recorded_at": "...Z"}` — inline text, allowed only in single-source
    drafts (import multi-source days first, then reference ids).
- **entries[].provenance** — `{"source_id": N}` plus exactly one of
  `{"char_start", "char_end"}` (offsets into that source's transcript) or
  `{"segments": [idx, ...]}` (whisper segment indexes). `source_id` may be
  omitted only in single-source drafts. **Required on every entry in a
  multi-source draft** — provenance must never degrade from "the passage
  behind this record" to "one of today's notes". Offsets index the transcript
  **as stored**, which is normalized: CRLF/CR become LF and outer whitespace
  is stripped. For an inline `text` source, count offsets in that normalized
  form — ingest checks them against it, not against the raw draft string.
- **extraction** — optional object: `provider` (non-empty string), `model`
  (string or null), `interface` (`claude_skill` or `manual`). Anything else —
  a non-object, an unknown key — is a contract error, so `ingest --dry-run`
  refuses exactly what a real ingest would.
- **day.summary** — optional non-empty string. Conflicts with an existing
  different summary become a decision item in the diff.
- **measurements** (day-level or per-entry) — `metric`, plus `numeric_value`
  and/or `text_value`, optional `unit`, optional `confidence`. Known metrics
  are validated and normalized (`mood`/`energy` 1–10, `sleep_hours` 0–16 h,
  `duration` -> min, `distance` -> km, `weight` -> kg, `calories`, `reps`,
  `sets`, `servings`, `steps`, `water_ml`, `money`, `rating`). Unknown
  metrics pass through as open-ended personal metrics.
- **entries[].kind** — what the record *is*: `event`, `observation`,
  `thought`, `idea`, `decision`, `task`, `plan`, `fact`, `reflection`
  (extensible via `lifecrm kinds add`).
- **entries[].categories** — what it's *about*: one `primary` plus optional
  `secondary` list (extensible via `lifecrm categories add`).
- **occurred_at / ended_at / due_at** — ISO dates or datetimes; future values
  are valid here.
- **entities[]** — `{"ref": "existing", "id": N, "role": R}` or
  `{"ref": "new", "type": T, "name": "...", "aliases": [...], "role": R}`.
  A `new` entity whose name exactly matches an existing active entity (or
  alias) of the same type is rejected — resolve with `lifecrm entities match`
  first.
- **entries[].uuid** — only for proposing an update to an existing
  **active entry of the same journal day**, and each uuid at most once.
  Changes to user-confirmed entries always become requires-decision diff
  items. A uuid on another day, or of a retracted/superseded entry, is
  refused (decision #5, 2026-10): cross-day closures go through
  `entry_updates`, any other cross-day change through `lifecrm entry edit`.
  (A cross-day uuid used to be auto-edited without a decision and outside the
  day fingerprint that guards the review.)
- **entry_updates** — the cross-day operation: *"finished the thing I
  mentioned Tuesday"* closes an entry on a DIFFERENT day than the one being
  extracted. Constraints, all enforced:
  - `set` may touch **closure fields only** (`closure`: done/dropped/obsolete,
    optional `closed_at`). Titles, categories, dates, bodies are deliberately
    excluded — cross-day content edits go through `lifecrm entry edit`, not
    through an extractor.
  - `provenance` is **required**: the closure must trace to the sentence that
    closed it.
  - Always a requires-decision diff item; never auto-applied; never silently
    modifies a confirmed record.
- **links** — thread structure. Each end is `from_index`/`to_index` (an index
  into this draft's `entries`) or `from_uuid`/`to_uuid` (an existing entry).
  Types and directions: `later continues earlier`, `new_event resolves
  old_task`, `child part_of parent`, `cause caused effect`, `newer
  contradicts older`, `relates` (loose association). Links ride the diff for
  review and are created at apply; a link whose index resolves to a
  skipped-duplicate entry attaches to the surviving entry instead.
- **ambiguities** — free-form audit trail of what was uncertain and how it
  was resolved; stored with the draft, not in the entries.

## Lifecycle

1. `lifecrm ingest draft.json` — validate, create the extraction run (linked
   to every source), reconcile against existing records, write
   `data\staging\diff-<date>-<run>.json`.
2. Review the diff. Items with `"accepted": null` (updates to confirmed
   entries, possible duplicates, retract proposals, every entry_update) stay
   skipped unless you set `"accepted": true`.
3. `lifecrm apply diff.json` — transactional persistence with audit +
   revision records. Applying is the user's confirmation: created entries are
   marked confirmed.
4. Or, for a diff that will never be applied (a wrong extraction, a diff
   superseded by a fresh ingest): `lifecrm admin discard-run <run>` — the
   full run uuid or the 8-character prefix in the diff's file name. The run
   stays on record as `discarded` (audited), its diff is renamed
   `.discarded`, apply refuses it, and its sources show as unprocessed again
   until another run applies them. Doctor's `stuck_drafts` warning names the
   runs and this command.

**Possible duplicates.** An entry without a uuid is compared with the day's
active entries. An exactly equal title (case and spacing aside) is always a
possible duplicate — that is what keeps a re-ingested day collapsing onto
itself. A merely similar title is one only with a corroborating signal: a
shared entity, an overlapping passage of the same source, or a body that says
the same thing (similarity >= 0.6). Title similarity >= 0.9 needs one signal;
>= 0.75 also needs the same kind and primary category. So "Called Mom" and
"Called Tom" (0.9, nothing shared) are two calls. Each `possible_duplicate`
item carries the incumbent for review — `of_title`, `of_body`, `of_kind`,
`of_categories`, `of_entities` — plus `similarity` and `signals`
(`same_title`, `similar_title`, `shared_entity`, `same_passage`,
`similar_body`). Apply reads none of these; they are there to decide by.

A diff is a review of one before-state, and it says so: `day_fingerprint`
records the day as ingest saw it. If anything about that day changed since —
another diff applied, an entry edited, closed, retracted or moved, a day
measurement or summary set, or a cross-day `entry_update` target touched —
apply refuses the diff and asks for a fresh `ingest`. Re-ingesting is always
the right answer and is safe: entries already in the day come back as
possible-duplicates, which stay skipped. Applied diffs are renamed
`diff-<date>-<run>.json.applied` so they cannot be applied twice by habit.

## Version history

- v2 (2026-07, schema v2): `sources` array replaces singular `source`;
  per-entry provenance names its source; `entry_updates` (closure-only,
  requires-decision, cross-day); `links`; closable loops (`closed_at`,
  `closure`) on entries.
- v1 (2026-07): initial contract.

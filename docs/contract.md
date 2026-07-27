# Extraction contract — version 1

The provider-neutral boundary of the system:

    transcript -> versioned extraction JSON -> validation/review -> persistence

Any extractor (the /log skill, a hand-written draft, a future tool) produces
this document. `lifecrm ingest` validates it deterministically — single-line
errors, safe to fix and retry — then reconciles it into a reviewable diff.
Nothing reaches the database except through `lifecrm apply` on that diff.

## Document shape

```json
{
  "contract_version": 1,
  "journal_date": "2026-07-27",
  "source": {"source_id": 3},
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
      "provenance": {"char_start": 210, "char_end": 330}
    }
  ],
  "ambiguities": [
    {"note": "'Sam' could be Sam Ortega (id 8) or someone new — asked the user."}
  ]
}
```

## Field rules

- **contract_version** — must be `1`.
- **journal_date** — `YYYY-MM-DD`, never in the future. The journal date is
  *when it was journaled about*; future events live in `due_at`/`occurred_at`
  of `plan`/`task` entries.
- **source** — exactly one of:
  - `{"source_id": N}` — an existing source (created by `transcribe`/`import`);
  - `{"text": "...", "type": "pasted|phone_transcript|manual", "recorded_at": "...Z"}`
    — inline text; ingest creates the source + sentence-boundary segments.
- **extraction** — optional metadata; `interface` must be `claude_skill` or
  `manual`.
- **day.summary** — optional non-empty string. Conflicts with an existing
  different summary become a decision item in the diff.
- **measurements** (day-level or per-entry) — `metric` (string), plus
  `numeric_value` and/or `text_value`, optional `unit`, optional `confidence`.
  Known metrics are validated and normalized (`mood`/`energy` 1–10,
  `sleep_hours` 0–16 h, `duration` -> min, `distance` -> km, `weight` -> kg,
  `calories`, `reps`, `sets`, `servings`, `steps`, `water_ml`, `money`,
  `rating`). Unknown metrics are allowed as open-ended personal metrics — no
  normalization, no range check.
- **entries[].kind** — what the record *is*: `event`, `observation`,
  `thought`, `idea`, `decision`, `task`, `plan`, `fact`, `reflection`
  (extensible via `lifecrm kinds add`).
- **entries[].categories** — what it's *about*: one `primary` plus optional
  `secondary` list (extensible via `lifecrm categories add`).
- **entries[].title** — required, short, human-scannable.
- **time_of_day** — `morning|afternoon|evening|night` or omitted.
- **occurred_at / ended_at / due_at** — ISO dates or datetimes; future values
  are valid here.
- **data** — JSON object for genuinely uncommon metadata only; anything
  measurable belongs in `measurements`, anything nameable in `entities`.
- **entities[]** — either `{"ref": "existing", "id": N, "role": R}` or
  `{"ref": "new", "type": T, "name": "...", "aliases": [...], "role": R}`.
  Types: person, place, food, project, activity, media, organization, topic.
  Roles: participant, location, item, subject, mention. A `new` entity whose
  name exactly matches an existing active entity (or alias) of the same type
  is **rejected** — resolve with `lifecrm entities match` first.
- **provenance** — exactly one of `{"char_start": N, "char_end": M}`
  (offsets into the transcript text; the primary form for imported text) or
  `{"segments": [idx, ...]}` (whisper segment indexes). Strongly recommended
  on every entry — it powers "show me the passage behind this record".
- **entries[].uuid** — only for proposing an update to an existing entry.
  Changes to user-confirmed entries always become requires-decision diff items.
- **ambiguities** — free-form audit trail of what was uncertain and how it
  was resolved; stored with the draft, not in the entries.

## Lifecycle

1. `lifecrm ingest draft.json` — validate, create the extraction run, reconcile
   against existing records, write `data\staging\diff-<date>-<run>.json`.
2. Review the diff. Items with `"accepted": null` (updates to confirmed
   entries, possible duplicates, retract proposals) stay skipped unless you
   set `"accepted": true`.
3. `lifecrm apply diff.json` — transactional persistence with audit +
   revision records. Applying is the user's confirmation: created entries are
   marked confirmed.

Version history: v1 — initial contract (2026-07). Version bumps accompany a
schema migration and a note here.

# The observed tier — design sketch

Status: **built as schema v4** (2026-08-24). The schema, the shared write
layer (`lifecrm/domain/observations.py`), the connector contract
(`lifecrm/connectors/base.py`) and the first connector
([ai_chat](ai-chat.md) — Claude and ChatGPT exports) are implemented and
tested. Remaining connectors (texts, calendar, finance, photos) are not.

## The problem this solves

The system today has exactly one way in: a source becomes a transcript, an
extractor proposes a draft, the owner reviews a diff, `apply` writes it. Every
record in `entries` and `measurements` is user-confirmed. That gate is the
single most valuable property of this codebase and it must not be weakened.

It also does not scale past voice. Reading texts, AI chat history, notes,
calendar, finance and photo metadata means tens to hundreds of thousands of
records. The owner will never review those, and should not be asked to.

So: **two tiers, one rule.**

| | confirmed | observed |
|---|---|---|
| tables | `entries`, `measurements`, `mentions` | `observations`, `health_records` |
| written by | `apply`, after review | importers, deterministically |
| volume | ~20/day | ~1000s/day, plus years of backfill |
| may be asserted as fact | yes | **only with the row cited** |
| corrections | revisions, retraction, merge | retract the import and re-run |

The rule: **observed records never become confirmed records without passing the
existing gate.** An importer may not write `entries`, `measurements`, `mentions`
or `entity_aliases`. Ever.

This is not a new idea in this codebase — `measurements` (what the journal said)
versus `health_records` (what the sensor recorded) is exactly this split, decided
in F8. The observed tier generalizes that precedent rather than inventing one.

## Why not put everything in `health_records`

`health_records` is metric-shaped: `metric`, `value`, `unit`, aggregation over a
day. Texts and calendar events are event-shaped: a body, participants, a
direction. Forcing them together produces a table where half the columns are
always null and a `stream` vocabulary where `steps` sits next to `imessage`.

Two tables, unified by a shared *contract* and shared lifecycle columns, not by a
shared row shape. `health_records` keeps working untouched; no risky migration of
the one importer that already runs.

## Schema

```sql
-- One row per importer run. The unit of provenance, liveness and undo.
CREATE TABLE imports (
  id          INTEGER PRIMARY KEY,
  uuid        TEXT NOT NULL UNIQUE,
  connector   TEXT NOT NULL,          -- 'imessage','gcal','claude_export','photos'
  account     TEXT,                   -- which mailbox/phone/calendar, when relevant
  source_file TEXT,                   -- export file or backup path, when file-based
  source_hash TEXT,                   -- content hash: re-importing the same export is a no-op
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running','applied','failed','retracted')),
  counts_json TEXT,                   -- {new, updated, refreshed, identical, conflicts}
  created_at  TEXT NOT NULL
);

-- Bulk-imported records. Lifecycle columns deliberately mirror health_records.
CREATE TABLE observations (
  id               INTEGER PRIMARY KEY,
  uuid             TEXT NOT NULL UNIQUE,
  stream           TEXT NOT NULL,     -- 'imessage','email','gcal','claude_chat','txn','photo'
  import_id        INTEGER NOT NULL REFERENCES imports(id),
  journal_date     TEXT NOT NULL REFERENCES days(journal_date),
  started_at       TEXT NOT NULL,     -- UTC ISO-8601
  ended_at         TEXT,              -- NULL for point-in-time records
  utc_offset_sec   INTEGER,           -- record's own zone offset, when known
  direction        TEXT CHECK (direction IN ('in','out','self')),
  title            TEXT,              -- subject, event name, merchant, filename
  body             TEXT,              -- message text, note body, chat turn
  value            REAL,              -- amount, duration, count
  unit             TEXT,              -- 'usd','min','count'
  value_json       TEXT,              -- structured detail: EXIF, attendees, line items
  media_path       TEXT,              -- external file path; media never enters the db
  origin_app       TEXT,
  external_id      TEXT NOT NULL,     -- source system's own stable id
  last_modified_at TEXT,              -- source system's mtime, for newer-wins
  status           TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','retracted')),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE (stream, external_id),
  CHECK (ended_at IS NULL OR started_at <= ended_at)
);
CREATE INDEX idx_obs_stream_date  ON observations(stream, journal_date);
CREATE INDEX idx_obs_journal_date ON observations(journal_date);
CREATE INDEX idx_obs_import       ON observations(import_id);

CREATE VIRTUAL TABLE observations_fts USING fts5(
  title, body, content='observations', content_rowid='id');
-- plus ai/ad/au triggers in the style of entries_fts

-- The bridge between machine identifiers and the entity graph.
-- entity_id NULL means "seen, not yet resolved" — a work queue, not an error.
CREATE TABLE entity_handles (
  kind              TEXT NOT NULL CHECK (kind IN
    ('phone','email','calendar','app_account','card','place_id')),
  value             TEXT NOT NULL COLLATE NOCASE,
  entity_id         INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  display_name      TEXT,             -- what the source system called it, for review
  confirmed_at      TEXT,             -- NULL until the owner confirms the mapping
  observation_count INTEGER NOT NULL DEFAULT 0,
  first_seen_at     TEXT NOT NULL,
  last_seen_at      TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (kind, value)
);
-- Deliberate contrast with entity_aliases, whose PK (alias, entity_id) makes
-- ambiguity a supported state. A phone number is not ambiguous; that is the
-- entire reason this table exists.

CREATE TABLE observation_participants (
  observation_id INTEGER NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
  handle_kind    TEXT NOT NULL,
  handle_value   TEXT NOT NULL,
  role           TEXT NOT NULL DEFAULT 'participant' CHECK (role IN
    ('sender','recipient','participant','organizer','location','counterparty')),
  PRIMARY KEY (observation_id, handle_kind, handle_value, role),
  FOREIGN KEY (handle_kind, handle_value)
    REFERENCES entity_handles(kind, value) ON DELETE CASCADE
);

-- A confirmed entry may cite an observation as its provenance, exactly as
-- entry_sources cites a transcript passage. Kept separate so the working
-- provenance path is untouched.
CREATE TABLE entry_observations (
  entry_id       INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  observation_id INTEGER NOT NULL REFERENCES observations(id),
  char_start     INTEGER,
  char_end       INTEGER,
  PRIMARY KEY (entry_id, observation_id)
);
```

## Entity resolution at volume — the load-bearing idea

50,000 texts mention thousands of name-strings, and none of them can go through
the `/log` entity gate. The move is: **importers never emit names. They emit
handles** — a phone number, an email address, a calendar attendee id, a card
last-four. Machine identifiers, stable and unambiguous.

Resolution is then a join:

```
observations -> observation_participants -> entity_handles -> entities
```

Confirming one handle resolves **every observation that ever referenced it, past
and future, with no backfill pass**. The review cost is O(contacts), not
O(messages): a few hundred decisions covering a decade, presented
noisiest-first.

```
lifecrm handles list --unresolved --top 50      # ranked by observation_count
lifecrm handles link phone:+15125551234 --entity 12
lifecrm handles link email:jake@acme.com --new person "Jake Reilly"
lifecrm handles ignore phone:+18005550100       # spam, delivery notifications
```

Fuzzy name-matching *inside* message bodies is deliberately excluded. It is a
derived, regenerable interpretation, and storing it as fact is exactly the
mistake the confirmed/observed split exists to prevent.

## The connector contract

Formalizes what `health_connect.py` already does, with one structural change:
**the plan/apply machinery moves out of the connector and is written once.**
Today `build_plan`/`apply_plan` live inside the Health Connect module; every new
connector would copy them.

```python
# lifecrm/connectors/base.py
class Scan(TypedDict):
    connector:   str
    account:     str | None
    source_file: str | None
    source_hash: str | None
    records:     list[dict]   # observation dicts, normalized units, UTC times
    handles:     list[dict]   # {kind, value, display_name}
    unmapped:    dict         # what the connector saw and did not understand
    skipped:     dict         # what it deliberately dropped, with counts

class Connector(Protocol):
    name:    str
    streams: tuple[str, ...]
    def scan(self, target: Path | str, cfg: Config) -> Scan: ...
    def report_lines(self, scan: Scan, plan: dict) -> list[str]: ...
```

A connector is a **pure reader**: it opens a file or calls an API and returns
records. It never touches the database. Everything else is shared:

```python
# lifecrm/domain/observations.py — written once, used by every connector
validate(rec)                    -> str | None      # single-line error
existing_by_key(conn, streams)   -> dict[(stream, external_id), Row]
classify(row, rec)               -> 'identical'|'refreshed'|'update'|'conflict'
build_plan(conn, scan)           -> plan            # newer-wins, conflicts surfaced
apply_plan(conn, scan, plan)     -> counts          # one transaction, one imports row
retract_import(conn, import_uuid)                   # status='retracted', audited
```

`classify` is lifted from `domain/health.py` — newer `last_modified_at` wins,
changed-but-not-newer is a conflict surfaced rather than an overwrite. That logic
is already correct and already tested; the observed tier inherits it instead of
reinventing it.

CLI, mirroring `health import` (dry-run by default, `--apply` to write):

```
lifecrm observe <connector> <target>            # dry-run report
lifecrm observe <connector> <target> --apply
lifecrm observe --list                          # connectors and last run time
lifecrm observe retract <import-uuid>
```

Note the naming collision: `lifecrm import` already means "import a text
transcript as a journal source." Hence `observe` for the new group. `health
import` should eventually move under it as `observe health <zip>`, but not until
the observed tier is real.

## What this changes in existing code

- **`doctor`** — `entries_have_provenance` becomes "has `entry_sources` **or**
  `entry_observations`". New checks: observations whose `import_id` is retracted,
  handles pointing at a merged entity, `imports` stuck in `running`. New warning:
  connector last-run age — the observed-tier equivalent of the capture-liveness
  gap already on the roadmap.
- **`export`** — three new tables in JSONL. Markdown export should *not* dump raw
  observations; a real day has hundreds. Day pages get a counts line
  (`42 messages · 3 events · 2 transactions`) and nothing more.
- **`status`** — per-connector last-run and unresolved-handle count. *(Not
  done: `lifecrm connectors` covers the last-run half, and `doctor` warns on
  unresolved handles, but `status` itself is unchanged.)*
- **`days`** — importing ten years of history creates ~3,650 `days` rows with no
  entries. Check that streak and "days logged" count days with *entries*, not
  days that merely exist, before the first backfill runs.
- **Contract** — unchanged. The extractor keeps producing drafts from
  transcripts. A later contract version could let a draft cite an observation as
  provenance for a confirmed entry (`entry_observations`), but that is a separate
  decision and not required by this design.

## Open decisions

1. ~~**Does `health_records` fold into `observations`?**~~ **Decided: no.**
   `health_records` is untouched. Revisit only if a third numeric connector
   appears and the duplication actually hurts.
2. **Import cadence** — file-based connectors (iPhone backup, chat exports) are
   periodic snapshots, not streams. Does a scheduled task drive them, or is
   backfill a deliberate manual act? Snapshot connectors re-scan everything each
   run; `source_hash` makes an unchanged export a no-op, which is what keeps that
   cheap.
3. **Deletion semantics** — a message deleted at the source disappears from the
   next export. Does its observation stay (the archive remembers) or get
   retracted (the archive mirrors)? **Provisionally decided as stay**, because
   that is what the ai_chat connector does today and it is the recoverable
   direction. Confirm before the first *text* import, where the stakes are
   higher and the choice is unrecoverable the other way.
4. **Encryption at rest.** A decade of texts, finance and photo metadata in one
   unencrypted SQLite is the largest liability this project will ever hold, and
   it makes `dashboard --host 0.0.0.0` considerably more dangerous than it is
   today. Decide before, not after.
5. **Handle privacy in exports** — do phone numbers and email addresses appear in
   the markdown archive, or only resolved entity names?

## Explicitly not in this design

- No fuzzy name resolution from message bodies (derived, not stored).
- No media in the database — `media_path` only, per the existing rule.
- No importer writes to `entries`, `measurements`, `mentions` or
  `entity_aliases`, under any circumstance.
- No federated query layer. Every connector copies into `lifecrm.db`; nothing is
  read live from an app at question time.
- No LLM anywhere in the import path. Connectors are deterministic parsers, the
  same way `contract.py` validation is deterministic.

-- Life CRM schema v5.
-- Conventions: timestamps are UTC ISO-8601 ('YYYY-MM-DDTHH:MM:SSZ'); journal
-- dates are 'YYYY-MM-DD' computed in the source's recorded timezone with the
-- configured rollover hour. User-facing records carry stable UUIDs that survive
-- reconciliation. Rows in sources/transcripts/transcript_segments are immutable
-- source material: corrections create revisions, never rewrites.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE days (
  journal_date TEXT PRIMARY KEY
    CHECK (journal_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  summary      TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

-- Extensible without migration: new rows, not new columns.
CREATE TABLE categories (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  created_at TEXT NOT NULL
);

CREATE TABLE entry_kinds (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
  created_at TEXT NOT NULL
);

CREATE TABLE sources (
  id           INTEGER PRIMARY KEY,
  uuid         TEXT NOT NULL UNIQUE,
  type         TEXT NOT NULL CHECK (type IN ('audio','pasted','phone_transcript','manual')),
  path         TEXT,
  content_hash TEXT,
  recorded_at  TEXT,
  timezone     TEXT NOT NULL,          -- IANA name, captured per source at ingest
  journal_date TEXT NOT NULL REFERENCES days(journal_date),
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_sources_journal_date ON sources(journal_date);
CREATE INDEX idx_sources_content_hash ON sources(content_hash);

CREATE TABLE transcripts (
  id           INTEGER PRIMARY KEY,
  source_id    INTEGER NOT NULL REFERENCES sources(id),
  engine       TEXT NOT NULL CHECK (engine IN ('faster-whisper','external')),
  model        TEXT,
  language     TEXT,
  duration_sec REAL,
  text         TEXT NOT NULL,
  created_at   TEXT NOT NULL
);
CREATE INDEX idx_transcripts_source ON transcripts(source_id);

-- Imported text gets sentence-boundary segments with NULL start/end_sec and
-- populated char offsets; whisper segments carry both.
CREATE TABLE transcript_segments (
  id            INTEGER PRIMARY KEY,
  transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
  idx           INTEGER NOT NULL,
  start_sec     REAL,
  end_sec       REAL,
  char_start    INTEGER NOT NULL,
  char_end      INTEGER NOT NULL,
  text          TEXT NOT NULL,
  UNIQUE (transcript_id, idx)
);

-- A run may extract several same-day sources into one draft (an evening note
-- plus mid-day quick notes). Sources live in the join table only — there is
-- deliberately no denormalized "primary source" pointer to drift out of sync.
CREATE TABLE extraction_runs (
  id               INTEGER PRIMARY KEY,
  uuid             TEXT NOT NULL UNIQUE,
  provider         TEXT NOT NULL,
  model            TEXT,
  interface        TEXT NOT NULL CHECK (interface IN ('claude_skill','manual')),
  contract_version INTEGER NOT NULL,
  input_hash       TEXT NOT NULL,
  started_at       TEXT NOT NULL,
  reviewed_at      TEXT,
  status           TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','reviewed','applied','discarded'))
);

CREATE TABLE extraction_run_sources (
  extraction_run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
  source_id         INTEGER NOT NULL REFERENCES sources(id),
  PRIMARY KEY (extraction_run_id, source_id)
);

CREATE TABLE entries (
  id            INTEGER PRIMARY KEY,
  uuid          TEXT NOT NULL UNIQUE,
  journal_date  TEXT NOT NULL REFERENCES days(journal_date),
  kind_id       INTEGER NOT NULL REFERENCES entry_kinds(id),
  title         TEXT NOT NULL,
  body          TEXT,
  time_of_day   TEXT CHECK (time_of_day IN ('morning','afternoon','evening','night')),
  occurred_at   TEXT,                  -- future dates valid for plans/tasks/events
  ended_at      TEXT,
  due_at        TEXT,
  data          TEXT,                  -- JSON; genuinely uncommon metadata only
  confidence    TEXT NOT NULL DEFAULT 'high' CHECK (confidence IN ('high','medium','low')),
  status        TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','superseded','retracted')),
  superseded_by INTEGER REFERENCES entries(id),
  confirmed_at  TEXT,                  -- set on user confirmation; protects from automated overwrite
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  -- Closable loops (v2). Deliberately separate from `status`: retracting means
  -- "this record was wrong"; dropping a plan means "this really happened and I
  -- chose not to do it" — those must stay distinguishable in 2040.
  -- closed_at/closure are last in the column list so a v1 database migrated
  -- via ALTER TABLE ADD COLUMN matches this definition exactly. The
  -- both-set-or-both-null invariant is enforced by the domain layer and
  -- verified by `lifecrm doctor` (ALTER cannot add a table-level CHECK, and
  -- migrated vs fresh schemas must not drift).
  closed_at     TEXT,
  closure       TEXT CHECK (closure IN ('done','dropped','obsolete'))
);
CREATE INDEX idx_entries_journal_date ON entries(journal_date);
CREATE INDEX idx_entries_status ON entries(status);

CREATE TABLE entry_categories (
  entry_id    INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  is_primary  INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1)),
  PRIMARY KEY (entry_id, category_id)
);
-- Backstop only: exactly-one-primary is enforced in the domain layer.
CREATE UNIQUE INDEX idx_entry_primary_category ON entry_categories(entry_id) WHERE is_primary = 1;

CREATE TABLE entry_revisions (
  id         INTEGER PRIMARY KEY,
  entry_uuid TEXT NOT NULL,
  revised_at TEXT NOT NULL,
  actor      TEXT NOT NULL CHECK (actor IN ('user','extraction','system')),
  change_kind TEXT NOT NULL,
  old_json   TEXT,
  new_json   TEXT,
  note       TEXT
);
CREATE INDEX idx_revisions_entry ON entry_revisions(entry_uuid);

CREATE TABLE measurements (
  id                INTEGER PRIMARY KEY,
  uuid              TEXT NOT NULL UNIQUE,
  entry_id          INTEGER REFERENCES entries(id) ON DELETE CASCADE,
  journal_date      TEXT REFERENCES days(journal_date),
  metric            TEXT NOT NULL,
  numeric_value     REAL,
  text_value        TEXT,
  unit              TEXT,
  normalized_value  REAL,
  normalized_unit   TEXT,
  confidence        TEXT NOT NULL DEFAULT 'high' CHECK (confidence IN ('high','medium','low')),
  extraction_run_id INTEGER REFERENCES extraction_runs(id),
  status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retracted')),
  created_at        TEXT NOT NULL,
  CHECK ((entry_id IS NULL) <> (journal_date IS NULL))   -- exactly one owner
);
CREATE INDEX idx_measurements_entry ON measurements(entry_id);
CREATE INDEX idx_measurements_day ON measurements(journal_date);
CREATE INDEX idx_measurements_metric ON measurements(metric);

-- Character offsets are the provenance when segment timestamps are unavailable.
CREATE TABLE entry_sources (
  entry_id          INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  source_id         INTEGER NOT NULL REFERENCES sources(id),
  transcript_id     INTEGER REFERENCES transcripts(id),
  segment_start     INTEGER,
  segment_end       INTEGER,
  char_start        INTEGER,
  char_end          INTEGER,
  extraction_run_id INTEGER REFERENCES extraction_runs(id),
  original_json     TEXT,              -- as extracted
  confirmed_json    TEXT,              -- as confirmed by the user, when different
  PRIMARY KEY (entry_id, source_id)
);

CREATE TABLE entities (
  id          INTEGER PRIMARY KEY,
  uuid        TEXT NOT NULL UNIQUE,
  type        TEXT NOT NULL CHECK (type IN
    ('person','place','food','project','activity','media','organization','topic')),
  name        TEXT NOT NULL,
  slug        TEXT NOT NULL UNIQUE,
  notes       TEXT,
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged')),
  merged_into INTEGER REFERENCES entities(id),
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
-- Uniqueness applies among live entities; merged rows keep their name for history.
CREATE UNIQUE INDEX idx_entities_type_name ON entities(type, name COLLATE NOCASE)
  WHERE status = 'active';

CREATE TABLE entity_aliases (
  alias      TEXT NOT NULL COLLATE NOCASE,
  entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  PRIMARY KEY (alias, entity_id)       -- ambiguous aliases are a supported state
);
CREATE INDEX idx_aliases_alias ON entity_aliases(alias COLLATE NOCASE);

CREATE TABLE mentions (
  entry_id  INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  entity_id INTEGER NOT NULL REFERENCES entities(id),
  role      TEXT NOT NULL DEFAULT 'mention'
    CHECK (role IN ('participant','location','item','subject','mention')),
  PRIMARY KEY (entry_id, entity_id, role)
);
CREATE INDEX idx_mentions_entity ON mentions(entity_id);

-- Thread structure between entries (v2): the edge that is only knowable while
-- the memory is fresh. Direction conventions (unambiguous, documented):
--   later  continues   earlier          new_event  resolves  old_task
--   child  part_of     parent           cause      caused    effect
--   newer  contradicts older            relates is loosely symmetric; store once
-- ON DELETE CASCADE is required, not decorative: admin purge-day hard-deletes
-- entries and foreign keys are ON for every connection, so a plain FK would
-- break purge. Deliberate asymmetry with entry_revisions.entry_uuid, which
-- carries NO foreign key: revision history must outlive a purge — a link to a
-- deleted entry is meaningless, a revision of one is not.
CREATE TABLE entry_links (
  id         INTEGER PRIMARY KEY,
  from_uuid  TEXT NOT NULL REFERENCES entries(uuid) ON DELETE CASCADE,
  to_uuid    TEXT NOT NULL REFERENCES entries(uuid) ON DELETE CASCADE,
  type       TEXT NOT NULL CHECK (type IN
    ('continues','caused','resolves','contradicts','part_of','relates')),
  note       TEXT,
  actor      TEXT NOT NULL CHECK (actor IN ('user','extraction','system')),
  created_at TEXT NOT NULL,
  UNIQUE (from_uuid, to_uuid, type),
  CHECK (from_uuid <> to_uuid)
);
CREATE INDEX idx_links_from ON entry_links(from_uuid);
CREATE INDEX idx_links_to ON entry_links(to_uuid);

-- health_records (v3): deterministic sensor/app imports (Health Connect
-- first). A second metrics layer beside `measurements`, which stays the home
-- of journal-extracted, human-stated facts. These rows carry an external
-- identity (the source system's record uuid), real time ranges, and upsert
-- semantics keyed on (metric, external_id) so a re-imported export never
-- duplicates. Updates land only from a newer last_modified_at and are
-- audit-logged; the export file named in source_file is the provenance anchor.
CREATE TABLE health_records (
  id               INTEGER PRIMARY KEY,
  uuid             TEXT NOT NULL UNIQUE,
  metric           TEXT NOT NULL,          -- 'sleep_session','steps','weight',...
  journal_date     TEXT NOT NULL REFERENCES days(journal_date),
  started_at       TEXT NOT NULL,          -- UTC ISO-8601
  ended_at         TEXT,                   -- NULL for point-in-time records
  utc_offset_sec   INTEGER,                -- record's own zone offset, when known
  value            REAL,                   -- primary numeric, normalized unit
  unit             TEXT,                   -- 'kg','min','kcal','bpm','count',...
  value_json       TEXT,                   -- structured detail (stages, macros)
  origin_app       TEXT,                   -- recording app's package name
  external_id      TEXT NOT NULL,          -- source system record uuid (hex)
  source_file      TEXT NOT NULL,          -- export file this row came from
  last_modified_at TEXT,                   -- source system's modification time
  status           TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','retracted')),
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE (metric, external_id),
  CHECK (ended_at IS NULL OR started_at <= ended_at),
  CHECK (value IS NOT NULL OR value_json IS NOT NULL)
);
CREATE INDEX idx_health_metric_date ON health_records(metric, journal_date);
CREATE INDEX idx_health_journal_date ON health_records(journal_date);

-- Append-only; the primary revision record alongside entry_revisions.
CREATE TABLE audit_log (
  id         INTEGER PRIMARY KEY,
  ts         TEXT NOT NULL,
  actor      TEXT NOT NULL,
  action     TEXT NOT NULL,
  table_name TEXT NOT NULL,
  row_uuid   TEXT,
  details    TEXT
);

-- Populated by the future /recall skill; created now so the extension point exists.
CREATE TABLE recall_log (
  id             INTEGER PRIMARY KEY,
  asked_at       TEXT NOT NULL,
  question       TEXT NOT NULL,
  answered       INTEGER NOT NULL DEFAULT 0 CHECK (answered IN (0,1)),
  answer_summary TEXT
);

-- Full-text search (external content, kept in sync by triggers).
CREATE VIRTUAL TABLE entries_fts USING fts5(title, body, content='entries', content_rowid='id');

CREATE TRIGGER entries_fts_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER entries_fts_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER entries_fts_au AFTER UPDATE OF title, body ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO entries_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE VIRTUAL TABLE transcripts_fts USING fts5(text, content='transcripts', content_rowid='id');

CREATE TRIGGER transcripts_fts_ai AFTER INSERT ON transcripts BEGIN
  INSERT INTO transcripts_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER transcripts_fts_ad AFTER DELETE ON transcripts BEGIN
  INSERT INTO transcripts_fts(transcripts_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER transcripts_fts_au AFTER UPDATE OF text ON transcripts BEGIN
  INSERT INTO transcripts_fts(transcripts_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO transcripts_fts(rowid, text) VALUES (new.id, new.text);
END;

-- ---------------------------------------------------------------- v4:
-- the observed tier. Bulk-imported records that never pass the review gate.
-- Kept byte-identical to migrations/0004_observed_tier.sql so a fresh init
-- and a migrated database produce the same sqlite_master.

-- One row per importer run: the unit of provenance, liveness and undo.
CREATE TABLE imports (
  id          INTEGER PRIMARY KEY,
  uuid        TEXT NOT NULL UNIQUE,
  connector   TEXT NOT NULL,          -- 'ai_chat', 'imessage', 'gcal', ...
  account     TEXT,                   -- which account/mailbox/phone, when known
  source_file TEXT,                   -- export file, when file-based
  source_hash TEXT,                   -- content hash; an unchanged export is a no-op
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT NOT NULL DEFAULT 'running'
    CHECK (status IN ('running','applied','failed','retracted')),
  counts_json TEXT,                   -- {new, updated, refreshed, identical, conflicts}
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_imports_connector ON imports(connector, started_at);

-- Bulk-imported records. Lifecycle columns deliberately mirror health_records
-- so both halves of the observed tier behave identically.
--
-- import_id is the import that CREATED the row, not the last one to touch it:
-- a later export refreshing an existing message does not move it. That is what
-- makes `observe retract` a precise undo for snapshot connectors, which re-scan
-- everything on every run.
CREATE TABLE observations (
  id               INTEGER PRIMARY KEY,
  uuid             TEXT NOT NULL UNIQUE,
  stream           TEXT NOT NULL,     -- 'claude_chat','chatgpt_chat','imessage',...
  import_id        INTEGER NOT NULL REFERENCES imports(id),
  journal_date     TEXT NOT NULL REFERENCES days(journal_date),
  started_at       TEXT NOT NULL,     -- UTC ISO-8601
  ended_at         TEXT,              -- NULL for point-in-time records
  utc_offset_sec   INTEGER,           -- record's own zone offset, when known
  direction        TEXT CHECK (direction IN ('in','out','self')),
  title            TEXT,              -- subject, event name, merchant, conversation
  body             TEXT,              -- message text, note body, chat turn
  value            REAL,              -- amount, duration, count
  unit             TEXT,
  value_json       TEXT,              -- structured detail, canonical JSON
  media_path       TEXT,              -- external file; media never enters the db
  origin_app       TEXT,
  external_id      TEXT NOT NULL,     -- source system's own stable id
  last_modified_at TEXT,              -- source system's edit clock, for newer-wins
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

CREATE VIRTUAL TABLE observations_fts USING fts5(title, body, content='observations', content_rowid='id');

CREATE TRIGGER observations_fts_ai AFTER INSERT ON observations BEGIN
  INSERT INTO observations_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER observations_fts_ad AFTER DELETE ON observations BEGIN
  INSERT INTO observations_fts(observations_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER observations_fts_au AFTER UPDATE OF title, body ON observations BEGIN
  INSERT INTO observations_fts(observations_fts, rowid, title, body) VALUES ('delete', old.id, old.title, old.body);
  INSERT INTO observations_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

-- The bridge between machine identifiers and the entity graph.
--
-- Importers emit handles, never names: a phone number, an email address, an
-- app account id. Resolution is then a join, so confirming ONE handle resolves
-- every observation that ever referenced it, past and future, with no backfill
-- pass. Review cost is O(contacts), not O(messages).
--
-- entity_id NULL means "seen, not yet resolved" — a work queue, not an error.
-- Deliberate contrast with entity_aliases, whose PK (alias, entity_id) makes
-- ambiguity a supported state: a phone number is not ambiguous, and that is
-- the entire reason this table exists.
CREATE TABLE entity_handles (
  kind              TEXT NOT NULL CHECK (kind IN
    ('phone','email','calendar','app_account','card','place_id')),
  value             TEXT NOT NULL COLLATE NOCASE,
  entity_id         INTEGER REFERENCES entities(id) ON DELETE CASCADE,
  display_name      TEXT,             -- what the source system called it, for review
  confirmed_at      TEXT,             -- NULL until the owner confirms the mapping
  ignored           INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0,1)),
  observation_count INTEGER NOT NULL DEFAULT 0,
  first_seen_at     TEXT NOT NULL,
  last_seen_at      TEXT NOT NULL,
  created_at        TEXT NOT NULL,
  PRIMARY KEY (kind, value),
  CHECK (NOT (ignored = 1 AND entity_id IS NOT NULL))
);
CREATE INDEX idx_handles_entity ON entity_handles(entity_id);

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
-- entry_sources cites a transcript passage. Kept as its own table so the
-- working entry_sources path is untouched.
CREATE TABLE entry_observations (
  entry_id       INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  observation_id INTEGER NOT NULL REFERENCES observations(id),
  char_start     INTEGER,
  char_end       INTEGER,
  PRIMARY KEY (entry_id, observation_id)
);

-- ---------------------------------------------------------------- v5:
-- entity <-> entity relationships (confirmed tier). Kept identical to
-- migrations/0005_entity_links.sql, seed rows included, so a fresh init and a
-- migrated database produce the same sqlite_master and the same types.

-- Extensible without migration: new rows, not new CHECK constraints.
-- `symmetric` types read the same both ways; `inverse` pairs two directed
-- types that state one fact from either end (parent_of / child_of).
CREATE TABLE entity_link_types (
  name        TEXT PRIMARY KEY,
  symmetric   INTEGER NOT NULL DEFAULT 0 CHECK (symmetric IN (0,1)),
  inverse     TEXT REFERENCES entity_link_types(name),
  description TEXT NOT NULL,          -- how "FROM type TO" reads
  created_at  TEXT NOT NULL,
  CHECK (NOT (symmetric = 1 AND inverse IS NOT NULL))
);

-- One statement, so the parent_of <-> child_of pair satisfies its own foreign
-- keys (immediate FK checks run at the end of the statement).
INSERT INTO entity_link_types (name, symmetric, inverse, description, created_at) VALUES
  ('knows',        1, NULL,        'FROM and TO know each other',                          strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('friend_of',    1, NULL,        'FROM and TO are friends',                              strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('partner_of',   1, NULL,        'FROM and TO are partners',                             strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('family_of',    1, NULL,        'FROM and TO are family (relation unspecified)',        strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('parent_of',    0, 'child_of',  'FROM is a parent of TO',                               strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('child_of',     0, 'parent_of', 'FROM is a child of TO',                                strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('sibling_of',   1, NULL,        'FROM and TO are siblings',                             strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('colleague_of', 1, NULL,        'FROM and TO work together',                            strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('manages',      0, NULL,        'FROM manages TO',                                      strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('works_at',     0, NULL,        'FROM works (or worked) at TO',                         strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('member_of',    0, NULL,        'FROM is a member of TO (a team, club, group, gym)',    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('lives_in',     0, NULL,        'FROM lives in TO',                                     strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('from',         0, NULL,        'FROM is from TO (grew up there; a hometown)',          strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('located_in',   0, NULL,        'FROM is located in TO (an office in a city)',          strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('part_of',      0, NULL,        'FROM is part of TO (a castle within a town, a sub-project)', strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  ('relates',      1, NULL,        'FROM and TO are related somehow (loose association)',  strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));

-- Direction reads as a sentence, subject first:
--   Jake works_at Acme        Jake from El Paso         Jake lives_in Austin
--   Priya manages Alex        Linda parent_of Alex      Sam member_of Movement
--   Acme located_in Austin    Bled Castle part_of Lake Bled
-- Symmetric types are stored ONCE, in canonical order (lower entity id
-- first), so "Alex partner_of Maya" and "Maya partner_of Alex" are one row
-- and the active-uniqueness index below can see the duplicate. An inverse
-- pair is deduplicated by the domain layer and verified by doctor.
--
-- Provenance — the passage (source_id + char span), the entry it came with,
-- and/or the observation it was read from. At least one is required for an
-- extraction-created link (domain-enforced, doctor-verified); a link the
-- owner typed is its own authority. entry_uuid is ON DELETE SET NULL, not
-- CASCADE: admin purge-day hard-deletes entries, and a relationship outlives
-- the entry it arrived with — the source passage still carries it.
-- observation_uuid has no ON DELETE action on purpose: a mirrored source
-- deletion must not silently take a confirmed record's citation with it.
--
-- status='retracted' is the undo (never a DELETE). Retracted rows may keep
-- pointing at a merged entity; active rows may not (doctor).
CREATE TABLE entity_links (
  id                INTEGER PRIMARY KEY,
  uuid              TEXT NOT NULL UNIQUE,
  from_entity_id    INTEGER NOT NULL REFERENCES entities(id),
  to_entity_id      INTEGER NOT NULL REFERENCES entities(id),
  type              TEXT NOT NULL REFERENCES entity_link_types(name),
  note              TEXT,
  valid_from        TEXT,              -- when it began, if stated (ISO date/datetime)
  valid_to          TEXT,              -- when it ended ("used to work at")
  status            TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','retracted')),
  actor             TEXT NOT NULL CHECK (actor IN ('user','extraction','system')),
  source_id         INTEGER REFERENCES sources(id),
  char_start        INTEGER,
  char_end          INTEGER,
  entry_uuid        TEXT REFERENCES entries(uuid) ON DELETE SET NULL,
  extraction_run_id INTEGER REFERENCES extraction_runs(id),
  observation_uuid  TEXT REFERENCES observations(uuid),
  confirmed_at      TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  CHECK (from_entity_id <> to_entity_id),
  CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_from <= valid_to),
  CHECK ((char_start IS NULL) = (char_end IS NULL)),
  CHECK (char_start IS NULL OR (source_id IS NOT NULL
                                AND char_start >= 0 AND char_start < char_end))
);
CREATE UNIQUE INDEX idx_entity_links_active
  ON entity_links(from_entity_id, to_entity_id, type) WHERE status = 'active';
CREATE INDEX idx_entity_links_to ON entity_links(to_entity_id);
CREATE INDEX idx_entity_links_entry ON entity_links(entry_uuid);

-- ---------------------------------------------------------------- v6:
-- the assistant layer's state (docs/assistant.md). Kept identical to
-- migrations/0006_assistant_state.sql so a fresh init and a migrated database
-- produce the same sqlite_master.

-- One row per nudge key (due:<entry uuid>, birthday:<entity id>:<year>,
-- followup:<observation uuid>, lapsed:<entity id>, drift:<series>,
-- nutrient:<key>, stale:<connector>, goal:<entry uuid>). A snooze carries the
-- date it ends; a dismissal has none. `restore` deletes the row (the audit log
-- keeps what it was).
CREATE TABLE nudge_states (
  nudge_key  TEXT PRIMARY KEY,
  state      TEXT NOT NULL CHECK (state IN ('snoozed','dismissed')),
  until      TEXT,
  note       TEXT,
  actor      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK ((state = 'snoozed') = (until IS NOT NULL))
);

-- One row per calendar component ever published, by UID. `fingerprint` hashes
-- the component without its DTSTAMP, SEQUENCE and LAST-MODIFIED; a changed
-- fingerprint, or a component that comes back after being left out, moves
-- `sequence` up and `last_modified` to the publication instant (RFC 5545
-- 3.8.7.4). A component left out keeps its row as a tombstone (present = 0),
-- so it can never come back at a sequence a client has already seen.
CREATE TABLE reminder_revisions (
  uid           TEXT PRIMARY KEY,
  sequence      INTEGER NOT NULL CHECK (sequence >= 0),
  fingerprint   TEXT NOT NULL,
  last_modified TEXT NOT NULL,
  present       INTEGER NOT NULL CHECK (present IN (0,1)),
  updated_at    TEXT NOT NULL
);

-- ---------------------------------------------------------------- v7:
-- declared import coverage (docs/assistant.md). Kept identical to
-- migrations/0007_import_coverage.sql so a fresh init and a migrated database
-- produce the same sqlite_master.

-- One row per declared interval: this import, this stream (and account,
-- when the source names one), these days, and the guarantee it rests on.
-- An interval counts only while its import is 'applied' (a retracted import
-- establishes nothing); the series layer (lifecrm/series.py) reads it, drift
-- and goals say "insufficient coverage" where none applies.
CREATE TABLE import_coverage (
  import_id  INTEGER NOT NULL REFERENCES imports(id),
  account    TEXT,
  stream     TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date   TEXT NOT NULL,
  basis      TEXT NOT NULL,
  PRIMARY KEY (import_id, stream, start_date, end_date),
  CHECK (start_date <= end_date)
);
CREATE INDEX idx_import_coverage_stream ON import_coverage(stream, start_date);

-- ---------------------------------------------------------------- v8:
-- the daily collection's outcomes (docs/collect.md). Kept identical to
-- migrations/0008_collect_runs.sql so a fresh init and a migrated database
-- produce the same sqlite_master.

-- One row per source per run. `run_uuid` groups the sources of one
-- invocation; `source` is a connector name or 'health'; `hold` says which
-- rule held it; `reason` is the one line that says why it was held, found
-- nothing or failed, and what to run; `import_uuid` names the import an
-- applied observed-tier run created (health imports have no import row);
-- `counts_json` what the plan held: added, updated, removed, conflicts.
CREATE TABLE collect_runs (
  id          INTEGER PRIMARY KEY,
  run_uuid    TEXT NOT NULL,
  source      TEXT NOT NULL,
  outcome     TEXT NOT NULL
    CHECK (outcome IN ('applied','unchanged','held','missing','error')),
  hold        TEXT CHECK (hold IN ('first_import','mass_removal','invalid')),
  reason      TEXT,
  target      TEXT,
  import_uuid TEXT,
  counts_json TEXT,
  started_at  TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  CHECK ((outcome = 'held') = (hold IS NOT NULL)),
  CHECK ((outcome IN ('applied','unchanged')) = (reason IS NULL))
);
CREATE INDEX idx_collect_runs_source ON collect_runs(source, id);

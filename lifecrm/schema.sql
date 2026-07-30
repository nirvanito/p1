-- Life CRM schema v3.
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

-- Schema v4 — the observed tier.
--
-- Two tiers, one rule. `entries`/`measurements`/`mentions` are the CONFIRMED
-- tier: every row passed the staging -> ingest -> review -> apply gate. The
-- tables below are the OBSERVED tier: bulk-imported, never user-reviewed,
-- never asserted as fact without citing the row. An importer may not write
-- entries, measurements, mentions or entity_aliases — ever.
--
-- This generalizes the split F8 already made for `health_records` (what the
-- sensor recorded) versus `measurements` (what the journal said). health_records
-- stays exactly as it is: it is metric-shaped, observations are event-shaped,
-- and merging them would leave half of every row null. They share a contract
-- and a lifecycle, not a row shape.
--
-- See docs/observed-tier.md for the design and the decisions behind it.

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

-- Schema v2: multi-source extraction runs, closable loops, entry links.
-- Ships contract v2 (the meta update at the bottom rides in this migration's
-- transaction — the runner only maintains schema_version).

-- Allowed inside a transaction (unlike PRAGMA foreign_keys, which is a no-op
-- there); defers FK enforcement to COMMIT so the extraction_runs rebuild below
-- is safe even on a populated database. Resets automatically at COMMIT.
PRAGMA defer_foreign_keys=ON;

-- ---- Closable loops: columns appended so ALTER matches a fresh v2 init.
-- The both-set-or-both-null invariant is domain-enforced + doctor-verified
-- (ALTER cannot add a table-level CHECK, and migrated vs fresh must not drift).
ALTER TABLE entries ADD COLUMN closed_at TEXT;
ALTER TABLE entries ADD COLUMN closure TEXT CHECK (closure IN ('done','dropped','obsolete'));

-- ---- Multi-source runs: rebuild extraction_runs without the singular
-- source_id (kept nowhere — no denormalized pointer to drift), then a join
-- table carrying the old pointers.
--
-- Rebuild order matters under deferred FKs: SQLite tracks violations with a
-- counter that only DECREMENTS when parent keys reappear via INSERTs into the
-- actual parent table. A create-copy-drop-RENAME dance leaves the counter
-- stuck (renames don't replay inserts) and COMMIT fails. So: stash, drop,
-- recreate under the final name, reinsert — each reinserted row settles the
-- child rows (entry_sources, measurements) counted by the DROP.
CREATE TABLE migration_stash_0002 AS
  SELECT id, uuid, provider, model, interface, contract_version, input_hash,
         started_at, reviewed_at, status, source_id
  FROM extraction_runs;

DROP TABLE extraction_runs;

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

INSERT INTO extraction_runs (id, uuid, provider, model, interface,
                             contract_version, input_hash, started_at,
                             reviewed_at, status)
  SELECT id, uuid, provider, model, interface, contract_version, input_hash,
         started_at, reviewed_at, status
  FROM migration_stash_0002;

CREATE TABLE extraction_run_sources (
  extraction_run_id INTEGER NOT NULL REFERENCES extraction_runs(id),
  source_id         INTEGER NOT NULL REFERENCES sources(id),
  PRIMARY KEY (extraction_run_id, source_id)
);

INSERT INTO extraction_run_sources (extraction_run_id, source_id)
  SELECT id, source_id FROM migration_stash_0002 WHERE source_id IS NOT NULL;

DROP TABLE migration_stash_0002;

-- ---- Entry links: thread structure captured while the memory is fresh.
-- Direction: later continues earlier; new_event resolves old_task;
-- child part_of parent; cause caused effect; newer contradicts older.
-- ON DELETE CASCADE is required (purge-day hard-deletes entries with FKs ON);
-- entry_revisions.entry_uuid deliberately has NO FK — history outlives purges.
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

-- ---- Contract v2 ships with this schema.
UPDATE meta SET value='2' WHERE key='contract_version';

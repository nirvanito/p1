-- Schema v5 — entity <-> entity relationships.
--
-- Mentions say an entry was ABOUT an entity; nothing said how two entities
-- relate to each other, so "where is Jake from" and "who does Jake know" could
-- only be answered by re-reading prose. entity_links is that edge: typed,
-- directed, and held to the same provenance discipline as the entries it is
-- usually extracted with.
--
-- Confirmed tier, like mentions. A relationship exists because the owner
-- typed it (`entities link add`) or reviewed it (a draft's `entity_links`,
-- through ingest -> review -> apply). An importer may not write one.
--
-- Contract v2 gains an optional top-level key with this schema; drafts
-- without it are unchanged, so contract_version stays 2.

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

-- Schema v8 — the daily collection's outcomes (docs/collect.md).
--
-- `lifecrm collect` runs, by itself every morning, the imports the owner
-- would otherwise run by hand: the sources `[collect] sources` names, each
-- from the file or folder its connector finds with no path given. Routine
-- changes are applied; anything unusual is held — a source never imported
-- before, a removal large enough to look like an incomplete source, records
-- the reader cannot validate — and waits for the owner's own run. A hold
-- writes nothing to the records, so what the owner needs to see has to be
-- kept somewhere: here, one row per source per run, read by `collect
-- --status`, doctor and the nudges pack (/morning). Bookkeeping about the
-- imports, not a record: no provenance, written through
-- lifecrm/domain/collect.py and audited.

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

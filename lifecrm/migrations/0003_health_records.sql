-- Schema v3: health_records — deterministic sensor/app imports (Health
-- Connect first). A second metrics layer beside `measurements`, which stays
-- the home of journal-extracted, human-stated facts. These rows differ in
-- kind: they carry an external identity (the source system's record uuid),
-- real time ranges, and upsert semantics keyed on (metric, external_id) so a
-- re-imported export never duplicates. Non-destructive rules still apply:
-- updates land only from a newer last_modified_at and are audit-logged; the
-- export file named in source_file is the provenance anchor and is never
-- consumed or altered by import.
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

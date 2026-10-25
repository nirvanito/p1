-- Schema v7 — declared import coverage (docs/assistant.md, "Coverage").
--
-- A day with no workout row is not a day without a workout: the export may
-- not have covered it. Activity dates cannot establish completeness, and
-- observations.import_id names the import that CREATED a row, not every
-- later export that contained it, so a span can never be rebuilt from rows.
-- What can establish it is a guarantee in the source format: an OFX
-- statement's own DTSTART/DTEND, an SMS backup marked type="full" whose
-- count matches, a phone backup that is the device's whole Messages store, a
-- fitness app's full-history export. A connector that has such a guarantee
-- declares the interval; everything else leaves the days unknown.

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

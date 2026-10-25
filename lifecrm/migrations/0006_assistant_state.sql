-- Schema v6 — the assistant layer's two pieces of state (docs/assistant.md).
--
-- `lifecrm nudges` reads the database and says what matters today: what is
-- due, whose birthday is near, which import went stale. Everything it says
-- is recomputed from the records; the only thing the owner tells it is "not
-- now" (snooze) or "never" (dismiss), and that must survive the next run.
-- The reminders calendar (`lifecrm reminders publish`) is regenerated from
-- the same records, and a calendar client only notices a changed event when
-- its SEQUENCE moves, so each published component's revision must be kept
-- across publications. Both are state about records, not records: neither
-- carries provenance, both are written through lifecrm/domain/ and audited.

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

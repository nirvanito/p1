"""The observed tier: bulk-imported records that never pass the review gate.

`entries`/`measurements`/`mentions` are the confirmed tier — every row was
reviewed by the owner before it landed. `observations` is the observed tier:
tens of thousands of messages, events and transactions that no one will ever
review one by one. They are never asserted as fact without citing the row, and
**no importer may write entries, measurements, mentions or entity_aliases.**

This module is the whole write layer for that tier. Connectors are pure
readers (see `lifecrm/connectors/base.py`); everything below — dedupe,
classification, transactional apply, retraction — is written once here and
shared by every connector. The classification rules are lifted from
`domain/health.py`, which established them in F8:

- identical:  every data field matches — skip.
- refreshed:  data matches but the source's last_modified_at moved — refresh
              that one field silently (metadata, not data; no audit noise).
- update:     data differs AND the incoming last_modified_at is strictly
              newer — apply, audit-logged with old and new values.
- conflict:   data differs but the incoming record is NOT newer — never
              applied, surfaced in the import report instead.

Identity is (stream, external_id) — the source system's own record id — so
re-importing an export is always safe.
"""

from __future__ import annotations

import json
import sqlite3

from . import audit, days
from .util import DomainError, canonical_json, new_uuid, now_utc

__all__ = [
    "COMPARE_FIELDS", "HANDLE_KINDS", "ROLES", "validate", "existing_by_key",
    "classify", "start_import", "finish_import", "build_plan", "apply_plan",
    "retract_import", "restore_import", "refresh_handle_stats", "unresolved_handles",
    "link_handle", "ignore_handle", "import_by_uuid", "mass_removal",
]

# Fields that constitute the record's data (identity and bookkeeping excluded).
COMPARE_FIELDS = ("journal_date", "started_at", "ended_at", "utc_offset_sec",
                  "direction", "title", "body", "value", "unit", "value_json",
                  "media_path", "origin_app")

HANDLE_KINDS = ("phone", "email", "calendar", "app_account", "card", "place_id")
ROLES = ("sender", "recipient", "participant", "organizer", "location",
         "counterparty")
DIRECTIONS = ("in", "out", "self")

# Mirroring. A connector whose every scan is its source's complete current
# state may declare which ids the source still holds (`present_ids`); archived
# rows it no longer holds are then removed, so the archive mirrors the source
# instead of remembering it. A removal this large at once looks like an
# incomplete source — a partial or cloud-optimized backup — more than like
# deletions, and is refused unless explicitly allowed.
MASS_REMOVAL_MIN = 100
MASS_REMOVAL_SHARE = 0.10


# ---------------------------------------------------------------- validation

def validate(rec: dict) -> str | None:
    """Single-line error or None. `rec` carries value_json already canonical."""
    for key in ("stream", "journal_date", "started_at", "external_id"):
        if not isinstance(rec.get(key), str) or not rec[key].strip():
            return f"observation needs a non-empty '{key}'"
    where = f"{rec['stream']} {rec['external_id'][:16]}"
    if rec.get("direction") is not None and rec["direction"] not in DIRECTIONS:
        return (f"observation {where} has direction '{rec['direction']}' "
                f"(expected one of {', '.join(DIRECTIONS)})")
    if rec.get("ended_at") is not None and rec["ended_at"] < rec["started_at"]:
        return (f"observation {where} ends before it starts "
                f"({rec['ended_at']} < {rec['started_at']})")
    if rec.get("value") is not None and not isinstance(rec["value"], (int, float)):
        return f"observation {where} value must be a number"
    if all(rec.get(f) in (None, "") for f in ("title", "body", "value", "value_json")):
        return (f"observation {where} has no content — needs at least one of "
                f"title, body, value, value_json")
    for p in rec.get("participants", ()):
        if p.get("kind") not in HANDLE_KINDS:
            return (f"observation {where} names handle kind '{p.get('kind')}' "
                    f"(expected one of {', '.join(HANDLE_KINDS)})")
        if p.get("role") not in ROLES:
            return (f"observation {where} names role '{p.get('role')}' "
                    f"(expected one of {', '.join(ROLES)})")
        if not isinstance(p.get("value"), str) or not p["value"].strip():
            return f"observation {where} has a participant with no handle value"
    return None


# ---------------------------------------------------------------- classify

def existing_by_key(conn: sqlite3.Connection,
                    streams: tuple[str, ...] | None = None
                    ) -> dict[tuple[str, str], sqlite3.Row]:
    """Existing rows keyed by (stream, external_id). Scoped to the connector's
    own streams so a large database does not load every observation."""
    if streams:
        marks = ",".join("?" for _ in streams)
        rows = conn.execute(
            f"SELECT * FROM observations WHERE stream IN ({marks})", streams)
    else:
        rows = conn.execute("SELECT * FROM observations")
    return {(r["stream"], r["external_id"]): r for r in rows}


def classify(row: sqlite3.Row, rec: dict) -> tuple[str, dict]:
    """('identical'|'refreshed'|'update'|'conflict', {field: (old, new)})."""
    changes = {f: (row[f], rec.get(f)) for f in COMPARE_FIELDS
               if row[f] != rec.get(f)}
    if not changes:
        return (("refreshed" if rec.get("last_modified_at") != row["last_modified_at"]
                 else "identical"), {})
    incoming, current = rec.get("last_modified_at"), row["last_modified_at"]
    if incoming is not None and (current is None or incoming > current):
        return "update", changes
    return "conflict", changes


# ---------------------------------------------------------------- import runs

def start_import(conn: sqlite3.Connection, connector: str, *,
                 account: str | None = None, source_file: str | None = None,
                 source_hash: str | None = None) -> tuple[int, str]:
    if not connector or not connector.strip():
        raise DomainError("import needs a connector name")
    uid, now = new_uuid(), now_utc()
    cur = conn.execute(
        "INSERT INTO imports (uuid, connector, account, source_file, "
        "source_hash, started_at, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
        (uid, connector, account, source_file, source_hash, now, now))
    return cur.lastrowid, uid


def finish_import(conn: sqlite3.Connection, import_id: int, status: str,
                  counts: dict, actor: str = "import") -> None:
    if status not in ("applied", "failed"):
        raise DomainError(f"import cannot finish as '{status}'")
    row = conn.execute("SELECT uuid, connector, source_file FROM imports "
                       "WHERE id=?", (import_id,)).fetchone()
    conn.execute("UPDATE imports SET status=?, finished_at=?, counts_json=? "
                 "WHERE id=?",
                 (status, now_utc(), canonical_json(counts), import_id))
    audit.log(conn, actor, status, "imports", row["uuid"],
              {"connector": row["connector"], "source_file": row["source_file"],
               **counts})


def import_by_uuid(conn: sqlite3.Connection, import_uuid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM imports WHERE uuid=?",
                       (import_uuid,)).fetchone()
    if row is None:
        raise DomainError(f"no import with uuid {import_uuid}")
    return row


# ---------------------------------------------------------------- row writes

def _insert(conn: sqlite3.Connection, rec: dict, import_id: int,
            actor: str = "import") -> tuple[int, str]:
    err = validate(rec)
    if err:
        raise DomainError(err)
    days.ensure_day(conn, rec["journal_date"])
    uid, now = new_uuid(), now_utc()
    cur = conn.execute(
        "INSERT INTO observations (uuid, stream, import_id, journal_date, "
        "started_at, ended_at, utc_offset_sec, direction, title, body, value, "
        "unit, value_json, media_path, origin_app, external_id, "
        "last_modified_at, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (uid, rec["stream"], import_id, rec["journal_date"], rec["started_at"],
         rec.get("ended_at"), rec.get("utc_offset_sec"), rec.get("direction"),
         rec.get("title"), rec.get("body"), rec.get("value"), rec.get("unit"),
         rec.get("value_json"), rec.get("media_path"), rec.get("origin_app"),
         rec["external_id"], rec.get("last_modified_at"), now, now))
    return cur.lastrowid, uid


def _update(conn: sqlite3.Connection, row: sqlite3.Row, rec: dict,
            changes: dict, actor: str = "import") -> None:
    err = validate(rec)
    if err:
        raise DomainError(err)
    days.ensure_day(conn, rec["journal_date"])
    conn.execute(
        "UPDATE observations SET journal_date=?, started_at=?, ended_at=?, "
        "utc_offset_sec=?, direction=?, title=?, body=?, value=?, unit=?, "
        "value_json=?, media_path=?, origin_app=?, last_modified_at=?, "
        "updated_at=? WHERE uuid=?",
        (rec["journal_date"], rec["started_at"], rec.get("ended_at"),
         rec.get("utc_offset_sec"), rec.get("direction"), rec.get("title"),
         rec.get("body"), rec.get("value"), rec.get("unit"),
         rec.get("value_json"), rec.get("media_path"), rec.get("origin_app"),
         rec.get("last_modified_at"), now_utc(), row["uuid"]))
    audit.log(conn, actor, "update", "observations", row["uuid"],
              {"stream": rec["stream"], "external_id": rec["external_id"],
               "changes": {f: {"old": old, "new": new}
                           for f, (old, new) in changes.items()}})


def _refresh_modified(conn: sqlite3.Connection, row: sqlite3.Row,
                      rec: dict) -> None:
    """Data unchanged, only the source's own edit clock moved. Metadata, not a
    revision — deliberately not audit-logged to keep the log meaningful."""
    conn.execute("UPDATE observations SET last_modified_at=?, updated_at=? "
                 "WHERE uuid=?",
                 (rec.get("last_modified_at"), now_utc(), row["uuid"]))


# ---------------------------------------------------------------- handles

def _upsert_handle(conn: sqlite3.Connection, h: dict) -> None:
    """Record that a handle exists. Never resolves it to an entity — that is
    always an explicit user decision (`lifecrm handles link`)."""
    if h.get("kind") not in HANDLE_KINDS:
        raise DomainError(f"unknown handle kind '{h.get('kind')}' "
                          f"(expected one of {', '.join(HANDLE_KINDS)})")
    if not isinstance(h.get("value"), str) or not h["value"].strip():
        raise DomainError(f"handle of kind '{h['kind']}' has no value")
    now = now_utc()
    conn.execute(
        "INSERT INTO entity_handles (kind, value, display_name, first_seen_at, "
        "last_seen_at, created_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, value) DO UPDATE SET "
        "display_name=COALESCE(entity_handles.display_name, excluded.display_name)",
        (h["kind"], h["value"], h.get("display_name"), now, now, now))


def _link_participants(conn: sqlite3.Connection, observation_id: int,
                       participants: list[dict]) -> None:
    conn.execute("DELETE FROM observation_participants WHERE observation_id=?",
                 (observation_id,))
    for p in participants:
        conn.execute(
            "INSERT OR IGNORE INTO observation_participants "
            "(observation_id, handle_kind, handle_value, role) VALUES (?, ?, ?, ?)",
            (observation_id, p["kind"], p["value"], p.get("role", "participant")))


def refresh_handle_stats(conn: sqlite3.Connection) -> None:
    """Recompute counts and seen-dates from the observations themselves.

    Recomputed rather than incremented: a counter that drifts is worse than no
    counter, and this is the number the review queue is ranked by.
    """
    conn.execute("""
        UPDATE entity_handles SET
          observation_count = (
            SELECT COUNT(*) FROM observation_participants p
            JOIN observations o ON o.id = p.observation_id
            WHERE p.handle_kind = entity_handles.kind
              AND p.handle_value = entity_handles.value
              AND o.status = 'active'),
          first_seen_at = COALESCE((
            SELECT MIN(o.started_at) FROM observation_participants p
            JOIN observations o ON o.id = p.observation_id
            WHERE p.handle_kind = entity_handles.kind
              AND p.handle_value = entity_handles.value
              AND o.status = 'active'), entity_handles.created_at),
          last_seen_at = COALESCE((
            SELECT MAX(o.started_at) FROM observation_participants p
            JOIN observations o ON o.id = p.observation_id
            WHERE p.handle_kind = entity_handles.kind
              AND p.handle_value = entity_handles.value
              AND o.status = 'active'), entity_handles.created_at)
    """)


def unresolved_handles(conn: sqlite3.Connection, limit: int = 50,
                       include_ignored: bool = False) -> list[sqlite3.Row]:
    """The review queue: handles with no entity yet, noisiest first."""
    clause = "" if include_ignored else " AND ignored = 0"
    return conn.execute(
        "SELECT * FROM entity_handles WHERE entity_id IS NULL" + clause +
        " ORDER BY observation_count DESC, kind, value LIMIT ?",
        (limit,)).fetchall()


def link_handle(conn: sqlite3.Connection, kind: str, value: str,
                entity_id: int, actor: str = "user") -> None:
    """Resolve one handle to one entity.

    This is the whole point of the handle table: every observation that ever
    referenced this handle resolves through the join immediately, past and
    future, with no backfill pass.
    """
    row = conn.execute("SELECT * FROM entity_handles WHERE kind=? AND value=?",
                       (kind, value)).fetchone()
    if row is None:
        raise DomainError(f"no handle {kind}:{value} — nothing has referenced it")
    ent = conn.execute("SELECT id, name, status FROM entities WHERE id=?",
                       (entity_id,)).fetchone()
    if ent is None:
        raise DomainError(f"no entity with id {entity_id}")
    if ent["status"] != "active":
        raise DomainError(f"entity {entity_id} ({ent['name']}) is "
                          f"{ent['status']} — link to the surviving entity")
    conn.execute("UPDATE entity_handles SET entity_id=?, confirmed_at=?, "
                 "ignored=0 WHERE kind=? AND value=?",
                 (entity_id, now_utc(), kind, value))
    audit.log(conn, actor, "link", "entity_handles", None,
              {"kind": kind, "value": value, "entity_id": entity_id,
               "entity": ent["name"], "previous_entity_id": row["entity_id"],
               "observations": row["observation_count"]})


def ignore_handle(conn: sqlite3.Connection, kind: str, value: str,
                  actor: str = "user") -> None:
    """Mark a handle as never worth resolving (spam, delivery notifications).
    Reversible: linking it later clears the flag."""
    row = conn.execute("SELECT * FROM entity_handles WHERE kind=? AND value=?",
                       (kind, value)).fetchone()
    if row is None:
        raise DomainError(f"no handle {kind}:{value} — nothing has referenced it")
    conn.execute("UPDATE entity_handles SET ignored=1, entity_id=NULL, "
                 "confirmed_at=NULL WHERE kind=? AND value=?", (kind, value))
    audit.log(conn, actor, "ignore", "entity_handles", None,
              {"kind": kind, "value": value,
               "observations": row["observation_count"]})


# ---------------------------------------------------------------- plan / apply

def build_plan(conn: sqlite3.Connection, scan: dict) -> dict:
    """Compare a connector's scan against the database. Reads only — this is
    what the dry-run report is built from."""
    streams = tuple(sorted({r.get("stream") for r in scan["records"]
                            if r.get("stream")}))
    existing = existing_by_key(conn, streams)
    seen: set[tuple[str, str]] = set()
    new, updates, refreshed, conflicts, invalid = [], [], [], [], []
    identical = dup_incoming = 0
    for rec in scan["records"]:
        err = validate(rec)
        if err:
            invalid.append(err)
            continue
        key = (rec["stream"], rec["external_id"])
        if key in seen:
            dup_incoming += 1
            continue
        seen.add(key)
        row = existing.get(key)
        if row is None:
            new.append(rec)
            continue
        kind, changes = classify(row, rec)
        if kind == "identical":
            identical += 1
        elif kind == "refreshed":
            refreshed.append((row, rec))
        elif kind == "update":
            updates.append((row, rec, changes))
        else:
            conflicts.append({"stream": rec["stream"],
                              "external_id": rec["external_id"],
                              "changes": {f: {"existing": old, "incoming": neu}
                                          for f, (old, neu) in changes.items()}})
    return {"new": new, "updates": updates, "refreshed": refreshed,
            "conflicts": conflicts, "identical": identical,
            "dup_incoming": dup_incoming, "invalid": invalid,
            "streams": list(streams), **_mirror_plan(conn, scan)}


def _mirror_plan(conn: sqlite3.Connection, scan: dict) -> dict:
    """What a mirroring scan removes: archived rows, first imported from the
    same connector and account, in the connector's own streams, whose id the
    source no longer holds.

    Scoped to the account so a second phone never deletes the first's
    messages, and keyed on what the source *holds*, not on what the connector
    chose to emit — a message the connector cannot read today is still on
    the phone, and must not vanish from the archive because of it.
    """
    present = scan.get("present_ids")
    if present is None:
        return {"removed": [], "mirror_scope": 0}
    if not scan.get("account"):
        # The account is what keeps one source's deletions out of another's
        # archive. An export that does not say whose it is removes nothing.
        return {"removed": [], "mirror_scope": 0,
                "mirror_note": "this source does not say which account it is, "
                               "so deletions cannot be scoped to it safely"}
    present = set(present)
    mirror_streams = tuple(scan.get("mirror_streams") or ())
    if not mirror_streams:
        return {"removed": [], "mirror_scope": 0}
    marks = ",".join("?" for _ in mirror_streams)
    rows = conn.execute(
        f"SELECT o.* FROM observations o JOIN imports i ON i.id = o.import_id "
        f"WHERE o.status = 'active' AND o.stream IN ({marks}) "
        f"AND i.connector = ? AND i.account IS ? "
        f"ORDER BY o.stream, o.started_at, o.external_id",
        (*mirror_streams, scan["connector"], scan.get("account"))).fetchall()
    return {"removed": [r for r in rows if r["external_id"] not in present],
            "mirror_scope": len(rows)}


def mass_removal(plan: dict) -> str | None:
    """The refusal message if this plan removes suspiciously much, else None."""
    n, scope = len(plan.get("removed", ())), plan.get("mirror_scope", 0)
    if n > MASS_REMOVAL_MIN and n > MASS_REMOVAL_SHARE * scope:
        return (f"this would remove {n} of {scope} archived record(s) at once — "
                f"more like an incomplete source (a partial or iCloud-optimized "
                f"backup) than deletions. Check the source; if it is really what "
                f"you want, re-run with --allow-mass-removal")
    return None


def _remove(conn: sqlite3.Connection, rows: list, actor: str) -> tuple[int, int]:
    """Mirror deletions at the source: (deleted, retracted).

    A row a confirmed entry cites as its provenance is retracted — hidden
    everywhere — rather than deleted: the confirmed tier's provenance is never
    destroyed (the lesson of entry merge, 1.1). Everything else is deleted;
    participants and the search index follow by cascade and trigger.
    """
    deleted = retracted = 0
    now = now_utc()
    for row in rows:
        cited = conn.execute("SELECT 1 FROM entry_observations WHERE "
                             "observation_id=? LIMIT 1", (row["id"],)).fetchone()
        if cited:
            conn.execute("UPDATE observations SET status='retracted', updated_at=? "
                         "WHERE id=?", (now, row["id"]))
            retracted += 1
        else:
            conn.execute("DELETE FROM observations WHERE id=?", (row["id"],))
            deleted += 1
    if rows:
        audit.log(conn, actor, "mirror_remove", "observations", None,
                  {"deleted": deleted, "retracted_cited": retracted,
                   "external_ids": sorted({r["external_id"] for r in rows})})
    return deleted, retracted


def apply_plan(conn: sqlite3.Connection, scan: dict, plan: dict,
               actor: str = "import", allow_mass_removal: bool = False) -> dict:
    """Write the plan. The caller owns the transaction; nothing here commits."""
    if plan["invalid"]:
        raise DomainError(plan["invalid"][0])
    refusal = mass_removal(plan)
    if refusal and not allow_mass_removal:
        raise DomainError(refusal)
    import_id, import_uuid = start_import(
        conn, scan["connector"], account=scan.get("account"),
        source_file=scan.get("source_file"), source_hash=scan.get("source_hash"))
    for h in scan.get("handles", ()):
        _upsert_handle(conn, h)
    for rec in plan["new"]:
        obs_id, _ = _insert(conn, rec, import_id, actor)
        _link_participants(conn, obs_id, rec.get("participants", []))
    for row, rec, changes in plan["updates"]:
        _update(conn, row, rec, changes, actor)
        _link_participants(conn, row["id"], rec.get("participants", []))
    for row, rec in plan["refreshed"]:
        _refresh_modified(conn, row, rec)
    deleted, retracted = _remove(conn, plan.get("removed", []), actor)
    refresh_handle_stats(conn)
    counts = {"added": len(plan["new"]), "updated": len(plan["updates"]),
              "refreshed": len(plan["refreshed"]),
              "identical": plan["identical"],
              "conflicts": len(plan["conflicts"])}
    if deleted or retracted:
        counts["removed"] = deleted
        if retracted:
            counts["retracted_cited"] = retracted
    finish_import(conn, import_id, "applied", counts, actor)
    return {**counts, "import_uuid": import_uuid}


def _id_ranges(ids: list[int]) -> list[list[int]]:
    """[3, 4, 5, 9] -> [[3, 5], [9, 9]]. Exact (every integer in a range was
    listed), and compact for what retraction records: one import's rows are
    inserted together, so a 50,000-message import is usually a single range."""
    ranges: list[list[int]] = []
    for i in sorted(ids):
        if ranges and i == ranges[-1][1] + 1:
            ranges[-1][1] = i
        else:
            ranges.append([i, i])
    return ranges


def retract_import(conn: sqlite3.Connection, import_uuid: str,
                   actor: str = "user") -> dict:
    """Undo an import: its observations go to status='retracted'.

    Coarse by design. Rows this import only *refreshed* or *updated* stay —
    `import_id` names the import that created the row, so retracting a later
    snapshot cannot delete records an earlier one contributed.

    The audit row records exactly which observations this hid, because
    status alone cannot say: mirroring also retracts rows of an applied import
    (a cited message deleted at the source). `restore_import` puts back these
    rows and only these.
    """
    row = import_by_uuid(conn, import_uuid)
    if row["status"] == "retracted":
        raise DomainError(f"import {import_uuid} is already retracted")
    if row["status"] != "applied":
        raise DomainError(f"import {import_uuid} is '{row['status']}' — "
                          f"only an applied import can be retracted")
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM observations WHERE import_id=? AND status='active'",
        (row["id"],))]
    n = conn.execute("UPDATE observations SET status='retracted', updated_at=? "
                     "WHERE import_id=? AND status='active'",
                     (now_utc(), row["id"])).rowcount
    conn.execute("UPDATE imports SET status='retracted' WHERE id=?", (row["id"],))
    refresh_handle_stats(conn)
    audit.log(conn, actor, "retract", "imports", import_uuid,
              {"connector": row["connector"], "source_file": row["source_file"],
               "observations_retracted": n,
               "observation_id_ranges": _id_ranges(ids)})
    return {"retracted": n, "connector": row["connector"],
            "source_file": row["source_file"]}


def _retracted_by(conn: sqlite3.Connection, imp: sqlite3.Row
                  ) -> tuple[list[list[int]], str | None]:
    """The id ranges the import's latest retraction hid, and when it ran.

    A retraction recorded before the ranges were (the one-way retract) is
    reconstructed: every retracted row of the import except any external id a
    mirror_remove audit names. That errs toward leaving a row hidden — never
    toward resurrecting one deleted at the source."""
    found = conn.execute(
        "SELECT ts, details FROM audit_log WHERE action='retract' "
        "AND table_name='imports' AND row_uuid=? ORDER BY id DESC LIMIT 1",
        (imp["uuid"],)).fetchone()
    when = found["ts"] if found else None
    details = json.loads(found["details"] or "{}") if found else {}
    if "observation_id_ranges" in details:
        return details["observation_id_ranges"], when
    mirrored: set[str] = set()
    for r in conn.execute("SELECT details FROM audit_log WHERE "
                          "action='mirror_remove' AND table_name='observations'"):
        mirrored.update(json.loads(r["details"] or "{}").get("external_ids", ()))
    ids = [r["id"] for r in conn.execute(
        "SELECT id, external_id FROM observations WHERE import_id=? "
        "AND status='retracted'", (imp["id"],)) if r["external_id"] not in mirrored]
    return _id_ranges(ids), when


def restore_import(conn: sqlite3.Connection, import_uuid: str,
                   actor: str = "user") -> dict:
    """Put back what retract_import hid — exactly that, nothing more.

    Not "every retracted row of this import": a row mirroring retracted (a
    cited message deleted at the source) belongs to an applied import too,
    and bringing it back would resurrect what the owner deleted on the phone.

    One gap is reported rather than papered over: mirroring only ever looks at
    active rows, so a mirroring import of the same account that ran while
    this one was retracted could not remove anything from it. The note says
    so; re-running observe on the current source settles it.
    """
    imp = import_by_uuid(conn, import_uuid)
    if imp["status"] != "retracted":
        raise DomainError(f"import {import_uuid} is '{imp['status']}' — only a "
                          f"retracted import can be restored")
    ranges, retracted_at = _retracted_by(conn, imp)
    now, n = now_utc(), 0
    for lo, hi in ranges:
        n += conn.execute(
            "UPDATE observations SET status='active', updated_at=? "
            "WHERE import_id=? AND status='retracted' AND id BETWEEN ? AND ?",
            (now, imp["id"], lo, hi)).rowcount
    conn.execute("UPDATE imports SET status='applied' WHERE id=?", (imp["id"],))
    refresh_handle_stats(conn)

    later = None
    if retracted_at is not None:
        later = conn.execute(
            "SELECT uuid, started_at FROM imports WHERE connector=? AND account IS ? "
            "AND id<>? AND status IN ('applied','retracted') AND started_at>=? "
            "ORDER BY id DESC LIMIT 1",
            (imp["connector"], imp["account"], imp["id"], retracted_at)).fetchone()
    note = None
    if later is not None:
        note = (f"{imp['connector']} import {later['uuid'][:8]} ran on "
                f"{later['started_at'][:10]} while this one was retracted, so "
                f"anything deleted at the source since was not removed from it — "
                f"if that source mirrors deletions (texts, AI chats), re-run "
                f"observe on it")
    audit.log(conn, actor, "restore", "imports", import_uuid,
              {"connector": imp["connector"], "source_file": imp["source_file"],
               "observations_restored": n, "observation_id_ranges": ranges})
    return {"restored": n, "connector": imp["connector"],
            "source_file": imp["source_file"], "note": note}

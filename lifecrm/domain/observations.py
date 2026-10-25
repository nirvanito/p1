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

Mirroring (see MASS_REMOVAL_MIN) adds two more: removed, for what a
mirroring source no longer holds, and reappeared, for a row it hid that the
source holds again.

A reader that changes how it *spells* a record — not what the record says —
would otherwise leave every row the old reader stored as a conflict on every
import, forever: its last_modified_at never moves, so it can never win as
newer. A connector declares that change once (`upgrade_stored`, see
`declared_upgrade`), and a stored row whose upgraded value_json is exactly
what the source sends today is a format upgrade: rewritten in place, counted,
audited once per run — not a conflict, and not an update of its content.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from . import audit, days
from .util import DomainError, canonical_json, is_iso_date, new_uuid, now_utc

__all__ = [
    "COMPARE_FIELDS", "HANDLE_KINDS", "ROLES", "validate", "existing_by_key",
    "classify", "start_import", "finish_import", "build_plan", "apply_plan",
    "retract_import", "restore_import", "refresh_handle_stats", "unresolved_handles",
    "link_handle", "ignore_handle", "import_by_uuid", "mass_removal",
    "declared_upgrade",
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
# incomplete source — a partial or cloud-optimized backup, a one-card vCard,
# a truncated export — more than like deletions, and is refused unless
# explicitly allowed: at least MASS_REMOVAL_MIN records that are more than
# MASS_REMOVAL_SHARE of the account's archive, or MASS_REMOVAL_MAJORITY of it
# whatever its size. The floor used to be 100, which let an incomplete source
# empty any archive of 100 records or fewer — an address book, a notes
# account, a calendar — without a word.
MASS_REMOVAL_MIN = 5
MASS_REMOVAL_SHARE = 0.10
MASS_REMOVAL_MAJORITY = 0.50


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


# ---------------------------------------------------------------- stored format

def declared_upgrade(connector: str | None):
    """The stored-format upgrade a connector declares, or None.

    A connector whose reader changed how it spells a record's value_json —
    a path written relative to another folder, say, with nothing it says
    changed — declares a module-level

        upgrade_stored(stream: str, value: dict) -> dict

    taking a stored row's parsed value_json and returning it as the reader
    emits it today. It must be pure and change spelling only: build_plan
    accepts its answer only when it equals, exactly, what the source sends now
    (`_upgraded`), so an upgrade can never invent content, and one that gets a
    record wrong just leaves it comparing as stored. The connector stays a
    pure reader; it never sees the database.
    """
    if not connector:
        return None
    from ..connectors import base as connectors   # lazy: connectors import the domain
    module = connectors.registry().get(connector)
    return getattr(module, "upgrade_stored", None)


def _upgraded(upgrade, row, rec: dict) -> str | None:
    """The stored row's value_json in the current format, when that is the
    whole difference between it and the incoming record's; else None.

    None as well when there is nothing to upgrade, or when the upgraded value
    still differs from the incoming one: then the row compares as stored, and
    whatever really differs is reported as it always was."""
    stored, incoming = row["value_json"], rec.get("value_json")
    if upgrade is None or not stored or stored == incoming:
        return None
    try:
        value = json.loads(stored)
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    current = canonical_json(upgrade(row["stream"], value))
    if current == stored or current != incoming:
        return None
    return current


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


def _upgrade_format(conn: sqlite3.Connection, pairs: list, connector: str,
                    import_uuid: str, actor: str) -> int:
    """Rows stored in an older spelling of what the source sends today: the
    current spelling, written in place.

    Not an update — nothing the record says changed, and the source did not
    send anything newer — so not one audit row per record with its old and
    new values. One row for the run says which records were rewritten, the
    way a mirror removal is logged: ids, never content (value_json can carry
    a message's edit history)."""
    if not pairs:
        return 0
    now = now_utc()
    per_stream: dict[str, int] = {}
    for row, value_json in pairs:
        conn.execute("UPDATE observations SET value_json=?, updated_at=? "
                     "WHERE id=?", (value_json, now, row["id"]))
        per_stream[row["stream"]] = per_stream.get(row["stream"], 0) + 1
    audit.log(conn, actor, "format_upgrade", "observations", None,
              {"connector": connector, "import": import_uuid,
               "field": "value_json", "upgraded": len(pairs),
               "streams": per_stream,
               "external_ids": sorted({row["external_id"] for row, _ in pairs})})
    return len(pairs)


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
    what the dry-run report is built from.

    `reappeared` lists the rows mirroring hid (cited, so retracted instead of
    deleted) that the source holds again; they also go through classify, so
    one that changed meanwhile updates or conflicts as usual. Without this a
    cited record could never come back: classify ignores status, so it read
    as identical and stayed hidden for good.

    `format_upgrades` lists (row, value_json) for stored rows the scanning
    connector's `upgrade_stored` brings to exactly the incoming value_json
    (see declared_upgrade). Such a row is classified with its upgraded value,
    so an old spelling is never a conflict; it is otherwise identical
    (counted here instead of as identical, since it is written), refreshed,
    or a conflict over something else (media_path, say) — the upgrade is
    written either way. A row that updates is not listed: the update
    rewrites it anyway, and its audit keeps the value the row really held."""
    streams = tuple(sorted({r.get("stream") for r in scan["records"]
                            if r.get("stream")}))
    existing = existing_by_key(conn, streams)
    back_scope = _reappear_scope(conn, scan)
    upgrade = declared_upgrade(scan.get("connector"))
    seen: set[tuple[str, str]] = set()
    new, updates, refreshed, conflicts, invalid = [], [], [], [], []
    reappeared, format_upgrades = [], []
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
        back = _reappears(row, rec, back_scope)
        if back:
            reappeared.append((row, rec))
        current = _upgraded(upgrade, row, rec)
        if current is None:
            kind, changes = classify(row, rec)
        else:
            kind, changes = classify({**dict(row), "value_json": current}, rec)
            if kind == "update":        # the audit records what the row held
                changes = classify(row, rec)[1]
            else:
                format_upgrades.append((row, current))
        if kind == "identical":
            if not back and current is None:   # coming back, or rewritten: not a skip
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
            "reappeared": reappeared, "format_upgrades": format_upgrades,
            "streams": list(streams), **_mirror_plan(conn, scan)}


def _reappear_scope(conn: sqlite3.Connection, scan: dict
                    ) -> tuple[set[int], set[str], set[str]] | None:
    """Where a row mirroring hid may come back: (applied import ids of this
    connector and account, mirror streams, ids the source holds), or None
    when this scan does not mirror.

    The exact scope `_mirror_plan` removes from, so what one source hid only
    that source brings back — a second phone, another account, or a
    connector sharing the streams (mac_messages beside ios_messages) never
    does. An import that is itself retracted is out: its rows are hidden
    because the owner undid it, and only `imports restore` brings them back.
    """
    present = scan.get("present_ids")
    mirror_streams = scan.get("mirror_streams")
    if present is None or not scan.get("account") or not mirror_streams:
        return None
    imports = {r["id"] for r in conn.execute(
        "SELECT id FROM imports WHERE status='applied' AND connector=? "
        "AND account IS ?", (scan["connector"], scan["account"]))}
    return imports, set(mirror_streams), set(present)


def _reappears(row: sqlite3.Row, rec: dict, scope) -> bool:
    """True for a row mirroring hid that the source holds again. A retracted
    row of an applied import can only have been hidden by mirroring:
    `retract_import` retracts the import with its rows, and `restore_import`
    puts back everything it hid."""
    if scope is None or row["status"] != "retracted":
        return False
    imports, streams, present = scope
    return (row["import_id"] in imports and rec["stream"] in streams
            and rec["external_id"] in present)


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
    """The refusal message if this plan removes suspiciously much, else None.

    Two bars, either one refuses: a real share of the archive (at least
    MASS_REMOVAL_MIN records and more than MASS_REMOVAL_SHARE of it), or half
    of it or more however few records that is — a small archive is exactly
    where one wrong file removes nearly everything. One record alone is never
    a mass removal: deleting one of your two notes is not suspicious, and the
    snapshot taken first covers it either way."""
    n, scope = len(plan.get("removed", ())), plan.get("mirror_scope", 0)
    if not n:
        return None
    if (n > 1 and n >= MASS_REMOVAL_MAJORITY * scope) or (
            n >= MASS_REMOVAL_MIN and n > MASS_REMOVAL_SHARE * scope):
        return (f"this would remove {n} of {scope} archived record(s) at once — "
                f"more like an incomplete source (a partial or iCloud-optimized "
                f"backup, an export of only part of the account) than deletions. "
                f"Check the source; if it is really what you want, re-run with "
                f"--allow-mass-removal")
    return None


def _remove(conn: sqlite3.Connection, rows: list, actor: str) -> tuple[int, int]:
    """Mirror deletions at the source: (deleted, retracted).

    A row a confirmed record cites as its provenance — an entry, or an entity
    relationship ("Jake works_at Acme", cited from his contact card) — is
    retracted, hidden everywhere, rather than deleted: the confirmed tier's
    provenance is never destroyed (the lesson of entry merge, 1.1). Everything
    else is deleted; participants and the search index follow by cascade and
    trigger. A hidden row comes back by itself when the source holds it again
    (`_reappear`); a deleted one returns as a new row.
    """
    deleted = retracted = 0
    now = now_utc()
    has_links = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='entity_links'").fetchone() is not None
    for row in rows:
        cited = conn.execute("SELECT 1 FROM entry_observations WHERE "
                             "observation_id=? LIMIT 1", (row["id"],)).fetchone()
        if not cited and has_links:
            cited = conn.execute("SELECT 1 FROM entity_links WHERE "
                                 "observation_uuid=? LIMIT 1", (row["uuid"],)).fetchone()
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


def _reappear(conn: sqlite3.Connection, pairs: list, actor: str) -> int:
    """Rows mirroring hid that the source holds again: active once more,
    with the participants the source names now. Audited, because a status
    change is a change to what `ask` and every view can see."""
    if not pairs:
        return 0
    now = now_utc()
    for row, rec in pairs:
        conn.execute("UPDATE observations SET status='active', updated_at=? "
                     "WHERE id=?", (now, row["id"]))
        _link_participants(conn, row["id"], rec.get("participants", []))
    audit.log(conn, actor, "mirror_restore", "observations", None,
              {"restored": len(pairs),
               "external_ids": sorted({rec["external_id"] for _, rec in pairs})})
    return len(pairs)


def apply_plan(conn: sqlite3.Connection, scan: dict, plan: dict,
               actor: str = "import", allow_mass_removal: bool = False,
               snapshot: str | None = None) -> dict:
    """Write the plan. The caller owns the transaction; nothing here commits.

    `snapshot` names the database snapshot the caller took before a plan that
    removes anything. It is recorded with the import's counts, because that
    snapshot — not `retract_import`, which only hides what an import added —
    is the undo for a removal."""
    if plan["invalid"]:
        raise DomainError(plan["invalid"][0])
    refusal = mass_removal(plan)
    if refusal and not allow_mass_removal:
        raise DomainError(refusal)
    import_id, import_uuid = start_import(
        conn, scan["connector"], account=scan.get("account"),
        source_file=scan.get("source_file"), source_hash=scan.get("source_hash"))
    declared = declare_coverage(conn, import_id, scan.get("coverage") or [],
                                default_account=scan.get("account"))
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
    upgraded = _upgrade_format(conn, plan.get("format_upgrades", []),
                               scan["connector"], import_uuid, actor)
    reappeared = _reappear(conn, plan.get("reappeared", []), actor)
    deleted, retracted = _remove(conn, plan.get("removed", []), actor)
    refresh_handle_stats(conn)
    counts = {"added": len(plan["new"]), "updated": len(plan["updates"]),
              "refreshed": len(plan["refreshed"]),
              "identical": plan["identical"],
              "conflicts": len(plan["conflicts"])}
    if upgraded:
        counts["format_upgraded"] = upgraded
    if reappeared:
        counts["reappeared"] = reappeared
    if deleted or retracted:
        counts["removed"] = deleted
        if retracted:
            counts["retracted_cited"] = retracted
        if snapshot:
            counts["snapshot"] = snapshot
    if declared:
        counts["coverage"] = declared
    finish_import(conn, import_id, "applied", counts, actor)
    return {**counts, "import_uuid": import_uuid}


def declare_coverage(conn: sqlite3.Connection, import_id: int, coverage: list,
                     *, default_account: str | None = None) -> int:
    """Store what a scan declared complete (schema v7, `import_coverage`):
    `[{stream, start, end, basis, account?}]`, each resting on a guarantee in
    the source format — never on the rows' own dates. A declaration with no
    stream, a date that is not one, an interval that ends before it starts,
    or no basis is a DomainError: a wrong "complete" would manufacture
    inactivity. Returns how many rows were stored."""
    if not isinstance(coverage, (list, tuple)):
        raise DomainError("coverage must be a list of {stream, start, end, basis}")
    n = 0
    for i, c in enumerate(coverage):
        if not isinstance(c, dict):
            raise DomainError(f"coverage[{i}] must be an object")
        stream, basis = c.get("stream"), c.get("basis")
        if not isinstance(stream, str) or not stream.strip():
            raise DomainError(f"coverage[{i}] needs a stream")
        if not isinstance(basis, str) or not basis.strip():
            raise DomainError(f"coverage[{i}] ({stream}) needs a basis: the guarantee "
                              f"in the source it rests on")
        start, end = c.get("start"), c.get("end")
        for label, v in (("start", start), ("end", end)):
            if not is_iso_date(v):
                raise DomainError(f"coverage[{i}] ({stream}) {label} must be YYYY-MM-DD, "
                                  f"got {v!r}")
        if end < start:
            raise DomainError(f"coverage[{i}] ({stream}) ends {end} before it starts {start}")
        account = c.get("account", default_account)
        if account is not None and not isinstance(account, str):
            raise DomainError(f"coverage[{i}] ({stream}) account must be a string or null")
        conn.execute(
            "INSERT OR IGNORE INTO import_coverage (import_id, account, stream, "
            "start_date, end_date, basis) VALUES (?, ?, ?, ?, ?, ?)",
            (import_id, account, stream.strip(), start, end, basis.strip()))
        n += 1
    return n


def coverage_days(conn: sqlite3.Connection, streams: tuple[str, ...], start: str,
                  end: str, *, account: str | None = None) -> set[str]:
    """The days in [start, end] some APPLIED import declared complete for one
    of `streams` (and `account`, when given): the only days on which a
    missing row means "none". Empty when nothing was declared."""
    marks = ",".join("?" for _ in streams)
    args: list = [*streams, end, start]
    where = ""
    if account is not None:
        where = " AND (c.account IS NULL OR c.account = ?)"
        args.append(account)
    rows = conn.execute(
        f"SELECT c.start_date, c.end_date FROM import_coverage c "
        f"JOIN imports i ON i.id = c.import_id "
        f"WHERE i.status = 'applied' AND c.stream IN ({marks}) "
        f"AND c.start_date <= ? AND c.end_date >= ?{where}", args).fetchall()
    out: set[str] = set()
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    for r in rows:
        d = max(lo, date.fromisoformat(r["start_date"]))
        last = min(hi, date.fromisoformat(r["end_date"]))
        while d <= last:
            out.add(d.isoformat())
            d += timedelta(days=1)
    return out


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

"""Entity links: typed relationships between entities — "Jake works_at Acme",
"Jake from El Paso", "Alex partner_of Maya".

Mentions say an entry was ABOUT an entity; nothing said how two entities
relate, so "where is Jake from" and "who does Jake know" could only be
answered by re-reading prose. A relationship is a confirmed-tier record, like
a mention: it exists because the owner typed it (`entities link add`) or
reviewed it (a draft's `entity_links`, through ingest -> review -> apply).
An importer never writes one.

Direction reads as a sentence, subject first (documented in schema.sql):
    Jake works_at Acme        Jake from El Paso        Jake lives_in Austin
    Priya manages Alex        Linda parent_of Alex     Sam member_of Movement
    Acme located_in Austin    Bled Castle part_of Lake Bled

Symmetric types (knows, friend_of, partner_of, family_of, sibling_of,
colleague_of, relates) are stored ONCE, lower entity id first, so the two
readings of one fact are one row and the active-uniqueness index sees a
duplicate. A type with an `inverse` (parent_of / child_of) states the same
fact from the other end: adding one half while the other is active returns
the existing link instead of storing the fact twice.

Non-destructive like everything else here: removing retracts
(status='retracted'), restore brings it back, entity merge repoints, and
every change is audit-logged.
"""

from __future__ import annotations

import re
import sqlite3

from . import audit, entities, entries
from .util import DomainError, is_iso_date, is_iso_datetime, new_uuid, now_utc

TYPE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
UUID_PREFIX_MIN = 8

_SELECT = (
    "SELECT l.*, lt.symmetric, "
    "f.uuid AS from_uuid, f.name AS from_name, f.type AS from_type, "
    "t.uuid AS to_uuid, t.name AS to_name, t.type AS to_type "
    "FROM entity_links l "
    "JOIN entity_link_types lt ON lt.name = l.type "
    "JOIN entities f ON f.id = l.from_entity_id "
    "JOIN entities t ON t.id = l.to_entity_id")


# ---------------------------------------------------------------- types

def link_types(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) | {"symmetric": bool(r["symmetric"])} for r in conn.execute(
        "SELECT name, symmetric, inverse, description FROM entity_link_types "
        "ORDER BY name")]


def get_type(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM entity_link_types WHERE name=?",
                       (name,)).fetchone()
    if row is None:
        known = ", ".join(r["name"] for r in conn.execute(
            "SELECT name FROM entity_link_types ORDER BY name"))
        raise DomainError(f"unknown relationship type '{name}' "
                          f"(expected one of: {known})")
    return row


def add_type(conn: sqlite3.Connection, name: str, description: str, *,
             symmetric: bool = False, inverse: str | None = None,
             actor: str = "user") -> dict:
    """A new relationship type — a row, never a migration. `inverse` pairs it
    with an existing directed type that reads the same fact from the other
    end (reports_to for manages); both rows then point at each other."""
    name = (name or "").strip()
    if not TYPE_NAME_RE.match(name):
        raise DomainError(f"relationship type '{name}' must be lowercase "
                          f"snake_case (e.g. reports_to)")
    if conn.execute("SELECT 1 FROM entity_link_types WHERE name=?",
                    (name,)).fetchone():
        raise DomainError(f"relationship type '{name}' already exists")
    description = (description or "").strip()
    if not description:
        raise DomainError("a relationship type needs a description of how "
                          "'FROM type TO' reads")
    if symmetric and inverse:
        raise DomainError("a symmetric type is its own inverse — give "
                          "symmetric or an inverse, not both")
    if inverse is not None:
        inv = get_type(conn, inverse)
        if inv["symmetric"]:
            raise DomainError(f"'{inverse}' is symmetric — it has no inverse")
        if inv["inverse"] is not None:
            raise DomainError(f"'{inverse}' already has an inverse "
                              f"('{inv['inverse']}')")
    conn.execute(
        "INSERT INTO entity_link_types (name, symmetric, inverse, description, "
        "created_at) VALUES (?, ?, ?, ?, ?)",
        (name, 1 if symmetric else 0, inverse, description, now_utc()))
    if inverse is not None:
        conn.execute("UPDATE entity_link_types SET inverse=? WHERE name=?",
                     (name, inverse))
    audit.log(conn, actor, "create", "entity_link_types", None,
              {"name": name, "symmetric": bool(symmetric), "inverse": inverse,
               "description": description})
    return {"name": name, "symmetric": bool(symmetric), "inverse": inverse,
            "description": description}


# ---------------------------------------------------------------- reading

def describe(row) -> str:
    """'Jake Morales from El Paso' — the stored direction, as a sentence."""
    return f"{row['from_name']} {row['type']} {row['to_name']}"


def _canonical(type_row: sqlite3.Row, from_id: int, to_id: int) -> tuple[int, int]:
    if type_row["symmetric"] and from_id > to_id:
        return to_id, from_id
    return from_id, to_id


def find_active(conn: sqlite3.Connection, from_id: int, to_id: int,
                link_type: str, exclude_id: int | None = None) -> sqlite3.Row | None:
    """The active link that already states this fact, in either reading of a
    symmetric type or from the other end of an inverse pair."""
    t = get_type(conn, link_type)
    f, to = _canonical(t, from_id, to_id)
    candidates = [(f, to, t["name"])]
    if t["inverse"]:
        candidates.append((to_id, from_id, t["inverse"]))
    for a, b, name in candidates:
        row = conn.execute(
            "SELECT * FROM entity_links WHERE status='active' AND "
            "from_entity_id=? AND to_entity_id=? AND type=? AND id IS NOT ?",
            (a, b, name, exclude_id)).fetchone()
        if row is not None:
            return row
    return None


def get(conn: sqlite3.Connection, link_uuid: str) -> sqlite3.Row:
    """A link by uuid, or by a unique prefix of at least 8 characters — what
    `entities link list` prints is typed back into `rm`."""
    ref = (link_uuid or "").strip().lower()
    rows = conn.execute(f"{_SELECT} WHERE l.uuid=?", (ref,)).fetchall()
    if not rows and len(ref) >= UUID_PREFIX_MIN:
        rows = conn.execute(f"{_SELECT} WHERE substr(l.uuid, 1, ?)=? ORDER BY l.id",
                            (len(ref), ref)).fetchall()
    if not rows:
        raise DomainError(f"relationship {link_uuid} does not exist")
    if len(rows) > 1:
        raise DomainError(f"'{link_uuid}' matches {len(rows)} relationships — "
                          f"give more of the uuid")
    return rows[0]


def _as_dict(row: sqlite3.Row, entity_id: int | None = None) -> dict:
    d = {
        "uuid": row["uuid"], "type": row["type"], "text": describe(row),
        "symmetric": bool(row["symmetric"]),
        "from": {"id": row["from_entity_id"], "uuid": row["from_uuid"],
                 "name": row["from_name"], "type": row["from_type"]},
        "to": {"id": row["to_entity_id"], "uuid": row["to_uuid"],
               "name": row["to_name"], "type": row["to_type"]},
        "note": row["note"], "valid_from": row["valid_from"],
        "valid_to": row["valid_to"], "status": row["status"],
        "actor": row["actor"],
        "provenance": {"source_id": row["source_id"],
                       "char_start": row["char_start"],
                       "char_end": row["char_end"],
                       "entry_uuid": row["entry_uuid"],
                       "observation_uuid": row["observation_uuid"],
                       "extraction_run_id": row["extraction_run_id"]},
        "confirmed_at": row["confirmed_at"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if entity_id is not None:
        if row["symmetric"]:
            d["direction"] = "both"
        else:
            d["direction"] = "out" if row["from_entity_id"] == entity_id else "in"
        d["other"] = d["to"] if row["from_entity_id"] == entity_id else d["from"]
    return d


def for_entity(conn: sqlite3.Connection, entity_id: int, *,
               types: list[str] | tuple[str, ...] | None = None,
               include_retracted: bool = False) -> list[dict]:
    """Every relationship touching an entity, read from its side: `direction`
    is 'out' (it is the subject: Jake from El Paso), 'in' (it is the object:
    Linda parent_of Jake) or 'both' (symmetric), and `other` is the far end.

    This is the read path for "where is X from" (types=('from',), direction
    'out') and "who does X know" (the person-to-person types)."""
    entities.get(conn, entity_id)   # raises if missing
    sql = f"{_SELECT} WHERE (l.from_entity_id=? OR l.to_entity_id=?)"
    args: list = [entity_id, entity_id]
    if not include_retracted:
        sql += " AND l.status='active'"
    if types:
        for name in types:
            get_type(conn, name)   # a typo'd type is an error, not "no relationships"
        sql += f" AND l.type IN ({','.join('?' for _ in types)})"
        args += list(types)
    rows = [_as_dict(r, entity_id) for r in conn.execute(sql, args)]
    rows.sort(key=lambda d: (d["type"], d["other"]["name"].casefold(), d["uuid"]))
    return rows


def list_links(conn: sqlite3.Connection, *, link_type: str | None = None,
               include_retracted: bool = False) -> list[dict]:
    sql = f"{_SELECT} WHERE 1=1"
    args: list = []
    if not include_retracted:
        sql += " AND l.status='active'"
    if link_type is not None:
        get_type(conn, link_type)
        sql += " AND l.type=?"
        args.append(link_type)
    rows = [_as_dict(r) for r in conn.execute(sql, args)]
    rows.sort(key=lambda d: (d["type"], d["from"]["name"].casefold(),
                             d["to"]["name"].casefold(), d["uuid"]))
    return rows


# ---------------------------------------------------------------- writing

def _check_dates(valid_from: str | None, valid_to: str | None) -> None:
    for label, v in (("valid_from", valid_from), ("valid_to", valid_to)):
        if v is not None and not (is_iso_date(v) or is_iso_datetime(v)):
            raise DomainError(f"{label} '{v}' is not an ISO date or datetime")
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        raise DomainError(f"valid_from {valid_from} is after valid_to {valid_to}")


def _check_provenance(conn: sqlite3.Connection, actor: str,
                      source_id: int | None, char_start: int | None,
                      char_end: int | None, entry_uuid: str | None,
                      observation_uuid: str | None,
                      extraction_run_id: int | None) -> None:
    if (char_start is None) != (char_end is None):
        raise DomainError("char_start and char_end go together")
    if char_start is not None:
        if source_id is None:
            raise DomainError("a character span needs the source it indexes "
                              "(source_id)")
        if (not isinstance(char_start, int) or not isinstance(char_end, int)
                or char_start < 0 or char_end <= char_start):
            raise DomainError("char_start/char_end must be integers with "
                              "0 <= start < end")
    if source_id is not None:
        if conn.execute("SELECT 1 FROM sources WHERE id=?",
                        (source_id,)).fetchone() is None:
            raise DomainError(f"source id={source_id} does not exist")
        row = conn.execute("SELECT text FROM transcripts WHERE source_id=? "
                           "ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()
        if row is not None and char_end is not None and char_end > len(row["text"]):
            raise DomainError(f"char_end {char_end} exceeds transcript length "
                              f"{len(row['text'])} of source id={source_id}")
    if entry_uuid is not None:
        entries.get_by_uuid(conn, entry_uuid)   # raises if missing
    if observation_uuid is not None and conn.execute(
            "SELECT 1 FROM observations WHERE uuid=?",
            (observation_uuid,)).fetchone() is None:
        raise DomainError(f"observation {observation_uuid} does not exist")
    if extraction_run_id is not None and conn.execute(
            "SELECT 1 FROM extraction_runs WHERE id=?",
            (extraction_run_id,)).fetchone() is None:
        raise DomainError(f"extraction run id={extraction_run_id} does not exist")
    if actor == "extraction" and char_start is None and entry_uuid is None \
            and observation_uuid is None:
        raise DomainError("an extracted relationship needs provenance — the "
                          "passage (source_id + char span), the entry, or the "
                          "observation that states it")


def add(conn: sqlite3.Connection, from_id: int, to_id: int, link_type: str, *,
        note: str | None = None, valid_from: str | None = None,
        valid_to: str | None = None, source_id: int | None = None,
        char_start: int | None = None, char_end: int | None = None,
        entry_uuid: str | None = None, extraction_run_id: int | None = None,
        observation_uuid: str | None = None, actor: str = "user") -> dict:
    """Relate two entities. Returns {uuid, created, text}: created is False
    when the same fact is already active — in either reading — and uuid then
    names the link that already says it."""
    t = get_type(conn, link_type)
    if from_id == to_id:
        raise DomainError("a relationship cannot connect an entity to itself")
    entities.get_active(conn, from_id)   # raises if missing or merged
    entities.get_active(conn, to_id)
    _check_dates(valid_from, valid_to)
    if isinstance(note, str):
        note = note.strip() or None
    _check_provenance(conn, actor, source_id, char_start, char_end, entry_uuid,
                      observation_uuid, extraction_run_id)

    existing = find_active(conn, from_id, to_id, t["name"])
    if existing is not None:
        return {"uuid": existing["uuid"], "created": False,
                "text": describe(get(conn, existing["uuid"]))}

    f, to = _canonical(t, from_id, to_id)
    now = now_utc()
    uid = new_uuid()
    conn.execute(
        "INSERT INTO entity_links (uuid, from_entity_id, to_entity_id, type, "
        "note, valid_from, valid_to, status, actor, source_id, char_start, "
        "char_end, entry_uuid, extraction_run_id, observation_uuid, "
        "confirmed_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (uid, f, to, t["name"], note, valid_from, valid_to, actor, source_id,
         char_start, char_end, entry_uuid, extraction_run_id, observation_uuid,
         now, now, now))
    row = get(conn, uid)
    audit.log(conn, actor, "create", "entity_links", uid,
              {"from": row["from_uuid"], "to": row["to_uuid"], "type": t["name"],
               "text": describe(row), "note": note, "source_id": source_id,
               "entry_uuid": entry_uuid, "observation_uuid": observation_uuid})
    return {"uuid": uid, "created": True, "text": describe(row)}


def retract(conn: sqlite3.Connection, link_uuid: str, actor: str = "user",
            reason: str | None = None) -> dict:
    """The undo for a wrong relationship: hidden everywhere, row kept."""
    row = get(conn, link_uuid)
    if row["status"] == "retracted":
        raise DomainError(f"relationship {row['uuid']} is already retracted")
    conn.execute("UPDATE entity_links SET status='retracted', updated_at=? "
                 "WHERE id=?", (now_utc(), row["id"]))
    audit.log(conn, actor, "retract", "entity_links", row["uuid"],
              {"text": describe(row), "reason": reason})
    return {"uuid": row["uuid"], "text": describe(row)}


def restore(conn: sqlite3.Connection, link_uuid: str, actor: str = "user") -> dict:
    row = get(conn, link_uuid)
    if row["status"] != "retracted":
        raise DomainError(f"relationship {row['uuid']} is not retracted "
                          f"(status: {row['status']})")
    for end in ("from_entity_id", "to_entity_id"):
        try:
            entities.get_active(conn, row[end])
        except DomainError as e:
            raise DomainError(f"cannot restore {row['uuid']}: {e} — add the "
                              f"relationship to the live entity instead") from None
    dup = find_active(conn, row["from_entity_id"], row["to_entity_id"],
                      row["type"], exclude_id=row["id"])
    if dup is not None:
        raise DomainError(f"cannot restore {row['uuid']}: the same relationship "
                          f"is already active ({dup['uuid']})")
    now = now_utc()
    conn.execute(
        "UPDATE entity_links SET status='active', updated_at=?, "
        "confirmed_at=COALESCE(?, confirmed_at) WHERE id=?",
        (now, now if actor == "user" else None, row["id"]))
    audit.log(conn, actor, "restore", "entity_links", row["uuid"],
              {"text": describe(row)})
    return {"uuid": row["uuid"], "text": describe(row)}


def repoint(conn: sqlite3.Connection, src_id: int, dst_id: int,
            actor: str = "user") -> dict:
    """Entity merge, the relationships half: every link that named src now
    names dst, so nothing active is left pointing at a merged entity.

    Two links cannot simply move. One between src and dst themselves ("Jake
    knows Jakey") would become a self-link; one that dst already states
    ("Jakey from El Paso" beside "Jake from El Paso") would become a
    duplicate. Both are retracted where they stand — kept, audited, pointing
    at src for the record — rather than deleted. Retracted links move too
    (when they can), so a later restore lands on the live entity.
    """
    moved = retracted = 0
    now = now_utc()
    rows = conn.execute(
        "SELECT l.*, lt.symmetric FROM entity_links l "
        "JOIN entity_link_types lt ON lt.name = l.type "
        "WHERE l.from_entity_id=? OR l.to_entity_id=? ORDER BY l.id",
        (src_id, src_id)).fetchall()
    for r in rows:
        f = dst_id if r["from_entity_id"] == src_id else r["from_entity_id"]
        t = dst_id if r["to_entity_id"] == src_id else r["to_entity_id"]
        reason = None
        if f == t:
            reason = "merge: both ends are now the same entity"
        elif r["status"] == "active":
            dup = find_active(conn, f, t, r["type"], exclude_id=r["id"])
            if dup is not None:
                reason = f"merge: duplicate of {dup['uuid']}"
        if reason is not None:
            if r["status"] == "active":
                conn.execute("UPDATE entity_links SET status='retracted', "
                             "updated_at=? WHERE id=?", (now, r["id"]))
                audit.log(conn, actor, "retract", "entity_links", r["uuid"],
                          {"reason": reason, "merged_entity_id": src_id,
                           "into_id": dst_id})
                retracted += 1
            continue
        if r["symmetric"] and f > t:
            f, t = t, f
        conn.execute("UPDATE entity_links SET from_entity_id=?, to_entity_id=?, "
                     "updated_at=? WHERE id=?", (f, t, now, r["id"]))
        audit.log(conn, actor, "repoint", "entity_links", r["uuid"],
                  {"old": [r["from_entity_id"], r["to_entity_id"]], "new": [f, t],
                   "merged_entity_id": src_id, "into_id": dst_id})
        moved += 1
    return {"moved": moved, "retracted": retracted}

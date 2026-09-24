"""Entities: canonical names, aliases, resolution, and merge.

Resolution order: exact canonical name → exact alias → fuzzy candidates
(difflib ratio or word-level agreement >= 0.75, plus substring hits).
Candidates are returned scored for a human (or the /log skill) to decide —
never auto-picked. An alias mapping to several entities is a supported state.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from difflib import SequenceMatcher

from ..config import ENTITY_TYPES
from . import audit
from .util import DomainError, new_uuid, now_utc, slugify

FUZZY_THRESHOLD = 0.75
_WORD = re.compile(r"\w+")


def _fold(text: str) -> str:
    """Case- and accent-insensitive: a contact card's 'José Núñez' and an
    entity typed as 'Jose Nunez' are the same words."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _word_score(query: str, label: str) -> float:
    """Word-level agreement, independent of order: 'Reilly, Jake' against
    'Jake Reilly', or a contact card's 'Alex Chen' against 'Alexander Chen'.

    Whole-string similarity misses both: reordering and a longer first name
    wreck a character diff. Here each word of the shorter name takes its best
    partner in the longer one — identical 1.0; one a prefix of the other
    ('alex'/'alexander') 0.85; a lone initial of the other ('w'/'whitfield')
    0.8; else character similarity — and words left unpartnered count
    against the total. Capped below 1.0, so it can never pass for an exact
    name or alias match.
    """
    q, l = _WORD.findall(_fold(query)), _WORD.findall(_fold(label))
    if not q or not l:
        return 0.0
    short, longer = (q, l) if len(q) <= len(l) else (l, q)
    total = 0.0
    for a in short:
        best = 0.0
        for b in longer:
            if a == b:
                s = 1.0
            elif min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)):
                s = 0.85
            elif min(len(a), len(b)) == 1 and a[0] == b[0]:
                s = 0.8
            else:
                s = SequenceMatcher(None, a, b).ratio()
            best = max(best, s)
        total += best
    return min(0.95, total / len(longer))


def _validate_type(entity_type: str) -> None:
    if entity_type not in ENTITY_TYPES:
        raise DomainError(
            f"unknown entity type '{entity_type}' (expected one of: {', '.join(ENTITY_TYPES)})")


def get(conn: sqlite3.Connection, entity_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
    if row is None:
        raise DomainError(f"entity id={entity_id} does not exist")
    return row


def get_active(conn: sqlite3.Connection, entity_id: int) -> sqlite3.Row:
    row = get(conn, entity_id)
    if row["status"] == "merged":
        raise DomainError(
            f"entity id={entity_id} '{row['name']}' was merged into id={row['merged_into']}")
    return row


def resolve_merged(conn: sqlite3.Connection, entity_id: int) -> int:
    """Follow merged_into pointers to the live entity."""
    seen = set()
    row = get(conn, entity_id)
    while row["status"] == "merged":
        if row["id"] in seen:
            raise DomainError(f"merge cycle detected at entity id={row['id']}")
        seen.add(row["id"])
        row = get(conn, row["merged_into"])
    return row["id"]


def match(conn: sqlite3.Connection, name: str, entity_type: str | None = None) -> dict:
    if entity_type is not None:
        _validate_type(entity_type)
    type_clause = " AND e.type=?" if entity_type else ""
    type_args = (entity_type,) if entity_type else ()

    exact = conn.execute(
        f"SELECT e.id, e.uuid, e.name, e.type FROM entities e "
        f"WHERE e.status='active' AND e.name=? COLLATE NOCASE{type_clause}",
        (name, *type_args)).fetchall()
    if exact:
        return {"query": name, "status": "exact",
                "matches": [dict(r) | {"score": 1.0, "via": "name"} for r in exact]}

    alias = conn.execute(
        f"SELECT DISTINCT e.id, e.uuid, e.name, e.type FROM entity_aliases a "
        f"JOIN entities e ON e.id=a.entity_id "
        f"WHERE e.status='active' AND a.alias=? COLLATE NOCASE{type_clause}",
        (name, *type_args)).fetchall()
    if alias:
        return {"query": name, "status": "alias",
                "matches": [dict(r) | {"score": 1.0, "via": "alias"} for r in alias]}

    # Fuzzy pass over canonical names and aliases of active entities.
    rows = conn.execute(
        f"SELECT e.id, e.uuid, e.name, e.type, e.name AS label, 'name' AS via "
        f"FROM entities e WHERE e.status='active'{type_clause.replace('e.type', 'e.type')} "
        f"UNION ALL "
        f"SELECT e.id, e.uuid, e.name, e.type, a.alias AS label, 'alias' AS via "
        f"FROM entity_aliases a JOIN entities e ON e.id=a.entity_id "
        f"WHERE e.status='active'{type_clause}",
        (*type_args, *type_args)).fetchall()

    q = name.lower().strip()
    best: dict[int, dict] = {}
    for r in rows:
        label = r["label"].lower().strip()
        score = max(SequenceMatcher(None, q, label).ratio(), _word_score(q, label))
        substring = len(q) >= 3 and (q in label or label in q)
        if score >= FUZZY_THRESHOLD or substring:
            cand = {"id": r["id"], "uuid": r["uuid"], "name": r["name"], "type": r["type"],
                    "score": round(max(score, FUZZY_THRESHOLD if substring else score), 3),
                    "via": r["via"], "matched_label": r["label"]}
            if r["id"] not in best or cand["score"] > best[r["id"]]["score"]:
                best[r["id"]] = cand
    if best:
        matches = sorted(best.values(), key=lambda c: -c["score"])
        return {"query": name, "status": "candidates", "matches": matches}
    return {"query": name, "status": "none", "matches": []}


def _unique_slug(conn: sqlite3.Connection, base: str) -> str:
    slug = base
    n = 2
    while conn.execute("SELECT 1 FROM entities WHERE slug=?", (slug,)).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def create(conn: sqlite3.Connection, entity_type: str, name: str,
           aliases: list[str] | None = None, notes: str | None = None,
           actor: str = "user") -> dict:
    _validate_type(entity_type)
    name = name.strip()
    if not name:
        raise DomainError("entity name must be non-empty")
    existing = conn.execute(
        "SELECT id, name FROM entities WHERE status='active' AND type=? AND name=? COLLATE NOCASE",
        (entity_type, name)).fetchone()
    if existing:
        raise DomainError(
            f"active {entity_type} '{existing['name']}' already exists (id={existing['id']})")
    now = now_utc()
    uid = new_uuid()
    slug = _unique_slug(conn, slugify(name))
    cur = conn.execute(
        "INSERT INTO entities (uuid, type, name, slug, notes, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
        (uid, entity_type, name, slug, notes, now, now))
    entity_id = cur.lastrowid
    for a in aliases or []:
        a = a.strip()
        if a and a.lower() != name.lower():
            conn.execute(
                "INSERT OR IGNORE INTO entity_aliases (alias, entity_id, created_at) VALUES (?, ?, ?)",
                (a, entity_id, now))
    audit.log(conn, actor, "create", "entities", uid,
              {"type": entity_type, "name": name, "aliases": aliases or []})
    return {"id": entity_id, "uuid": uid, "type": entity_type, "name": name, "slug": slug}


def add_alias(conn: sqlite3.Connection, entity_id: int, alias: str,
              actor: str = "user") -> None:
    row = get_active(conn, entity_id)
    alias = alias.strip()
    if not alias:
        raise DomainError("alias must be non-empty")
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (alias, entity_id, created_at) VALUES (?, ?, ?)",
        (alias, entity_id, now_utc()))
    audit.log(conn, actor, "add_alias", "entities", row["uuid"], {"alias": alias})


def rename(conn: sqlite3.Connection, entity_id: int, new_name: str,
           actor: str = "user") -> None:
    row = get_active(conn, entity_id)
    new_name = new_name.strip()
    if not new_name:
        raise DomainError("new name must be non-empty")
    clash = conn.execute(
        "SELECT id FROM entities WHERE status='active' AND type=? AND name=? COLLATE NOCASE AND id<>?",
        (row["type"], new_name, entity_id)).fetchone()
    if clash:
        raise DomainError(
            f"active {row['type']} named '{new_name}' already exists (id={clash['id']})")
    now = now_utc()
    conn.execute("UPDATE entities SET name=?, updated_at=? WHERE id=?",
                 (new_name, now, entity_id))
    if new_name.lower() != row["name"].lower():
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (alias, entity_id, created_at) VALUES (?, ?, ?)",
            (row["name"], entity_id, now))
    audit.log(conn, actor, "rename", "entities", row["uuid"],
              {"old": row["name"], "new": new_name})


def merge(conn: sqlite3.Connection, src_id: int, dst_id: int,
          actor: str = "user") -> dict:
    """Merge src into dst: mentions reassigned, aliases moved, src name becomes
    an alias of dst, src row kept with status='merged' (UUIDs stay stable)."""
    if src_id == dst_id:
        raise DomainError("cannot merge an entity into itself")
    src = get_active(conn, src_id)
    dst = get_active(conn, dst_id)
    if src["type"] != dst["type"]:
        raise DomainError(
            f"type mismatch: id={src_id} is {src['type']}, id={dst_id} is {dst['type']}")
    now = now_utc()
    moved = conn.execute(
        "UPDATE OR IGNORE mentions SET entity_id=? WHERE entity_id=?",
        (dst_id, src_id)).rowcount
    conn.execute("DELETE FROM mentions WHERE entity_id=?", (src_id,))
    conn.execute(
        "UPDATE OR IGNORE entity_aliases SET entity_id=? WHERE entity_id=?",
        (dst_id, src_id))
    conn.execute("DELETE FROM entity_aliases WHERE entity_id=?", (src_id,))
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (alias, entity_id, created_at) VALUES (?, ?, ?)",
        (src["name"], dst_id, now))
    conn.execute(
        "UPDATE entities SET status='merged', merged_into=?, updated_at=? WHERE id=?",
        (dst_id, now, src_id))
    conn.execute("UPDATE entities SET updated_at=? WHERE id=?", (now, dst_id))
    audit.log(conn, actor, "merge", "entities", src["uuid"],
              {"into_id": dst_id, "into_uuid": dst["uuid"],
               "src_name": src["name"], "dst_name": dst["name"], "mentions_moved": moved})
    return {"src": src["name"], "dst": dst["name"], "mentions_moved": moved}

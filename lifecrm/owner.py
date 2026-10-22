"""Who "you" are in the observed tier — one rule, read by `ask`, the web
UI's People page and the weekly review pack alike.

The owner takes part in every text, call and meeting, so wherever the owner
is not recognised they read as a third party ('with Alex Rivera, Dr. Rebecca
Ellis') and head every people list as the most recently contacted. The
handles that are you:

  - the ones config.toml lists under [owner] handles — the explicit,
    durable answer, needed when no import says it on its own (an iPhone
    calendar's account is 'calendar:Home'; its events carry no direction);
  - whoever sent an outgoing record (a text you sent, a call you placed);
  - any handle whose value is an import's own account (the phone number an
    SMS backup belongs to, the address an AI-chat export was made under).

An entity linked to any of them — or to a handle its source itself labels
'you (this phone)' — is the owner. Reads only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Config, ConfigError

_KNOWN_KEYS = ("handles", "name")


@dataclass(frozen=True)
class Identity:
    name: str | None
    # [owner] handles, as (kind, lowercased value)
    configured: frozenset[tuple[str, str]]
    # every import's own account, lowercased
    accounts: frozenset[str]
    # configured + outgoing senders + handles whose value is an account
    handles: frozenset[tuple[str, str]]
    # live (merge-followed) entity ids that are the owner
    entities: frozenset[int]


def _bad(cfg: Config, what: str) -> ConfigError:
    return ConfigError(f"{cfg.root / 'config.toml'}: [owner] {what}")


def _handle(cfg: Config, raw) -> tuple[str, str]:
    from .connectors.base import normalize_phone
    from .domain.observations import HANDLE_KINDS
    example = 'e.g. "email:you@example.com" or "phone:+15125550100"'
    if not isinstance(raw, str):
        raise _bad(cfg, f"handles must be 'kind:value' strings ({example}), got {raw!r}")
    kind, sep, value = raw.partition(":")
    kind, value = kind.strip().lower(), value.strip()
    if not sep or not kind or not value:
        raise _bad(cfg, f"handle {raw!r} is not 'kind:value' ({example})")
    if kind not in HANDLE_KINDS:
        raise _bad(cfg, f"handle {raw!r} has an unknown kind '{kind}' "
                        f"(expected one of {', '.join(HANDLE_KINDS)})")
    if kind == "phone":
        value = normalize_phone(value)
        if not value.lstrip("+"):
            raise _bad(cfg, f"handle {raw!r} has no phone number in it ({example})")
    return kind, value.lower()


def configured(cfg: Config) -> tuple[str | None, frozenset[tuple[str, str]]]:
    """[owner] from config.toml: (name, handles). Absent is fine — no
    configured identity. A wrong shape is a one-line ConfigError naming the
    fix, never a silently ignored setting."""
    table = (cfg.extra or {}).get("owner")
    if table is None:
        return None, frozenset()
    if not isinstance(table, dict):
        raise _bad(cfg, 'must be a table: [owner] then handles = ["email:you@example.com"]')
    unknown = sorted(set(table) - set(_KNOWN_KEYS))
    if unknown:
        raise _bad(cfg, f"has no setting '{unknown[0]}' (known: {', '.join(_KNOWN_KEYS)})")
    name = table.get("name")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise _bad(cfg, f"name must be your name as text, got {name!r}")
    raw = table.get("handles", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise _bad(cfg, f"handles must be a list of 'kind:value' strings, got {raw!r}")
    return (name.strip() if name else None), frozenset(_handle(cfg, h) for h in raw)


def _live(conn: sqlite3.Connection, entity_id: int) -> int | None:
    """The surviving entity at the end of a merge chain (handles stay on the
    merged row); None for one that no longer exists."""
    cur, seen = entity_id, set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = conn.execute("SELECT id, status, merged_into FROM entities WHERE id=?",
                           (cur,)).fetchone()
        if row is None:
            return None
        if row["status"] != "merged" or row["merged_into"] is None:
            return row["id"]
        cur = row["merged_into"]
    return cur


def identity(conn: sqlite3.Connection, cfg: Config) -> Identity:
    """Who the owner is in this database (see the module docstring). A
    database with no observed tier has only the configured handles."""
    name, conf = configured(cfg)
    try:
        accounts = frozenset(r[0].lower() for r in conn.execute(
            "SELECT DISTINCT account FROM imports WHERE account IS NOT NULL "
            "AND account != ''"))
        senders = {(r[0], r[1].lower()) for r in conn.execute(
            "SELECT DISTINCT p.handle_kind, p.handle_value FROM observation_participants p "
            "JOIN observations o ON o.id = p.observation_id "
            "WHERE o.direction = 'out' AND p.role = 'sender'")}
        rows = conn.execute(
            "SELECT kind, value, display_name, entity_id, confirmed_at "
            "FROM entity_handles WHERE ignored = 0").fetchall()
    except sqlite3.OperationalError:          # pre-v4: no observed tier
        return Identity(name, conf, frozenset(), conf, frozenset())
    handles = set(conf) | senders
    for r in rows:
        if r["value"].lower() in accounts:
            handles.add((r["kind"], r["value"].lower()))
    owners: set[int] = set()
    for r in rows:
        if r["entity_id"] is None or r["confirmed_at"] is None:
            continue
        if ((r["kind"], r["value"].lower()) in handles
                or (r["display_name"] or "").lower().startswith("you (")):
            live = _live(conn, r["entity_id"])
            if live is not None:
                owners.add(live)
    return Identity(name, conf, accounts, frozenset(handles), frozenset(owners))

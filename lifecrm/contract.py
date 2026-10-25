"""Versioned extraction-contract validation (contract v2).

The contract is the provider-neutral boundary: any extractor (the Claude Code
skill, a manual draft, or a future alternative) produces this JSON document,
and nothing reaches the database without passing this deterministic validation.
Errors are single-line and precise so the extractor can self-correct and retry.

v2 (ships with schema v2):
- `sources: [...]` — a day made of several notes extracts as ONE draft.
- per-entry provenance names WHICH source supports it (mandatory when the
  draft has more than one source) — provenance must never degrade to "one of
  today's notes".
- `entry_updates` — close a task/plan recorded on a DIFFERENT day ("finished
  the thing I mentioned Tuesday"). Deliberately narrow: closure fields only.
- `links` — thread structure between entries (continues/resolves/...).
- `entity_links` (optional, added with schema v5; contract_version stays 2
  because a draft without it is unchanged) — relationships between entities
  ("Jake from El Paso"), each citing the passage that states it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import CONTRACT_VERSION
from .config import CONFIDENCE, ENTITY_TYPES, MENTION_ROLES, TIME_OF_DAY
from .domain import measurements as measurements_mod
from .domain.links import LINK_TYPES
from .domain.util import is_iso_date, is_iso_datetime

ENTRY_KEYS = {
    "kind", "categories", "title", "body", "time_of_day", "occurred_at",
    "ended_at", "due_at", "data", "confidence", "measurements", "entities",
    "provenance", "uuid",
}
TOP_KEYS = {"contract_version", "journal_date", "sources", "day", "entries",
            "entry_updates", "links", "ambiguities", "extraction",
            "entity_links"}
ENTITY_LINK_KEYS = {"from", "to", "type", "note", "valid_from", "valid_to",
                    "entry_index", "provenance"}
CLOSURES = ("done", "dropped", "obsolete")
INTERFACES = ("claude_skill", "manual")


def validate_draft(conn: sqlite3.Connection, draft: dict,
                   texts: dict[int, str] | None = None,
                   inline_text: str | None = None) -> list[str]:
    """Return a list of single-line errors; empty list means the draft is valid.

    texts maps source_id -> transcript text (for provenance bounds checks);
    inline_text is the text of a single inline source, when present.
    """
    errors: list[str] = []
    texts = texts or {}
    if not isinstance(draft, dict):
        return ["draft must be a JSON object"]

    for key in set(draft) - TOP_KEYS:
        if key == "source":
            errors.append("source: contract v2 takes a `sources` ARRAY — "
                          "wrap it: \"sources\": [{...}]")
        else:
            errors.append(f"{key}: unknown top-level key")

    if draft.get("contract_version") != CONTRACT_VERSION:
        errors.append(
            f"contract_version: must be {CONTRACT_VERSION} "
            f"(got {draft.get('contract_version')!r})")

    jd = draft.get("journal_date")
    if not isinstance(jd, str) or not is_iso_date(jd):
        errors.append(f"journal_date: '{jd}' is not a valid YYYY-MM-DD date")
    else:
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        if jd > tomorrow:
            errors.append(
                f"journal_date: '{jd}' is in the future — journal dates record days "
                f"that happened; future events belong in occurred_at/due_at")

    # --- sources ---
    declared_ids: list[int] = []
    sole_len: int | None = None
    srcs = draft.get("sources")
    if not isinstance(srcs, list) or not srcs:
        errors.append("sources: required non-empty array of "
                      "{source_id} or (single) {text} objects")
        srcs = []
    for i, src in enumerate(srcs):
        p = f"sources[{i}]"
        if not isinstance(src, dict):
            errors.append(f"{p}: must be an object")
            continue
        has_id = "source_id" in src
        has_text = "text" in src
        if has_id == has_text:
            errors.append(f"{p}: exactly one of source_id or text is required")
        elif has_id:
            if not isinstance(src["source_id"], int):
                errors.append(f"{p}.source_id: must be an integer")
            elif conn.execute("SELECT 1 FROM sources WHERE id=?",
                              (src["source_id"],)).fetchone() is None:
                errors.append(f"{p}.source_id: source id={src['source_id']} does not exist")
            elif src["source_id"] in declared_ids:
                errors.append(f"{p}.source_id: source id={src['source_id']} listed twice")
            else:
                declared_ids.append(src["source_id"])
        else:
            if len(srcs) > 1:
                errors.append(f"{p}: inline text is only allowed in single-source "
                              f"drafts — import multi-source days first "
                              f"(lifecrm import / intake), then reference ids")
            if not isinstance(src["text"], str) or not src["text"].strip():
                errors.append(f"{p}.text: must be a non-empty string")
            stype = src.get("type", "pasted")
            if stype not in ("pasted", "phone_transcript", "manual"):
                errors.append(f"{p}.type: '{stype}' must be pasted, "
                              f"phone_transcript, or manual")
            if "recorded_at" in src and not is_iso_datetime(src["recorded_at"]):
                errors.append(f"{p}.recorded_at: '{src['recorded_at']}' is not ISO datetime")

    multi_source = len(srcs) > 1
    if len(srcs) == 1:
        if inline_text is not None:
            sole_len = len(inline_text)
        elif declared_ids and declared_ids[0] in texts:
            sole_len = len(texts[declared_ids[0]])

    def prov_len(prov: dict) -> int | None:
        sid = prov.get("source_id")
        if sid is not None:
            return len(texts[sid]) if sid in texts else None
        return sole_len

    # --- day ---
    day = draft.get("day")
    if day is not None:
        if not isinstance(day, dict):
            errors.append("day: must be an object")
        else:
            for key in set(day) - {"summary", "measurements"}:
                errors.append(f"day.{key}: unknown key")
            if "summary" in day and (not isinstance(day["summary"], str)
                                     or not day["summary"].strip()):
                errors.append("day.summary: must be a non-empty string")
            for i, m in enumerate(day.get("measurements", []) or []):
                err = _validate_measurement(m, f"day.measurements[{i}]")
                if err:
                    errors.append(err)

    # --- entries ---
    entries_list = draft.get("entries")
    if not isinstance(entries_list, list):
        errors.append("entries: required array (may be empty)")
        entries_list = []
    known_kinds = _known(conn, "entry_kinds")
    known_cats = _known(conn, "categories")
    draft_date = jd if isinstance(jd, str) and is_iso_date(jd) else None
    targeted: set[str] = set()
    for i, entry in enumerate(entries_list):
        errors.extend(_validate_entry(conn, entry, i, known_kinds, known_cats,
                                      declared_ids, multi_source, prov_len,
                                      draft_date, targeted))

    # --- entry_updates ---
    updates = draft.get("entry_updates")
    if updates is not None:
        if not isinstance(updates, list):
            errors.append("entry_updates: must be an array")
        else:
            for i, upd in enumerate(updates):
                errors.extend(_validate_entry_update(
                    conn, upd, i, declared_ids, multi_source, prov_len))

    # --- links ---
    links = draft.get("links")
    if links is not None:
        if not isinstance(links, list):
            errors.append("links: must be an array")
        else:
            for i, link in enumerate(links):
                errors.extend(_validate_link(conn, link, i, len(entries_list)))

    # --- entity_links (relationships between entities; schema v5) ---
    errors.extend(_validate_entity_links(conn, draft.get("entity_links"),
                                         entries_list, declared_ids,
                                         multi_source, prov_len))

    # --- ambiguities ---
    amb = draft.get("ambiguities")
    if amb is not None:
        if not isinstance(amb, list):
            errors.append("ambiguities: must be an array of {note} objects")
        else:
            for i, a in enumerate(amb):
                if not isinstance(a, dict) or not isinstance(a.get("note"), str):
                    errors.append(f"ambiguities[{i}]: must be an object with a string 'note'")

    # --- extraction --- validated here, not only at ingest time, so a
    # --dry-run that says "valid" means the real ingest will not refuse it.
    ext = draft.get("extraction")
    if ext is not None:
        if not isinstance(ext, dict):
            errors.append(f"extraction: must be an object {{provider, model, "
                          f"interface}} (got {type(ext).__name__})")
        else:
            for key in set(ext) - {"provider", "model", "interface"}:
                errors.append(f"extraction.{key}: unknown key")
            if "interface" in ext and ext["interface"] not in INTERFACES:
                errors.append(f"extraction.interface: '{ext['interface']}' must be "
                              f"{' or '.join(INTERFACES)}")
            if "provider" in ext and (not isinstance(ext["provider"], str)
                                      or not ext["provider"].strip()):
                errors.append("extraction.provider: must be a non-empty string")
            if ext.get("model") is not None and not isinstance(ext["model"], str):
                errors.append("extraction.model: must be a string or null")

    return errors


def _known(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"SELECT name FROM {table} ORDER BY id")]


def _validate_measurement(m, path: str) -> str | None:
    if not isinstance(m, dict):
        return f"{path}: must be an object"
    allowed = {"metric", "numeric_value", "text_value", "unit", "confidence"}
    for key in set(m) - allowed:
        return f"{path}.{key}: unknown key"
    return_err = measurements_mod.validate_fields(
        m.get("metric", ""), m.get("numeric_value"), m.get("text_value"),
        m.get("unit"), m.get("confidence", "high"))
    return f"{path}: {return_err}" if return_err else None


def _validate_entry(conn: sqlite3.Connection, entry, i: int, known_kinds: list[str],
                    known_cats: list[str], declared_ids: list[int],
                    multi_source: bool, prov_len, draft_date: str | None = None,
                    targeted: set[str] | None = None) -> list[str]:
    errs: list[str] = []
    p = f"entries[{i}]"
    if not isinstance(entry, dict):
        return [f"{p}: must be an object"]
    for key in set(entry) - ENTRY_KEYS:
        errs.append(f"{p}.{key}: unknown key")

    kind = entry.get("kind")
    if not isinstance(kind, str) or kind.lower() not in [k.lower() for k in known_kinds]:
        errs.append(f"{p}.kind: '{kind}' is not a known kind ({', '.join(known_kinds)})")

    cats = entry.get("categories")
    if not isinstance(cats, dict) or "primary" not in cats:
        errs.append(f"{p}.categories: required object with 'primary' (and optional 'secondary')")
    else:
        for key in set(cats) - {"primary", "secondary"}:
            errs.append(f"{p}.categories.{key}: unknown key")
        lower_cats = [c.lower() for c in known_cats]
        primary = cats.get("primary")
        if not isinstance(primary, str) or primary.lower() not in lower_cats:
            errs.append(f"{p}.categories.primary: unknown category '{primary}' "
                        f"({', '.join(known_cats)})")
        sec = cats.get("secondary", [])
        if not isinstance(sec, list):
            errs.append(f"{p}.categories.secondary: must be an array")
        else:
            for s in sec:
                if not isinstance(s, str) or s.lower() not in lower_cats:
                    errs.append(f"{p}.categories.secondary: unknown category '{s}'")

    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        errs.append(f"{p}.title: required non-empty string")
    if "body" in entry and entry["body"] is not None and not isinstance(entry["body"], str):
        errs.append(f"{p}.body: must be a string or null")

    tod = entry.get("time_of_day")
    if tod is not None and tod not in TIME_OF_DAY:
        errs.append(f"{p}.time_of_day: '{tod}' must be one of {', '.join(TIME_OF_DAY)}")
    for f in ("occurred_at", "ended_at", "due_at"):
        v = entry.get(f)
        if v is not None and not is_iso_datetime(v) and not is_iso_date(v):
            errs.append(f"{p}.{f}: '{v}' is not an ISO date or datetime")

    conf = entry.get("confidence", "high")
    if conf not in CONFIDENCE:
        errs.append(f"{p}.confidence: '{conf}' must be one of {', '.join(CONFIDENCE)}")
    if "data" in entry and entry["data"] is not None and not isinstance(entry["data"], dict):
        errs.append(f"{p}.data: must be an object or null")
    elif isinstance(entry.get("data"), dict) and "goal" in entry["data"]:
        # A goal (docs/contract.md, "Goals") is checked by the same rule the
        # domain layer applies on create and edit.
        from .domain import goals as goals_mod
        from .domain.util import DomainError
        try:
            goals_mod.validate_entry_data(entry.get("kind"), entry["data"])
        except DomainError as e:
            errs.append(f"{p}.{e}")

    for j, m in enumerate(entry.get("measurements", []) or []):
        err = _validate_measurement(m, f"{p}.measurements[{j}]")
        if err:
            errs.append(err)

    ents = entry.get("entities", []) or []
    if not isinstance(ents, list):
        errs.append(f"{p}.entities: must be an array")
        ents = []
    for j, ref in enumerate(ents):
        errs.extend(_validate_entity_ref(conn, ref, f"{p}.entities[{j}]"))

    prov = entry.get("provenance")
    if prov is not None:
        errs.extend(_validate_provenance(prov, f"{p}.provenance", declared_ids,
                                         multi_source, prov_len))
    elif multi_source:
        errs.append(f"{p}.provenance: required in multi-source drafts — every "
                    f"entry must name which source supports it")

    if "uuid" in entry:
        errs.extend(_validate_entry_uuid(conn, entry["uuid"], p, draft_date,
                                         targeted if targeted is not None else set()))
    return errs


def _validate_entry_uuid(conn: sqlite3.Connection, u, p: str,
                         draft_date: str | None, targeted: set[str]) -> list[str]:
    """entries[].uuid re-extracts an ACTIVE entry of THIS day (decision #5:
    forbid, not surface). A cross-day uuid used to be auto-edited without a
    decision, outside the day fingerprint that guards the review, and a
    retracted target failed the whole apply. Cross-day edits stay explicit."""
    if not isinstance(u, str):
        return [f"{p}.uuid: must be a string"]
    row = conn.execute("SELECT journal_date, status FROM entries WHERE uuid=?",
                       (u,)).fetchone()
    if row is None:
        return [f"{p}.uuid: entry {u} does not exist (omit uuid for new entries)"]
    if u in targeted:
        return [f"{p}.uuid: entry {u} is targeted by more than one entry — "
                f"merge them into one"]
    targeted.add(u)
    if row["status"] != "active":
        return [f"{p}.uuid: entry {u} is {row['status']} — only active entries "
                f"can be re-extracted (omit uuid to propose a new entry)"]
    if draft_date is not None and row["journal_date"] != draft_date:
        return [f"{p}.uuid: entry {u} is on {row['journal_date']}, not "
                f"{draft_date} — entries[].uuid targets same-day entries only; "
                f"cross-day changes go through entry_updates or lifecrm entry edit"]
    return []


def _validate_entity_ref(conn: sqlite3.Connection, ref, path: str) -> list[str]:
    if not isinstance(ref, dict):
        return [f"{path}: must be an object"]
    errs: list[str] = []
    role = ref.get("role", "mention")
    if role not in MENTION_ROLES:
        errs.append(f"{path}.role: '{role}' must be one of {', '.join(MENTION_ROLES)}")
    kind = ref.get("ref")
    if kind == "existing":
        eid = ref.get("id")
        if not isinstance(eid, int):
            errs.append(f"{path}.id: existing ref requires an integer id")
        else:
            row = conn.execute("SELECT id, name, type, status, merged_into FROM entities "
                               "WHERE id=?", (eid,)).fetchone()
            if row is None:
                errs.append(f"{path}.id: entity id={eid} does not exist")
            elif row["status"] == "merged":
                errs.append(f"{path}.id: entity id={eid} '{row['name']}' was merged into "
                            f"id={row['merged_into']} — use that id")
            elif "type" in ref and ref["type"] != row["type"]:
                errs.append(f"{path}.type: entity id={eid} is a {row['type']}, "
                            f"not {ref['type']}")
    elif kind == "new":
        etype = ref.get("type")
        name = ref.get("name")
        if etype not in ENTITY_TYPES:
            errs.append(f"{path}.type: '{etype}' must be one of {', '.join(ENTITY_TYPES)}")
        if not isinstance(name, str) or not name.strip():
            errs.append(f"{path}.name: new entity requires a non-empty name")
        elif etype in ENTITY_TYPES:
            # Defense in depth: the extractor should have resolved this already.
            hit = conn.execute(
                "SELECT e.id, e.name FROM entities e WHERE e.status='active' AND e.type=? "
                "AND e.name=? COLLATE NOCASE "
                "UNION SELECT e.id, e.name FROM entity_aliases a "
                "JOIN entities e ON e.id=a.entity_id "
                "WHERE e.status='active' AND e.type=? AND a.alias=? COLLATE NOCASE",
                (etype, name.strip(), etype, name.strip())).fetchone()
            if hit:
                errs.append(f"{path}: new entity '{name}' exactly matches existing {etype} "
                            f"id={hit['id']} '{hit['name']}' — use ref:existing")
        aliases = ref.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(a, str) for a in aliases):
            errs.append(f"{path}.aliases: must be an array of strings")
    else:
        errs.append(f"{path}.ref: must be 'existing' or 'new'")
    return errs


def _validate_provenance(prov, path: str, declared_ids: list[int],
                         multi_source: bool, prov_len) -> list[str]:
    if not isinstance(prov, dict):
        return [f"{path}: must be an object"]
    errs: list[str] = []
    sid = prov.get("source_id")
    if sid is not None:
        if not isinstance(sid, int):
            errs.append(f"{path}.source_id: must be an integer")
        elif sid not in declared_ids:
            errs.append(f"{path}.source_id: {sid} is not one of this draft's "
                        f"sources ({', '.join(map(str, declared_ids)) or 'none declared'})")
    elif multi_source:
        errs.append(f"{path}.source_id: required in multi-source drafts")

    has_chars = "char_start" in prov or "char_end" in prov
    has_segs = "segments" in prov
    if has_chars == has_segs:
        return errs + [f"{path}: exactly one of {{char_start, char_end}} "
                       f"or {{segments}} required"]
    if has_chars:
        cs, ce = prov.get("char_start"), prov.get("char_end")
        if not isinstance(cs, int) or not isinstance(ce, int) or cs < 0 or ce <= cs:
            return errs + [f"{path}: char_start/char_end must be integers "
                           f"with 0 <= start < end"]
        tlen = prov_len(prov)
        if tlen is not None and ce > tlen:
            errs.append(f"{path}: char_end {ce} exceeds transcript length {tlen}")
    else:
        segs = prov["segments"]
        if (not isinstance(segs, list) or not segs
                or any(not isinstance(s, int) or s < 0 for s in segs)):
            errs.append(f"{path}.segments: must be a non-empty array of "
                        f"non-negative segment indexes")
    for key in set(prov) - {"source_id", "char_start", "char_end", "segments"}:
        errs.append(f"{path}.{key}: unknown key")
    return errs


def _validate_entry_update(conn: sqlite3.Connection, upd, i: int,
                           declared_ids: list[int], multi_source: bool,
                           prov_len) -> list[str]:
    p = f"entry_updates[{i}]"
    if not isinstance(upd, dict):
        return [f"{p}: must be an object"]
    errs: list[str] = []
    for key in set(upd) - {"target_uuid", "set", "provenance", "note"}:
        errs.append(f"{p}.{key}: unknown key")

    target = upd.get("target_uuid")
    if not isinstance(target, str):
        errs.append(f"{p}.target_uuid: required string")
    else:
        row = conn.execute("SELECT status FROM entries WHERE uuid=?", (target,)).fetchone()
        if row is None:
            errs.append(f"{p}.target_uuid: entry {target} does not exist")
        elif row["status"] != "active":
            errs.append(f"{p}.target_uuid: entry {target} is {row['status']} — "
                        f"only active entries can be closed")

    st = upd.get("set")
    if not isinstance(st, dict) or not st:
        errs.append(f"{p}.set: required object — v2 allows closure fields only")
    else:
        # Deliberately narrow in v2: closure fields only. Cross-day edits to
        # titles/categories/etc. go through the manual corrections CLI.
        for key in set(st) - {"closure", "closed_at"}:
            errs.append(f"{p}.set.{key}: not settable via entry_updates in v2 "
                        f"(allowed: closure, closed_at)")
        closure = st.get("closure")
        if closure not in CLOSURES:
            errs.append(f"{p}.set.closure: '{closure}' must be one of "
                        f"{', '.join(CLOSURES)}")
        if "closed_at" in st and not is_iso_datetime(st["closed_at"]) \
                and not is_iso_date(st["closed_at"]):
            errs.append(f"{p}.set.closed_at: '{st['closed_at']}' is not an ISO "
                        f"date or datetime")

    prov = upd.get("provenance")
    if prov is None:
        errs.append(f"{p}.provenance: required — a closure must trace to the "
                    f"sentence that closed it")
    else:
        errs.extend(_validate_provenance(prov, f"{p}.provenance", declared_ids,
                                         multi_source, prov_len))
    return errs


def _validate_link(conn: sqlite3.Connection, link, i: int,
                   n_entries: int) -> list[str]:
    p = f"links[{i}]"
    if not isinstance(link, dict):
        return [f"{p}: must be an object"]
    errs: list[str] = []
    for key in set(link) - {"from_index", "from_uuid", "to_index", "to_uuid",
                            "type", "note"}:
        errs.append(f"{p}.{key}: unknown key")
    if link.get("type") not in LINK_TYPES:
        errs.append(f"{p}.type: '{link.get('type')}' must be one of "
                    f"{', '.join(LINK_TYPES)}")

    ends = {}
    for side in ("from", "to"):
        has_idx = f"{side}_index" in link
        has_uuid = f"{side}_uuid" in link
        if has_idx == has_uuid:
            errs.append(f"{p}: exactly one of {side}_index or {side}_uuid required")
            continue
        if has_idx:
            idx = link[f"{side}_index"]
            if not isinstance(idx, int) or not (0 <= idx < n_entries):
                errs.append(f"{p}.{side}_index: {idx!r} is not a valid index into "
                            f"entries (0..{n_entries - 1})")
            else:
                ends[side] = ("index", idx)
        else:
            u = link[f"{side}_uuid"]
            if conn.execute("SELECT 1 FROM entries WHERE uuid=?", (u,)).fetchone() is None:
                errs.append(f"{p}.{side}_uuid: entry {u} does not exist")
            else:
                ends[side] = ("uuid", u)
    if len(ends) == 2 and ends["from"] == ends["to"]:
        errs.append(f"{p}: a link cannot connect an entry to itself")
    return errs


# ---------------------------------------------------------------- entity_links

def _validate_entity_links(conn: sqlite3.Connection, links, entries_list: list,
                           declared_ids: list[int], multi_source: bool,
                           prov_len) -> list[str]:
    """Relationships between entities ("Jake from El Paso").

    Each end is an existing entity or one this draft creates — a `new` ref
    that must match a `new` ref in some entry's entities[], so a relationship
    never conjures an entity no entry mentions. Provenance is required and is
    a character span: the relationship must trace to the sentence that states
    it, never to an inference across the day."""
    if links is None:
        return []
    if not isinstance(links, list):
        return ["entity_links: must be an array"]
    known = {r["name"]: bool(r["symmetric"]) for r in conn.execute(
        "SELECT name, symmetric FROM entity_link_types")}
    new_refs: set[tuple] = set()
    for entry in entries_list:
        ents = entry.get("entities") if isinstance(entry, dict) else None
        for ref in ents if isinstance(ents, list) else []:
            if isinstance(ref, dict) and ref.get("ref") == "new" \
                    and isinstance(ref.get("name"), str):
                new_refs.add((ref.get("type"), ref["name"].strip().lower()))

    errs: list[str] = []
    seen: dict[tuple, int] = {}
    for i, link in enumerate(links):
        p = f"entity_links[{i}]"
        if not isinstance(link, dict):
            errs.append(f"{p}: must be an object")
            continue
        for key in sorted(set(link) - ENTITY_LINK_KEYS):
            errs.append(f"{p}.{key}: unknown key")
        ltype = link.get("type")
        if ltype not in known:
            errs.append(f"{p}.type: '{ltype}' is not a relationship type "
                        f"({', '.join(sorted(known))})")

        ends = {}
        for side in ("from", "to"):
            key, side_errs = _validate_link_end(conn, link.get(side),
                                                f"{p}.{side}", new_refs)
            errs.extend(side_errs)
            if key is not None:
                ends[side] = key
        if len(ends) == 2:
            if ends["from"] == ends["to"]:
                errs.append(f"{p}: a relationship cannot connect an entity to itself")
            elif ltype in known:
                pair = (ends["from"], ends["to"])
                if known[ltype]:
                    pair = tuple(sorted(pair))
                if (pair, ltype) in seen:
                    errs.append(f"{p}: same relationship as "
                                f"entity_links[{seen[(pair, ltype)]}]")
                else:
                    seen[(pair, ltype)] = i

        if link.get("note") is not None and not isinstance(link["note"], str):
            errs.append(f"{p}.note: must be a string or null")
        dates_ok = True
        for f in ("valid_from", "valid_to"):
            v = link.get(f)
            if v is not None and not (is_iso_date(v) or is_iso_datetime(v)):
                errs.append(f"{p}.{f}: '{v}' is not an ISO date or datetime")
                dates_ok = False
        vf, vt = link.get("valid_from"), link.get("valid_to")
        if dates_ok and vf is not None and vt is not None and vf > vt:
            errs.append(f"{p}: valid_from {vf} is after valid_to {vt}")
        if "entry_index" in link:
            idx = link["entry_index"]
            if not isinstance(idx, int) or not (0 <= idx < len(entries_list)):
                errs.append(f"{p}.entry_index: {idx!r} is not a valid index into "
                            f"entries (0..{len(entries_list) - 1})")

        prov = link.get("provenance")
        if prov is None:
            errs.append(f"{p}.provenance: required — a relationship must trace "
                        f"to the sentence that states it")
        elif isinstance(prov, dict) and "segments" in prov:
            errs.append(f"{p}.provenance: cite the sentence by char_start/char_end "
                        f"— segments are not accepted for relationships")
        else:
            errs.extend(_validate_provenance(prov, f"{p}.provenance", declared_ids,
                                             multi_source, prov_len))
    return errs


def _validate_link_end(conn: sqlite3.Connection, ref, path: str,
                       new_refs: set[tuple]) -> tuple[tuple | None, list[str]]:
    """(identity key, errors) for one end of an entity link; the key is None
    when the end is invalid."""
    if not isinstance(ref, dict):
        return None, [f"{path}: required object — {{\"ref\": \"existing\", "
                      f"\"id\": N}} or {{\"ref\": \"new\", \"type\": T, \"name\": \"...\"}}"]
    errs: list[str] = []
    kind = ref.get("ref")
    if kind == "existing":
        for key in sorted(set(ref) - {"ref", "id", "type"}):
            errs.append(f"{path}.{key}: unknown key")
        eid = ref.get("id")
        if not isinstance(eid, int):
            return None, errs + [f"{path}.id: existing ref requires an integer id"]
        row = conn.execute("SELECT name, type, status, merged_into FROM entities "
                           "WHERE id=?", (eid,)).fetchone()
        if row is None:
            errs.append(f"{path}.id: entity id={eid} does not exist")
        elif row["status"] == "merged":
            errs.append(f"{path}.id: entity id={eid} '{row['name']}' was merged into "
                        f"id={row['merged_into']} — use that id")
        elif "type" in ref and ref["type"] != row["type"]:
            errs.append(f"{path}.type: entity id={eid} is a {row['type']}, "
                        f"not {ref['type']}")
        return (None if errs else ("existing", eid)), errs
    if kind == "new":
        for key in sorted(set(ref) - {"ref", "type", "name"}):
            errs.append(f"{path}.{key}: unknown key")
        etype, name = ref.get("type"), ref.get("name")
        if etype not in ENTITY_TYPES:
            errs.append(f"{path}.type: '{etype}' must be one of {', '.join(ENTITY_TYPES)}")
        if not isinstance(name, str) or not name.strip():
            errs.append(f"{path}.name: new entity requires a non-empty name")
        elif etype in ENTITY_TYPES and (etype, name.strip().lower()) not in new_refs:
            errs.append(f"{path}: new {etype} '{name}' is not a new entity in this "
                        f"draft's entries — add it to the entities of the entry "
                        f"that states the relationship, or use ref:existing")
        if errs:
            return None, errs
        return ("new", etype, name.strip().lower()), errs
    return None, [f"{path}.ref: must be 'existing' or 'new'"]

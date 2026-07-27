"""Versioned extraction-contract validation.

The contract is the provider-neutral boundary: any extractor (the Claude Code
skill, a manual draft, or a future alternative) produces this JSON document,
and nothing reaches the database without passing this deterministic validation.
Errors are single-line and precise so the extractor can self-correct and retry.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import CONTRACT_VERSION
from .config import CONFIDENCE, ENTITY_TYPES, MENTION_ROLES, TIME_OF_DAY
from .domain import measurements as measurements_mod
from .domain.util import is_iso_date, is_iso_datetime

ENTRY_KEYS = {
    "kind", "categories", "title", "body", "time_of_day", "occurred_at",
    "ended_at", "due_at", "data", "confidence", "measurements", "entities",
    "provenance", "uuid",
}
TOP_KEYS = {"contract_version", "journal_date", "source", "day", "entries",
            "ambiguities", "extraction"}


def _known(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"SELECT name FROM {table} ORDER BY id")]


def validate_draft(conn: sqlite3.Connection, draft: dict,
                   transcript_text: str | None = None) -> list[str]:
    """Return a list of single-line errors; empty list means the draft is valid."""
    errors: list[str] = []
    if not isinstance(draft, dict):
        return ["draft must be a JSON object"]

    for key in set(draft) - TOP_KEYS:
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

    # --- source ---
    src = draft.get("source")
    if not isinstance(src, dict):
        errors.append("source: required object with source_id (int) or text (string)")
    else:
        has_id = "source_id" in src
        has_text = "text" in src
        if has_id == has_text:
            errors.append("source: exactly one of source_id or text is required")
        elif has_id:
            if not isinstance(src["source_id"], int):
                errors.append("source.source_id: must be an integer")
            elif conn.execute("SELECT 1 FROM sources WHERE id=?",
                              (src["source_id"],)).fetchone() is None:
                errors.append(f"source.source_id: source id={src['source_id']} does not exist")
        else:
            if not isinstance(src["text"], str) or not src["text"].strip():
                errors.append("source.text: must be a non-empty string")
            stype = src.get("type", "pasted")
            if stype not in ("pasted", "phone_transcript", "manual"):
                errors.append(
                    f"source.type: '{stype}' must be pasted, phone_transcript, or manual")
            if "recorded_at" in src and not is_iso_datetime(src["recorded_at"]):
                errors.append(f"source.recorded_at: '{src['recorded_at']}' is not ISO datetime")

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
    tlen = len(transcript_text) if transcript_text is not None else None
    for i, entry in enumerate(entries_list):
        errors.extend(_validate_entry(conn, entry, i, known_kinds, known_cats, tlen))

    # --- ambiguities ---
    amb = draft.get("ambiguities")
    if amb is not None:
        if not isinstance(amb, list):
            errors.append("ambiguities: must be an array of {note} objects")
        else:
            for i, a in enumerate(amb):
                if not isinstance(a, dict) or not isinstance(a.get("note"), str):
                    errors.append(f"ambiguities[{i}]: must be an object with a string 'note'")

    return errors


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
                    known_cats: list[str], transcript_len: int | None) -> list[str]:
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
        errs.extend(_validate_provenance(prov, f"{p}.provenance", transcript_len))

    if "uuid" in entry:
        if conn.execute("SELECT 1 FROM entries WHERE uuid=?", (entry["uuid"],)).fetchone() is None:
            errs.append(f"{p}.uuid: entry {entry['uuid']} does not exist "
                        f"(omit uuid for new entries)")
    return errs


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


def _validate_provenance(prov, path: str, transcript_len: int | None) -> list[str]:
    if not isinstance(prov, dict):
        return [f"{path}: must be an object"]
    has_chars = "char_start" in prov or "char_end" in prov
    has_segs = "segments" in prov
    if has_chars == has_segs:
        return [f"{path}: exactly one of {{char_start, char_end}} or {{segments}} required"]
    if has_chars:
        cs, ce = prov.get("char_start"), prov.get("char_end")
        if not isinstance(cs, int) or not isinstance(ce, int) or cs < 0 or ce <= cs:
            return [f"{path}: char_start/char_end must be integers with 0 <= start < end"]
        if transcript_len is not None and ce > transcript_len:
            return [f"{path}: char_end {ce} exceeds transcript length {transcript_len}"]
    else:
        segs = prov["segments"]
        if (not isinstance(segs, list) or not segs
                or any(not isinstance(s, int) or s < 0 for s in segs)):
            return [f"{path}.segments: must be a non-empty array of non-negative segment indexes"]
    return []

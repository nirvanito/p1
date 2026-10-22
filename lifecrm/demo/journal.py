"""The persona's journal as phone notes and contract-v2 drafts.

In real use a note arrives from the phone, `lifecrm intake` turns it into a
source, and the /log skill (Claude, interactively) writes the draft that
`ingest` reconciles into a diff for review. The demo has no person in the
loop, so this module plays the extractor's part: it writes each note under
the file name the phone would give it, and builds the draft the extractor
would write — every entry citing the exact character span of the sentence
that supports it, entity references resolved the way /log resolves them
(new on first mention, existing afterwards), earlier tasks closed through
`entry_updates`, threads linked.

Pure functions; `lifecrm/demo/build.py` runs them through the real CLI.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import persona

ROLLOVER_HOUR = 5


# ---------------------------------------------------------------- note files

def note_filename(anchor: date, offset: int, note: persona.Note, combo: str,
                  ext: str = ".txt") -> str:
    """What the phone names the file.

    iPhone (the Shortcuts recipe in docs/iphone.md): the JOURNAL date (clock
    minus the rollover hours), the clock time with dots, and the UTC offset.
    Android (an automation app or a recorder with a date-time template):
    the calendar date and the clock time, no offset.
    """
    dt = persona.local_dt(anchor, offset, note.time)
    if combo == "iphone":
        journal_day = (dt - timedelta(hours=ROLLOVER_HOUR)).date()
        return f"{journal_day.isoformat()} {dt:%H.%M.%S} {dt:%z}{ext}"
    return f"{dt:%Y-%m-%d %H.%M.%S}{ext}"


def note_files(anchor: date, combo: str, *, pending: bool = False) -> list[dict]:
    """Every note of the journal (or only the pending ones), in time order:
    {offset, note_index, filename, text, journal_date}."""
    out = []
    for jd in persona.JOURNAL:
        if jd.pending != pending:
            continue
        for i, note in enumerate(jd.notes):
            out.append({"offset": jd.offset, "note_index": i,
                        "filename": note_filename(anchor, jd.offset, note, combo),
                        "text": note.text,
                        "journal_date": persona.day(anchor, jd.offset).isoformat()})
    return out


# ---------------------------------------------------------------- entities

def entity_spec(ref: str) -> dict:
    """type, name and aliases for a persona reference key."""
    if ref.startswith("media:"):
        return {"type": "media", "name": ref.split(":", 1)[1], "aliases": []}
    if ref.startswith("project:"):
        return {"type": "project", "name": ref.split(":", 1)[1], "aliases": []}
    if ref in {p.key for p in persona.PEOPLE}:
        p = persona.person(ref)
        aliases = [a for a in (p.first, *p.aliases) if a and a != p.name]
        return {"type": "person", "name": p.name, "aliases": aliases}
    pl = persona.place(ref)
    return {"type": "place", "name": pl.name, "aliases": list(pl.aliases)}


def _entity_ref(ref: str, role: str, known: dict[str, int]) -> dict:
    if ref in known:
        return {"ref": "existing", "id": known[ref], "role": role}
    spec = entity_spec(ref)
    return {"ref": "new", "type": spec["type"], "name": spec["name"],
            "aliases": spec["aliases"], "role": role}


def entity_keys(jday: persona.JournalDay) -> list[str]:
    seen: list[str] = []
    for note in jday.notes:
        for _, spec in note.sentences:
            for ref, _role in (spec or {}).get("entities", []):
                if ref not in seen:
                    seen.append(ref)
    return seen


# ---------------------------------------------------------------- drafts

def _utc(anchor: date, offset: int, hhmm: str) -> str:
    return persona.utc_iso(persona.local_dt(anchor, offset, hhmm))


def build_draft(jday: persona.JournalDay, anchor: date, source_ids: list[int],
                known: dict[str, int], keyed: dict[str, str]) -> tuple[dict, list[str]]:
    """The contract-v2 draft for one journal day.

    source_ids: one per note, in note order (from intake).
    known:      persona ref key -> entity id, for entities created on earlier days.
    keyed:      persona entry key -> entry uuid, for entries applied on earlier days.

    Returns (draft, keys) where keys[i] is the persona key of entries[i]
    (or None), so the caller can learn the new uuids after apply.
    """
    if len(source_ids) != len(jday.notes):
        raise ValueError(f"day {jday.offset}: {len(jday.notes)} notes, "
                         f"{len(source_ids)} source ids")
    journal_date = persona.day(anchor, jday.offset).isoformat()
    entries: list[dict] = []
    keys: list[str | None] = []
    pending_links: list[tuple[int, str, str]] = []      # (from_index, type, key)
    updates: list[dict] = []
    relationships: list[dict] = []

    for note, source_id in zip(jday.notes, source_ids):
        pos = 0
        for i, (sentence, spec) in enumerate(note.sentences):
            if i:
                pos += 1                                   # the joining space
            start, end = pos, pos + len(sentence)
            pos = end
            if not spec:
                continue
            provenance = {"source_id": source_id, "char_start": start, "char_end": end}
            entry = {
                "kind": spec["kind"],
                "categories": {"primary": spec["primary"],
                               "secondary": list(spec.get("secondary", []))},
                "title": spec["title"],
                "confidence": "high",
                "provenance": provenance,
            }
            if spec.get("body"):
                entry["body"] = spec["body"]
            if spec.get("time_of_day"):
                entry["time_of_day"] = spec["time_of_day"]
            if spec.get("occurred"):
                entry["occurred_at"] = _utc(anchor, jday.offset, spec["occurred"])
            if spec.get("due_offset") is not None:
                entry["due_at"] = persona.day(anchor, spec["due_offset"]).isoformat()
            ms = []
            for metric, value, unit in spec.get("measurements", []):
                m = {"metric": metric, "numeric_value": value}
                if unit:
                    m["unit"] = unit
                ms.append(m)
            if ms:
                entry["measurements"] = ms
            refs = [_entity_ref(ref, role, known) for ref, role in spec.get("entities", [])]
            if refs:
                entry["entities"] = refs
            idx = len(entries)
            entries.append(entry)
            keys.append(spec.get("key"))
            for link_type, target in spec.get("links", []):
                pending_links.append((idx, link_type, target))
            for src, rel_type, dst in spec.get("relations", []):
                ends = []
                for ref in (src, dst):
                    if ref in known:
                        ends.append({"ref": "existing", "id": known[ref]})
                    else:
                        s = entity_spec(ref)
                        ends.append({"ref": "new", "type": s["type"], "name": s["name"]})
                relationships.append({"from": ends[0], "to": ends[1], "type": rel_type,
                                      "entry_index": idx, "provenance": provenance})
            if spec.get("closes"):
                target_uuid = keyed.get(spec["closes"])
                if target_uuid:
                    closed = spec.get("occurred") or note.time[:5]
                    updates.append({
                        "target_uuid": target_uuid,
                        "set": {"closure": "done",
                                "closed_at": _utc(anchor, jday.offset, closed)},
                        "provenance": provenance,
                        "note": sentence,
                    })

    links = []
    for from_index, link_type, target in pending_links:
        if target in keys:
            links.append({"from_index": from_index, "to_index": keys.index(target),
                          "type": link_type})
        elif target in keyed:
            links.append({"from_index": from_index, "to_uuid": keyed[target],
                          "type": link_type})

    measurements = []
    if jday.mood is not None:
        measurements.append({"metric": "mood", "numeric_value": jday.mood})
    if jday.energy is not None:
        measurements.append({"metric": "energy", "numeric_value": jday.energy})
    if jday.sleep_hours is not None:
        measurements.append({"metric": "sleep_hours", "numeric_value": jday.sleep_hours,
                             "unit": "h"})

    draft = {
        "contract_version": 2,
        "journal_date": journal_date,
        "sources": [{"source_id": s} for s in source_ids],
        "extraction": {"provider": "lifecrm-demo", "model": None, "interface": "manual"},
        "day": {"summary": jday.summary, "measurements": measurements},
        "entries": entries,
        "ambiguities": [{"note": "Demo draft written from the synthetic persona; it "
                                 "stands in for a /log extraction."}],
    }
    if updates:
        draft["entry_updates"] = updates
    if links:
        draft["links"] = links
    if relationships:
        draft["entity_links"] = relationships
    return draft, keys

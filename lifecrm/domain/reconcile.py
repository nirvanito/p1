"""Reconciliation: a validated draft never overwrites a day — it produces a
reviewable diff, and apply persists exactly what the diff says.

Guarantees:
- Confirmed or manually edited records are never silently overwritten; changes
  to them surface as requires-decision items that stay skipped unless accepted.
- Stable UUIDs: matched entries keep their identity across re-extraction.
- Retractions are only ever *proposed* (same-source entries missing from a
  re-extraction), never applied without acceptance.
- v2: one run may cover several same-day sources; every applied entry's
  provenance names WHICH source supports it. Cross-day closures arrive as
  `entry_update` items that ALWAYS require a decision. Links ride the diff
  and are resolved (draft index -> uuid) at apply time.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from difflib import SequenceMatcher

from . import audit, corrections, days, entities as entities_mod, entries as entries_mod
from . import links as links_mod
from . import measurements as measurements_mod
from .util import DomainError, new_uuid, now_utc

DIFF_VERSION = 3
DUPLICATE_TITLE_SIM = 0.9
DUPLICATE_RELATED_SIM = 0.75

COMPARE_FIELDS = ["title", "body", "kind", "time_of_day", "occurred_at",
                  "ended_at", "due_at", "confidence", "data"]


def create_extraction_run(conn: sqlite3.Connection, *, source_ids: list[int],
                          provider: str, model: str | None, interface: str,
                          contract_version: int, input_hash: str) -> dict:
    if not source_ids:
        raise DomainError("an extraction run needs at least one source")
    uid = new_uuid()
    cur = conn.execute(
        "INSERT INTO extraction_runs (uuid, provider, model, interface, "
        "contract_version, input_hash, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')",
        (uid, provider, model, interface, contract_version, input_hash, now_utc()))
    run_id = cur.lastrowid
    for sid in source_ids:
        conn.execute(
            "INSERT INTO extraction_run_sources (extraction_run_id, source_id) "
            "VALUES (?, ?)", (run_id, sid))
    return {"id": run_id, "uuid": uid, "source_ids": list(source_ids)}


def run_source_ids(conn: sqlite3.Connection, run_id: int) -> list[int]:
    return [r["source_id"] for r in conn.execute(
        "SELECT source_id FROM extraction_run_sources WHERE extraction_run_id=? "
        "ORDER BY source_id", (run_id,))]


def _title_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _draft_vs_existing_changes(conn: sqlite3.Connection, draft_entry: dict,
                               snap: dict) -> dict:
    """Field-level changes a draft proposes against an existing entry snapshot.
    Scalar fields and categories only; entities/measurements are corrected via
    the corrections workflow, not re-extraction."""
    changes: dict[str, dict] = {}
    for f in COMPARE_FIELDS:
        new = draft_entry.get(f) if f != "data" else draft_entry.get("data")
        if f == "confidence":
            new = draft_entry.get("confidence", "high")
        old = snap.get(f)
        if f == "body" and (new or None) == (old or None):
            continue
        if f == "kind" and isinstance(new, str) and isinstance(old, str) \
                and new.lower() == old.lower():
            continue
        if new is not None and new != old:
            changes[f] = {"old": old, "new": new}
    d_primary = draft_entry["categories"]["primary"].lower()
    d_secondary = sorted(s.lower() for s in draft_entry["categories"].get("secondary", []))
    e_primary = (snap["categories"]["primary"] or "").lower()
    e_secondary = sorted(s.lower() for s in snap["categories"]["secondary"])
    if d_primary != e_primary:
        changes["primary_category"] = {"old": snap["categories"]["primary"],
                                       "new": draft_entry["categories"]["primary"]}
    if d_secondary != e_secondary:
        changes["secondary_categories"] = {"old": snap["categories"]["secondary"],
                                           "new": draft_entry["categories"].get("secondary", [])}
    return changes


def update_targets(diff: dict) -> list[str]:
    """The cross-day entries an entry_update item points at."""
    return sorted({i["target_uuid"] for i in diff.get("items", [])
                   if i.get("action") == "entry_update"})


def day_fingerprint(conn: sqlite3.Connection, journal_date: str,
                    target_uuids: list[str] | tuple[str, ...] = ()) -> str:
    """Hash of everything build_diff read about a day.

    A diff is a reviewed statement about one before-state: "these six entries
    are new, that one is a duplicate of the entry you already have". Apply it
    against a different before-state and the review no longer describes
    reality — most visibly by ingesting the same day twice and applying both
    diffs, which used to add every entry a second time because the second diff
    had been built against the empty day. Stamped at build, re-checked at
    apply.

    Deliberately broad: entries of every status (an entry can be restored or
    moved between days), their full snapshots (an entry's entities and
    measurements are what `changes` is computed from), which sources support
    them (that is what turns an omission into a retract proposal), the day's
    summary and day-owned measurements, and the current state of any cross-day
    entry_update target. Over-sensitivity costs one `ingest` re-run; under-
    sensitivity costs silent duplicate history.
    """
    entries_state = []
    for row in conn.execute("SELECT id, uuid FROM entries WHERE journal_date=? "
                            "ORDER BY uuid", (journal_date,)):
        snap = entries_mod.snapshot(conn, row["id"])
        snap["sources"] = [r["source_id"] for r in conn.execute(
            "SELECT source_id FROM entry_sources WHERE entry_id=? ORDER BY source_id",
            (row["id"],))]
        entries_state.append(snap)
    day_row = conn.execute("SELECT summary FROM days WHERE journal_date=?",
                           (journal_date,)).fetchone()
    day_measurements = [dict(r) for r in conn.execute(
        "SELECT uuid, metric, numeric_value, text_value, unit, status "
        "FROM measurements WHERE journal_date=? ORDER BY uuid", (journal_date,))]
    targets = []
    for uuid_ in sorted(set(target_uuids)):
        row = conn.execute(
            "SELECT journal_date, status, closure, closed_at, confirmed_at "
            "FROM entries WHERE uuid=?", (uuid_,)).fetchone()
        targets.append({"uuid": uuid_,
                        **(dict(row) if row is not None else {"missing": True})})
    blob = json.dumps({"entries": entries_state,
                       "summary": day_row["summary"] if day_row else None,
                       "measurements": day_measurements,
                       "targets": targets},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def build_diff(conn: sqlite3.Connection, draft: dict, extraction_run: dict) -> dict:
    """Compare a validated draft against the existing records for its day."""
    journal_date = draft["journal_date"]
    source_ids = [s["source_id"] for s in draft["sources"]]

    existing_rows = conn.execute(
        "SELECT id, uuid, confirmed_at FROM entries "
        "WHERE journal_date=? AND status='active' ORDER BY id",
        (journal_date,)).fetchall()
    existing = {}
    for row in existing_rows:
        snap = entries_mod.snapshot(conn, row["id"])
        from_sources = [r["source_id"] for r in conn.execute(
            "SELECT source_id FROM entry_sources WHERE entry_id=?", (row["id"],))]
        existing[row["uuid"]] = {"id": row["id"], "snap": snap,
                                 "confirmed": bool(row["confirmed_at"]),
                                 "sources": from_sources}

    items: list[dict] = []
    matched: set[str] = set()

    for index, draft_entry in enumerate(draft.get("entries", [])):
        target_uuid = draft_entry.get("uuid")
        if target_uuid:
            info = existing.get(target_uuid)
            if info is None:
                # Exists (contract validated) but not active on this date.
                row = entries_mod.get_by_uuid(conn, target_uuid)
                info = {"id": row["id"], "snap": entries_mod.snapshot(conn, row["id"]),
                        "confirmed": bool(row["confirmed_at"]), "sources": []}
            matched.add(target_uuid)
            changes = _draft_vs_existing_changes(conn, draft_entry, info["snap"])
            if not changes:
                items.append({"action": "unchanged", "index": index,
                              "entry_uuid": target_uuid,
                              "title": info["snap"]["title"]})
            else:
                items.append({"action": "update", "index": index,
                              "entry_uuid": target_uuid,
                              "title": info["snap"]["title"], "changes": changes,
                              "requires_decision": info["confirmed"],
                              "accepted": None if info["confirmed"] else True})
            continue

        best_uuid, best_sim = None, 0.0
        for uuid_, info in existing.items():
            if uuid_ in matched:
                continue
            sim = _title_sim(draft_entry["title"], info["snap"]["title"])
            if sim > best_sim:
                best_uuid, best_sim = uuid_, sim
        is_dup = False
        if best_uuid is not None:
            info = existing[best_uuid]
            same_cat = (draft_entry["categories"]["primary"].lower()
                        == (info["snap"]["categories"]["primary"] or "").lower())
            same_kind = draft_entry["kind"].lower() == info["snap"]["kind"].lower()
            if best_sim >= DUPLICATE_TITLE_SIM or (
                    best_sim >= DUPLICATE_RELATED_SIM and same_cat and same_kind):
                is_dup = True
        if is_dup:
            matched.add(best_uuid)
            items.append({"action": "possible_duplicate", "index": index,
                          "entry": draft_entry, "of_entry_uuid": best_uuid,
                          "of_title": existing[best_uuid]["snap"]["title"],
                          "similarity": round(best_sim, 3), "accepted": None})
        else:
            items.append({"action": "add", "index": index, "entry": draft_entry})

    # Unmatched existing entries. Same-source omissions become retract
    # *proposals* (accepted=None -> skipped unless explicitly accepted, even for
    # confirmed entries — surfaced, never silent). Confirmed entries from other
    # sources are listed as untouched; unconfirmed ones are left alone.
    for uuid_, info in existing.items():
        if uuid_ in matched:
            continue
        if any(sid in info["sources"] for sid in source_ids):
            items.append({"action": "retract", "entry_uuid": uuid_,
                          "title": info["snap"]["title"],
                          "reason": "re-extraction of the same source omitted this entry",
                          "requires_decision": True,
                          "accepted": None})
        elif info["confirmed"]:
            items.append({"action": "unchanged_confirmed", "entry_uuid": uuid_,
                          "title": info["snap"]["title"]})

    # Cross-day closures: ALWAYS a decision, never auto-applied.
    for upd in draft.get("entry_updates", []) or []:
        target = entries_mod.get_by_uuid(conn, upd["target_uuid"])
        items.append({
            "action": "entry_update",
            "target_uuid": upd["target_uuid"],
            "target_title": target["title"],
            "target_date": target["journal_date"],
            "set": upd["set"],
            "current": {"closure": target["closure"],
                        "closed_at": target["closed_at"],
                        "confirmed": bool(target["confirmed_at"])},
            "provenance": upd.get("provenance"),
            "note": upd.get("note"),
            "requires_decision": True,
            "accepted": None,
        })

    # Links ride the diff for visibility; the reviewed apply creates them.
    for link in draft.get("links", []) or []:
        items.append({
            "action": "link",
            "from": ({"index": link["from_index"]} if "from_index" in link
                     else {"uuid": link["from_uuid"]}),
            "to": ({"index": link["to_index"]} if "to_index" in link
                   else {"uuid": link["to_uuid"]}),
            "type": link["type"],
            "note": link.get("note"),
        })

    # Day summary + day-owned measurements.
    day_diff: dict = {"summary": {"action": "none"}, "measurements": []}
    draft_day = draft.get("day") or {}
    if draft_day.get("summary"):
        row = conn.execute("SELECT summary FROM days WHERE journal_date=?",
                           (journal_date,)).fetchone()
        existing_summary = row["summary"] if row else None
        if existing_summary is None or existing_summary == draft_day["summary"]:
            day_diff["summary"] = {"action": "set", "value": draft_day["summary"]}
        else:
            day_diff["summary"] = {"action": "conflict", "value": draft_day["summary"],
                                   "existing": existing_summary, "accepted": None}
    for m in draft_day.get("measurements", []) or []:
        row = conn.execute(
            "SELECT uuid, numeric_value, text_value, unit FROM measurements "
            "WHERE journal_date=? AND metric=? AND status='active'",
            (journal_date, m["metric"])).fetchone()
        if row is None:
            day_diff["measurements"].append({"action": "add", "measurement": m})
        elif (row["numeric_value"] == m.get("numeric_value")
              and row["text_value"] == m.get("text_value")
              and row["unit"] == m.get("unit")):
            day_diff["measurements"].append({"action": "skip", "measurement": m,
                                             "reason": "identical value already recorded"})
        else:
            day_diff["measurements"].append({
                "action": "conflict", "measurement": m,
                "existing": {"uuid": row["uuid"], "numeric_value": row["numeric_value"],
                             "text_value": row["text_value"], "unit": row["unit"]},
                "accepted": None})

    counts = {
        "add": sum(1 for i in items if i["action"] == "add"),
        "update": sum(1 for i in items if i["action"] == "update"),
        "possible_duplicate": sum(1 for i in items if i["action"] == "possible_duplicate"),
        "retract_proposals": sum(1 for i in items if i["action"] == "retract"),
        "entry_updates": sum(1 for i in items if i["action"] == "entry_update"),
        "links": sum(1 for i in items if i["action"] == "link"),
        "unchanged": sum(1 for i in items if i["action"] == "unchanged"),
        "unchanged_confirmed": sum(1 for i in items if i["action"] == "unchanged_confirmed"),
        "requires_decision": sum(1 for i in items
                                 if i.get("requires_decision") and i.get("accepted") is None),
    }
    diff = {
        "diff_version": DIFF_VERSION,
        "journal_date": journal_date,
        "source_ids": source_ids,
        "extraction_run_uuid": extraction_run["uuid"],
        "counts": counts,
        "day": day_diff,
        "items": items,
        "ambiguities": draft.get("ambiguities", []),
    }
    diff["day_fingerprint"] = day_fingerprint(conn, journal_date,
                                              update_targets(diff))
    return diff


def apply_diff(conn: sqlite3.Connection, diff: dict) -> dict:
    """Persist a reviewed diff. The caller owns the transaction boundary.

    Applying a diff is itself the user's confirmation (single-keystroke apply),
    so created entries get confirmed_at. Items requiring a decision are applied
    only when accepted is exactly true.
    """
    if diff.get("diff_version") != DIFF_VERSION:
        raise DomainError(f"diff_version must be {DIFF_VERSION} — re-run ingest "
                          f"to rebuild an old diff")
    run = conn.execute("SELECT * FROM extraction_runs WHERE uuid=?",
                       (diff["extraction_run_uuid"],)).fetchone()
    if run is None:
        raise DomainError(f"extraction run {diff['extraction_run_uuid']} does not exist")
    if run["status"] == "applied":
        raise DomainError(
            f"extraction run {diff['extraction_run_uuid']} was already applied — "
            f"re-run ingest to build a fresh diff")

    journal_date = diff["journal_date"]
    # Staleness: the review was of a specific before-state. Checked before any
    # write, and never bypassable — rebuilding a diff costs one `ingest`.
    if diff.get("day_fingerprint") != day_fingerprint(conn, journal_date,
                                                      update_targets(diff)):
        raise DomainError(
            f"{journal_date} has changed since this diff was built — re-run "
            f"ingest to rebuild it")

    source_ids = diff.get("source_ids") or []
    if not source_ids:
        raise DomainError("diff has no source_ids")
    days.ensure_day(conn, journal_date)
    transcript_by_source: dict[int, int | None] = {}
    for sid in source_ids:
        row = conn.execute(
            "SELECT id FROM transcripts WHERE source_id=? ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
        transcript_by_source[sid] = row["id"] if row else None
    sole_source = source_ids[0] if len(source_ids) == 1 else None

    created_entities: dict[tuple[str, str], int] = {}
    index_to_uuid: dict[int, str] = {}
    summary = {"added": 0, "updated": 0, "retracted": 0, "closed": 0,
               "links_created": 0, "skipped_duplicates": 0,
               "skipped_decisions": 0, "unchanged": 0, "entities_created": 0,
               "day_measurements": 0}

    def _resolve_entity(ref: dict) -> int:
        if ref["ref"] == "existing":
            return entities_mod.resolve_merged(conn, ref["id"])
        key = (ref["type"], ref["name"].strip().lower())
        if key in created_entities:
            return created_entities[key]
        hit = conn.execute(
            "SELECT id FROM entities WHERE status='active' AND type=? AND name=? COLLATE NOCASE",
            (ref["type"], ref["name"].strip())).fetchone()
        if hit:
            eid = hit["id"]
        else:
            created = entities_mod.create(conn, ref["type"], ref["name"],
                                          aliases=ref.get("aliases"),
                                          actor="extraction")
            eid = created["id"]
            summary["entities_created"] += 1
        created_entities[key] = eid
        return eid

    def _prov_source(prov: dict | None) -> int:
        if prov and prov.get("source_id") is not None:
            return prov["source_id"]
        if sole_source is not None:
            return sole_source
        raise DomainError("multi-source diff item lacks provenance.source_id")

    def _add_entry(draft_entry: dict) -> str:
        created = entries_mod.create(
            conn,
            journal_date=journal_date,
            kind=draft_entry["kind"],
            title=draft_entry["title"],
            primary_category=draft_entry["categories"]["primary"],
            secondary_categories=draft_entry["categories"].get("secondary", []),
            body=draft_entry.get("body"),
            time_of_day=draft_entry.get("time_of_day"),
            occurred_at=draft_entry.get("occurred_at"),
            ended_at=draft_entry.get("ended_at"),
            due_at=draft_entry.get("due_at"),
            data=draft_entry.get("data"),
            confidence=draft_entry.get("confidence", "high"),
            actor="extraction",
            confirmed=True,   # applying the reviewed diff is the confirmation
        )
        for ref in draft_entry.get("entities", []) or []:
            entries_mod.attach_entity(conn, created["id"], _resolve_entity(ref),
                                      ref.get("role", "mention"), actor="extraction")
        for m in draft_entry.get("measurements", []) or []:
            measurements_mod.add(
                conn, entry_id=created["id"], metric=m["metric"],
                numeric_value=m.get("numeric_value"), text_value=m.get("text_value"),
                unit=m.get("unit"), confidence=m.get("confidence", "high"),
                extraction_run_id=run["id"], actor="extraction")
        prov = draft_entry.get("provenance") or {}
        prov_sid = _prov_source(prov)
        segs = prov.get("segments")
        entries_mod.add_provenance(
            conn, entry_id=created["id"], source_id=prov_sid,
            transcript_id=transcript_by_source.get(prov_sid),
            segment_start=min(segs) if segs else None,
            segment_end=max(segs) if segs else None,
            char_start=prov.get("char_start"), char_end=prov.get("char_end"),
            extraction_run_id=run["id"], original_json=draft_entry)
        summary["added"] += 1
        return created["uuid"]

    link_items: list[dict] = []
    for item in diff.get("items", []):
        action = item["action"]
        if action == "add":
            uuid_ = _add_entry(item["entry"])
            if "index" in item:
                index_to_uuid[item["index"]] = uuid_
        elif action == "possible_duplicate":
            if item.get("accepted") is True:
                uuid_ = _add_entry(item["entry"])
            else:
                uuid_ = item["of_entry_uuid"]   # links resolve to the survivor
                summary["skipped_duplicates"] += 1
            if "index" in item:
                index_to_uuid[item["index"]] = uuid_
        elif action == "update":
            if "index" in item:
                index_to_uuid[item["index"]] = item["entry_uuid"]
            if item.get("requires_decision") and item.get("accepted") is not True:
                summary["skipped_decisions"] += 1
                continue
            changes = {f: c["new"] for f, c in item["changes"].items()}
            actor = "user" if item.get("requires_decision") else "extraction"
            corrections.edit_entry(conn, item["entry_uuid"], changes, actor=actor,
                                   note=f"reconciliation {diff['extraction_run_uuid']}")
            summary["updated"] += 1
        elif action == "retract":
            if item.get("accepted") is True:
                corrections.retract_entry(conn, item["entry_uuid"], actor="user",
                                          reason=item.get("reason"))
                summary["retracted"] += 1
            else:
                summary["skipped_decisions"] += 1
        elif action == "entry_update":
            if item.get("accepted") is not True:
                summary["skipped_decisions"] += 1
                continue
            st = item["set"]
            corrections.close_entry(conn, item["target_uuid"], st["closure"],
                                    closed_at=st.get("closed_at"), actor="user",
                                    note=item.get("note")
                                    or f"closed via {diff['extraction_run_uuid']}")
            prov = item.get("provenance")
            if prov:
                prov_sid = _prov_source(prov)
                target_row = entries_mod.get_by_uuid(conn, item["target_uuid"])
                segs = prov.get("segments")
                entries_mod.add_provenance(
                    conn, entry_id=target_row["id"], source_id=prov_sid,
                    transcript_id=transcript_by_source.get(prov_sid),
                    segment_start=min(segs) if segs else None,
                    segment_end=max(segs) if segs else None,
                    char_start=prov.get("char_start"), char_end=prov.get("char_end"),
                    extraction_run_id=run["id"],
                    original_json={"entry_update": item["set"]})
            summary["closed"] += 1
        elif action == "link":
            link_items.append(item)   # resolved after every index is mapped
        elif action in ("unchanged", "unchanged_confirmed"):
            if "index" in item:
                index_to_uuid[item["index"]] = item["entry_uuid"]
            summary["unchanged"] += 1
        else:
            raise DomainError(f"unknown diff item action '{action}'")

    def _resolve_end(end: dict) -> str:
        if "uuid" in end:
            return end["uuid"]
        idx = end["index"]
        if idx not in index_to_uuid:
            raise DomainError(f"link references entries[{idx}], which was not applied")
        return index_to_uuid[idx]

    for item in link_items:
        from_uuid = _resolve_end(item["from"])
        to_uuid = _resolve_end(item["to"])
        if from_uuid == to_uuid:
            continue  # both ends collapsed onto the same surviving entry
        if links_mod.add(conn, from_uuid, to_uuid, item["type"],
                         note=item.get("note"), actor="extraction"):
            summary["links_created"] += 1

    day_diff = diff.get("day") or {}
    sm = day_diff.get("summary", {})
    if sm.get("action") == "set" or (sm.get("action") == "conflict"
                                     and sm.get("accepted") is True):
        days.set_summary(conn, journal_date, sm["value"])
    for md in day_diff.get("measurements", []):
        m = md["measurement"]
        if md["action"] == "add" or (md["action"] == "conflict"
                                     and md.get("accepted") is True):
            if md["action"] == "conflict":
                measurements_mod.retract(conn, md["existing"]["uuid"], actor="user")
            measurements_mod.add(
                conn, journal_date=journal_date, metric=m["metric"],
                numeric_value=m.get("numeric_value"), text_value=m.get("text_value"),
                unit=m.get("unit"), confidence=m.get("confidence", "high"),
                extraction_run_id=run["id"],
                actor="user" if md["action"] == "conflict" else "extraction")
            summary["day_measurements"] += 1

    now = now_utc()
    conn.execute(
        "UPDATE extraction_runs SET status='applied', reviewed_at=? WHERE id=?",
        (now, run["id"]))
    audit.log(conn, "user", "apply_diff", "extraction_runs", run["uuid"],
              {"journal_date": journal_date, **summary})
    return summary

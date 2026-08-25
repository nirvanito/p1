"""Export: the archive must outlive the code.

Two formats, both deterministic (stable ordering, no timestamps in content —
the manifest carries the only timestamp), so consecutive exports diff cleanly:

- jsonl: one file per table, one JSON object per line, UUIDs and names in
  place of integer ids wherever a stable identity exists, plus manifest.json.
- markdown: YYYY/YYYY-MM-DD.md per day plus _entities/<slug>.md pages with
  [[Wiki Links]] — the artifact that has to make sense to a person in 2046
  with no tooling at all.

This is a one-way dump: no sync, no vault config, no live updating.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import CONTRACT_VERSION, SCHEMA_VERSION
from .config import TIME_OF_DAY
from .domain.util import now_utc


def _jl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                   for r in rows)


def _parse(text: str | None):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def export_jsonl(conn: sqlite3.Connection, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    q = conn.execute
    entry_uuid = {r["id"]: r["uuid"] for r in q("SELECT id, uuid FROM entries")}
    entity_uuid = {r["id"]: r["uuid"] for r in q("SELECT id, uuid FROM entities")}
    source_uuid = {r["id"]: r["uuid"] for r in q("SELECT id, uuid FROM sources")}
    run_uuid = {r["id"]: r["uuid"] for r in q("SELECT id, uuid FROM extraction_runs")}
    import_uuid = {r["id"]: r["uuid"] for r in q("SELECT id, uuid FROM imports")}
    obs_uuid = {r["id"]: r["uuid"] for r in q("SELECT id, uuid FROM observations")}
    kind_name = {r["id"]: r["name"] for r in q("SELECT id, name FROM entry_kinds")}
    cat_name = {r["id"]: r["name"] for r in q("SELECT id, name FROM categories")}
    # transcripts have no uuid: identify as (source_uuid, ordinal within source)
    transcript_key: dict[int, tuple[str, int]] = {}
    for r in q("SELECT id, source_id FROM transcripts ORDER BY source_id, id"):
        ordinal = sum(1 for k in transcript_key.values()
                      if k[0] == source_uuid[r["source_id"]])
        transcript_key[r["id"]] = (source_uuid[r["source_id"]], ordinal)

    tables: dict[str, list[dict]] = {}

    tables["meta"] = [dict(r) for r in q("SELECT key, value FROM meta ORDER BY key")]
    tables["days"] = [dict(r) for r in q(
        "SELECT journal_date, summary, created_at, updated_at FROM days "
        "ORDER BY journal_date")]
    tables["categories"] = [dict(r) for r in q(
        "SELECT name, created_at FROM categories ORDER BY id")]
    tables["entry_kinds"] = [dict(r) for r in q(
        "SELECT name, created_at FROM entry_kinds ORDER BY id")]
    tables["sources"] = [dict(r) for r in q(
        "SELECT uuid, type, path, content_hash, recorded_at, timezone, "
        "journal_date, created_at FROM sources ORDER BY journal_date, uuid")]
    tables["transcripts"] = [
        {"source_uuid": transcript_key[r["id"]][0],
         "transcript_index": transcript_key[r["id"]][1],
         "engine": r["engine"], "model": r["model"], "language": r["language"],
         "duration_sec": r["duration_sec"], "text": r["text"],
         "created_at": r["created_at"]}
        for r in q("SELECT * FROM transcripts ORDER BY source_id, id")]
    tables["transcript_segments"] = [
        {"source_uuid": transcript_key[r["transcript_id"]][0],
         "transcript_index": transcript_key[r["transcript_id"]][1],
         "idx": r["idx"], "start_sec": r["start_sec"], "end_sec": r["end_sec"],
         "char_start": r["char_start"], "char_end": r["char_end"], "text": r["text"]}
        for r in q("SELECT * FROM transcript_segments "
                   "ORDER BY transcript_id, idx")]
    tables["extraction_runs"] = [
        {"uuid": r["uuid"], "provider": r["provider"], "model": r["model"],
         "interface": r["interface"], "contract_version": r["contract_version"],
         "input_hash": r["input_hash"], "started_at": r["started_at"],
         "reviewed_at": r["reviewed_at"], "status": r["status"],
         "source_uuids": sorted(source_uuid[s["source_id"]] for s in q(
             "SELECT source_id FROM extraction_run_sources "
             "WHERE extraction_run_id=?", (r["id"],)))}
        for r in q("SELECT * FROM extraction_runs ORDER BY uuid")]
    tables["entries"] = [
        {"uuid": r["uuid"], "journal_date": r["journal_date"],
         "kind": kind_name[r["kind_id"]], "title": r["title"], "body": r["body"],
         "time_of_day": r["time_of_day"], "occurred_at": r["occurred_at"],
         "ended_at": r["ended_at"], "due_at": r["due_at"],
         "data": _parse(r["data"]), "confidence": r["confidence"],
         "status": r["status"],
         "superseded_by_uuid": entry_uuid.get(r["superseded_by"]),
         "confirmed_at": r["confirmed_at"], "closed_at": r["closed_at"],
         "closure": r["closure"], "created_at": r["created_at"],
         "updated_at": r["updated_at"]}
        for r in q("SELECT * FROM entries ORDER BY journal_date, uuid")]
    tables["entry_categories"] = [
        {"entry_uuid": entry_uuid[r["entry_id"]],
         "category": cat_name[r["category_id"]],
         "is_primary": bool(r["is_primary"])}
        for r in q("SELECT ec.* FROM entry_categories ec "
                   "JOIN entries e ON e.id=ec.entry_id "
                   "ORDER BY e.uuid, ec.is_primary DESC, ec.category_id")]
    revisions = []
    seq: dict[str, int] = {}
    for r in q("SELECT * FROM entry_revisions ORDER BY entry_uuid, id"):
        n = seq.get(r["entry_uuid"], 0)
        seq[r["entry_uuid"]] = n + 1
        revisions.append({"entry_uuid": r["entry_uuid"], "seq": n,
                          "revised_at": r["revised_at"], "actor": r["actor"],
                          "change_kind": r["change_kind"],
                          "old": _parse(r["old_json"]), "new": _parse(r["new_json"]),
                          "note": r["note"]})
    tables["entry_revisions"] = revisions
    tables["measurements"] = [
        {"uuid": r["uuid"], "entry_uuid": entry_uuid.get(r["entry_id"]),
         "journal_date": r["journal_date"], "metric": r["metric"],
         "numeric_value": r["numeric_value"], "text_value": r["text_value"],
         "unit": r["unit"], "normalized_value": r["normalized_value"],
         "normalized_unit": r["normalized_unit"], "confidence": r["confidence"],
         "extraction_run_uuid": run_uuid.get(r["extraction_run_id"]),
         "status": r["status"], "created_at": r["created_at"]}
        for r in q("SELECT * FROM measurements ORDER BY uuid")]
    tables["entry_sources"] = [
        {"entry_uuid": entry_uuid[r["entry_id"]],
         "source_uuid": source_uuid[r["source_id"]],
         "transcript_index": (transcript_key[r["transcript_id"]][1]
                              if r["transcript_id"] else None),
         "segment_start": r["segment_start"], "segment_end": r["segment_end"],
         "char_start": r["char_start"], "char_end": r["char_end"],
         "extraction_run_uuid": run_uuid.get(r["extraction_run_id"]),
         "original": _parse(r["original_json"]),
         "confirmed": _parse(r["confirmed_json"])}
        for r in q("SELECT es.* FROM entry_sources es "
                   "JOIN entries e ON e.id=es.entry_id "
                   "JOIN sources s ON s.id=es.source_id ORDER BY e.uuid, s.uuid")]
    tables["entities"] = [
        {"uuid": r["uuid"], "type": r["type"], "name": r["name"],
         "slug": r["slug"], "notes": r["notes"], "status": r["status"],
         "merged_into_uuid": entity_uuid.get(r["merged_into"]),
         "created_at": r["created_at"], "updated_at": r["updated_at"]}
        for r in q("SELECT * FROM entities ORDER BY uuid")]
    tables["entity_aliases"] = [
        {"entity_uuid": entity_uuid[r["entity_id"]], "alias": r["alias"]}
        for r in q("SELECT ea.* FROM entity_aliases ea "
                   "JOIN entities e ON e.id=ea.entity_id "
                   "ORDER BY e.uuid, ea.alias")]
    tables["mentions"] = [
        {"entry_uuid": entry_uuid[r["entry_id"]],
         "entity_uuid": entity_uuid[r["entity_id"]], "role": r["role"]}
        for r in q("SELECT m.* FROM mentions m "
                   "JOIN entries en ON en.id=m.entry_id "
                   "JOIN entities et ON et.id=m.entity_id "
                   "ORDER BY en.uuid, et.uuid, m.role")]
    tables["entry_links"] = [
        {"from_uuid": r["from_uuid"], "to_uuid": r["to_uuid"], "type": r["type"],
         "note": r["note"], "actor": r["actor"], "created_at": r["created_at"]}
        for r in q("SELECT * FROM entry_links ORDER BY from_uuid, to_uuid, type")]
    tables["audit_log"] = [
        {"seq": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
         "table_name": r["table_name"], "row_uuid": r["row_uuid"],
         "details": _parse(r["details"])}
        for r in q("SELECT * FROM audit_log ORDER BY id")]
    tables["recall_log"] = [
        {"seq": r["id"], "asked_at": r["asked_at"], "question": r["question"],
         "answered": bool(r["answered"]), "answer_summary": r["answer_summary"]}
        for r in q("SELECT * FROM recall_log ORDER BY id")]
    tables["health_records"] = [
        {"uuid": r["uuid"], "metric": r["metric"],
         "journal_date": r["journal_date"], "started_at": r["started_at"],
         "ended_at": r["ended_at"], "utc_offset_sec": r["utc_offset_sec"],
         "value": r["value"], "unit": r["unit"],
         "detail": _parse(r["value_json"]), "origin_app": r["origin_app"],
         "external_id": r["external_id"], "source_file": r["source_file"],
         "last_modified_at": r["last_modified_at"], "status": r["status"],
         "created_at": r["created_at"], "updated_at": r["updated_at"]}
        for r in q("SELECT * FROM health_records ORDER BY metric, external_id")]

    # --- observed tier (v4): bulk-imported records, never review-gated ---
    tables["imports"] = [
        {"uuid": r["uuid"], "connector": r["connector"], "account": r["account"],
         "source_file": r["source_file"], "source_hash": r["source_hash"],
         "started_at": r["started_at"], "finished_at": r["finished_at"],
         "status": r["status"], "counts": _parse(r["counts_json"]),
         "created_at": r["created_at"]}
        for r in q("SELECT * FROM imports ORDER BY uuid")]
    tables["observations"] = [
        {"uuid": r["uuid"], "stream": r["stream"],
         "import_uuid": import_uuid[r["import_id"]],
         "journal_date": r["journal_date"], "started_at": r["started_at"],
         "ended_at": r["ended_at"], "utc_offset_sec": r["utc_offset_sec"],
         "direction": r["direction"], "title": r["title"], "body": r["body"],
         "value": r["value"], "unit": r["unit"], "detail": _parse(r["value_json"]),
         "media_path": r["media_path"], "origin_app": r["origin_app"],
         "external_id": r["external_id"],
         "last_modified_at": r["last_modified_at"], "status": r["status"],
         "created_at": r["created_at"], "updated_at": r["updated_at"]}
        for r in q("SELECT * FROM observations ORDER BY stream, external_id")]
    tables["entity_handles"] = [
        {"kind": r["kind"], "value": r["value"],
         "entity_uuid": entity_uuid.get(r["entity_id"]),
         "display_name": r["display_name"], "confirmed_at": r["confirmed_at"],
         "ignored": bool(r["ignored"]),
         "observation_count": r["observation_count"],
         "first_seen_at": r["first_seen_at"], "last_seen_at": r["last_seen_at"],
         "created_at": r["created_at"]}
        for r in q("SELECT * FROM entity_handles ORDER BY kind, value")]
    tables["observation_participants"] = [
        {"observation_uuid": obs_uuid[r["observation_id"]],
         "handle_kind": r["handle_kind"], "handle_value": r["handle_value"],
         "role": r["role"]}
        for r in q("SELECT p.* FROM observation_participants p "
                   "JOIN observations o ON o.id=p.observation_id "
                   "ORDER BY o.stream, o.external_id, p.handle_kind, "
                   "p.handle_value, p.role")]
    tables["entry_observations"] = [
        {"entry_uuid": entry_uuid[r["entry_id"]],
         "observation_uuid": obs_uuid[r["observation_id"]],
         "char_start": r["char_start"], "char_end": r["char_end"]}
        for r in q("SELECT eo.* FROM entry_observations eo "
                   "JOIN entries e ON e.id=eo.entry_id "
                   "JOIN observations o ON o.id=eo.observation_id "
                   "ORDER BY e.uuid, o.uuid")]

    for name, rows in tables.items():
        (out_dir / f"{name}.jsonl").write_text(_jl(rows), encoding="utf-8",
                                               newline="\n")
    manifest = {
        "format": "jsonl",
        "schema_version": SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "exported_at": now_utc(),
        "tables": {name: len(rows) for name, rows in sorted(tables.items())},
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    return manifest


# ---------------------------------------------------------------- markdown

def _wiki(name: str) -> str:
    return "[[" + name.replace("[", "(").replace("]", ")") + "]]"


def export_markdown(conn: sqlite3.Connection, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    q = conn.execute
    tod_rank = {t: i for i, t in enumerate(TIME_OF_DAY)}
    days = [dict(r) for r in q("SELECT * FROM days ORDER BY journal_date")]
    files = 0

    entity_days: dict[int, dict] = {}   # entity_id -> {entity, appearances}

    for day in days:
        jd = day["journal_date"]
        day_meas = [dict(r) for r in q(
            "SELECT metric, numeric_value, text_value, unit FROM measurements "
            "WHERE journal_date=? AND status='active' ORDER BY metric", (jd,))]
        entries = [dict(r) for r in q(
            "SELECT e.*, k.name AS kind FROM entries e "
            "JOIN entry_kinds k ON k.id=e.kind_id "
            "WHERE e.journal_date=? AND e.status='active'", (jd,))]
        if not entries and not day["summary"] and not day_meas:
            continue
        entries.sort(key=lambda e: (tod_rank.get(e["time_of_day"], 99), e["uuid"]))

        lines = ["---", f"date: {jd}"]
        for m in day_meas:
            val = m["numeric_value"] if m["numeric_value"] is not None else m["text_value"]
            lines.append(f"{m['metric']}: {val}" + (f" {m['unit']}" if m["unit"] else ""))
        lines += ["---", ""]
        if day["summary"]:
            lines += [day["summary"], ""]

        by_cat: dict[str, list[dict]] = {}
        for e in entries:
            cat = q("SELECT c.name FROM entry_categories ec "
                    "JOIN categories c ON c.id=ec.category_id "
                    "WHERE ec.entry_id=? AND ec.is_primary=1",
                    (e["id"],)).fetchone()
            by_cat.setdefault(cat["name"] if cat else "other", []).append(e)

        cat_order = [r["name"] for r in q("SELECT name FROM categories ORDER BY id")]
        for cat in sorted(by_cat, key=lambda c: (cat_order.index(c)
                                                 if c in cat_order else 99)):
            lines.append(f"## {cat}")
            lines.append("")
            for e in by_cat[cat]:
                closure = (f" — closed: {e['closure']} ({e['closed_at']})"
                           if e["closure"] else "")
                lines.append(f"### {e['title']}  `{e['kind']}`{closure}")
                if e["body"]:
                    lines.append(e["body"])
                for m in q("SELECT metric, numeric_value, text_value, unit "
                           "FROM measurements WHERE entry_id=? AND status='active' "
                           "ORDER BY metric", (e["id"],)):
                    val = (m["numeric_value"] if m["numeric_value"] is not None
                           else m["text_value"])
                    lines.append(f"- {m['metric']}: {val}"
                                 + (f" {m['unit']}" if m["unit"] else ""))
                ments = [dict(r) for r in q(
                    "SELECT et.id, et.name, et.slug, et.type, m.role FROM mentions m "
                    "JOIN entities et ON et.id=m.entity_id WHERE m.entry_id=? "
                    "ORDER BY et.name", (e["id"],))]
                if ments:
                    lines.append("- with: " + ", ".join(
                        _wiki(mm["name"]) for mm in ments))
                for mm in ments:
                    info = entity_days.setdefault(
                        mm["id"], {"name": mm["name"], "slug": mm["slug"],
                                   "type": mm["type"], "appearances": []})
                    info["appearances"].append((jd, e["title"]))
                for lk in q(
                        "SELECT l.type, e2.title, e2.journal_date FROM entry_links l "
                        "JOIN entries e2 ON e2.uuid=l.to_uuid WHERE l.from_uuid=? "
                        "ORDER BY l.to_uuid, l.type", (e["uuid"],)):
                    lines.append(f"- {lk['type']} -> {lk['title']} "
                                 f"({lk['journal_date']})")
                for lk in q(
                        "SELECT l.type, e2.title, e2.journal_date FROM entry_links l "
                        "JOIN entries e2 ON e2.uuid=l.from_uuid WHERE l.to_uuid=? "
                        "ORDER BY l.from_uuid, l.type", (e["uuid"],)):
                    lines.append(f"- {lk['type']} <- {lk['title']} "
                                 f"({lk['journal_date']})")
                lines.append("")

        year_dir = out_dir / jd[:4]
        year_dir.mkdir(parents=True, exist_ok=True)
        (year_dir / f"{jd}.md").write_text("\n".join(lines).rstrip() + "\n",
                                           encoding="utf-8", newline="\n")
        files += 1

    ent_dir = out_dir / "_entities"
    if entity_days:
        ent_dir.mkdir(parents=True, exist_ok=True)
    for info in sorted(entity_days.values(), key=lambda x: x["slug"]):
        aliases = [r["alias"] for r in q(
            "SELECT ea.alias FROM entity_aliases ea JOIN entities e "
            "ON e.id=ea.entity_id WHERE e.slug=? ORDER BY ea.alias",
            (info["slug"],))]
        lines = [f"# {info['name']}", "", f"type: {info['type']}"]
        if aliases:
            lines.append("aliases: " + ", ".join(aliases))
        lines.append("")
        for jd, title in sorted(set(info["appearances"])):
            lines.append(f"- {jd} — {title}")
        (ent_dir / f"{info['slug']}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        files += 1

    return {"format": "markdown", "files": files, "days": len(days),
            "entities": len(entity_days)}

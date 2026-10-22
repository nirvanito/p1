"""Export: the archive must outlive the code.

Three formats, all deterministic (stable ordering, no timestamps in content —
the jsonl manifest carries the only timestamp), so consecutive exports diff
cleanly:

- jsonl: one file per table, one JSON object per line, UUIDs and names in
  place of integer ids wherever a stable identity exists, plus manifest.json.
- markdown: YYYY/YYYY-MM-DD.md per day plus _entities/<slug>.md pages with
  [[Wiki Links]] — the artifact that has to make sense to a person in 2046
  with no tooling at all.
- obsidian: the markdown export grown into a vault a person lives in — day
  notes with the health line and the observed-tier digest, one note per
  entity, open loops, an index. Re-exportable into a vault that also holds
  the owner's own notes, because it only ever touches the files it generated.

All three are one-way dumps: no sync, nothing read back, no live updating.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from . import CONTRACT_VERSION, SCHEMA_VERSION
from .config import MENTION_ROLES, TIME_OF_DAY
from .domain.util import DomainError, now_utc
from .recall import HEALTH_METRICS, JOURNAL_METRICS, fmt_value
from .views import entry_provenance


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
    tables.update(_entity_link_tables(q, entity_uuid, source_uuid, run_uuid))
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


def _entity_link_tables(q, entity_uuid: dict, source_uuid: dict,
                        run_uuid: dict) -> dict[str, list[dict]]:
    """Relationships (v5), every status: a retracted link is history too.
    Ends export as entity uuids, the cited passage as its source uuid."""
    types = [
        {"name": r["name"], "symmetric": bool(r["symmetric"]),
         "inverse": r["inverse"], "description": r["description"],
         "created_at": r["created_at"]}
        for r in q("SELECT * FROM entity_link_types ORDER BY name")]
    links = [
        {"uuid": r["uuid"],
         "from_entity_uuid": entity_uuid[r["from_entity_id"]],
         "to_entity_uuid": entity_uuid[r["to_entity_id"]],
         "type": r["type"], "note": r["note"],
         "valid_from": r["valid_from"], "valid_to": r["valid_to"],
         "status": r["status"], "actor": r["actor"],
         "source_uuid": source_uuid.get(r["source_id"]),
         "char_start": r["char_start"], "char_end": r["char_end"],
         "entry_uuid": r["entry_uuid"],
         "extraction_run_uuid": run_uuid.get(r["extraction_run_id"]),
         "observation_uuid": r["observation_uuid"],
         "confirmed_at": r["confirmed_at"], "created_at": r["created_at"],
         "updated_at": r["updated_at"]}
        for r in q("SELECT l.* FROM entity_links l "
                   "JOIN entities f ON f.id=l.from_entity_id "
                   "JOIN entities t ON t.id=l.to_entity_id "
                   "ORDER BY f.uuid, t.uuid, l.type, l.uuid")]
    return {"entity_link_types": types, "entity_links": links}


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

    _add_related_entities(q, entity_days)
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
        relationships = _relationship_lines(q, info["slug"])
        if relationships:
            if lines[-1]:
                lines.append("")
            lines += relationships
        (ent_dir / f"{info['slug']}.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        files += 1

    return {"format": "markdown", "files": files, "days": len(days),
            "entities": len(entity_days)}


def _add_related_entities(q, entity_days: dict) -> None:
    """An entity known only through a relationship — Jake's hometown, never
    itself the subject of an entry — still gets a page, so the [[link]] on
    the other entity's page leads somewhere."""
    for r in q("SELECT DISTINCT e.id, e.name, e.slug, e.type FROM entity_links l "
               "JOIN entities e ON e.id IN (l.from_entity_id, l.to_entity_id) "
               "WHERE l.status='active' AND e.status='active'"):
        entity_days.setdefault(r["id"], {"name": r["name"], "slug": r["slug"],
                                         "type": r["type"], "appearances": []})


def _relationship_lines(q, slug: str) -> list[str]:
    """Every active relationship touching the entity, written the same way on
    both ends' pages — the stored direction, as a sentence."""
    rows = q("SELECT l.type, l.note, l.valid_from, l.valid_to, "
             "f.name AS from_name, t.name AS to_name FROM entity_links l "
             "JOIN entities f ON f.id=l.from_entity_id "
             "JOIN entities t ON t.id=l.to_entity_id "
             "JOIN entities me ON me.slug=? "
             "WHERE l.status='active' AND me.id IN (l.from_entity_id, l.to_entity_id) "
             "ORDER BY l.type, f.name, t.name, l.uuid", (slug,)).fetchall()
    if not rows:
        return []
    lines = ["## relationships", ""]
    for r in rows:
        when = ""
        if r["valid_from"] and r["valid_to"]:
            when = f" ({r['valid_from']} – {r['valid_to']})"
        elif r["valid_from"]:
            when = f" (since {r['valid_from']})"
        elif r["valid_to"]:
            when = f" (until {r['valid_to']})"
        lines.append(f"- {_wiki(r['from_name'])} {r['type']} {_wiki(r['to_name'])}"
                     + when + (f" — {r['note']}" if r["note"] else ""))
    return lines


# ---------------------------------------------------------------- obsidian
#
# Read-only by design. Nothing in the vault is ever read back into lifecrm.db:
# an edit made in Obsidian flowing back would be a second write path around
# the review gate, and the architecture has exactly one (lifecrm/domain/).
#
# So the exporter owns only what it generated. Every generated note starts
# with the front-matter line `lifecrm: generated`, and .lifecrm-manifest.json
# lists them; a re-export rewrites only those and prunes the ones that no
# longer apply. A file without the marker line is never written or deleted —
# that is how the owner's own notes in the same vault stay untouched, and
# deleting the line from a generated note is how the owner adopts one.
#
# The two tiers stay visibly apart: journal entries are the confirmed record;
# health lines and the observed digest are bulk imports and every item says
# so, citing its source file or its observations row.

VAULT_MANIFEST = ".lifecrm-manifest.json"
GENERATED_LINE = "lifecrm: generated"
NOTICE = ("%% Generated by `lifecrm export --format obsidian` — edits here are "
          "not read back, and the next export overwrites them. Delete the "
          "'lifecrm: generated' line to keep this note as your own. %%")
_GENERATED_RE = re.compile(r"""^lifecrm:\s*["']?generated["']?\s*$""")

TYPE_FOLDERS = {"person": "People", "place": "Places",
                "organization": "Organizations", "project": "Projects",
                "activity": "Activities", "food": "Food", "media": "Media",
                "topic": "Topics"}
_ROLE_LABELS = {"participant": "with", "location": "at", "item": "item",
                "subject": "about", "mention": "mentions"}

# Streams the day digest names item by item; every other stream is counted
# only. Tapbacks and thread events are not "contact" with anyone.
_MESSAGE_STREAMS = ("imessage", "sms", "rcs", "android_sms", "android_mms",
                    "whatsapp")
_CALL_STREAMS = ("ios_call", "android_call")
_CHAT_STREAMS = {"claude_chat": "Claude", "chatgpt_chat": "ChatGPT"}
_NOT_CONTACT = ("text_reaction", "text_event")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")
_RECENT_DAYS = 14
_EXCERPT_MAX = 600

# Windows refuses these characters and device names (with any extension:
# "con.md" is the console); Obsidian links break on # ^ [ ] |.
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*#^\[\]\x00-\x1f\x7f]')
_RESERVED_STEMS = re.compile(
    r"^(con|prn|aux|nul|conin\$|conout\$|com[0-9¹²³]|lpt[0-9¹²³])$",
    re.IGNORECASE)
_STEM_MAX = 80


# ---- names and links

def safe_stem(name: str) -> str:
    """A file name every target file system can hold and every wikilink can
    name: forbidden characters become '-', trailing dots and spaces go (Windows
    drops them silently), a leading dot would hide the file, device names get
    a '_', and the result is NFC so a Mac and a PC spell it identically."""
    s = _UNSAFE_CHARS.sub("-", unicodedata.normalize("NFC", name))
    s = " ".join(s.split()).strip(" .")
    if len(s) > _STEM_MAX:
        s = s[:_STEM_MAX].rstrip(" .")
    s = s or "untitled"
    head, dot, rest = s.partition(".")
    if _RESERVED_STEMS.match(head.rstrip()):
        s = head.rstrip() + "_" + dot + rest
    return s


def unique_stems(items: list[tuple], fixed: set[str]) -> dict:
    """{key: stem} for (key, name, type, uuid) items, unique across the whole
    vault ignoring case and never equal to a fixed stem (Index, Loops, the
    journal dates).

    Wikilinks resolve by file name alone, wherever the note lives, and
    Windows and macOS file systems ignore case — so 'Jake' the person and
    'jake' the topic cannot both be 'Jake.md'. Every member of a colliding
    group escalates together (name, then name + type, then + uuid prefix,
    then + whole uuid), so the outcome depends only on what collides, never
    on which entity was created first."""
    base = {key: safe_stem(name) for key, name, _, _ in items}
    level = {key: 0 for key, *_ in items}

    def stem(key, etype, uid):
        return (base[key], f"{base[key]} ({etype})",
                f"{base[key]} ({etype} {uid[:8]})",
                f"{base[key]} ({etype} {uid})")[level[key]]

    while True:
        current = {key: stem(key, etype, uid) for key, _, etype, uid in items}
        groups: dict[str, list] = {}
        for key, s in current.items():
            groups.setdefault(s.casefold(), []).append(key)
        bump = [key for folded, keys in groups.items()
                if len(keys) > 1 or folded in fixed
                for key in keys if level[key] < 3]
        if not bump:
            return current
        for key in bump:
            level[key] += 1


def _label(text: str) -> str:
    return " ".join(text.replace("|", "/").replace("[", "(")
                    .replace("]", ")").split())


def _wikilink(stem: str, label: str | None = None) -> str:
    label = _label(label) if label is not None else None
    return f"[[{stem}]]" if label in (None, stem) else f"[[{stem}|{label}]]"


def _tag(name: str) -> str | None:
    t = re.sub(r"[^\w/-]+", "-", name.strip().lower()).strip("-/")
    return t if t and not t.isdigit() else None


def _snip(text: str | None, n: int = 80) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def _num(value) -> str:
    v = float(value)
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(round(v, 3))


def _amount(value, unit: str) -> str:
    """7/10, 6.5 h, 10,432 steps — never 1.23457e+06."""
    if unit in ("steps", "ml", "kcal"):
        return fmt_value(value, unit)
    text = _num(value)
    return f"{text}{unit}" if unit.startswith("/") else f"{text} {unit}".strip()


def _yaml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, date):
        return value.isoformat()
    # a JSON string is a valid YAML double-quoted scalar
    return json.dumps(str(value), ensure_ascii=False)


def _front_matter(fields: list[tuple[str, object]]) -> list[str]:
    lines = ["---", GENERATED_LINE]
    for key, value in fields:
        if value is None or (isinstance(value, list) and not value):
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines += [f"  - {_yaml(v)}" for v in value]
        else:
            lines.append(f"{key}: {_yaml(value)}")
    return lines + ["---", NOTICE, ""]


def _local(started_at: str, offset: int | None, tz: ZoneInfo) -> datetime | None:
    try:
        at = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (at + timedelta(seconds=offset)).replace(tzinfo=None) \
        if offset is not None else at.astimezone(tz).replace(tzinfo=None)


def _hm(row, tz: ZoneInfo) -> str:
    at = _local(row["started_at"], row["utc_offset_sec"], tz)
    return at.strftime("%H:%M") if at else row["started_at"][11:16]


def _callout(head: str, text: str) -> list[str]:
    body = text.strip()
    if len(body) > _EXCERPT_MAX:
        body = body[:_EXCERPT_MAX - 1].rstrip() + "…"
    return [f"> [!quote] {head}"] + [f"> {ln}".rstrip() for ln in body.splitlines()]


def _render(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"


# ---- the notes

def _vault_days(conn: sqlite3.Connection) -> list[str]:
    """Every day with something active on it, in either tier."""
    return [r[0] for r in conn.execute(
        "SELECT journal_date FROM days "
        "WHERE summary IS NOT NULL AND TRIM(summary) <> '' "
        "UNION SELECT journal_date FROM entries WHERE status='active' "
        "UNION SELECT journal_date FROM measurements "
        "      WHERE journal_date IS NOT NULL AND status='active' "
        "UNION SELECT journal_date FROM health_records WHERE status='active' "
        "UNION SELECT journal_date FROM observations WHERE status='active' "
        "ORDER BY 1")]


def _entity_link(ctx: dict, entity_id: int, fallback: str = "") -> str:
    ent = ctx["ents"].get(entity_id)
    if ent is None:          # merged or missing: name it, link nothing
        return fallback
    return _wikilink(ctx["stems"][entity_id], ent["name"])


def _day_measurements(conn, jd: str) -> tuple[dict, list[str]]:
    props: dict[str, float] = {}
    parts = []
    for m in conn.execute(
            "SELECT metric, numeric_value, text_value, unit FROM measurements "
            "WHERE journal_date=? AND status='active' ORDER BY metric, uuid",
            (jd,)):
        if m["numeric_value"] is None:
            parts.append(f"{m['metric']} {m['text_value']}")
            continue
        unit = m["unit"] or JOURNAL_METRICS.get(m["metric"], {}).get("unit", "")
        parts.append(f"{m['metric']} {_amount(m['numeric_value'], unit)}")
        key = {"sleep_hours": "sleep"}.get(m["metric"], m["metric"])
        if key in ("mood", "energy", "sleep") and key not in props:
            props[key] = m["numeric_value"]
    return props, parts


def _health_line(conn, jd: str) -> str | None:
    """Sleep, steps, workouts and weight from the sensor tier, citing the
    export file(s) — the provenance anchor of a health record."""
    rows = {r["metric"]: r for r in conn.execute(
        "SELECT metric, SUM(value) AS total, AVG(value) AS mean, COUNT(*) AS n "
        "FROM health_records WHERE journal_date=? AND status='active' "
        "GROUP BY metric ORDER BY metric", (jd,))}
    if not rows:
        return None
    parts = []
    if "sleep_session" in rows and rows["sleep_session"]["total"] is not None:
        hours = rows["sleep_session"]["total"] * HEALTH_METRICS["sleep_session"]["factor"]
        parts.append(f"sleep {_amount(round(hours, 1), 'h')}")
    if "steps" in rows and rows["steps"]["total"] is not None:
        parts.append(_amount(round(rows["steps"]["total"]), "steps"))
    if "exercise_session" in rows:
        w = rows["exercise_session"]
        mins = f" ({_amount(round(w['total']), 'min')})" if w["total"] else ""
        parts.append(f"{w['n']} workout{'s' if w['n'] != 1 else ''}{mins}")
    if "weight" in rows and rows["weight"]["mean"] is not None:
        parts.append(f"weight {_amount(round(rows['weight']['mean'], 2), 'kg')}")
    others = [m for m in rows if m not in
              ("sleep_session", "steps", "exercise_session", "weight")]
    if others:
        parts.append("also " + ", ".join(others))
    if not parts:
        parts.append(f"{sum(r['n'] for r in rows.values())} record(s)")
    files = [r[0] for r in conn.execute(
        "SELECT DISTINCT source_file FROM health_records "
        "WHERE journal_date=? AND status='active' ORDER BY source_file", (jd,))]
    shown = ", ".join(files[:3]) + (f" +{len(files) - 3}" if len(files) > 3 else "")
    return f"**Health** · {' · '.join(parts)} — observed, from {shown}"


def _mentions(ctx: dict, entry_id: int) -> str:
    """'with [[Jake]] · at [[Movement]] · about [[Bouldering]]' — the role
    decides the word, so a place is never something you were 'with'."""
    by_role: dict[str, list[str]] = {}
    for m in ctx["conn"].execute(
            "SELECT m.entity_id, m.role, et.name FROM mentions m "
            "JOIN entities et ON et.id=m.entity_id WHERE m.entry_id=? "
            "ORDER BY et.name, et.id", (entry_id,)):
        by_role.setdefault(m["role"], []).append(
            _entity_link(ctx, m["entity_id"], m["name"]))
    return " · ".join(f"{_ROLE_LABELS.get(role, role)} " + ", ".join(by_role[role])
                      for role in MENTION_ROLES if role in by_role)


def _entry_block(ctx: dict, e: dict) -> list[str]:
    conn = ctx["conn"]
    q = conn.execute
    cats = [r["name"] for r in q(
        "SELECT c.name FROM entry_categories ec "
        "JOIN categories c ON c.id=ec.category_id "
        "WHERE ec.entry_id=? AND ec.is_primary=0 ORDER BY c.id", (e["id"],))]
    meta = [f"`{e['kind']}`"]
    if e["time_of_day"]:
        meta.append(e["time_of_day"])
    if e["occurred_at"]:
        meta.append(f"at {e['occurred_at']}"
                    + (f" – {e['ended_at']}" if e["ended_at"] else ""))
    if e["due_at"]:
        meta.append(f"due {e['due_at']}")
    if cats:
        meta.append("also " + ", ".join(cats))
    if e["confidence"] != "high":
        meta.append(f"{e['confidence']} confidence")
    if e["closure"]:
        meta.append(f"closed: {e['closure']} {(e['closed_at'] or '')[:10]}".rstrip())
    elif e["kind"] in ("task", "plan"):
        meta.append("open loop")
    lines = [f"### {e['title']}", " · ".join(meta) + f" %%entry {e['uuid']}%%"]
    mentioned = _mentions(ctx, e["id"])
    if mentioned:
        lines.append(mentioned)
    lines.append("")
    if e["body"]:
        lines += [e["body"].strip(), ""]

    items = []
    for m in q("SELECT metric, numeric_value, text_value, unit FROM measurements "
               "WHERE entry_id=? AND status='active' ORDER BY metric, uuid",
               (e["id"],)):
        val = _num(m["numeric_value"]) if m["numeric_value"] is not None \
            else m["text_value"]
        items.append(f"- {m['metric']}: {val}" + (f" {m['unit']}" if m["unit"] else ""))
    for arrow, mine, other in (("→", "from_uuid", "to_uuid"),
                               ("←", "to_uuid", "from_uuid")):
        for lk in q(f"SELECT l.type, e2.title, e2.journal_date FROM entry_links l "
                    f"JOIN entries e2 ON e2.uuid=l.{other} "
                    f"WHERE l.{mine}=? AND e2.status='active' "
                    f"ORDER BY e2.journal_date, e2.title, l.type", (e["uuid"],)):
            items.append(f"- {lk['type']} {arrow} {_wikilink(lk['journal_date'])} "
                         f"· {lk['title']}")
    if items:
        lines += items + [""]

    for p in sorted(entry_provenance(conn, e["id"]), key=lambda p: p["source_id"]):
        if p.get("transcript_excerpt"):
            lines += _callout(f"Source {p['source_id']} · {p['source_type']} · "
                              f"{p['source_date']}", p["transcript_excerpt"]) + [""]
    for o in q("SELECT eo.char_start, eo.char_end, o.uuid, o.stream, o.status, "
               "o.started_at, o.utc_offset_sec, o.title, o.body "
               "FROM entry_observations eo "
               "JOIN observations o ON o.id=eo.observation_id "
               "WHERE eo.entry_id=? ORDER BY o.started_at, o.uuid", (e["id"],)):
        text = o["body"] or o["title"] or ""
        if o["char_start"] is not None:
            text = text[o["char_start"]:o["char_end"]]
        state = "" if o["status"] == "active" else f" ({o['status']})"
        lines += _callout(f"Observed · {o['stream']}{state} · `{o['uuid']}`",
                          text or "(no text)") + [""]
    return lines


def _observed_digest(ctx: dict, jd: str) -> list[str]:
    """Counts per stream, then up to `notable` items — each labelled as
    observed and citing its row, because none of it was ever reviewed."""
    conn, tz = ctx["conn"], ctx["tz"]
    rows = conn.execute(
        "SELECT id, uuid, stream, started_at, utc_offset_sec, direction, title, "
        "body FROM observations WHERE journal_date=? AND status='active' "
        "ORDER BY started_at, uuid", (jd,)).fetchall()
    if not rows:
        return []
    who: dict[int, list[int]] = {}
    for r in conn.execute(
            "SELECT DISTINCT p.observation_id AS oid, h.entity_id AS eid "
            "FROM observations o "
            "JOIN observation_participants p ON p.observation_id=o.id "
            "JOIN entity_handles h ON h.kind=p.handle_kind AND h.value=p.handle_value "
            "JOIN entities et ON et.id=h.entity_id AND et.status='active' "
            "WHERE o.journal_date=? AND o.status='active' "
            "ORDER BY p.observation_id, h.entity_id", (jd,)):
        if r["eid"] not in ctx["owners"]:
            who.setdefault(r["oid"], []).append(r["eid"])

    def cite(row) -> str:
        return f" — observed `{row['uuid']}`"

    def what(row) -> str:
        return _snip(row["title"] or row["body"], 100) or row["stream"]

    def name_key(eid):
        return (ctx["ents"][eid]["name"].casefold(), eid)

    counts: dict[str, int] = {}
    items: list[tuple[tuple, str]] = []
    texts: dict[int, list] = {}
    calls: dict[int, list] = {}
    chats: dict[tuple, list] = {}
    for r in rows:
        counts[r["stream"]] = counts.get(r["stream"], 0) + 1
        people = who.get(r["id"], [])
        if r["stream"] == "calendar_event":
            with_ = (" · with " + ", ".join(_entity_link(ctx, eid)
                                            for eid in sorted(people, key=name_key))
                     if people else "")
            items.append(((0, r["started_at"], r["uuid"]),
                          f"- {_hm(r, tz)} {what(r)}{with_}{cite(r)}"))
        elif r["stream"] == "place_visit":
            items.append(((0, r["started_at"], r["uuid"]),
                          f"- {_hm(r, tz)} at {what(r)}{cite(r)}"))
        elif r["stream"] == "note":
            items.append(((3, r["started_at"], r["uuid"]),
                          f"- note: {what(r)}{cite(r)}"))
        elif r["stream"] in _CHAT_STREAMS:
            chats.setdefault((r["stream"], r["title"] or "(untitled)"), []).append(r)
        elif r["stream"] in _MESSAGE_STREAMS:
            for eid in people:
                texts.setdefault(eid, []).append(r)
        elif r["stream"] in _CALL_STREAMS:
            for eid in people:
                calls.setdefault(eid, []).append(r)

    for eid in sorted(texts, key=name_key):
        msgs = texts[eid]
        last = msgs[-1]
        streams = ", ".join(sorted({m["stream"] for m in msgs}))
        said = _snip(last["body"] or last["title"], 80) or "[attachment]"
        items.append(((1, *name_key(eid)),
                      f"- {len(msgs)} text{'s' if len(msgs) != 1 else ''} with "
                      f"{_entity_link(ctx, eid)} ({streams}) · latest "
                      f"{_hm(last, tz)}: “{said}”{cite(last)}"))
    for eid in sorted(calls, key=name_key):
        rs = calls[eid]
        items.append(((2, *name_key(eid)),
                      f"- {len(rs)} call{'s' if len(rs) != 1 else ''} with "
                      f"{_entity_link(ctx, eid)} · latest {_hm(rs[-1], tz)}"
                      f"{cite(rs[-1])}"))
    for (stream, title), msgs in chats.items():
        items.append(((4, msgs[0]["started_at"], stream, title),
                      f"- {_CHAT_STREAMS[stream]} chat: {_snip(title, 100)} "
                      f"({len(msgs)} message{'s' if len(msgs) != 1 else ''})"
                      f"{cite(msgs[0])}"))

    items.sort(key=lambda kv: kv[0])
    lines = ["## Observed", "",
             "_Bulk-imported, never reviewed — each item cites its "
             "`observations` row._", "",
             " · ".join(f"{s} {n:,}" for s, n in sorted(counts.items())), ""]
    lines += [text for _, text in items[:ctx["notable"]]]
    if len(items) > ctx["notable"]:
        lines.append(f"- … and {len(items) - ctx['notable']} more")
    return lines


def _day_note(ctx: dict, jd: str, prev: str | None, nxt: str | None) -> str:
    conn = ctx["conn"]
    q = conn.execute
    tod_rank = {t: i for i, t in enumerate(TIME_OF_DAY)}
    day = q("SELECT summary FROM days WHERE journal_date=?", (jd,)).fetchone()
    summary = ((day["summary"] if day else None) or "").strip()
    props, measured = _day_measurements(conn, jd)
    entries = [dict(r) for r in q(
        "SELECT e.*, k.name AS kind FROM entries e "
        "JOIN entry_kinds k ON k.id=e.kind_id "
        "WHERE e.journal_date=? AND e.status='active'", (jd,))]
    # narrative order within a time of day: the order the draft listed them
    entries.sort(key=lambda e: (tod_rank.get(e["time_of_day"], 99), e["id"]))

    digest = _observed_digest(ctx, jd)

    primary: dict[int, str] = {}
    # 'journal' marks a day the owner actually logged; a day known only from
    # imports is 'observed', so a tag search never mistakes one for the other.
    tags = {"journal"} if (entries or summary or measured) else set()
    if digest:
        tags.add("observed")
    for r in q("SELECT ec.entry_id, ec.is_primary, c.name FROM entry_categories ec "
               "JOIN entries e ON e.id=ec.entry_id "
               "JOIN categories c ON c.id=ec.category_id "
               "WHERE e.journal_date=? AND e.status='active'", (jd,)):
        if r["is_primary"]:
            primary[r["entry_id"]] = r["name"]
        if _tag(r["name"]):
            tags.add(_tag(r["name"]))

    d = date.fromisoformat(jd)
    lines = _front_matter([("date", d), ("mood", props.get("mood")),
                           ("energy", props.get("energy")),
                           ("sleep", props.get("sleep")),
                           ("tags", sorted(tags))])
    lines += [f"# {jd} · {_WEEKDAYS[d.weekday()]}", ""]
    nav = [f"← {_wikilink(prev)}" if prev else None, _wikilink("Index"),
           f"{_wikilink(nxt)} →" if nxt else None]
    lines += [" · ".join(n for n in nav if n), ""]
    if summary:
        lines += [summary, ""]
    if measured:
        lines += ["**Journal** · " + " · ".join(measured), ""]
    health = _health_line(conn, jd)
    if health:
        lines += [health, ""]

    by_cat: dict[str, list[dict]] = {}
    for e in entries:
        by_cat.setdefault(primary.get(e["id"], "other"), []).append(e)
    cat_order = [r["name"] for r in q("SELECT name FROM categories ORDER BY id")]
    for cat in sorted(by_cat, key=lambda c: (cat_order.index(c)
                                             if c in cat_order else 99, c)):
        lines += [f"## {cat}", ""]
        for e in by_cat[cat]:
            lines += _entry_block(ctx, e)
    return _render(lines + digest)


def _entity_facts(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per entity: aliases, journal days, observed days, handles, and the
    latest observed contact — gathered in a few passes, not per entity."""
    facts: dict[int, dict] = {}

    def f(eid):
        return facts.setdefault(eid, {"aliases": [], "journal": {}, "observed": {},
                                      "handles": [], "latest_obs": None})

    for r in conn.execute("SELECT entity_id, alias FROM entity_aliases "
                          "ORDER BY entity_id, alias"):
        f(r["entity_id"])["aliases"].append(r["alias"])
    for r in conn.execute(
            "SELECT m.entity_id, m.role, e.id, e.journal_date, e.title "
            "FROM mentions m JOIN entries e ON e.id=m.entry_id "
            "WHERE e.status='active' ORDER BY m.entity_id, e.journal_date, e.id, m.role"):
        f(r["entity_id"])["journal"].setdefault(r["journal_date"], []).append(
            (r["title"], r["role"]))
    for r in conn.execute("SELECT * FROM entity_handles WHERE entity_id IS NOT NULL "
                          "ORDER BY entity_id, kind, value"):
        f(r["entity_id"])["handles"].append(dict(r))
    linked = ("FROM entity_handles h "
              "JOIN observation_participants p "
              "  ON h.kind=p.handle_kind AND h.value=p.handle_value "
              "JOIN observations o ON o.id=p.observation_id "
              "WHERE h.entity_id IS NOT NULL AND o.status='active' ")
    for r in conn.execute(
            "SELECT h.entity_id, o.journal_date, o.stream, "
            "COUNT(DISTINCT o.id) AS n " + linked +
            "GROUP BY h.entity_id, o.journal_date, o.stream "
            "ORDER BY h.entity_id, o.journal_date, o.stream"):
        day = f(r["entity_id"])["observed"].setdefault(r["journal_date"], {})
        day[r["stream"]] = r["n"]
    marks = ",".join("?" for _ in _NOT_CONTACT)
    for r in conn.execute(
            "SELECT * FROM (SELECT h.entity_id AS eid, o.uuid, o.stream, "
            "o.started_at, o.utc_offset_sec, o.journal_date, o.title, o.body, "
            "ROW_NUMBER() OVER (PARTITION BY h.entity_id "
            "  ORDER BY o.started_at DESC, o.uuid DESC) AS rn " + linked +
            f"AND o.stream NOT IN ({marks})) WHERE rn=1", _NOT_CONTACT):
        f(r["eid"])["latest_obs"] = dict(r)
    return facts


_EMPTY_FACTS = {"aliases": [], "journal": {}, "observed": {}, "handles": [],
                "latest_obs": None}


def _seen_span(facts: dict) -> tuple[str | None, str | None]:
    days = sorted(set(facts["journal"]) | set(facts["observed"]))
    return (days[0], days[-1]) if days else (None, None)


def _entity_note(ctx: dict, ent: dict, facts: dict) -> str:
    tz = ctx["tz"]
    stem = ctx["stems"][ent["id"]]
    aliases = sorted(set(facts["aliases"]), key=lambda a: (a.casefold(), a))
    if stem != ent["name"]:
        aliases = [ent["name"]] + aliases   # so search and link suggestions find it
    first, last = _seen_span(facts)
    lines = _front_matter([
        ("type", ent["type"]), ("uuid", ent["uuid"]), ("aliases", aliases),
        ("first_seen", date.fromisoformat(first) if first else None),
        ("last_seen", date.fromisoformat(last) if last else None),
        ("tags", [t for t in [_tag(ent["type"])] if t])])
    lines += [f"# {ent['name']}", ""]
    head = [f"**{ent['type']}**"]
    if first:
        head += [f"first seen {_wikilink(first)}", f"last seen {_wikilink(last)}"]
    lines += [" · ".join(head), ""]
    others = [a for a in facts["aliases"] if a != ent["name"]]
    if others:
        lines += ["also known as: " + ", ".join(sorted(set(others),
                                                       key=lambda a: (a.casefold(), a))), ""]
    if ent["notes"]:
        lines += [ent["notes"].strip(), ""]

    latest = []
    if facts["journal"]:
        jd = max(facts["journal"])
        titles = "; ".join(t for t, _ in facts["journal"][jd])
        latest.append(((jd, 0), f"- journal · {_wikilink(jd)} — {titles}"))
    obs = facts["latest_obs"]
    if obs:
        at = _local(obs["started_at"], obs["utc_offset_sec"], tz)
        when = at.strftime("%Y-%m-%d %H:%M") if at else obs["started_at"]
        said = _snip(obs["body"] or obs["title"], 80)
        latest.append(((obs["journal_date"], 1),
                       f"- observed · {when} · {obs['stream']}"
                       + (f" — “{said}”" if said else "")
                       + f" · {_wikilink(obs['journal_date'])} · `{obs['uuid']}`"))
    if latest:
        lines += ["## Latest contact" if ent["type"] == "person" else "## Latest", ""]
        lines += [text for _, text in sorted(latest, key=lambda kv: kv[0],
                                             reverse=True)] + [""]

    if facts["handles"]:
        lines += ["## Handles", ""]
        for h in facts["handles"]:
            bits = [f"- {h['kind']} `{h['value']}`"]
            if h["display_name"]:
                bits.append(f"the source calls it “{h['display_name']}”")
            bits.append(f"{h['observation_count']:,} observation"
                        f"{'s' if h['observation_count'] != 1 else ''}")
            if h["observation_count"]:
                bits.append(f"{h['first_seen_at'][:10]} … {h['last_seen_at'][:10]}")
            if h["confirmed_at"]:
                bits.append(f"linked {h['confirmed_at'][:10]}")
            lines.append(" · ".join(bits))
        lines.append("")

    if facts["journal"]:
        lines += ["## Journal", ""]
        for jd in sorted(facts["journal"], reverse=True):
            lines.append(f"- {_wikilink(jd)} — " + "; ".join(
                f"{t} ({role})" for t, role in facts["journal"][jd]))
        lines.append("")
    if facts["observed"]:
        lines += ["## Observed", "",
                  "_Bulk-imported through the handles above, never reviewed._", ""]
        for jd in sorted(facts["observed"], reverse=True):
            lines.append(f"- {_wikilink(jd)} — " + " · ".join(
                f"{s} {n:,}" for s, n in sorted(facts["observed"][jd].items())))
    return _render(lines)


def _open_loops(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT e.id, e.uuid, e.title, e.journal_date, e.due_at, k.name AS kind "
        "FROM entries e JOIN entry_kinds k ON k.id=e.kind_id "
        "WHERE e.status='active' AND e.closed_at IS NULL "
        "AND k.name IN ('task','plan') "
        "ORDER BY (e.due_at IS NULL), e.due_at, e.journal_date, e.id")]


def _loops_note(ctx: dict, loops: list[dict]) -> str:
    lines = _front_matter([("tags", ["loops"])])
    lines += ["# Open loops", ""]
    if not loops:
        return _render(lines + ["No open tasks or plans."])
    lines += [f"{len(loops)} open task(s) and plan(s), soonest due first. Close "
              f"one with `lifecrm entry close <uuid> --closure done` — editing "
              f"this note changes nothing.", ""]
    due = [lp for lp in loops if lp["due_at"]]
    undated = [lp for lp in loops if not lp["due_at"]]
    for heading, group in (("## Due", due), ("## No due date", undated)):
        if not group:
            continue
        lines += [heading, ""]
        for lp in group:
            mentioned = _mentions(ctx, lp["id"])
            lead = f"**{lp['due_at']}** · " if lp["due_at"] else ""
            since = "from" if lp["due_at"] else "since"
            lines.append(f"- {lead}{lp['title']} `{lp['kind']}` — {since} "
                         f"{_wikilink(lp['journal_date'])}"
                         + (f" · {mentioned}" if mentioned else "")
                         + f" · `{lp['uuid']}`")
        lines.append("")
    return _render(lines)


def _index_note(ctx: dict, days: list[str], facts: dict[int, dict],
                loops: list[dict]) -> str:
    conn = ctx["conn"]
    lines = _front_matter([("tags", ["lifecrm"])])
    lines += ["# Life CRM", "",
              "A read-only mirror of lifecrm.db. Journal entries are the "
              "confirmed record; anything marked *observed* was bulk-imported "
              "and never reviewed.", ""]
    span = f" ({days[0]} … {days[-1]})" if days else ""
    lines += [f"{len(days)} day(s){span} · {_wikilink('Loops')}: "
              f"{len(loops)} open", ""]
    if days:
        lines += ["## Recent days", ""]
        for jd in reversed(days[-_RECENT_DAYS:]):
            row = conn.execute("SELECT summary FROM days WHERE journal_date=?",
                               (jd,)).fetchone()
            n = conn.execute("SELECT COUNT(*) FROM entries WHERE journal_date=? "
                             "AND status='active'", (jd,)).fetchone()[0]
            m = conn.execute("SELECT COUNT(*) FROM observations WHERE "
                             "journal_date=? AND status='active'", (jd,)).fetchone()[0]
            if row and row["summary"]:
                blurb = _snip(row["summary"], 100)
            else:
                blurb = " · ".join(
                    ([f"{n} entr{'y' if n == 1 else 'ies'}"] if n else [])
                    + ([f"{m:,} observed"] if m else []))
            weekday = _WEEKDAYS[date.fromisoformat(jd).weekday()]
            lines.append(f"- {_wikilink(jd)} · {weekday}"
                         + (f" — {blurb}" if blurb else ""))
        lines.append("")
    by_type: dict[str, list[dict]] = {}
    for ent in ctx["ents"].values():
        by_type.setdefault(ent["type"], []).append(ent)
    for etype, folder in TYPE_FOLDERS.items():
        group = sorted(by_type.get(etype, []),
                       key=lambda e: (e["name"].casefold(), e["uuid"]))
        if not group:
            continue
        lines += [f"## {folder}", ""]
        for ent in group:
            _, last = _seen_span(facts.get(ent["id"], _EMPTY_FACTS))
            lines.append(f"- {_entity_link(ctx, ent['id'])}"
                         + (f" — last seen {_wikilink(last)}" if last else ""))
        lines.append("")
    tiers = conn.execute(
        "SELECT 'observed' AS tier, stream AS name, COUNT(*) AS n, "
        "MIN(journal_date) AS first, MAX(journal_date) AS last FROM observations "
        "WHERE status='active' GROUP BY stream "
        "UNION ALL SELECT 'health', metric, COUNT(*), MIN(journal_date), "
        "MAX(journal_date) FROM health_records WHERE status='active' "
        "GROUP BY metric ORDER BY 1 DESC, 2").fetchall()
    if tiers:
        lines += ["## Imported", "", "_Bulk-imported, never reviewed._", ""]
        lines += [f"- {r['tier']} · {r['name']}: {r['n']:,} row(s), "
                  f"{r['first']} … {r['last']}" for r in tiers]
    return _render(lines)


def build_vault(conn: sqlite3.Connection, *, tz_name: str = "UTC",
                notable: int = 10) -> dict[str, str]:
    """Every generated note as {vault-relative posix path: text}. Reads only:
    the same database (and timezone) renders byte-identical notes."""
    days = _vault_days(conn)
    ents = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, uuid, type, name, notes FROM entities "
        "WHERE status='active' ORDER BY id")}
    stems = unique_stems([(e["id"], e["name"], e["type"], e["uuid"])
                          for e in ents.values()],
                         fixed={"index", "loops", *days})
    # The owner is a participant in every message they send or receive; once
    # their own number is linked to them, "texts with <owner>" would head
    # every day. A handle that is some import's account is the owner's.
    owners = {r[0] for r in conn.execute(
        "SELECT DISTINCT h.entity_id FROM entity_handles h "
        "JOIN imports i ON h.value = i.account WHERE h.entity_id IS NOT NULL")}
    ctx = {"conn": conn, "tz": ZoneInfo(tz_name), "notable": notable,
           "ents": ents, "stems": stems, "owners": owners}
    notes: dict[str, str] = {}
    for i, jd in enumerate(days):
        notes[f"Journal/{jd[:4]}/{jd}.md"] = _day_note(
            ctx, jd, days[i - 1] if i else None,
            days[i + 1] if i + 1 < len(days) else None)
    facts = _entity_facts(conn)
    for eid, ent in ents.items():
        notes[f"{TYPE_FOLDERS.get(ent['type'], 'Topics')}/{stems[eid]}.md"] = \
            _entity_note(ctx, ent, facts.get(eid, _EMPTY_FACTS))
    loops = _open_loops(conn)
    notes["Loops.md"] = _loops_note(ctx, loops)
    notes["Index.md"] = _index_note(ctx, days, facts, loops)
    return dict(sorted(notes.items()))


# ---- the vault on disk

def is_generated(path: Path) -> bool:
    """True when the file's front matter carries `lifecrm: generated` — the
    only files a re-export may overwrite or delete."""
    try:
        with path.open("rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    lines = head.decode("utf-8", "replace").lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if _GENERATED_RE.match(line.strip()):
            return True
    return False


def _manifest_path_ok(rel: str) -> bool:
    """A manifest entry this exporter could have written: relative, inside the
    vault, a visible .md file. Anything else is never deleted, whatever the
    manifest says."""
    if not isinstance(rel, str) or "\\" in rel or ":" in rel:
        return False
    p = PurePosixPath(rel)
    return (not p.is_absolute() and p.suffix == ".md" and bool(p.parts)
            and all(part not in ("", ".", "..") and not part.startswith(".")
                    for part in p.parts))


def _read_manifest(out_dir: Path) -> set[str]:
    path = out_dir / VAULT_MANIFEST
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise DomainError(f"{path} is not readable as a lifecrm manifest ({e}) — "
                          f"move it aside and re-run")
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        raise DomainError(f"{path} has no 'files' list, so it is not a lifecrm "
                          f"manifest — move it aside and re-run")
    return {f for f in files if _manifest_path_ok(f)}


def _prune_empty_dirs(out_dir: Path, start: Path) -> None:
    root = out_dir.resolve()
    d = start
    while d.resolve() != root and root in d.resolve().parents:
        try:
            d.rmdir()            # only ever succeeds on an empty folder
        except OSError:
            return
        d = d.parent


def export_obsidian(conn: sqlite3.Connection, out_dir: Path, *,
                    tz_name: str = "UTC", dry_run: bool = False,
                    notable: int = 10) -> dict:
    """Render the vault and bring `out_dir` in line with it, touching only
    generated files. With dry_run nothing is written; the result lists what
    would change either way."""
    if out_dir.exists() and not out_dir.is_dir():
        raise DomainError(f"{out_dir} is a file — --out must be a vault folder")
    notes = build_vault(conn, tz_name=tz_name, notable=notable)
    previous = _read_manifest(out_dir)

    created, updated, kept, deleted = [], [], [], []
    unchanged = 0
    writes: dict[str, bytes] = {}
    try:
        for rel, text in notes.items():
            path = out_dir / rel
            data = text.encode("utf-8")
            if not path.exists():
                created.append(rel)
                writes[rel] = data
            elif not path.is_file() or not is_generated(path):
                kept.append({"path": rel,
                             "reason": "exists and is not a generated note "
                                       "(no 'lifecrm: generated' line)"})
            elif path.read_bytes() == data:
                unchanged += 1
            else:
                updated.append(rel)
                writes[rel] = data
        for rel in sorted(previous - set(notes)):
            path = out_dir / rel
            if not path.is_file():
                continue
            if not is_generated(path):
                kept.append({"path": rel,
                             "reason": "no longer generated, and its 'lifecrm: "
                                       "generated' line is gone — left as yours"})
                continue
            deleted.append(rel)
    except OSError as e:
        raise DomainError(f"cannot read {e.filename or out_dir} "
                          f"({e.strerror or e}) — check the vault folder's "
                          f"permissions and re-run")
    not_ours = {k["path"] for k in kept}
    ours = [rel for rel in notes if rel not in not_ours]
    # A case-only rename ('jake' -> 'Jake') is one file on Windows and macOS:
    # the stale name is deleted first, so the new one must then be written.
    folded = {rel.casefold() for rel in deleted}
    for rel in ours:
        if rel.casefold() in folded and rel not in writes:
            writes[rel] = notes[rel].encode("utf-8")

    manifest = json.dumps(
        {"format": "obsidian", "schema_version": SCHEMA_VERSION,
         "about": "Notes generated by lifecrm export --format obsidian. Edits "
                  "to them are not read back; a re-export rewrites and prunes "
                  "only the files listed here.",
         "files": sorted(ours)},
        indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)
        current = None
        try:
            for rel in deleted:
                current = rel
                (out_dir / rel).unlink()
                _prune_empty_dirs(out_dir, (out_dir / rel).parent)
            for rel, data in sorted(writes.items()):
                current = rel
                (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                (out_dir / rel).write_bytes(data)
            current = VAULT_MANIFEST
            mpath = out_dir / VAULT_MANIFEST
            mdata = manifest.encode("utf-8")
            if not mpath.is_file() or mpath.read_bytes() != mdata:
                mpath.write_bytes(mdata)
        except OSError as e:
            raise DomainError(f"cannot write {current} in {out_dir} "
                              f"({e.strerror or e}) — files before it were "
                              f"updated; re-run when it is free")

    return {"format": "obsidian", "dry_run": dry_run, "files": len(ours),
            "days": sum(1 for rel in notes if rel.startswith("Journal/")),
            "entities": sum(1 for rel in notes
                            if rel.split("/")[0] in TYPE_FOLDERS.values()),
            "created": created, "updated": updated, "deleted": deleted,
            "unchanged": unchanged, "kept": kept}


def vault_report_lines(result: dict, out_dir: Path) -> list[str]:
    lines = [f"obsidian vault {out_dir}: {result['files']} generated note(s) "
             f"({result['days']} day(s), {result['entities']} entities) — "
             f"{len(result['created'])} new, {len(result['updated'])} changed, "
             f"{result['unchanged']} unchanged, {len(result['deleted'])} removed"]
    if result["dry_run"]:
        lines += [f"  + {rel}" for rel in result["created"]]
        lines += [f"  ~ {rel}" for rel in result["updated"]]
    lines += [f"  - {rel}" for rel in result["deleted"]]
    lines += [f"  kept {k['path']} — {k['reason']}" for k in result["kept"]]
    if result["dry_run"]:
        lines.append("dry run — nothing written. Re-run without --dry-run to write.")
    return lines

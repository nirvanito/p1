"""lifecrm doctor: prove the database is intact.

A system this careful about provenance needs a way to verify its own
invariants — especially the ones the schema cannot enforce (exactly-one
primary category, the closed_at/closure pairing) where a domain-layer bug
would otherwise be silent.

Failures mean corruption or an invariant violation (non-zero exit).
Warnings mean operational drift (old backups, stuck drafts, overdue loops).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from . import db as db_mod
from .config import Config

BACKUP_MAX_AGE_DAYS = 7
STUCK_DRAFT_DAYS = 2
HEALTH_STALE_DAYS = 14


def run_checks(conn: sqlite3.Connection, cfg: Config | None = None) -> dict:
    checks: list[dict] = []

    def _add(status: str, name: str, detail: str = "") -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    def _rows(name: str, query: str, describe: str, limit: int = 5) -> None:
        rows = conn.execute(query).fetchall()
        if rows:
            sample = "; ".join(str(tuple(r)) for r in rows[:limit])
            _add("fail", name, f"{len(rows)} {describe}: {sample}")
        else:
            _add("ok", name)

    # --- storage-level integrity ---
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    _add("ok" if result == "ok" else "fail", "integrity_check",
         "" if result == "ok" else result)
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    _add("ok" if not fk else "fail", "foreign_key_check",
         "" if not fk else f"{len(fk)} violation(s): "
         + "; ".join(str(tuple(r)) for r in fk[:5]))

    # --- schema version ---
    version = db_mod.schema_version(conn)
    if version != SCHEMA_VERSION:
        _add("fail", "schema_version",
             f"database is v{version}, code expects v{SCHEMA_VERSION} — "
             f"run: python -m lifecrm migrate")
    else:
        _add("ok", "schema_version")
    try:
        pending = db_mod.pending_migrations(conn)
        _add("ok" if not pending else "fail", "pending_migrations",
             "" if not pending else ", ".join(p.name for _, p in pending))
    except db_mod.MigrationError as e:
        _add("fail", "pending_migrations", str(e))

    # --- exactly one primary category (schema.sql says outright this is
    # domain-enforced with only a partial backstop — a bug there is silent
    # everywhere but here) ---
    _rows("one_primary_category",
          "SELECT e.id, e.uuid FROM entries e LEFT JOIN "
          "(SELECT entry_id, SUM(is_primary) s FROM entry_categories "
          " GROUP BY entry_id) ec ON ec.entry_id=e.id "
          "WHERE COALESCE(ec.s, 0) <> 1",
          "entr(y/ies) without exactly one primary category")

    # --- FTS actually in sync: the index verified against the content
    # tables, not the triggers trusted ---
    for fts in ("entries_fts", "transcripts_fts"):
        try:
            conn.execute(f"INSERT INTO {fts}({fts}, rank) "
                         f"VALUES('integrity-check', 1)")
            _add("ok", f"fts_sync_{fts}")
        except sqlite3.DatabaseError as e:
            _add("fail", f"fts_sync_{fts}", f"index does not match content: {e}")

    # --- provenance chain: entry -> source -> transcript -> segments ---
    # A confirmed entry may be sourced from a transcript passage (the /log
    # path) or from an imported observation (v4). Either is provenance;
    # neither is optional.
    _rows("entries_have_provenance",
          "SELECT e.id, e.uuid FROM entries e WHERE NOT EXISTS "
          "(SELECT 1 FROM entry_sources es WHERE es.entry_id=e.id) "
          "AND NOT EXISTS "
          "(SELECT 1 FROM entry_observations eo WHERE eo.entry_id=e.id)",
          "entr(y/ies) with no provenance row at all")
    _rows("sources_have_transcripts",
          "SELECT s.id, s.uuid FROM sources s WHERE NOT EXISTS "
          "(SELECT 1 FROM transcripts t WHERE t.source_id=s.id)",
          "source(s) without a transcript")
    _rows("transcripts_have_segments",
          "SELECT t.id FROM transcripts t WHERE NOT EXISTS "
          "(SELECT 1 FROM transcript_segments g WHERE g.transcript_id=t.id)",
          "transcript(s) without segments")

    # --- referential sanity beyond FKs ---
    _rows("superseded_consistency",
          "SELECT id, uuid FROM entries WHERE "
          "(status='superseded') <> (superseded_by IS NOT NULL)",
          "entr(y/ies) where status and superseded_by disagree")
    _rows("no_mentions_on_merged_entities",
          "SELECT m.entry_id, m.entity_id FROM mentions m "
          "JOIN entities e ON e.id=m.entity_id WHERE e.status='merged'",
          "mention(s) pointing at merged entities")
    _rows("measurement_owner_xor",
          "SELECT id, uuid FROM measurements WHERE "
          "(entry_id IS NULL) = (journal_date IS NULL)",
          "measurement(s) violating the exactly-one-owner rule")

    # --- v2 invariants ---
    _rows("closure_pairing",
          "SELECT id, uuid FROM entries WHERE (closed_at IS NULL) <> (closure IS NULL)",
          "entr(y/ies) with closed_at/closure not set together")
    _rows("run_sources_consistent",
          "SELECT es.entry_id, es.source_id, es.extraction_run_id "
          "FROM entry_sources es WHERE es.extraction_run_id IS NOT NULL "
          "AND NOT EXISTS (SELECT 1 FROM extraction_run_sources rs "
          "WHERE rs.extraction_run_id=es.extraction_run_id "
          "AND rs.source_id=es.source_id)",
          "entry_sources row(s) whose run does not list their source")
    _rows("links_resolve",
          "SELECT l.id FROM entry_links l WHERE "
          "NOT EXISTS (SELECT 1 FROM entries e WHERE e.uuid=l.from_uuid) "
          "OR NOT EXISTS (SELECT 1 FROM entries e WHERE e.uuid=l.to_uuid)",
          "link(s) pointing at nonexistent entries")

    # --- warnings: operational drift, not corruption ---
    if cfg is not None:
        newest_backup = None
        for d in (cfg.backups_dir, cfg.onedrive_backups_dir):
            if d and d.exists():
                for f in d.glob("lifecrm-*.db"):
                    ts = f.stat().st_mtime
                    if newest_backup is None or ts > newest_backup:
                        newest_backup = ts
        if newest_backup is None:
            _add("warn", "backup_age", "no snapshots found — run: "
                 "python -m lifecrm backup")
        else:
            age_days = (time.time() - newest_backup) / 86400
            if age_days > BACKUP_MAX_AGE_DAYS:
                _add("warn", "backup_age", f"newest snapshot is {age_days:.0f} "
                     f"days old")
            else:
                _add("ok", "backup_age")

        stale = []
        if cfg.staging_dir.exists():
            cutoff = time.time() - STUCK_DRAFT_DAYS * 86400
            stale = [p.name for p in cfg.staging_dir.glob("*.json")
                     if p.stat().st_mtime < cutoff]
        if stale:
            _add("warn", "staging_drafts",
                 f"{len(stale)} file(s) older than {STUCK_DRAFT_DAYS} days in "
                 f"staging: {', '.join(stale[:5])}")
        else:
            _add("ok", "staging_drafts")

    stuck = conn.execute(
        "SELECT COUNT(*) c FROM extraction_runs WHERE status='draft' "
        "AND started_at < ?",
        ((datetime.now(timezone.utc)
          .strftime("%Y-%m-%dT%H:%M:%SZ")[:10]),)).fetchone()["c"]
    # started_at is a full timestamp; comparing against today's date string
    # flags anything started before today — close enough for a nudge.
    if stuck:
        _add("warn", "stuck_drafts",
             f"{stuck} extraction run(s) still in draft from before today — "
             f"apply or discard them")
    else:
        _add("ok", "stuck_drafts")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    overdue = conn.execute(
        "SELECT COUNT(*) c FROM entries e JOIN entry_kinds k ON k.id=e.kind_id "
        "WHERE k.name IN ('task','plan') AND e.status='active' "
        "AND e.closed_at IS NULL AND e.due_at IS NOT NULL "
        "AND substr(e.due_at, 1, 10) < ?", (today,)).fetchone()["c"]
    if overdue:
        _add("warn", "overdue_loops", f"{overdue} open loop(s) past due — "
             f"see: python -m lifecrm status")
    else:
        _add("ok", "overdue_loops")

    # --- health imports gone quiet: a broken pipeline looks exactly like a
    # healthy one unless something watches the clock. Only meaningful once the
    # feature is in use (any active rows at all).
    try:
        hr = conn.execute("SELECT COUNT(*) c, MAX(journal_date) latest "
                          "FROM health_records WHERE status='active'").fetchone()
        if hr["c"]:
            stale_cutoff = (datetime.now(timezone.utc)
                            - timedelta(days=HEALTH_STALE_DAYS)).strftime("%Y-%m-%d")
            if hr["latest"] < stale_cutoff:
                _add("warn", "health_data_age",
                     f"latest health record is {hr['latest']} — no import in "
                     f"over {HEALTH_STALE_DAYS} days")
            else:
                _add("ok", "health_data_age")
    except sqlite3.OperationalError:
        pass   # pre-v3 schema; pending_migrations already failed above

    # --- observed tier (v4) ---
    try:
        _rows("observations_import_live",
              "SELECT o.uuid FROM observations o JOIN imports i ON i.id=o.import_id "
              "WHERE o.status='active' AND i.status='retracted'",
              "active observation(s) belonging to a retracted import")
        _rows("handles_resolve",
              "SELECT h.kind, h.value FROM entity_handles h "
              "WHERE h.entity_id IS NOT NULL AND NOT EXISTS "
              "(SELECT 1 FROM entities e WHERE e.id=h.entity_id "
              "AND e.status='active')",
              "handle(s) resolving to a missing or merged entity")

        stuck = conn.execute(
            "SELECT COUNT(*) c FROM imports WHERE status='running' "
            "AND started_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=STUCK_DRAFT_DAYS))
             .strftime("%Y-%m-%dT%H:%M:%SZ"),)).fetchone()["c"]
        if stuck:
            _add("warn", "stuck_imports",
                 f"{stuck} import(s) started over {STUCK_DRAFT_DAYS} days ago and "
                 f"never finished — see: python -m lifecrm imports list")
        else:
            _add("ok", "stuck_imports")

        # Unresolved handles are not corruption — they are the review queue.
        # Worth surfacing because an observation attributed to nobody answers
        # none of the questions the observed tier exists to answer.
        unresolved = conn.execute(
            "SELECT COUNT(*) c FROM entity_handles WHERE entity_id IS NULL "
            "AND ignored=0 AND observation_count > 0").fetchone()["c"]
        if unresolved:
            _add("warn", "unresolved_handles",
                 f"{unresolved} handle(s) seen in imports but not linked to an "
                 f"entity — see: python -m lifecrm handles list")
        else:
            _add("ok", "unresolved_handles")
    except sqlite3.OperationalError:
        pass   # pre-v4 schema; pending_migrations already failed above

    failures = sum(1 for c in checks if c["status"] == "fail")
    warnings = sum(1 for c in checks if c["status"] == "warn")
    return {"checks": checks, "failures": failures, "warnings": warnings,
            "healthy": failures == 0}

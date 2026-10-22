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
INBOX_STUCK_DAYS = 2
# System litter the phone, OneDrive or Finder leaves beside real files
# ('Icon\r' is a folder's custom-icon file; the CR is part of its name).
_INBOX_LITTER = {"desktop.ini", "thumbs.db", ".ds_store", "icon\r"}


def _capture_checks(cfg: Config, _add) -> None:
    """The capture side. A typo in an inbox path, a recording intake fails on
    every run, and a format nothing reads all look exactly like a quiet day —
    no error anywhere, just nothing arriving. These make them visible."""
    from .transcribe import AUDIO_EXTS, TEXT_EXTS, icloud_stub
    folders = [("inbox_dir", cfg.inbox_dir, AUDIO_EXTS),
               ("inbox_text_dir", cfg.inbox_text_dir, TEXT_EXTS),
               ("archive_dir", cfg.archive_dir, None)]

    missing = [f"{name} = {path}" for name, path, _ in folders
               if path is not None and not path.is_dir()]
    if missing:
        _add("warn", "configured_dirs",
             f"configured folder(s) do not exist: {'; '.join(missing)} — a typo "
             f"here means nothing ever arrives; fix config.toml or create them")
    else:
        _add("ok", "configured_dirs")

    cutoff = time.time() - INBOX_STUCK_DAYS * 86400
    stuck, unknown, in_cloud = [], [], []
    for name, path, exts in folders:
        if exts is None or path is None or not path.is_dir():
            continue
        for p in sorted(path.iterdir()):
            # A hidden '.x.m4a.icloud' stub is a note iCloud has not put on
            # this disk: skipped with the other dotfiles, it would read as a
            # quiet day.
            target = icloud_stub(p, exts)
            if target:
                in_cloud.append(f"{name}/{target}")
                continue
            if not p.is_file() or p.name.lower() in _INBOX_LITTER \
                    or p.name.startswith((".", "~")):
                continue
            if p.suffix.lower() not in exts:
                unknown.append(f"{name}/{p.name}")
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    stuck.append(f"{name}/{p.name}")
            except OSError:
                continue
    problems = []
    if stuck:
        problems.append(
            f"{len(stuck)} file(s) waiting over {INBOX_STUCK_DAYS} days — intake "
            f"is failing on them or not running: {', '.join(stuck[:5])}"
            + (" …" if len(stuck) > 5 else "")
            + " — see data/transcribe.log")
    if in_cloud:
        problems.append(
            f"{len(in_cloud)} note(s) still in iCloud — open the VoiceJournal "
            f"folder in Finder or set it to Keep Downloaded: "
            f"{', '.join(in_cloud[:5])}" + (" …" if len(in_cloud) > 5 else ""))
    if problems:
        _add("warn", "inbox_stuck", "; ".join(problems))
    else:
        _add("ok", "inbox_stuck")
    if unknown:
        _add("warn", "inbox_unrecognized",
             f"{len(unknown)} file(s) intake will never read (unrecognized type): "
             f"{', '.join(unknown[:5])}" + (" …" if len(unknown) > 5 else "")
             + f" — audio: {', '.join(sorted(AUDIO_EXTS))}; "
               f"text: {', '.join(sorted(TEXT_EXTS))}")
    else:
        _add("ok", "inbox_unrecognized")


def _health_multi_origin(conn: sqlite3.Connection, _add) -> None:
    """Two apps recording the same metric on the same day — Samsung Health and
    Google Fit both counting steps, a phone and a watch both logging sleep.
    Dashboards and `ask` SUM a day's records, so every such day reads double.
    Not corruption, so a warning; it persists until one writer is gone."""
    from .health_connect import AVERAGED_METRICS
    marks = ",".join("?" for _ in AVERAGED_METRICS)
    rows = conn.execute(
        f"SELECT metric, COUNT(*) days, "
        f"(SELECT group_concat(origin_app, ', ') FROM (SELECT DISTINCT origin_app "
        f" FROM health_records h2 WHERE h2.metric=d.metric AND h2.status='active' "
        f" AND h2.origin_app IS NOT NULL ORDER BY origin_app)) apps "
        f"FROM (SELECT metric, journal_date FROM health_records "
        f"      WHERE status='active' AND origin_app IS NOT NULL "
        f"      AND metric NOT IN ({marks}) "
        f"      GROUP BY metric, journal_date "
        f"      HAVING COUNT(DISTINCT origin_app) > 1) d "
        f"GROUP BY metric ORDER BY metric", sorted(AVERAGED_METRICS)).fetchall()
    if rows:
        _add("warn", "health_multi_origin",
             "; ".join(f"{r['metric']}: {r['apps']} on {r['days']} day(s)"
                       for r in rows)
             + " — more than one app wrote these, so daily totals double-count; "
               "see docs/health-connect.md (first-import checklist)")
    else:
        _add("ok", "health_multi_origin")


def _snapshot_age_days(folder: Path | None) -> float | None:
    """Age of the newest lifecrm-*.db in folder, or None when there is none."""
    if folder is None or not folder.is_dir():
        return None
    times = []
    for f in folder.glob("lifecrm-*.db"):
        try:
            times.append(f.stat().st_mtime)
        except OSError:
            continue        # a cloud placeholder mid-sync; the others still count
    return (time.time() - max(times)) / 86400 if times else None


def _backup_checks(cfg: Config, _add) -> None:
    """Local and cloud snapshot age, checked separately. Taking the newest
    across both folders let a dead cloud copy hide behind a fresh local one —
    and the cloud copy is the one that survives losing the laptop."""
    age = _snapshot_age_days(cfg.backups_dir)
    if age is None:
        _add("warn", "backup_age", f"no snapshots in {cfg.backups_dir} — run: "
             f"python -m lifecrm backup")
    elif age > BACKUP_MAX_AGE_DAYS:
        _add("warn", "backup_age", f"newest local snapshot is {age:.0f} days "
             f"old — is the scheduled backup running?")
    else:
        _add("ok", "backup_age")

    cloud = cfg.cloud_backups_dir
    if cloud is None:
        return                       # local-only by choice: nothing to check
    age = _snapshot_age_days(cloud)
    if age is None:
        _add("warn", "backup_age_cloud",
             f"no snapshots in the cloud copy folder {cloud} — backup copies "
             f"every snapshot there; check backup's WARNING line and that the "
             f"folder exists and syncs")
    elif age > BACKUP_MAX_AGE_DAYS:
        _add("warn", "backup_age_cloud",
             f"newest cloud copy is {age:.0f} days old — the copy step is "
             f"failing or the folder moved; see backup's WARNING line")
    else:
        _add("ok", "backup_age_cloud")


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
    fts_tables = ["entries_fts", "transcripts_fts"]
    # observations_fts arrived with v4. A pre-v4 database has no index to
    # check, and already fails pending_migrations above.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='observations_fts'").fetchone():
        fts_tables.append("observations_fts")
    for fts in fts_tables:
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
        _backup_checks(cfg, _add)

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

        _capture_checks(cfg, _add)

    stuck = [r["uuid"][:8] for r in conn.execute(
        "SELECT uuid FROM extraction_runs WHERE status='draft' "
        "AND started_at < ? ORDER BY id",
        ((datetime.now(timezone.utc)
          .strftime("%Y-%m-%dT%H:%M:%SZ")[:10]),))]
    # started_at is a full timestamp; comparing against today's date string
    # flags anything started before today — close enough for a nudge.
    if stuck:
        _add("warn", "stuck_drafts",
             f"{len(stuck)} extraction run(s) still in draft from before today: "
             f"{', '.join(stuck[:5])}" + (" …" if len(stuck) > 5 else "")
             + f" — apply the diff (data/staging/diff-<date>-<run>.json) or "
               f"discard the run: python -m lifecrm admin discard-run {stuck[0]}")
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
            _health_multi_origin(conn, _add)
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

        # A snapshot connector that stops being fed looks exactly like a
        # healthy one. Connectors that expect a steady flow declare how stale
        # is too stale; the check applies once the connector is in use, and
        # measures the newest record, not the newest run — re-importing an old
        # backup does not make the data fresh.
        from .connectors import base as conn_base
        for cname, c in sorted(conn_base.registry().items()):
            limit = getattr(c, "stale_after_days", None)
            if not limit:
                continue
            marks = ",".join("?" for _ in c.streams)
            latest = conn.execute(
                f"SELECT MAX(started_at) latest FROM observations "
                f"WHERE status='active' AND stream IN ({marks})",
                c.streams).fetchone()["latest"]
            if latest is None:
                continue
            cutoff = (datetime.now(timezone.utc) - timedelta(days=limit)
                      ).strftime("%Y-%m-%dT%H:%M:%SZ")
            if latest < cutoff:
                _add("warn", f"{cname}_data_age",
                     f"newest {cname} record is {latest[:10]} — nothing newer in "
                     f"over {limit} days; is the backup/export still running?")
            else:
                _add("ok", f"{cname}_data_age")
    except sqlite3.OperationalError:
        pass   # pre-v4 schema; pending_migrations already failed above

    try:
        _relationship_checks(conn, _add, _rows)
        _hidden_citation_check(conn, _add)
    except sqlite3.OperationalError:
        pass   # pre-v5 schema; pending_migrations already failed above

    failures = sum(1 for c in checks if c["status"] == "fail")
    warnings = sum(1 for c in checks if c["status"] == "warn")
    return {"checks": checks, "failures": failures, "warnings": warnings,
            "healthy": failures == 0}


def _hidden_citation_check(conn: sqlite3.Connection, _add) -> None:
    """A confirmed record citing an observation nobody can see any more.

    Mirroring hides a cited record instead of deleting it (deleted at the
    source), and a retracted import hides its rows; either way the entry or
    the relationship still stands while its provenance is out of sight —
    `ask` and every view skip the row. Not corruption: the citation is intact
    and a row mirroring hid comes back when the source holds it again. But
    whether the record still stands is the owner's call, and until this
    check nothing said it had happened."""
    entries = [r["uuid"] for r in conn.execute(
        "SELECT DISTINCT e.uuid FROM entry_observations eo "
        "JOIN entries e ON e.id=eo.entry_id "
        "JOIN observations o ON o.id=eo.observation_id "
        "WHERE e.status='active' AND o.status='retracted' ORDER BY e.uuid")]
    links = [r["uuid"] for r in conn.execute(
        "SELECT l.uuid FROM entity_links l "
        "JOIN observations o ON o.uuid=l.observation_uuid "
        "WHERE l.status='active' AND o.status='retracted' ORDER BY l.uuid")]
    if not entries and not links:
        _add("ok", "cited_observations_hidden")
        return
    found = ([f"entry {u[:8]}" for u in entries[:3]]
             + [f"relationship {u[:8]}" for u in links[:3]])
    _add("warn", "cited_observations_hidden",
         f"{len(entries)} active entr(y/ies) and {len(links)} active "
         f"relationship(s) cite an observation that is now hidden — deleted at "
         f"the source (kept because it is cited) or its import retracted: "
         f"{', '.join(found)} — check them: python -m lifecrm entry show <uuid> / "
         f"python -m lifecrm entities link list; a record deleted at the source "
         f"comes back when it is there again")


def _relationship_checks(conn: sqlite3.Connection, _add, _rows) -> None:
    """Entity links (v5). The schema carries the self-link CHECK and active
    uniqueness; what it cannot carry — live ends, provenance on what an
    extractor proposed, canonical order for symmetric types, one row per
    inverse fact — the domain layer enforces and this verifies."""
    _rows("entity_links_resolve",
          "SELECT l.uuid FROM entity_links l WHERE l.status='active' AND ("
          "NOT EXISTS (SELECT 1 FROM entities e WHERE e.id=l.from_entity_id "
          "AND e.status='active') OR "
          "NOT EXISTS (SELECT 1 FROM entities e WHERE e.id=l.to_entity_id "
          "AND e.status='active'))",
          "active relationship(s) pointing at a missing or merged entity")
    _rows("entity_links_no_self",
          "SELECT uuid FROM entity_links WHERE from_entity_id = to_entity_id",
          "relationship(s) connecting an entity to itself")
    _rows("entity_links_provenance",
          "SELECT uuid FROM entity_links WHERE actor='extraction' "
          "AND char_start IS NULL AND entry_uuid IS NULL "
          "AND observation_uuid IS NULL",
          "extracted relationship(s) citing no passage, entry or observation")
    _rows("entity_links_canonical",
          "SELECT l.uuid FROM entity_links l "
          "JOIN entity_link_types t ON t.name=l.type "
          "WHERE t.symmetric=1 AND l.from_entity_id > l.to_entity_id",
          "symmetric relationship(s) stored out of canonical order — a "
          "duplicate could slip past the uniqueness index")
    _rows("entity_links_inverse_once",
          "SELECT a.uuid, b.uuid FROM entity_links a "
          "JOIN entity_link_types t ON t.name=a.type "
          "JOIN entity_links b ON b.type=t.inverse "
          "AND b.from_entity_id=a.to_entity_id AND b.to_entity_id=a.from_entity_id "
          "WHERE a.status='active' AND b.status='active' AND a.type < b.type",
          "fact(s) stored as both halves of an inverse pair")
    _rows("entity_link_types_inverse",
          "SELECT a.name FROM entity_link_types a "
          "JOIN entity_link_types b ON b.name=a.inverse "
          "WHERE b.inverse IS NOT a.name OR a.symmetric=1 OR b.symmetric=1",
          "relationship type(s) whose inverse does not point back")

    # A retracted entry ("this record was wrong") may have carried a wrong
    # relationship with it. Retracting the link too is the owner's call.
    n = conn.execute(
        "SELECT COUNT(*) c FROM entity_links l JOIN entries e ON e.uuid=l.entry_uuid "
        "WHERE l.status='active' AND e.status='retracted'").fetchone()["c"]
    if n:
        _add("warn", "entity_links_cite_retracted",
             f"{n} active relationship(s) came with an entry since retracted — "
             f"check them: python -m lifecrm entities link list")
    else:
        _add("ok", "entity_links_cite_retracted")
